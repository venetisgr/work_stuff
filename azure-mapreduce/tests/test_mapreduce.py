"""MapReduce's public API: settings, prompts, the map step on pandas, the recursive reduce, run() and the bars."""

from __future__ import annotations

import asyncio
import contextlib
import decimal
import fractions
import gc
import inspect
import io
import json
import logging
import math
import re
import signal
import sys

import numpy as np
import pandas as pd
import pytest

from azure_mapreduce import MapReduce, MapReduceResult, ReduceResult, reduce_levels
from azure_mapreduce import mapreduce as mapreduce_module
from azure_mapreduce import progress as progress_module
from azure_mapreduce.errors import ConfigError, LLMRequestError, LLMSetupError, MapReduceError, StepFailedError
from azure_mapreduce.frames import PandasFrame, as_text
from azure_mapreduce.mapreduce import make_groups

from .conftest import FakeClient, FakeClock, content_of

# --- helpers ---------------------------------------------------------------------------------------------


def bracket(messages: list[dict]) -> str:
    """Reply with the prompt minus its two-character prefix ("M:", "R:", "C:") in brackets, so nesting shows groups."""
    return f"[{content_of(messages)[2:]}]"


def fail_on(*needles: str, retryable: bool = False, message: str = "blocked"):
    """A responder that fails every prompt containing one of ``needles`` and brackets the rest."""

    def responder(messages: list[dict]) -> str:
        if any(needle in content_of(messages) for needle in needles):
            raise LLMRequestError(message, retryable=retryable)
        return bracket(messages)

    return responder


def make(client: FakeClient, **options) -> MapReduce:
    """A MapReduce with short prompts, the loop strategy, "|" between texts and hidden bars, unless overridden."""
    settings = {
        "map_prompt": "M:{text}",
        "reduce_prompt": "R:{text}",
        "strategies": ("sync",),
        "separator": "|",
        "show_progress": False,
    }
    settings.update(options)
    return MapReduce(client, **settings)


def make_batch(client: FakeClient, clock, **options) -> MapReduce:
    """A MapReduce on the Batch API only, polling on the fake clock."""
    settings = {"strategies": ("batch",), "batch_poll_interval": 1.0, "sleep": clock.sleep, "clock": clock}
    settings.update(options)
    return make(client, **settings)


def frame(texts, **columns) -> pd.DataFrame:
    return pd.DataFrame({"text": pd.Series(list(texts), dtype=object), **columns})


def contents(client: FakeClient, route: str = "sync") -> list[str]:
    return [content_of(messages) for messages in client.calls[route]]


def job_sizes(client: FakeClient) -> list[tuple[str, int]]:
    """Each batch job in submission order: the prompt prefix of its requests and how many it held."""
    return [(job.lines[0]["body"]["messages"][-1]["content"][0], len(job.lines)) for job in client.jobs.values()]


def warnings_in(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]


def level_sizes(count: int, group_size: int) -> list[int]:
    """How many texts each level of the reduce should hold, computed independently of the package."""
    sizes = [count]
    while True:
        count = -(-count // group_size)
        sizes.append(count)
        if count == 1:
            return sizes


@contextlib.contextmanager
def time_limit(seconds: float):
    """Raise TimeoutError in the test if the block runs longer than ``seconds`` (so a hang fails instead)."""

    def expire(signum, frame):
        raise TimeoutError(f"still running after {seconds}s")

    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


@pytest.fixture
def bars(monkeypatch):
    """Every tqdm bar the package creates, in creation order (they still render, to the captured stderr)."""
    created = []

    class RecordingTqdm(progress_module.tqdm):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.shown = not self.disable  # close() sets disable, so remember it now
            created.append(self)

    monkeypatch.setattr(progress_module, "tqdm", RecordingTqdm)
    return created


# --- constructor validation ------------------------------------------------------------------------------


def test_valid_settings_are_accepted_and_nothing_is_sent_yet():
    client = FakeClient()
    mr = make(client, collapse_prompt="C:{text}", reduce_group_size=2, map_batch_size=1, batch_timeout=0.5)
    assert mr.reduce_group_size == 2
    assert client.calls == {"sync": [], "async": [], "batch": []}
    assert client.files == {} and client.async_sessions == 0


def test_defaults_collapse_prompt_to_the_reduce_prompt_and_reduce_batch_size_to_the_map_batch_size():
    mr = make(FakeClient(), map_batch_size=7)
    assert mr.collapse_prompt == "R:{text}"
    assert mr.reduce_batch_size == 7
    assert make(FakeClient(), map_batch_size=7, reduce_batch_size=3).reduce_batch_size == 3


@pytest.mark.parametrize("name", ["map_prompt", "reduce_prompt", "collapse_prompt"])
def test_string_prompt_without_the_placeholder_is_rejected(name):
    with pytest.raises(ConfigError, match=rf"{name} needs a \{{text\}} placeholder"):
        make(FakeClient(), **{name: "Summarize the text."})


@pytest.mark.parametrize("prompt", [42, b"M:{text}", ["M:{text}"], ("M:{text}",)])
@pytest.mark.parametrize("name", ["map_prompt", "reduce_prompt", "collapse_prompt"])
def test_prompt_that_is_neither_a_string_nor_a_function_is_rejected(name, prompt):
    with pytest.raises(ConfigError, match=f"{name} must be a string or a function"):
        make(FakeClient(), **{name: prompt})


@pytest.mark.parametrize("name", ["map_prompt", "reduce_prompt"])
def test_required_prompt_given_as_none_is_rejected(name):
    with pytest.raises(ConfigError, match=name):
        make(FakeClient(), **{name: None})


def test_custom_placeholder_is_what_the_prompts_must_contain():
    with pytest.raises(ConfigError, match="<<TEXT>>"):
        make(FakeClient(), placeholder="<<TEXT>>")  # the default prompts only have {text}
    make(FakeClient(), placeholder="<<TEXT>>", map_prompt="M:<<TEXT>>", reduce_prompt="R:<<TEXT>>")


@pytest.mark.parametrize("placeholder", ["", None, 5, b"{text}", ["{text}"]])
def test_empty_or_missing_placeholder_is_rejected(placeholder):
    client = FakeClient()
    with pytest.raises(ConfigError, match="placeholder must be a non-empty string"):
        make(client, placeholder=placeholder, map_prompt="Summarize: ", reduce_prompt="Combine: ")
    assert client.calls == {"sync": [], "async": [], "batch": []}


@pytest.mark.parametrize("size", [1, 0, -3, True, False, 2.0, 10.5, "10", None])
def test_reduce_group_size_below_two_or_not_an_int_is_rejected(size):
    with pytest.raises(ConfigError, match="reduce_group_size must be 2 or more"):
        make(FakeClient(), reduce_group_size=size)


@pytest.mark.parametrize("value", [0, -1, 1.5, 2.0, True, False, "10", None])
@pytest.mark.parametrize("name", ["map_batch_size", "max_concurrency", "max_concurrent_batch_jobs"])
def test_counts_must_be_whole_numbers_of_at_least_one(name, value):
    with pytest.raises(ConfigError, match=f"{name} must be a whole number of at least 1"):
        make(FakeClient(), **{name: value})


@pytest.mark.parametrize("value", [0, -1, 1.5, True, False, "10"])
def test_reduce_batch_size_must_be_a_whole_number_of_at_least_one_or_none(value):
    with pytest.raises(ConfigError, match="reduce_batch_size must be a whole number of at least 1"):
        make(FakeClient(), reduce_batch_size=value)


@pytest.mark.parametrize("value", [0, 0.0, -1, -0.5])
def test_batch_poll_interval_must_be_positive(value):
    with pytest.raises(ConfigError, match="batch_poll_interval must be a number of seconds greater than zero"):
        make(FakeClient(), batch_poll_interval=value)


@pytest.mark.parametrize("value", [0, 0.0, -5])
def test_batch_timeout_must_be_positive_or_none(value):
    with pytest.raises(
        ConfigError,
        match=re.escape("batch_timeout must be a number of seconds greater than zero, or None to wait as long as it"),
    ):
        make(FakeClient(), batch_timeout=value)
    make(FakeClient(), batch_timeout=None)
    make(FakeClient(), batch_timeout=0.25, batch_poll_interval=0.01)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("batch_poll_interval", "30"),
        ("batch_poll_interval", None),
        ("batch_poll_interval", math.nan),
        ("batch_timeout", "60"),
        ("batch_timeout", math.nan),
    ],
)
def test_batch_poll_interval_and_timeout_of_the_wrong_type_are_config_errors(name, value):
    with pytest.raises(ConfigError, match=f"{name} must be a number of seconds"):
        make(FakeClient(), **{name: value})


@pytest.mark.parametrize("value", ["ignore", "WARN", "", None, True])
def test_on_error_must_be_warn_or_raise(value):
    with pytest.raises(ConfigError, match="on_error must be"):
        make(FakeClient(), on_error=value)


@pytest.mark.parametrize(
    "strategies", [("batch", "foo"), ("Sync",), ("sync", "sync"), ("batch", "async", "batch"), (), "sync"]
)
def test_unknown_repeated_or_missing_strategies_are_rejected_up_front(strategies):
    client = FakeClient()
    with pytest.raises(ConfigError):
        make(client, strategies=strategies)
    assert client.calls == {"sync": [], "async": [], "batch": []}


@pytest.mark.parametrize("strategies", [("sync",), ["async", "sync"], ("batch", "async", "sync"), ("sync", "batch")])
def test_any_order_of_known_strategies_is_accepted(strategies):
    make(FakeClient(), strategies=strategies)


# --- prompts ---------------------------------------------------------------------------------------------


def test_placeholder_replacement_leaves_other_braces_alone():
    client = FakeClient()
    prompt = 'Reply as JSON like {"a": 1, "b": {"c": [2]}} or {0} or {} for: {text}'
    make(client, map_prompt=prompt).map_texts(["hello"])
    assert contents(client) == ['Reply as JSON like {"a": 1, "b": {"c": [2]}} or {0} or {} for: hello']


def test_every_occurrence_of_the_placeholder_is_replaced():
    client = FakeClient()
    make(client, map_prompt="{text} -- again: {text}").map_texts(["hi"])
    assert contents(client) == ["hi -- again: hi"]


def test_braces_and_placeholders_inside_the_text_are_sent_verbatim():
    client = FakeClient()
    make(client).map_texts(['{text} {"k": 1} {0} {'])
    assert contents(client) == ['M:{text} {"k": 1} {0} {']


def test_custom_placeholder_is_replaced_and_literal_text_braces_are_kept():
    client = FakeClient(responder=lambda messages: "out")
    mr = make(
        client,
        placeholder="<<TEXT>>",
        map_prompt='Keep {text} and {"x": 1} literal: <<TEXT>>',
        reduce_prompt="Combine {text}: <<TEXT>>",
    )
    mr.map_texts(["hello"])
    mr.reduce(["a", "b"])
    assert contents(client) == ['Keep {text} and {"x": 1} literal: hello', "Combine {text}: a|b"]


def test_callable_prompts_get_the_record_and_the_joined_group():
    seen = {"map": [], "reduce": [], "collapse": []}

    def prompt(kind, prefix):
        def build(text):
            seen[kind].append(text)
            return prefix + text

        return build

    client = FakeClient(responder=bracket)
    mr = make(
        client,
        map_prompt=prompt("map", "M:"),
        collapse_prompt=prompt("collapse", "C:"),
        reduce_prompt=prompt("reduce", "R:"),
        reduce_group_size=2,
    )
    result = mr.run(frame(["a", "b", "c"]), "text", "summary")
    assert seen == {"map": ["a", "b", "c"], "collapse": ["[a]|[b]", "[c]"], "reduce": ["[[a]|[b]]|[[c]]"]}
    assert result.output == "[[[a]|[b]]|[[c]]]"


def test_callable_prompt_needs_no_placeholder_even_with_a_custom_one():
    client = FakeClient()
    make(client, placeholder="<<X>>", map_prompt=str.upper, reduce_prompt=lambda text: text).map_texts(["abc"])
    assert contents(client) == ["ABC"]


def test_system_prompt_is_sent_first_with_every_map_and_reduce_request():
    client = FakeClient(responder=bracket)
    make(client, system_prompt="Be brief.", reduce_group_size=2).run(frame(["a", "b", "c"]), "text", "out")
    assert len(client.calls["sync"]) == 3 + 2 + 1
    for messages in client.calls["sync"]:
        assert messages[0] == {"role": "system", "content": "Be brief."}
        assert [message["role"] for message in messages] == ["system", "user"]


@pytest.mark.parametrize("system_prompt", [None, ""])
def test_without_a_system_prompt_only_the_user_message_is_sent(system_prompt):
    client = FakeClient()
    make(client, system_prompt=system_prompt).map_texts(["a"])
    assert client.calls["sync"] == [[{"role": "user", "content": "M:a"}]]


# --- map on pandas ---------------------------------------------------------------------------------------


def test_map_returns_a_new_dataframe_and_leaves_the_input_alone():
    client = FakeClient(responder=bracket)
    df = frame(["a", "b"], n=[1, 2])
    before = df.copy()
    out = make(client).map(df, "text", "summary")
    assert out is not df
    pd.testing.assert_frame_equal(df, before)
    assert list(df.columns) == ["text", "n"]
    assert list(out.columns) == ["text", "n", "summary"]
    assert out["summary"].tolist() == ["[a]", "[b]"]
    assert out["summary"].dtype == object
    pd.testing.assert_frame_equal(out[["text", "n"]], before)


@pytest.mark.parametrize(
    "index",
    [
        pd.Index(["x", "y", "z"]),
        pd.Index([10, 3, 7]),
        pd.Index(["dup", "dup", "other"]),
        pd.date_range("2024-01-01", periods=3),
        pd.MultiIndex.from_tuples([("a", 1), ("a", 2), ("b", 1)]),
    ],
)
def test_map_keeps_the_index_and_lines_outputs_up_by_position(index):
    client = FakeClient(responder=bracket)
    df = pd.DataFrame({"text": ["first", "second", "third"]}, index=index)
    out = make(client).map(df, "text", "summary", error_column="error")
    assert out.index.equals(df.index)
    assert out["summary"].tolist() == ["[first]", "[second]", "[third]"]
    assert out["error"].tolist() == [None, None, None]


def test_map_on_a_filtered_frame_with_gaps_in_the_index():
    client = FakeClient(responder=bracket)
    df = frame(["a", "b", "c", "d", "e"], keep=[True, False, True, False, True])
    subset = df[df["keep"]]
    out = make(client).map(subset, "text", "summary")
    assert out.index.tolist() == [0, 2, 4]
    assert out["summary"].to_dict() == {0: "[a]", 2: "[c]", 4: "[e]"}


