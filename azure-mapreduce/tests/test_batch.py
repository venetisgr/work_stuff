"""BatchRunner against the fake Batch service: chunking, custom_id mapping, polling, timeouts, cleanup, fallback."""

from __future__ import annotations

import contextlib
import dataclasses
import itertools
import json
import logging
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace

import httpx2
import openai
import pandas as pd
import pytest

from azure_mapreduce import AzureChatClient, MapReduce, runners
from azure_mapreduce import client as client_module
from azure_mapreduce.client import BATCH_ENDPOINT, BATCH_FILE_EXPIRY_SECONDS, QUOTA_CODE, BatchJob
from azure_mapreduce.errors import ConfigError, LLMRequestError, LLMSetupError
from azure_mapreduce.progress import StepProgress
from azure_mapreduce.runners import (
    _CANCEL_GRACE_SECONDS,
    _DOWNLOAD_ATTEMPTS,
    _FILE_READY_TIMEOUT_SECONDS,
    _POLL_GIVE_UP_SECONDS,
    _QUOTA_RETRIES,
    _SUBMIT_RETRIES,
    AsyncRunner,
    BatchRunner,
    FallbackExecutor,
    LLMRequest,
    SyncRunner,
    _Chunk,
    build_executor,
)

from .conftest import FakeClient, FakeJob, chat_body, content_of, echo

TERMINAL = ("completed", "failed", "expired", "cancelled")


# --- helpers ---------------------------------------------------------------------------------------------


class RecordingProgress:
    """Stands in for StepProgress and remembers every call."""

    def __init__(self):
        self.done = 0
        self.peak = 0  # the most ``done`` ever reached
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
            self.peak = max(self.peak, self.done)

    def fail(self, count: int = 1) -> None:
        self.failed += count
        self.done += count
        self.peak = max(self.peak, self.done)

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


@dataclass
class QuotaClient(ScriptedClient):
    """ScriptedClient whose enqueued-token quota fits ``quota_jobs`` running jobs: a job created while that many
    are running fails validation with token_limit_exceeded (set ``batch_error_codes`` to report the code)."""

    quota_jobs: int = 1

    def create_batch(self, input_file_id):
        over = len(running_jobs(self)) >= self.quota_jobs
        view = super().create_batch(input_file_id)
        if over:
            self.jobs[view.id].statuses = ["validating", "failed"]
        return view


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


def failing_calls(client, method: str, when: Callable[..., bool], make_error: Callable[[], BaseException]) -> list:
    """Record the calls of ``client.<method>``; a call raises ``make_error()`` when ``when(n, *args)`` (n 1-based)."""
    original = getattr(client, method)
    calls = []

    def wrapper(*args):
        calls.append(args)
        if when(len(calls), *args):
            raise make_error()
        return original(*args)

    setattr(client, method, wrapper)
    return calls


def quota_error() -> LLMRequestError:
    """What AzureChatClient raises when the Batch API's enqueued-token quota is full."""
    return LLMRequestError(
        "The Batch API's enqueued-token quota is full: Enqueued token limit reached", retryable=True, code=QUOTA_CODE
    )


def passing_error() -> LLMRequestError:
    """What AzureChatClient raises for a timeout (or a 5xx) while submitting a job."""
    return LLMRequestError("The request timed out.", retryable=True, code="timeout")


def running_jobs(client: ScriptedClient) -> set[str]:
    """Jobs the ScriptedClient created and hasn't yet reported over."""
    started = {batch_id for kind, batch_id, _ in client.events if kind == "start"}
    ended = {batch_id for kind, batch_id, _ in client.events if kind == "end"}
    return started - ended


