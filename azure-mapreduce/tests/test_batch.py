"""BatchRunner against the fake Batch service: chunking, custom_id mapping, polling, timeouts, cleanup, fallback."""

from __future__ import annotations

import dataclasses
import itertools
import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace

import pandas as pd
import pytest

from azure_mapreduce import AzureChatClient, MapReduce, runners
from azure_mapreduce.client import BATCH_ENDPOINT
from azure_mapreduce.errors import LLMRequestError, LLMSetupError
from azure_mapreduce.runners import (
    _CANCEL_GRACE_SECONDS,
    _FILE_READY_TIMEOUT_SECONDS,
    _MAX_POLL_ERRORS,
    BatchRunner,
    FallbackExecutor,
    LLMRequest,
    SyncRunner,
)

from .conftest import FakeClient, FakeJob, chat_body, content_of, echo

TERMINAL = ("completed", "failed", "expired", "cancelled")


# --- helpers ---------------------------------------------------------------------------------------------


class RecordingProgress:
    """Stands in for StepProgress and remembers every call."""

    def __init__(self):
        self.done = 0
        self.failed = 0
        self.advances: list[int] = []
        self.notes: list[str] = []
        self.strategies: list[str] = []

    def strategy(self, name: str) -> None:
        self.strategies.append(name)

    def advance(self, count: int = 1) -> None:
        if count:
            self.advances.append(count)
            self.done += count

    def fail(self, count: int = 1) -> None:
        self.failed += count
        self.done += count

    def note(self, text: str) -> None:
        self.notes.append(text)


@dataclass
class ScriptedClient(FakeClient):
    """FakeClient with a more scriptable Batch service.

    - ``write(job)`` returns the (output lines, error lines) a finished job writes: dicts, or raw strings.
    - ``job_statuses[n]`` replaces ``batch_statuses`` for the n-th job created.
    - ``counts`` are the request_counts.completed reported by a job's successive polls (the last repeats).
    - ``poll_fails(batch_id, attempt)`` says whether that get_batch call (1-based, per job) raises.
    - ``stuck_cancelling``: a cancelled job reports "cancelling" forever.

    ``events`` records ("start" | "end", batch id, ``now()``) when a job is created and first reported over.
    """

    write: Callable[[FakeJob], tuple[list, list]] | None = None
    job_statuses: list[tuple[str, ...]] = field(default_factory=list)
    counts: tuple[int, ...] = ()
    poll_fails: Callable[[str, int], bool] | None = None
    stuck_cancelling: bool = False
    now: Callable[[], float] = lambda: 0.0
    events: list[tuple[str, str, float]] = field(default_factory=list)
    poll_attempts: Counter = field(default_factory=Counter)

    def create_batch(self, input_file_id):
        view = super().create_batch(input_file_id)
        if self.job_statuses:
            self.jobs[view.id].statuses = list(self.job_statuses[len(self.jobs) - 1])
        self.events.append(("start", view.id, self.now()))
        return view

    def get_batch(self, batch_id):
        self.poll_attempts[batch_id] += 1
        if self.poll_fails and self.poll_fails(batch_id, self.poll_attempts[batch_id]):
            raise ConnectionError(f"couldn't reach Azure to check {batch_id}")
        job = self.jobs[batch_id]
        if job.cancelled and self.stuck_cancelling:
            job.polls += 1
            return self._view(job, "cancelling")
        view = super().get_batch(batch_id)
        if self.counts:
            view = dataclasses.replace(view, completed=self.counts[min(job.polls, len(self.counts)) - 1])
        if view.status in TERMINAL and ("end", batch_id) not in [event[:2] for event in self.events]:
            self.events.append(("end", batch_id, self.now()))
        return view

    def _finish(self, job, *, partial):
        if self.write is None:
            return super()._finish(job, partial=partial)
        outputs, errors = self.write(job)
        job.output_file_id = self._store(jsonl(outputs)) if outputs else None
        job.error_file_id = self._store(jsonl(errors)) if errors else None


def jsonl(lines: list) -> str:
    return "\n".join(line if isinstance(line, str) else json.dumps(line) for line in lines)


def ok_line(key, text, **body) -> dict:
    """A batch output line with a reply."""
    return {"custom_id": f"request-{key}", "response": {"status_code": 200, "body": chat_body(text, **body)}}


def http_error_line(key, code, status=400) -> dict:
    """A batch line whose request got an HTTP error (what Azure writes to the error file)."""
    body = {"error": {"code": code, "message": f"{code} happened"}}
    return {"custom_id": f"request-{key}", "response": {"status_code": status, "body": body}, "error": None}


def error_line(key, code) -> dict:
    """A batch line with a top-level error (e.g. a request the job never got to before it expired)."""
    return {"custom_id": f"request-{key}", "response": None, "error": {"code": code, "message": f"{code} happened"}}


def job_keys(job: FakeJob) -> list[int]:
    return [int(line["custom_id"].removeprefix("request-")) for line in job.lines]


def texts(count: int, prefix: str = "t") -> list[str]:
    return [f"{prefix}{index}" for index in range(count)]


def echoed(*keys: int, prefix: str = "t") -> dict[int, str]:
    """What the fake's echo responder replies for these keys of ``make_requests(texts(n))``."""
    return {key: f"<{prefix}{key}>" for key in keys}


def make_requests(prompts: list[str]) -> list[LLMRequest]:
    return [LLMRequest(key, [{"role": "user", "content": prompt}]) for key, prompt in enumerate(prompts)]


def batch_runner(client, clock, **options) -> BatchRunner:
    options.setdefault("poll_interval", 1.0)
    options.setdefault("sleep", clock.sleep)
    return BatchRunner(client, clock=clock, **options)


def run_batch(runner, requests, *, batch_size=100, progress=None):
    """Run the batch runner on its own; returns (results, failures, progress)."""
    progress = progress or RecordingProgress()
    results: dict[int, str] = {}
    failures = runner.run(requests, results, progress, batch_size)
    return results, failures, progress


def sleep_raising(clock, on_call: int, error: BaseException):
    """clock.sleep, except that its ``on_call``-th call raises ``error``."""
    calls = 0

    def sleep(seconds):
        nonlocal calls
        calls += 1
        if calls == on_call:
            raise error
        clock.sleep(seconds)

    return sleep


def watch_calls(client, method: str, *, fail_on: int | None = None, error: Exception | None = None) -> list:
    """Record the calls of ``client.<method>`` (returned list); with ``fail_on``, that call (1-based) raises."""
    original = getattr(client, method)
    calls = []

    def wrapper(*args):
        calls.append(args)
        if len(calls) == fail_on:
            raise error
        return original(*args)

    setattr(client, method, wrapper)
    return calls


