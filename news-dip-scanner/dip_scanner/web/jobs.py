"""'Analyse now': manual analyses started from the website, run one at a time in a background thread.

A member may start ANALYZE_LIMIT_PER_USER of them in 24 hours (admins have no limit), and at most BURST_LIMIT in
BURST_WINDOW (everyone); one that fails before the model is asked (failure_kind: no prices, Yahoo down, a setup
problem, or a restart) doesn't count. Each is a row in the jobs table (accounts.py), so /jobs/<id> can show how it is
going and a restart can mark the unfinished ones failed. The analysis is the scanner's own (Scanner.analyze_ticker):
its result is stored as a manual analysis (Store.add_opportunity(manual=True), which the scanner's cooldown ignores),
never alerted by itself (it can take a waiting alert's place: see Scanner._send_alerts_to), and counts as seen by the
user who started it (recipients.mark_manual_analysis).

Pages start one with a form: POST /analyze with ticker (and csrf_token, and optionally next: the page to return to
when it can't start). Handlers can call ctx.jobs.submit(user, ticker) directly.
"""

from __future__ import annotations

import contextlib
import logging
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from ..accounts import RESTART_FAILURE, AccountError, Accounts, Job, User
from ..config import ConfigError, Settings
from ..llm import LLMError, LLMSetupError
from ..models import Opportunity
from ..notices import one_line, scrub, secrets_of
from ..prices import PriceError, PriceFetchError
from ..recipients import mark_manual_analysis
from ..triage import normalise_ticker
from . import auth
from .app import error_page, redirect, render

log = logging.getLogger(__name__)

Analyse = Callable[[str, datetime], Opportunity]  # (ticker, now) -> the analysis, not stored yet

BURST_LIMIT = 10  # analyses per user in BURST_WINDOW, admins included: a double click or a script can't run up a bill
# What members read when analyses can't run here, or one failed on the server's setup: the operator's reason (a key,
# a setting, a command) is for admins.
MEMBER_UNAVAILABLE = "Manual analyses aren't available right now. The site's admin can see why on the admin page."
BURST_WINDOW = timedelta(minutes=10)
REFRESH_SECONDS = 5  # the job page reloads itself this often while the job waits or runs

router = APIRouter()


class JobLimitError(AccountError):
    """The user has started as many analyses as they may for now (shown with HTTP 429)."""


class InlineExecutor(Executor):
    """Runs each job at once in the caller's thread: for tests, so nothing waits on a background thread."""

    def submit(self, fn, /, *args, **kwargs) -> Future:
        future: Future = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:
            future.set_exception(exc)
        return future


