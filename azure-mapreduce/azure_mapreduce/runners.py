"""How a list of chat requests gets answered: the Batch API, async calls, or one call after another.

``FallbackExecutor`` tries the strategies in order. A strategy that can't run at all (no batch deployment
accepted, the Batch API rejected the upload, the event loop broke...) hands every request to the next one and
is skipped for the rest of the run. A strategy that runs but loses some requests (a batch job that failed or
expired, a request that kept getting throttled) hands just those on. Requests that fail because of their own
content (content filter, too long) aren't retried, since every route would fail the same way.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import Counter, deque
from collections.abc import Callable, Coroutine, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from .client import AzureChatClient, BatchJob, Messages, batch_line_result
from .errors import ConfigError, LLMRequestError, LLMSetupError
from .progress import StepProgress

log = logging.getLogger(__name__)

T = TypeVar("T")

STRATEGIES = ("batch", "async", "sync")

# Azure's limits for one batch input file: 100,000 requests and 200 MB (kept a little under).
MAX_BATCH_REQUESTS = 100_000
MAX_BATCH_FILE_BYTES = 190 * 1024 * 1024
_TERMINAL = {"completed", "failed", "expired", "cancelled"}
_CANCEL_GRACE_SECONDS = 600.0  # Azure says a cancellation can take up to 10 minutes
_FILE_READY_TIMEOUT_SECONDS = 600.0
_MAX_POLL_ERRORS = 5


@dataclass(frozen=True)
class LLMRequest:
    """One prompt to send. ``key`` is its position in the step (a record, or a reduce group)."""

    key: int
    messages: Messages


class Runner(Protocol):
    name: str

    def available(self) -> bool:
        """Whether the client is set up for this strategy (skipped quietly if not)."""

    def run(
        self, requests: Sequence[LLMRequest], results: dict[int, str], progress: StepProgress, batch_size: int
    ) -> dict[int, LLMRequestError]:
        """Answer the requests: put each reply in ``results`` as it arrives and return the failures.

        Raises when the strategy can't be used at all; replies already in ``results`` are kept.
        """


def chunked(items: Sequence[T], size: int) -> list[Sequence[T]]:
    return [items[start : start + size] for start in range(0, len(items), size)]


class SyncRunner:
    """One request after another: slow, but it needs nothing beyond a working deployment."""

    name = "sync"

    def __init__(self, client: AzureChatClient):
        self.client = client

    def available(self) -> bool:
        return self.client.supports_sync

    def run(self, requests, results, progress, batch_size):
        failures: dict[int, LLMRequestError] = {}
        chunks = chunked(requests, batch_size)
        for index, chunk in enumerate(chunks, start=1):
            if len(chunks) > 1:
                progress.note(f"chunk {index}/{len(chunks)}")
            for request in chunk:
                try:
                    results[request.key] = self.client.complete(request.messages)
                except LLMRequestError as exc:
                    failures[request.key] = exc
                else:
                    progress.advance()
        return failures


class AsyncRunner:
    """Concurrent requests on one event loop, at most ``max_concurrency`` in flight, a chunk at a time."""

    name = "async"

    def __init__(self, client: AzureChatClient, *, max_concurrency: int):
        self.client = client
        self.max_concurrency = max_concurrency

    def available(self) -> bool:
        return self.client.supports_async

    def run(self, requests, results, progress, batch_size):
        return run_coroutine(self._run(requests, results, progress, batch_size))

    async def _run(self, requests, results, progress, batch_size) -> dict[int, LLMRequestError]:
        failures: dict[int, LLMRequestError] = {}
        semaphore = asyncio.Semaphore(self.max_concurrency)
        chunks = chunked(requests, batch_size)
        async with self.client.async_session() as complete:

            async def answer(request: LLMRequest) -> None:
                async with semaphore:
                    try:
                        text = await complete(request.messages)
                    except LLMRequestError as exc:
                        failures[request.key] = exc
                    else:
                        results[request.key] = text
                        progress.advance()

            for index, chunk in enumerate(chunks, start=1):
                if len(chunks) > 1:
                    progress.note(f"chunk {index}/{len(chunks)}")
                try:
                    async with asyncio.TaskGroup() as group:  # the first setup error cancels the rest
                        for request in chunk:
                            group.create_task(answer(request))
                except* LLMSetupError as errors:
                    raise errors.exceptions[0] from None
        return failures


def run_coroutine(coroutine: Coroutine[Any, Any, T]) -> T:
    """Run a coroutine to completion, even from inside a running event loop (Jupyter, Databricks)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    # asyncio.run() refuses to nest, so give the coroutine a loop of its own on a worker thread.
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="azure-mapreduce") as pool:
        return pool.submit(asyncio.run, coroutine).result()