def mapreduce(client, clock, **options) -> MapReduce:
    options.setdefault("show_progress", False)
    return MapReduce(
        client,
        map_prompt="Summarize: {text}",
        reduce_prompt="Combine: {text}",
        batch_poll_interval=1.0,
        sleep=clock.sleep,
        clock=clock,
        **options,
    )


class StubOpenAI:
    """Just enough of openai.OpenAI's files and batches APIs for AzureChatClient's Batch API calls.

    A job reports in_progress (1 request done) once, then completed with a reply to every line of its input:
    ``"<body.model>: <last message>"``. With ``fail_errors`` it ends "failed" with those batch errors instead.
    """

    def __init__(self, fail_errors: list | None = None):
        self.fail_errors = fail_errors
        self.uploads: list[dict] = []
        self.batch_creates: list[dict] = []
        self.deleted: list[str] = []
        self.contents: dict[str, str] = {}
        self._inputs: dict[str, str] = {}
        self._outputs: dict[str, str] = {}
        self._polls: Counter = Counter()
        self.files = SimpleNamespace(
            create=self._upload, retrieve=self._file, content=self._content, delete=self.deleted.append
        )
        self.batches = SimpleNamespace(create=self._create, retrieve=self._retrieve, cancel=self._cancel)

    def _upload(self, *, file, purpose):
        name, content, mime = file
        file_id = f"file-{len(self.contents) + 1}"
        self.contents[file_id] = content.decode("utf-8")
        self.uploads.append({"id": file_id, "name": name, "mime": mime, "purpose": purpose})
        return SimpleNamespace(id=file_id, status="uploaded")

    def _file(self, file_id):
        return SimpleNamespace(id=file_id, status="processed", status_details=None)

    def _content(self, file_id):
        return SimpleNamespace(text=self.contents[file_id])

    def _create(self, *, input_file_id, endpoint, completion_window):
        batch_id = f"batch_{len(self.batch_creates) + 1}"
        self.batch_creates.append(
            {"input_file_id": input_file_id, "endpoint": endpoint, "completion_window": completion_window}
        )
        self._inputs[batch_id] = input_file_id
        return self._batch(batch_id, "validating")

    def _retrieve(self, batch_id):
        self._polls[batch_id] += 1
        if self._polls[batch_id] == 1:
            return self._batch(batch_id, "in_progress", completed=1)
        if self.fail_errors is not None:
            return self._batch(batch_id, "failed", errors=SimpleNamespace(object="list", data=self.fail_errors))
        lines = [json.loads(raw) for raw in self.contents[self._inputs[batch_id]].splitlines()]
        if batch_id not in self._outputs:
            replies = [
                {
                    "custom_id": line["custom_id"],
                    "response": {
                        "status_code": 200,
                        "body": chat_body(f"{line['body']['model']}: {line['body']['messages'][-1]['content']}"),
                    },
                    "error": None,
                }
                for line in lines
            ]
            self._outputs[batch_id] = f"file-{len(self.contents) + 1}"
            self.contents[self._outputs[batch_id]] = jsonl(replies)
        return self._batch(batch_id, "completed", completed=len(lines), output_file_id=self._outputs[batch_id])

    def _cancel(self, batch_id):
        return self._batch(batch_id, "cancelling")

    @staticmethod
    def _batch(batch_id, status, *, completed=0, output_file_id=None, errors=None):
        counts = SimpleNamespace(completed=completed, failed=0, total=completed)
        return SimpleNamespace(
            id=batch_id,
            status=status,
            request_counts=counts,
            output_file_id=output_file_id,
            error_file_id=None,
            errors=errors,
        )


# --- basics ----------------------------------------------------------------------------------------------


def test_available_needs_a_batch_deployment():
    assert BatchRunner(FakeClient()).available()
    assert not BatchRunner(FakeClient(batch_deployment=None)).available()


def test_no_requests_submits_nothing(clock):
    client = FakeClient()
    results, failures, progress = run_batch(batch_runner(client, clock), [])
    assert results == {} and failures == {}
    assert not client.files and not client.jobs and clock.sleeps == []


# --- chunking --------------------------------------------------------------------------------------------


def test_one_job_per_batch_size_chunk(clock):
    client = FakeClient()
    results, failures, progress = run_batch(batch_runner(client, clock), make_requests(texts(7)), batch_size=3)
    assert [job_keys(job) for job in client.jobs.values()] == [[0, 1, 2], [3, 4, 5], [6]]
    assert results == echoed(*range(7))
    assert failures == {}
    assert progress.done == 7
    assert progress.notes[-1] == "jobs 3/3 done"


def test_batch_size_one_makes_one_job_per_request(clock):
    client = FakeClient()
    results, _, _ = run_batch(
        batch_runner(client, clock, max_concurrent_jobs=10), make_requests(texts(3)), batch_size=1
    )
    assert [job_keys(job) for job in client.jobs.values()] == [[0], [1], [2]]
    assert results == echoed(0, 1, 2)


def test_max_batch_requests_caps_the_chunk_size(monkeypatch, clock):
    monkeypatch.setattr(runners, "MAX_BATCH_REQUESTS", 2)
    client = FakeClient()
    results, failures, _ = run_batch(batch_runner(client, clock), make_requests(texts(5)), batch_size=100)
    assert [job_keys(job) for job in client.jobs.values()] == [[0, 1], [2, 3], [4]]
    assert results == echoed(*range(5)) and failures == {}


def test_file_size_limit_splits_chunks(monkeypatch, clock):
    client = FakeClient()
    requests = make_requests(texts(6))  # every line has the same length
    line_bytes = len((client.batch_line("request-0", requests[0].messages) + "\n").encode())
    limit = 2 * line_bytes + line_bytes // 2
    monkeypatch.setattr(runners, "MAX_BATCH_FILE_BYTES", limit)
    results, failures, _ = run_batch(batch_runner(client, clock), requests, batch_size=100)
    assert [job_keys(job) for job in client.jobs.values()] == [[0, 1], [2, 3], [4, 5]]
    assert all(len(client.files[job.input_file_id].encode()) <= limit for job in client.jobs.values())
    assert results == echoed(*range(6)) and failures == {}


