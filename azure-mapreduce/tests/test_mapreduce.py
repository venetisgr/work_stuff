"""MapReduce's public API: settings, prompts, the map step on pandas, the recursive reduce, run() and the bars."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import re
import signal

import numpy as np
import pandas as pd
import pytest

from azure_mapreduce import MapReduce, MapReduceResult, ReduceResult, reduce_levels
from azure_mapreduce import progress as progress_module
from azure_mapreduce.errors import ConfigError, LLMRequestError, LLMSetupError, MapReduceError, StepFailedError

from .conftest import FakeClient, content_of

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


@pytest.mark.xfail(
    strict=True,
    reason="BUG: placeholder isn't validated: '' splices the text between every character, None raises TypeError",
)
@pytest.mark.parametrize("placeholder", ["", None])
def test_empty_or_missing_placeholder_is_rejected(placeholder):
    with pytest.raises(ConfigError):
        make(FakeClient(), placeholder=placeholder, map_prompt="Summarize: ", reduce_prompt="Combine: ")


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
    with pytest.raises(ConfigError, match="batch_poll_interval must be greater than zero"):
        make(FakeClient(), batch_poll_interval=value)


@pytest.mark.parametrize("value", [0, 0.0, -5])
def test_batch_timeout_must_be_positive_or_none(value):
    with pytest.raises(ConfigError, match="batch_timeout must be greater than zero"):
        make(FakeClient(), batch_timeout=value)
    make(FakeClient(), batch_timeout=None)
    make(FakeClient(), batch_timeout=0.25, batch_poll_interval=0.01)


@pytest.mark.xfail(
    strict=True,
    reason="BUG: non-numeric or NaN batch_poll_interval/batch_timeout raise TypeError or are accepted, not ConfigError",
)
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
    with pytest.raises(ConfigError):
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


@pytest.mark.xfail(
    strict=True,
    reason="BUG: as_text() sends NaN-like numpy scalars in object columns (float32 NaN, datetime64 NaT) as 'nan'/'NaT'",
)
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


@pytest.mark.xfail(
    strict=True, reason="BUG: DataFrame.assign(**{'self': ...}) raises TypeError after every request was already paid"
)
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


@pytest.mark.xfail(
    strict=True, reason="BUG: pandas map with error_column == output_column silently overwrites every output"
)
def test_error_column_with_the_output_column_name_is_rejected():
    client = FakeClient(responder=bracket)
    with pytest.raises(ConfigError):
        make(client).map(frame(["a", "b"]), "text", "summary", error_column="summary")


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


@pytest.mark.xfail(strict=True, reason="BUG: a duplicated column label crashes with AttributeError, not ConfigError")
@pytest.mark.parametrize("step", ["map", "reduce"])
def test_duplicated_column_label_is_a_config_error(step):
    df = pd.DataFrame([["a", "b"]], columns=["text", "text"])
    mr = make(FakeClient())
    with pytest.raises(ConfigError):
        if step == "map":
            mr.map(df, "text", "summary")
        else:
            mr.reduce(df, "text")


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


@pytest.mark.xfail(strict=True, reason="BUG: map_texts('some text') maps every character as its own request")
def test_map_texts_does_not_split_a_single_string_into_characters():
    client = FakeClient(responder=bracket)
    try:
        outputs = make(client).map_texts("hello world")
    except (TypeError, ConfigError):
        outputs = None
    assert contents(client) in ([], ["M:hello world"])
    assert outputs in (None, ["[hello world]"])


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


@pytest.mark.xfail(strict=True, reason="BUG: reduce(str, column) silently ignores the column instead of raising")
def test_reduce_column_with_a_single_string_is_rejected_too():
    with pytest.raises(ConfigError, match="column only applies"):
        make(FakeClient()).reduce("one text", "summary")


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
@pytest.mark.xfail(strict=True, reason="BUG: reduce_levels() hangs for group_size 1 and misbehaves below it")
@pytest.mark.parametrize("group_size", [1, 0, -2])
def test_reduce_levels_rejects_group_sizes_below_two(group_size):
    with time_limit(1.0), pytest.raises((ValueError, ConfigError)):
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
    mr = make(client, collapse_prompt="C:{text}", reduce_group_size=3, show_progress=True)
    result = mr.reduce([f"t{i:02d}" for i in range(10)])  # would take 3 levels: 10 → 4 → 2 → 1
    assert result.levels == [
        [f"t{i:02d}" for i in range(10)],
        ["[t00|t01|t02]", "[t09]"],
        ["[[t00|t01|t02]|[t09]]"],
    ]
    assert result.depth == 2
    assert contents(client)[-1] == "R:[t00|t01|t02]|[t09]"  # the new last level gets the reduce prompt
    assert warnings_in(caplog) == ["2 of 4 groups failed in the reduce level 1: blocked (×2)"]
    reduce_bar, first, second = bars
    assert (reduce_bar.total, reduce_bar.n) == (2, 2)
    assert first.desc == "Level 1/3: 10 → 4 [sync]"
    assert (first.total, first.n) == (4, 4)
    assert "failed=2" in first.postfix
    assert second.desc == "Level 2/2: 2 → 1 [sync]"


def test_a_level_left_with_one_text_after_failures_still_gets_the_final_reduce():
    client = FakeClient(responder=fail_on("t0", "t1"))
    result = make(client, collapse_prompt="C:{text}", reduce_group_size=2).reduce(["t0", "t1", "t2", "t3"])
    assert contents(client)[-1] == "R:[t2|t3]"
    assert result.output == "[[t2|t3]]"
    assert result.depth == 2


def test_every_group_failing_is_an_error(caplog):
    client = FakeClient(responder=fail_on("R:"))
    with pytest.raises(MapReduceError, match="Every group failed at reduce level 1") as info:
        make(client, reduce_group_size=2).reduce(["a", "b", "c"])
    assert not isinstance(info.value, StepFailedError)
    assert warnings_in(caplog) == ["2 of 2 groups failed in the reduce level 1: blocked (×2)"]


def test_the_last_group_failing_is_an_error():
    client = FakeClient(responder=fail_on("R:"))
    with pytest.raises(MapReduceError, match="Every group failed at reduce level 2"):
        make(client, collapse_prompt="C:{text}", reduce_group_size=2).reduce(["a", "b", "c"])
    assert len(client.calls["sync"]) == 3


def test_on_error_raise_stops_the_reduce_at_the_failing_level():
    client = FakeClient(responder=fail_on("c"))
    with pytest.raises(StepFailedError, match="1 of 3 groups failed in the reduce level 1") as info:
        make(client, on_error="raise", reduce_group_size=2).reduce(["a", "b", "c", "d", "e", "f"])
    assert info.value.outputs == ["[a|b]", None, "[e|f]"]
    assert list(info.value.failures) == [1]
    assert str(info.value.failures[1]) == "blocked"
    assert len(client.calls["sync"]) == 3  # no second level


def test_on_error_raise_in_a_later_level_reports_that_level():
    client = FakeClient(responder=fail_on("[a|b]|[c|d]"))
    with pytest.raises(StepFailedError, match="reduce level 2") as info:
        make(client, on_error="raise", reduce_group_size=2).reduce(list("abcdef"))
    assert info.value.outputs == [None, "[[e|f]]"]


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