@dataclass
class _Job:
    """A submitted batch job and the requests in it."""

    requests: Sequence[LLMRequest]
    input_file_id: str
    batch: BatchJob
    started: float
    shown: int = 0  # replies already counted on the progress bar
    cancel_requested_at: float | None = None
    poll_errors: int = 0
    keys: set[int] = field(default_factory=set)


class BatchRunner:
    """The Azure Batch API: each chunk of requests becomes one batch job on the batch deployment.

    Up to ``max_concurrent_jobs`` jobs run at once. Jobs are polled every ``poll_interval`` seconds; a job
    still unfinished after ``timeout`` seconds (None: wait for the 24-hour window) is cancelled, its finished
    replies are kept, and the rest go to the next strategy.
    """

    name = "batch"

    def __init__(
        self,
        client: AzureChatClient,
        *,
        poll_interval: float = 30.0,
        timeout: float | None = None,
        max_concurrent_jobs: int = 4,
        cleanup: bool = True,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.client = client
        self.poll_interval = poll_interval
        self.timeout = timeout
        self.max_concurrent_jobs = max_concurrent_jobs
        self.cleanup = cleanup
        self._sleep = sleep
        self._clock = clock

    def available(self) -> bool:
        return self.client.supports_batch

    def run(self, requests, results, progress, batch_size):
        failures: dict[int, LLMRequestError] = {}
        queue = deque(self._chunks(requests, batch_size))
        total_jobs = len(queue)
        finished_jobs = 0
        active: list[_Job] = []
        submitted_any = False
        try:
            while queue or active:
                while queue and len(active) < self.max_concurrent_jobs:
                    chunk, payload = queue.popleft()
                    try:
                        job = self._submit(chunk, payload)
                    except Exception as exc:
                        if not submitted_any:
                            raise  # the Batch API isn't usable here: the executor falls back for everything
                        log.warning("Couldn't submit a batch job (%s); its %d requests fall back.", exc, len(chunk))
                        self._fail(chunk, f"couldn't submit the batch job: {exc}", results, failures)
                        finished_jobs += 1
                        continue
                    submitted_any = True
                    active.append(job)
                    self._describe(progress, active, finished_jobs, total_jobs)
                if not active:
                    continue
                self._sleep(self.poll_interval)
                for job in list(active):
                    if self._poll(job, progress):
                        self._collect(job, results, failures, progress)
                        active.remove(job)
                        finished_jobs += 1
                self._describe(progress, active, finished_jobs, total_jobs)
        finally:
            for job in active:  # interrupted or failed: don't leave jobs running (and billing) behind
                self._cancel(job)
        return failures

    # --- building and submitting jobs --------------------------------------------------------------------

    def _chunks(self, requests: Sequence[LLMRequest], batch_size: int) -> list[tuple[list[LLMRequest], bytes]]:
        """Split the requests into input files of at most batch_size requests and Azure's size limits."""
        limit = min(batch_size, MAX_BATCH_REQUESTS)
        chunks: list[tuple[list[LLMRequest], bytes]] = []
        current: list[LLMRequest] = []
        lines: list[bytes] = []
        size = 0
        for request in requests:
            line = (self.client.batch_line(_custom_id(request.key), request.messages) + "\n").encode("utf-8")
            if current and (len(current) >= limit or size + len(line) > MAX_BATCH_FILE_BYTES):
                chunks.append((current, b"".join(lines)))
                current, lines, size = [], [], 0
            current.append(request)
            lines.append(line)
            size += len(line)
        if current:
            chunks.append((current, b"".join(lines)))
        return chunks

    def _submit(self, chunk: list[LLMRequest], payload: bytes) -> _Job:
        file_id = self.client.upload_batch_file(payload)
        try:
            self._wait_until_processed(file_id)
            batch = self.client.create_batch(file_id)
        except BaseException:
            self._delete(file_id)
            raise
        log.info("Submitted batch job %s with %d requests.", batch.id, len(chunk))
        return _Job(chunk, file_id, batch, started=self._clock(), keys={request.key for request in chunk})

    def _wait_until_processed(self, file_id: str) -> None:
        """Azure validates an uploaded file before a batch job can use it."""
        deadline = self._clock() + _FILE_READY_TIMEOUT_SECONDS
        while True:
            status, details = self.client.file_status(file_id)
            if status in (None, "processed"):
                return
            if status in ("error", "failed", "deleted"):
                raise LLMSetupError(f"Azure couldn't process the batch input file: {details or status}")
            if self._clock() > deadline:
                raise LLMSetupError(
                    f"The batch input file was still {status} after {_FILE_READY_TIMEOUT_SECONDS:.0f}s."
                )
            self._sleep(min(self.poll_interval, 5.0))

    # --- watching jobs -----------------------------------------------------------------------------------

    def _poll(self, job: _Job, progress: StepProgress) -> bool:
        """Refresh a job; True once it's over (finished, or given up on) and ready to collect."""
        try:
            job.batch = self.client.get_batch(job.batch.id)
            job.poll_errors = 0
        except Exception as exc:
            job.poll_errors += 1
            log.warning("Couldn't check batch job %s (%s).", job.batch.id, exc)
            if job.poll_errors >= _MAX_POLL_ERRORS:
                log.warning("Giving up on batch job %s after %d failed checks.", job.batch.id, job.poll_errors)
                self._cancel(job)
                return True
            return False

        # Show replies as the service reports them, rather than all at once when the job ends.
        if job.batch.completed > job.shown:
            progress.advance(job.batch.completed - job.shown)
            job.shown = job.batch.completed

        if job.batch.status in _TERMINAL:
            return True
        now = self._clock()
        if job.cancel_requested_at is None:
            if self.timeout is not None and now - job.started > self.timeout:
                log.warning(
                    "Batch job %s is still %s after %.0fs; cancelling it and sending the rest another way.",
                    job.batch.id,
                    job.batch.status,
                    self.timeout,
                )
                self._cancel(job)
                job.cancel_requested_at = now
            return False
        if now - job.cancel_requested_at > _CANCEL_GRACE_SECONDS:
            log.warning("Batch job %s didn't finish cancelling; moving on without its replies.", job.batch.id)
            return True
        return False

    def _collect(self, job: _Job, results: dict[int, str], failures: dict[int, LLMRequestError], progress) -> None:
        """Read a finished job's output and error files into results and failures."""
        replies = 0
        for file_id in (job.batch.output_file_id, job.batch.error_file_id):
            if not file_id:
                continue
            try:
                content = self.client.read_file(file_id)
            except Exception as exc:
                log.warning("Couldn't download file %s of batch job %s (%s).", file_id, job.batch.id, exc)
                continue
            for raw in content.splitlines():
                if not raw.strip():
                    continue
                try:
                    line = json.loads(raw)
                except ValueError:
                    log.warning("Skipping an unreadable line in batch job %s's results.", job.batch.id)
                    continue
                key = _key_from_custom_id(line.get("custom_id"))
                if key is None or key not in job.keys or key in results:
                    continue
                outcome = batch_line_result(line)
                if isinstance(outcome, str):
                    results[key] = outcome
                    failures.pop(key, None)
                    replies += 1
                else:
                    failures.setdefault(key, outcome)

        reason = f"batch job {job.batch.id} ended {job.batch.status}"
        if job.batch.errors:
            reason += ": " + "; ".join(job.batch.errors[:3])
        missing = [request for request in job.requests if request.key not in results and request.key not in failures]
        if missing and job.batch.status != "completed":
            log.warning("%s without %d replies; they fall back.", reason.capitalize(), len(missing))
        self._fail(missing, reason, results, failures)
        progress.advance(replies - job.shown)  # settle the running estimate with what actually came back
        job.shown = replies
        if self.cleanup:
            for file_id in (job.input_file_id, job.batch.output_file_id, job.batch.error_file_id):
                if file_id:
                    self._delete(file_id)

    def _describe(self, progress: StepProgress, active: list[_Job], finished: int, total: int) -> None:
        statuses = Counter(job.batch.status for job in active)
        running = ", ".join(f"{count} {status}" for status, count in sorted(statuses.items()))
        progress.note(f"jobs {finished}/{total} done" + (f" · {running}" if running else ""))

    # --- housekeeping ------------------------------------------------------------------------------------

    @staticmethod
    def _fail(requests, reason: str, results, failures) -> None:
        for request in requests:
            if request.key not in results:
                failures.setdefault(request.key, LLMRequestError(reason, retryable=True, code="batch"))

    def _cancel(self, job: _Job) -> None:
        if job.batch.status in _TERMINAL or job.batch.status == "cancelling":
            return
        try:
            job.batch = self.client.cancel_batch(job.batch.id)
        except Exception as exc:
            log.warning("Couldn't cancel batch job %s (%s); cancel it in the Azure portal.", job.batch.id, exc)

    def _delete(self, file_id: str) -> None:
        try:
            self.client.delete_file(file_id)
        except Exception as exc:
            log.debug("Couldn't delete file %s (%s).", file_id, exc)


def _custom_id(key: int) -> str:
    return f"request-{key}"


def _key_from_custom_id(custom_id: object) -> int | None:
    if isinstance(custom_id, str) and custom_id.startswith("request-"):
        suffix = custom_id.removeprefix("request-")
        if suffix.isdigit():
            return int(suffix)
    return None


class FallbackExecutor:
    """Answers requests with the first strategy that works, handing failures down the chain."""

    def __init__(self, runners: Sequence[Runner]):
        if not runners:
            raise ConfigError("Choose at least one strategy.")
        self.runners = list(runners)
        self.broken: set[str] = set()  # strategies that failed outright; skipped for the rest of the run

    def run(
        self, requests: Sequence[LLMRequest], progress: StepProgress, *, batch_size: int
    ) -> tuple[dict[int, str], dict[int, LLMRequestError]]:
        """Answer every request. Returns the replies and the requests that failed for good, by key."""
        results: dict[int, str] = {}
        failures: dict[int, LLMRequestError] = {}
        if not requests:
            return results, failures
        runners = [runner for runner in self.runners if runner.name not in self.broken and runner.available()]
        if not runners:
            names = ", ".join(runner.name for runner in self.runners)
            raise ConfigError(
                f"None of the strategies ({names}) can run with this client. The Batch API needs a "
                "batch_deployment; async and sync calls need a standard deployment."
            )
        pending = list(requests)
        for index, runner in enumerate(runners):
            last = index == len(runners) - 1
            progress.strategy(runner.name)
            try:
                failed = runner.run(pending, results, progress, batch_size)
            except Exception as exc:
                pending = [request for request in pending if request.key not in results]
                if last:
                    raise
                self.broken.add(runner.name)
                log.warning(
                    "The %s strategy failed (%s: %s); sending %d requests with %s instead.",
                    runner.name,
                    type(exc).__name__,
                    exc,
                    len(pending),
                    runners[index + 1].name,
                )
                continue

            retry: list[LLMRequest] = []
            for request in pending:
                if request.key in results:
                    continue
                error = failed.get(request.key) or LLMRequestError("No reply came back.", retryable=True)
                if error.retryable and not last:
                    retry.append(request)
                else:
                    failures[request.key] = error
                    progress.fail()
            if retry:
                log.info(
                    "%d requests failed with %s; retrying them with %s.",
                    len(retry),
                    runner.name,
                    runners[index + 1].name,
                )
            pending = retry
            if not pending:
                break
        return results, failures


def build_executor(
    client: AzureChatClient,
    strategies: Sequence[str] = STRATEGIES,
    *,
    max_concurrency: int = 16,
    batch_poll_interval: float = 30.0,
    batch_timeout: float | None = None,
    max_concurrent_batch_jobs: int = 4,
    batch_cleanup: bool = True,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> FallbackExecutor:
    """A FallbackExecutor with the named strategies, in the order given."""
    unknown = [name for name in strategies if name not in STRATEGIES]
    if unknown or len(set(strategies)) != len(strategies):
        raise ConfigError(f"strategies must be distinct names from {STRATEGIES} (got {tuple(strategies)}).")
    factories: dict[str, Callable[[], Runner]] = {
        "batch": lambda: BatchRunner(
            client,
            poll_interval=batch_poll_interval,
            timeout=batch_timeout,
            max_concurrent_jobs=max_concurrent_batch_jobs,
            cleanup=batch_cleanup,
            sleep=sleep,
            clock=clock,
        ),
        "async": lambda: AsyncRunner(client, max_concurrency=max_concurrency),
        "sync": lambda: SyncRunner(client),
    }
    return FallbackExecutor([factories[name]() for name in strategies])