def input_file_of(client, batch_id: str) -> str:
    return client.jobs[batch_id].input_file_id


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
    ``create_errors`` and ``content_errors`` are raised by successive batches.create and files.content calls
    (None lets that call through). ``uploads`` and ``batch_creates`` record every keyword argument sent.
    """

    def __init__(self, fail_errors: list | None = None, *, create_errors=(), content_errors=()):
        self.fail_errors = fail_errors
        self.uploads: list[dict] = []
        self.batch_creates: list[dict] = []
        self.deleted: list[str] = []
        self.contents: dict[str, str] = {}
        self._inputs: dict[str, str] = {}
        self._outputs: dict[str, str] = {}
        self._polls: Counter = Counter()
        self._create_errors = list(create_errors)
        self._content_errors = list(content_errors)
        self.files = SimpleNamespace(
            create=self._upload, retrieve=self._file, content=self._content, delete=self.deleted.append
        )
        self.batches = SimpleNamespace(create=self._create, retrieve=self._retrieve, cancel=self._cancel)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._chat))

    def _chat(self, **kwargs):
        raise AssertionError("these tests only use the Batch API")

    def _upload(self, *, file, purpose, **options):
        name, content, mime = file
        file_id = f"file-{len(self.contents) + 1}"
        self.contents[file_id] = content.decode("utf-8")
        self.uploads.append({"id": file_id, "name": name, "mime": mime, "purpose": purpose, **options})
        return SimpleNamespace(id=file_id, status="uploaded")

    def _file(self, file_id):
        return SimpleNamespace(id=file_id, status="processed", status_details=None)

    def _content(self, file_id):
        error = self._content_errors.pop(0) if self._content_errors else None
        if error is not None:
            raise error
        return SimpleNamespace(text=self.contents[file_id])

    def _create(self, *, input_file_id, endpoint, completion_window, **options):
        error = self._create_errors.pop(0) if self._create_errors else None
        if error is not None:
            raise error
        batch_id = f"batch_{len(self.batch_creates) + 1}"
        self.batch_creates.append(
            {"input_file_id": input_file_id, "endpoint": endpoint, "completion_window": completion_window, **options}
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


def test_file_size_limit_counts_the_escaped_text(monkeypatch, clock):
    """The real client escapes non-ASCII text (é is written \\u00e9), so a line takes the size of its escaped form."""
    sdk = StubOpenAI()
    client = AzureChatClient(client=sdk, deployment="gpt-std", batch_deployment="gpt-batch-dep")
    requests = make_requests(["é" * 100, "ü" * 100, "ø" * 100])  # same lengths; custom ids too
    line = client.batch_line("request-0", requests[0].messages) + "\n"
    size = len(line.encode("utf-8"))
    assert line.isascii() and size == len(line)
    utf8_size = len(line.replace("\\u00e9", "é").encode("utf-8"))  # what the line would take as raw UTF-8
    assert size - utf8_size == 100 * (6 - 2)
    limit = max(size, 2 * utf8_size)  # one escaped line fits; two would as raw UTF-8, but not escaped
    assert limit < 2 * size
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
    assert BATCH_ENDPOINT == "/v1/chat/completions"
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
            "url": "/v1/chat/completions",
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
    assert raw.isascii() and "caf\\u00e9 \\u2615\\nline two" in raw  # non-ASCII written as \u escapes
    assert json.loads(raw) == {
        "custom_id": "request-7",
        "method": "POST",
        "url": "/v1/chat/completions",
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

    expiry = {"anchor": "created_at", "seconds": 1209600}  # 14 days
    assert sdk.uploads == [
        {
            "id": "file-1",
            "name": "requests.jsonl",
            "mime": "application/jsonl",
            "purpose": "batch",
            "expires_after": expiry,
        }
    ]
    assert sdk.batch_creates == [
        {
            "input_file_id": "file-1",
            "endpoint": "/v1/chat/completions",
            "completion_window": "24h",
            "output_expires_after": expiry,
        }
    ]
    lines = [json.loads(raw) for raw in sdk.contents["file-1"].splitlines()]
    assert [line["custom_id"] for line in lines] == ["request-0", "request-1"]
    assert all(line["url"] == "/v1/chat/completions" for line in lines)
    assert all(line["body"]["model"] == "gpt-batch-dep" and line["body"]["temperature"] == 0 for line in lines)
    assert results == {0: "gpt-batch-dep: café ☕", 1: "gpt-batch-dep: b"}
    assert failures == {}
    assert progress.advances == [1, 1]  # one from request_counts while in progress, one at completion
    assert sdk.deleted == ["file-1", "file-2"]  # input and output


def test_real_client_failed_job_reports_azures_errors(clock):
    errors = [
        SimpleNamespace(code="internal_error", message="Invalid JSON", line=3),
        SimpleNamespace(code=None, message="Model not supported for batch", line=None),
    ]
    sdk = StubOpenAI(fail_errors=errors)
    client = AzureChatClient(client=sdk, deployment="gpt-std", batch_deployment="gpt-batch-dep")
    results, failures, progress = run_batch(batch_runner(client, clock), make_requests(texts(2)))
    assert results == {}
    assert sorted(failures) == [0, 1]
    message = str(failures[0])
    assert "batch job batch_1 ended failed" in message
    assert "internal_error: Invalid JSON (line 3)" in message
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


def test_progress_estimate_taken_back_when_the_batch_run_breaks(clock):
    client = ScriptedClient(batch_statuses=("in_progress",), counts=(3,))
    batch = batch_runner(client, clock, sleep=sleep_raising(clock, 2, RuntimeError("poll loop broke")))
    executor = FallbackExecutor([batch, SyncRunner(client)])
    progress = RecordingProgress()
    results, failures = executor.run(make_requests(texts(4)), progress, batch_size=100)
    assert results == echoed(0, 1, 2, 3) and failures == {}
    assert progress.done == 4  # 3 estimated by the batch job that never delivered, + 4 from sync
    assert progress.peak == 4


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
    reads = failing_calls(
        client, "read_file", lambda n, file_id: file_id == "file-3", lambda: ConnectionError("download reset")
    )
    results, failures, _ = run_batch(batch_runner(client, clock), make_requests(texts(4)), batch_size=2)
    assert client.jobs["batch-1"].output_file_id == "file-3"
    assert [args for args in reads if args == ("file-3",)] == [("file-3",)] * _DOWNLOAD_ATTEMPTS
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
    results, failures, _ = run_batch(batch_runner(client, clock, poll_interval=60.0), make_requests(texts(3)))
    assert _POLL_GIVE_UP_SECONDS == 1800.0
    assert clock.now == 60.0 + _POLL_GIVE_UP_SECONDS  # 30 minutes after the first failed check
    assert len(clock.sleeps) == 31
    assert client.cancelled == ["batch-1"]  # don't leave it running (and billing) unwatched
    assert results == {}
    assert sorted(failures) == [0, 1, 2] and all(error.retryable for error in failures.values())
    assert "Giving up on batch job batch-1" in caplog.text
    assert client.jobs["batch-1"].input_file_id not in client.deleted  # it may still be reading it


def test_poll_errors_that_clear_up_dont_give_up(clock):
    # 29 minutes of failed checks, one that works, 29 more minutes of failures: never 30 minutes in a row.
    pattern = [True] * 29 + [False] + [True] * 29

    def poll_fails(batch_id, attempt):
        return attempt <= len(pattern) and pattern[attempt - 1]

    client = ScriptedClient(poll_fails=poll_fails)
    results, failures, _ = run_batch(batch_runner(client, clock, poll_interval=60.0), make_requests(texts(3)))
    assert client.poll_attempts["batch-1"] > len(pattern)
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


# --- defaults, chunks --------------------------------------------------------------------------------------


def test_batch_runner_defaults():
    runner = BatchRunner(FakeClient())
    assert runner.poll_interval == 60.0
    assert runner.timeout == 24 * 3600.0
    assert runner.cancel_wait == _CANCEL_GRACE_SECONDS == 600.0
    assert runner.max_concurrent_jobs == 4 and runner.cleanup is True
    assert (_POLL_GIVE_UP_SECONDS, _DOWNLOAD_ATTEMPTS, _QUOTA_RETRIES) == (1800.0, 3, 6)
    assert not hasattr(runners, "_MAX_POLL_ERRORS")  # jobs are given up on after a time, not a number of checks


def test_build_executor_passes_the_batch_settings_on(clock):
    (default,) = build_executor(FakeClient(), ("batch",)).runners
    assert (default.poll_interval, default.timeout, default.cancel_wait) == (60.0, 24 * 3600.0, 600.0)
    (runner,) = build_executor(
        FakeClient(),
        ("batch",),
        batch_poll_interval=5.0,
        batch_timeout=None,
        batch_cancel_wait=0.0,
        batch_cleanup=False,
        sleep=clock.sleep,
        clock=clock,
    ).runners
    assert (runner.poll_interval, runner.timeout, runner.cancel_wait, runner.cleanup) == (5.0, None, 0.0, False)


def test_chunks_are_chunk_objects_with_their_input_file(clock):
    client = FakeClient()
    requests = make_requests(texts(5))
    chunks = batch_runner(client, clock)._chunks(requests, 2)
    assert all(isinstance(chunk, _Chunk) for chunk in chunks)
    assert [chunk.requests for chunk in chunks] == [requests[0:2], requests[2:4], requests[4:]]
    for chunk in chunks:
        lines = [client.batch_line(f"request-{request.key}", request.messages) + "\n" for request in chunk.requests]
        assert chunk.payload == "".join(lines).encode("utf-8")
        assert chunk.submit_retries == 0 and chunk.ready_at == 0.0 and chunk.waiting_for == ""


@pytest.mark.parametrize("batch_size", [0, -3])
def test_batch_size_below_one_is_a_config_error(clock, batch_size):
    client = FakeClient()
    with pytest.raises(ConfigError, match="at least 1"):
        run_batch(batch_runner(client, clock), make_requests(texts(2)), batch_size=batch_size)
    with pytest.raises(ConfigError, match="at least 1"):
        batch_runner(client, clock)._chunks([], batch_size)
    assert not client.files and not client.jobs


# --- the real client: endpoint, file expiry, job error codes -----------------------------------------------


def bad_request(code: str, message: str) -> openai.BadRequestError:
    request = httpx2.Request("POST", "https://res.openai.azure.com/openai/v1/batches")
    return openai.BadRequestError(
        f"Error code: 400 - {message}",
        response=httpx2.Response(400, request=request),
        body={"code": code, "message": message},
    )


def test_batch_endpoint_can_be_overridden(clock):
    sdk = StubOpenAI()
    client = AzureChatClient(
        client=sdk, deployment="gpt-std", batch_deployment="gpt-batch-dep", batch_endpoint="/chat/completions"
    )
    results, failures, _ = run_batch(batch_runner(client, clock), make_requests(texts(2)))
    assert results == {0: "gpt-batch-dep: t0", 1: "gpt-batch-dep: t1"} and failures == {}
    assert [json.loads(raw)["url"] for raw in sdk.contents["file-1"].splitlines()] == ["/chat/completions"] * 2
    assert [create["endpoint"] for create in sdk.batch_creates] == ["/chat/completions"]


@pytest.mark.parametrize("expiry", [None, 20 * 24 * 3600, 30 * 24 * 3600])
def test_batch_file_expiry_setting(clock, expiry):
    sdk = StubOpenAI()
    client = AzureChatClient(
        client=sdk, deployment="gpt-std", batch_deployment="gpt-batch-dep", batch_file_expiry=expiry
    )
    results, _, _ = run_batch(batch_runner(client, clock), make_requests(texts(2)))
    assert len(results) == 2
    (upload,), (create,) = sdk.uploads, sdk.batch_creates
    if expiry is None:  # kept until deleted: neither option is sent
        assert "expires_after" not in upload and "output_expires_after" not in create
    else:
        assert upload["expires_after"] == create["output_expires_after"] == {"anchor": "created_at", "seconds": expiry}
    assert BATCH_FILE_EXPIRY_SECONDS == 1209600  # the default: 14 days


def test_batch_file_expiry_shorter_than_azure_accepts_is_refused_before_any_upload():
    sdk = StubOpenAI()
    with pytest.raises(ConfigError, match="batch_file_expiry"):
        AzureChatClient(client=sdk, deployment="gpt-std", batch_deployment="gpt-batch-dep", batch_file_expiry=259200)
    assert sdk.uploads == []


def test_batch_job_error_codes_come_from_the_errors_data():
    errors = [
        SimpleNamespace(code="model_not_found", message="No such model", line=None),
        SimpleNamespace(code=None, message="Something else", line=2),
        SimpleNamespace(code="invalid_json_line", message="Bad line", line=7),
    ]
    job = BatchJob.from_sdk(SimpleNamespace(id="batch_1", status="failed", errors=SimpleNamespace(data=errors)))
    assert job.error_codes == ("model_not_found", "invalid_json_line")  # errors without a code have none to give
    assert job.errors == (
        "model_not_found: No such model",
        "Something else (line 2)",
        "invalid_json_line: Bad line (line 7)",
    )
    for missing in (None, SimpleNamespace(data=None), SimpleNamespace(data=[])):
        empty = BatchJob.from_sdk(SimpleNamespace(id="batch_2", status="completed", errors=missing))
        assert empty.error_codes == () and empty.errors == ()
    assert BatchJob(id="batch_3", status="validating").error_codes == ()


def test_real_client_quota_error_on_create_backs_off_and_resubmits(clock):
    sdk = StubOpenAI(create_errors=[bad_request(QUOTA_CODE, "Enqueued token limit reached")])
    client = AzureChatClient(client=sdk, deployment="gpt-std", batch_deployment="gpt-batch-dep")
    results, failures, _ = run_batch(batch_runner(client, clock), make_requests(texts(2)))
    assert results == {0: "gpt-batch-dep: t0", 1: "gpt-batch-dep: t1"} and failures == {}
    assert clock.sleeps == [60.0, 1.0, 1.0]  # backed off a minute, then the job's two polls
    assert [create["input_file_id"] for create in sdk.batch_creates] == ["file-2"]  # a fresh upload
    assert sdk.deleted == ["file-1", "file-2", "file-3"]  # the refused upload, then the job's input and output


def test_real_client_job_failing_validation_for_a_setup_reason_raises(clock):
    errors = [SimpleNamespace(code="model_not_found", message="The model 'gpt-batch-dep' does not exist", line=None)]
    sdk = StubOpenAI(fail_errors=errors)
    client = AzureChatClient(client=sdk, deployment="gpt-std", batch_deployment="gpt-batch-dep")
    message = "Batch job batch_1 failed validation, and every job would: model_not_found: The model 'gpt-batch-dep'"
    with pytest.raises(LLMSetupError, match=re.escape(message)):
        run_batch(batch_runner(client, clock), make_requests(texts(2)))
    assert sdk.deleted == ["file-1"]


def test_real_client_download_is_retried(clock):
    request = httpx2.Request("GET", "https://res.openai.azure.com/openai/v1/files/file-2/content")
    sdk = StubOpenAI(content_errors=[openai.APIConnectionError(request=request)])
    client = AzureChatClient(client=sdk, deployment="gpt-std", batch_deployment="gpt-batch-dep")
    results, failures, _ = run_batch(batch_runner(client, clock), make_requests(texts(2)))
    assert results == {0: "gpt-batch-dep: t0", 1: "gpt-batch-dep: t1"} and failures == {}
    assert clock.sleeps == [1.0, 1.0, 5.0]
    assert sdk.deleted == ["file-1", "file-2"]


# --- giving up on a job that can't be checked --------------------------------------------------------------


def test_a_twenty_minute_outage_is_ridden_out(clock, caplog):
    client = ScriptedClient(poll_fails=lambda batch_id, attempt: 2 <= attempt <= 21)  # t=120s to t=1260s
    results, failures, progress = run_batch(batch_runner(client, clock, poll_interval=60.0), make_requests(texts(3)))
    assert results == echoed(0, 1, 2) and failures == {}
    assert client.cancelled == []
    assert client.poll_attempts["batch-1"] == 1 + 20 + 3  # validating, the outage, in_progress/finalizing/completed
    assert caplog.text.count("Couldn't check batch job batch-1 (couldn't reach Azure to check batch-1).") == 20
    assert "Giving up" not in caplog.text
    assert input_file_of(client, "batch-1") in client.deleted
    assert progress.done == 3


def test_gives_up_after_thirty_minutes_without_news(clock, caplog):
    client = ScriptedClient(batch_statuses=("in_progress",), poll_fails=lambda batch_id, attempt: attempt >= 2)
    results, failures, progress = run_batch(batch_runner(client, clock, poll_interval=60.0), make_requests(texts(3)))
    assert clock.now == 120.0 + _POLL_GIVE_UP_SECONDS  # 30 minutes after the first failed check
    assert client.poll_attempts["batch-1"] == 32
    assert client.cancelled == ["batch-1"]  # asked to stop, as far as that's possible
    assert results == {} and sorted(failures) == [0, 1, 2]
    for error in failures.values():
        assert error.retryable and error.code == "batch"
        assert "batch job batch-1 was given up on while" in str(error)
    assert caplog.text.count("Couldn't check batch job batch-1") == 31
    assert (
        "Giving up on batch job batch-1 after 30 minutes without news. It may still be running: check it in the "
        "Azure portal and cancel it there if so."
    ) in caplog.text
    assert (
        "Batch job batch-1 was given up on while" in caplog.text and "without 3 replies; they fall back." in caplog.text
    )
    assert client.deleted == []  # the input file stays: the job may still be reading it
    assert progress.done == 0


@pytest.mark.parametrize(("poll_interval", "checks", "minutes"), [(700.0, 4, 35), (3600.0, 2, 60)])
def test_giving_up_is_timed_from_the_first_failed_check(clock, caplog, poll_interval, checks, minutes):
    """It's the time without news that counts, not the number of failed checks."""
    client = ScriptedClient(poll_fails=lambda batch_id, attempt: True)
    results, failures, _ = run_batch(batch_runner(client, clock, poll_interval=poll_interval), make_requests(texts(2)))
    assert client.poll_attempts["batch-1"] == checks
    assert clock.now == checks * poll_interval
    assert f"Giving up on batch job batch-1 after {minutes} minutes without news." in caplog.text
    assert results == {} and sorted(failures) == [0, 1]