def test_map_skips_none_nan_na_nat_and_blank_cells_without_a_request(caplog):
    caplog.set_level(logging.INFO, logger="azure_mapreduce")
    client = FakeClient(responder=bracket)
    values = ["a", None, math.nan, np.nan, pd.NA, pd.NaT, "", "   ", "\n\t ", b"", b"  ", "b"]
    out = make(client).map(frame(values), "text", "summary", error_column="error")
    assert contents(client) == ["M:a", "M:b"]
    assert out["summary"].tolist() == ["[a]"] + [None] * 10 + ["[b]"]
    assert all(value is None for value in out["summary"].iloc[1:-1])
    assert out["error"].tolist() == [None] * 12
    assert "Skipping 10 empty records." in caplog.text
    assert warnings_in(caplog) == []


@pytest.mark.parametrize("dtype", [object, "str", "string", "string[pyarrow]"])
def test_map_skips_missing_and_blank_cells_in_every_string_dtype(dtype):
    if "pyarrow" in str(dtype):
        pytest.importorskip("pyarrow")
    client = FakeClient(responder=bracket)
    df = pd.DataFrame({"text": pd.Series(["a", None, "", "  \n", "b"], dtype=dtype)})
    out = make(client).map(df, "text", "summary")
    assert contents(client) == ["M:a", "M:b"]
    assert out["summary"].tolist() == ["[a]", None, None, None, "[b]"]


@pytest.mark.parametrize(
    ("series", "sent"),
    [
        (pd.Series([1.5, np.nan, 3.0]), ["M:1.5", "M:3.0"]),
        (pd.Series([1, None, 3], dtype="Int64"), ["M:1", "M:3"]),
        (pd.Series([1.5, np.nan], dtype="float32"), ["M:1.5"]),
        (pd.Series(pd.to_datetime(["2024-01-02", None])), ["M:2024-01-02 00:00:00"]),
        (pd.Series(["x", None, "y"], dtype="category"), ["M:x", "M:y"]),
    ],
)
def test_map_skips_missing_values_in_non_string_dtypes(series, sent):
    client = FakeClient()
    out = make(client).map(pd.DataFrame({"text": series}), "text", "summary")
    assert contents(client) == sent
    assert out["summary"].isna().sum() == len(series) - len(sent)


def test_map_skips_numpy_missing_scalars_in_an_object_column():
    client = FakeClient(responder=bracket)
    df = pd.DataFrame({"text": ["a review", np.float32("nan"), np.datetime64("NaT"), np.timedelta64("NaT")]})
    assert pd.isna(df["text"]).tolist() == [False, True, True, True]  # pandas itself calls them missing
    out = make(client).map(df, "text", "summary")
    assert contents(client) == ["M:a review"]
    assert out["summary"].tolist() == ["[a review]", None, None, None]


def test_map_sends_numbers_booleans_and_bytes_as_text():
    client = FakeClient(responder=bracket)
    values = [7, 2.5, True, 0, b"raw bytes", bytearray(b"array"), "café".encode(), b"\xff", b"   "]
    out = make(client).map(frame(values), "text", "summary")
    assert contents(client) == ["M:7", "M:2.5", "M:True", "M:0", "M:raw bytes", "M:array", "M:café", "M:\ufffd"]
    assert out["summary"].tolist()[-1] is None


def test_map_on_a_numeric_column():
    client = FakeClient()
    make(client).map(pd.DataFrame({"n": [3, 1, 2]}), "n", "out")
    assert contents(client) == ["M:3", "M:1", "M:2"]


def test_output_column_replaces_an_existing_column_in_place():
    client = FakeClient(responder=bracket)
    df = pd.DataFrame({"id": [1, 2], "summary": [0, 0], "text": ["a", "b"]})
    out = make(client).map(df, "text", "summary")
    assert list(out.columns) == ["id", "summary", "text"]
    assert out["summary"].tolist() == ["[a]", "[b]"]
    assert df["summary"].tolist() == [0, 0]


def test_output_column_can_replace_the_input_column_in_the_result_only():
    client = FakeClient(responder=bracket)
    df = frame(["a", None, "b"])
    out = make(client).map(df, "text", "text")
    assert list(out.columns) == ["text"]
    assert out["text"].tolist() == ["[a]", None, "[b]"]
    assert df["text"].tolist() == ["a", None, "b"]


@pytest.mark.parametrize("name", ["summary (gpt-4.1)", "1st", "kwargs", "with.dots"])
def test_output_column_can_be_any_string(name):
    out = make(FakeClient(responder=bracket)).map(frame(["a"]), "text", name)
    assert out[name].tolist() == ["[a]"]


@pytest.mark.parametrize("names", [("self", None), ("summary", "self")])
def test_output_and_error_columns_named_self_work(names):
    output_column, error_column = names
    client = FakeClient(responder=bracket)
    out = make(client).map(frame(["a"]), "text", output_column, error_column=error_column)
    assert out[output_column].tolist() == ["[a]"]


def test_error_column_holds_the_error_only_for_records_that_failed(caplog):
    client = FakeClient(responder=fail_on("bad"))
    out = make(client).map(frame(["good", "bad", None, "fine", "bad too"]), "text", "summary", error_column="why")
    assert out["summary"].tolist() == ["[good]", None, None, "[fine]", None]
    assert out["why"].tolist() == [None, "blocked", None, None, "blocked"]
    assert out["why"].dtype == object
    assert warnings_in(caplog) == ["2 of 4 records failed in the map: blocked (×2)"]


def test_error_column_is_added_only_when_asked_for():
    client = FakeClient(responder=fail_on("bad"))
    out = make(client).map(frame(["good", "bad"]), "text", "summary")
    assert list(out.columns) == ["text", "summary"]


def test_error_column_replaces_an_existing_column_in_place():
    client = FakeClient(responder=fail_on("bad"))
    df = pd.DataFrame({"error": ["old", "old"], "text": ["ok", "bad"]})
    out = make(client).map(df, "text", "summary", error_column="error")
    assert list(out.columns) == ["error", "text", "summary"]
    assert out["error"].tolist() == [None, "blocked"]


def test_error_column_with_the_output_column_name_is_rejected():
    client = FakeClient(responder=bracket)
    with pytest.raises(ConfigError, match="The output and error columns need different names"):
        make(client).map(frame(["a", "b"]), "text", "summary", error_column="summary")
    with pytest.raises(ConfigError, match="The output and error columns need different names"):
        make(client).run(frame(["a", "b"]), "text", "summary", error_column="summary")
    assert client.calls["sync"] == []


def test_retryable_failures_fall_back_to_the_next_strategy_in_the_map():
    client = FakeClient(async_responder=fail_on("b", retryable=True), sync_responder=lambda messages: "from sync")
    out = make(client, strategies=("async", "sync")).map(frame(["a", "b", "c"]), "text", "summary")
    assert out["summary"].tolist() == ["[a]", "from sync", "[c]"]
    assert contents(client) == ["M:b"]


def test_missing_column_is_a_config_error_naming_the_columns_and_sends_nothing():
    client = FakeClient()
    with pytest.raises(ConfigError, match='Column "review" not found.*text, n'):
        make(client).map(frame(["a"], n=[1]), "review", "summary")
    assert client.calls["sync"] == []


def test_id_column_with_pandas_is_a_config_error():
    client = FakeClient()
    df = frame(["a"], id=[1])
    with pytest.raises(ConfigError, match="id_column is for Spark"):
        make(client).map(df, "text", "summary", id_column="id")
    with pytest.raises(ConfigError, match="id_column is for Spark"):
        make(client).run(df, "text", "summary", id_column="id")
    assert client.calls["sync"] == []


@pytest.mark.parametrize("data", [["a", "b"], pd.Series(["a", "b"]), {"text": ["a"]}, "a"])
def test_map_needs_a_dataframe(data):
    client = FakeClient()
    with pytest.raises(TypeError, match="Expected a pandas or PySpark DataFrame"):
        make(client).map(data, "text", "summary")
    assert client.calls["sync"] == []


@pytest.mark.parametrize("step", ["map", "reduce", "run"])
def test_duplicated_column_label_is_a_config_error(step):
    df = pd.DataFrame([["a", "b"]], columns=["text", "text"])
    client = FakeClient()
    mr = make(client)
    with pytest.raises(ConfigError, match='more than one column named "text"'):
        if step == "map":
            mr.map(df, "text", "summary")
        elif step == "run":
            mr.run(df, "text", "summary")
        else:
            mr.reduce(df, "text")
    assert client.calls["sync"] == []


def test_map_on_an_empty_dataframe_adds_the_column_and_sends_nothing():
    client = FakeClient()
    out = make(client).map(frame([]), "text", "summary", error_column="error")
    assert list(out.columns) == ["text", "summary", "error"]
    assert len(out) == 0
    assert client.calls["sync"] == []


def test_map_on_an_all_empty_column_needs_no_working_strategy():
    client = FakeClient(deployment=None, batch_deployment=None)
    out = make(client).map(frame([None, "", "  "]), "text", "summary")
    assert out["summary"].tolist() == [None, None, None]


def test_map_with_a_client_that_supports_no_strategy_is_a_config_error():
    client = FakeClient(deployment=None, batch_deployment=None)
    with pytest.raises(ConfigError, match="None of the strategies"):
        make(client, strategies=("batch", "async", "sync")).map(frame(["a"]), "text", "summary")


def test_setup_error_in_the_last_strategy_stops_the_map():
    def responder(messages):
        raise LLMSetupError("Azure rejected the credentials (401).")

    with pytest.raises(LLMSetupError, match="credentials"):
        make(FakeClient(responder=responder)).map(frame(["a"]), "text", "summary")


def test_on_error_raise_stops_the_map_and_keeps_what_succeeded():
    client = FakeClient(responder=fail_on("bad"))
    with pytest.raises(StepFailedError, match="1 of 3 records failed in the map: blocked") as info:
        make(client, on_error="raise").map(frame(["ok", None, "bad", "fine"]), "text", "summary")
    assert info.value.outputs == ["[ok]", None, None, "[fine]"]
    assert list(info.value.failures) == [2]
    assert isinstance(info.value.failures[2], LLMRequestError)
    assert str(info.value.failures[2]) == "blocked"
    assert isinstance(info.value, MapReduceError)


def test_on_error_warn_keeps_going_and_logs_a_summary(caplog):
    client = FakeClient(responder=fail_on("bad", "worse", message="nope"))
    out = make(client).map(frame(["bad", "ok", "worse"]), "text", "summary")
    assert out["summary"].tolist() == [None, "[ok]", None]
    assert warnings_in(caplog) == ["2 of 3 records failed in the map: nope (×2)"]


def test_map_works_inside_a_running_event_loop():
    client = FakeClient(responder=bracket)

    async def main():
        return make(client, strategies=("async",)).map(frame(["a", "b"]), "text", "summary")

    out = asyncio.run(main())
    assert out["summary"].tolist() == ["[a]", "[b]"]
    assert client.async_sessions == 1


# --- map_texts -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "texts", [["a", None, "b"], ("a", None, "b"), pd.Series(["a", None, "b"], index=[9, 8, 7]), iter(["a", None, "b"])]
)
def test_map_texts_returns_one_output_per_text_in_order(texts):
    client = FakeClient(responder=bracket)
    assert make(client).map_texts(texts) == ["[a]", None, "[b]"]
    assert contents(client) == ["M:a", "M:b"]


def test_map_texts_on_an_empty_list():
    client = FakeClient()
    assert make(client).map_texts([]) == []
    assert client.calls["sync"] == []


def test_map_texts_failures_become_none_or_raise():
    client = FakeClient(responder=fail_on("bad"))
    assert make(client).map_texts(["bad", "ok"]) == [None, "[ok]"]
    with pytest.raises(StepFailedError) as info:
        make(client, on_error="raise").map_texts(["bad", "ok"])
    assert info.value.outputs == [None, "[ok]"]
    assert list(info.value.failures) == [0]


def test_map_texts_does_not_split_a_single_string_into_characters():
    client = FakeClient(responder=bracket)
    assert make(client).map_texts("hello world") == ["[hello world]"]
    assert contents(client) == ["M:hello world"]


# --- reduce: inputs --------------------------------------------------------------------------------------


def test_reduce_a_list():
    client = FakeClient(responder=bracket)
    result = make(client, reduce_group_size=2).reduce(["a", "b", "c", "d", "e"])
    assert isinstance(result, ReduceResult)
    assert result.levels == [
        ["a", "b", "c", "d", "e"],
        ["[a|b]", "[c|d]", "[e]"],
        ["[[a|b]|[c|d]]", "[[e]]"],
        ["[[[a|b]|[c|d]]|[[e]]]"],
    ]
    assert result.output == "[[[a|b]|[c|d]]|[[e]]]"
    assert result.depth == 3


def test_reduce_a_series_by_position_leaving_empty_values_out():
    client = FakeClient(responder=bracket)
    series = pd.Series(["a", None, "b", np.nan, "  ", "c"], index=["z", "y", "x", "w", "v", "u"])
    result = make(client).reduce(series)
    assert result.levels[0] == ["a", "b", "c"]
    assert result.output == "[a|b|c]"


def test_reduce_a_dataframe_column():
    client = FakeClient(responder=bracket)
    df = pd.DataFrame({"summary": ["a", None, "b"], "other": ["x", "y", "z"]})
    assert make(client).reduce(df, "summary").output == "[a|b]"
    assert make(client).reduce(df, column="other").output == "[x|y|z]"


def test_reduce_a_single_string_makes_one_reduce_call():
    client = FakeClient(responder=bracket)
    result = make(client, collapse_prompt="C:{text}").reduce("one long text")
    assert contents(client) == ["R:one long text"]
    assert result.output == "[one long text]"
    assert result.levels == [["one long text"], ["[one long text]"]]
    assert result.depth == 1


def test_a_single_input_still_gets_one_reduce_call():
    client = FakeClient(responder=bracket)
    result = make(client, collapse_prompt="C:{text}", reduce_group_size=2).reduce(["only", None, ""])
    assert contents(client) == ["R:only"]
    assert result.output == "[only]"
    assert result.depth == 1


def test_reduce_converts_numbers_to_text():
    client = FakeClient(responder=bracket)
    assert make(client).reduce([1, 2.5, None, b"x"]).output == "[1|2.5|x]"


def test_reduce_of_the_map_output_column_leaves_failed_records_out():
    client = FakeClient(responder=fail_on("bad"))
    mr = make(client)
    mapped = mr.map(frame(["a", "bad", "b"]), "text", "summary")
    assert mr.reduce(mapped, "summary").levels[0] == ["[a]", "[b]"]


@pytest.mark.parametrize(
    "data",
    [
        [],
        [None, "", "   ", math.nan, pd.NA],
        pd.Series([], dtype=object),
        pd.Series([None, None]),
        "",
        "  \n ",
        iter([]),
    ],
)
def test_reduce_of_nothing_is_an_error_and_sends_nothing(data):
    client = FakeClient()
    with pytest.raises(MapReduceError, match="Nothing to reduce"):
        make(client).reduce(data)
    assert client.calls["sync"] == []


