"""Shared test helpers: a fake Azure chat client with a scriptable Batch API, sync and async calls."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from azure_mapreduce.client import BATCH_ENDPOINT, BatchJob
from azure_mapreduce.errors import LLMRequestError, LLMSetupError

Responder = Callable[[list[dict]], str]


def echo(messages: list[dict]) -> str:
    """The default reply: the user prompt, marked so tests can tell which call produced it."""
    return f"<{messages[-1]['content']}>"


def chat_body(content: str | None = "ok", finish_reason: str = "stop", refusal: str | None = None) -> dict:
    """A chat completion response body, as the API (or a batch output line) returns it."""
    message = {"role": "assistant", "content": content}
    if refusal is not None:
        message["refusal"] = refusal
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "gpt-test",
        "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}],
    }


@dataclass
class FakeJob:
    id: str
    input_file_id: str
    lines: list[dict]
    statuses: list[str]  # returned by successive get_batch calls; the last one repeats
    polls: int = 0
    cancelled: bool = False
    output_file_id: str | None = None
    error_file_id: str | None = None
    errors: tuple[str, ...] = ()


@dataclass
class FakeClient:
    """Stands in for AzureChatClient.

    ``responder(messages)`` answers every route (raise LLMRequestError or LLMSetupError to fail). Per-route
    overrides (``sync_responder``, ``async_responder``, ``batch_responder``) take precedence. In batch jobs a
    responder may also return None to leave that request out of the output entirely.
    """

    responder: Responder = echo
    sync_responder: Responder | None = None
    async_responder: Responder | None = None
    batch_responder: Callable[[list[dict]], str | None] | None = None
    deployment: str | None = "gpt-test"
    batch_deployment: str | None = "gpt-batch"
    async_ok: bool = True
    # Batch job statuses reported by successive polls (the last one repeats). Jobs that end "completed",
    # "expired" or "cancelled" get output/error files; "failed" ones don't.
    batch_statuses: tuple[str, ...] = ("validating", "in_progress", "finalizing", "completed")
    batch_errors: tuple[str, ...] = ()
    batch_error_codes: tuple[str, ...] = ()  # reported with batch_errors when a job ends "failed"
    file_statuses: tuple[str | None, ...] = ("processed",)
    upload_error: Exception | None = None
    create_error: Exception | None = None
    poll_error: Exception | None = None
    completion_options: dict = field(default_factory=dict)

    calls: dict[str, list[list[dict]]] = field(default_factory=lambda: {"sync": [], "async": [], "batch": []})
    jobs: dict[str, FakeJob] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)
    deleted: list[str] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    async_sessions: int = 0
    _file_polls: int = 0

    # --- what AzureChatClient exposes ------------------------------------------------------------------

    @property
    def supports_batch(self) -> bool:
        return bool(self.batch_deployment)

    @property
    def supports_async(self) -> bool:
        return bool(self.deployment) and self.async_ok

    @property
    def supports_sync(self) -> bool:
        return bool(self.deployment)

    def request_body(self, messages, *, batch=False):
        return {"model": self.batch_deployment if batch else self.deployment, "messages": messages}

    def complete(self, messages):
        if not self.deployment:
            raise LLMSetupError("no deployment")
        self.calls["sync"].append(messages)
        return (self.sync_responder or self.responder)(messages)

    @contextlib.asynccontextmanager
    async def async_session(self):
        if not self.supports_async:
            raise LLMSetupError("no async client")
        self.async_sessions += 1

        async def complete(messages):
            self.calls["async"].append(messages)
            return (self.async_responder or self.responder)(messages)

        yield complete

    def batch_line(self, custom_id, messages):
        body = self.request_body(messages, batch=True)
        return json.dumps({"custom_id": custom_id, "method": "POST", "url": BATCH_ENDPOINT, "body": body})

    def upload_batch_file(self, content: bytes) -> str:
        if self.upload_error:
            raise self.upload_error
        file_id = f"file-{len(self.files) + 1}"
        self.files[file_id] = content.decode("utf-8")
        return file_id

    def file_status(self, file_id):
        status = self.file_statuses[min(self._file_polls, len(self.file_statuses) - 1)]
        self._file_polls += 1
        return status, ("bad file" if status == "error" else None)

    def create_batch(self, input_file_id: str) -> BatchJob:
        if self.create_error:
            raise self.create_error
        lines = [json.loads(raw) for raw in self.files[input_file_id].splitlines() if raw.strip()]
        job = FakeJob(f"batch-{len(self.jobs) + 1}", input_file_id, lines, list(self.batch_statuses))
        self.jobs[job.id] = job
        return self._view(job, "validating")

    def get_batch(self, batch_id: str) -> BatchJob:
        if self.poll_error:
            raise self.poll_error
        job = self.jobs[batch_id]
        status = "cancelled" if job.cancelled else job.statuses[min(job.polls, len(job.statuses) - 1)]
        job.polls += 1
        if status in ("completed", "expired", "cancelled") and job.output_file_id is None:
            self._finish(job, partial=status != "completed")
        return self._view(job, status)

    def cancel_batch(self, batch_id: str) -> BatchJob:
        self.cancelled.append(batch_id)
        job = self.jobs[batch_id]
        job.cancelled = True
        return self._view(job, "cancelling")

    def read_file(self, file_id: str) -> str:
        return self.files[file_id]

    def delete_file(self, file_id: str) -> None:
        self.deleted.append(file_id)

    # --- the fake service ------------------------------------------------------------------------------

    def _finish(self, job: FakeJob, *, partial: bool) -> None:
        """Answer the job's requests into output and error files (the first half only if partial)."""
        outputs, errors = [], []
        answered = job.lines[: len(job.lines) // 2] if partial else job.lines
        for line in answered:
            messages = line["body"]["messages"]
            self.calls["batch"].append(messages)
            try:
                text = (self.batch_responder or self.responder)(messages)
            except LLMRequestError as exc:
                errors.append(
                    {
                        "custom_id": line["custom_id"],
                        "response": {"status_code": 400, "body": {"error": {"code": exc.code, "message": str(exc)}}},
                        "error": None,
                    }
                )
                continue
            if text is None:
                continue
            outputs.append(
                {
                    "custom_id": line["custom_id"],
                    "response": {"status_code": 200, "body": chat_body(text)},
                    "error": None,
                }
            )
        job.output_file_id = self._store("\n".join(json.dumps(o) for o in outputs)) if outputs else None
        job.error_file_id = self._store("\n".join(json.dumps(e) for e in errors)) if errors else None

    def _store(self, content: str) -> str:
        file_id = f"file-{len(self.files) + 1}"
        self.files[file_id] = content
        return file_id

    def _view(self, job: FakeJob, status: str) -> BatchJob:
        done = status in ("completed", "expired", "cancelled")
        completed = 0
        if done and job.output_file_id:
            completed = len(self.files[job.output_file_id].splitlines())
        return BatchJob(
            id=job.id,
            status=status,
            completed=completed,
            failed=0,
            total=len(job.lines),
            output_file_id=job.output_file_id if done else None,
            error_file_id=job.error_file_id if done else None,
            errors=self.batch_errors if status == "failed" else (),
            error_codes=self.batch_error_codes if status == "failed" else (),
        )


class FakeClock:
    """A clock that only moves when the code under test sleeps."""

    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def content_of(messages: list[dict]) -> str:
    return messages[-1]["content"]