def test_giving_up_when_the_cancel_fails_too(clock, caplog):
    client = ScriptedClient(batch_statuses=("in_progress",), poll_fails=lambda batch_id, attempt: attempt >= 2)
    cancels = failing_calls(client, "cancel_batch", lambda n, batch_id: True, lambda: ConnectionError("no route"))
    results, failures, _ = run_batch(batch_runner(client, clock, poll_interval=60.0), make_requests(texts(2)))
    assert cancels == [("batch-1",)] and client.cancelled == []
    assert "Couldn't cancel batch job batch-1 (no route); cancel it in the Azure portal." in caplog.text
    assert "Batch job batch-1 was given up on while in_progress without 2 replies; they fall back." in caplog.text
    assert all("batch job batch-1 was given up on while in_progress" in str(error) for error in failures.values())
    assert results == {} and client.deleted == []


def test_job_given_up_on_falls_back_and_batch_stays_usable(clock):
    client = ScriptedClient(
        poll_fails=lambda batch_id, attempt: batch_id == "batch-1", sync_responder=lambda m: f"sync {content_of(m)}"
    )
    executor = FallbackExecutor([batch_runner(client, clock, poll_interval=60.0), SyncRunner(client)])
    results, failures = executor.run(make_requests(texts(4)), RecordingProgress(), batch_size=2)
    assert results == {0: "sync t0", 1: "sync t1", 2: "<t2>", 3: "<t3>"} and failures == {}
    assert executor.broken == set()  # one job going quiet doesn't mean the Batch API is broken
    assert input_file_of(client, "batch-1") not in client.deleted
    assert input_file_of(client, "batch-2") in client.deleted


# --- downloading results -----------------------------------------------------------------------------------


def test_download_is_retried_and_succeeds_on_the_second_attempt(clock, caplog):
    client = FakeClient()
    reads = failing_calls(client, "read_file", lambda n, file_id: n == 1, lambda: ConnectionError("download reset"))
    results, failures, progress = run_batch(batch_runner(client, clock), make_requests(texts(3)))
    assert results == echoed(0, 1, 2) and failures == {}
    assert reads == [("file-2",), ("file-2",)]
    assert clock.sleeps == [1.0] * 4 + [5.0]
    assert "Couldn't download file file-2 of batch job batch-1 (attempt 1 of 3: download reset)." in caplog.text
    assert "Kept file" not in caplog.text
    assert sorted(client.deleted) == ["file-1", "file-2"]
    assert progress.done == 3


def test_file_that_cant_be_downloaded_is_kept_and_the_readable_one_deleted(clock, caplog):
    def responder(messages):
        if content_of(messages) == "t1":
            raise LLMRequestError("blocked", retryable=False, code="content_filter")
        return echo(messages)

    client = FakeClient(batch_responder=responder)
    reads = failing_calls(
        client, "read_file", lambda n, file_id: file_id == "file-2", lambda: ConnectionError("download reset")
    )
    results, failures, progress = run_batch(batch_runner(client, clock), make_requests(texts(3)))
    job = client.jobs["batch-1"]
    assert (job.output_file_id, job.error_file_id) == ("file-2", "file-3")
    assert reads == [("file-2",)] * 3 + [("file-3",)]
    assert clock.sleeps == [1.0] * 4 + [5.0, 10.0]
    assert results == {}
    assert not failures[1].retryable  # from the error file, which could be read
    assert failures[0].retryable and failures[2].retryable and "ended completed" in str(failures[0])
    assert sorted(client.deleted) == ["file-1", "file-3"]  # the output file stays on Azure
    for attempt in (1, 2, 3):
        assert f"Couldn't download file file-2 of batch job batch-1 (attempt {attempt} of 3: download reset)." in (
            caplog.text
        )
    assert (
        "Kept file(s) file-2 of batch job batch-1 on Azure because they couldn't be downloaded; the replies in them "
        "are recoverable from the Azure portal."
    ) in caplog.text
    assert progress.done == 0  # the 2 replies Azure counted are taken back


def test_both_files_that_cant_be_downloaded_are_kept(clock, caplog):
    def responder(messages):
        if content_of(messages) == "t1":
            raise LLMRequestError("blocked", retryable=False, code="content_filter")
        return echo(messages)

    client = FakeClient(batch_responder=responder)
    failing_calls(client, "read_file", lambda n, file_id: True, lambda: TimeoutError("read timed out"))
    results, failures, _ = run_batch(batch_runner(client, clock), make_requests(texts(3)))
    assert results == {} and sorted(failures) == [0, 1, 2]
    assert all(error.retryable for error in failures.values())  # the error file couldn't be read either
    assert clock.sleeps == [1.0] * 4 + [5.0, 10.0, 5.0, 10.0]
    assert client.deleted == ["file-1"]
    assert "Kept file(s) file-2, file-3 of batch job batch-1 on Azure" in caplog.text


# --- the enqueued-token quota ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "make_error",
    [quota_error, lambda: RuntimeError("Error code: 400 - token_limit_exceeded: Enqueued token limit reached")],
    ids=["translated", "in-the-message"],
)
def test_quota_error_on_submit_backs_off_then_submits(clock, caplog, make_error):
    caplog.set_level(logging.INFO)
    client = FakeClient()
    creates = failing_calls(client, "create_batch", lambda n, file_id: n <= 3, make_error)
    results, failures, progress = run_batch(batch_runner(client, clock), make_requests(texts(3)))
    assert results == echoed(0, 1, 2) and failures == {}
    assert clock.sleeps == [60.0, 120.0, 240.0] + [1.0] * 4
    assert len(creates) == 4 and list(client.jobs) == ["batch-1"]
    assert input_file_of(client, "batch-1") == "file-4"
    assert {"file-1", "file-2", "file-3"} <= set(client.deleted)  # the refused uploads aren't left behind
    assert "trying again in 60s" in caplog.text and "trying again in 240s" in caplog.text
    assert "jobs 0/1 done · waiting for token quota" in progress.notes
    assert progress.notes[-1] == "jobs 1/1 done"


def test_quota_that_stays_full_hands_the_chunk_back(clock):
    client = FakeClient()
    creates = failing_calls(client, "create_batch", lambda n, file_id: True, quota_error)
    results, failures, _ = run_batch(batch_runner(client, clock), make_requests(texts(3)))
    assert clock.sleeps == [60.0, 120.0, 240.0, 480.0, 900.0, 900.0]
    assert len(creates) == 1 + _QUOTA_RETRIES
    assert results == {} and sorted(failures) == [0, 1, 2]
    for error in failures.values():
        assert error.retryable and error.code == "batch"
        assert str(error).startswith("the Batch API's enqueued-token quota stayed full: The Batch API's")
    assert sorted(client.deleted) == sorted(client.files)  # all 7 uploads


def test_quota_that_stays_full_falls_back_only_after_six_retries(clock):
    answered_at = []

    def async_responder(messages):
        answered_at.append(clock.now)
        return f"async {content_of(messages)}"

    client = FakeClient(async_responder=async_responder)
    failing_calls(client, "create_batch", lambda n, file_id: True, quota_error)
    executor = FallbackExecutor([batch_runner(client, clock), AsyncRunner(client, max_concurrency=2)])
    progress = RecordingProgress()
    results, failures = executor.run(make_requests(texts(3)), progress, batch_size=100)
    assert answered_at == [60.0 + 120 + 240 + 480 + 900 + 900] * 3  # nothing went to async before that
    assert results == {key: f"async t{key}" for key in range(3)} and failures == {}
    assert executor.broken == set()  # a full quota isn't a broken setup, even on the first submission
    assert progress.strategies == ["batch", "async"]
    assert progress.done == progress.peak == 3