def test_reduce_of_an_empty_dataframe_column_is_an_error():
    client = FakeClient()
    with pytest.raises(MapReduceError, match="Nothing to reduce"):
        make(client).reduce(pd.DataFrame({"summary": [None, ""]}), "summary")


def test_reduce_a_dataframe_needs_a_column_that_exists():
    client = FakeClient()
    df = pd.DataFrame({"summary": ["a"]})
    with pytest.raises(ConfigError, match="Say which column"):
        make(client).reduce(df)
    with pytest.raises(ConfigError, match='Column "missing" not found'):
        make(client).reduce(df, "missing")
    assert client.calls["sync"] == []


@pytest.mark.parametrize("data", [["a", "b"], pd.Series(["a"]), iter(["a"])])
def test_reduce_column_only_applies_to_dataframes(data):
    with pytest.raises(ConfigError, match="column only applies"):
        make(FakeClient()).reduce(data, "summary")


def test_reduce_column_with_a_single_string_is_rejected_too():
    client = FakeClient()
    with pytest.raises(ConfigError, match="column only applies"):
        make(client).reduce("one text", "summary")
    assert client.calls["sync"] == []


@pytest.mark.parametrize("data", [None, 42, 3.5])
def test_reduce_needs_texts(data):
    with pytest.raises(TypeError, match="Expected a DataFrame, a Series or a list"):
        make(FakeClient()).reduce(data)


# --- reduce: grouping and levels -------------------------------------------------------------------------


def test_reduce_groups_in_order_with_the_separator_level_by_level():
    client = FakeClient(responder=bracket)
    texts = [f"t{i:02d}" for i in range(25)]
    result = make(client, reduce_group_size=4).reduce(texts)
    assert [len(level) for level in result.levels] == [25, 7, 2, 1]
    first = result.levels[1]
    assert first[0] == "[t00|t01|t02|t03]"
    assert first[5] == "[t20|t21|t22|t23]"
    assert first[6] == "[t24]"
    assert result.levels[2] == ["[" + "|".join(first[:4]) + "]", "[" + "|".join(first[4:]) + "]"]
    assert result.output == "[" + "|".join(result.levels[2]) + "]"
    assert re.findall(r"t\d\d", result.output) == texts
    assert len(client.calls["sync"]) == 7 + 2 + 1
    assert contents(client)[0] == "R:t00|t01|t02|t03"


def test_default_separator_is_a_blank_line():
    client = FakeClient()
    mr = MapReduce(client, map_prompt="M:{text}", reduce_prompt="R:{text}", strategies=("sync",), show_progress=False)
    mr.reduce(["a", "b", "c"])
    assert contents(client) == ["R:a\n\nb\n\nc"]


@pytest.mark.parametrize("separator", ["\n---\n", "", " {text} "])
def test_custom_separator_is_used_verbatim(separator):
    client = FakeClient()
    make(client, separator=separator).reduce(["a", "b"])
    assert contents(client) == [f"R:a{separator}b"]


@pytest.mark.parametrize(
    ("count", "group_size", "levels"),
    [
        (1, 10, 1),  # n = 1
        (10, 10, 1),  # n = g
        (11, 10, 2),  # n = g + 1
        (100, 10, 2),  # n = g²
        (101, 10, 3),  # n = g² + 1
        (1000, 10, 3),
        (1001, 10, 4),
        (1, 2, 1),
        (2, 2, 1),
        (3, 2, 2),
        (4, 2, 2),
        (5, 2, 3),
        (25, 4, 3),
        (125, 5, 3),
        (126, 5, 4),
        (0, 10, 1),
    ],
)
def test_reduce_levels_table(count, group_size, levels):
    assert reduce_levels(count, group_size) == levels


def test_reduce_levels_is_the_smallest_k_with_g_to_the_k_at_least_n():
    for group_size in range(2, 8):
        for count in range(1, 400):
            expected = next(k for k in range(1, 20) if group_size**k >= count)
            assert reduce_levels(count, group_size) == expected, (count, group_size)


@pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="needs SIGALRM")
@pytest.mark.parametrize("group_size", [1, 0, -2, 2.0, None])
def test_reduce_levels_rejects_group_sizes_below_two(group_size):
    with time_limit(1.0), pytest.raises(ValueError, match="group_size must be 2 or more"):
        reduce_levels(5, group_size)


@pytest.mark.parametrize("group_size", [2, 3, 5])
def test_reduce_depth_and_request_count_follow_the_levels(group_size):
    for count in range(1, 28):
        client = FakeClient(responder=bracket)
        texts = [f"t{i}" for i in range(count)]
        result = make(client, reduce_group_size=group_size).reduce(texts)
        sizes = level_sizes(count, group_size)
        assert [len(level) for level in result.levels] == sizes, count
        assert result.depth == reduce_levels(count, group_size) == len(sizes) - 1
        assert len(client.calls["sync"]) == sum(sizes[1:])
        assert result.levels[-1] == [result.output]
        assert re.findall(r"t\d+", result.output) == texts


def test_collapse_prompt_on_intermediate_levels_and_reduce_prompt_only_on_the_last():
    client = FakeClient(responder=bracket)
    result = make(client, collapse_prompt="C:{text}", reduce_group_size=3).reduce([f"t{i}" for i in range(10)])
    assert [len(level) for level in result.levels] == [10, 4, 2, 1]
    assert [content[:2] for content in contents(client)] == ["C:"] * 4 + ["C:"] * 2 + ["R:"]
    assert contents(client)[-1] == "R:" + "|".join(result.levels[2])


def test_one_level_reduce_uses_only_the_reduce_prompt():
    client = FakeClient(responder=bracket)
    make(client, collapse_prompt="C:{text}", reduce_group_size=3).reduce(["a", "b", "c"])
    assert contents(client) == ["R:a|b|c"]


def test_without_a_collapse_prompt_every_level_uses_the_reduce_prompt():
    client = FakeClient(responder=bracket)
    make(client, reduce_group_size=2).reduce(["a", "b", "c", "d", "e"])
    assert all(content.startswith("R:") for content in contents(client))
    assert len(client.calls["sync"]) == 3 + 2 + 1


# --- reduce: failures ------------------------------------------------------------------------------------


def test_failed_groups_are_dropped_with_a_warning_and_the_levels_recomputed(caplog, bars):
    client = FakeClient(responder=fail_on("t03", "t06"))
    mr = make(client, collapse_prompt="C:{text}", reduce_group_size=3, show_progress=True, reduce_on_error="warn")
    result = mr.reduce([f"t{i:02d}" for i in range(10)])  # would take 3 levels: 10 → 4 → 2 → 1
    assert result.levels == [
        [f"t{i:02d}" for i in range(10)],
        ["[t00|t01|t02]", "[t09]"],
        ["[[t00|t01|t02]|[t09]]"],
    ]
    assert result.depth == 2
    assert contents(client)[-1] == "R:[t00|t01|t02]|[t09]"  # the new last level gets the reduce prompt
    assert warnings_in(caplog) == [
        "2 of 4 groups failed in the reduce level 1: blocked (×2). The final text leaves out what those groups held."
    ]
    assert list(result.failures) == [1]
    assert sorted(result.failures[1]) == [1, 2]  # the groups holding t03-t05 and t06-t08
    assert not result.complete
    reduce_bar, first, second = bars
    assert (reduce_bar.total, reduce_bar.n) == (2, 2)
    assert first.desc == "Level 1/3: 10 → 4 [sync]"
    assert (first.total, first.n) == (4, 4)
    assert "failed=2" in first.postfix
    assert second.desc == "Level 2/2: 2 → 1 [sync]"


def test_a_level_left_with_one_text_after_failures_still_gets_the_final_reduce():
    client = FakeClient(responder=fail_on("t0", "t1"))
    mr = make(client, collapse_prompt="C:{text}", reduce_group_size=2, reduce_on_error="warn")
    result = mr.reduce(["t0", "t1", "t2", "t3"])
    assert contents(client)[-1] == "R:[t2|t3]"
    assert result.output == "[[t2|t3]]"
    assert result.depth == 2


def test_every_group_failing_is_an_error(caplog):
    client = FakeClient(responder=fail_on("R:"))
    with pytest.raises(MapReduceError, match="Every group failed at reduce level 1") as info:
        make(client, reduce_group_size=2, reduce_on_error="warn").reduce(["a", "b", "c"])
    assert not isinstance(info.value, StepFailedError)
    assert warnings_in(caplog) == [
        "2 of 2 groups failed in the reduce level 1: blocked (×2). The final text leaves out what those groups held."
    ]
    # Like a StepFailedError, it carries the failing level and the levels before it.
    assert info.value.outputs == [None, None]
    assert sorted(info.value.failures) == [0, 1]
    assert all(str(error) == "blocked" for error in info.value.failures.values())
    assert info.value.levels == [["a", "b", "c"]]


def test_the_last_group_failing_is_an_error():
    client = FakeClient(responder=fail_on("R:"))
    with pytest.raises(MapReduceError, match="Every group failed at reduce level 2") as info:
        make(client, collapse_prompt="C:{text}", reduce_group_size=2, reduce_on_error="warn").reduce(["a", "b", "c"])
    assert not isinstance(info.value, StepFailedError)
    assert len(client.calls["sync"]) == 3


def test_every_group_failing_with_the_default_raises_step_failed_error():
    client = FakeClient(responder=fail_on("R:"))
    with pytest.raises(StepFailedError, match="2 of 2 groups failed in the reduce level 1") as info:
        make(client, reduce_group_size=2).reduce(["a", "b", "c"])
    assert info.value.outputs == [None, None]
    assert sorted(info.value.failures) == [0, 1]


def test_reduce_on_error_raise_stops_the_reduce_at_the_failing_level():
    client = FakeClient(responder=fail_on("c"))
    with pytest.raises(StepFailedError, match="1 of 3 groups failed in the reduce level 1") as info:
        make(client, reduce_on_error="raise", reduce_group_size=2).reduce(["a", "b", "c", "d", "e", "f"])
    assert info.value.outputs == ["[a|b]", None, "[e|f]"]
    assert list(info.value.failures) == [1]
    assert str(info.value.failures[1]) == "blocked"
    assert info.value.levels == [["a", "b", "c", "d", "e", "f"]]
    assert len(client.calls["sync"]) == 3  # no second level


def test_reduce_on_error_raise_in_a_later_level_reports_that_level():
    client = FakeClient(responder=fail_on("[a|b]|[c|d]"))
    with pytest.raises(StepFailedError, match="reduce level 2") as info:
        make(client, reduce_on_error="raise", reduce_group_size=2).reduce(list("abcdef"))
    assert info.value.outputs == [None, "[[e|f]]"]
    assert info.value.levels == [list("abcdef"), ["[a|b]", "[c|d]", "[e|f]"]]  # the paid level-1 replies are kept


def test_retryable_reduce_failures_fall_back_to_the_next_strategy():
    client = FakeClient(async_responder=fail_on("b", retryable=True), sync_responder=lambda messages: "sync")
    result = make(client, strategies=("async", "sync"), reduce_group_size=2).reduce(["a", "b", "c"])
    assert result.levels[1] == ["sync", "[c]"]
    assert contents(client) == ["R:a|b"]


# --- run -------------------------------------------------------------------------------------------------


def test_run_maps_then_reduces():
    client = FakeClient(responder=bracket)
    df = frame(["a", None, "b", "c"], n=[1, 2, 3, 4])
    result = make(client, reduce_group_size=2).run(df, "text", "summary")
    assert isinstance(result, MapReduceResult)
    assert result.frame["summary"].tolist() == ["[a]", None, "[b]", "[c]"]
    assert result.frame["n"].tolist() == [1, 2, 3, 4]
    assert result.levels[0] == ["[a]", "[b]", "[c]"]
    assert result.output == "[[[a]|[b]]|[[c]]]"
    assert result.levels[-1] == [result.output]
    assert result.depth == 2
    assert result.map_failures == {}
    assert df.columns.tolist() == ["text", "n"]
    assert make(FakeClient(responder=bracket), reduce_group_size=2).reduce(result.frame, "summary").output == (
        result.output
    )


def test_run_leaves_map_failures_out_of_the_reduce_and_reports_them(caplog):
    client = FakeClient(responder=fail_on("bad"))
    df = pd.DataFrame({"text": ["a", "bad", "b"]}, index=["x", "y", "z"])
    result = make(client).run(df, "text", "summary", error_column="error")
    assert result.frame["error"].tolist() == [None, "blocked", None]
    assert list(result.map_failures) == [1]  # by position, like StepFailedError.outputs
    assert str(result.map_failures[1]) == "blocked"
    assert result.levels[0] == ["[a]", "[b]"]
    assert result.output == "[[a]|[b]]"
    assert warnings_in(caplog) == ["1 of 3 records failed in the map: blocked (×1)"]


def test_run_with_on_error_raise_stops_before_the_reduce():
    client = FakeClient(responder=fail_on("bad"))
    with pytest.raises(StepFailedError, match="in the map") as info:
        make(client, on_error="raise").run(frame(["a", "bad"]), "text", "summary")
    assert info.value.outputs == ["[a]", None]
    assert not any(content.startswith("R:") for content in contents(client))


def test_run_with_nothing_to_reduce_is_an_error():
    client = FakeClient(responder=fail_on("bad"))
    with pytest.raises(MapReduceError, match="Nothing to reduce"):
        make(client).run(frame(["bad", None, " "]), "text", "summary")


def test_run_checks_the_column_before_sending_anything():
    client = FakeClient()
    with pytest.raises(ConfigError, match='Column "nope" not found'):
        make(client).run(frame(["a"]), "nope", "summary")
    assert client.calls["sync"] == []


def test_run_output_column_can_replace_the_input_column():
    client = FakeClient(responder=bracket)
    result = make(client).run(frame(["a", "b"]), "text", "text")
    assert result.frame["text"].tolist() == ["[a]", "[b]"]
    assert result.output == "[[a]|[b]]"


def test_run_does_not_retry_an_async_strategy_that_broke_in_the_map(caplog):
    def broken(messages):
        raise LLMSetupError("the async client can't start")

    client = FakeClient(responder=bracket, async_responder=broken)
    result = make(client, strategies=("async", "sync"), reduce_group_size=2).run(frame("abcd"), "text", "out")
    assert result.output == "[[[a]|[b]]|[[c]|[d]]]"
    assert client.async_sessions == 1
    assert len(client.calls["sync"]) == 4 + 2 + 1
    assert sum("The async strategy failed" in message for message in warnings_in(caplog)) == 1


def test_run_does_not_retry_a_batch_api_that_broke_in_the_map(clock, caplog):
    client = FakeClient(responder=bracket, create_error=LLMSetupError("Batch API not enabled here"))
    mr = make_batch(client, clock, strategies=("batch", "async", "sync"), reduce_group_size=2)
    result = mr.run(frame("abcde"), "text", "out")
    assert result.output == "[[[[a]|[b]]|[[c]|[d]]]|[[[e]]]]"
    assert len(client.files) == 1  # one upload, in the map; the reduce levels never tried the Batch API again
    assert client.jobs == {} and client.calls["batch"] == []
    assert client.async_sessions == 1 + 3
    assert sum("The batch strategy failed" in message for message in warnings_in(caplog)) == 1


