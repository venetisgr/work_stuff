"""The fallback chain (FallbackExecutor), the async and loop strategies, run_coroutine and build_executor."""

from __future__ import annotations

import _thread
import asyncio
import contextlib
import logging
import signal
import threading
import time
import traceback

import pytest

from azure_mapreduce import progress as progress_module
from azure_mapreduce.errors import ConfigError, LLMRequestError, LLMSetupError
from azure_mapreduce.progress import StepProgress
from azure_mapreduce.runners import (
    STRATEGIES,
    AsyncRunner,
    BatchRunner,
    FallbackExecutor,
    LLMRequest,
    SyncRunner,
    _main_error,
    build_executor,
    chunked,
    run_coroutine,
)

from .conftest import FakeClient, content_of

# --- helpers ---------------------------------------------------------------------------------------------


def make_requests(count: int, start: int = 0) -> list[LLMRequest]:
    return [LLMRequest(key, [{"role": "user", "content": f"t{key}"}]) for key in range(start, start + count)]


class RecordingProgress(StepProgress):
    """A hidden bar that also remembers what the runners told it."""

    def __init__(self, total: int):
        super().__init__(total, "Test", unit="x", show=False)
        self.advanced = 0
        self.notes: list[str] = []
        self.strategies: list[str] = []

    def strategy(self, name: str) -> None:
        self.strategies.append(name)
        super().strategy(name)

    def advance(self, count: int = 1) -> None:
        self.advanced += count
        super().advance(count)

    def note(self, text: str) -> None:
        self.notes.append(text)
        super().note(text)


MISSING = object()  # a request the scripted strategy leaves out: no reply and no failure


class ScriptedRunner:
    """A strategy whose outcome per request key is scripted.

    ``outcomes[key]`` is a reply, an LLMRequestError, or MISSING; unscripted keys get ``"<name>:<key>"``. With
    ``raises``, the strategy raises it after answering ``raise_after`` of its requests.
    """

    def __init__(self, name, outcomes=None, *, available=True, raises=None, raise_after=0):
        self.name = name
        self.outcomes = dict(outcomes or {})
        self.is_available = available
        self.raises = raises
        self.raise_after = raise_after
        self.calls: list[list[int]] = []
        self.batch_sizes: list[int] = []

    def available(self) -> bool:
        return self.is_available

    def run(self, requests, results, progress, batch_size):
        self.calls.append([request.key for request in requests])
        self.batch_sizes.append(batch_size)
        failures = {}
        for index, request in enumerate(requests):
            if self.raises is not None and index == self.raise_after:
                raise self.raises
            outcome = self.outcomes.get(request.key, f"{self.name}:{request.key}")
            if isinstance(outcome, LLMRequestError):
                failures[request.key] = outcome
            elif outcome is not MISSING:
                results[request.key] = outcome
                progress.advance()
        if self.raises is not None:
            raise self.raises
        return failures


def retryable(message: str = "throttled") -> LLMRequestError:
    return LLMRequestError(message, retryable=True, code="rate_limit")


def permanent(message: str = "blocked") -> LLMRequestError:
    return LLMRequestError(message, retryable=False, code="content_filter")


def run_executor(runners, count, *, batch_size=100, start=0):
    progress = RecordingProgress(count)
    executor = FallbackExecutor(runners)
    results, failures = executor.run(make_requests(count, start), progress, batch_size=batch_size)
    return executor, progress, results, failures


class TimedAsyncClient:
    """An async-only client whose replies take real (short) time, to watch concurrency, chunks and cancellation.

    ``script[content] = (delay, error)``: wait ``delay`` seconds, then raise ``error`` if set. Unscripted prompts
    wait ``delay`` and reply ``"<content>"``.
    """

    supports_async = True

    def __init__(self, script=None, delay=0.005):
        self.script = dict(script or {})
        self.delay = delay
        self.in_flight = 0
        self.max_in_flight = 0
        self.events: list[tuple[str, str]] = []
        self.sessions = 0
        self.closed = 0

    @contextlib.asynccontextmanager
    async def async_session(self):
        self.sessions += 1

        async def complete(messages):
            text = content_of(messages)
            delay, error = self.script.get(text, (self.delay, None))
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            self.events.append(("start", text))
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                self.events.append(("cancelled", text))
                raise
            finally:
                self.in_flight -= 1
            if error is not None:
                self.events.append(("error", text))
                raise error
            self.events.append(("end", text))
            return f"<{text}>"

        try:
            yield complete
        finally:
            self.closed += 1

    def keys(self, kind: str) -> set[int]:
        return {int(text[1:]) for event, text in self.events if event == kind}


def request_error(code: str) -> LLMRequestError:
    """A failed request as the client reports it; codes "connection" and "credential" mean Azure wasn't reached."""
    return LLMRequestError(f"request failed ({code})", retryable=code != "content_filter", code=code)


def pattern_responder(pattern: list[str]):
    """Prompt ``tK`` gets ``pattern[K]``: "ok" replies "ok", anything else raises ``request_error`` with that code."""

    def responder(messages):
        outcome = pattern[int(content_of(messages)[1:])]
        if outcome == "ok":
            return "ok"
        raise request_error(outcome)

    return responder


def pattern_async_client(pattern: list[str], delays: list[float] | None = None) -> TimedAsyncClient:
    """A TimedAsyncClient where prompt ``tK`` takes ``delays[K]`` seconds and then does what ``pattern[K]`` says."""
    script = {}
    for key, outcome in enumerate(pattern):
        delay = delays[key] if delays else 0.001
        script[f"t{key}"] = (delay, None if outcome == "ok" else request_error(outcome))
    return TimedAsyncClient(script)


