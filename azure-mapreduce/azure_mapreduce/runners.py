"""How a list of chat requests gets answered: the Batch API, async calls, or one call after another.

``FallbackExecutor`` tries the strategies in order. A strategy that can't run at all (the Batch API rejected
the upload, a batch job failed validation, the credentials are wrong, Azure can't be reached...) hands every
request it hasn't answered to the next one and is skipped for the rest of the run. A strategy that runs but
loses some requests (a batch job that expired, a request that kept getting throttled) hands just those on.
Requests that fail because of their own content (content filter, too long) aren't retried, since every route
would fail the same way.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Coroutine, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from .client import BATCH_SETUP_CODES, QUOTA_CODE, AzureChatClient, BatchJob, Messages, batch_line_result
from .errors import ConfigError, LLMRequestError, LLMSetupError, attach
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
_POLL_GIVE_UP_SECONDS = 1800.0  # a job that can't be checked for this long is given up on
_DOWNLOAD_ATTEMPTS = 3
# The enqueued-token quota: with none of our own jobs holding it, wait 1, 2, 4, 8, 15, 15 minutes before giving up.
_QUOTA_RETRIES = 6
_QUOTA_BACKOFF_SECONDS = 60.0
_QUOTA_BACKOFF_MAX_SECONDS = 900.0
# Passing errors (timeouts, 5xx) while submitting a job: try again after 30, 60 and 120 seconds.
_SUBMIT_RETRIES = 3
_SUBMIT_BACKOFF_SECONDS = 30.0
# A strategy stops when this many requests in a row can't reach Azure at all (fewer before any has succeeded).
_UNREACHABLE_CODES = {"connection", "credential"}
_UNREACHABLE_LIMIT = 10
_UNREACHABLE_LIMIT_BEFORE_SUCCESS = 3


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
    if size < 1:
        raise ConfigError(f"The batch size must be at least 1 (got {size!r}).")
    return [items[start : start + size] for start in range(0, len(items), size)]


class _Reachability:
    """Stops a strategy once Azure can't be reached on request after request (wrong endpoint, no network).

    A single connection error that outlasts the SDK's retries is just a failed request, retried later by the
    next strategy; a run of them means every other request would fail the same way. Requests that were already
    in flight when a failure was counted went through the same outage, so they don't add to the streak: only
    requests started afterwards do. Call ``begin()`` when a request starts and pass its result to ``failure()``.
    """

    def __init__(self) -> None:
        self.streak = 0
        self.succeeded = False
        self._counted = 0  # failures counted so far; a request started before the last one shares its outage

    def begin(self) -> int:
        return self._counted

    def success(self) -> None:
        self.streak = 0
        self.succeeded = True

    def failure(self, error: LLMRequestError, started: int | None = None) -> None:
        if error.code not in _UNREACHABLE_CODES:
            self.streak = 0  # Azure answered, so it can be reached
            return
        if started is not None and started < self._counted:
            return  # in flight during an outage that was already counted
        self._counted += 1
        self.streak += 1
        limit = _UNREACHABLE_LIMIT if self.succeeded else _UNREACHABLE_LIMIT_BEFORE_SUCCESS
        if self.streak >= limit:
            raise LLMSetupError(f"{self.streak} requests in a row couldn't reach Azure. The last error: {error}")


class SyncRunner:
    """One request after another: slow, but it needs nothing beyond a working deployment."""

    name = "sync"

    def __init__(self, client: AzureChatClient):
        self.client = client

    def available(self) -> bool:
        return self.client.supports_sync

    def run(self, requests, results, progress, batch_size):
        failures: dict[int, LLMRequestError] = {}
        reachability = _Reachability()
        chunks = chunked(requests, batch_size)
        for index, chunk in enumerate(chunks, start=1):
            if len(chunks) > 1:
                progress.note(f"chunk {index}/{len(chunks)}")
            for request in chunk:
                started = reachability.begin()
                try:
                    results[request.key] = self.client.complete(request.messages)
                except LLMRequestError as exc:
                    failures[request.key] = exc
                    reachability.failure(exc, started)
                else:
                    reachability.success()
                    progress.advance()
        return failures


class AsyncRunner:
    """Concurrent requests on one event loop, at most ``max_concurrency`` in flight, a chunk at a time."""

    name = "async"

    def __init__(self, client: AzureChatClient, *, max_concurrency: int):
        if not isinstance(max_concurrency, int) or max_concurrency < 1:
            raise ConfigError(f"max_concurrency must be at least 1 (got {max_concurrency!r}).")
        self.client = client
        self.max_concurrency = max_concurrency

    def available(self) -> bool:
        return self.client.supports_async

    def run(self, requests, results, progress, batch_size):
        return run_coroutine(self._run(requests, results, progress, batch_size))

    async def _run(self, requests, results, progress, batch_size) -> dict[int, LLMRequestError]:
        failures: dict[int, LLMRequestError] = {}
        reachability = _Reachability()
        semaphore = asyncio.Semaphore(self.max_concurrency)
        chunks = chunked(requests, batch_size)
        async with self.client.async_session() as complete:

            async def answer(request: LLMRequest) -> None:
                async with semaphore:
                    started = reachability.begin()
                    try:
                        text = await complete(request.messages)
                    except LLMRequestError as exc:
                        failures[request.key] = exc
                        reachability.failure(exc, started)  # raises once Azure is clearly unreachable
                    else:
                        results[request.key] = text
                        reachability.success()
                        progress.advance()

            for index, chunk in enumerate(chunks, start=1):
                if len(chunks) > 1:
                    progress.note(f"chunk {index}/{len(chunks)}")
                error: BaseException | None = None
                try:
                    async with asyncio.TaskGroup() as group:  # the first error cancels the rest of the chunk
                        for request in chunk:
                            group.create_task(answer(request))
                except BaseExceptionGroup as errors:
                    error = _main_error(errors)
                if error is not None:
                    raise error  # outside the handler, so it keeps its own cause and context
        return failures


def _main_error(group: BaseExceptionGroup) -> BaseException:
    """The exception worth raising from a TaskGroup's group: a setup error first, else the first real one."""
    leaves = list(_leaves(group))
    for leaf in leaves:
        if isinstance(leaf, LLMSetupError):
            return leaf
    for leaf in leaves:
        if not isinstance(leaf, asyncio.CancelledError):
            return leaf
    return leaves[0]