def test_quota_error_while_our_jobs_run_waits_for_one_to_finish(clock, caplog):
    """The quota fits one job: each later chunk is refused once, then goes in as soon as the running job ends."""
    caplog.set_level(logging.INFO)
    client = ScriptedClient(batch_statuses=("in_progress", "completed"), now=clock)
    creates = failing_calls(client, "create_batch", lambda n, file_id: bool(running_jobs(client)), quota_error)
    results, failures, progress = run_batch(
        batch_runner(client, clock, max_concurrent_jobs=4), make_requests(texts(4)), batch_size=1
    )
    assert results == echoed(0, 1, 2, 3) and failures == {}
    starts = {batch_id: time for kind, batch_id, time in client.events if kind == "start"}
    assert starts == {"batch-1": 0.0, "batch-2": 2.0, "batch-3": 4.0, "batch-4": 6.0}
    assert clock.sleeps == [1.0] * 8  # no backing off: the chunks waited for our own jobs
    assert len(creates) == 7
    assert "waiting for a running job to finish" in caplog.text
    assert "jobs 0/4 done · 1 validating · waiting for token quota" in progress.notes


def test_waiting_behind_our_own_jobs_uses_no_quota_retries(clock):
    """Refused more than _QUOTA_RETRIES times, but always while our own jobs held the quota: never given up."""
    statuses = [("in_progress",) * (k - 1) + ("completed",) for k in range(1, 9)] + [("completed",)]
    client = ScriptedClient(job_statuses=statuses, now=clock)
    refusals = []

    def refused(n, file_id):
        if '"request-8"' in client.files[file_id] and len(refusals) < 8:
            refusals.append(clock.now)
            return True
        return False

    failing_calls(client, "create_batch", refused, quota_error)
    results, failures, _ = run_batch(
        batch_runner(client, clock, max_concurrent_jobs=9), make_requests(texts(9)), batch_size=1
    )
    assert refusals == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]  # one after each of jobs 1-7 ended
    assert len(refusals) > _QUOTA_RETRIES
    assert results == echoed(*range(9)) and failures == {}
    assert clock.sleeps == [1.0] * 9


def test_job_that_fails_for_quota_is_resubmitted_after_a_backoff(clock):
    client = ScriptedClient(
        job_statuses=[("validating", "failed"), ("validating", "in_progress", "completed")],
        batch_errors=("token_limit_exceeded: Enqueued token limit reached",),
        batch_error_codes=(QUOTA_CODE,),
        counts=(1, 1, 2),
        now=clock,
    )
    results, failures, progress = run_batch(batch_runner(client, clock), make_requests(texts(2)))
    assert results == echoed(0, 1) and failures == {}
    assert clock.sleeps == [1.0, 1.0, 60.0, 1.0, 1.0, 1.0]
    assert [(kind, batch_id) for kind, batch_id, _ in client.events][:3] == [
        ("start", "batch-1"),
        ("end", "batch-1"),
        ("start", "batch-2"),
    ]
    assert client.events[2][2] == 62.0
    assert input_file_of(client, "batch-1") in client.deleted
    assert progress.advances == [1, -1, 1, 1]  # the failed job's count is taken back
    assert progress.done == 2


def test_job_that_fails_for_quota_while_others_run_waits_for_one_to_finish(clock):
    client = QuotaClient(
        batch_statuses=("in_progress",) * 5 + ("completed",),
        batch_errors=("token_limit_exceeded: Enqueued token limit reached",),
        batch_error_codes=(QUOTA_CODE,),
        now=clock,
    )
    results, failures, _ = run_batch(
        batch_runner(client, clock, max_concurrent_jobs=2), make_requests(texts(4)), batch_size=2
    )
    assert results == echoed(0, 1, 2, 3) and failures == {}
    starts = {batch_id: time for kind, batch_id, time in client.events if kind == "start"}
    assert starts == {"batch-1": 0.0, "batch-2": 0.0, "batch-3": 6.0}  # resubmitted once batch-1 ended
    assert 60.0 not in clock.sleeps  # never backed off: our own job held the quota


def test_job_that_fails_for_quota_every_time_falls_back(clock):
    client = FakeClient(
        batch_statuses=("validating", "failed"),
        batch_errors=("token_limit_exceeded: Enqueued token limit reached",),
        batch_error_codes=(QUOTA_CODE,),
    )
    results, failures, _ = run_batch(batch_runner(client, clock), make_requests(texts(2)))
    backoffs = [sleep for sleep in clock.sleeps if sleep != 1.0]
    assert backoffs == [60.0, 120.0, 240.0, 480.0, 900.0, 900.0]
    assert len(client.jobs) == 1 + _QUOTA_RETRIES
    assert results == {} and sorted(failures) == [0, 1]
    for error in failures.values():
        assert error.retryable and error.code == "batch"
        assert "batch job batch-7 ended failed: token_limit_exceeded: Enqueued token limit reached" in str(error)
    assert set(client.deleted) == {job.input_file_id for job in client.jobs.values()}


def test_job_that_fails_for_quota_every_time_deletes_each_input_once(clock):
    client = FakeClient(
        batch_statuses=("validating", "failed"),
        batch_errors=("token_limit_exceeded: Enqueued token limit reached",),
        batch_error_codes=(QUOTA_CODE,),
    )
    run_batch(batch_runner(client, clock), make_requests(texts(2)))
    assert Counter(client.deleted) == Counter(job.input_file_id for job in client.jobs.values())