def test_file_size_chunks_are_greedy_and_keep_the_order(monkeypatch, clock):
    """Mixed sizes: each file stays under the limit unless one line alone is bigger, and nothing is reordered."""
    limit = 600
    monkeypatch.setattr(runners, "MAX_BATCH_FILE_BYTES", limit)
    client = FakeClient()
    prompts = ["x" * 10, "q" * 240, "y" * 40, "z" * 700, "w" * 5, "v" * 200, "u" * 120, "s" * 90, "r" * 30]
    results, failures, _ = run_batch(batch_runner(client, clock, max_concurrent_jobs=20), make_requests(prompts))

    files = [client.files[job.input_file_id].encode("utf-8") for job in client.jobs.values()]
    chunks = [[line + b"\n" for line in content.splitlines()] for content in files]
    custom_ids = [json.loads(line)["custom_id"] for chunk in chunks for line in chunk]
    assert custom_ids == [f"request-{key}" for key in range(len(prompts))]
    for chunk in chunks:
        assert len(chunk) == 1 or sum(map(len, chunk)) <= limit
    for chunk, following in itertools.pairwise(chunks):
        assert sum(map(len, chunk)) + len(following[0]) > limit  # the next line really didn't fit
    assert [len(chunk) for chunk in chunks if sum(map(len, chunk)) > limit] == [1]  # the 700-char prompt, alone
    assert any(len(chunk) > 1 for chunk in chunks)
    assert len(results) == len(prompts) and failures == {}


def test_file_size_limit_counts_utf8_bytes_not_characters(monkeypatch, clock):
    """The real client writes non-ASCII text as UTF-8, so a line takes more bytes than characters."""
    sdk = StubOpenAI()
    client = AzureChatClient(client=sdk, deployment="gpt-std", batch_deployment="gpt-batch-dep")
    requests = make_requests(["é" * 100, "ü" * 100, "ø" * 100])  # same lengths; custom ids too
    line = client.batch_line("request-0", requests[0].messages) + "\n"
    chars, size = len(line), len(line.encode("utf-8"))
    limit = 2 * chars + 10  # two lines would fit if characters were counted, but not bytes
    assert size < limit < 2 * size
    monkeypatch.setattr(runners, "MAX_BATCH_FILE_BYTES", limit)
    results, failures, _ = run_batch(batch_runner(client, clock, max_concurrent_jobs=5), requests)
    assert len(sdk.batch_creates) == 3
    assert all(len(sdk.contents[upload["id"]].encode("utf-8")) <= limit for upload in sdk.uploads)
    assert results == {
        0: "gpt-batch-dep: " + "é" * 100,
        1: "gpt-batch-dep: " + "ü" * 100,
        2: "gpt-batch-dep: " + "ø" * 100,
    }
    assert failures == {}


def test_batch_input_lines_have_the_batch_api_shape(clock):
    client = FakeClient()
    messages = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "first"}]
    requests = [LLMRequest(3, messages), LLMRequest(10, [{"role": "user", "content": "second"}])]
    results, _, _ = run_batch(batch_runner(client, clock), requests)
    raw = client.files[client.jobs["batch-1"].input_file_id]
    assert raw.endswith("\n")
    assert [json.loads(line) for line in raw.splitlines()] == [
        {
            "custom_id": "request-3",
            "method": "POST",
            "url": BATCH_ENDPOINT,
            "body": {"model": "gpt-batch", "messages": messages},
        },
        {
            "custom_id": "request-10",
            "method": "POST",
            "url": "/chat/completions",
            "body": {"model": "gpt-batch", "messages": [{"role": "user", "content": "second"}]},
        },
    ]
    assert results == {3: "<first>", 10: "<second>"}


def test_azure_client_batch_line_targets_the_batch_deployment():
    client = AzureChatClient(
        client=StubOpenAI(),
        deployment="gpt-std",
        batch_deployment="gpt-batch-dep",
        completion_options={"temperature": 0, "max_completion_tokens": 50},
    )
    messages = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "café ☕\nline two"}]
    raw = client.batch_line("request-7", messages)
    assert "\n" not in raw  # one JSONL line, whatever the text holds
    assert "café ☕" in raw  # written as UTF-8, not \u escapes
    assert json.loads(raw) == {
        "custom_id": "request-7",
        "method": "POST",
        "url": "/chat/completions",
        "body": {"model": "gpt-batch-dep", "messages": messages, "temperature": 0, "max_completion_tokens": 50},
    }


def test_real_client_batch_round_trip(clock):
    """BatchRunner with the real AzureChatClient on a stubbed SDK: upload, create, poll, download, clean up."""
    sdk = StubOpenAI()
    client = AzureChatClient(
        client=sdk, deployment="gpt-std", batch_deployment="gpt-batch-dep", completion_options={"temperature": 0}
    )
    requests = make_requests(["café ☕", "b"])
    results, failures, progress = run_batch(batch_runner(client, clock), requests)

    assert sdk.uploads == [{"id": "file-1", "name": "requests.jsonl", "mime": "application/jsonl", "purpose": "batch"}]
    assert sdk.batch_creates == [
        {"input_file_id": "file-1", "endpoint": "/chat/completions", "completion_window": "24h"}
    ]
    lines = [json.loads(raw) for raw in sdk.contents["file-1"].splitlines()]
    assert [line["custom_id"] for line in lines] == ["request-0", "request-1"]
    assert all(line["body"]["model"] == "gpt-batch-dep" and line["body"]["temperature"] == 0 for line in lines)
    assert results == {0: "gpt-batch-dep: café ☕", 1: "gpt-batch-dep: b"}
    assert failures == {}
    assert progress.advances == [1, 1]  # one from request_counts while in progress, one at completion
    assert sdk.deleted == ["file-1", "file-2"]  # input and output


def test_real_client_failed_job_reports_azures_errors(clock):
    errors = [
        SimpleNamespace(code="invalid_json_line", message="Invalid JSON", line=3),
        SimpleNamespace(code=None, message="Model not supported for batch", line=None),
    ]
    sdk = StubOpenAI(fail_errors=errors)
    client = AzureChatClient(client=sdk, deployment="gpt-std", batch_deployment="gpt-batch-dep")
    results, failures, progress = run_batch(batch_runner(client, clock), make_requests(texts(2)))
    assert results == {}
    assert sorted(failures) == [0, 1]
    message = str(failures[0])
    assert "batch job batch_1 ended failed" in message
    assert "invalid_json_line: Invalid JSON (line 3)" in message
    assert "Model not supported for batch" in message
    assert progress.done == 0  # the 1 reported while in progress is taken back
    assert sdk.deleted == ["file-1"]


# --- mapping output lines back to requests ---------------------------------------------------------------


def test_output_lines_out_of_order_map_back_by_custom_id(clock):
    def write(job):
        return [ok_line(key, f"reply {key}") for key in reversed(job_keys(job))], []

    client = ScriptedClient(write=write)
    results, failures, _ = run_batch(batch_runner(client, clock), make_requests(texts(5)), batch_size=3)
    assert results == {key: f"reply {key}" for key in range(5)}
    assert failures == {}