class JobRunner:
    """Queues manual analyses and runs them with analyse (None: not available on this server, unavailable says why).

    executor defaults to a single background thread, so analyses run one after another, and never at the same time as
    each other; the scanner's own cycle may run alongside.
    """

    def __init__(
        self,
        accounts: Accounts,
        *,
        analyse: Analyse | None,
        settings: Settings,
        clock: Callable[[], datetime],
        executor: Executor | None = None,
        unavailable: str | None = None,
    ) -> None:
        self.accounts = accounts
        self.analyse = analyse
        self.settings = settings
        self.unavailable = unavailable
        self._clock = clock
        self._executor = executor
        self._lock = threading.Lock()
        self._secrets = secrets_of(settings)
        self._stopping = False
        self._futures: dict[int, Future] = {}  # job id -> its run, until it is done

    @property
    def available(self) -> bool:
        return self.analyse is not None

    def limit_for(self, user: User) -> int | None:
        """How many analyses the user may start in 24 hours (None: no limit)."""
        return None if user.is_admin else self.settings.web.analyze_limit_per_user

    def remaining(self, user: User) -> int | None:
        """How many more analyses the user may start now (None: no limit)."""
        limit = self.limit_for(user)
        if limit is None:
            return None
        return max(0, limit - self.accounts.count_jobs(user.id))

    def submit(self, user: User, ticker: str) -> Job:
        """Queue an analysis of ticker for user and return its job. When the user already has one of the same ticker
        waiting or running, that one is returned. AccountError says why it can't start (JobLimitError for limits)."""
        if self.analyse is None:
            raise AccountError(self.unavailable or "Manual analyses aren't available on this server.")
        if self._stopping:
            raise AccountError("The website is restarting; start the analysis again in a minute.")
        symbol = normalise_ticker(str(ticker or ""))
        if symbol is None:
            shown = str(ticker or "").strip()[:20]
            raise AccountError(f"{shown!r} isn't a Yahoo Finance symbol; write it like AMD, SAP.DE or ALWN.AT.")
        with self._lock:
            for job in self.accounts.list_jobs(user_id=user.id, limit=20):
                if job.ticker == symbol and not job.done:
                    return job
            limit = self.limit_for(user)
            if limit is not None:
                if limit == 0:
                    raise JobLimitError("Manual analyses are switched off for members on this server.")
                if self.accounts.count_jobs(user.id) >= limit:
                    raise JobLimitError(
                        f"You have used your {limit} manual analyses of the last 24 hours. The scanner keeps analysing "
                        "dips on its own; try again later."
                    )
            if self.accounts.rate_limited(f"analyze:{user.id}", limit=BURST_LIMIT, window=BURST_WINDOW):
                raise JobLimitError("Too many analyses started in the last few minutes. Wait a little and try again.")
            job = self.accounts.create_job(user.id, symbol)
        log.info("Manual analysis #%d of %s queued by account #%d.", job.id, symbol, user.id)
        future = self._pool().submit(self._run, job.id, user.recipient_key, symbol)
        with self._lock:
            if not future.done():
                self._futures[job.id] = future
        future.add_done_callback(lambda _, job_id=job.id: self._forget(job_id))
        return job

    def _forget(self, job_id: int) -> None:
        with self._lock:
            self._futures.pop(job_id, None)

    def _pool(self) -> Executor:
        with self._lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dip-analyse")
            return self._executor

    def _run(self, job_id: int, viewer: str, ticker: str) -> None:
        assert self.analyse is not None
        try:
            self.accounts.start_job(job_id)
            now = self._clock()
            opp = self.analyse(ticker, now)
            opp = self.accounts.store.add_opportunity(opp, manual=True)
            mark_manual_analysis(self.accounts.store, [opp.id], viewer=viewer, when=opp.created)
        except Exception as exc:  # every failure ends the job with a message for the user
            message = self._failure(exc, ticker)
            try:
                self.accounts.finish_job(job_id, error=message, failure=failure_kind(exc))
            except Exception:
                log.exception("Couldn't record the failure of manual analysis #%d.", job_id)
            return
        self.accounts.finish_job(job_id, opportunity_id=opp.id)
        log.info("Manual analysis #%d of %s done: opportunity #%d.", job_id, ticker, opp.id)

    def _failure(self, exc: Exception, ticker: str) -> str:
        """The message a failed job shows its member (secrets scrubbed)."""
        if isinstance(exc, PriceError):  # the command line's hint, in the website's words
            text = re.sub(r"try `dip-scanner analyze ([^`\s]+)`", r"open \1's page to analyse it", str(exc))
        elif isinstance(exc, PriceFetchError):
            text = f"Yahoo Finance couldn't be reached for {ticker}; try again in a few minutes."
        elif isinstance(exc, LLMSetupError):
            text = f"The language model can't be used: {exc}"
        elif isinstance(exc, LLMError):
            text = f"The analysis of {ticker} failed: {exc}"
        elif isinstance(exc, ConfigError):
            text = f"Configuration problem: {exc}"
        else:
            log.exception("Manual analysis of %s failed.", ticker)
            return f"The analysis of {ticker} failed because of an error on the server (it was logged)."
        log.warning("Manual analysis of %s failed: %s", ticker, scrub(text, self._secrets))
        return one_line(scrub(text, self._secrets), 480)

    def stop(self) -> None:
        """Stop taking jobs (the website is shutting down): submit() refuses new ones, and the queued ones are failed
        as interrupted by the restart (that doesn't count toward the member's limit). A running one goes on: wait()."""
        with self._lock:
            self._stopping = True
            executor = self._executor
            futures = dict(self._futures)
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        for job_id, future in futures.items():
            if future.cancelled():
                self._interrupted(job_id)

    def wait(self, timeout: float) -> list[int]:
        """Wait up to timeout seconds for the running jobs; the ids of those still running then are marked failed as
        interrupted by the restart (not counted), while the database is still open, and returned."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            futures = dict(self._futures)
        for future in futures.values():
            if not future.running():  # still waiting for its turn: it won't get one now
                continue
            # A timeout, or the job's own failure (recorded by _run): the state is read below.
            with contextlib.suppress(Exception):
                future.result(timeout=max(0.0, deadline - time.monotonic()))
        left = [job_id for job_id, future in futures.items() if not future.done()]
        for job_id in left:
            log.warning("Manual analysis #%d didn't finish before the website stopped.", job_id)
            self._interrupted(job_id)
        return left

    def _interrupted(self, job_id: int) -> None:
        try:
            self.accounts.finish_job(job_id, error=RESTART_FAILURE, failure="restart")
        except Exception:
            log.exception("Couldn't record that manual analysis #%d was interrupted.", job_id)

    def shutdown(self) -> None:
        """stop() without waiting (the running job, if any, is marked failed by the next start)."""
        self.stop()


def shown_error(job: Job, viewer: User) -> str | None:
    """A failed job's message for the person reading it: a setup failure's detail only for admins."""
    if job.failure == "setup" and not viewer.is_admin:
        return (
            f"The analysis of {job.ticker} couldn't run: the language model isn't set up right on the server. The "
            "site's admin can see why."
        )
    return job.error


def failure_kind(exc: Exception) -> str:
    """The kind of a job's failure (accounts.JOB_FAILURES): those raised before any model call (no prices, Yahoo
    down, a setup problem) don't count toward the member's limit."""
    if isinstance(exc, PriceError):
        return "no_prices"
    if isinstance(exc, PriceFetchError):
        return "prices_down"
    if isinstance(exc, LLMSetupError | ConfigError):
        return "setup"
    if isinstance(exc, LLMError):
        return "model"
    return "error"


@router.post("/analyze")
def start_analysis(request: Request, user: auth.SignedIn, form: auth.SignedForm, ctx: auth.Ctx) -> Response:
    """Start an analysis (the "Analyse now" buttons) and show its job page."""
    back = auth.safe_next(form.get("next"))
    try:
        job = ctx.jobs.submit(user, str(form.get("ticker") or ""))
    except JobLimitError as exc:
        return error_page(request, 429, str(exc))
    except AccountError as exc:
        message = str(exc) if ctx.jobs.available or user.is_admin else MEMBER_UNAVAILABLE
        return redirect(request, back, message, kind="error")
    return redirect(request, f"/jobs/{job.id}")


@router.get("/jobs/{job_id}")
def job_page(request: Request, job_id: int, user: auth.SignedIn, ctx: auth.Ctx) -> Response:
    """How an analysis is going: reloads itself while it waits or runs, goes to the idea when it is done."""
    job = ctx.accounts.get_job(job_id)
    if job is None or (job.user_id != user.id and not user.is_admin):
        raise HTTPException(status_code=404)
    if job.status == "done" and job.opportunity_id is not None:
        return redirect(request, f"/ideas/{job.opportunity_id}")
    ahead = 0
    if job.status == "queued":
        ahead = sum(
            1
            for other in ctx.accounts.list_jobs(limit=200)
            if other.id < job.id and other.status in ("queued", "running")
        )
    return render(
        request,
        "jobs/job.html",
        {
            "job": job,
            "error": shown_error(job, user),
            "ahead": ahead,
            "refresh": REFRESH_SECONDS if not job.done else None,
            "remaining": ctx.jobs.remaining(user),
            "debate": ctx.settings.llm.analysis_mode == "debate",
            "page_title": f"Analysis of {job.ticker}",
        },
    )