def test_run_goes_batch_then_async_then_sync_at_every_step(clock, caplog):
    """Failed batch jobs don't break the Batch API (only a strategy that raises does), so every level tries it."""
    client = FakeClient(
        responder=bracket,
        batch_statuses=("validating", "failed"),
        batch_errors=("quota exceeded",),
        async_responder=fail_on("b", retryable=True),
        sync_responder=lambda messages: "sync",
    )
    mr = make_batch(client, clock, strategies=("batch", "async", "sync"), reduce_group_size=2)
    result = mr.run(frame("abc"), "text", "summary")
    assert result.frame["summary"].tolist() == ["[a]", "sync", "[c]"]
    assert result.levels == [["[a]", "sync", "[c]"], ["[[a]|sync]", "[[c]]"], ["[[[a]|sync]|[[c]]]"]]
    assert len(client.jobs) == 1 + 2  # the map, then each reduce level
    assert client.async_sessions == 3
    assert contents(client) == ["M:b"]
    assert result.map_failures == {}
    assert not any(" failed in the " in message for message in warnings_in(caplog))  # nothing failed for good
    assert any("ended failed: quota exceeded" in message for message in warnings_in(caplog))


def test_separate_map_and_reduce_calls_each_start_from_the_first_strategy(clock):
    """Only run() shares the executor; map() and reduce() each get a fresh fallback chain."""
    client = FakeClient(responder=bracket, create_error=LLMSetupError("Batch API not enabled here"))
    mr = make_batch(client, clock, strategies=("batch", "sync"))
    mapped = mr.map(frame(["a", "b"]), "text", "out")
    mr.reduce(mapped, "out")
    assert len(client.files) == 2


# --- batch sizes and the other executor options reach the runners ----------------------------------------


def test_map_batch_size_and_reduce_batch_size_set_the_batch_job_sizes(clock):
    client = FakeClient(responder=bracket)
    mr = make_batch(client, clock, map_batch_size=5, reduce_group_size=2, reduce_batch_size=2)
    result = mr.run(frame([f"r{i:02d}" for i in range(12)]), "text", "summary")
    assert [len(level) for level in result.levels] == [12, 6, 3, 2, 1]
    assert job_sizes(client) == [
        ("M", 5),
        ("M", 5),
        ("M", 2),  # map: 12 records in jobs of up to 5
        ("R", 2),
        ("R", 2),
        ("R", 2),  # level 1: 6 groups in jobs of up to 2
        ("R", 2),
        ("R", 1),  # level 2: 3 groups
        ("R", 2),  # level 3: 2 groups
        ("R", 1),  # level 4: 1 group
    ]
    assert result.frame["summary"].tolist() == [f"[r{i:02d}]" for i in range(12)]
    assert client.calls["sync"] == [] and client.calls["async"] == []


def test_reduce_batch_size_defaults_to_the_map_batch_size(clock):
    client = FakeClient(responder=bracket)
    make_batch(client, clock, map_batch_size=2, reduce_group_size=2).run(frame("abcdefgh"), "text", "summary")
    assert job_sizes(client) == [("M", 2)] * 4 + [("R", 2), ("R", 2), ("R", 2), ("R", 1)]


def test_reduce_batch_size_applies_to_reduce_called_on_its_own(clock):
    client = FakeClient(responder=bracket)
    make_batch(client, clock, map_batch_size=100, reduce_group_size=2, reduce_batch_size=1).reduce(list("abcd"))
    assert job_sizes(client) == [("R", 1)] * 3


def test_map_batch_size_does_not_change_what_the_map_returns():
    for size in (1, 2, 3, 50):
        client = FakeClient(responder=bracket)
        out = make(client, map_batch_size=size, strategies=("async", "sync")).map_texts(list("abcde"))
        assert out == ["[a]", "[b]", "[c]", "[d]", "[e]"]


@pytest.mark.parametrize(("max_jobs", "rounds"), [(1, 12), (2, 8), (4, 4)])
def test_max_concurrent_batch_jobs_and_poll_interval_reach_the_batch_runner(clock, max_jobs, rounds):
    client = FakeClient(responder=bracket)  # every job takes 4 polls
    mr = make_batch(client, clock, map_batch_size=2, max_concurrent_batch_jobs=max_jobs, batch_poll_interval=7.5)
    assert mr.map_texts(list("abcdef")) == ["[a]", "[b]", "[c]", "[d]", "[e]", "[f]"]
    assert clock.sleeps == [7.5] * rounds


@pytest.mark.parametrize("cleanup", [True, False])
def test_batch_cleanup_reaches_the_batch_runner(clock, cleanup):
    client = FakeClient(responder=bracket)
    make_batch(client, clock, batch_cleanup=cleanup).map_texts(["a", "b"])
    assert sorted(client.deleted) == (["file-1", "file-2"] if cleanup else [])


def test_batch_timeout_cancels_the_job_and_sends_the_rest_another_way(clock):
    client = FakeClient(responder=bracket, sync_responder=lambda messages: "sync", batch_statuses=("in_progress",))
    mr = make_batch(client, clock, strategies=("batch", "sync"), batch_timeout=5.0)
    assert mr.map_texts(list("abcd")) == ["[a]", "[b]", "sync", "sync"]
    assert client.cancelled == ["batch-1"]
    assert contents(client) == ["M:c", "M:d"]


# --- progress --------------------------------------------------------------------------------------------


def test_progress_bars_for_the_map_the_levels_and_each_level_reach_their_totals(bars):
    client = FakeClient(responder=bracket)
    df = frame(["a", "b", None, "c", "d", "e", "f"])
    make(client, reduce_group_size=2, show_progress=True).run(df, "text", "summary")
    map_bar, reduce_bar, *level_bars = bars
    assert all(bar.shown for bar in bars)
    assert map_bar.desc == "Map [sync]"
    assert (map_bar.total, map_bar.n) == (6, 6)  # the empty record isn't counted
    assert reduce_bar.desc == "Reduce"
    assert (reduce_bar.total, reduce_bar.n) == (3, 3)
    assert [(bar.desc, bar.total, bar.n) for bar in level_bars] == [
        ("Level 1/3: 6 → 3 [sync]", 3, 3),
        ("Level 2/3: 3 → 2 [sync]", 2, 2),
        ("Level 3/3: 2 → 1 [sync]", 1, 1),
    ]
    assert [bar.unit for bar in bars] == ["record", "level", "group", "group", "group"]
    assert "level 3: 2 texts in 1 group of up to 2" in reduce_bar.postfix


def test_map_bar_reaches_its_total_when_records_fail(bars):
    client = FakeClient(responder=fail_on("bad"))
    make(client, show_progress=True).map(frame(["a", "bad", "b"]), "text", "summary")
    (map_bar,) = bars
    assert (map_bar.total, map_bar.n) == (3, 3)
    assert "failed=1" in map_bar.postfix


def test_bars_reach_their_totals_on_the_batch_api_with_a_fallback(bars, clock):
    client = FakeClient(
        responder=bracket, sync_responder=lambda messages: "sync", batch_statuses=("in_progress", "expired")
    )
    mr = make_batch(client, clock, strategies=("batch", "sync"), map_batch_size=3, show_progress=True)
    result = mr.run(frame("abcdef"), "text", "summary")
    assert result.frame["summary"].tolist() == ["[a]", "sync", "sync", "[d]", "sync", "sync"]
    map_bar, reduce_bar, level_bar = bars
    assert (map_bar.total, map_bar.n) == (6, 6)
    assert map_bar.desc == "Map [sync]"  # the last strategy that worked on it
    assert (reduce_bar.total, reduce_bar.n) == (1, 1)
    assert (level_bar.total, level_bar.n) == (1, 1)


def test_batch_api_progress_notes_the_jobs(bars, clock):
    client = FakeClient(responder=bracket)
    make_batch(client, clock, map_batch_size=2, show_progress=True).map(frame("abcde"), "text", "summary")
    (map_bar,) = bars
    assert map_bar.desc == "Map [batch]"
    assert (map_bar.total, map_bar.n) == (5, 5)
    assert "jobs 3/3 done" in map_bar.postfix


def test_progress_is_shown_on_stderr(capsys):
    client = FakeClient(responder=bracket)
    make(client, reduce_group_size=2, show_progress=True).run(frame("abcdef"), "text", "summary")
    err = capsys.readouterr().err
    assert "Map [sync]: 100%" in err and "6/6" in err
    assert "Reduce: 100%" in err and "3/3" in err
    assert "Level 1/3: 6 → 3 [sync]" in err
    assert "Level 3/3: 2 → 1 [sync]" in err


def test_hidden_progress_prints_nothing(capsys, bars):
    client = FakeClient(responder=bracket)
    make(client, reduce_group_size=2, show_progress=False).run(frame("abcdef"), "text", "summary")
    assert capsys.readouterr().err == ""
    assert bars and not any(bar.shown for bar in bars)


def test_progress_bars_are_closed_when_a_step_fails(bars):
    client = FakeClient(responder=fail_on("R:"))
    with pytest.raises(MapReduceError):
        make(client, reduce_group_size=2, show_progress=True).run(frame("abc"), "text", "summary")
    assert [bar.desc for bar in bars] == ["Map [sync]", "Reduce", "Level 1/2: 3 → 2 [sync]"]
    assert not any(bar in progress_module.tqdm._instances for bar in bars)  # closed bars leave the registry


def test_warnings_still_reach_logging_while_bars_are_shown(caplog):
    client = FakeClient(responder=fail_on("bad"))
    make(client, show_progress=True).map(frame(["a", "bad"]), "text", "summary")
    assert warnings_in(caplog) == ["1 of 2 records failed in the map: blocked (×1)"]


# --- defaults and the seconds options --------------------------------------------------------------------


def test_documented_defaults():
    parameters = inspect.signature(MapReduce).parameters
    defaults = {name: parameters[name].default for name in parameters}
    assert defaults["map_batch_size"] == 1000
    assert defaults["reduce_group_size"] == 10
    assert defaults["reduce_batch_size"] is None
    assert defaults["batch_poll_interval"] == 60
    assert defaults["batch_timeout"] == 24 * 3600 == 86400
    assert defaults["batch_cancel_wait"] == 600
    assert defaults["on_error"] == "warn"
    assert defaults["reduce_on_error"] == "raise"
    assert defaults["balance_groups"] is False
    mr = MapReduce(FakeClient(), map_prompt="M:{text}", reduce_prompt="R:{text}")
    assert mr.map_batch_size == mr.reduce_batch_size == 1000
    assert mr.reduce_on_error == "raise" and mr.balance_groups is False


def test_default_poll_interval_and_timeout_reach_the_batch_runner(clock):
    client = FakeClient(responder=bracket, sync_responder=lambda messages: "sync", batch_statuses=("in_progress",))
    mr = make(client, strategies=("batch", "sync"), sleep=clock.sleep, clock=clock)
    assert mr.map_texts(["a", "b"]) == ["[a]", "sync"]  # cancelled: the fake answers the first half
    assert set(clock.sleeps) == {60.0}
    # Cancelled on the first poll more than 24 hours after submitting, and the cancel shows on the next poll.
    assert clock.now == 86400 + 60 + 60
    assert client.cancelled == ["batch-1"]


class StuckCancelClient(FakeClient):
    """A Batch API whose cancellations never take effect: jobs stay in progress after cancel_batch."""

    def cancel_batch(self, batch_id):
        self.cancelled.append(batch_id)
        return self._view(self.jobs[batch_id], "cancelling")


@pytest.mark.parametrize("wait", [0, 0.0, 3, 10.5])
def test_batch_cancel_wait_is_how_long_a_cancelled_job_gets_to_stop(clock, caplog, wait):
    client = StuckCancelClient(
        responder=bracket, sync_responder=lambda messages: "sync", batch_statuses=("in_progress",)
    )
    mr = make_batch(client, clock, strategies=("batch", "sync"), batch_timeout=5.0, batch_cancel_wait=wait)
    assert mr.map_texts(["a", "b"]) == ["sync", "sync"]
    assert client.cancelled == ["batch-1"]
    # Polled every second: cancelled at t=6 (> 5s), then given up on at the first poll more than `wait` later.
    assert clock.now == 6 + math.floor(wait) + 1
    assert any("didn't finish cancelling" in message for message in warnings_in(caplog))


SECONDS_OPTIONS = ["batch_poll_interval", "batch_timeout", "batch_cancel_wait"]


def seconds_message(name: str, value: object) -> str:
    wanted = "zero or more" if name == "batch_cancel_wait" else "greater than zero"
    suffix = ", or None to wait as long as it takes" if name == "batch_timeout" else ""
    return f"{name} must be a number of seconds {wanted}{suffix} (got {value!r})."


@pytest.mark.parametrize(
    "value", [math.nan, math.inf, -math.inf, True, False, "30", "", b"1", [1], -1, -0.5, -1e-9, 1 + 0j]
)
@pytest.mark.parametrize("name", SECONDS_OPTIONS)
def test_seconds_options_reject_nan_inf_bools_strings_and_negatives(name, value):
    client = FakeClient()
    with pytest.raises(ConfigError) as info:
        make(client, **{name: value})
    assert str(info.value) == seconds_message(name, value)
    assert client.calls == {"sync": [], "async": [], "batch": []}


@pytest.mark.parametrize("name", ["batch_poll_interval", "batch_timeout"])
@pytest.mark.parametrize("value", [0, 0.0, -0.0])
def test_poll_interval_and_timeout_reject_zero(name, value):
    with pytest.raises(ConfigError) as info:
        make(FakeClient(), **{name: value})
    assert str(info.value) == seconds_message(name, value)


@pytest.mark.parametrize("value", [0, 0.0, -0.0])
def test_zero_cancel_wait_is_allowed(value):
    assert make(FakeClient(), batch_cancel_wait=value)


@pytest.mark.parametrize("name", ["batch_poll_interval", "batch_cancel_wait"])
def test_only_batch_timeout_accepts_none(name):
    with pytest.raises(ConfigError) as info:
        make(FakeClient(), **{name: None})
    assert str(info.value) == seconds_message(name, None)
    make(FakeClient(), batch_timeout=None)


@pytest.mark.parametrize("value", [1, 0.001, 2.5, 1e9, np.float64(30.0)])
@pytest.mark.parametrize("name", SECONDS_OPTIONS)
def test_seconds_options_accept_positive_finite_numbers(name, value):
    make(FakeClient(), **{name: value})


# --- numpy (and other numbers.*) option values -------------------------------------------------------------


COUNT_OPTIONS = ["map_batch_size", "reduce_batch_size", "max_concurrency", "max_concurrent_batch_jobs"]


@pytest.fixture
def executor_options(monkeypatch):
    """The keyword arguments every executor is built with (captured, then built as usual)."""
    captured = []
    real = mapreduce_module.build_executor

    def capture(client, **options):
        captured.append(options)
        return real(client, **options)

    monkeypatch.setattr(mapreduce_module, "build_executor", capture)
    return captured


