"""The scanner inside the website's process: its watch loop in a background thread, and what the pages see of it.

ScannerControl starts Scanner.watch() in a daemon thread when the website starts (create_app's lifespan) and stops it
when the website shuts down. Admins pause and resume it (the flag is kept in the database's app_state, so it survives a
restart) and ask for a cycle now. A setup problem (LLMSetupError or ConfigError: a rejected key, no credit, a broken
setting) stops the loop, like it stops `dip-scanner watch`: the website keeps serving, status() says "stopped" with
the reason, a "dip-scanner stopped" notice goes out (on_stop), and an admin can start the loop again after fixing it.

status() is what the dashboard's status strip, the admin page and /healthz show: running, paused, stopped, disabled
(SCANNER_ENABLED=false or `serve --no-scanner`) or stalled (no cycle has finished for three intervals while the loop
should be running).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from ..config import ConfigError
from ..llm import LLMSetupError
from ..models import CycleRecord, utc
from ..notices import one_line, scrub
from ..store import Store

if TYPE_CHECKING:
    from ..pipeline import Scanner

log = logging.getLogger(__name__)

STATES = ("running", "paused", "stopped", "disabled", "stalled")
# What members read instead of an operator's reason (which names keys, settings and commands they can't change).
MEMBER_REASONS = {
    "stopped": "The scanner is stopped for now. The site's admin can see why on the admin page.",
    "disabled": "The scanner isn't running on this server right now.",
}
STALLED_AFTER_INTERVALS = 3
# Seconds the website's shutdown waits, all together, for the scanner's cycle and a running manual analysis to finish
# (a cycle stops between candidates; a debate that has started can take a few minutes of model calls). Only then are
# the databases closed: work cut off under them would be paid for and lost. Behind the thread is left after that.
SHUTDOWN_BUDGET = 150.0
STOP_TIMEOUT = SHUTDOWN_BUDGET  # what shutdown() waits by default


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class ScannerStatus:
    """What the scanner is doing, for the pages and /healthz."""

    state: str  # STATES
    reason: str | None  # why it is stopped, disabled or stalled; None otherwise
    interval_minutes: float
    running_since: datetime | None  # when the watch loop started
    cycle_started: datetime | None  # a cycle is running since then
    next_cycle_at: datetime | None  # when the loop waits: the next cycle is due then
    last_cycle: CycleRecord | None  # the newest recorded cycle (from the database, so also from before a restart)
    can_run_now: bool  # the loop is alive, so run_now() works
    can_restart: bool  # the loop stopped on an error and restart() can start it again

    @property
    def label(self) -> str:
        """One line for people: "Running", "Paused", "Stopped: <reason>"..."""
        text = {
            "running": "Running",
            "paused": "Paused",
            "stopped": "Stopped",
            "disabled": "Off",
            "stalled": "Stalled",
        }.get(self.state, self.state.capitalize())
        return f"{text}: {self.reason}" if self.reason else text

    @property
    def last_cycle_at(self) -> datetime | None:
        """When the newest recorded cycle finished (or started, for one that never finished)."""
        if self.last_cycle is None:
            return None
        return self.last_cycle.finished or self.last_cycle.started


def member_view(status: ScannerStatus) -> ScannerStatus:
    """The status as a member sees it: a stopped or disabled scanner's reason in plain words (the operator's reason,
    e.g. "fly secrets set ANTHROPIC_API_KEY=...", is for admins: the admin page shows it)."""
    if status.state in MEMBER_REASONS:
        return replace(status, reason=MEMBER_REASONS[status.state])
    return status


class ScannerControl:
    """Runs a Scanner's watch loop in a thread and reports on it.

    scanner is None when it couldn't be built (reason says why) or isn't wanted; enabled=False means the scanner
    isn't supposed to run in this process (status "disabled"). store is used for the pause flag and the cycle records
    (the scanner's own store in `serve`). on_stop(reason) is called when the loop stops on a setup problem, or at
    start() when the scanner couldn't be built: it sends the "dip-scanner stopped" notice. secrets are scrubbed from
    every reason.
    """

    def __init__(
        self,
        scanner: Scanner | None,
        store: Store,
        *,
        enabled: bool = True,
        interval_minutes: float = 5,
        reason: str | None = None,
        on_stop: Callable[[str], None] | None = None,
        clock: Callable[[], datetime] = _now,
        secrets: Iterable[str] = (),
    ) -> None:
        self.scanner = scanner
        self.store = store
        self.enabled = enabled
        self.interval_minutes = float(interval_minutes)
        self._secrets = list(secrets)
        self._reason = self._clean(reason) if reason else None
        if scanner is None and enabled and self._reason is None:
            self._reason = "The scanner isn't set up in this process."
        self._on_stop = on_stop
        self._clock = clock
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._running_since: datetime | None = None
        self._resumed_at: datetime | None = None
        self._started = False
        self._shutting_down = False

    # --- the loop ---

    def start(self) -> None:
        """Start the watch loop in a daemon thread (nothing when disabled or already running). When the scanner
        couldn't be built, on_stop is told the reason once instead."""
        if not self.enabled:
            log.info("The scanner is off in this process (SCANNER_ENABLED=false or serve --no-scanner).")
            return
        unbuilt = None
        with self._lock:
            first = not self._started
            self._started = True
            if self.scanner is None:
                unbuilt = self._reason if first else None
            elif self._thread is None or not self._thread.is_alive():
                self._reason = None
                self._running_since = utc(self._clock())
                self._thread = threading.Thread(target=self._run, name="dip-scanner-watch", daemon=True)
                self._thread.start()
        if unbuilt:
            log.error("The scanner can't start: %s The website keeps running.", unbuilt)
            self._notify(unbuilt)

    def _run(self) -> None:
        assert self.scanner is not None
        try:
            self.scanner.watch(interval_minutes=self.interval_minutes)
        except (LLMSetupError, ConfigError) as exc:
            what = "Configuration problem" if isinstance(exc, ConfigError) else "The language model can't be used"
            self._stopped(f"{what}: {exc}", notify=True)
        except Exception as exc:  # a bug: the website keeps serving and says so
            log.exception("The scanner's loop failed.")
            self._stopped(f"The scanner stopped unexpectedly ({type(exc).__name__}: {exc}); see the log.", notify=True)
        else:
            if not self._shutting_down:
                self._stopped("The scanner's loop ended.", notify=False)

    def _stopped(self, reason: str, *, notify: bool) -> None:
        cleaned = self._clean(reason)
        with self._lock:
            self._reason = cleaned
            self._running_since = None
        log.error("The scanner stopped: %s The website keeps running.", cleaned)
        if notify:
            self._notify(cleaned)

    def _notify(self, reason: str) -> None:
        if self._on_stop is None:
            return
        try:
            self._on_stop(reason)
        except Exception as exc:  # the notice is a courtesy; the status shows the reason anyway
            log.warning("Couldn't send the notice that the scanner stopped: %s", self._clean(str(exc)))

    def _clean(self, text: str) -> str:
        return one_line(scrub(str(text), self._secrets))

    def begin_shutdown(self) -> None:
        """Ask the loop to stop (the website is shutting down): a cycle that is running stops before its next
        candidate, reports what it found and ends; no cycle starts after it."""
        self._shutting_down = True
        if self.scanner is not None:
            self.scanner.stop()

    def shutdown(self, timeout: float = STOP_TIMEOUT) -> None:
        """begin_shutdown() and wait up to timeout seconds for a running cycle."""
        thread = self._thread
        self.begin_shutdown()
        if thread is not None and thread.is_alive():
            thread.join(timeout)
            if thread.is_alive():
                log.warning("The scanner's cycle didn't finish within %.0f s; leaving it behind.", timeout)

    def join(self, timeout: float | None = None) -> None:
        """Wait up to timeout seconds for the loop's thread to end (it ends on stop() or a setup problem)."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def alive(self) -> bool:
        """Whether the watch loop's thread is running."""
        thread = self._thread
        return thread is not None and thread.is_alive()

    # --- what admins do ---

    def pause(self) -> None:
        """Pause the scanner: the loop skips its cycles until resume(). Kept in the database (survives restarts)."""
        self.store.set_scanner_paused(True)
        log.info("The scanner was paused on the admin page.")

    def resume(self) -> None:
        """Resume a paused scanner; the next cycle runs at the next interval."""
        self.store.set_scanner_paused(False)
        self._resumed_at = utc(self._clock())
        log.info("The scanner was resumed on the admin page.")

    def paused(self) -> bool:
        try:
            return self.store.scanner_paused()
        except sqlite3.Error:
            return False

    def run_now(self) -> bool:
        """Ask the loop for a cycle at once (also while paused); False when the loop isn't running."""
        if self.scanner is None or not self.alive():
            return False
        self.scanner.request_cycle_now()
        log.info("A cycle was requested on the admin page.")
        return True

    def restart(self) -> bool:
        """Start the loop again after it stopped on an error (e.g. once the provider has credit again); False when it
        can't be (disabled, never built, or still running)."""
        if not self.enabled or self.scanner is None or self.alive() or self._shutting_down:
            return False
        log.info("The scanner is being started again from the admin page.")
        self.start()
        return True

    # --- what the pages see ---

    def status(self, *, now: datetime | None = None) -> ScannerStatus:
        """The scanner's state now (see the module docstring)."""
        now = utc(now) if now is not None else utc(self._clock())
        try:
            last = self.store.last_cycle()
        except sqlite3.Error as exc:
            log.warning("Couldn't read the last cycle: %s", exc)
            last = None
        scanner = self.scanner
        alive = self.alive()
        reason = self._reason
        if not self.enabled:
            state = "disabled"
            reason = reason or "The scanner doesn't run in this process (SCANNER_ENABLED=false or serve --no-scanner)."
        elif scanner is None or reason is not None or not alive:
            state = "stopped"
            reason = reason or "The scanner hasn't started."
        elif self.paused():
            state = "paused"
        else:
            state = "running"
            limit = timedelta(minutes=self.interval_minutes * STALLED_AFTER_INTERVALS)
            marks = [self._running_since, self._resumed_at]
            if last is not None:
                marks.append(last.finished or last.started)
            progress = max((mark for mark in marks if mark is not None), default=None)
            if progress is not None and now - progress > limit:
                state = "stalled"
                reason = f"no cycle has finished for over {limit.total_seconds() / 60:.0f} minutes"
        return ScannerStatus(
            state=state,
            reason=reason,
            interval_minutes=self.interval_minutes,
            running_since=self._running_since if alive else None,
            cycle_started=getattr(scanner, "cycle_started", None) if alive else None,
            next_cycle_at=getattr(scanner, "next_cycle_at", None) if alive else None,
            last_cycle=last,
            can_run_now=alive,
            can_restart=self.enabled and scanner is not None and not alive and self._started,
        )