def test_unknown_custom_ids_are_ignored(clock):
    def write(job):
        keys = job_keys(job)
        if keys == [0, 1]:
            lines = [ok_line(0, "zero")]  # key 1 gets no line
        else:
            lines = [ok_line(2, "two"), ok_line(3, "three"), ok_line(1, "one from another job")]
        stray = [
            {"custom_id": "request-99", "response": {"status_code": 200, "body": chat_body("unknown")}},
            {"custom_id": "req-0", "response": {"status_code": 200, "body": chat_body("wrong prefix")}},
            {"custom_id": "request-x", "response": {"status_code": 200, "body": chat_body("not a number")}},
            {"custom_id": "request--1", "response": {"status_code": 200, "body": chat_body("negative")}},
            {"custom_id": None, "response": {"status_code": 200, "body": chat_body("no id")}},
            {"custom_id": 0, "response": {"status_code": 200, "body": chat_body("int id")}},
            {"response": {"status_code": 200, "body": chat_body("missing id")}},
        ]
        return stray + lines, [error_line(98, "server_error")]

    client = ScriptedClient(write=write)
    results, failures, progress = run_batch(batch_runner(client, clock), make_requests(texts(4)), batch_size=2)
    assert results == {0: "zero", 2: "two", 3: "three"}
    assert list(failures) == [1]  # the other job's line for key 1 doesn't count
    assert failures[1].retryable and "batch job batch-1 ended completed" in str(failures[1])
    assert progress.done == 3


def test_duplicate_lines_first_reply_wins_and_a_reply_beats_an_error(clock):
    def write(job):
        outputs = [
            ok_line(0, "first"),
            ok_line(0, "second"),
            http_error_line(1, "server_error", status=500),
            ok_line(1, "one after an error"),
            ok_line(2, "two"),
        ]
        errors = [error_line(2, "content_filter"), error_line(3, "server_error"), error_line(3, "content_filter")]
        return outputs, errors

    client = ScriptedClient(write=write)
    results, failures, progress = run_batch(batch_runner(client, clock), make_requests(texts(4)))
    assert results == {0: "first", 1: "one after an error", 2: "two"}
    assert list(failures) == [3]
    assert failures[3].retryable and failures[3].code == "server_error"  # the first error for a key is kept
    assert progress.done == 3


def test_response_body_sent_as_a_json_string_is_read(clock):
    """Some gateways write the response body as a JSON string rather than an object."""

    def write(job):
        line = {"custom_id": "request-0", "response": {"status_code": 200, "body": json.dumps(chat_body("zero"))}}
        refused = {"custom_id": "request-1", "response": {"status_code": 400, "body": "not json"}}
        return [line, refused], []

    client = ScriptedClient(write=write)
    results, failures, _ = run_batch(batch_runner(client, clock), make_requests(texts(2)))
    assert results == {0: "zero"}
    assert list(failures) == [1] and failures[1].retryable and "status 400" in str(failures[1])


def test_unreadable_and_blank_lines_are_skipped(clock):
    def write(job):
        return ["not json at all", "", "   ", ok_line(0, "zero"), "{truncated", ok_line(1, "one")], []

    client = ScriptedClient(write=write)
    results, failures, _ = run_batch(batch_runner(client, clock), make_requests(texts(3)))
    assert results == {0: "zero", 1: "one"}
    assert list(failures) == [2] and failures[2].retryable


@pytest.mark.xfail(strict=True, reason="BUG: a valid-JSON line that isn't the expected object crashes _collect")
@pytest.mark.parametrize(
    "bad_line",
    [
        "[1, 2]",
        "null",
        '"just a string"',
        '{"custom_id": "request-1", "response": "oops"}',
        '{"custom_id": "request-1", "error": "boom"}',
    ],
)
def test_malformed_json_lines_are_skipped_like_unreadable_ones(clock, bad_line):
    def write(job):
        return [ok_line(0, "zero"), bad_line, ok_line(2, "two")], []

    client = ScriptedClient(write=write)
    results, failures, _ = run_batch(batch_runner(client, clock), make_requests(texts(3)))
    assert results == {0: "zero", 2: "two"}
    assert list(failures) == [1] and failures[1].retryable


# --- error lines -----------------------------------------------------------------------------------------


def error_lines_job(job):
    outputs = [
        ok_line(0, "fine"),
        ok_line(7, "", finish_reason="content_filter"),  # 200, but the reply was filtered
        ok_line(8, "   "),  # 200 with an empty reply
    ]
    errors = [
        http_error_line(1, "content_filter"),
        http_error_line(2, "server_error", status=500),
        error_line(3, "ResponsibleAIPolicyViolation"),
        error_line(4, "batch_expired"),
        http_error_line(5, "context_length_exceeded"),
        http_error_line(6, "429", status=429),
    ]
    return outputs, errors


def test_error_lines_about_the_content_are_final_others_retryable(clock):
    client = ScriptedClient(write=error_lines_job)
    results, failures, _ = run_batch(batch_runner(client, clock), make_requests(texts(9)))
    assert results == {0: "fine"}
    assert {key: error.retryable for key, error in failures.items()} == {
        1: False,
        2: True,
        3: False,
        4: True,
        5: False,
        6: True,
        7: False,
        8: True,
    }
    assert failures[1].code == "content_filter" and "content filter" in str(failures[1])
    assert failures[3].code == "ResponsibleAIPolicyViolation"
    assert failures[5].code == "context_length_exceeded" and "too long" in str(failures[5])
    assert failures[4].code == "batch_expired"
    assert failures[8].code == "empty"


def test_error_lines_through_fallback_only_retryable_ones_go_to_sync(clock):
    client = ScriptedClient(write=error_lines_job, sync_responder=lambda messages: "sync")
    executor = FallbackExecutor([batch_runner(client, clock), SyncRunner(client)])
    progress = RecordingProgress()
    results, failures = executor.run(make_requests(texts(9)), progress, batch_size=100)
    assert results == {0: "fine", 2: "sync", 4: "sync", 6: "sync", 8: "sync"}
    assert sorted(failures) == [1, 3, 5, 7]
    assert sorted(content_of(messages) for messages in client.calls["sync"]) == ["t2", "t4", "t6", "t8"]
    assert progress.done == 9 and progress.failed == 4
    assert progress.strategies == ["batch", "sync"]


# --- progress --------------------------------------------------------------------------------------------


def test_progress_follows_request_counts_then_settles_on_the_real_replies(clock):
    """Counts reported while polling move the bar; at collection it's corrected to the replies actually read."""

    def write(job):
        # Azure counts 4 requests completed, but one reply was blocked by the content filter.
        return [ok_line(0, "a"), ok_line(1, "b"), ok_line(2, "c"), ok_line(3, "", finish_reason="content_filter")], []

    client = ScriptedClient(
        write=write, batch_statuses=("validating", "in_progress", "in_progress", "completed"), counts=(0, 1, 3, 4)
    )
    results, failures, progress = run_batch(batch_runner(client, clock), make_requests(texts(4)))
    assert progress.advances == [1, 2, 1, -1]
    assert progress.done == len(results) == 3
    assert list(failures) == [3] and not failures[3].retryable