def wait_until(condition, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


def interrupted(call):
    """Run ``call()`` and interrupt the main thread (Ctrl+C, "Interrupt kernel") 0.15 s in.

    ``call`` must keep running until it is interrupted. Returns the pytest ExceptionInfo of the KeyboardInterrupt.
    """
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("interrupt_main() needs the test to run on the main thread")
    if signal.getsignal(signal.SIGINT) is not signal.default_int_handler:
        pytest.skip("something else handles SIGINT here")
    timer = threading.Timer(0.15, _thread.interrupt_main)
    timer.start()
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            call()
    finally:
        timer.cancel()
        timer.join()
    return caught


def interrupted_in_a_running_loop(cell):
    """Run the plain function ``cell`` while an event loop is running and interrupt it, as in a notebook.

    ``loop.run_until_complete`` (unlike ``asyncio.run``) installs no SIGINT handler of its own, so the interrupt
    raises KeyboardInterrupt right where ``cell`` is, as it does in a Jupyter kernel.
    """

    async def notebook():
        return cell()

    loop = asyncio.new_event_loop()
    try:
        return interrupted(lambda: loop.run_until_complete(notebook()))
    finally:
        loop.close()


class Ticker:
    """A coroutine that keeps sending (pretend) requests until it's cancelled, and records what happened."""

    def __init__(self, close_delay: float = 0.0):
        self.close_delay = close_delay
        self.calls = 0
        self.events: list[str] = []
        self.thread: int | None = None
        self.finished = threading.Event()

    async def run(self):
        self.thread = threading.get_ident()
        try:
            for _ in range(1000):  # about 10 s at most, in case the interrupt never comes
                self.calls += 1
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            self.events.append("cancelled")
            raise
        finally:
            await asyncio.sleep(self.close_delay)  # closing the async client
            self.events.append("closed")
            self.finished.set()


# --- FallbackExecutor: order and hand-offs ---------------------------------------------------------------


def test_executor_needs_at_least_one_strategy():
    with pytest.raises(ConfigError):
        FallbackExecutor([])


def test_first_strategy_answers_everything_and_the_rest_are_never_called():
    first, second = ScriptedRunner("a"), ScriptedRunner("b")
    executor, progress, results, failures = run_executor([first, second], 4, batch_size=3)
    assert results == {key: f"a:{key}" for key in range(4)}
    assert failures == {}
    assert first.calls == [[0, 1, 2, 3]]
    assert first.batch_sizes == [3]
    assert second.calls == []
    assert progress.strategies == ["a"]
    assert executor.broken == set()


def test_strategies_are_tried_in_the_given_order():
    first = ScriptedRunner("a", {key: retryable() for key in range(3)})
    second = ScriptedRunner("b", {key: retryable() for key in range(3)})
    third = ScriptedRunner("c")
    _, progress, results, failures = run_executor([first, second, third], 3)
    assert progress.strategies == ["a", "b", "c"]
    assert [first.calls, second.calls, third.calls] == [[[0, 1, 2]], [[0, 1, 2]], [[0, 1, 2]]]
    assert results == {0: "c:0", 1: "c:1", 2: "c:2"}
    assert failures == {}


def test_only_retryable_failures_and_missing_replies_go_to_the_next_strategy():
    first = ScriptedRunner("a", {1: retryable(), 3: MISSING, 4: permanent()})
    second = ScriptedRunner("b")
    _, progress, results, failures = run_executor([first, second], 5)
    assert second.calls == [[1, 3]]
    assert results == {0: "a:0", 1: "b:1", 2: "a:2", 3: "b:3"}
    assert list(failures) == [4]
    assert progress.strategies == ["a", "b"]


def test_non_retryable_failure_is_final_and_never_reaches_the_next_strategy():
    error = permanent("Azure's content filter blocked this text")
    first = ScriptedRunner("a", {0: error, 2: error})
    second = ScriptedRunner("b")
    _, progress, results, failures = run_executor([first, second], 3)
    assert failures == {0: error, 2: error}
    assert results == {1: "a:1"}
    assert second.calls == []  # nothing left to retry, so the next strategy isn't even started
    assert progress.strategies == ["a"]
    assert progress.failed == 2


def test_retryable_failures_in_the_last_strategy_become_final():
    error = retryable("Azure kept throttling requests (429)")
    first = ScriptedRunner("a", {0: retryable(), 1: retryable()})
    second = ScriptedRunner("b", {1: error})
    _, progress, results, failures = run_executor([first, second], 2)
    assert results == {0: "b:0"}
    assert failures == {1: error}
    assert failures[1].retryable  # kept as it was reported
    assert progress.failed == 1


def test_missing_reply_in_the_last_strategy_is_a_final_failure():
    only = ScriptedRunner("a", {1: MISSING})
    _, progress, results, failures = run_executor([only], 2)
    assert results == {0: "a:0"}
    assert list(failures) == [1]
    assert isinstance(failures[1], LLMRequestError)
    assert "No reply" in str(failures[1])
    assert progress.failed == 1


def test_a_reply_wins_over_a_failure_reported_for_the_same_request():
    class Contradictory(ScriptedRunner):
        def run(self, requests, results, progress, batch_size):
            super().run(requests, results, progress, batch_size)
            return {request.key: permanent() for request in requests}

    second = ScriptedRunner("b")
    _, progress, results, failures = run_executor([Contradictory("a"), second], 2)
    assert results == {0: "a:0", 1: "a:1"}
    assert failures == {}
    assert progress.failed == 0
    assert second.calls == []


def test_keys_need_not_start_at_zero_or_be_contiguous():
    requests = [LLMRequest(key, [{"role": "user", "content": f"t{key}"}]) for key in (7, 3, 42)]
    first = ScriptedRunner("a", {3: retryable()})
    second = ScriptedRunner("b")
    results, failures = FallbackExecutor([first, second]).run(requests, RecordingProgress(3), batch_size=1)
    assert results == {7: "a:7", 3: "b:3", 42: "a:42"}
    assert failures == {}
    assert second.calls == [[3]]


# --- FallbackExecutor: strategies that raise -------------------------------------------------------------


def test_raising_strategy_hands_everything_pending_to_the_next_and_keeps_its_replies(caplog):
    first = ScriptedRunner("a", raises=RuntimeError("upload rejected"), raise_after=2)
    second = ScriptedRunner("b")
    with caplog.at_level(logging.WARNING, logger="azure_mapreduce.runners"):
        executor, progress, results, failures = run_executor([first, second], 5)
    assert results == {0: "a:0", 1: "a:1", 2: "b:2", 3: "b:3", 4: "b:4"}
    assert failures == {}
    assert second.calls == [[2, 3, 4]]
    assert executor.broken == {"a"}
    assert progress.strategies == ["a", "b"]
    assert "a strategy failed" in caplog.text
    assert "upload rejected" in caplog.text
    assert "3 requests with b" in caplog.text


def test_setup_error_in_a_middle_strategy_falls_through_to_the_next():
    first = ScriptedRunner("a", {0: retryable(), 2: retryable()})
    second = ScriptedRunner("b", raises=LLMSetupError("no async client"))
    third = ScriptedRunner("c")
    executor, progress, results, failures = run_executor([first, second, third], 3)
    assert results == {0: "c:0", 1: "a:1", 2: "c:2"}
    assert failures == {}
    assert third.calls == [[0, 2]]
    assert executor.broken == {"b"}
    assert progress.strategies == ["a", "b", "c"]


def test_broken_strategy_is_skipped_on_the_next_run():
    first = ScriptedRunner("a", raises=LLMSetupError("the Batch API isn't enabled"))
    second = ScriptedRunner("b")
    executor = FallbackExecutor([first, second])
    executor.run(make_requests(2), RecordingProgress(2), batch_size=10)
    progress = RecordingProgress(3)
    results, failures = executor.run(make_requests(3, start=10), progress, batch_size=10)
    assert first.calls == [[0, 1]]  # not tried again
    assert second.calls == [[0, 1], [10, 11, 12]]
    assert progress.strategies == ["b"]
    assert results == {10: "b:10", 11: "b:11", 12: "b:12"}
    assert failures == {}


def test_broken_strategy_skipped_later_means_the_new_last_strategy_makes_failures_final():
    first = ScriptedRunner("a", raises=LLMSetupError("broken"))
    second = ScriptedRunner("b", {10: retryable()})
    executor = FallbackExecutor([first, second])
    executor.run(make_requests(1), RecordingProgress(1), batch_size=10)
    first.raises = None  # would work now, but it's out for the rest of the run
    progress = RecordingProgress(1)
    results, failures = executor.run(make_requests(1, start=10), progress, batch_size=10)
    assert results == {}
    assert list(failures) == [10]
    assert first.calls == [[0]]
    assert progress.failed == 1


def test_per_request_failures_do_not_mark_a_strategy_broken():
    first = ScriptedRunner("a", {0: retryable(), 1: permanent()})
    second = ScriptedRunner("b")
    executor = FallbackExecutor([first, second])
    executor.run(make_requests(3), RecordingProgress(3), batch_size=10)
    assert executor.broken == set()
    executor.run(make_requests(2, start=5), RecordingProgress(2), batch_size=10)
    assert first.calls == [[0, 1, 2], [5, 6]]


def test_last_strategy_raising_propagates():
    only = ScriptedRunner("a", raises=LLMSetupError("Azure rejected the credentials (401)"), raise_after=1)
    with pytest.raises(LLMSetupError, match="credentials"):
        run_executor([only], 3)


def test_last_strategy_raising_after_a_fallback_propagates_its_own_error():
    first = ScriptedRunner("a", raises=RuntimeError("batch broke"))
    second = ScriptedRunner("b", raises=LLMSetupError("No deployment named gpt-x"))
    executor = FallbackExecutor([first, second])
    with pytest.raises(LLMSetupError, match="gpt-x"):
        executor.run(make_requests(2), RecordingProgress(2), batch_size=10)
    assert executor.broken == {"a"}
    assert second.calls == [[0, 1]]


def test_last_strategy_raising_any_exception_propagates_unchanged():
    error = ValueError("unexpected")
    only = ScriptedRunner("a", raises=error)
    with pytest.raises(ValueError) as caught:
        run_executor([only], 1)
    assert caught.value is error


def test_keyboard_interrupt_is_not_treated_as_a_broken_strategy():
    first = ScriptedRunner("a", raises=KeyboardInterrupt())
    second = ScriptedRunner("b")
    executor = FallbackExecutor([first, second])
    with pytest.raises(KeyboardInterrupt):
        executor.run(make_requests(2), RecordingProgress(2), batch_size=10)
    assert second.calls == []
    assert executor.broken == set()


def test_strategy_raising_after_answering_everything_leaves_nothing_for_the_next():
    first = ScriptedRunner("a", raises=RuntimeError("cleanup failed"), raise_after=99)
    second = ScriptedRunner("b")
    executor, progress, results, failures = run_executor([first, second], 3)
    assert results == {0: "a:0", 1: "a:1", 2: "a:2"}
    assert failures == {}
    assert second.calls == []  # not even started: there was nothing left to answer
    assert progress.strategies == ["a"]
    assert executor.broken == {"a"}


def test_middle_strategy_raising_after_answering_everything_skips_every_later_strategy():
    first = ScriptedRunner("a", {0: retryable()})
    second = ScriptedRunner("b", raises=RuntimeError("cleanup failed"), raise_after=99)
    third = ScriptedRunner("c")
    executor, progress, results, failures = run_executor([first, second, third], 3)
    assert results == {0: "b:0", 1: "a:1", 2: "a:2"}
    assert failures == {}
    assert third.calls == []
    assert progress.strategies == ["a", "b"]
    assert progress.failed == 0
    assert executor.broken == {"b"}


# --- FallbackExecutor: replies carried by the exception of the last strategy ----------------------------


def test_last_strategy_raising_carries_the_replies_so_far():
    only = ScriptedRunner("a", raises=LLMSetupError("Azure rejected the credentials (401)"), raise_after=2)
    with pytest.raises(LLMSetupError) as caught:
        run_executor([only], 4)
    assert caught.value.results == {0: "a:0", 1: "a:1"}


def test_last_strategy_raising_carries_replies_from_every_strategy():
    first = ScriptedRunner("a", {1: retryable(), 2: retryable(), 3: MISSING})
    second = ScriptedRunner("b", raises=LLMSetupError("No deployment named gpt-x"), raise_after=1)
    with pytest.raises(LLMSetupError) as caught:
        run_executor([first, second], 5)
    assert second.calls == [[1, 2, 3]]
    assert caught.value.results == {0: "a:0", 4: "a:4", 1: "b:1"}


def test_last_strategy_raising_before_any_reply_carries_empty_results():
    first = ScriptedRunner("a", raises=RuntimeError("batch broke"))
    second = ScriptedRunner("b", raises=LLMSetupError("async broke"))
    with pytest.raises(LLMSetupError) as caught:
        run_executor([first, second], 2)
    assert caught.value.results == {}


def test_last_strategy_raising_any_exception_carries_the_replies_and_stays_the_same_object():
    error = ValueError("unexpected")
    only = ScriptedRunner("a", raises=error, raise_after=1)
    with pytest.raises(ValueError) as caught:
        run_executor([only], 3)
    assert caught.value is error
    assert error.results == {0: "a:0"}


def test_results_are_a_snapshot_of_the_replies_when_the_strategy_raised():
    class KeepsTheDict(ScriptedRunner):
        def run(self, requests, results, progress, batch_size):
            self.kept = results
            return super().run(requests, results, progress, batch_size)

    only = KeepsTheDict("a", raises=LLMSetupError("broken"), raise_after=1)
    with pytest.raises(LLMSetupError) as caught:
        run_executor([only], 2)
    only.kept[1] = "written late by a straggler"
    assert caught.value.results == {0: "a:0"}
    assert type(caught.value.results) is dict


def test_an_exception_that_refuses_the_results_attribute_still_propagates_unchanged():
    class ReadOnlyResults(Exception):
        @property
        def results(self):
            return "fixed"

    error = ReadOnlyResults("odd")
    only = ScriptedRunner("a", raises=error, raise_after=1)
    with pytest.raises(ReadOnlyResults) as caught:
        run_executor([only], 2)
    assert caught.value is error
    assert error.results == "fixed"


# --- FallbackExecutor: availability and edge cases -------------------------------------------------------


def test_unavailable_strategies_are_skipped_silently(caplog):
    first = ScriptedRunner("a", available=False)
    second = ScriptedRunner("b")
    with caplog.at_level(logging.DEBUG, logger="azure_mapreduce.runners"):
        executor, progress, results, _ = run_executor([first, second], 2)
    assert results == {0: "b:0", 1: "b:1"}
    assert first.calls == []
    assert progress.strategies == ["b"]
    assert executor.broken == set()
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


def test_unavailable_strategy_is_not_marked_broken_and_is_used_once_available():
    first = ScriptedRunner("a", available=False)
    second = ScriptedRunner("b")
    executor = FallbackExecutor([first, second])
    executor.run(make_requests(1), RecordingProgress(1), batch_size=10)
    first.is_available = True
    results, _ = executor.run(make_requests(1, start=1), RecordingProgress(1), batch_size=10)
    assert results == {1: "a:1"}
    assert first.calls == [[1]]


def test_unavailable_last_strategy_makes_the_previous_one_last():
    """Retryable failures become final, and a raise propagates, in the last strategy that can actually run."""
    first = ScriptedRunner("a", {0: retryable()})
    second = ScriptedRunner("b", available=False)
    _, progress, results, failures = run_executor([first, second], 2)
    assert results == {1: "a:1"}
    assert list(failures) == [0]
    assert progress.failed == 1

    raising = ScriptedRunner("a", raises=LLMSetupError("bad key"))
    with pytest.raises(LLMSetupError):
        run_executor([raising, ScriptedRunner("b", available=False)], 2)


def test_config_error_when_no_strategy_is_available():
    runners = [ScriptedRunner("batch", available=False), ScriptedRunner("sync", available=False)]
    with pytest.raises(ConfigError, match="batch, sync"):
        run_executor(runners, 1)
    assert all(runner.calls == [] for runner in runners)


def test_empty_request_list_returns_nothing_and_starts_no_strategy():
    first = ScriptedRunner("a")
    progress = RecordingProgress(0)
    results, failures = FallbackExecutor([first]).run([], progress, batch_size=10)
    assert (results, failures) == ({}, {})
    assert first.calls == []
    assert progress.strategies == []
    assert progress.advanced == progress.failed == 0


# --- FallbackExecutor: progress accounting ---------------------------------------------------------------


def test_progress_counts_every_request_once_including_final_failures():
    first = ScriptedRunner("a", {0: retryable(), 1: permanent(), 2: MISSING, 3: retryable()})
    second = ScriptedRunner("b", raises=LLMSetupError("async is down"), raise_after=1)
    third = ScriptedRunner("c", {3: retryable()})
    _, progress, results, failures = run_executor([first, second, third], 6)
    # a: 4, 5 answered; 1 final; 0, 2, 3 go on. b: 0 answered, then breaks. c: 2 answered, 3 fails for good.
    assert results == {0: "b:0", 2: "c:2", 4: "a:4", 5: "a:5"}
    assert sorted(failures) == [1, 3]
    assert progress.failed == 2
    assert progress.advanced == 4
    assert progress.advanced + progress.failed == progress.total


def test_failed_counter_matches_the_final_failures():
    errors = {key: permanent() for key in range(0, 10, 3)}
    _, progress, results, failures = run_executor([ScriptedRunner("a", errors)], 10)
    assert progress.failed == len(failures) == 4
    assert progress.advanced == len(results) == 6


def test_visible_progress_bar_reaches_the_total(capsys):
    progress = StepProgress(4, "Map", unit="record", show=True)
    first = ScriptedRunner("a", {0: retryable(), 3: permanent()})
    FallbackExecutor([first, ScriptedRunner("b", {0: retryable()})]).run(make_requests(4), progress, batch_size=2)
    assert progress.done == 4
    assert progress.failed == 2
    progress.close()
    assert "failed=2" in capsys.readouterr().err


def test_hidden_progress_bar_still_counts():
    progress = StepProgress(3, "Map", unit="record", show=False)
    FallbackExecutor([ScriptedRunner("a", {2: permanent()})]).run(make_requests(3), progress, batch_size=10)
    assert progress.failed == 1
    assert progress.done == 3


# --- StepProgress ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("show", [False, True])
def test_step_progress_counts_the_same_whether_shown_or_hidden(show):
    progress = StepProgress(10, "Map", unit="record", show=show)
    assert (progress.total, progress.done, progress.failed) == (10, 0, 0)
    progress.strategy("batch")
    progress.advance(4)  # a running batch job's estimate
    progress.note("jobs 0/1 done")
    progress.advance(-1)  # ...taken back when fewer replies came back
    progress.advance(0)
    progress.fail(2)
    progress.advance()
    assert progress.done == 6
    assert progress.failed == 2
    assert progress.total == 10
    progress.close()


def test_step_progress_total_is_what_it_was_given():
    assert StepProgress(0, "Reduce", unit="group", show=False).total == 0
    assert StepProgress(12, "Map", unit="record", show=False).total == 12


def test_hidden_step_progress_counts_updates_from_many_threads():
    progress = StepProgress(8 * 500, "Map", unit="record", show=False)

    def work():
        for _ in range(500):
            progress.advance()

    threads = [threading.Thread(target=work) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert progress.done == 8 * 500


def test_step_progress_is_a_context_manager_and_null_progress_is_gone():
    with StepProgress(2, "Map", unit="record", show=False) as progress:
        progress.advance(2)
    assert progress.done == 2
    assert not hasattr(progress_module, "NullProgress")


# --- SyncRunner ------------------------------------------------------------------------------------------


def test_sync_runner_availability_follows_the_standard_deployment():
    assert SyncRunner(FakeClient()).available()
    assert not SyncRunner(FakeClient(deployment=None)).available()
    assert SyncRunner(FakeClient(batch_deployment=None)).available()


def test_sync_runner_answers_in_order_and_counts_progress():
    client = FakeClient()
    progress = RecordingProgress(3)
    results: dict[int, str] = {}
    failures = SyncRunner(client).run(make_requests(3), results, progress, batch_size=10)
    assert results == {0: "<t0>", 1: "<t1>", 2: "<t2>"}
    assert failures == {}
    assert [content_of(messages) for messages in client.calls["sync"]] == ["t0", "t1", "t2"]
    assert progress.advanced == 3
    assert progress.notes == []  # a single chunk isn't worth mentioning


def test_sync_runner_returns_request_failures_without_counting_them():
    error = permanent()

    def responder(messages):
        if content_of(messages) == "t1":
            raise error
        return "fine"

    progress = RecordingProgress(3)
    results: dict[int, str] = {}
    failures = SyncRunner(FakeClient(sync_responder=responder)).run(make_requests(3), results, progress, 10)
    assert results == {0: "fine", 2: "fine"}
    assert failures == {1: error}
    assert progress.advanced == 2
    assert progress.failed == 0  # the executor decides whether the failure is final


def test_sync_runner_notes_each_chunk():
    progress = RecordingProgress(5)
    SyncRunner(FakeClient()).run(make_requests(5), {}, progress, batch_size=2)
    assert progress.notes == ["chunk 1/3", "chunk 2/3", "chunk 3/3"]


def test_sync_runner_setup_error_stops_the_loop_and_keeps_earlier_replies():
    def responder(messages):
        if content_of(messages) == "t2":
            raise LLMSetupError("Azure rejected the credentials (401)")
        return "fine"

    client = FakeClient(sync_responder=responder)
    results: dict[int, str] = {}
    with pytest.raises(LLMSetupError):
        SyncRunner(client).run(make_requests(5), results, RecordingProgress(5), batch_size=10)
    assert results == {0: "fine", 1: "fine"}
    assert len(client.calls["sync"]) == 3  # nothing sent after the setup error


def test_sync_runner_keeps_the_cause_of_a_translated_error():
    def responder(messages):
        try:
            raise ConnectionRefusedError("the SDK's own error")
        except ConnectionRefusedError as exc:
            raise LLMSetupError("Azure rejected the credentials (401: bad key)") from exc

    with pytest.raises(LLMSetupError) as caught:
        SyncRunner(FakeClient(sync_responder=responder)).run(make_requests(1), {}, RecordingProgress(1), 10)
    assert isinstance(caught.value.__cause__, ConnectionRefusedError)


@pytest.mark.parametrize("batch_size", [0, -1])
def test_sync_runner_rejects_a_batch_size_below_one(batch_size):
    client = FakeClient()
    with pytest.raises(ConfigError, match="batch size must be at least 1"):
        SyncRunner(client).run(make_requests(2), {}, RecordingProgress(2), batch_size=batch_size)
    assert client.calls["sync"] == []


# --- chunked ---------------------------------------------------------------------------------------------


def test_chunked_splits_into_pieces_of_at_most_size():
    assert chunked(list(range(5)), 2) == [[0, 1], [2, 3], [4]]
    assert chunked(list(range(4)), 4) == [[0, 1, 2, 3]]
    assert chunked(list(range(3)), 10) == [[0, 1, 2]]
    assert chunked(list(range(3)), 1) == [[0], [1], [2]]
    assert chunked([], 3) == []


@pytest.mark.parametrize("size", [0, -1, -100])
def test_chunked_rejects_sizes_below_one(size):
    with pytest.raises(ConfigError, match=rf"at least 1 \(got {size}\)"):
        chunked([1, 2, 3], size)
    with pytest.raises(ConfigError):
        chunked([], size)  # even with nothing to split


# --- SyncRunner: stopping when Azure can't be reached ----------------------------------------------------


@pytest.mark.parametrize("code", ["connection", "credential"])
def test_sync_runner_stops_after_three_unreachable_requests_before_any_success(code):
    client = FakeClient(sync_responder=pattern_responder([code] * 6))
    results: dict[int, str] = {}
    with pytest.raises(LLMSetupError) as caught:
        SyncRunner(client).run(make_requests(6), results, RecordingProgress(6), batch_size=10)
    assert str(caught.value) == f"3 requests in a row couldn't reach Azure. The last error: request failed ({code})"
    assert len(client.calls["sync"]) == 3  # nothing sent after the third
    assert results == {}


def test_sync_runner_counts_connection_and_credential_failures_in_one_streak():
    client = FakeClient(sync_responder=pattern_responder(["credential", "connection", "credential", "ok"]))
    with pytest.raises(LLMSetupError, match=r"^3 requests in a row .* request failed \(credential\)$"):
        SyncRunner(client).run(make_requests(4), {}, RecordingProgress(4), batch_size=10)
    assert len(client.calls["sync"]) == 3


def test_sync_runner_below_the_limit_just_returns_the_failures():
    client = FakeClient(sync_responder=pattern_responder(["connection", "connection", "ok"]))
    results: dict[int, str] = {}
    failures = SyncRunner(client).run(make_requests(3), results, RecordingProgress(3), batch_size=10)
    assert results == {2: "ok"}
    assert sorted(failures) == [0, 1]
    assert all(error.code == "connection" and error.retryable for error in failures.values())


def test_sync_runner_allows_ten_unreachable_requests_in_a_row_after_a_success():
    pattern = ["ok"] + ["connection"] * 9 + ["ok"] + ["credential"] * 10 + ["ok"] * 3
    client = FakeClient(sync_responder=pattern_responder(pattern))
    results: dict[int, str] = {}
    with pytest.raises(LLMSetupError) as caught:
        SyncRunner(client).run(make_requests(len(pattern)), results, RecordingProgress(len(pattern)), 100)
    assert str(caught.value) == "10 requests in a row couldn't reach Azure. The last error: request failed (credential)"
    assert len(client.calls["sync"]) == 21  # 9 in a row were fine; the 10th of the second run stopped it
    assert results == {0: "ok", 10: "ok"}


def test_sync_runner_every_success_starts_the_streak_over():
    pattern = ["ok"] + (["connection"] * 9 + ["ok"]) * 3
    client = FakeClient(sync_responder=pattern_responder(pattern))
    results: dict[int, str] = {}
    failures = SyncRunner(client).run(make_requests(len(pattern)), results, RecordingProgress(len(pattern)), 100)
    assert sorted(results) == [0, 10, 20, 30]
    assert len(failures) == 27


@pytest.mark.parametrize("other", ["rate_limit", "timeout", "content_filter", "batch"])
def test_sync_runner_other_failures_reset_the_streak(other):
    """Any other failure means Azure answered, so a run of unreachable requests starts over."""
    pattern = ["connection", "connection", other] * 3 + ["connection", "connection"]
    client = FakeClient(sync_responder=pattern_responder(pattern))
    failures = SyncRunner(client).run(make_requests(len(pattern)), {}, RecordingProgress(len(pattern)), 100)
    assert sorted(failures) == list(range(len(pattern)))
    assert failures[2].code == other


def test_sync_runner_a_failure_without_a_code_resets_the_streak():
    def responder(messages):
        if content_of(messages) == "t2":
            raise LLMRequestError("The model returned an empty reply.", retryable=True)  # no code
        raise request_error("connection")

    failures = SyncRunner(FakeClient(sync_responder=responder)).run(make_requests(4), {}, RecordingProgress(4), 10)
    assert sorted(failures) == [0, 1, 2, 3]


def test_sync_runner_other_failures_do_not_lift_the_limit_to_ten():
    """Only a reply shows the deployment works; before one, three unreachable requests in a row stop the run."""
    pattern = ["rate_limit", "content_filter"] + ["connection"] * 3 + ["ok"]
    client = FakeClient(sync_responder=pattern_responder(pattern))
    with pytest.raises(LLMSetupError, match=r"^3 requests in a row"):
        SyncRunner(client).run(make_requests(len(pattern)), {}, RecordingProgress(len(pattern)), 100)
    assert len(client.calls["sync"]) == 5


def test_sync_runner_streak_carries_across_chunks():
    client = FakeClient(sync_responder=pattern_responder(["connection"] * 3 + ["ok"] * 3))
    progress = RecordingProgress(6)
    with pytest.raises(LLMSetupError):
        SyncRunner(client).run(make_requests(6), {}, progress, batch_size=2)
    assert len(client.calls["sync"]) == 3
    assert progress.notes == ["chunk 1/3", "chunk 2/3"]


def test_unreachable_last_strategy_raises_a_setup_error_carrying_the_replies():
    pattern = ["ok", "ok"] + ["connection"] * 10 + ["ok"] * 3
    client = FakeClient(batch_deployment=None, sync_responder=pattern_responder(pattern))
    executor = build_executor(client, ("sync",))
    with pytest.raises(LLMSetupError, match=r"^10 requests in a row") as caught:
        executor.run(make_requests(len(pattern)), RecordingProgress(len(pattern)), batch_size=100)
    assert caught.value.results == {0: "ok", 1: "ok"}
    assert len(client.calls["sync"]) == 12


# --- AsyncRunner -----------------------------------------------------------------------------------------


def test_async_runner_availability():
    assert AsyncRunner(FakeClient(), max_concurrency=2).available()
    assert not AsyncRunner(FakeClient(async_ok=False), max_concurrency=2).available()
    assert not AsyncRunner(FakeClient(deployment=None), max_concurrency=2).available()


def test_async_runner_answers_everything_in_one_session():
    client = FakeClient()
    progress = RecordingProgress(7)
    results: dict[int, str] = {}
    failures = AsyncRunner(client, max_concurrency=3).run(make_requests(7), results, progress, batch_size=3)
    assert results == {key: f"<t{key}>" for key in range(7)}
    assert failures == {}
    assert client.async_sessions == 1  # one client for all the chunks
    assert progress.advanced == 7
    assert progress.notes == ["chunk 1/3", "chunk 2/3", "chunk 3/3"]


def test_async_runner_single_chunk_has_no_chunk_note():
    progress = RecordingProgress(3)
    AsyncRunner(FakeClient(), max_concurrency=3).run(make_requests(3), {}, progress, batch_size=3)
    assert progress.notes == []


def test_async_runner_request_failures_do_not_stop_the_others():
    error = retryable()

    def responder(messages):
        if content_of(messages) in ("t1", "t4"):
            raise error
        return "fine"

    progress = RecordingProgress(6)
    results: dict[int, str] = {}
    failures = AsyncRunner(FakeClient(async_responder=responder), max_concurrency=2).run(
        make_requests(6), results, progress, batch_size=4
    )
    assert results == {0: "fine", 2: "fine", 3: "fine", 5: "fine"}
    assert failures == {1: error, 4: error}
    assert progress.advanced == 4


@pytest.mark.parametrize("max_concurrency", [1, 3, 8])
def test_async_runner_respects_max_concurrency(max_concurrency):
    client = TimedAsyncClient()
    results: dict[int, str] = {}
    AsyncRunner(client, max_concurrency=max_concurrency).run(make_requests(20), results, RecordingProgress(20), 100)
    assert len(results) == 20
    assert client.max_in_flight == max_concurrency


def test_async_runner_never_has_more_in_flight_than_a_chunk():
    client = TimedAsyncClient()
    results: dict[int, str] = {}
    AsyncRunner(client, max_concurrency=50).run(make_requests(12), results, RecordingProgress(12), batch_size=4)
    assert len(results) == 12
    assert client.max_in_flight == 4


def test_async_runner_finishes_each_chunk_before_starting_the_next():
    # Early requests are slow, so without chunking later ones would start (and end) before them.
    script = {f"t{key}": (0.03 if key % 3 == 0 else 0.001, None) for key in range(9)}
    client = TimedAsyncClient(script)
    AsyncRunner(client, max_concurrency=10).run(make_requests(9), {}, RecordingProgress(9), batch_size=3)
    position = {(event, text): index for index, (event, text) in enumerate(client.events)}
    for chunk in range(2):
        this_chunk = [f"t{key}" for key in range(chunk * 3, chunk * 3 + 3)]
        next_chunk = [f"t{key}" for key in range(chunk * 3 + 3, chunk * 3 + 6)]
        last_end = max(position[("end", text)] for text in this_chunk)
        first_start = min(position[("start", text)] for text in next_chunk)
        assert last_end < first_start


def test_async_runner_setup_error_cancels_the_rest_and_propagates():
    script = {
        "t0": (0.02, LLMSetupError("No deployment named gpt-x")),
        "t1": (0.0, None),
        "t2": (0.0, None),
        "t3": (10.0, None),
        "t4": (10.0, None),
    }
    client = TimedAsyncClient(script)
    results: dict[int, str] = {}
    progress = RecordingProgress(8)
    started = time.monotonic()
    with pytest.raises(LLMSetupError, match="gpt-x"):
        AsyncRunner(client, max_concurrency=10).run(make_requests(8), results, progress, batch_size=5)
    assert time.monotonic() - started < 5  # the slow requests were cancelled, not waited for
    assert client.keys("cancelled") == {3, 4}
    assert results == {1: "<t1>", 2: "<t2>"}  # replies that came back first are kept
    assert progress.advanced == 2
    assert client.keys("start") == {0, 1, 2, 3, 4}  # the second chunk never started
    assert client.closed == 1  # the session was still closed


def test_async_runner_setup_error_when_opening_the_session_propagates():
    """A strategy the executor wasn't told is unavailable still fails cleanly when its client won't start."""
    client = FakeClient(async_ok=False)
    with pytest.raises(LLMSetupError):
        AsyncRunner(client, max_concurrency=2).run(make_requests(2), {}, RecordingProgress(2), batch_size=10)
    assert client.calls["async"] == []


def test_async_runner_unexpected_error_propagates_as_itself():
    def responder(messages):
        raise RuntimeError("unexpected response shape")

    with pytest.raises(RuntimeError, match="unexpected response shape"):
        AsyncRunner(FakeClient(async_responder=responder), max_concurrency=2).run(
            make_requests(2), {}, RecordingProgress(2), batch_size=10
        )


def test_async_runner_works_inside_a_running_event_loop():
    """As in a Jupyter or Databricks cell, where an event loop is already running."""
    client = FakeClient()
    progress = RecordingProgress(5)

    async def notebook_cell():
        results: dict[int, str] = {}
        failures = AsyncRunner(client, max_concurrency=2).run(make_requests(5), results, progress, batch_size=2)
        return results, failures

    results, failures = asyncio.run(notebook_cell())
    assert results == {key: f"<t{key}>" for key in range(5)}
    assert failures == {}
    assert progress.advanced == 5


def test_async_runner_setup_error_propagates_from_inside_a_running_event_loop():
    def responder(messages):
        raise LLMSetupError("Azure rejected the credentials (401)")

    async def notebook_cell():
        AsyncRunner(FakeClient(async_responder=responder), max_concurrency=2).run(
            make_requests(2), {}, RecordingProgress(2), batch_size=10
        )

    with pytest.raises(LLMSetupError, match="credentials"):
        asyncio.run(notebook_cell())


@pytest.mark.parametrize("max_concurrency", [0, -1, 2.5, None, "4"])
def test_async_runner_rejects_a_max_concurrency_below_one_or_not_an_int(max_concurrency):
    with pytest.raises(ConfigError, match=rf"max_concurrency must be at least 1 \(got {max_concurrency!r}\)"):
        AsyncRunner(FakeClient(), max_concurrency=max_concurrency)


def test_async_runner_accepts_a_max_concurrency_of_one():
    results: dict[int, str] = {}
    AsyncRunner(FakeClient(), max_concurrency=1).run(make_requests(3), results, RecordingProgress(3), 10)
    assert len(results) == 3


def test_build_executor_validates_max_concurrency_up_front():
    with pytest.raises(ConfigError, match="max_concurrency"):
        build_executor(FakeClient(), max_concurrency=0)


@pytest.mark.parametrize("batch_size", [0, -3])
def test_async_runner_rejects_a_batch_size_below_one_before_opening_a_session(batch_size):
    client = FakeClient()
    with pytest.raises(ConfigError, match="batch size must be at least 1"):
        AsyncRunner(client, max_concurrency=2).run(make_requests(2), {}, RecordingProgress(2), batch_size=batch_size)
    assert client.async_sessions == 0
    assert client.calls["async"] == []


# --- AsyncRunner: unexpected errors ----------------------------------------------------------------------


def test_async_runner_unexpected_error_cancels_the_rest_of_its_chunk_and_keeps_earlier_replies():
    script = {"t2": (0.001, RuntimeError("unexpected response shape")), "t3": (10.0, None)}
    client = TimedAsyncClient(script)
    results: dict[int, str] = {}
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="unexpected response shape") as caught:
        AsyncRunner(client, max_concurrency=4).run(make_requests(6), results, RecordingProgress(6), batch_size=2)
    assert not isinstance(caught.value, BaseExceptionGroup)
    assert time.monotonic() - started < 5
    assert results == {0: "<t0>", 1: "<t1>"}
    assert client.keys("cancelled") == {3}
    assert client.keys("start") == {0, 1, 2, 3}  # the third chunk never started
    assert client.closed == 1


@pytest.mark.parametrize("setup_first", [True, False])
def test_async_runner_prefers_a_setup_error_over_an_unexpected_one_in_the_same_chunk(setup_first):
    # FakeClient's calls never wait, so both errors are raised before the TaskGroup can cancel anything.
    setup, unexpected = "t0" if setup_first else "t1", "t1" if setup_first else "t0"

    def responder(messages):
        if content_of(messages) == setup:
            raise LLMSetupError("No deployment named gpt-x")
        if content_of(messages) == unexpected:
            raise RuntimeError("unexpected response shape")
        return "fine"

    with pytest.raises(LLMSetupError, match="gpt-x"):
        AsyncRunner(FakeClient(async_responder=responder), max_concurrency=4).run(
            make_requests(3), {}, RecordingProgress(3), batch_size=10
        )


def test_async_runner_raises_the_leaf_of_an_error_group_from_a_request():
    def responder(messages):  # as when the client itself runs a task group
        raise ExceptionGroup("outer", [ExceptionGroup("inner", [ValueError("deep down")])])

    with pytest.raises(ValueError, match="deep down") as caught:
        AsyncRunner(FakeClient(async_responder=responder), max_concurrency=2).run(
            make_requests(1), {}, RecordingProgress(1), batch_size=10
        )
    assert not isinstance(caught.value, BaseExceptionGroup)


def test_async_runner_finds_a_setup_error_nested_in_an_error_group():
    def responder(messages):
        raise ExceptionGroup(
            "outer", [RuntimeError("first"), ExceptionGroup("inner", [KeyError("k"), LLMSetupError("bad key")])]
        )

    with pytest.raises(LLMSetupError, match="bad key"):
        AsyncRunner(FakeClient(async_responder=responder), max_concurrency=2).run(
            make_requests(1), {}, RecordingProgress(1), batch_size=10
        )


def test_main_error_picks_a_setup_error_then_the_first_real_error_then_a_cancellation():
    setup = LLMSetupError("setup")
    first = ValueError("first")
    cancelled = asyncio.CancelledError()
    nested = BaseExceptionGroup("g", [cancelled, ExceptionGroup("h", [first, RuntimeError("second")])])
    assert _main_error(nested) is first
    assert _main_error(BaseExceptionGroup("g", [first, BaseExceptionGroup("h", [cancelled, setup])])) is setup
    assert _main_error(BaseExceptionGroup("g", [cancelled])) is cancelled


def test_unexpected_async_error_falls_back_to_sync_for_what_is_left():
    def async_responder(messages):
        if content_of(messages) == "t1":
            raise RuntimeError("unexpected response shape")
        return "async"

    client = FakeClient(batch_deployment=None, async_responder=async_responder, sync_responder=lambda m: "sync")
    executor = build_executor(client, ("async", "sync"))
    results, failures = executor.run(make_requests(4), RecordingProgress(4), batch_size=10)
    assert failures == {}
    assert sorted(results) == [0, 1, 2, 3]
    assert results[1] == "sync"
    assert len(client.calls["sync"]) == list(results.values()).count("sync")
    assert executor.broken == {"async"}


@pytest.mark.xfail(
    strict=True,
    reason="BUG: AsyncRunner re-raises the TaskGroup's error with `raise ... from None`, which wipes its "
    "__cause__ (the SDK error an LLMSetupError was translated from); SyncRunner keeps it",
)
def test_async_runner_keeps_the_cause_of_a_translated_error():
    def responder(messages):
        try:
            raise ConnectionRefusedError("the SDK's own error")
        except ConnectionRefusedError as exc:
            raise LLMSetupError("Azure rejected the credentials (401: bad key)") from exc

    with pytest.raises(LLMSetupError) as caught:
        AsyncRunner(FakeClient(async_responder=responder), max_concurrency=2).run(
            make_requests(1), {}, RecordingProgress(1), batch_size=10
        )
    assert isinstance(caught.value.__cause__, ConnectionRefusedError)


# --- AsyncRunner: stopping when Azure can't be reached ---------------------------------------------------


def test_async_runner_stops_after_three_unreachable_requests_and_cancels_those_in_flight():
    pattern = ["connection", "credential", "connection", "ok", "ok", "ok"]
    client = pattern_async_client(pattern, delays=[0.01, 0.02, 0.03, 10.0, 10.0, 10.0])
    results: dict[int, str] = {}
    started = time.monotonic()
    with pytest.raises(LLMSetupError) as caught:
        AsyncRunner(client, max_concurrency=10).run(make_requests(6), results, RecordingProgress(6), batch_size=10)
    assert str(caught.value) == "3 requests in a row couldn't reach Azure. The last error: request failed (connection)"
    assert time.monotonic() - started < 5
    assert client.keys("error") == {0, 1, 2}
    assert client.keys("cancelled") == {3, 4, 5}
    assert results == {}
    assert client.closed == 1


@pytest.mark.parametrize("code", ["connection", "credential"])
def test_async_runner_allows_ten_unreachable_requests_in_a_row_after_a_success(code):
    pattern = ["ok"] + [code] * 10 + ["ok"] * 2
    client = pattern_async_client(pattern)
    results: dict[int, str] = {}
    with pytest.raises(LLMSetupError, match=rf"^10 requests in a row couldn't reach Azure\. .*\({code}\)$"):
        AsyncRunner(client, max_concurrency=1).run(make_requests(13), results, RecordingProgress(13), 100)
    assert client.keys("error") == set(range(1, 11))
    assert results == {0: "<t0>"}
    assert client.keys("end") == {0}


def test_async_runner_below_the_limits_and_with_resets_returns_the_failures():
    pattern = ["connection", "credential", "rate_limit", "connection", "connection", "ok"]
    pattern += ["connection"] * 9 + ["ok"] + ["timeout", "connection"]
    client = pattern_async_client(pattern)
    results: dict[int, str] = {}
    failures = AsyncRunner(client, max_concurrency=1).run(
        make_requests(len(pattern)), results, RecordingProgress(len(pattern)), 100
    )
    assert results == {5: "<t5>", 15: "<t15>"}
    assert sorted(failures) == [key for key, outcome in enumerate(pattern) if outcome != "ok"]


def test_async_runner_other_failures_do_not_lift_the_limit_to_ten():
    pattern = ["rate_limit"] + ["connection"] * 3 + ["ok"]
    client = pattern_async_client(pattern)
    with pytest.raises(LLMSetupError, match=r"^3 requests in a row"):
        AsyncRunner(client, max_concurrency=1).run(make_requests(5), {}, RecordingProgress(5), 100)
    assert client.keys("error") == {0, 1, 2, 3}


def test_async_runner_streak_carries_across_chunks():
    pattern = ["connection", "connection", "connection", "ok", "ok", "ok"]
    client = pattern_async_client(pattern, delays=[0.001, 0.001, 0.001, 10.0, 0.001, 0.001])
    progress = RecordingProgress(6)
    with pytest.raises(LLMSetupError, match=r"^3 requests in a row"):
        AsyncRunner(client, max_concurrency=4).run(make_requests(6), {}, progress, batch_size=2)
    assert client.keys("start") == {0, 1, 2, 3}
    assert client.keys("cancelled") == {3}
    assert progress.notes == ["chunk 1/3", "chunk 2/3"]


def test_unreachable_async_strategy_hands_everything_to_sync():
    client = FakeClient(
        batch_deployment=None,
        async_responder=pattern_responder(["connection"] * 5),
        sync_responder=lambda messages: "sync",
    )
    executor = build_executor(client, ("async", "sync"))
    progress = RecordingProgress(5)
    results, failures = executor.run(make_requests(5), progress, batch_size=10)
    assert results == {key: "sync" for key in range(5)}
    assert failures == {}
    assert executor.broken == {"async"}
    assert progress.strategies == ["async", "sync"]


def test_a_few_unreachable_async_requests_are_retried_by_sync_without_breaking_async():
    pattern = ["ok", "connection", "ok", "connection", "connection", "ok"]
    client = FakeClient(
        batch_deployment=None, async_responder=pattern_responder(pattern), sync_responder=lambda m: "sync"
    )
    executor = build_executor(client, ("async", "sync"))
    results, failures = executor.run(make_requests(6), RecordingProgress(6), batch_size=10)
    assert results == {0: "ok", 1: "sync", 2: "ok", 3: "sync", 4: "sync", 5: "ok"}
    assert failures == {}
    assert executor.broken == set()


# --- run_coroutine ---------------------------------------------------------------------------------------


async def _where_am_i(value):
    await asyncio.sleep(0)
    return value, threading.get_ident(), asyncio.get_running_loop()


def test_run_coroutine_outside_an_event_loop_runs_here():
    value, thread, _ = run_coroutine(_where_am_i(42))
    assert value == 42
    assert thread == threading.get_ident()


def test_run_coroutine_inside_a_running_event_loop_uses_a_loop_of_its_own():
    async def outer():
        outer_loop = asyncio.get_running_loop()
        value, thread, inner_loop = run_coroutine(_where_am_i("inner"))
        await asyncio.sleep(0)  # the outer loop still works afterwards
        return outer_loop, value, thread, inner_loop

    outer_loop, value, thread, inner_loop = asyncio.run(outer())
    assert value == "inner"
    assert thread != threading.get_ident()
    assert inner_loop is not outer_loop


def test_run_coroutine_propagates_exceptions_both_ways():
    async def fails():
        await asyncio.sleep(0)
        raise LLMSetupError("nope")

    with pytest.raises(LLMSetupError, match="nope"):
        run_coroutine(fails())

    async def outer():
        return run_coroutine(fails())

    with pytest.raises(LLMSetupError, match="nope"):
        asyncio.run(outer())


def test_run_coroutine_can_be_called_repeatedly_inside_one_loop():
    async def outer():
        return [run_coroutine(_where_am_i(index))[0] for index in range(3)]

    assert asyncio.run(outer()) == [0, 1, 2]


def test_run_coroutine_inside_a_running_event_loop_leaves_no_worker_thread_behind():
    async def outer():
        return run_coroutine(_where_am_i("inner"))[1]

    worker = asyncio.run(outer())
    assert all(thread.ident != worker for thread in threading.enumerate())


@pytest.mark.xfail(
    strict=True,
    reason="BUG: run_coroutine calls asyncio.run() inside its `except RuntimeError:` block, so every error from "
    "the coroutine gets __context__ = RuntimeError('no running event loop') and tracebacks show a misleading "
    "'During handling of the above exception, another exception occurred'",
)
def test_run_coroutine_errors_do_not_carry_the_no_running_loop_probe():
    async def fails():
        await asyncio.sleep(0)
        raise LLMSetupError("No async client is available")

    with pytest.raises(LLMSetupError) as caught:
        run_coroutine(fails())
    assert caught.value.__context__ is None
    assert "no running event loop" not in "".join(traceback.format_exception(caught.value))


# --- run_coroutine: interrupts ---------------------------------------------------------------------------


def test_interrupting_run_coroutine_outside_an_event_loop_cancels_the_coroutine_first():
    ticker = Ticker(close_delay=0.2)
    seen_at_interrupt: list[str] = []

    def script():
        try:
            return run_coroutine(ticker.run())
        except KeyboardInterrupt:
            seen_at_interrupt.extend(ticker.events)
            raise

    interrupted(script)
    assert ticker.thread == threading.get_ident()
    assert seen_at_interrupt == ["cancelled", "closed"]


def test_interrupting_run_coroutine_inside_a_running_loop_cancels_the_coroutine():
    ticker = Ticker()

    def cell():  # plain code in a notebook cell: map() calling run_coroutine, with the kernel's loop running
        return run_coroutine(ticker.run())

    interrupted_in_a_running_loop(cell)  # KeyboardInterrupt reached the caller
    assert ticker.finished.wait(5), "the coroutine was left running after the interrupt"
    assert ticker.events == ["cancelled", "closed"]
    assert ticker.thread != threading.get_ident()
    calls = ticker.calls
    time.sleep(0.1)
    assert ticker.calls == calls  # no more requests
    assert wait_until(lambda: all(thread.ident != ticker.thread for thread in threading.enumerate()))


@pytest.mark.xfail(
    strict=True,
    reason="BUG: run_coroutine re-raises the interrupt before the coroutine is cancelled: on Python 3.11 a "
    "Thread.join() interrupted by KeyboardInterrupt marks the still-running worker as stopped, so the "
    "worker.join(10) meant to let the cancellation finish returns at once",
)
def test_interrupting_run_coroutine_inside_a_running_loop_waits_for_the_cancellation():
    ticker = Ticker(close_delay=0.2)
    seen_at_interrupt: list[str] = []

    def cell():
        try:
            return run_coroutine(ticker.run())
        except KeyboardInterrupt:
            seen_at_interrupt.extend(ticker.events)
            raise

    try:
        interrupted_in_a_running_loop(cell)
    finally:
        if ticker.thread is not None:
            ticker.finished.wait(5)  # don't leave the worker running into other tests
    assert seen_at_interrupt == ["cancelled", "closed"]


def test_interrupting_the_async_runner_in_a_notebook_cancels_its_requests_and_closes_the_session():
    client = TimedAsyncClient(delay=10.0)
    results: dict[int, str] = {}

    def cell():
        return AsyncRunner(client, max_concurrency=2).run(make_requests(4), results, RecordingProgress(4), 10)

    interrupted_in_a_running_loop(cell)
    assert wait_until(lambda: client.closed == 1), "the async session was never closed"
    assert client.keys("start") == {0, 1}
    assert client.keys("cancelled") == {0, 1}
    assert results == {}


def test_interrupting_the_executor_in_a_notebook_is_not_treated_as_a_broken_strategy():
    client = TimedAsyncClient(delay=10.0)
    second = ScriptedRunner("sync")
    executor = FallbackExecutor([AsyncRunner(client, max_concurrency=2), second])

    interrupted_in_a_running_loop(lambda: executor.run(make_requests(2), RecordingProgress(2), batch_size=10))
    assert wait_until(lambda: client.closed == 1)
    assert executor.broken == set()
    assert second.calls == []


# --- build_executor --------------------------------------------------------------------------------------


def test_default_strategies_are_batch_then_async_then_sync():
    assert STRATEGIES == ("batch", "async", "sync")
    executor = build_executor(FakeClient())
    assert [type(runner) for runner in executor.runners] == [BatchRunner, AsyncRunner, SyncRunner]
    assert [runner.name for runner in executor.runners] == ["batch", "async", "sync"]


def test_build_executor_defaults():
    batch, async_runner, _ = build_executor(FakeClient()).runners
    assert (batch.poll_interval, batch.timeout, batch.cancel_wait) == (60.0, 24 * 3600.0, 600.0)
    assert (batch.max_concurrent_jobs, batch.cleanup) == (4, True)
    assert async_runner.max_concurrency == 16


@pytest.mark.parametrize("strategies", [("sync", "async"), ["async"], ("sync", "batch", "async")])
def test_build_executor_keeps_the_given_order(strategies):
    executor = build_executor(FakeClient(), strategies)
    assert [runner.name for runner in executor.runners] == list(strategies)


def test_build_executor_passes_the_options_on(clock):
    client = FakeClient()
    executor = build_executor(
        client,
        max_concurrency=5,
        batch_poll_interval=7.0,
        batch_timeout=90.0,
        max_concurrent_batch_jobs=2,
        batch_cleanup=False,
        batch_cancel_wait=30.0,
        sleep=clock.sleep,
        clock=clock,
    )
    batch, async_runner, sync = executor.runners
    assert all(runner.client is client for runner in executor.runners)
    assert (batch.poll_interval, batch.timeout, batch.max_concurrent_jobs, batch.cleanup) == (7.0, 90.0, 2, False)
    assert batch.cancel_wait == 30.0
    assert async_runner.max_concurrency == 5

    results, failures = executor.run(make_requests(2), RecordingProgress(2), batch_size=10)
    assert results == {0: "<t0>", 1: "<t1>"}
    assert failures == {}
    assert 7.0 in clock.sleeps  # the injected sleep and poll interval are used
    assert client.deleted == []  # cleanup is off


@pytest.mark.parametrize(
    "strategies",
    [
        ("batch", "stream"),
        ("Batch",),
        ("sync", "sync"),
        ("async", "sync", "async"),
        "sync",  # a bare string rather than a sequence of names
        (),
    ],
)
def test_build_executor_rejects_bad_strategy_lists(strategies):
    with pytest.raises(ConfigError):
        build_executor(FakeClient(), strategies)


def test_executor_skips_batch_without_a_batch_deployment():
    client = FakeClient(batch_deployment=None)
    progress = RecordingProgress(3)
    results, failures = build_executor(client).run(make_requests(3), progress, batch_size=10)
    assert results == {key: f"<t{key}>" for key in range(3)}
    assert failures == {}
    assert progress.strategies == ["async"]
    assert client.files == {} and client.jobs == {}


def test_executor_with_nothing_the_client_supports_raises_config_error():
    client = FakeClient(deployment=None, batch_deployment=None)
    with pytest.raises(ConfigError):
        build_executor(client).run(make_requests(1), RecordingProgress(1), batch_size=10)


def test_rejected_batch_upload_falls_back_to_async_and_is_not_retried(clock):
    client = FakeClient(upload_error=LLMSetupError("The Batch API isn't available in this region"))
    executor = build_executor(client, sleep=clock.sleep, clock=clock)
    progress = RecordingProgress(4)
    results, failures = executor.run(make_requests(4), progress, batch_size=2)
    assert results == {key: f"<t{key}>" for key in range(4)}
    assert failures == {}
    assert progress.strategies == ["batch", "async"]
    assert executor.broken == {"batch"}
    assert len(client.calls["async"]) == 4 and client.calls["sync"] == []

    progress = RecordingProgress(2)
    executor.run(make_requests(2, start=4), progress, batch_size=2)
    assert progress.strategies == ["async"]


def test_async_setup_error_falls_back_to_sync_keeping_async_replies():
    def async_responder(messages):
        if content_of(messages) == "t2":
            raise LLMSetupError("the async client can't start")
        return "async"

    client = FakeClient(batch_deployment=None, async_responder=async_responder, sync_responder=lambda m: "sync")
    executor = build_executor(client)
    progress = RecordingProgress(6)
    results, failures = executor.run(make_requests(6), progress, batch_size=10)
    assert failures == {}
    assert sorted(results) == list(range(6))
    assert results[2] == "sync"
    assert results[0] == results[1] == "async"  # answered before the setup error; not sent again
    answered_by_async = [key for key, text in results.items() if text == "async"]
    assert len(client.calls["sync"]) == 6 - len(answered_by_async)
    assert executor.broken == {"async"}
    assert progress.advanced == 6


def test_async_then_sync_chain_works_inside_a_running_event_loop():
    client = FakeClient(batch_deployment=None)

    async def notebook_cell():
        return build_executor(client, ("async", "sync")).run(make_requests(4), RecordingProgress(4), batch_size=2)

    results, failures = asyncio.run(notebook_cell())
    assert results == {key: f"<t{key}>" for key in range(4)}
    assert failures == {}
    assert client.calls["sync"] == []  # the async strategy didn't have to give up


def test_failed_batch_job_is_final_when_batch_is_the_only_strategy_the_client_supports(clock):
    client = FakeClient(deployment=None, batch_statuses=("validating", "failed"), batch_errors=("quota exceeded",))
    progress = RecordingProgress(3)
    results, failures = build_executor(client, sleep=clock.sleep, clock=clock).run(
        make_requests(3), progress, batch_size=10
    )
    assert results == {}
    assert sorted(failures) == [0, 1, 2]
    assert all(error.retryable and error.code == "batch" for error in failures.values())
    assert "quota exceeded" in str(failures[0])
    assert progress.failed == 3