@pytest.mark.parametrize("value", [np.int64(3), np.int32(3), np.uint8(3), np.int16(3)])
@pytest.mark.parametrize("name", COUNT_OPTIONS)
def test_count_options_accept_numpy_ints_and_store_plain_ints(executor_options, name, value):
    mr = make(FakeClient(), **{name: value})
    stored = {
        "map_batch_size": mr.map_batch_size,
        "reduce_batch_size": mr.reduce_batch_size,
        "max_concurrency": executor_options[-1]["max_concurrency"],
        "max_concurrent_batch_jobs": executor_options[-1]["max_concurrent_batch_jobs"],
    }[name]
    assert stored == 3 and type(stored) is int


@pytest.mark.parametrize("value", [np.int64(2), np.int32(5), np.uint64(10)])
def test_reduce_group_size_accepts_numpy_ints_and_stores_a_plain_int(value):
    mr = make(FakeClient(), reduce_group_size=value)
    assert mr.reduce_group_size == int(value) and type(mr.reduce_group_size) is int


def test_numpy_batch_sizes_default_the_reduce_batch_size_to_a_plain_int():
    mr = make(FakeClient(), map_batch_size=np.int64(7))
    assert (mr.map_batch_size, mr.reduce_batch_size) == (7, 7)
    assert type(mr.map_batch_size) is int and type(mr.reduce_batch_size) is int


@pytest.mark.parametrize(
    "value", [np.float64(2.5), np.float32(2.5), np.int64(3), np.uint16(3), fractions.Fraction(5, 2)]
)
@pytest.mark.parametrize("name", SECONDS_OPTIONS)
def test_seconds_options_accept_numpy_numbers_and_store_plain_floats(executor_options, name, value):
    make(FakeClient(), **{name: value})
    stored = executor_options[-1][name]
    assert stored == float(value) and type(stored) is float


def test_batch_timeout_none_stays_none(executor_options):
    make(FakeClient(), batch_timeout=None)
    assert executor_options[-1]["batch_timeout"] is None


@pytest.mark.parametrize(
    "value", [np.int64(0), np.int64(-2), np.float64(2.0), np.float32(3.0), np.bool_(True), np.bool_(False)]
)
@pytest.mark.parametrize("name", ["map_batch_size", "max_concurrency", "max_concurrent_batch_jobs"])
def test_numpy_counts_that_are_not_whole_numbers_of_at_least_one_are_rejected(name, value):
    with pytest.raises(ConfigError, match=f"{name} must be a whole number of at least 1") as info:
        make(FakeClient(), **{name: value})
    assert repr(value) in str(info.value)


@pytest.mark.parametrize("value", [np.int64(1), np.int64(0), np.float64(10.0), np.bool_(True)])
def test_numpy_reduce_group_sizes_below_two_or_not_whole_are_rejected(value):
    with pytest.raises(ConfigError, match="reduce_group_size must be 2 or more"):
        make(FakeClient(), reduce_group_size=value)


@pytest.mark.parametrize(
    "value", [np.float64("nan"), np.float64("inf"), np.float32("-inf"), np.float64(-1.0), np.int64(-5), np.bool_(True)]
)
@pytest.mark.parametrize("name", SECONDS_OPTIONS)
def test_numpy_seconds_that_are_not_finite_or_are_negative_are_rejected(name, value):
    with pytest.raises(ConfigError) as info:
        make(FakeClient(), **{name: value})
    assert str(info.value) == seconds_message(name, value)


@pytest.mark.parametrize("value", [np.float64(0.0), np.int64(0)])
@pytest.mark.parametrize("name", ["batch_poll_interval", "batch_timeout"])
def test_numpy_zero_poll_interval_or_timeout_is_rejected(name, value):
    with pytest.raises(ConfigError, match=f"{name} must be a number of seconds greater than zero"):
        make(FakeClient(), **{name: value})


def test_numpy_sizes_group_and_batch_like_plain_ints(clock):
    plain, numpy = FakeClient(responder=bracket), FakeClient(responder=bracket)
    options = {"map_batch_size": 2, "reduce_batch_size": 1, "reduce_group_size": 2, "max_concurrent_batch_jobs": 2}
    expected = make_batch(plain, clock, **options).run(frame("abcde"), "text", "summary")
    result = make_batch(numpy, FakeClock(), **{k: np.int64(v) for k, v in options.items()}).run(
        frame("abcde"), "text", "summary"
    )
    assert result.output == expected.output == "[[[[a]|[b]]|[[c]|[d]]]|[[[e]]]]"
    assert result.levels == expected.levels
    assert job_sizes(numpy) == job_sizes(plain) == [("M", 2), ("M", 2), ("M", 1)] + [("R", 1)] * 6


def test_numpy_max_concurrency_reaches_the_async_strategy():
    client = FakeClient(responder=bracket)
    mr = make(client, strategies=("async",), max_concurrency=np.int64(2))
    assert mr.map_texts(list("abc")) == ["[a]", "[b]", "[c]"]
    assert client.async_sessions == 1 and client.calls["sync"] == []


def test_numpy_seconds_reach_the_batch_runner_as_plain_floats():
    clock = FakeClock()
    client = FakeClient(responder=bracket, sync_responder=lambda messages: "sync", batch_statuses=("in_progress",))
    mr = make_batch(
        client,
        clock,
        strategies=("batch", "sync"),
        batch_poll_interval=np.int64(2),
        batch_timeout=np.float64(5.0),
        batch_cancel_wait=np.int64(0),
    )
    assert mr.map_texts(["a", "b"]) == ["[a]", "sync"]  # cancelled: the fake answers the first half
    assert client.cancelled == ["batch-1"]
    assert clock.sleeps and all(type(seconds) is float and seconds == 2.0 for seconds in clock.sleeps)
    assert clock.now == 8.0  # cancelled at the first poll past 5 s (t=6), the cancel shows on the next one


# --- reduce_on_error ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["ignore", "WARN", "Raise", "", None, True])
def test_reduce_on_error_must_be_warn_or_raise(value):
    with pytest.raises(ConfigError, match='reduce_on_error must be "warn" or "raise"'):
        make(FakeClient(), reduce_on_error=value)


def test_a_failed_group_raises_by_default_even_with_on_error_warn(caplog):
    client = FakeClient(responder=fail_on("c|d"))
    mr = make(client, reduce_group_size=2)
    assert (mr.on_error, mr.reduce_on_error) == ("warn", "raise")
    with pytest.raises(
        StepFailedError, match=re.escape("1 of 3 groups failed in the reduce level 1: blocked (×1)")
    ) as info:
        mr.reduce(list("abcdef"))
    assert info.value.outputs == ["[a|b]", None, "[e|f]"]
    assert list(info.value.failures) == [1]
    assert isinstance(info.value.failures[1], LLMRequestError)
    assert info.value.levels == [list("abcdef")]
    assert contents(client) == ["R:a|b", "R:c|d", "R:e|f"]  # nothing sent after the failing level
    assert warnings_in(caplog) == []  # raised, not warned


def test_on_error_raise_with_reduce_on_error_warn_raises_only_in_the_map(caplog):
    client = FakeClient(responder=fail_on("bad", "R:[a]|[b]"))
    mr = make(client, on_error="raise", reduce_on_error="warn", reduce_group_size=2)
    with pytest.raises(StepFailedError, match="in the map"):
        mr.run(frame(["a", "bad"]), "text", "summary")
    result = mr.run(frame(["a", "b", "c"]), "text", "summary")
    assert result.output == "[[[c]]]"
    assert list(result.reduce_failures) == [1]
    assert any("groups failed in the reduce level 1" in message for message in warnings_in(caplog))


def test_on_error_warn_with_reduce_on_error_raise_warns_in_the_map_and_raises_in_the_reduce(caplog):
    client = FakeClient(responder=fail_on("bad", "R:[a]|[b]"))
    mr = make(client, on_error="warn", reduce_on_error="raise", reduce_group_size=2)
    with pytest.raises(StepFailedError, match="in the reduce level 1") as info:
        mr.run(frame(["a", "bad", "b", "c"]), "text", "summary")
    assert warnings_in(caplog) == ["1 of 4 records failed in the map: blocked (×1)"]
    assert list(info.value.map_failures) == [1]


def test_warn_records_failures_by_level_and_group_index(caplog):
    # Level 1: [t0|t1] [t2|t3]✗ [t4|t5] [t6|t7]; level 2: [[t0|t1]|[t4|t5]] [[t6|t7]]✗; level 3: the one text left.
    client = FakeClient(responder=fail_on("t2", "[t6|t7]"))
    mr = make(client, collapse_prompt="C:{text}", reduce_group_size=2, reduce_on_error="warn")
    result = mr.reduce([f"t{i}" for i in range(8)])
    assert result.levels == [
        [f"t{i}" for i in range(8)],
        ["[t0|t1]", "[t4|t5]", "[t6|t7]"],
        ["[[t0|t1]|[t4|t5]]"],
        ["[[[t0|t1]|[t4|t5]]]"],
    ]
    assert result.output == "[[[t0|t1]|[t4|t5]]]"
    assert contents(client)[-1] == "R:[[t0|t1]|[t4|t5]]"
    assert sorted(result.failures) == [1, 2]
    assert list(result.failures[1]) == [1]
    assert list(result.failures[2]) == [1]
    assert all(isinstance(error, LLMRequestError) and str(error) == "blocked" for error in result.failures[1].values())
    assert result.complete is False
    suffix = ". The final text leaves out what those groups held."
    assert warnings_in(caplog) == [
        "1 of 4 groups failed in the reduce level 1: blocked (×1)" + suffix,
        "1 of 2 groups failed in the reduce level 2: blocked (×1)" + suffix,
    ]


def test_warning_counts_the_reasons_of_the_failed_groups(caplog):
    def responder(messages):
        content = content_of(messages)
        if "a" in content or "c" in content:
            raise LLMRequestError("content filter", retryable=False)
        if "e" in content:
            raise LLMRequestError("too long", retryable=False)
        return bracket(messages)

    result = make(FakeClient(responder=responder), reduce_group_size=2, reduce_on_error="warn").reduce(list("abcdefg"))
    assert sorted(result.failures[1]) == [0, 1, 2]
    assert warnings_in(caplog)[0] == (
        "3 of 4 groups failed in the reduce level 1: content filter (×2); too long (×1). "
        "The final text leaves out what those groups held."
    )
    assert result.output == "[[g]]"


def test_complete_results_have_no_failures():
    client = FakeClient(responder=bracket)
    reduced = make(client, reduce_group_size=2, reduce_on_error="warn").reduce(list("abc"))
    assert reduced.failures == {} and reduced.complete is True
    result = make(client, reduce_group_size=2).run(frame(["a", None, "b"]), "text", "summary")
    assert result.map_failures == {} and result.reduce_failures == {}
    assert result.complete is True  # an empty record is skipped, not a failure


def test_run_result_reports_map_and_reduce_failures_separately():
    client = FakeClient(responder=fail_on("bad", "R:[c]"))
    mr = make(client, reduce_group_size=2, reduce_on_error="warn")
    result = mr.run(frame(["a", "b", "c", "bad"]), "text", "summary")
    assert list(result.map_failures) == [3]
    assert list(result.reduce_failures) == [1] and list(result.reduce_failures[1]) == [1]
    assert result.complete is False
    assert result.output == "[[[a]|[b]]]"

    only_map = make(FakeClient(responder=fail_on("bad")), reduce_on_error="warn").run(frame(["a", "bad"]), "text", "s")
    assert list(only_map.map_failures) == [1]
    assert only_map.reduce_failures == {}
    assert only_map.complete is False

    only_reduce = mr.run(frame(["a", "b", "c"]), "text", "summary")
    assert only_reduce.map_failures == {}
    assert list(only_reduce.reduce_failures) == [1]
    assert only_reduce.complete is False


def test_result_failures_default_to_a_fresh_empty_dict():
    first = ReduceResult(output="x", levels=[["a"], ["x"]])
    second = ReduceResult(output="y", levels=[["b"], ["y"]])
    assert first.failures == {} and first.failures is not second.failures
    assert first.complete is True
    assert "failures" not in repr(first)
    mapped = MapReduceResult(frame=None, output="x", levels=[["a"], ["x"]])
    assert mapped.complete is True and mapped.map_failures == {} and mapped.reduce_failures == {}
    partial = MapReduceResult(frame=None, output="x", levels=[], reduce_failures={1: {0: LLMRequestError("e")}})
    assert partial.complete is False
    assert MapReduceResult(frame=None, output="x", levels=[], map_failures={0: LLMRequestError("e")}).complete is False


def test_every_group_failing_keeps_the_levels_that_were_paid_for():
    client = FakeClient(responder=fail_on("R:[a|b]|[c|d]", "R:[e|f]"))
    with pytest.raises(MapReduceError, match="Every group failed at reduce level 2") as info:
        make(client, reduce_group_size=2, reduce_on_error="warn").reduce(list("abcdef"))
    assert not isinstance(info.value, StepFailedError)
    assert info.value.outputs == [None, None]
    assert info.value.levels == [list("abcdef"), ["[a|b]", "[c|d]", "[e|f]"]]
    assert sorted(info.value.failures) == [0, 1]  # the failing level's groups
    assert all(isinstance(error, LLMRequestError) for error in info.value.failures.values())


def test_every_group_failing_after_earlier_failures_carries_only_the_texts_that_made_it():
    # Level 1 drops [c|d] (warned), level 2 then loses both of its groups.
    client = FakeClient(responder=fail_on("R:c|d", "R:[a|b]|[e|f]", "R:[g]"))
    with pytest.raises(MapReduceError, match="Every group failed at reduce level 2") as info:
        make(client, reduce_group_size=2, reduce_on_error="warn").reduce(list("abcdefg"))
    assert info.value.levels == [list("abcdefg"), ["[a|b]", "[e|f]", "[g]"]]
    assert info.value.outputs == [None, None]
    assert sorted(info.value.failures) == [0, 1]


def test_every_group_failing_in_run_also_hands_back_the_mapped_frame():
    client = FakeClient(responder=fail_on("bad", "R:"))
    with pytest.raises(MapReduceError, match="Every group failed at reduce level 1") as info:
        make(client, reduce_group_size=2, reduce_on_error="warn").run(
            frame(["a", "bad", "b", "c"]), "text", "summary", error_column="error"
        )
    assert info.value.frame["summary"].tolist() == ["[a]", None, "[b]", "[c]"]
    assert info.value.frame["error"].tolist() == [None, "blocked", None, None]
    assert list(info.value.map_failures) == [1]
    assert info.value.outputs == [None, None]
    assert sorted(info.value.failures) == [0, 1]
    assert info.value.levels == [["[a]", "[b]", "[c]"]]