def test_progress_catches_up_when_request_counts_lag(clock):
    client = ScriptedClient(counts=(0,))  # the service never reports progress
    results, _, progress = run_batch(batch_runner(client, clock), make_requests(texts(4)))
    assert progress.advances == [4]
    assert progress.done == len(results) == 4


def test_progress_per_job_across_several_jobs(clock):
    client = ScriptedClient(batch_statuses=("in_progress", "in_progress", "completed"), counts=(1, 2, 2))
    results, _, progress = run_batch(batch_runner(client, clock), make_requests(texts(5)), batch_size=2)
    # jobs of 2, 2 and 1 requests; the last job's count (2) is more than it had, and is taken back.
    assert progress.done == len(results) == 5
    assert progress.advances.count(-1) == 1


def test_progress_notes_count_jobs(clock):
    client = FakeClient()
    _, _, progress = run_batch(
        batch_runner(client, clock, max_concurrent_jobs=2), make_requests(texts(5)), batch_size=2
    )
    assert progress.notes[0] == "jobs 0/3 done · 1 validating"
    assert "jobs 0/3 done · 2 in_progress" in progress.notes
    assert "jobs 2/3 done · 1 validating" in progress.notes  # the third job, submitted once a slot freed
    assert progress.notes[-1] == "jobs 3/3 done"


def test_progress_reaches_the_total_through_fallback(clock):
    def responder(messages):
        if content_of(messages) == "t1":
            raise LLMRequestError("blocked", retryable=False, code="content_filter")
        return echo(messages)

    client = FakeClient(
        batch_statuses=("validating", "expired"), batch_responder=responder, sync_responder=lambda m: "sync"
    )
    executor = FallbackExecutor([batch_runner(client, clock), SyncRunner(client)])
    progress = RecordingProgress()
    results, failures = executor.run(make_requests(texts(4)), progress, batch_size=100)
    assert results == {0: "<t0>", 2: "sync", 3: "sync"}
    assert list(failures) == [1]
    assert progress.done == 4 and progress.failed == 1


@pytest.mark.xfail(
    strict=True,
    reason="BUG: request_counts estimates aren't taken back when the batch run breaks; the bar overshoots",
)
def test_progress_estimate_taken_back_when_the_batch_run_breaks(clock):
    client = ScriptedClient(batch_statuses=("in_progress",), counts=(3,))
    batch = batch_runner(client, clock, sleep=sleep_raising(clock, 2, RuntimeError("poll loop broke")))
    executor = FallbackExecutor([batch, SyncRunner(client)])
    progress = RecordingProgress()
    results, failures = executor.run(make_requests(texts(4)), progress, batch_size=100)
    assert results == echoed(0, 1, 2, 3) and failures == {}
    assert progress.done == 4  # 3 estimated by the batch job that never delivered, + 4 from sync


def test_map_bar_names_the_batch_strategy_and_counts_jobs(clock, capsys):
    client = FakeClient()
    mr = mapreduce(client, clock, map_batch_size=2, show_progress=True)
    assert mr.map_texts(["a", "b", "c"]) == ["<Summarize: a>", "<Summarize: b>", "<Summarize: c>"]
    err = capsys.readouterr().err
    assert "Map [batch]" in err
    assert "jobs 2/2 done" in err
    assert "3/3" in err


# --- concurrency -----------------------------------------------------------------------------------------


def test_max_concurrent_jobs_respected(clock):
    def finishes_after(polls):
        return ("in_progress",) * (polls - 1) + ("completed",)

    client = ScriptedClient(job_statuses=[finishes_after(n) for n in (2, 5, 3, 1, 2)], now=clock)
    runner = batch_runner(client, clock, max_concurrent_jobs=2)
    results, failures, _ = run_batch(runner, make_requests(texts(5)), batch_size=1)
    assert results == echoed(*range(5)) and failures == {}

    active = peak = 0
    for kind, _, _ in client.events:
        active += 1 if kind == "start" else -1
        peak = max(peak, active)
    assert peak == 2
    # A freed slot is refilled at once: job 3 starts when job 1 ends, jobs 4 and 5 when jobs 2 and 3 end.
    starts = {batch_id: time for kind, batch_id, time in client.events if kind == "start"}
    ends = {batch_id: time for kind, batch_id, time in client.events if kind == "end"}
    assert starts == {"batch-1": 0.0, "batch-2": 0.0, "batch-3": 2.0, "batch-4": 5.0, "batch-5": 5.0}
    assert ends == {"batch-1": 2.0, "batch-2": 5.0, "batch-3": 5.0, "batch-4": 6.0, "batch-5": 7.0}


@pytest.mark.parametrize("limit", [1, 3])
def test_never_more_jobs_than_the_limit(clock, limit):
    client = ScriptedClient(now=clock)
    results, _, _ = run_batch(
        batch_runner(client, clock, max_concurrent_jobs=limit), make_requests(texts(7)), batch_size=1
    )
    active = peak = 0
    for kind, _, _ in client.events:
        active += 1 if kind == "start" else -1
        peak = max(peak, active)
    assert peak == limit
    assert len(client.jobs) == 7 and results == echoed(*range(7))


def test_timeout_counts_from_each_jobs_submission(clock):
    """A job that waited for a free slot gets its full timeout once it's submitted."""
    client = ScriptedClient(job_statuses=[("in_progress", "in_progress", "completed")] * 2)
    runner = batch_runner(client, clock, max_concurrent_jobs=1, timeout=4.0)
    results, failures, _ = run_batch(runner, make_requests(texts(2)), batch_size=1)
    assert client.cancelled == []
    assert results == echoed(0, 1) and failures == {}


# --- jobs that end badly ---------------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["failed", "expired", "cancelled"])
def test_job_that_ends_badly_keeps_its_replies_and_hands_back_the_rest(clock, status):
    client = FakeClient(
        batch_statuses=("validating", "in_progress", status),
        batch_errors=("token_limit_exceeded: Enqueued token limit reached",),
    )
    results, failures, progress = run_batch(batch_runner(client, clock), make_requests(texts(4)))
    kept = {} if status == "failed" else echoed(0, 1)  # the fake answers the first half before expiring
    assert results == kept
    assert sorted(failures) == [key for key in range(4) if key not in kept]
    for error in failures.values():
        assert error.retryable and error.code == "batch"
        assert f"batch job batch-1 ended {status}" in str(error)
    if status == "failed":
        assert "Enqueued token limit reached" in str(failures[0])
    assert client.cancelled == []  # it's already over: nothing to cancel
    assert client.jobs["batch-1"].input_file_id in client.deleted
    assert progress.done == len(kept)


def test_completed_job_with_missing_lines_hands_those_back(clock):
    client = FakeClient(batch_responder=lambda m: None if content_of(m) in ("t1", "t3") else echo(m))
    results, failures, _ = run_batch(batch_runner(client, clock), make_requests(texts(4)))
    assert results == echoed(0, 2)
    assert sorted(failures) == [1, 3]
    assert all(error.retryable and "ended completed" in str(error) for error in failures.values())


