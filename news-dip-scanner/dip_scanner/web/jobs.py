"""'Analyse now': manual analyses started from the website, run one at a time in a background thread.

A member may start ANALYZE_LIMIT_PER_USER of them in 24 hours (admins have no limit), and at most BURST_LIMIT in
BURST_WINDOW (everyone). Each is a row in the jobs table (accounts.py), so /jobs/<id> can show how it is going and
a restart can mark the unfinished ones failed. The analysis is the scanner's own (Scanner.analyze_ticker): its
result is stored like `dip-scanner analyze` stores it, never alerted to anybody, and counts as seen by the user who
started it (recipients.mark_manual_analysis).

Pages start one with a form: POST /analyze with ticker (and csrf_token, and optionally next: the page to return to
when it can't start). Handlers can call ctx.jobs.submit(user, ticker) directly.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from ..accounts import AccountError, Accounts, Job, User
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
        self._pool().submit(self._run, job.id, user.recipient_key, symbol)
        return job

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
            opp = self.accounts.store.add_opportunity(opp)
            mark_manual_analysis(self.accounts.store, [opp.id], viewer=viewer, when=opp.created)
        except Exception as exc:  # every failure ends the job with a message for the user
            message = self._failure(exc, ticker)
            try:
                self.accounts.finish_job(job_id, error=message)
            except Exception:
                log.exception("Couldn't record the failure of manual analysis #%d.", job_id)
            return
        self.accounts.finish_job(job_id, opportunity_id=opp.id)
        log.info("Manual analysis #%d of %s done: opportunity #%d.", job_id, ticker, opp.id)

    def _failure(self, exc: Exception, ticker: str) -> str:
        if isinstance(exc, PriceError):
            text = str(exc)
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

    def shutdown(self) -> None:
        """Stop taking jobs: queued ones are dropped (the next start marks them failed); a running one finishes
        before the process exits, unless the process is killed first (then the next start marks it failed too)."""
        with self._lock:
            executor = self._executor
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)


@router.post("/analyze")
def start_analysis(request: Request, user: auth.SignedIn, form: auth.SignedForm, ctx: auth.Ctx) -> Response:
    """Start an analysis (the "Analyse now" buttons) and show its job page."""
    back = auth.safe_next(form.get("next"))
    try:
        job = ctx.jobs.submit(user, str(form.get("ticker") or ""))
    except JobLimitError as exc:
        return error_page(request, 429, str(exc))
    except AccountError as exc:
        return redirect(request, back, str(exc), kind="error")
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
            "ahead": ahead,
            "refresh": REFRESH_SECONDS if not job.done else None,
            "remaining": ctx.jobs.remaining(user),
            "page_title": f"Analysis of {job.ticker}",
        },
    )