# --- make_groups and balance_groups ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("count", "size", "fixed", "balanced"),
    [
        (1, 3, [1], [1]),  # n < size
        (2, 10, [2], [2]),
        (3, 3, [3], [3]),  # n == size
        (4, 3, [3, 1], [2, 2]),
        (7, 3, [3, 3, 1], [3, 2, 2]),
        (11, 10, [10, 1], [6, 5]),
        (20, 10, [10, 10], [10, 10]),
        (21, 10, [10, 10, 1], [7, 7, 7]),
        (5, 2, [2, 2, 1], [2, 2, 1]),
        (101, 10, [10] * 10 + [1], [10, 10] + [9] * 9),
    ],
)
def test_make_groups_table(count, size, fixed, balanced):
    texts = [f"t{i}" for i in range(count)]
    assert [len(group) for group in make_groups(texts, size)] == fixed
    assert [len(group) for group in make_groups(texts, size, balanced=True)] == balanced


@pytest.mark.parametrize("balanced", [False, True])
def test_make_groups_keeps_order_size_and_count(balanced):
    for size in range(2, 8):
        for count in range(1, 60):
            texts = [f"t{i}" for i in range(count)]
            groups = make_groups(texts, size, balanced=balanced)
            assert [text for group in groups for text in group] == texts, (count, size)
            assert len(groups) == math.ceil(count / size), (count, size)
            sizes = [len(group) for group in groups]
            assert all(1 <= length <= size for length in sizes), (count, size)
            if balanced:
                assert max(sizes) - min(sizes) <= 1, (count, size)
                assert sizes == sorted(sizes, reverse=True), (count, size)
            else:
                assert all(length == size for length in sizes[:-1]), (count, size)


@pytest.mark.parametrize("balanced", [False, True])
def test_make_groups_returns_new_lists_and_leaves_the_input_alone(balanced):
    texts = ("a", "b", "c")
    groups = make_groups(texts, 3, balanced=balanced)
    assert groups == [["a", "b", "c"]]
    assert isinstance(groups[0], list)
    same = ["a", "b"]
    (group,) = make_groups(same, 5, balanced=balanced)
    assert group == same and group is not same
    group.append("x")
    assert same == ["a", "b"]


def test_make_groups_of_nothing_fixed():
    assert make_groups([], 3) == []


def test_make_groups_of_nothing_balanced():
    assert make_groups([], 3, balanced=True) == []


@pytest.mark.parametrize("balanced", [False, True])
@pytest.mark.parametrize("empty", [[], ()])
@pytest.mark.parametrize("size", [1, 2, 10, np.int64(3)])
def test_make_groups_of_nothing_is_no_groups_in_both_modes(balanced, empty, size):
    groups = make_groups(empty, size, balanced=balanced)
    assert groups == [] and isinstance(groups, list)


def test_balance_groups_changes_the_grouping_but_not_the_levels():
    texts = [f"t{i:02d}" for i in range(11)]
    fixed_client, balanced_client = FakeClient(responder=bracket), FakeClient(responder=bracket)
    fixed = make(fixed_client, collapse_prompt="C:{text}").reduce(texts)
    balanced = make(balanced_client, collapse_prompt="C:{text}", balance_groups=True).reduce(texts)
    assert contents(fixed_client)[:2] == ["C:" + "|".join(texts[:10]), "C:t10"]
    assert contents(balanced_client)[:2] == ["C:" + "|".join(texts[:6]), "C:" + "|".join(texts[6:])]
    assert [len(level) for level in fixed.levels] == [len(level) for level in balanced.levels] == [11, 2, 1]
    assert fixed.depth == balanced.depth == 2
    assert re.findall(r"t\d\d", balanced.output) == texts


@pytest.mark.parametrize("group_size", [2, 3, 4, 10])
def test_balanced_reduce_has_the_same_level_sizes_and_request_count(group_size):
    for count in range(1, 45):
        client = FakeClient(responder=bracket)
        texts = [f"t{i}" for i in range(count)]
        result = make(client, reduce_group_size=group_size, balance_groups=True).reduce(texts)
        sizes = level_sizes(count, group_size)
        assert [len(level) for level in result.levels] == sizes, count
        assert result.depth == reduce_levels(count, group_size), count
        assert len(client.calls["sync"]) == sum(sizes[1:]), count
        assert re.findall(r"t\d+", result.output) == texts, count
        first_level = [len(re.findall(r"t\d+", content)) for content in contents(client)[: sizes[1]]]
        assert max(first_level) - min(first_level) <= 1 and max(first_level) <= group_size, count


def test_balance_groups_in_run_and_on_the_bars(bars):
    client = FakeClient(responder=bracket)
    mr = make(client, reduce_group_size=4, balance_groups=True, show_progress=True)
    result = mr.run(frame([f"r{i}" for i in range(9)]), "text", "summary")  # 9 → 3 groups of 3 (not 4, 4, 1)
    assert [len(level) for level in result.levels] == [9, 3, 1]
    assert contents(client)[9:12] == ["R:[r0]|[r1]|[r2]", "R:[r3]|[r4]|[r5]", "R:[r6]|[r7]|[r8]"]
    _, reduce_bar, *level_bars = bars
    assert (reduce_bar.total, reduce_bar.n) == (2, 2)
    assert [(bar.desc, bar.total, bar.n) for bar in level_bars] == [
        ("Level 1/2: 9 → 3 [sync]", 3, 3),
        ("Level 2/2: 3 → 1 [sync]", 1, 1),
    ]


def test_failures_with_balanced_groups_are_indexed_by_the_balanced_groups():
    texts = [f"t{i:02d}" for i in range(11)]
    client = FakeClient(responder=fail_on("t07"))
    result = make(client, balance_groups=True, reduce_on_error="warn").reduce(texts)  # groups t00-t05, t06-t10
    assert list(result.failures) == [1] and list(result.failures[1]) == [1]
    assert result.levels[1] == ["[" + "|".join(texts[:6]) + "]"]


# --- exceptions carry what was paid for --------------------------------------------------------------------


def setup_error_on(*needles: str):
    """Reply like ``bracket``, but raise LLMSetupError for prompts containing one of ``needles``."""

    def responder(messages):
        if any(needle in content_of(messages) for needle in needles):
            raise LLMSetupError("Azure rejected the credentials (401: nope).")
        return bracket(messages)

    return responder


@pytest.mark.parametrize("method", ["map", "run"])
def test_setup_error_in_map_carries_the_outputs_and_the_partial_frame(method):
    client = FakeClient(responder=setup_error_on("M:c"))
    df = pd.DataFrame({"text": ["a", None, "b", "c", "d"], "n": range(5)}, index=list("vwxyz"))
    before = df.copy()
    with pytest.raises(LLMSetupError, match="credentials") as info:
        getattr(make(client), method)(df, "text", "summary", error_column="error")
    assert info.value.outputs == ["[a]", None, "[b]", None, None]
    partial = info.value.frame
    assert list(partial.columns) == ["text", "n", "summary", "error"]
    assert partial.index.tolist() == list("vwxyz")
    assert partial["summary"].tolist() == ["[a]", None, "[b]", None, None]
    assert partial["error"].tolist() == [None] * 5
    pd.testing.assert_frame_equal(df, before)
    assert contents(client) == ["M:a", "M:b", "M:c"]  # stopped: d was never sent


def test_step_failed_error_in_map_carries_the_partial_frame_with_the_errors():
    client = FakeClient(responder=fail_on("bad"))
    df = pd.DataFrame({"text": ["ok", "bad", None, "fine"]}, index=[10, 20, 30, 40])
    with pytest.raises(StepFailedError) as info:
        make(client, on_error="raise").map(df, "text", "summary", error_column="why")
    assert info.value.outputs == ["[ok]", None, None, "[fine]"]
    partial = info.value.frame
    assert partial.index.tolist() == [10, 20, 30, 40]
    assert partial["summary"].tolist() == ["[ok]", None, None, "[fine]"]
    assert partial["why"].tolist() == [None, "blocked", None, None]
    assert list(df.columns) == ["text"]


def test_setup_error_in_map_texts_carries_the_outputs():
    client = FakeClient(responder=setup_error_on("M:b"))
    with pytest.raises(LLMSetupError) as info:
        make(client).map_texts(["a", "", "b", "c"])
    assert info.value.outputs == ["[a]", None, None, None]
    assert not hasattr(info.value, "frame")


def test_setup_error_in_a_reduce_level_carries_that_levels_outputs_and_the_levels_so_far():
    client = FakeClient(responder=setup_error_on("R:[e|f]"))
    with pytest.raises(LLMSetupError) as info:
        make(client, reduce_group_size=2).reduce(list("abcdef"))
    assert info.value.outputs == ["[[a|b]|[c|d]]", None]
    assert info.value.levels == [list("abcdef"), ["[a|b]", "[c|d]", "[e|f]"]]


def test_run_map_step_failure_carries_the_partial_frame_and_sends_no_reduce():
    client = FakeClient(responder=fail_on("bad"))
    with pytest.raises(StepFailedError, match="in the map") as info:
        make(client, on_error="raise").run(frame(["a", "bad", "c"], n=[1, 2, 3]), "text", "summary", error_column="e")
    partial = info.value.frame
    assert partial["summary"].tolist() == ["[a]", None, "[c]"]
    assert partial["e"].tolist() == [None, "blocked", None]
    assert partial["n"].tolist() == [1, 2, 3]
    assert not any(content.startswith("R:") for content in contents(client))


def test_run_reduce_failure_carries_the_mapped_frame_and_the_map_failures():
    client = FakeClient(responder=fail_on("bad", "R:[c]"))
    df = frame(["a", "b", "bad", "c"], n=[1, 2, 3, 4])
    mr = make(client, reduce_group_size=2)
    with pytest.raises(StepFailedError, match="in the reduce level 1") as info:
        mr.run(df, "text", "summary", error_column="error")
    mapped = info.value.frame
    assert list(mapped.columns) == ["text", "n", "summary", "error"]
    assert mapped["summary"].tolist() == ["[a]", "[b]", None, "[c]"]
    assert mapped["error"].tolist() == [None, None, "blocked", None]
    assert list(info.value.map_failures) == [2]
    assert info.value.outputs == ["[[a]|[b]]", None]
    assert info.value.levels == [["[a]", "[b]", "[c]"]]
    assert list(df.columns) == ["text", "n"]


def test_run_setup_error_in_the_reduce_carries_the_mapped_frame():
    client = FakeClient(responder=setup_error_on("R:"))
    with pytest.raises(LLMSetupError) as info:
        make(client).run(frame(["a", "b"]), "text", "summary")
    assert info.value.frame["summary"].tolist() == ["[a]", "[b]"]
    assert info.value.map_failures == {}
    assert info.value.outputs == [None]
    assert info.value.levels == [["[a]", "[b]"]]


def test_run_with_nothing_to_reduce_still_hands_back_the_mapped_frame():
    client = FakeClient(responder=fail_on("bad"))
    with pytest.raises(MapReduceError, match="Nothing to reduce") as info:
        make(client).run(frame(["bad", None]), "text", "summary", error_column="error")
    assert info.value.frame["summary"].tolist() == [None, None]
    assert info.value.frame["error"].tolist() == ["blocked", None]
    assert list(info.value.map_failures) == [0]


# --- Ctrl+C keeps what was paid for ------------------------------------------------------------------------


def interrupt_on(*needles: str, then=bracket):
    """A responder that raises KeyboardInterrupt (as Ctrl+C would, mid-request) for prompts containing a needle."""

    def responder(messages):
        if any(needle in content_of(messages) for needle in needles):
            raise KeyboardInterrupt
        return then(messages)

    return responder


@pytest.fixture
def collect_garbage():
    """Run the garbage collector after the test, so asyncio reports a task an interrupt left behind here."""
    yield
    gc.collect()


INTERRUPTIBLE = [("sync",), ("async",), ("async", "sync"), ("sync", "async")]


@pytest.mark.parametrize("strategies", INTERRUPTIBLE)
@pytest.mark.parametrize("method", ["map", "run"])
def test_ctrl_c_in_the_map_carries_the_outputs_and_the_partial_frame(collect_garbage, method, strategies):
    client = FakeClient(responder=interrupt_on("M:c", then=fail_on("bad")))
    df = pd.DataFrame({"text": ["a", None, "bad", "b", "c", "d"], "n": range(6)}, index=list("uvwxyz"))
    before = df.copy()
    with pytest.raises(KeyboardInterrupt) as info:
        getattr(make(client, strategies=strategies), method)(df, "text", "summary", error_column="error")
    assert info.value.outputs == ["[a]", None, None, "[b]", None, None]
    partial = info.value.frame
    assert list(partial.columns) == ["text", "n", "summary", "error"]
    assert partial.index.tolist() == list("uvwxyz")
    assert partial["summary"].tolist() == ["[a]", None, None, "[b]", None, None]
    pd.testing.assert_frame_equal(df, before)
    first = strategies[0]
    # Nothing falls back after an interrupt: the next strategy isn't tried, and nothing after "c" is sent.
    assert [content_of(m) for m in client.calls[first]] == ["M:a", "M:bad", "M:b", "M:c"]
    assert all(client.calls[other] == [] for other in ("sync", "async") if other != first)
    assert not any(content.startswith("R:") for route in ("sync", "async") for content in contents(client, route))


def test_ctrl_c_in_map_texts_carries_the_outputs(collect_garbage):
    for strategies in INTERRUPTIBLE:
        client = FakeClient(responder=interrupt_on("M:c"))
        with pytest.raises(KeyboardInterrupt) as info:
            make(client, strategies=strategies).map_texts(["a", "", "b", "c", "d"])
        assert info.value.outputs == ["[a]", None, "[b]", None, None], strategies
        assert not hasattr(info.value, "frame")


def test_ctrl_c_before_any_reply_still_carries_an_empty_frame(collect_garbage):
    client = FakeClient(responder=interrupt_on("M:"))
    with pytest.raises(KeyboardInterrupt) as info:
        make(client).map(frame(["a", "b"]), "text", "summary")
    assert info.value.outputs == [None, None]
    assert info.value.frame["summary"].tolist() == [None, None]


class YieldingClient(FakeClient):
    """Async requests that give the event loop a turn before answering, as real network calls do."""

    @contextlib.asynccontextmanager
    async def async_session(self):
        async with super().async_session() as complete:

            async def answer(messages):
                await asyncio.sleep(0)
                return await complete(messages)

            yield answer