def test_completed_job_without_any_files_hands_everything_back(clock):
    client = FakeClient(batch_responder=lambda m: None)
    results, failures, _ = run_batch(batch_runner(client, clock), make_requests(texts(3)))
    assert results == {}
    assert sorted(failures) == [0, 1, 2] and all(error.retryable for error in failures.values())


def test_download_failure_hands_the_jobs_requests_back(clock):
    client = FakeClient()
    watch_calls(client, "read_file", fail_on=1, error=ConnectionError("download reset"))
    results, failures, _ = run_batch(batch_runner(client, clock), make_requests(texts(4)), batch_size=2)
    assert results == echoed(2, 3)
    assert sorted(failures) == [0, 1] and all(error.retryable for error in failures.values())


# --- timeouts and cancelling -----------------------------------------------------------------------------


def test_timeout_cancels_the_job_then_collects_it_once_cancelled(clock):
    client = FakeClient(batch_statuses=("in_progress",))
    runner = batch_runner(client, clock, poll_interval=4.0, timeout=10.0)
    results, failures, _ = run_batch(runner, make_requests(texts(4)))
    assert client.cancelled == ["batch-1"]
    assert clock.sleeps == [4.0] * 4  # cancelled at t=12 (past 10s), collected at the next check
    assert results == echoed(0, 1)  # what the job finished before it was cancelled
    assert sorted(failures) == [2, 3]
    assert all(error.retryable and "ended cancelled" in str(error) for error in failures.values())
    job = client.jobs["batch-1"]
    assert set(client.deleted) == {job.input_file_id, job.output_file_id}


def test_no_timeout_waits_for_the_job(clock):
    client = ScriptedClient(job_statuses=[("in_progress",) * 99 + ("completed",)])
    results, failures, _ = run_batch(batch_runner(client, clock, poll_interval=30.0), make_requests(texts(2)))
    assert client.cancelled == []
    assert results == echoed(0, 1) and failures == {}
    assert clock.now == 100 * 30.0


def test_job_stuck_cancelling_is_given_up_after_the_grace_period(clock):
    client = ScriptedClient(batch_statuses=("in_progress",), stuck_cancelling=True)
    runner = batch_runner(client, clock, poll_interval=30.0, timeout=60.0)
    results, failures, _ = run_batch(runner, make_requests(texts(3)))
    cancelled_at = 90.0  # the first check past 60s
    assert client.cancelled == ["batch-1"]  # asked once, not on every check
    assert cancelled_at + _CANCEL_GRACE_SECONDS < clock.now <= cancelled_at + _CANCEL_GRACE_SECONDS + 30.0
    assert results == {}
    assert sorted(failures) == [0, 1, 2] and all(error.retryable for error in failures.values())


def test_failed_cancel_keeps_watching_and_uses_the_replies_if_the_job_finishes(clock, caplog):
    client = ScriptedClient(job_statuses=[("in_progress",) * 4 + ("completed",)])
    watch_calls(client, "cancel_batch", fail_on=1, error=ConnectionError("cancel refused"))
    runner = batch_runner(client, clock, timeout=2.0)
    results, failures, _ = run_batch(runner, make_requests(texts(3)))
    assert results == echoed(0, 1, 2) and failures == {}  # it finished before the grace period ran out
    assert "Couldn't cancel batch job batch-1" in caplog.text


def test_timeout_through_mapreduce_falls_back_to_sync(clock):
    client = FakeClient(batch_statuses=("in_progress",), sync_responder=lambda m: f"sync {content_of(m)}")
    mr = mapreduce(client, clock, strategies=("batch", "sync"), batch_timeout=5.0)
    out = mr.map_texts(["a", "b", "c", "d"])
    assert out == ["<Summarize: a>", "<Summarize: b>", "sync Summarize: c", "sync Summarize: d"]
    assert client.cancelled == ["batch-1"]


# --- polling errors --------------------------------------------------------------------------------------


def test_repeated_poll_errors_give_up_on_the_job(clock, caplog):
    client = FakeClient(poll_error=ConnectionError("connection reset"))
    results, failures, _ = run_batch(batch_runner(client, clock), make_requests(texts(3)))
    assert len(clock.sleeps) == _MAX_POLL_ERRORS
    assert client.cancelled == ["batch-1"]  # don't leave it running (and billing) unwatched
    assert results == {}
    assert sorted(failures) == [0, 1, 2] and all(error.retryable for error in failures.values())
    assert "Giving up on batch job batch-1" in caplog.text
    assert client.jobs["batch-1"].input_file_id in client.deleted


def test_poll_errors_that_clear_up_dont_give_up(clock):
    pattern = [True] * (_MAX_POLL_ERRORS - 1) + [False] + [True] * (_MAX_POLL_ERRORS - 1)

    def poll_fails(batch_id, attempt):
        return attempt <= len(pattern) and pattern[attempt - 1]

    client = ScriptedClient(poll_fails=poll_fails)
    results, failures, _ = run_batch(batch_runner(client, clock), make_requests(texts(3)))
    assert results == echoed(0, 1, 2) and failures == {}
    assert client.cancelled == []


def test_poll_errors_only_give_up_on_that_job(clock):
    client = ScriptedClient(poll_fails=lambda batch_id, attempt: batch_id == "batch-1")
    results, failures, _ = run_batch(batch_runner(client, clock), make_requests(texts(4)), batch_size=2)
    assert results == echoed(2, 3)
    assert sorted(failures) == [0, 1] and all(error.retryable for error in failures.values())
    assert client.cancelled == ["batch-1"]


# --- submitting ------------------------------------------------------------------------------------------


def test_first_upload_failure_raises(clock):
    client = FakeClient(upload_error=RuntimeError("Batch API not enabled"))
    uploads = watch_calls(client, "upload_batch_file")
    with pytest.raises(RuntimeError, match="Batch API not enabled"):
        run_batch(batch_runner(client, clock), make_requests(texts(6)), batch_size=2)
    assert len(uploads) == 1  # no point trying the other chunks
    assert not client.jobs


def test_first_create_failure_raises_and_deletes_the_upload(clock):
    client = FakeClient(create_error=LLMSetupError("not a batch deployment"))
    with pytest.raises(LLMSetupError, match="not a batch deployment"):
        run_batch(batch_runner(client, clock), make_requests(texts(4)), batch_size=2)
    assert client.deleted == ["file-1"]
    assert list(client.files) == ["file-1"]