def test_interrupt_while_backing_off_from_the_quota(clock):
    client = FakeClient()
    failing_calls(client, "create_batch", lambda n, file_id: True, quota_error)
    runner = batch_runner(client, clock, sleep=sleep_raising(clock, 1, KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        run_batch(runner, make_requests(texts(2)))
    assert client.deleted == ["file-1"] and not client.jobs and client.cancelled == []


# --- jobs that fail validation -----------------------------------------------------------------------------

SETUP_CODES = [
    "model_not_found",
    "model_mismatch",
    "invalid_request",
    "url_mismatch",
    "invalid_json_line",
    "empty_file",
    "duplicate_custom_id",
    "too_many_tasks",
    "DeploymentNotFound",
    "OperationNotSupported",
    "unsupported_parameter",
    "unsupported_value",
]


def setup_failure_client(code: str = "model_not_found", **options) -> FakeClient:
    """A client whose batch jobs fail validation with ``code``."""
    options.setdefault("batch_statuses", ("validating", "failed"))
    return FakeClient(batch_errors=(f"{code}: it won't work",), batch_error_codes=(code,), **options)


@pytest.mark.parametrize("code", SETUP_CODES)
def test_job_failing_validation_for_a_setup_reason_raises(clock, code):
    client = setup_failure_client(code)
    with pytest.raises(LLMSetupError) as caught:
        run_batch(batch_runner(client, clock), make_requests(texts(2)))
    assert str(caught.value) == f"Batch job batch-1 failed validation, and every job would: {code}: it won't work"
    assert client.deleted == ["file-1"] and client.cancelled == []


def test_setup_failure_cancels_the_other_jobs_and_deletes_their_inputs(clock):
    client = ScriptedClient(
        job_statuses=[("validating", "failed"), ("in_progress",), ("in_progress",)],
        batch_errors=("model_not_found: gone",),
        batch_error_codes=("model_not_found",),
    )
    with pytest.raises(LLMSetupError, match="failed validation"):
        run_batch(batch_runner(client, clock), make_requests(texts(6)), batch_size=2)
    assert client.cancelled == ["batch-2", "batch-3"]
    assert sorted(client.deleted) == ["file-1", "file-2", "file-3"]


def test_setup_failure_keeps_the_replies_already_collected(clock):
    client = ScriptedClient(
        job_statuses=[("completed",), ("validating", "failed")],
        batch_errors=("url_mismatch: wrong url",),
        batch_error_codes=("url_mismatch",),
        sync_responder=lambda m: f"sync {content_of(m)}",
    )
    executor = FallbackExecutor([batch_runner(client, clock), SyncRunner(client)])
    results, failures = executor.run(make_requests(texts(4)), RecordingProgress(), batch_size=2)
    assert results == {0: "<t0>", 1: "<t1>", 2: "sync t2", 3: "sync t3"} and failures == {}
    assert executor.broken == {"batch"}


def test_setup_failure_disables_batch_for_the_rest_of_the_run(clock):
    client = setup_failure_client(sync_responder=lambda m: "sync")
    executor = FallbackExecutor([batch_runner(client, clock, max_concurrent_jobs=1), SyncRunner(client)])
    for _ in range(2):  # e.g. the map step, then a reduce level
        results, failures = executor.run(make_requests(texts(4)), RecordingProgress(), batch_size=2)
        assert results == dict.fromkeys(range(4), "sync") and failures == {}
    assert executor.broken == {"batch"}
    assert len(client.jobs) == 1  # the second chunk was never submitted, nor anything in the second run


def test_setup_failure_through_mapreduce_skips_batch_for_later_chunks_and_levels(clock, caplog):
    client = setup_failure_client(sync_responder=lambda m: f"sync {content_of(m)}")
    mr = mapreduce(
        client, clock, strategies=("batch", "sync"), map_batch_size=2, max_concurrent_batch_jobs=1, reduce_group_size=2
    )
    result = mr.run(pd.DataFrame({"review": [f"r{i}" for i in range(6)]}), "review", "summary")
    assert len(client.jobs) == 1  # the map's first job; not the other map chunks, nor any reduce level
    assert result.frame["summary"].tolist() == [f"sync Summarize: r{i}" for i in range(6)]
    assert [len(level) for level in result.levels] == [6, 3, 2, 1]
    assert len(client.calls["sync"]) == 6 + 3 + 2 + 1
    assert result.complete
    assert "The batch strategy failed (LLMSetupError: Batch job batch-1 failed validation" in caplog.text


def test_failed_job_with_another_code_falls_back_and_batch_stays_usable(clock):
    client = ScriptedClient(
        job_statuses=[("validating", "failed"), ("completed",)],
        batch_errors=("internal_error: Something went wrong",),
        batch_error_codes=("internal_error",),
        sync_responder=lambda m: f"sync {content_of(m)}",
    )
    executor = FallbackExecutor([batch_runner(client, clock), SyncRunner(client)])
    results, failures = executor.run(make_requests(texts(4)), RecordingProgress(), batch_size=2)
    assert results == {0: "sync t0", 1: "sync t1", 2: "<t2>", 3: "<t3>"} and failures == {}
    assert executor.broken == set()


def test_setup_failure_takes_back_the_jobs_progress_estimate(clock):
    client = ScriptedClient(
        batch_statuses=("in_progress", "failed"),
        counts=(2,),
        batch_errors=("model_not_found: gone",),
        batch_error_codes=("model_not_found",),
        sync_responder=lambda m: "sync",
    )
    executor = FallbackExecutor([batch_runner(client, clock), SyncRunner(client)])
    progress = RecordingProgress()
    results, failures = executor.run(make_requests(texts(4)), progress, batch_size=100)
    assert results == dict.fromkeys(range(4), "sync") and failures == {}
    assert progress.done == 4 and progress.peak == 4


# --- stopping part-way: jobs still running -----------------------------------------------------------------


@pytest.mark.parametrize("cleanup", [True, False])
def test_breaking_off_takes_back_estimates_cancels_and_deletes_inputs(clock, cleanup):
    client = ScriptedClient(batch_statuses=("in_progress",), counts=(1,))
    runner = batch_runner(client, clock, cleanup=cleanup, sleep=sleep_raising(clock, 3, RuntimeError("loop broke")))
    progress = RecordingProgress()
    with pytest.raises(RuntimeError, match="loop broke"):
        run_batch(runner, make_requests(texts(4)), batch_size=2, progress=progress)
    assert client.cancelled == ["batch-1", "batch-2"]
    assert sorted(client.deleted) == (["file-1", "file-2"] if cleanup else [])
    assert progress.advances == [1, 1, -1, -1]
    assert progress.done == 0


def test_keyboard_interrupt_deletes_the_inputs_of_running_jobs(clock):
    client = FakeClient()
    runner = batch_runner(client, clock, max_concurrent_jobs=2, sleep=sleep_raising(clock, 3, KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        run_batch(runner, make_requests(texts(6)), batch_size=2)
    assert client.cancelled == ["batch-1", "batch-2"]
    assert sorted(client.deleted) == ["file-1", "file-2"]
    assert len(client.files) == 2  # the third chunk was never uploaded


def test_fallback_after_breaking_off_leaves_the_bar_at_its_total(clock):
    client = ScriptedClient(batch_statuses=("in_progress",), counts=(2,), sync_responder=lambda m: "sync")
    batch = batch_runner(client, clock, sleep=sleep_raising(clock, 2, RuntimeError("loop broke")))
    executor = FallbackExecutor([batch, SyncRunner(client)])
    seen = []
    with StepProgress(4, "Map", unit="record", show=False) as progress:
        original = progress.advance

        def advance(count=1):
            original(count)
            seen.append(progress.done)

        progress.advance = advance
        results, failures = executor.run(make_requests(texts(4)), progress, batch_size=2)
    assert results == dict.fromkeys(range(4), "sync") and failures == {}
    assert progress.done == progress.total == 4
    assert max(seen) <= progress.total


# --- result lines of odd shapes ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_line",
    [
        "42",
        "true",
        '[{"custom_id": "request-1"}]',
        {"custom_id": "request-1", "response": {"status_code": 200, "body": [1, 2]}},
        {"custom_id": "request-1", "response": {"status_code": 200, "body": "[1, 2]"}},
        {"custom_id": "request-1", "response": {"status_code": 200, "body": {"choices": 5}}},
        {"custom_id": "request-1", "response": {"status_code": 200, "body": {"choices": [None]}}},
        {"custom_id": "request-1", "response": {"status_code": 200, "body": {"choices": [{"message": "hi"}]}}},
        {
            "custom_id": "request-1",
            "response": {"status_code": 200, "body": {"choices": [{"message": {"content": 5}}]}},
        },
        {"custom_id": "request-1", "response": {"status_code": 500, "body": {"error": ["server_error"]}}},
        {"custom_id": ["request-1"], "response": {"status_code": 200, "body": chat_body("a list for an id")}},
    ],
)
def test_odd_shaped_lines_dont_stop_the_job(clock, bad_line):
    def write(job):
        return [ok_line(0, "zero"), bad_line, ok_line(2, "two")], []

    client = ScriptedClient(write=write)
    results, failures, _ = run_batch(batch_runner(client, clock), make_requests(texts(3)))
    assert results == {0: "zero", 2: "two"}
    assert list(failures) == [1] and failures[1].retryable


def test_lines_that_arent_objects_are_logged_and_skipped(clock, caplog):
    def write(job):
        return ["[1, 2]", "null", ok_line(0, "zero")], []

    client = ScriptedClient(write=write)
    results, _, _ = run_batch(batch_runner(client, clock), make_requests(texts(1)))
    assert results == {0: "zero"}
    assert caplog.text.count("Skipping an unreadable line in batch job batch-1's results.") == 2


@pytest.mark.parametrize(
    "bad_line",
    [
        {"custom_id": "request-1", "error": {"code": ["server_error"], "message": "odd"}},
        {"custom_id": "request-1", "response": {"status_code": 400, "body": {"error": {"code": {"a": 1}}}}},
    ],
)
def test_error_codes_that_arent_strings_dont_stop_the_job(clock, bad_line):
    def write(job):
        return [ok_line(0, "zero"), bad_line, ok_line(2, "two")], []

    client = ScriptedClient(write=write)
    results, failures, _ = run_batch(batch_runner(client, clock), make_requests(texts(3)))
    assert results == {0: "zero", 2: "two"}
    assert list(failures) == [1] and failures[1].retryable


# --- the fall-back warning ---------------------------------------------------------------------------------


def test_the_fall_back_warning_is_logged_for_a_completed_job_too(clock, caplog):
    client = FakeClient(batch_responder=lambda m: None if content_of(m) in ("t1", "t3") else echo(m))
    run_batch(batch_runner(client, clock), make_requests(texts(4)))
    assert "Batch job batch-1 ended completed without 2 replies; they fall back." in caplog.text


@pytest.mark.parametrize("where", ["job", "submit"])
def test_the_fall_back_warning_keeps_azures_error_text(clock, caplog, where):
    if where == "job":
        client = FakeClient(
            batch_statuses=("validating", "failed"),
            batch_errors=("internal_error: The Service had an Internal Error",),
            batch_error_codes=("internal_error",),
        )
        run_batch(batch_runner(client, clock), make_requests(texts(2)))
        expected = "Batch job batch-1 ended failed: internal_error: The Service had an Internal Error without 2"
    else:
        client = FakeClient()
        error = LLMSetupError("Azure rejected the credentials (403: Forbidden). Check the API key")
        watch_calls(client, "create_batch", fail_on=2, error=error)
        run_batch(batch_runner(client, clock), make_requests(texts(4)), batch_size=2)
        expected = "Couldn't submit the batch job: Azure rejected the credentials (403: Forbidden). Check the API key"
    assert expected in caplog.text


# --- how long a cancelled job is waited for ----------------------------------------------------------------


@pytest.mark.parametrize(("cancel_wait", "given_up_at"), [(0.0, 120.0), (45.0, 150.0), (120.0, 240.0)])
def test_cancel_wait_is_how_long_a_cancelling_job_is_waited_for(clock, caplog, cancel_wait, given_up_at):
    client = ScriptedClient(batch_statuses=("in_progress",), stuck_cancelling=True)
    runner = batch_runner(client, clock, poll_interval=30.0, timeout=60.0, cancel_wait=cancel_wait)
    results, failures, _ = run_batch(runner, make_requests(texts(3)))
    assert client.cancelled == ["batch-1"]  # at t=90, the first check past the timeout
    assert clock.now == given_up_at
    assert results == {} and sorted(failures) == [0, 1, 2]
    assert all("batch job batch-1 was given up on while cancelling" in str(error) for error in failures.values())
    assert "Batch job batch-1 didn't finish cancelling; moving on without its replies." in caplog.text
    assert input_file_of(client, "batch-1") not in client.deleted  # it may still be reading it


def test_mapreduce_batch_cancel_wait(clock):
    client = ScriptedClient(
        batch_statuses=("in_progress",), stuck_cancelling=True, sync_responder=lambda m: f"sync {content_of(m)}"
    )
    mr = mapreduce(client, clock, strategies=("batch", "sync"), batch_timeout=5.0, batch_cancel_wait=0.0)
    assert mr.map_texts(["a", "b"]) == ["sync Summarize: a", "sync Summarize: b"]
    assert client.cancelled == ["batch-1"]
    assert clock.now == 7.0  # cancelled at t=6, given up at the next check


# --- ASCII-only batch lines: sizes are those of the escaped text -------------------------------------------


def real_client(sdk: StubOpenAI | None = None, **options) -> AzureChatClient:
    return AzureChatClient(
        client=sdk or StubOpenAI(), deployment="gpt-std", batch_deployment="gpt-batch-dep", **options
    )


def test_batch_lines_escape_everything_that_isnt_ascii():
    client = real_client()
    text = "é ☕ 😀 a b c\x85d \ud800 end"  # line separators, an astral character, a lone surrogate
    raw = client.batch_line("request-0", [{"role": "user", "content": text}])
    assert raw.isascii()
    assert "\\u00e9 \\u2615 \\ud83d\\ude00 a\\u2028b\\u2029c\\u0085d \\ud800 end" in raw
    assert raw.splitlines() == [raw]  # nothing any reader could take for a line break
    assert json.loads(raw)["body"]["messages"][0]["content"] == text


def test_chunk_sizes_are_the_sizes_of_the_escaped_lines(clock):
    client = real_client()
    requests = make_requests(["a" * 50, "é" * 50, "☕" * 50, "😀" * 50])  # custom ids of the same length
    (chunk,) = batch_runner(client, clock)._chunks(requests, 100)
    assert chunk.payload.isascii()
    lines = chunk.payload.split(b"\n")
    assert len(lines) == 5 and lines[-1] == b""
    # "a" is 1 byte; é and ☕ are 6 (\uXXXX), 😀 is 12 (a surrogate pair), whatever they'd take as UTF-8
    assert [len(line) - len(lines[0]) for line in lines[:4]] == [0, 50 * 5, 50 * 5, 50 * 11]
    assert len(chunk.payload) == sum(len(client.batch_line(f"request-{r.key}", r.messages)) + 1 for r in requests)


@pytest.mark.parametrize(("slack", "jobs"), [(0, 1), (-1, 2)])
def test_file_size_limit_boundary_is_the_escaped_size(monkeypatch, clock, slack, jobs):
    sdk = StubOpenAI()
    client = real_client(sdk)
    requests = make_requests(["é" * 100, "ü" * 100])
    size = sum(len(client.batch_line(f"request-{r.key}", r.messages)) + 1 for r in requests)
    monkeypatch.setattr(runners, "MAX_BATCH_FILE_BYTES", size + slack)  # exactly both lines, or a byte short
    results, failures, _ = run_batch(batch_runner(client, clock), requests)
    assert len(sdk.batch_creates) == jobs
    assert sum(len(sdk.contents[upload["id"]].encode("utf-8")) for upload in sdk.uploads) == size
    assert results == {0: "gpt-batch-dep: " + "é" * 100, 1: "gpt-batch-dep: " + "ü" * 100} and failures == {}


def test_text_with_line_separators_or_a_lone_surrogate_goes_through_a_batch_job(clock):
    """A raw U+2028 splits the line for readers using str.splitlines, and a lone surrogate (a cut emoji) can't be
    written as UTF-8 at all; escaped, both round-trip."""
    sdk = StubOpenAI()  # splits its input file with str.splitlines
    client = real_client(sdk)
    prompts = ["one two", "a b\x85c", "cut emoji \ud83d", "plain"]
    results, failures, _ = run_batch(batch_runner(client, clock), make_requests(prompts))
    assert failures == {}
    assert results == {key: f"gpt-batch-dep: {prompt}" for key, prompt in enumerate(prompts)}
    assert sdk.contents["file-1"].isascii() and len(sdk.contents["file-1"].splitlines()) == len(prompts)


# --- passing errors while submitting: 30, 60 and 120 seconds -----------------------------------------------

SUBMIT_STAGES = {"upload": "upload_batch_file", "file check": "file_status", "create": "create_batch"}


@pytest.mark.parametrize("stage", SUBMIT_STAGES)
def test_passing_submit_error_is_retried_after_30_then_60_seconds(clock, caplog, stage):
    client = FakeClient()
    calls = failing_calls(client, SUBMIT_STAGES[stage], lambda n, *args: n <= 2, passing_error)
    results, failures, progress = run_batch(batch_runner(client, clock), make_requests(texts(3)))
    assert results == echoed(0, 1, 2) and failures == {}
    assert clock.sleeps == [30.0, 60.0] + [1.0] * 4  # the backoffs, then the job's own polls
    assert len(calls) == 3 and list(client.jobs) == ["batch-1"]
    for delay in (30, 60):
        assert f"Couldn't submit a batch job (The request timed out.); trying again in {delay}s." in caplog.text
    assert Counter(client.deleted) == Counter(list(client.files))  # refused uploads, input, output: once each
    assert progress.notes[-1] == "jobs 1/1 done"


def test_three_passing_errors_in_a_row_are_ridden_out(clock):
    client = FakeClient()
    creates = failing_calls(client, "create_batch", lambda n, file_id: n <= _SUBMIT_RETRIES, passing_error)
    results, failures, _ = run_batch(batch_runner(client, clock), make_requests(texts(2)))
    assert results == echoed(0, 1) and failures == {}
    assert _SUBMIT_RETRIES == 3
    assert clock.sleeps == [30.0, 60.0, 120.0] + [1.0] * 4
    assert len(creates) == _SUBMIT_RETRIES + 1 and len(client.jobs) == 1


def test_first_chunk_out_of_submit_retries_raises(clock):
    client = FakeClient()
    creates = failing_calls(client, "create_batch", lambda n, file_id: True, passing_error)
    with pytest.raises(LLMRequestError, match="timed out") as caught:
        run_batch(batch_runner(client, clock), make_requests(texts(4)), batch_size=2)
    assert caught.value.code == "timeout"
    assert clock.sleeps == [30.0, 60.0, 120.0]
    assert len(creates) == 1 + _SUBMIT_RETRIES
    assert all('"request-0"' in client.files[file_id] for (file_id,) in creates)  # the first chunk every time
    assert not client.jobs
    assert Counter(client.deleted) == Counter(list(client.files))  # each refused upload deleted once


def test_first_chunk_out_of_submit_retries_hands_everything_on_and_skips_batch(clock):
    client = FakeClient(sync_responder=lambda m: f"sync {content_of(m)}")
    creates = failing_calls(client, "create_batch", lambda n, file_id: True, passing_error)
    executor = FallbackExecutor([batch_runner(client, clock), SyncRunner(client)])
    for _ in range(2):  # e.g. the map step, then a reduce level
        results, failures = executor.run(make_requests(texts(4)), RecordingProgress(), batch_size=2)
        assert results == {key: f"sync t{key}" for key in range(4)} and failures == {}
    assert executor.broken == {"batch"}
    assert len(creates) == 1 + _SUBMIT_RETRIES  # not tried again in the second step


def test_later_chunk_out_of_submit_retries_falls_back_alone(clock, caplog):
    client = ScriptedClient(now=clock)

    def second_chunk(n, file_id):
        return '"request-2"' in client.files[file_id]

    creates = failing_calls(client, "create_batch", second_chunk, passing_error)
    results, failures, progress = run_batch(batch_runner(client, clock), make_requests(texts(6)), batch_size=2)
    assert results == echoed(0, 1, 4, 5)
    assert sorted(failures) == [2, 3]
    for error in failures.values():
        assert error.retryable and error.code == "batch"
        assert str(error) == "couldn't submit the batch job: The request timed out."
    assert sum(second_chunk(0, file_id) for (file_id,) in creates) == 1 + _SUBMIT_RETRIES
    assert list(client.jobs) == ["batch-1", "batch-2"]
    starts = {batch_id: time for kind, batch_id, time in client.events if kind == "start"}
    assert starts == {"batch-1": 0.0, "batch-2": 30.0 + 60.0 + 120.0}  # the third chunk waited behind the second
    assert "Couldn't submit the batch job: The request timed out.; 2 requests fall back." in caplog.text
    assert progress.notes[-1] == "jobs 3/3 done"


def test_each_chunk_has_its_own_submit_retries(clock, caplog):
    client = FakeClient()
    refused = Counter()

    def flaky(n, file_id):
        chunk = "first" if '"request-0"' in client.files[file_id] else "second"
        refused[chunk] += 1
        return refused[chunk] <= {"first": 2, "second": 3}[chunk]

    failing_calls(client, "create_batch", flaky, passing_error)
    results, failures, _ = run_batch(batch_runner(client, clock), make_requests(texts(4)), batch_size=2)
    assert results == echoed(0, 1, 2, 3) and failures == {}
    assert refused == {"first": 3, "second": 4}
    assert [caplog.text.count(f"trying again in {delay}s.") for delay in (30, 60, 120)] == [2, 2, 1]


@pytest.mark.parametrize("which", ["first", "later"])
def test_submit_error_that_isnt_passing_is_not_retried(clock, which):
    client = FakeClient()
    target = '"request-0"' if which == "first" else '"request-2"'

    def rejected():
        return LLMRequestError("Azure rejected the request: bad input file", retryable=False, code="invalid_file")

    creates = failing_calls(client, "create_batch", lambda n, file_id: target in client.files[file_id], rejected)
    if which == "first":
        with pytest.raises(LLMRequestError, match="bad input file"):
            run_batch(batch_runner(client, clock), make_requests(texts(4)), batch_size=2)
        assert not client.jobs
    else:
        results, failures, _ = run_batch(batch_runner(client, clock), make_requests(texts(4)), batch_size=2)
        assert results == echoed(0, 1) and sorted(failures) == [2, 3]
    assert sum(target in client.files[file_id] for (file_id,) in creates) == 1
    assert 30.0 not in clock.sleeps


def test_quota_and_passing_errors_have_separate_budgets(clock):
    client = FakeClient()
    errors = iter([passing_error, quota_error, passing_error, quota_error, passing_error])
    creates = failing_calls(client, "create_batch", lambda n, file_id: n <= 5, lambda: next(errors)())
    results, failures, _ = run_batch(batch_runner(client, clock), make_requests(texts(2)))
    assert results == echoed(0, 1) and failures == {}  # five refusals, more than either budget alone allows
    assert clock.sleeps == [30.0, 60.0, 60.0, 120.0, 120.0] + [1.0] * 4
    assert len(creates) == 6


def test_real_client_create_that_timed_out_is_retried_on_a_fresh_upload(clock):
    request = httpx2.Request("POST", "https://res.openai.azure.com/openai/v1/batches")
    sdk = StubOpenAI(create_errors=[openai.APITimeoutError(request=request)])  # no batches.list: nothing adopted
    client = real_client(sdk)
    results, failures, _ = run_batch(batch_runner(client, clock), make_requests(texts(2)))
    assert results == {0: "gpt-batch-dep: t0", 1: "gpt-batch-dep: t1"} and failures == {}
    assert clock.sleeps == [30.0, 1.0, 1.0]
    assert [create["input_file_id"] for create in sdk.batch_creates] == ["file-2"]
    assert sdk.deleted == ["file-1", "file-2", "file-3"]  # the first upload, then the job's input and output


def test_real_client_create_whose_reply_was_lost_adopts_the_job_without_retrying(clock):
    request = httpx2.Request("POST", "https://res.openai.azure.com/openai/v1/batches")
    sdk = StubOpenAI()
    create = sdk.batches.create

    def lost_reply(**kwargs):
        batch = create(**kwargs)  # Azure made the job...
        if len(sdk.batch_creates) == 1:
            raise openai.APITimeoutError(request=request)  # ...but its reply never arrived
        return batch

    def list_batches(*, limit):
        return SimpleNamespace(
            data=[
                SimpleNamespace(**vars(sdk._batch(f"batch_{n}", "validating")), input_file_id=made["input_file_id"])
                for n, made in enumerate(sdk.batch_creates, start=1)
            ]
        )

    sdk.batches.create, sdk.batches.list = lost_reply, list_batches
    results, failures, _ = run_batch(batch_runner(real_client(sdk), clock), make_requests(texts(2)))
    assert results == {0: "gpt-batch-dep: t0", 1: "gpt-batch-dep: t1"} and failures == {}
    assert len(sdk.batch_creates) == 1  # not a second, billed job
    assert 30.0 not in clock.sleeps
    assert sdk.deleted == ["file-1", "file-2"]


def test_backing_off_after_a_passing_error_isnt_shown_as_waiting_for_quota(clock):
    client = FakeClient()
    failing_calls(client, "create_batch", lambda n, file_id: n <= 2, passing_error)
    results, _, progress = run_batch(batch_runner(client, clock), make_requests(texts(2)))
    assert results == echoed(0, 1) and clock.sleeps[:2] == [30.0, 60.0]
    assert not any("token quota" in note for note in progress.notes)


# --- the quota: one budget per step, and waiting for our own jobs ------------------------------------------


def test_quota_that_stays_full_falls_back_the_whole_queue_after_one_wait(clock, caplog):
    client = FakeClient()
    creates = failing_calls(client, "create_batch", lambda n, file_id: True, quota_error)
    results, failures, progress = run_batch(batch_runner(client, clock), make_requests(texts(6)), batch_size=2)
    assert clock.sleeps == [60.0, 120.0, 240.0, 480.0, 900.0, 900.0]  # one wait for the step, not one per chunk
    assert len(creates) == 1 + _QUOTA_RETRIES
    assert len(client.files) == 1 + _QUOTA_RETRIES  # the later chunks were never uploaded
    assert results == {} and sorted(failures) == list(range(6))
    for error in failures.values():
        assert error.retryable and error.code == "batch"
        assert str(error).startswith("the Batch API's enqueued-token quota stayed full: The Batch API's")
    assert (
        "The Batch API's enqueued-token quota stayed full: The Batch API's enqueued-token quota is full: Enqueued "
        "token limit reached; 6 requests fall back."
    ) in caplog.text  # only the first letter is changed
    assert progress.notes[-1] == "jobs 3/3 done"


def test_quota_that_stays_full_sends_the_whole_step_to_the_next_strategy(clock):
    client = FakeClient(sync_responder=lambda m: f"sync {content_of(m)}")
    creates = failing_calls(client, "create_batch", lambda n, file_id: True, quota_error)
    executor = FallbackExecutor([batch_runner(client, clock), SyncRunner(client)])
    results, failures = executor.run(make_requests(texts(6)), RecordingProgress(), batch_size=2)
    assert results == {key: f"sync t{key}" for key in range(6)} and failures == {}
    assert len(creates) == 1 + _QUOTA_RETRIES and clock.now == 2700.0
    assert executor.broken == set()  # a full quota isn't a broken setup


def test_jobs_that_keep_failing_for_quota_fall_back_the_whole_queue(clock, caplog):
    client = FakeClient(
        batch_statuses=("validating", "failed"),
        batch_errors=("token_limit_exceeded: Enqueued token limit reached",),
        batch_error_codes=(QUOTA_CODE,),
    )
    results, failures, _ = run_batch(
        batch_runner(client, clock, max_concurrent_jobs=1), make_requests(texts(6)), batch_size=2
    )
    assert [sleep for sleep in clock.sleeps if sleep != 1.0] == [60.0, 120.0, 240.0, 480.0, 900.0, 900.0]
    assert len(client.jobs) == 1 + _QUOTA_RETRIES
    assert all('"request-0"' in client.files[job.input_file_id] for job in client.jobs.values())
    assert results == {} and sorted(failures) == list(range(6))
    assert "batch job batch-7 ended failed: token_limit_exceeded" in str(failures[0])
    assert str(failures[2]) == str(failures[5]) == "the Batch API's enqueued-token quota stayed full: batch-7"
    assert "The Batch API's enqueued-token quota stayed full: batch-7; 4 requests fall back." in caplog.text


def test_jobs_failing_for_quota_together_share_one_wait(clock):
    client = FakeClient(
        batch_statuses=("validating", "failed"),
        batch_errors=("token_limit_exceeded: Enqueued token limit reached",),
        batch_error_codes=(QUOTA_CODE,),
    )
    results, failures, _ = run_batch(batch_runner(client, clock), make_requests(texts(6)), batch_size=2)
    assert results == {} and sorted(failures) == list(range(6))
    assert [sleep for sleep in clock.sleeps if sleep != 1.0] == [60.0, 120.0, 240.0, 480.0, 900.0, 900.0]
    assert len(client.jobs) <= 3 * (1 + _QUOTA_RETRIES)


def test_job_failing_for_quota_as_our_other_job_ends_is_resubmitted_at_once(clock):
    client = ScriptedClient(
        job_statuses=[("in_progress", "completed"), ("validating", "failed"), ("completed",)],
        batch_errors=("token_limit_exceeded: Enqueued token limit reached",),
        batch_error_codes=(QUOTA_CODE,),
        now=clock,
    )
    results, failures, _ = run_batch(
        batch_runner(client, clock, max_concurrent_jobs=2), make_requests(texts(4)), batch_size=2
    )
    assert results == echoed(0, 1, 2, 3) and failures == {}
    starts = {batch_id: time for kind, batch_id, time in client.events if kind == "start"}
    assert starts == {"batch-1": 0.0, "batch-2": 0.0, "batch-3": 2.0}
    assert 60.0 not in clock.sleeps


def test_job_failing_for_quota_isnt_resubmitted_until_our_running_job_ends(clock):
    client = QuotaClient(
        job_statuses=[("in_progress",) * 19 + ("completed",), ("completed",), ("completed",)],
        batch_errors=("token_limit_exceeded: Enqueued token limit reached",),
        batch_error_codes=(QUOTA_CODE,),
        now=clock,
    )
    creates = watch_calls(client, "create_batch")
    results, failures, progress = run_batch(
        batch_runner(client, clock, max_concurrent_jobs=2), make_requests(texts(4)), batch_size=2
    )
    assert results == echoed(0, 1, 2, 3) and failures == {}
    assert len(creates) == len(client.jobs) == 3  # refused once, then no new job on every poll
    starts = {batch_id: time for kind, batch_id, time in client.events if kind == "start"}
    assert starts == {"batch-1": 0.0, "batch-2": 0.0, "batch-3": 20.0}  # when batch-1 ended
    assert clock.sleeps == [1.0] * 21  # no backing off: our own job held the quota
    assert "jobs 0/2 done · 1 in_progress · waiting for token quota" in progress.notes


# --- progress notes and estimates --------------------------------------------------------------------------


def test_note_is_refreshed_after_each_submission(clock):
    """A slow upload or file check of the next chunk leaves the jobs already submitted on the bar."""
    client = FakeClient()
    progress = RecordingProgress()
    shown_while_checking = []
    original = client.file_status

    def file_status(file_id):
        shown_while_checking.append(progress.notes[-1] if progress.notes else None)
        return original(file_id)

    client.file_status = file_status
    run_batch(batch_runner(client, clock), make_requests(texts(6)), batch_size=2, progress=progress)
    assert shown_while_checking == [None, "jobs 0/3 done · 1 validating", "jobs 0/3 done · 2 validating"]


def test_setup_failure_takes_back_every_running_jobs_estimate(clock):
    client = ScriptedClient(
        job_statuses=[("in_progress", "failed"), ("in_progress",)],
        counts=(1,),
        batch_errors=("model_not_found: gone",),
        batch_error_codes=("model_not_found",),
        sync_responder=lambda m: "sync",
    )
    executor = FallbackExecutor([batch_runner(client, clock), SyncRunner(client)])
    progress = RecordingProgress()
    results, failures = executor.run(make_requests(texts(4)), progress, batch_size=2)
    assert results == dict.fromkeys(range(4), "sync") and failures == {}
    assert progress.advances[:4] == [1, 1, -1, -1]  # the failed job's, then the cancelled one's
    assert progress.done == progress.peak == 4
    assert client.cancelled == ["batch-2"]
    assert sorted(client.deleted) == ["file-1", "file-2"]


# --- deleting input files: once, and only when no job can still read them ----------------------------------

QUOTA_FAILURE = {
    "batch_errors": ("token_limit_exceeded: Enqueued token limit reached",),
    "batch_error_codes": (QUOTA_CODE,),
}


@pytest.mark.parametrize(
    "scenario",
    ["quota job, then fine", "quota job every time", "setup failure", "passing errors", "expired", "timed out"],
)
def test_every_file_is_deleted_exactly_once(clock, scenario):
    options = {}
    if scenario == "quota job, then fine":
        client = ScriptedClient(job_statuses=[("validating", "failed"), ("completed",)], **QUOTA_FAILURE)
    elif scenario == "quota job every time":
        client = FakeClient(batch_statuses=("validating", "failed"), **QUOTA_FAILURE)
    elif scenario == "setup failure":
        client = setup_failure_client()
    elif scenario == "passing errors":
        client = FakeClient()
        failing_calls(client, "create_batch", lambda n, file_id: n <= 2, passing_error)
    elif scenario == "expired":
        client = FakeClient(batch_statuses=("validating", "expired"))
    else:
        client = FakeClient(batch_statuses=("in_progress",))
        options["timeout"] = 2.0
    with contextlib.suppress(LLMSetupError):
        run_batch(batch_runner(client, clock, **options), make_requests(texts(4)))
    assert client.deleted
    assert Counter(client.deleted) == Counter(list(client.files))  # every file, each once


def test_interrupt_keeps_the_input_of_a_job_whose_cancel_failed(clock, caplog):
    client = FakeClient()
    watch_calls(client, "cancel_batch", fail_on=1, error=ConnectionError("cancel refused"))
    runner = batch_runner(client, clock, sleep=sleep_raising(clock, 2, KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        run_batch(runner, make_requests(texts(4)), batch_size=2)
    assert client.cancelled == ["batch-2"]
    assert client.deleted == [input_file_of(client, "batch-2")]  # batch-1 may still be running and reading its input
    assert "Couldn't cancel batch job batch-1 (cancel refused); cancel it in the Azure portal." in caplog.text


def test_breaking_off_keeps_the_input_of_a_job_whose_cancel_failed(clock):
    client = ScriptedClient(batch_statuses=("in_progress",), sync_responder=lambda m: "sync")
    failing_calls(
        client, "cancel_batch", lambda n, batch_id: batch_id == "batch-2", lambda: ConnectionError("cancel refused")
    )
    batch = batch_runner(client, clock, sleep=sleep_raising(clock, 2, RuntimeError("loop broke")))
    executor = FallbackExecutor([batch, SyncRunner(client)])
    results, failures = executor.run(make_requests(texts(6)), RecordingProgress(), batch_size=2)
    assert results == dict.fromkeys(range(6), "sync") and failures == {}
    assert client.cancelled == ["batch-1", "batch-3"]
    assert sorted(client.deleted) == sorted(input_file_of(client, batch_id) for batch_id in ("batch-1", "batch-3"))


def test_interrupt_deletes_the_input_of_a_job_already_cancelling(clock):
    client = FakeClient(batch_statuses=("in_progress",))
    cancels = watch_calls(client, "cancel_batch")
    runner = batch_runner(client, clock, timeout=2.0, sleep=sleep_raising(clock, 4, KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        run_batch(runner, make_requests(texts(2)))
    assert cancels == [("batch-1",)]  # cancelled at t=3 for the timeout; not asked again
    assert client.deleted == [input_file_of(client, "batch-1")]


@pytest.mark.parametrize(
    ("status", "cancel_fails", "stopping", "sent"),
    [
        ("completed", False, True, False),
        ("failed", False, True, False),
        ("expired", False, True, False),
        ("cancelled", False, True, False),
        ("cancelling", False, True, False),
        ("in_progress", False, True, True),
        ("validating", True, False, True),
        ("finalizing", True, False, True),
    ],
)
def test_cancel_says_whether_the_job_is_over_or_stopping(clock, status, cancel_fails, stopping, sent):
    client = FakeClient()
    file_id = client.upload_batch_file(b"")
    view = dataclasses.replace(client.create_batch(file_id), status=status)
    job = runners._Job(chunk=_Chunk([], b""), input_file_id=file_id, batch=view, started=0.0)
    cancels = watch_calls(client, "cancel_batch", fail_on=1 if cancel_fails else None, error=ConnectionError("no"))
    assert batch_runner(client, clock)._cancel(job) is stopping
    assert len(cancels) == (1 if sent else 0)
    if sent and not cancel_fails:
        assert job.batch.status == "cancelling"


def test_batch_setup_codes_live_in_the_client_module():
    assert runners.BATCH_SETUP_CODES is client_module.BATCH_SETUP_CODES
    assert set(SETUP_CODES) == client_module.BATCH_SETUP_CODES
    assert QUOTA_CODE not in client_module.BATCH_SETUP_CODES


# --- output files: split on "\n" only ----------------------------------------------------------------------


@pytest.mark.parametrize("ending", ["\n", "\r\n"])
def test_output_with_unicode_line_breaks_inside_json_strings_is_read(clock, ending):
    replies = {0: "one two", 1: "a b", 2: "c\x85d", 3: "plain"}

    def write(job):
        raw = [json.dumps(ok_line(key, text), ensure_ascii=False) for key, text in replies.items()]
        return [line + ending.removesuffix("\n") for line in raw], []  # written unescaped, as some services do

    client = ScriptedClient(write=write)
    results, failures, progress = run_batch(batch_runner(client, clock), make_requests(texts(4)))
    output = client.files[client.jobs["batch-1"].output_file_id]
    assert " " in output and len(output.splitlines()) > len(replies)  # splitlines() would cut lines apart
    assert results == replies and failures == {}
    assert progress.done == 4


# --- error objects of odd shapes in result lines -----------------------------------------------------------

FILTERED = {"error": {"code": "content_filter", "message": "The response was filtered"}}
FILTER_TEXT = "Azure's content filter blocked this text: The response was filtered"


@pytest.mark.parametrize(
    ("line", "code", "retryable", "text"),
    [
        pytest.param(
            {"custom_id": "request-1", "response": None, "error": {"code": None, "message": FILTERED}},
            "content_filter",
            False,
            FILTER_TEXT,
            id="wrapped-object",
        ),
        pytest.param(
            {"custom_id": "request-1", "error": {"code": None, "message": json.dumps(FILTERED)}},
            "content_filter",
            False,
            FILTER_TEXT,
            id="wrapped-json-string",
        ),
        pytest.param(
            {"custom_id": "request-1", "error": {"code": None, "message": "  \n" + json.dumps(FILTERED)}},
            "content_filter",
            False,
            FILTER_TEXT,
            id="wrapped-json-string-after-whitespace",
        ),
        pytest.param(
            {
                "custom_id": "request-1",
                "error": {"code": None, "message": {"error": {"code": None, "message": json.dumps(FILTERED)}}},
            },
            "content_filter",
            False,
            FILTER_TEXT,
            id="wrapped-twice",
        ),
        pytest.param(
            {
                "custom_id": "request-1",
                "response": {
                    "status_code": 400,
                    "body": {
                        "error": {
                            "code": None,
                            "message": {"error": {"code": "context_length_exceeded", "message": "too many tokens"}},
                        }
                    },
                },
            },
            "context_length_exceeded",
            False,
            "The text is too long for the model: too many tokens",
            id="wrapped-in-the-response-body",
        ),
        pytest.param(
            {
                "custom_id": "request-1",
                "response": {"status_code": 400, "body": json.dumps({"error": {"code": None, "message": FILTERED}})},
            },
            "content_filter",
            False,
            FILTER_TEXT,
            id="wrapped-in-a-body-sent-as-a-string",
        ),
        pytest.param(
            {
                "custom_id": "request-1",
                "error": {"code": None, "message": {"code": "server_error", "message": "later"}},
            },
            "server_error",
            True,
            "server_error: later",
            id="message-is-the-error-itself",
        ),
        pytest.param(
            {"custom_id": "request-1", "error": {"code": 429, "message": "Too many requests"}},
            "429",
            True,
            "429: Too many requests",
            id="numeric-code",
        ),
        pytest.param(
            {
                "custom_id": "request-1",
                "response": {"status_code": 500, "body": {"error": {"code": ["x"], "message": "odd"}}},
            },
            "['x']",
            True,
            "['x']: odd",
            id="list-code",
        ),
        pytest.param(
            {"custom_id": "request-1", "error": {"code": {"inner": "content_filter"}, "message": "odd"}},
            "{'inner': 'content_filter'}",
            True,
            "{'inner': 'content_filter'}: odd",
            id="object-code-isnt-unwrapped",
        ),
        pytest.param(
            {"custom_id": "request-1", "error": {"code": None, "message": "{not json"}},
            None,
            True,
            "{not json",
            id="brace-but-not-json",
        ),
        pytest.param(
            {"custom_id": "request-1", "error": {"code": None, "message": None}},
            None,
            True,
            "no details",
            id="nothing-at-all",
        ),
    ],
)
def test_odd_error_shapes_in_result_lines(clock, line, code, retryable, text):
    def write(job):
        return [ok_line(0, "zero")], [line]

    client = ScriptedClient(write=write)
    results, failures, _ = run_batch(batch_runner(client, clock), make_requests(texts(2)))
    assert results == {0: "zero"} and list(failures) == [1]
    error = failures[1]
    assert error.code == code
    assert error.code is None or type(error.code) is str
    assert error.retryable is retryable
    assert str(error) == text


def test_wrapped_content_filter_line_isnt_retried_by_the_next_strategy(clock):
    def write(job):
        wrapped = {"custom_id": "request-1", "error": {"code": None, "message": json.dumps(FILTERED)}}
        return [ok_line(0, "zero")], [wrapped]

    client = ScriptedClient(write=write, sync_responder=lambda m: "sync")
    executor = FallbackExecutor([batch_runner(client, clock), SyncRunner(client)])
    results, failures = executor.run(make_requests(texts(2)), RecordingProgress(), batch_size=100)
    assert results == {0: "zero"} and list(failures) == [1]
    assert failures[1].code == "content_filter" and not client.calls["sync"]


@pytest.mark.parametrize("as_json", [False, True], ids=["object", "json-string"])
def test_error_message_object_that_isnt_a_wrapper_keeps_its_text(clock, as_json):
    message = {"detail": "Deployment is busy"}

    def write(job):
        error = {"code": None, "message": json.dumps(message) if as_json else message}
        return [ok_line(0, "zero")], [{"custom_id": "request-1", "response": None, "error": error}]

    client = ScriptedClient(write=write)
    results, failures, _ = run_batch(batch_runner(client, clock), make_requests(texts(2)))
    assert results == {0: "zero"} and failures[1].retryable
    assert "Deployment is busy" in str(failures[1])