@pytest.mark.skipif(not hasattr(signal, "raise_signal"), reason="needs signal.raise_signal")
def test_a_real_sigint_during_the_async_map_keeps_the_replies_and_sends_nothing_more():
    if signal.getsignal(signal.SIGINT) is not signal.default_int_handler:
        pytest.skip("SIGINT is handled by something else here")

    def ctrl_c_on_c(messages):
        if content_of(messages) == "M:c":
            signal.raise_signal(signal.SIGINT)  # asyncio.run turns it into a cancellation, then a KeyboardInterrupt
        return bracket(messages)

    client = YieldingClient(responder=ctrl_c_on_c)
    df = frame(list("abcdef"))
    with time_limit(10), pytest.raises(KeyboardInterrupt) as info:
        make(client, strategies=("async", "sync"), max_concurrency=1).map(df, "text", "summary")
    assert info.value.outputs == ["[a]", "[b]", "[c]", None, None, None]  # c's reply had arrived
    assert info.value.frame["summary"].tolist() == ["[a]", "[b]", "[c]", None, None, None]
    assert contents(client, "async") == ["M:a", "M:b", "M:c"]
    assert client.calls["sync"] == []
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_ctrl_c_while_waiting_on_the_batch_api_keeps_the_finished_jobs(clock):
    def sleep(seconds):
        clock.sleep(seconds)
        if len(clock.sleeps) == 5:  # job 1 finished on the 4th poll; job 2 is running
            raise KeyboardInterrupt

    client = FakeClient(responder=bracket)
    mr = make_batch(
        client, clock, strategies=("batch", "sync"), sleep=sleep, map_batch_size=2, max_concurrent_batch_jobs=1
    )
    with pytest.raises(KeyboardInterrupt) as info:
        mr.map(frame(list("abcde")), "text", "summary")
    assert info.value.outputs == ["[a]", "[b]", None, None, None]
    assert info.value.frame["summary"].tolist() == ["[a]", "[b]", None, None, None]
    assert list(client.jobs) == ["batch-1", "batch-2"]  # the third job was never submitted
    assert client.cancelled == ["batch-2"]  # the running one doesn't go on billing
    assert client.calls["sync"] == []


@pytest.mark.parametrize("strategies", INTERRUPTIBLE)
def test_ctrl_c_in_the_first_reduce_level_of_run_keeps_the_map_step(collect_garbage, strategies):
    client = FakeClient(responder=interrupt_on("R:[c]|[d]", then=fail_on("bad")))
    mr = make(client, strategies=strategies, reduce_group_size=2)
    with pytest.raises(KeyboardInterrupt) as info:
        mr.run(frame(["a", "bad", "b", "c", "d", "e"], n=range(6)), "text", "summary", error_column="error")
    mapped = info.value.frame
    assert mapped["summary"].tolist() == ["[a]", None, "[b]", "[c]", "[d]", "[e]"]
    assert mapped["error"].tolist() == [None, "blocked", None, None, None, None]
    assert mapped["n"].tolist() == list(range(6))
    assert list(info.value.map_failures) == [1]
    assert info.value.outputs == ["[[a]|[b]]", None, None]  # this level's replies so far
    assert info.value.levels == [["[a]", "[b]", "[c]", "[d]", "[e]"]]
    route = strategies[0]
    assert contents(client, route)[-2:] == ["R:[a]|[b]", "R:[c]|[d]"]  # "[e]" was never sent
    assert all(client.calls[other] == [] for other in ("sync", "async") if other != route)


@pytest.mark.parametrize("strategies", [("sync",), ("async",)])
def test_ctrl_c_in_a_later_reduce_level_of_run_keeps_the_levels_so_far(collect_garbage, strategies):
    client = FakeClient(responder=interrupt_on("C:[[e]]"))  # level 2 of 3, the second group
    mr = make(client, strategies=strategies, reduce_group_size=2, collapse_prompt="C:{text}")
    with pytest.raises(KeyboardInterrupt) as info:
        mr.run(frame("abcde"), "text", "summary")
    assert info.value.frame["summary"].tolist() == ["[a]", "[b]", "[c]", "[d]", "[e]"]
    assert info.value.map_failures == {}
    assert info.value.levels == [["[a]", "[b]", "[c]", "[d]", "[e]"], ["[[a]|[b]]", "[[c]|[d]]", "[[e]]"]]
    assert info.value.outputs == ["[[[a]|[b]]|[[c]|[d]]]", None]


def test_ctrl_c_in_reduce_on_its_own_carries_the_levels_but_no_frame():
    client = FakeClient(responder=interrupt_on("R:c|d"))
    with pytest.raises(KeyboardInterrupt) as info:
        make(client, reduce_group_size=2).reduce(list("abcdef"))
    assert info.value.outputs == ["[a|b]", None, None]
    assert info.value.levels == [list("abcdef")]
    assert not hasattr(info.value, "frame") and not hasattr(info.value, "map_failures")


def test_bars_and_console_handlers_are_restored_after_ctrl_c(bars, capsys):
    handler = logging.StreamHandler(sys.stderr)
    with root_handler(handler):
        client = FakeClient(responder=interrupt_on("R:"))
        with pytest.raises(KeyboardInterrupt):
            make(client, reduce_group_size=2, show_progress=True).run(frame("abc"), "text", "summary")
        assert handler.stream is sys.stderr
    assert [bar.desc for bar in bars] == ["Map [sync]", "Reduce", "Level 1/2: 3 → 2 [sync]"]
    assert not any(bar in progress_module.tqdm._instances for bar in bars)


# --- adding the output column fails ------------------------------------------------------------------------


def broken_with_outputs(monkeypatch, error: BaseException) -> list:
    """Make PandasFrame.with_outputs raise ``error``; returns the outputs it was asked to add, call by call."""
    calls = []

    def with_outputs(self, output_column, outputs, *, error_column=None, failures=None):
        calls.append(list(outputs))
        raise error

    monkeypatch.setattr(PandasFrame, "with_outputs", with_outputs)
    return calls


@pytest.mark.parametrize("method", ["map", "run"])
def test_a_failure_adding_the_output_column_carries_the_outputs_and_failures(monkeypatch, caplog, method):
    calls = broken_with_outputs(monkeypatch, MemoryError("no room for the column"))
    client = FakeClient(responder=fail_on("bad"))
    with pytest.raises(MemoryError, match="no room") as info:
        getattr(make(client), method)(frame(["a", "bad", None, "b"]), "text", "summary", error_column="error")
    assert info.value.outputs == ["[a]", None, None, "[b]"]
    assert list(info.value.failures) == [1]
    assert isinstance(info.value.failures[1], LLMRequestError) and str(info.value.failures[1]) == "blocked"
    assert calls == [["[a]", None, None, "[b]"]]
    assert not hasattr(info.value, "frame")
    assert contents(client) == ["M:a", "M:bad", "M:b"]  # run() doesn't go on to the reduce
    # The hint about id_column is for Spark; pandas gets no error log.
    assert not [r for r in caplog.records if r.name.startswith("azure_mapreduce") and r.levelno >= logging.ERROR]


def test_a_failure_adding_the_output_column_with_nothing_failed_has_empty_failures(monkeypatch):
    broken_with_outputs(monkeypatch, ValueError("cannot set a frame with no defined index"))
    with pytest.raises(ValueError) as info:
        make(FakeClient(responder=bracket)).map(frame(["a"]), "text", "summary")
    assert info.value.outputs == ["[a]"]
    assert info.value.failures == {}


def test_ctrl_c_while_adding_the_output_column_keeps_the_outputs(monkeypatch):
    broken_with_outputs(monkeypatch, KeyboardInterrupt())
    client = FakeClient(responder=bracket)
    with pytest.raises(KeyboardInterrupt) as info:
        make(client).run(frame(["a", "b"]), "text", "summary")
    assert info.value.outputs == ["[a]", "[b]"]
    assert info.value.failures == {}
    assert not any(content.startswith("R:") for content in contents(client))


def test_a_map_error_is_not_replaced_when_the_partial_frame_cannot_be_built(monkeypatch):
    calls = broken_with_outputs(monkeypatch, RuntimeError("frame broken too"))
    client = FakeClient(responder=setup_error_on("M:b"))
    with pytest.raises(LLMSetupError, match="credentials") as info:
        make(client).map(frame(["a", "b", "c"]), "text", "summary")
    assert info.value.outputs == ["[a]", None, None]  # the replies still go with the original error
    assert not hasattr(info.value, "frame")
    assert calls == [["[a]", None, None]]  # the partial frame was attempted once


# --- map_texts inputs --------------------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["", "   ", "\n"])
def test_map_texts_with_one_blank_string_sends_nothing(text):
    client = FakeClient()
    assert make(client).map_texts(text) == [None]
    assert client.calls["sync"] == []


def test_map_texts_rejects_a_dataframe():
    client = FakeClient()
    with pytest.raises(
        TypeError, match=re.escape("map_texts takes a list of texts; use map(df, column, output_column)")
    ):
        make(client).map_texts(frame(["a", "b"]))
    assert client.calls["sync"] == []


@pytest.mark.parametrize("step", ["map_texts", "reduce"])
def test_a_single_bytes_value_is_one_text(step):
    client = FakeClient(responder=bracket)
    mr = make(client)
    if step == "map_texts":
        assert mr.map_texts(b"hello world") == ["[hello world]"]
        assert contents(client) == ["M:hello world"]
    else:
        assert mr.reduce(b"hello world").output == "[hello world]"
        assert contents(client) == ["R:hello world"]


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (bytearray(b"hello world"), "hello world"),
        ("café au lait".encode(), "café au lait"),
        (b"bad \xff byte", "bad \ufffd byte"),  # not UTF-8: replaced, not an error
        (np.bytes_(b"numpy bytes"), "numpy bytes"),
    ],
)
@pytest.mark.parametrize("step", ["map_texts", "reduce"])
def test_a_single_bytes_like_value_is_decoded_as_one_text(step, value, text):
    client = FakeClient(responder=bracket)
    mr = make(client, collapse_prompt="C:{text}", reduce_group_size=2)
    if step == "map_texts":
        assert mr.map_texts(value) == [f"[{text}]"]
        assert contents(client) == [f"M:{text}"]
    else:
        result = mr.reduce(value)
        assert result.levels == [[text], [f"[{text}]"]]
        assert contents(client) == [f"R:{text}"]  # one text: one call, straight to the reduce prompt


@pytest.mark.parametrize("value", [b"", b"   ", bytearray(b"\n\t")])
def test_a_single_blank_bytes_value_sends_nothing(value):
    client = FakeClient()
    assert make(client).map_texts(value) == [None]
    with pytest.raises(MapReduceError, match="Nothing to reduce"):
        make(client).reduce(value)
    assert client.calls["sync"] == []


def test_a_list_of_bytes_is_still_one_text_per_item():
    client = FakeClient(responder=bracket)
    assert make(client).map_texts([b"a", bytearray(b"b"), b""]) == ["[a]", "[b]", None]
    assert make(client, reduce_group_size=2).reduce([b"x", b"y", b"z"]).levels[0] == ["x", "y", "z"]


def test_reduce_column_with_a_single_bytes_value_is_rejected_too():
    client = FakeClient()
    with pytest.raises(ConfigError, match="column only applies"):
        make(client).reduce(b"one text", "summary")
    assert client.calls["sync"] == []


# --- cells that aren't plain strings -----------------------------------------------------------------------


def test_containers_are_sent_as_json():
    client = FakeClient(responder=bracket)
    values = [
        ["a", "b"],
        ("x", 1, None),
        {"k": "v", "n": 2},
        {"only"},
        frozenset({"one"}),
        np.array([1, 2, 3]),
        np.array([["a"], ["b"]]),
        pd.Series([1.5, 2.5]),
        {1: "int key"},
        ["café", "日本語"],
    ]
    out = make(client).map(frame(values), "text", "summary")
    assert contents(client) == [
        'M:["a", "b"]',
        'M:["x", 1, null]',
        'M:{"k": "v", "n": 2}',
        'M:["only"]',
        'M:["one"]',
        "M:[1, 2, 3]",
        'M:[["a"], ["b"]]',
        "M:[1.5, 2.5]",
        'M:{"1": "int key"}',
        'M:["café", "日本語"]',  # ensure_ascii=False: no \\u escapes
    ]
    assert out["summary"].tolist()[0] == '[["a", "b"]]'


def test_lists_from_a_groupby_are_sent_as_json():
    client = FakeClient(responder=bracket)
    df = pd.DataFrame({"k": [1, 1, 2], "v": ["a", "b", "c"]})
    grouped = df.groupby("k")["v"].agg(list).reset_index()
    make(client).map(grouped, "v", "summary")
    assert contents(client) == ['M:["a", "b"]', 'M:["c"]']


@pytest.mark.parametrize("value", [np.arange(2000), list(range(2000)), np.arange(3000.0).reshape(1000, 3)])
def test_long_arrays_are_not_truncated(value):
    client = FakeClient()
    make(client).map(frame([value]), "text", "summary")
    (content,) = contents(client)
    assert "..." not in content
    assert json.loads(content[2:]) == np.asarray(value).tolist()


def test_nested_numpy_values_are_made_plain():
    client = FakeClient()
    value = {
        "id": np.int64(7),
        "score": np.float32(0.5),
        "ok": np.bool_(True),
        "tags": np.array(["a", "b"]),
        "nested": [np.int32(1), {"x": np.float64(2.5)}, (np.uint8(3),)],
    }
    make(client).map(frame([value]), "text", "summary")
    (content,) = contents(client)
    assert json.loads(content[2:]) == {
        "id": 7,
        "score": 0.5,
        "ok": True,
        "tags": ["a", "b"],
        "nested": [1, {"x": 2.5}, [3]],
    }


def test_empty_containers_are_skipped_like_empty_cells(caplog):
    caplog.set_level(logging.INFO, logger="azure_mapreduce")
    client = FakeClient(responder=bracket)
    empties = [[], (), {}, set(), frozenset(), np.array([]), np.empty((0, 3)), pd.Series([], dtype=object)]
    out = make(client).map(frame(["a", *empties, ["b"]]), "text", "summary", error_column="error")
    assert contents(client) == ["M:a", 'M:["b"]']
    assert out["summary"].tolist() == ["[a]"] + [None] * len(empties) + ['[["b"]]']
    assert out["error"].tolist() == [None] * (len(empties) + 2)
    assert f"Skipping {len(empties)} empty records." in caplog.text


@pytest.mark.parametrize(
    "value",
    [
        None,
        math.nan,
        np.nan,
        np.float32("nan"),
        np.float16("nan"),
        np.datetime64("NaT"),
        np.timedelta64("NaT"),
        pd.NA,
        pd.NaT,
        decimal.Decimal("NaN"),
    ],
)
def test_missing_scalars_are_skipped(value):
    assert as_text(value) is None
    client = FakeClient()
    out = make(client).map(frame(["a", value]), "text", "summary")
    assert contents(client) == ["M:a"]
    assert out["summary"].tolist()[1] is None


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (np.int64(5), "5"),
        (np.float32(1.5), "1.5"),
        (np.bool_(False), "False"),
        (np.str_("hi"), "hi"),
        (0, "0"),
        (0.0, "0.0"),
        (False, "False"),
        (pd.Timestamp("2024-01-02 03:04"), "2024-01-02 03:04:00"),
        (decimal.Decimal("1.10"), "1.10"),
    ],
)
def test_non_missing_scalars_are_sent_as_their_text(value, text):
    assert as_text(value) == text