@pytest.mark.parametrize("status", ["error", "failed", "deleted"])
def test_first_input_file_rejected_raises(clock, status):
    client = FakeClient(file_statuses=(status,))
    with pytest.raises(LLMSetupError, match="couldn't process the batch input file") as caught:
        run_batch(batch_runner(client, clock), make_requests(texts(2)))
    if status == "error":
        assert "bad file" in str(caught.value)
    assert client.deleted == ["file-1"] and not client.jobs


def test_first_submission_failure_hands_everything_to_the_next_strategy(clock):
    client = FakeClient(upload_error=RuntimeError("Batch API not enabled"), sync_responder=lambda m: "sync")
    uploads = watch_calls(client, "upload_batch_file")
    executor = FallbackExecutor([batch_runner(client, clock), SyncRunner(client)])
    for _ in range(2):  # e.g. the map step, then a reduce level
        results, failures = executor.run(make_requests(texts(3)), RecordingProgress(), batch_size=1)
        assert results == dict.fromkeys(range(3), "sync") and failures == {}
    assert executor.broken == {"batch"}
    assert len(uploads) == 1  # skipped for the rest of the run


@pytest.mark.parametrize("stage", ["upload", "create", "file"])
def test_later_submission_failure_fails_only_that_chunk(clock, stage):
    client = FakeClient()
    if stage == "upload":
        watch_calls(client, "upload_batch_file", fail_on=2, error=RuntimeError("upload refused"))
    elif stage == "create":
        watch_calls(client, "create_batch", fail_on=2, error=RuntimeError("enqueued token quota exceeded"))
    else:
        client.file_statuses = ("processed", "error", "processed")
    results, failures, progress = run_batch(batch_runner(client, clock), make_requests(texts(6)), batch_size=2)
    assert results == echoed(0, 1, 4, 5)
    assert sorted(failures) == [2, 3]
    assert all(error.retryable and "couldn't submit the batch job" in str(error) for error in failures.values())
    assert len(client.jobs) == 2
    assert progress.notes[-1] == "jobs 3/3 done"
    if stage != "upload":  # the failed chunk's upload isn't left behind
        assert "file-2" in client.deleted
        assert "file-2" not in {job.input_file_id for job in client.jobs.values()}


def test_later_submission_failure_goes_to_the_next_strategy(clock):
    client = FakeClient(sync_responder=lambda m: "sync")
    watch_calls(client, "create_batch", fail_on=2, error=RuntimeError("enqueued token quota exceeded"))
    executor = FallbackExecutor([batch_runner(client, clock), SyncRunner(client)])
    results, failures = executor.run(make_requests(texts(4)), RecordingProgress(), batch_size=2)
    assert results == {0: "<t0>", 1: "<t1>", 2: "sync", 3: "sync"} and failures == {}
    assert executor.broken == set()  # batch still works for the next step


# --- waiting for the input file --------------------------------------------------------------------------


def test_waits_for_the_input_file_to_be_processed(clock):
    client = ScriptedClient(file_statuses=("pending", "running", "processed"), now=clock)
    results, failures, _ = run_batch(batch_runner(client, clock, poll_interval=30.0), make_requests(texts(2)))
    assert clock.sleeps[:2] == [5.0, 5.0]  # file checks are at most 5s apart
    assert client.events[0] == ("start", "batch-1", 10.0)  # the job was created once the file was processed
    assert results == echoed(0, 1) and failures == {}


def test_file_checks_follow_a_short_poll_interval(clock):
    client = FakeClient(file_statuses=("pending", "processed"))
    run_batch(batch_runner(client, clock, poll_interval=0.5), make_requests(texts(1)))
    assert clock.sleeps[0] == 0.5


def test_input_file_without_a_status_counts_as_processed(clock):
    client = FakeClient(file_statuses=(None,))
    results, _, _ = run_batch(batch_runner(client, clock), make_requests(texts(2)))
    assert results == echoed(0, 1)
    assert clock.sleeps == [1.0] * 4  # only the job's own polls


def test_input_file_never_processed_times_out(clock):
    client = FakeClient(file_statuses=("pending",))
    with pytest.raises(LLMSetupError, match="still pending"):
        run_batch(batch_runner(client, clock, poll_interval=30.0), make_requests(texts(2)))
    assert _FILE_READY_TIMEOUT_SECONDS < clock.now <= _FILE_READY_TIMEOUT_SECONDS + 5.0
    assert client.deleted == ["file-1"] and not client.jobs


def test_later_input_file_timing_out_fails_only_that_chunk(clock):
    client = FakeClient(file_statuses=("processed",) + ("pending",) * 1000)
    results, failures, _ = run_batch(
        batch_runner(client, clock, poll_interval=30.0), make_requests(texts(4)), batch_size=2
    )
    assert results == echoed(0, 1)
    assert sorted(failures) == [2, 3] and all("still pending" in str(error) for error in failures.values())


# --- cleanup ---------------------------------------------------------------------------------------------


def test_cleanup_deletes_input_output_and_error_files(clock):
    def responder(messages):
        if content_of(messages) == "t1":
            raise LLMRequestError("blocked", retryable=False, code="content_filter")
        return echo(messages)

    client = FakeClient(batch_responder=responder)
    run_batch(batch_runner(client, clock), make_requests(texts(4)), batch_size=2)
    assert client.jobs["batch-1"].error_file_id is not None  # job 1 had an error file, job 2 didn't
    assert client.jobs["batch-2"].error_file_id is None
    assert sorted(client.deleted) == sorted(client.files)  # 2 inputs, 2 outputs, 1 error file
    assert len(client.deleted) == 5


def test_no_cleanup_keeps_the_files(clock):
    client = FakeClient(batch_statuses=("validating", "expired"))
    results, _, _ = run_batch(batch_runner(client, clock, cleanup=False), make_requests(texts(4)), batch_size=2)
    assert results == echoed(0, 2)
    assert client.deleted == []


def test_cleanup_failure_is_not_an_error(clock):
    client = FakeClient()
    watch_calls(client, "delete_file", fail_on=1, error=RuntimeError("delete refused"))
    results, failures, _ = run_batch(batch_runner(client, clock), make_requests(texts(2)))
    assert results == echoed(0, 1) and failures == {}


# --- interruptions ---------------------------------------------------------------------------------------


def test_keyboard_interrupt_while_polling_cancels_running_jobs(clock):
    client = FakeClient()
    sleep = sleep_raising(clock, 3, KeyboardInterrupt())
    runner = batch_runner(client, clock, max_concurrent_jobs=2, sleep=sleep)
    with pytest.raises(KeyboardInterrupt):
        run_batch(runner, make_requests(texts(6)), batch_size=2)
    assert client.cancelled == ["batch-1", "batch-2"]
    assert len(client.jobs) == 2  # the third chunk was never submitted