def _leaves(group: BaseExceptionGroup) -> Iterator[BaseException]:
    for error in group.exceptions:
        if isinstance(error, BaseExceptionGroup):
            yield from _leaves(error)
        else:
            yield error


def run_coroutine(coroutine: Coroutine[Any, Any, T]) -> T:
    """Run a coroutine to completion, even from inside a running event loop (Jupyter, Databricks).

    asyncio.run() refuses to nest, so inside a running loop the coroutine gets a loop of its own on a worker
    thread. Interrupting the caller (Ctrl+C, "Interrupt kernel") cancels the coroutine instead of leaving it
    sending requests in the background.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        running = False
    else:
        running = True
    if not running:  # outside the handler above, so the coroutine's errors don't carry its RuntimeError
        return asyncio.run(coroutine)

    loop = asyncio.new_event_loop()
    task = loop.create_task(coroutine)
    outcome: dict[str, Any] = {}
    finished = threading.Event()

    def work() -> None:
        asyncio.set_event_loop(loop)
        try:
            outcome["value"] = loop.run_until_complete(task)
        except BaseException as exc:  # handed to the caller's thread below
            outcome["error"] = exc
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()
                finished.set()

    threading.Thread(target=work, name="azure-mapreduce", daemon=True).start()
    try:
        # Short waits on an Event, so an interrupt reaches this thread promptly. (An interrupted Thread.join
        # would mark the worker as stopped while it still runs.)
        while not finished.wait(0.1):
            pass
    except BaseException:
        with contextlib.suppress(RuntimeError):  # the loop may have closed in the meantime
            loop.call_soon_threadsafe(task.cancel)
        finished.wait(10)  # let the cancellation close the async client
        raise
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


@dataclass
class _Chunk:
    """The requests for one batch job and its input file."""

    requests: list[LLMRequest]
    payload: bytes
    quota_retries: int = 0
    submit_retries: int = 0
    ready_at: float = 0.0  # don't submit before this time (backing off)


@dataclass
class _Job:
    """A submitted batch job."""

    chunk: _Chunk
    input_file_id: str
    batch: BatchJob
    started: float
    keys: set[int] = field(default_factory=set)
    shown: int = 0  # replies already counted on the progress bar
    cancel_requested_at: float | None = None
    unreachable_since: float | None = None
    abandoned: bool = False  # given up on without knowing how it ended


class BatchRunner:
    """The Azure Batch API: each chunk of requests becomes one batch job on the batch deployment.

    Up to ``max_concurrent_jobs`` jobs run at once, checked every ``poll_interval`` seconds. A job still
    unfinished after ``timeout`` seconds (None: wait as long as it takes) is cancelled; after at most
    ``cancel_wait`` more seconds its finished replies are kept and the rest go to the next strategy.

    When Azure's enqueued-token quota is full, chunks wait for running jobs to finish (or back off) and are
    submitted again rather than falling back. A job that fails validation for a setup reason (unknown or
    non-batch deployment...) stops the strategy, since every other job would fail the same way.
    """

    name = "batch"

    def __init__(
        self,
        client: AzureChatClient,
        *,
        poll_interval: float = 60.0,
        timeout: float | None = 24 * 3600.0,
        max_concurrent_jobs: int = 4,
        cleanup: bool = True,
        cancel_wait: float = _CANCEL_GRACE_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not isinstance(max_concurrent_jobs, int) or max_concurrent_jobs < 1:
            raise ConfigError(f"max_concurrent_batch_jobs must be at least 1 (got {max_concurrent_jobs!r}).")
        self.client = client
        self.poll_interval = poll_interval
        self.timeout = timeout
        self.max_concurrent_jobs = max_concurrent_jobs
        self.cleanup = cleanup
        self.cancel_wait = cancel_wait
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
        quota_blocked = False  # our own running jobs hold the quota: wait for one of them to end

        def give_up(chunks: list[_Chunk], reason: str) -> None:
            nonlocal finished_jobs
            log.warning("%s; %d requests fall back.", _sentence(reason), sum(len(c.requests) for c in chunks))
            for chunk in chunks:
                self._fail(chunk.requests, reason, results, failures)
            finished_jobs += len(chunks)

        try:
            while queue or active:
                while queue and len(active) < self.max_concurrent_jobs and not quota_blocked:
                    if queue[0].ready_at > self._clock():
                        break
                    chunk = queue.popleft()
                    try:
                        job = self._submit(chunk)
                    except Exception as exc:
                        if _is_quota_error(exc):
                            if self._requeue_for_quota(chunk, queue, active, exc):
                                quota_blocked = bool(active)
                                continue
                            # Full with none of our jobs holding it, for the whole wait: the rest gives up too.
                            give_up([chunk, *queue], f"the Batch API's enqueued-token quota stayed full: {exc}")
                            queue.clear()
                            continue
                        if _is_transient(exc) and self._requeue_after_error(chunk, queue, exc):
                            continue
                        if not submitted_any:
                            raise  # the Batch API isn't usable here: the executor falls back for everything
                        give_up([chunk], f"couldn't submit the batch job: {exc}")
                        continue
                    submitted_any = True
                    active.append(job)
                    self._describe(progress, active, queue, finished_jobs, total_jobs, quota_blocked)
                self._describe(progress, active, queue, finished_jobs, total_jobs, quota_blocked)
                if not active:
                    quota_blocked = False
                    if queue:  # backing off before the next submission
                        self._sleep(max(queue[0].ready_at - self._clock(), 1.0))
                    continue
                self._sleep(self.poll_interval)
                freed = requeued = False
                for job in list(active):
                    if not self._poll(job, progress):
                        continue
                    active.remove(job)
                    if job.batch.status == "failed" and QUOTA_CODE in job.batch.error_codes:
                        if self._quota_failure(job, queue, active, progress):
                            requeued = True
                            continue
                        self._collect(job, results, failures, progress)
                        finished_jobs += 1
                        if queue:
                            give_up(list(queue), f"the Batch API's enqueued-token quota stayed full: {job.batch.id}")
                            queue.clear()
                        continue
                    if job.batch.status == "failed":
                        self._check_setup_failure(job, progress)  # raises when every job would fail the same way
                    freed = True  # a job ended for its own reasons, so its quota is free again
                    self._collect(job, results, failures, progress)
                    finished_jobs += 1
                if freed:
                    quota_blocked = False
                elif requeued:
                    quota_blocked = bool(active)
                self._describe(progress, active, queue, finished_jobs, total_jobs, quota_blocked)
        finally:
            for job in active:  # interrupted or broken: don't leave jobs running (and billing) behind
                progress.advance(-job.shown)  # their requests go elsewhere now
                job.shown = 0
                if self._cancel(job) and self.cleanup:  # a job still running may still read its input
                    self._delete(job.input_file_id)
        return failures

    # --- building and submitting jobs --------------------------------------------------------------------

    def _chunks(self, requests: Sequence[LLMRequest], batch_size: int) -> list[_Chunk]:
        """Split the requests into input files of at most batch_size requests and Azure's size limits."""
        if batch_size < 1:
            raise ConfigError(f"The batch size must be at least 1 (got {batch_size!r}).")
        limit = min(batch_size, MAX_BATCH_REQUESTS)
        chunks: list[_Chunk] = []
        current: list[LLMRequest] = []
        lines: list[bytes] = []
        size = 0
        for request in requests:
            line = (self.client.batch_line(_custom_id(request.key), request.messages) + "\n").encode("utf-8")
            if current and (len(current) >= limit or size + len(line) > MAX_BATCH_FILE_BYTES):
                chunks.append(_Chunk(current, b"".join(lines)))
                current, lines, size = [], [], 0
            current.append(request)
            lines.append(line)
            size += len(line)
        if current:
            chunks.append(_Chunk(current, b"".join(lines)))
        return chunks

    def _submit(self, chunk: _Chunk) -> _Job:
        file_id = self.client.upload_batch_file(chunk.payload)
        try:
            self._wait_until_processed(file_id)
            batch = self.client.create_batch(file_id)
        except BaseException:
            self._delete(file_id)
            raise
        log.info("Submitted batch job %s with %d requests.", batch.id, len(chunk.requests))
        keys = {request.key for request in chunk.requests}
        return _Job(chunk, file_id, batch, started=self._clock(), keys=keys)

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

    def _requeue_for_quota(self, chunk: _Chunk, queue: deque[_Chunk], active: list[_Job], error: object) -> bool:
        """Put a chunk back after a full-quota error; False once it has waited long enough."""
        if active:
            # Our own jobs hold the quota: try again when one of them ends, without using up a retry.
            chunk.ready_at = 0.0
            log.info("The Batch API's token quota is full; waiting for a running job to finish.")
        else:
            chunk.quota_retries += 1
            if chunk.quota_retries > _QUOTA_RETRIES:
                return False
            delay = min(_QUOTA_BACKOFF_SECONDS * 2 ** (chunk.quota_retries - 1), _QUOTA_BACKOFF_MAX_SECONDS)
            chunk.ready_at = self._clock() + delay
            log.info("The Batch API's token quota is full (%s); trying again in %.0fs.", error, delay)
        queue.appendleft(chunk)
        return True

    def _requeue_after_error(self, chunk: _Chunk, queue: deque[_Chunk], error: object) -> bool:
        """Put a chunk back after a passing submission error (a timeout, a 5xx); False once out of retries."""
        if chunk.submit_retries >= _SUBMIT_RETRIES:
            return False
        delay = _SUBMIT_BACKOFF_SECONDS * 2**chunk.submit_retries
        chunk.submit_retries += 1
        chunk.ready_at = self._clock() + delay
        log.warning("Couldn't submit a batch job (%s); trying again in %.0fs.", error, delay)
        queue.appendleft(chunk)
        return True

    # --- watching jobs -----------------------------------------------------------------------------------

    def _poll(self, job: _Job, progress: StepProgress) -> bool:
        """Refresh a job; True once it's over (ended, or given up on) and ready to collect."""
        now = self._clock()
        try:
            job.batch = self.client.get_batch(job.batch.id)
            job.unreachable_since = None
        except Exception as exc:
            if job.unreachable_since is None:
                job.unreachable_since = now
            log.warning("Couldn't check batch job %s (%s).", job.batch.id, exc)
            if now - job.unreachable_since < _POLL_GIVE_UP_SECONDS:
                return False
            log.warning(
                "Giving up on batch job %s after %.0f minutes without news. It may still be running: check it in "
                "the Azure portal and cancel it there if so.",
                job.batch.id,
                (now - job.unreachable_since) / 60,
            )
            self._cancel(job)
            job.abandoned = True
            return True

        # Show replies as the service reports them, rather than all at once when the job ends.
        if job.batch.completed > job.shown:
            progress.advance(job.batch.completed - job.shown)
            job.shown = job.batch.completed

        if job.batch.status in _TERMINAL:
            return True
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
        if now - job.cancel_requested_at > self.cancel_wait:
            log.warning("Batch job %s didn't finish cancelling; moving on without its replies.", job.batch.id)
            job.abandoned = True
            return True
        return False

    def _quota_failure(self, job: _Job, queue: deque[_Chunk], active: list[_Job], progress: StepProgress) -> bool:
        """A job that failed validation because the token quota was full: True if its chunk was put back."""
        progress.advance(-job.shown)
        job.shown = 0
        if not self._requeue_for_quota(job.chunk, queue, active, "; ".join(job.batch.errors[:1]) or job.batch.id):
            return False  # _collect reports it and cleans up
        if self.cleanup:
            self._delete(job.input_file_id)
        return True

    def _check_setup_failure(self, job: _Job, progress: StepProgress) -> None:
        """Raise when a job failed validation for a reason every other job would hit too."""
        if not set(job.batch.error_codes) & BATCH_SETUP_CODES:
            return
        progress.advance(-job.shown)
        job.shown = 0
        if self.cleanup:
            self._delete(job.input_file_id)
        details = "; ".join(job.batch.errors[:3]) or "no details"
        raise LLMSetupError(f"Batch job {job.batch.id} failed validation, and every job would: {details}")

    def _collect(self, job: _Job, results: dict[int, str], failures: dict[int, LLMRequestError], progress) -> None:
        """Read an ended job's output and error files into results and failures."""
        replies = 0
        unread: list[str] = []
        for file_id in (job.batch.output_file_id, job.batch.error_file_id):
            if not file_id:
                continue
            content = self._download(file_id, job)
            if content is None:
                unread.append(file_id)
                continue
            for raw in content.split("\n"):  # not splitlines(): JSON may carry U+2028 and friends unescaped
                if not raw.strip():
                    continue
                try:
                    line = json.loads(raw)
                except ValueError:
                    line = None
                if not isinstance(line, dict):
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

        if job.abandoned:
            reason = f"batch job {job.batch.id} was given up on while {job.batch.status}"
        else:
            reason = f"batch job {job.batch.id} ended {job.batch.status}"
        if job.batch.errors:
            reason += ": " + "; ".join(job.batch.errors[:3])
        missing = [r for r in job.chunk.requests if r.key not in results and r.key not in failures]
        if missing:
            log.warning("%s without %d replies; they fall back.", _sentence(reason), len(missing))
        self._fail(missing, reason, results, failures)
        progress.advance(replies - job.shown)  # settle the running estimate with what actually came back
        job.shown = replies
        if unread:
            log.warning(
                "Kept file(s) %s of batch job %s on Azure because they couldn't be downloaded; the replies in "
                "them are recoverable from the Azure portal.",
                ", ".join(unread),
                job.batch.id,
            )
        if self.cleanup:
            if not job.abandoned:  # an abandoned job may still be running and reading its input
                self._delete(job.input_file_id)
            for file_id in (job.batch.output_file_id, job.batch.error_file_id):
                if file_id and file_id not in unread:
                    self._delete(file_id)

    def _download(self, file_id: str, job: _Job) -> str | None:
        for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
            try:
                return self.client.read_file(file_id)
            except Exception as exc:
                log.warning(
                    "Couldn't download file %s of batch job %s (attempt %d of %d: %s).",
                    file_id,
                    job.batch.id,
                    attempt,
                    _DOWNLOAD_ATTEMPTS,
                    exc,
                )
                if attempt < _DOWNLOAD_ATTEMPTS:
                    self._sleep(5.0 * attempt)
        return None

    def _describe(self, progress, active: list[_Job], queue, finished: int, total: int, quota_blocked: bool) -> None:
        statuses = Counter(job.batch.status for job in active)
        parts = [f"jobs {finished}/{total} done"]
        if statuses:
            parts.append(", ".join(f"{count} {status}" for status, count in sorted(statuses.items())))
        if queue and (quota_blocked or queue[0].ready_at > self._clock()):
            parts.append("waiting for token quota")
        progress.note(" · ".join(parts))

    # --- housekeeping ------------------------------------------------------------------------------------

    @staticmethod
    def _fail(requests, reason: str, results, failures) -> None:
        for request in requests:
            if request.key not in results:
                failures.setdefault(request.key, LLMRequestError(reason, retryable=True, code="batch"))

    def _cancel(self, job: _Job) -> bool:
        """Cancel a job unless it has ended; False if the cancel couldn't be sent (the job may still run)."""
        if job.batch.status in _TERMINAL or job.batch.status == "cancelling":
            return True
        try:
            job.batch = self.client.cancel_batch(job.batch.id)
        except Exception as exc:
            log.warning("Couldn't cancel batch job %s (%s); cancel it in the Azure portal.", job.batch.id, exc)
            return False
        return True

    def _delete(self, file_id: str) -> None:
        try:
            self.client.delete_file(file_id)
        except Exception as exc:
            log.debug("Couldn't delete file %s (%s).", file_id, exc)