def test_pyarrow_scalars_and_arrow_backed_list_columns():
    pa = pytest.importorskip("pyarrow")
    assert as_text(pa.scalar("x")) == "x"
    assert as_text(pa.scalar(3)) == "3"
    assert as_text(pa.scalar(None, type=pa.string())) is None
    assert as_text(pa.scalar("  ")) is None
    assert as_text(pa.scalar([1, 2])) == "[1, 2]"
    assert as_text(pa.scalar([], type=pa.list_(pa.int64()))) is None
    assert as_text(pa.scalar({"a": 1})) == '{"a": 1}'
    client = FakeClient(responder=bracket)
    df = pd.DataFrame({"text": pd.Series([[1, 2], None, []], dtype=pd.ArrowDtype(pa.list_(pa.int64())))})
    out = make(client).map(df, "text", "summary")
    assert contents(client) == ["M:[1, 2]"]
    assert out["summary"].tolist() == ["[[1, 2]]", None, None]


@pytest.mark.parametrize(
    "value",
    [
        np.array(["2024-01-01T10:00"], dtype="datetime64[ns]"),
        [np.datetime64("2024-01-01T10:00", "ns")],
        np.array([3600 * 10**9], dtype="timedelta64[ns]"),
    ],
)
def test_numpy_datetimes_in_containers_stay_readable(value):
    text = as_text(value)
    assert text is not None
    assert not re.search(r"\d{12,}", text), text


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (np.array(["2024-01-01T10:00"], dtype="datetime64[ns]"), '["2024-01-01T10:00:00.000000000"]'),
        ([np.datetime64("2024-01-01T10:00", "ns")], '["2024-01-01T10:00:00.000000000"]'),
        ({"when": np.datetime64("2024-01-01T10:00:00.123456789", "ns")}, '{"when": "2024-01-01T10:00:00.123456789"}'),
        ({"day": np.datetime64("2024-03-01", "D")}, '{"day": "2024-03-01"}'),
        ((np.datetime64("2024-03-01T10:30", "m"),), '["2024-03-01T10:30"]'),
        (np.array(["2024-01-01", "NaT"], dtype="datetime64[D]"), '["2024-01-01", "NaT"]'),
        (
            np.array([["2024-01-01"], ["2024-01-02"]], dtype="datetime64[s]"),
            '[["2024-01-01T00:00:00"], ["2024-01-02T00:00:00"]]',
        ),
        ([np.array(np.datetime64("2024-01-01T10:00", "ns"))], '["2024-01-01T10:00:00.000000000"]'),  # 0-d array
        (np.array([np.datetime64("2024-01-01", "D")], dtype=object), '["2024-01-01"]'),
        (
            {"nested": [{"at": np.datetime64("2024-01-01T10:00", "ns")}]},
            '{"nested": [{"at": "2024-01-01T10:00:00.000000000"}]}',
        ),
        ([np.datetime64("NaT")], '["NaT"]'),
        ({"took": np.timedelta64(90, "s")}, '{"took": "0:01:30"}'),
        (np.array([1, 2], dtype="timedelta64[h]"), '["1:00:00", "2:00:00"]'),
        ([np.timedelta64("NaT")], '["NaT"]'),
    ],
)
def test_numpy_datetimes_and_durations_in_containers_become_strings(value, text):
    assert as_text(value) == text


def test_numpy_datetimes_in_a_cell_reach_the_prompt_as_strings():
    client = FakeClient(responder=bracket)
    cells = [
        {"at": np.datetime64("2024-01-01T10:00", "ns"), "took": np.timedelta64(5, "m")},
        np.array(["2024-01-01T10:00", "2024-01-02T11:30"], dtype="datetime64[ns]"),
    ]
    make(client).map(frame(cells), "text", "summary")
    assert contents(client) == [
        'M:{"at": "2024-01-01T10:00:00.000000000", "took": "0:05:00"}',
        'M:["2024-01-01T10:00:00.000000000", "2024-01-02T11:30:00.000000000"]',
    ]


def duration_list(array: np.ndarray) -> list:
    """The same durations as a (nested) list of numpy scalars, which _plain formats one by one."""
    return [duration_list(row) for row in array] if array.ndim > 1 else [array[i] for i in range(len(array))]


@pytest.mark.parametrize(
    "array",
    [
        np.array([1, 2], dtype="timedelta64[s]"),
        np.array([90, 7], dtype="timedelta64[m]"),
        np.array([[1, 2], [3, 4]], dtype="timedelta64[D]"),
        np.array([10**6], dtype="timedelta64[ns]"),
        np.array([3600 * 10**9], dtype="timedelta64[ns]"),
        np.array([10**12], dtype="timedelta64[us]"),
        np.array([[5 * 10**12, 7]], dtype="timedelta64[ns]"),
    ],
)
def test_numpy_duration_arrays_read_like_the_same_durations_in_a_list(array):
    assert as_text(array) == as_text(duration_list(array))  # an array says what the same scalars in a list say


@pytest.mark.parametrize(
    ("value", "text"),
    [
        ([b"abc", bytearray(b"def")], '["abc", "def"]'),
        ((b"x", "y", 1), '["x", "y", 1]'),
        ({"name": b"caf\xc3\xa9"}, '{"name": "café"}'),  # UTF-8, and no \\u escapes
        ({"bad": b"\xff\xfe ok"}, '{"bad": "\ufffd\ufffd ok"}'),  # not UTF-8: replaced, not an error
        ([[b"deep", {"deeper": [bytearray(b"still")]}]], '[["deep", {"deeper": ["still"]}]]'),
        ([np.bytes_(b"numpy")], '["numpy"]'),
        (np.array([b"s1", b"s2"]), '["s1", "s2"]'),  # a numpy bytes ("S") array
        (np.array([b"obj"], dtype=object), '["obj"]'),
        ({"empty": b""}, '{"empty": ""}'),
        (frozenset({b"only"}), '["only"]'),
    ],
)
def test_bytes_inside_containers_are_decoded(value, text):
    assert as_text(value) == text
    assert "b'" not in as_text(value)


def test_bytes_inside_a_cell_reach_the_prompt_decoded():
    client = FakeClient(responder=bracket)
    make(client).map(frame([[b"one", b"two"], {"k": "é".encode()}]), "text", "summary")
    assert contents(client) == ['M:["one", "two"]', 'M:{"k": "é"}']


def test_bytes_dict_keys_are_decoded_too():
    assert as_text({b"name": b"value", "caf\xe9".encode(): 1}) == '{"name": "value", "café": 1}'


# --- building the pandas result ----------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["self", "copy", "index", "columns", "df", "data", "values"])
def test_output_and_error_columns_can_be_named_like_dataframe_attributes(name):
    client = FakeClient(responder=fail_on("bad"))
    out = make(client).map(frame(["a", "bad"]), "text", name, error_column="error")
    assert list(out.columns) == ["text", name, "error"]
    assert out[name].tolist() == ["[a]", None]
    out = make(client).map(frame(["a", "bad"]), "text", "summary", error_column=name)
    assert out[name].tolist() == [None, "blocked"]


def test_input_column_named_self_and_replacing_an_existing_self_column():
    client = FakeClient(responder=bracket)
    df = pd.DataFrame({"self": ["a", "b"], "n": [1, 2]})
    out = make(client).map(df, "self", "self")
    assert list(out.columns) == ["self", "n"]
    assert out["self"].tolist() == ["[a]", "[b]"]
    assert df["self"].tolist() == ["a", "b"]


def test_changing_the_result_leaves_the_input_alone():
    client = FakeClient(responder=bracket)
    df = frame(["a", "b"], n=[1, 2])
    out = make(client).map(df, "text", "summary")
    out.loc[0, "n"] = 99
    out.loc[1, "text"] = "changed"
    assert df["n"].tolist() == [1, 2]
    assert df["text"].tolist() == ["a", "b"]


@pytest.mark.parametrize("which", ["output", "error"])
def test_duplicated_output_or_error_column_label_is_a_config_error(which):
    df = pd.DataFrame([["a", 0, 0]], columns=["text", "dup", "dup"])
    client = FakeClient()
    names = {"output_column": "dup"} if which == "output" else {"output_column": "summary", "error_column": "dup"}
    for method in ("map", "run"):
        with pytest.raises(ConfigError, match='more than one column labelled "dup"'):
            getattr(make(client), method)(df, "text", **names)
    assert client.calls["sync"] == []


def test_duplicated_labels_elsewhere_in_the_frame_are_kept():
    client = FakeClient(responder=bracket)
    df = pd.DataFrame([["a", 1, 2], ["b", 3, 4]], columns=["text", "x", "x"])
    out = make(client).map(df, "text", "summary")
    assert list(out.columns) == ["text", "x", "x", "summary"]
    assert out["summary"].tolist() == ["[a]", "[b]"]
    assert out.iloc[:, 1:3].values.tolist() == [[1, 2], [3, 4]]


# --- everything is checked before any request ----------------------------------------------------------


def dup_frame(*columns: str) -> pd.DataFrame:
    return pd.DataFrame([[f"v{i}" for i in range(len(columns))]], columns=list(columns))


BAD_CALLS = {
    "missing column": (frame(["a"]), ("nope", "summary"), {}),
    "duplicated text column": (dup_frame("text", "text"), ("text", "summary"), {}),
    "duplicated output label": (dup_frame("text", "out", "out"), ("text", "out"), {}),
    "duplicated error label": (dup_frame("text", "err", "err"), ("text", "out"), {"error_column": "err"}),
    "output is the error column": (frame(["a"]), ("text", "out"), {"error_column": "out"}),
    "empty output name": (frame(["a"]), ("text", ""), {}),
    "output name None": (frame(["a"]), ("text", None), {}),
    "output name not a string": (frame(["a"]), ("text", 5), {}),
    "empty error name": (frame(["a"]), ("text", "out"), {"error_column": ""}),
    "error name not a string": (frame(["a"]), ("text", "out"), {"error_column": 7}),
    "id_column with pandas": (frame(["a"], id=[1]), ("text", "out"), {"id_column": "id"}),
}


@pytest.mark.parametrize("method", ["map", "run"])
@pytest.mark.parametrize("case", list(BAD_CALLS))
def test_bad_column_choices_are_config_errors_before_any_request(clock, method, case):
    df, args, kwargs = BAD_CALLS[case]
    client = FakeClient(responder=bracket)
    mr = make_batch(client, clock, strategies=("batch", "async", "sync"))
    with pytest.raises(ConfigError):
        getattr(mr, method)(df, *args, **kwargs)
    assert client.calls == {"sync": [], "async": [], "batch": []}
    assert client.files == {} and client.jobs == {} and client.async_sessions == 0
    assert clock.sleeps == []


# --- logging while the bars are up -------------------------------------------------------------------------


@contextlib.contextmanager
def root_handler(handler: logging.Handler):
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield root
    finally:
        root.removeHandler(handler)
        handler.close()


def watching(responder, *, handler: logging.Handler, seen: list):
    """Wrap a responder to record the root handlers and ``handler``'s stream while requests are being sent."""

    def wrapped(messages):
        seen.append((list(logging.getLogger().handlers), handler.stream, handler.level))
        logging.getLogger("azure_mapreduce.tests").debug("DEBUG-LINE from %s", content_of(messages))
        logging.getLogger("azure_mapreduce.tests").info("INFO-LINE from %s", content_of(messages))
        return responder(messages)

    return wrapped


def test_file_only_logging_stays_file_only(tmp_path, capsys, caplog):
    caplog.set_level(logging.DEBUG, logger="azure_mapreduce")
    path = tmp_path / "run.log"
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setLevel(logging.INFO)
    file_stream = handler.stream
    seen = []
    with root_handler(handler) as root:
        before = list(root.handlers)
        client = FakeClient(responder=watching(fail_on("bad"), handler=handler, seen=seen))
        make(client, show_progress=True).map(frame(["a", "bad", None]), "text", "summary")
        assert root.handlers == before
        assert handler.stream is file_stream and handler.level == logging.INFO
    assert seen and all(handlers == before and stream is file_stream for handlers, stream, _ in seen)
    assert not any(type(h) is logging.StreamHandler for h in before)
    logged = path.read_text(encoding="utf-8")
    assert "1 of 2 records failed in the map: blocked (×1)" in logged
    assert "Skipping 1 empty records." in logged and "INFO-LINE" in logged
    assert "DEBUG-LINE" not in logged
    err = capsys.readouterr().err
    assert "Map [sync]" in err  # the bar
    assert "records failed" not in err and "Skipping" not in err and "INFO-LINE" not in err


def test_warning_level_console_handler_prints_warnings_only(capsys, caplog):
    caplog.set_level(logging.DEBUG, logger="azure_mapreduce")
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.WARNING)
    formatter = logging.Formatter("CONSOLE %(levelname)s %(message)s")
    handler.setFormatter(formatter)
    seen = []
    with root_handler(handler) as root:
        before = list(root.handlers)
        client = FakeClient(responder=watching(fail_on("bad"), handler=handler, seen=seen))
        make(client, reduce_group_size=2, show_progress=True).run(frame(["a", "bad", None, "b"]), "text", "summary")
        assert root.handlers == before
        assert handler.stream is sys.stderr
        assert handler.level == logging.WARNING and handler.formatter is formatter
    # While the bars were up, the handler wrote through tqdm, at the same level.
    assert seen and all(handlers == before for handlers, _, _ in seen)
    assert all(isinstance(stream, progress_module._BarSafeStream) for _, stream, _ in seen)
    assert all(level == logging.WARNING for _, _, level in seen)
    err = capsys.readouterr().err
    assert err.count("CONSOLE WARNING 1 of 3 records failed in the map: blocked (×1)") == 1
    assert "INFO" not in err and "DEBUG" not in err
    assert "Skipping" not in err and "INFO-LINE" not in err and "DEBUG-LINE" not in err
    assert "Skipping 1 empty records." in caplog.text  # emitted, just not printed by the console handler


def test_console_handler_is_restored_when_the_run_fails(capsys):
    handler = logging.StreamHandler(sys.stderr)
    with root_handler(handler):
        client = FakeClient(responder=fail_on("R:"))
        with pytest.raises(StepFailedError):
            make(client, reduce_group_size=2, show_progress=True).run(frame("abc"), "text", "summary")
        assert handler.stream is sys.stderr


def test_handlers_on_other_streams_and_hidden_bars_are_left_alone():
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setLevel(logging.WARNING)
    seen = []
    with root_handler(handler):
        client = FakeClient(responder=watching(fail_on("bad"), handler=handler, seen=seen))
        make(client, show_progress=True).map(frame(["a", "bad"]), "text", "summary")
        assert {stream for _, stream, _ in seen} == {buffer}
    assert "1 of 2 records failed in the map" in buffer.getvalue()

    console = logging.StreamHandler(sys.stderr)
    seen = []
    with root_handler(console):
        client = FakeClient(responder=watching(bracket, handler=console, seen=seen))
        make(client, show_progress=False).map(frame(["a"]), "text", "summary")
        assert [stream for _, stream, _ in seen] == [sys.stderr]