def test_keyboard_interrupt_through_mapreduce_cancels_and_stops(clock):
    client = FakeClient()
    mr = MapReduce(
        client,
        map_prompt="Summarize: {text}",
        reduce_prompt="Combine: {text}",
        show_progress=False,
        batch_poll_interval=1.0,
        sleep=sleep_raising(clock, 2, KeyboardInterrupt()),
        clock=clock,
    )
    with pytest.raises(KeyboardInterrupt):
        mr.map_texts(["a", "b"])
    assert client.cancelled == ["batch-1"]
    assert not client.calls["sync"] and not client.calls["async"]  # an interrupt isn't a reason to fall back


def test_interrupt_still_raised_when_cancelling_fails(clock, caplog):
    client = FakeClient()
    cancels = watch_calls(client, "cancel_batch", fail_on=1, error=ConnectionError("cancel refused"))
    runner = batch_runner(client, clock, sleep=sleep_raising(clock, 2, KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        run_batch(runner, make_requests(texts(4)), batch_size=2)
    assert [call[0] for call in cancels] == ["batch-1", "batch-2"]  # the second job is still cancelled
    assert client.cancelled == ["batch-2"]
    assert "cancel it in the Azure portal" in caplog.text


def test_interrupt_doesnt_cancel_a_job_twice(clock):
    client = FakeClient(batch_statuses=("in_progress",))
    runner = batch_runner(client, clock, timeout=2.0, sleep=sleep_raising(clock, 4, KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        run_batch(runner, make_requests(texts(2)))
    assert client.cancelled == ["batch-1"]  # cancelled at t=3 for the timeout; already cancelling at the interrupt


def test_interrupt_while_waiting_for_the_input_file_deletes_it(clock):
    client = FakeClient(file_statuses=("pending",))
    runner = batch_runner(client, clock, sleep=sleep_raising(clock, 1, KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        run_batch(runner, make_requests(texts(2)))
    assert client.deleted == ["file-1"] and not client.jobs


def test_error_while_polling_cancels_jobs_and_the_next_strategy_takes_over(clock):
    client = FakeClient(sync_responder=lambda m: "sync")
    batch = batch_runner(client, clock, sleep=sleep_raising(clock, 2, RuntimeError("poll loop broke")))
    executor = FallbackExecutor([batch, SyncRunner(client)])
    results, failures = executor.run(make_requests(texts(4)), RecordingProgress(), batch_size=2)
    assert client.cancelled == ["batch-1", "batch-2"]
    assert results == dict.fromkeys(range(4), "sync") and failures == {}
    assert executor.broken == {"batch"}


def test_error_while_polling_keeps_replies_already_collected(clock):
    """Jobs collected before the loop broke keep their replies; only the rest go to the next strategy."""
    client = ScriptedClient(
        job_statuses=[("completed",), ("in_progress",)], sync_responder=lambda m: f"sync {content_of(m)}"
    )
    batch = batch_runner(client, clock, sleep=sleep_raising(clock, 2, RuntimeError("poll loop broke")))
    executor = FallbackExecutor([batch, SyncRunner(client)])
    results, failures = executor.run(make_requests(texts(4)), RecordingProgress(), batch_size=2)
    assert results == {0: "<t0>", 1: "<t1>", 2: "sync t2", 3: "sync t3"} and failures == {}
    assert client.cancelled == ["batch-2"]


# --- through MapReduce -----------------------------------------------------------------------------------


def test_mapreduce_missing_batch_lines_go_to_sync(clock):
    def batch_responder(messages):
        return None if content_of(messages) in ("Summarize: r2", "Summarize: r5") else echo(messages)

    client = FakeClient(batch_responder=batch_responder, sync_responder=lambda m: f"sync {content_of(m)}")
    df = pd.DataFrame({"review": [f"r{i}" for i in range(8)]})
    mr = mapreduce(client, clock, strategies=("batch", "sync"), map_batch_size=4, reduce_group_size=3)
    result = mr.run(df, "review", "summary")

    expected = [f"<Summarize: r{i}>" for i in range(8)]
    expected[2], expected[5] = "sync Summarize: r2", "sync Summarize: r5"
    assert result.frame["summary"].tolist() == expected
    assert [content_of(messages) for messages in client.calls["sync"]] == ["Summarize: r2", "Summarize: r5"]
    assert result.map_failures == {}
    assert [len(level) for level in result.levels] == [8, 3, 1]
    assert len(client.jobs) == 2 + 1 + 1  # map: 2 jobs of 4; each reduce level fits in one job
    assert result.output.startswith("<Combine: <Combine: <Summarize: r0>")


def test_mapreduce_content_filtered_line_is_not_retried(clock, caplog):
    def batch_responder(messages):
        if content_of(messages) == "Summarize: r1":
            raise LLMRequestError("blocked", retryable=False, code="content_filter")
        return echo(messages)

    client = FakeClient(batch_responder=batch_responder)
    df = pd.DataFrame({"review": ["r0", "r1", "r2"]})
    out = mapreduce(client, clock, strategies=("batch", "sync")).map(df, "review", "summary", error_column="error")
    assert out["summary"].tolist() == ["<Summarize: r0>", None, "<Summarize: r2>"]
    assert "content filter" in out["error"][1]
    assert out["error"].isna().tolist() == [True, False, True]
    assert not client.calls["sync"]
    assert "1 of 3 records failed in the map" in caplog.text


def test_reduce_batch_size_splits_reduce_groups_into_jobs(clock):
    client = FakeClient()
    mr = mapreduce(client, clock, reduce_group_size=5, reduce_batch_size=2)
    result = mr.reduce([f"s{i}" for i in range(25)])
    # level 1: 5 groups of 5 texts in jobs of 2, 2, 1 groups; level 2: 1 group
    assert [len(job.lines) for job in client.jobs.values()] == [2, 2, 1, 1]
    first_group = client.jobs["batch-1"].lines[0]["body"]["messages"][-1]["content"]
    assert first_group == "Combine: s0\n\ns1\n\ns2\n\ns3\n\ns4"
    assert [len(level) for level in result.levels] == [25, 5, 1]


def test_map_batch_size_sets_records_per_job(clock):
    client = FakeClient()
    df = pd.DataFrame({"review": [f"r{i}" for i in range(10)]})
    mapreduce(client, clock, map_batch_size=4).map(df, "review", "summary")
    assert [len(job.lines) for job in client.jobs.values()] == [4, 4, 2]


def test_batch_only_leaves_missing_lines_empty_with_a_warning(clock, caplog):
    client = FakeClient(batch_responder=lambda m: None if content_of(m) == "Summarize: b" else echo(m))
    out = mapreduce(client, clock, strategies=("batch",)).map_texts(["a", "b", "c"])
    assert out == ["<Summarize: a>", None, "<Summarize: c>"]
    assert "1 of 3 records failed in the map: batch job batch-1 ended completed" in caplog.text
    assert not client.calls["sync"] and not client.calls["async"]