def _is_quota_error(exc: BaseException) -> bool:
    return getattr(exc, "code", None) == QUOTA_CODE or QUOTA_CODE in str(exc)


def _is_transient(exc: BaseException) -> bool:
    return isinstance(exc, LLMRequestError) and exc.retryable


def _sentence(text: str) -> str:
    """Capitalise the first letter only (str.capitalize would lowercase Azure's own words)."""
    return text[:1].upper() + text[1:]


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
        """Answer every request. Returns the replies and the requests that failed for good, by key.

        If the last strategy raises (or the run is interrupted), the exception carries the replies that did
        arrive as ``.results`` and the requests that failed for good as ``.failures``.
        """
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
                    attach(exc, results=dict(results), failures=dict(failures))
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
                if not pending:
                    break
                continue
            except BaseException as exc:  # an interrupt: keep what was paid for
                attach(exc, results=dict(results), failures=dict(failures))
                raise

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
    batch_poll_interval: float = 60.0,
    batch_timeout: float | None = 24 * 3600.0,
    max_concurrent_batch_jobs: int = 4,
    batch_cleanup: bool = True,
    batch_cancel_wait: float = _CANCEL_GRACE_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> FallbackExecutor:
    """A FallbackExecutor with the named strategies, in the order given."""
    if isinstance(strategies, str):
        raise ConfigError(f"strategies must be a list of names such as {STRATEGIES}, not one string.")
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
            cancel_wait=batch_cancel_wait,
            sleep=sleep,
            clock=clock,
        ),
        "async": lambda: AsyncRunner(client, max_concurrency=max_concurrency),
        "sync": lambda: SyncRunner(client),
    }
    return FallbackExecutor([factories[name]() for name in strategies])
