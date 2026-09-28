"""The JSON API under /api/v1: what the Next.js front end (frontend/, on Vercel) reads on behalf of the signed-in
visitor, and the one action it starts itself ("Analyse again").

The contract is frontend/contract/api-v1.schema.json (JSON Schema draft 2020-12, one definition per response, and
x-endpoints mapping each endpoint to its definition); frontend/src/lib/types.ts mirrors it, and tests/test_web_api.py
validates this module's real answers against it. Change them together.

- The same session cookie (dsid) as the pages; without a session every endpoint answers 401.
- Every error is JSON: {"error": {"code", "message", "retry_after"}} (app.api_error), with the codes of the contract:
  not_signed_in 401; forbidden, csrf, origin 403; not_found 404; method_not_allowed 405; bad_request 400; rate_limited
  and limit_reached 429 (with Retry-After); unavailable 503; server_error 500.
- A POST needs the X-CSRF-Token header equal to the session's token (GET /me's csrf), and its Origin (or Referer)
  must be this site or one of TRUSTED_ORIGINS, like the pages' forms.
- Numbers are JSON numbers, never formatted text; moments are ISO 8601 in UTC, days YYYY-MM-DD; amounts are in the
  idea's trading currency with "approx" in the reader's (Money), and what a page shows as "–" is null.
- Everything is computed for the reader: their currency, watchlist, alert rules and limits, as the pages do (the
  helpers are pages.py's).
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from ..accounts import DAY, AccountError, Session, User, same_token
from ..config import LLM_PROVIDERS
from ..fx import main_currency, same_money
from ..models import AGREEMENTS, DEBATE_MODES, VERDICTS, Debate, Opportunity, analysis_to_dict, to_iso, utc
from ..report import debate_line, fx_text, model_display_name, score_band, verdict_label
from ..track import STATUS_LABELS, Outcome, signal_day
from . import auth, pages
from .app import api_error, paginate
from .context import AppContext
from .control import member_view
from .jobs import BURST_WINDOW, JobLimitError, shown_error
from .pages import (
    DAY_CHOICES,
    DEFAULT_DAYS,
    IDEA_HISTORY,
    PAGE_SIZE,
    SCORE_CHOICES,
    THESIS_DAYS,
    IdeaPrices,
    _analyse_info,
    _choice,
    _flag,
    debater_names,
    idea_prices,
    index_name,
    ladder,
    matches_rules,
    newest_per_ticker,
    rule_misses,
    thesis_changes,
    watchlist_of,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")

SORTS = ("score", "new")
MAX_THESIS_DAYS = 30
MAX_CRITIQUE = 5  # critique and concessions per participant (the debate keeps at most 5)
STATE_LABELS = {
    "running": "Scanner running",
    "paused": "Scanner paused",
    "stopped": "Scanner stopped",
    "disabled": "Scanner off",
    "stalled": "Scanner stalled",
}
# pages.ladder's keys -> the contract's level keys, and the chart's short labels (charts.Level)
LEVELS = {
    "target": ("target", "Target"),
    "price": ("price", "Reported"),
    "entry": ("entry", "Entry"),
    "stat-low": ("stat_low", "Stat. low"),
    "low": ("potential_low", "Low"),
}
NO_IDEA = "There is no idea with that number. It may have been removed."
NO_JOB = "There is no such analysis."
CSRF_FAILED = "This page has expired. Reload it and try again."
ORIGIN_REFUSED = "This request came from another site, so it was refused. Open the page on this site and try again."
SWITCHED_OFF = "Manual analyses are switched off for members on this server."
DIGITS = 4  # computed amounts and percentages are rounded to this many decimals


class ApiError(Exception):
    """An error answer of the API (see app.api_error): status, code (the status's by default), a message for people,
    and for a 429 the seconds until trying again makes sense."""

    def __init__(
        self, status: int, code: str | None = None, message: str | None = None, *, retry_after: int | None = None
    ) -> None:
        super().__init__(message or code or str(status))
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after


async def api_error_handler(request: Request, exc: ApiError) -> Response:
    return api_error(exc.status, exc.code, exc.message, retry_after=exc.retry_after)


# --- who is asking -------------------------------------------------------------------------------------------------


def signed_in(request: Request) -> Session:
    """Dependency: the visitor's session; 401 without one."""
    session = auth.load_session(request)
    if session is None:
        raise ApiError(401)
    return session


async def signed_post(request: Request) -> Session:
    """Dependency for a POST: the Origin check (403 origin), the session (401) and the X-CSRF-Token header, which must
    be the session's token (403 csrf)."""
    if not auth.origin_ok(request):
        raise ApiError(403, "origin", ORIGIN_REFUSED)
    session = await run_in_threadpool(auth.load_session, request)
    if session is None:
        raise ApiError(401)
    if not same_token(session.csrf, request.headers.get("x-csrf-token")):
        raise ApiError(403, "csrf", CSRF_FAILED)
    return session


SignedIn = Annotated[Session, Depends(signed_in)]
SignedPost = Annotated[Session, Depends(signed_post)]


# --- small conversions ---------------------------------------------------------------------------------------------


def _iso(moment: datetime | None) -> str | None:
    return to_iso(moment) if moment is not None else None


def _day(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _number(value: float | None, digits: int = DIGITS) -> float | None:
    """A computed number for JSON: rounded, None for a missing or non-finite one."""
    if value is None or not math.isfinite(value):
        return None
    return round(float(value), digits)


def _converts(view: Opportunity) -> bool:
    """Whether the view's amounts have "≈" amounts in the reader's currency (report.in_account's test)."""
    account = view.account_currency
    return view.fx_rate is not None and bool(account) and not same_money(view.currency, account)


def money(amount: float, view: Opportunity) -> dict[str, Any]:
    """Money: the amount in the trading currency and about the same in the reader's (None when nothing converts)."""
    return {"amount": amount, "approx": _number(amount * view.fx_rate) if _converts(view) else None}


def fx_view(view: Opportunity, now: datetime) -> dict[str, Any] | None:
    """Fx: the rate behind the view's approx amounts, or None."""
    if not _converts(view):
        return None
    assert view.fx_rate is not None
    main, factor = main_currency(view.currency)
    return {
        "currency": view.account_currency,
        "rate": view.fx_rate,
        "main_currency": main,
        "rate_main_unit": view.fx_rate * factor,
        "source": "today" if view.fx_rate_today else "analysis",
        "as_of": _iso(now if view.fx_rate_today else view.created),
        "note": fx_text(view) or "",
    }


def _band(score: float) -> str:
    return score_band(float(score))[1]


# --- debates -------------------------------------------------------------------------------------------------------


def shown_debate(opp: Opportunity) -> Debate | None:
    """The idea's debate when it can be shown as the contract describes it (a known mode and agreement, one or two
    participants of known providers); None for a single model's analysis, an old record or a damaged debate."""
    debate = opp.debate
    if debate is None:
        return None
    if (
        debate.mode not in DEBATE_MODES
        or (debate.agreement is not None and debate.agreement not in AGREEMENTS)
        or not 1 <= len(debate.participants) <= 2
        or any(side.model.split(":", 1)[0] not in LLM_PROVIDERS for side in debate.participants)
        or any(side.final.verdict not in VERDICTS for side in debate.participants)
    ):
        log.debug("The debate of opportunity #%s can't be shown in the API; leaving it out.", opp.id)
        return None
    return debate


def _label(position: int, label: str) -> str:
    return label if label in ("A", "B") else "AB"[position]


def debate_summary(opp: Opportunity) -> dict[str, Any] | None:
    """DebateSummary: the debate behind an idea in one line, for lists."""
    debate = shown_debate(opp)
    if debate is None:
        return None
    names = debater_names(debate)
    return {
        "mode": debate.mode,
        "agreement": debate.agreement,
        "line": debate_line(opp) or "",
        "participants": [
            {
                "label": _label(position, side.label),
                "model": side.model,
                "model_label": names[side.model],
                "opening_probability": int(side.opening.probability_up_6m),
                "final_probability": int(side.final.probability_up_6m),
                "final_verdict": side.final.verdict,
                "changed_mind": bool(side.changed_mind),
            }
            for position, side in enumerate(debate.participants)
        ],
        "final_probability": int(opp.analysis.probability_up_6m),
        "judge": debate.judge,
        "judge_label": _name(debate.judge, names),
    }


def _name(model: str | None, names: dict[str, str]) -> str | None:
    """A "provider:model" label as the idea page names it (pages.debater_names), or None."""
    if not model:
        return None
    return names.get(model) or model_display_name(model)


def debate_view(opp: Opportunity, *, admin: bool = False) -> dict[str, Any] | None:
    """DebateView: the stored debate with the texts of the Fly idea page's Debate card (pages.debate_view), so both
    sites word it the same way. The raw reason (the provider's error) is for admins only: members get null, and
    reason_label in plain words."""
    debate = shown_debate(opp)
    card = pages.debate_view(opp, admin=admin) if debate is not None else None
    if debate is None or card is None:
        return None
    names = debater_names(debate)
    return {
        "mode": debate.mode,
        "reason": debate.reason if admin else None,
        "reason_label": card.reason,
        "rounds": max(0, int(debate.rounds)),
        "title": card.title,
        "how": card.how,
        "participants": [
            {
                "label": _label(position, side.participant.label),
                "model": side.participant.model,
                "model_label": side.name,
                "provider": side.participant.model.split(":", 1)[0],
                "provider_label": side.provider,
                "other_label": side.other,
                "opening": analysis_to_dict(side.participant.opening),
                "final": analysis_to_dict(side.participant.final),
                "critique": list(side.participant.critique[:MAX_CRITIQUE]),
                "concessions": list(side.participant.concessions[:MAX_CRITIQUE]),
                "changed_mind": bool(side.participant.changed_mind),
                "favoured": side.favoured,
                "compare": side.compare,
            }
            for position, side in enumerate(card.sides)
        ],
        "judge": debate.judge,
        "judge_label": _name(debate.judge, names),
        "summary": debate.summary,
        "agreement": debate.agreement,
        "favoured": debate.favoured,
        "favoured_label": _name(debate.favoured, names),
        "ruling_title": card.ruling_title,
        "judge_note": card.judge_note,
        "line": debate_line(opp) or "",
    }


# --- ideas ---------------------------------------------------------------------------------------------------------


def idea_summary(
    ctx: AppContext,
    view: Opportunity,
    user: User,
    *,
    now: datetime,
    count: int,
    newest: int | None,
    watchlist: Sequence[str],
) -> dict[str, Any]:
    """IdeaSummary of an idea as the reader sees it. view is the opportunity in the reader's currency (AppContext.view
    or Opportunity.in_currency); count the analyses of the ticker; newest the id of its newest analysis."""
    analysis = view.analysis
    stats = view.stats
    superseded = newest is not None and newest != view.id
    return {
        "id": view.id,
        "ticker": view.ticker,
        "company": view.company,
        "exchange": stats.exchange,
        "currency": view.currency,
        "created": _iso(view.created),
        "age_seconds": max(0, int((utc(now) - utc(view.created)).total_seconds())),
        "score": view.score,
        "score_band": _band(view.score),
        "verdict": analysis.verdict,
        "verdict_label": verdict_label(analysis.verdict),
        "confidence": analysis.confidence,
        "probability_up_6m": int(analysis.probability_up_6m),
        "price": money(view.price, view),
        "entry": money(analysis.entry_price, view),
        "target": money(analysis.target_price, view),
        "potential_low": money(analysis.potential_low, view),
        "stat_low": money(stats.stat_low_6m, view),
        "fx": fx_view(view, now),
        "upside_pct": _number(view.upside_pct()),
        "downside_pct": _number(view.downside_pct()),
        "entry_upside_pct": _number(view.entry_upside_pct()),
        "entry_downside_pct": _number(view.entry_downside_pct()),
        "change_1d_pct": _number(stats.change_1d_pct),
        "change_5d_pct": _number(stats.change_5d_pct),
        "analyses_count": max(1, count),
        "superseded": superseded,
        "superseded_by": newest if superseded else None,
        "matches_my_rules": matches_rules(view, user, ctx.config),
        "on_my_watchlist": view.ticker in watchlist,
        "debate": debate_summary(view),
    }


@router.get("/ideas")
def ideas(request: Request, session: SignedIn, ctx: auth.Ctx) -> dict[str, Any]:
    """IdeasList: the newest analysis of every ticker analysed in the last days, filtered, ranked and paged like the
    dashboard (only rates stored with the analyses convert, as there)."""
    user = session.user
    now = ctx.now()
    query = request.query_params
    days = _choice(query.get("days"), DAY_CHOICES, DEFAULT_DAYS)
    min_score = _choice(query.get("min_score"), SCORE_CHOICES, None)
    verdict = query.get("verdict") if query.get("verdict") in VERDICTS else None
    only_watchlist = _flag(query.get("watchlist"))
    only_matching = _flag(query.get("matching"))
    sort = query.get("sort") if query.get("sort") in SORTS else "score"

    found, counts = newest_per_ticker(ctx.store.opportunities(since=now - timedelta(days=days)))
    total = len(found)
    watchlist = watchlist_of(user, ctx.config)
    if min_score is not None:
        found = [opp for opp in found if opp.score >= min_score]
    if verdict:
        found = [opp for opp in found if opp.analysis.verdict == verdict]
    if only_watchlist:
        found = [opp for opp in found if opp.ticker in watchlist]
    if only_matching:
        found = [opp for opp in found if matches_rules(opp, user, ctx.config)]
    if sort == "score":
        found.sort(key=lambda opp: opp.score, reverse=True)
    page = paginate(len(found), query.get("page"), PAGE_SIZE)
    currency = user.settings.currency
    return {
        "generated_at": _iso(now),
        "filters": {
            "days": days,
            "min_score": min_score,
            "verdict": verdict,
            "watchlist": only_watchlist,
            "matching": only_matching,
            "sort": sort,
        },
        "total": total,
        "count": len(found),
        "page": {"number": page.number, "pages": page.pages, "size": page.size},
        "ideas": [
            # The period ends now, so the newest analysis of a ticker in it is its newest of all.
            idea_summary(
                ctx,
                opp.in_currency(currency),
                user,
                now=now,
                count=counts.get(opp.ticker, 1),
                newest=opp.id,
                watchlist=watchlist,
            )
            for opp in page.slice(found)
        ],
    }


def raw_opportunity(opp: Opportunity, *, admin: bool = False) -> dict[str, Any]:
    """Opportunity.to_dict(), except that a debate the contract can't describe (shown_debate) is left out, and that a
    member doesn't get the debate's raw reason (the provider's error, for admins; see debate_view)."""
    data = opp.to_dict()
    if shown_debate(opp) is None:
        data["debate"] = None
    elif not admin and isinstance(data.get("debate"), dict):
        data["debate"] = {**data["debate"], "reason": pages.member_reason(opp.debate.reason if opp.debate else None)}
    return data


def levels_view(view: Opportunity) -> list[dict[str, Any]]:
    """The five Level rows of the levels card, highest first (pages.ladder)."""
    rows = []
    for row in ladder(view):
        key, short = LEVELS[row["key"]]
        from_entry = None
        if key == "target":
            from_entry = view.entry_upside_pct()
        elif key == "potential_low":
            from_entry = view.entry_downside_pct()
        rows.append(
            {
                "key": key,
                "label": row["label"],
                "short_label": short,
                "value": money(row["value"], view),
                "change_pct": _number(row["change"]),
                "from_entry_pct": _number(from_entry),
            }
        )
    return rows


def chart_view(opp: Opportunity, prices: IdeaPrices) -> dict[str, Any] | None:
    """Chart: the closes pages.idea_prices drew, and the idea's levels on the same split basis."""
    chart = prices.chart
    if chart is None or not chart.points:
        return None
    factor = prices.split or 1.0
    analysis = opp.analysis
    return {
        "currency": opp.currency,
        "closes": [[day.isoformat(), close] for day, close in chart.points],
        "levels": {
            "reported": opp.price / factor,
            "entry": analysis.entry_price / factor,
            "target": analysis.target_price / factor,
            "potential_low": analysis.potential_low / factor,
            "stat_low": opp.stats.stat_low_6m / factor,
        },
        "signal_day": signal_day(opp).isoformat(),
        "split_factor": factor,
        "split_note": prices.split_note,
    }


def outcome_view(outcome: Outcome) -> dict[str, Any]:
    """Outcome: track.evaluate with the index and the reader's currency. Until something traded since the report, or
    when the report's price doesn't match Yahoo's history, every return and day is null (the page's "–")."""
    measured = outcome.priced and not outcome.price_mismatch

    def when(value: Any) -> Any:
        return value if measured else None

    return {
        "status": outcome.status,
        "status_label": STATUS_LABELS.get(outcome.status, outcome.status),
        "priced": outcome.priced,
        "price_mismatch": outcome.price_mismatch,
        "days": outcome.days,
        "last_price": when(outcome.last_price),
        "last_day": when(_day(outcome.last_day)),
        "return_pct": when(_number(outcome.return_pct)),
        "account_currency": outcome.account_currency,
        "account_return_pct": when(_number(outcome.account_return_pct)),
        "benchmark": outcome.benchmark,
        "benchmark_name": index_name(outcome.benchmark) if outcome.benchmark else None,
        "benchmark_return_pct": when(_number(outcome.benchmark_return_pct)),
        "excess_return_pct": when(_number(outcome.excess_return_pct)),
        "entry_filled": when(_day(outcome.entry_filled)),
        "target_hit": when(_day(outcome.target_hit)),
        "low_breached": when(_day(outcome.low_breached)),
        "max_gain_pct": when(_number(outcome.max_gain_pct)),
        "max_loss_pct": when(_number(outcome.max_loss_pct)),
        "trade_return_pct": when(_number(outcome.trade_return_pct)),
        "up_after_6m": when(outcome.up_after_6m),
        "split_factor": outcome.split_factor,
    }


def history_item(opp: Opportunity, *, newest: int | None, current: int | None) -> dict[str, Any]:
    return {
        "id": opp.id,
        "created": _iso(opp.created),
        "verdict": opp.analysis.verdict,
        "verdict_label": verdict_label(opp.analysis.verdict),
        "score": opp.score,
        "score_band": _band(opp.score),
        "probability_up_6m": int(opp.analysis.probability_up_6m),
        "superseded": newest is not None and opp.id != newest,
        "current": opp.id == current,
    }


def fx_note(view: Opportunity, currency: str | None) -> str | None:
    """The rate behind the approx amounts, or why there are none; None when the reader has no other currency."""
    if not currency or same_money(view.currency, currency):
        return None
    return fx_text(view) or f"No {view.currency}/{currency} rate is known, so the amounts are in {view.currency} only"


@router.get("/ideas/{opportunity_id}")
def idea(opportunity_id: int, session: SignedIn, ctx: auth.Ctx) -> dict[str, Any]:
    """IdeaDetail: everything the idea page shows (prices downloaded at most every PAGE_PRICES_SECONDS)."""
    user = session.user
    opp = ctx.store.get_opportunity(opportunity_id)
    if opp is None:
        raise ApiError(404, message=NO_IDEA)
    now = ctx.now()
    view = ctx.view(opp, user)
    currency = user.settings.currency
    stored = ctx.store.opportunities(ticker=opp.ticker)  # newest first
    newest = stored[0].id if stored else opp.id
    history = stored[:IDEA_HISTORY]
    if all(item.id != opp.id for item in history):
        history = [*history[: IDEA_HISTORY - 1], opp]
    prices = idea_prices(ctx, opp, user, now=now)
    chart = chart_view(opp, prices)
    problem = prices.problem
    if chart is None and problem is None:
        problem = f"Yahoo Finance has no closing prices of {opp.ticker} for the chart."
    return {
        "generated_at": _iso(now),
        "idea": idea_summary(
            ctx,
            view,
            user,
            now=now,
            count=len(stored) or 1,
            newest=newest,
            watchlist=watchlist_of(user, ctx.config),
        ),
        "opportunity": raw_opportunity(opp, admin=user.is_admin),
        "levels": levels_view(view),
        "fx_note": fx_note(view, currency),
        "chart": chart,
        "prices_problem": problem,
        "outcome": outcome_view(prices.outcome) if prices.outcome is not None else None,
        "outcome_notes": list(prices.notes),
        "history": [history_item(item, newest=newest, current=opp.id) for item in history],
        "rule_misses": rule_misses(opp, user, ctx.config),
        "debate": debate_view(opp, admin=user.is_admin),
    }


# --- "Analyse again" -----------------------------------------------------------------------------------------------


def _limit_retry(ctx: AppContext, user: User, limit: int, now: datetime) -> int:
    """Seconds until the oldest of the user's analyses that count against the daily limit is 24 hours old."""
    jobs = ctx.accounts.list_jobs(user_id=user.id, limit=limit + 100)
    recent = sorted(job.created for job in jobs if job.created > now - DAY and job.counts)
    if len(recent) < limit or limit <= 0:
        return 60
    frees = recent[len(recent) - limit] + DAY
    return max(1, math.ceil((frees - utc(now)).total_seconds()))


@router.post("/ideas/{opportunity_id}/reanalyse", status_code=202)
def reanalyse(opportunity_id: int, session: SignedPost, ctx: auth.Ctx) -> JSONResponse:
    """ReanalyseAccepted (202): a manual analysis of the idea's ticker is queued, as with "Analyse now" (the same
    per-user daily limit and burst limit). An analysis of the ticker the reader already has waiting is returned."""
    user = session.user
    opp = ctx.store.get_opportunity(opportunity_id)
    if opp is None:
        raise ApiError(404, message=NO_IDEA)
    if not ctx.jobs.available:
        raise ApiError(503, "unavailable", pages.unavailable_note(ctx, user))
    try:
        job = ctx.jobs.submit(user, opp.ticker)
    except JobLimitError as exc:
        limit = ctx.jobs.limit_for(user)
        if limit == 0:
            raise ApiError(403, "forbidden", SWITCHED_OFF) from None
        if limit is not None and ctx.jobs.remaining(user) == 0:
            retry = _limit_retry(ctx, user, limit, ctx.now())
            raise ApiError(429, "limit_reached", str(exc), retry_after=retry) from None
        raise ApiError(429, "rate_limited", str(exc), retry_after=int(BURST_WINDOW.total_seconds())) from None
    except AccountError as exc:
        raise ApiError(400, "bad_request", str(exc)) from None
    status = job.status if job.status in ("queued", "running") else "queued"
    return JSONResponse({"job_id": job.id, "status": status, "ticker": job.ticker}, status_code=202)


@router.get("/jobs/{job_id}")
def job(job_id: int, session: SignedIn, ctx: auth.Ctx) -> dict[str, Any]:
    """Job: how a manual analysis is going (only its owner, or an admin, may see it)."""
    user = session.user
    found = ctx.accounts.get_job(job_id)
    if found is None or (found.user_id != user.id and not user.is_admin):
        raise ApiError(404, message=NO_JOB)
    ahead = None
    if found.status == "queued":
        ahead = sum(
            1
            for other in ctx.accounts.list_jobs(limit=200)
            if other.id < found.id and other.status in ("queued", "running")
        )
    return {
        "id": found.id,
        "ticker": found.ticker,
        "status": found.status,
        "created": _iso(found.created),
        "finished": _iso(found.finished),
        "opportunity_id": found.opportunity_id,
        "error": shown_error(found, user),
        "ahead": ahead,
        "remaining": ctx.jobs.remaining(user),
    }


# --- the reader, the scanner, thesis changes -----------------------------------------------------------------------


@router.get("/me")
def me(session: SignedIn, ctx: auth.Ctx) -> dict[str, Any]:
    """Me: who is signed in, their settings, the session's CSRF token and what they may do."""
    user = session.user
    chosen = user.settings
    analyse = _analyse_info(ctx, user)
    return {
        "user": {"id": user.id, "email": user.email, "name": user.name, "label": user.label, "role": user.role},
        "settings": {
            "currency": chosen.currency,
            "timezone": chosen.timezone,
            "watchlist": list(chosen.watchlist),
            "alert_rules": {
                "min_score": chosen.min_score,
                "min_probability": int(chosen.min_probability),
                "verdicts": list(dict.fromkeys(chosen.verdicts)),
                "only_watchlist": chosen.only_watchlist,
                "thesis_changes": chosen.thesis_changes,
            },
            "has_alert_channel": chosen.has_channel,
        },
        "csrf": session.csrf,
        "capabilities": {
            "admin": user.is_admin,
            "analyse": {
                "available": analyse["available"],
                "limit": analyse["limit"],
                "remaining": analyse["remaining"],
                "note": analyse["note"],
            },
        },
    }


@router.get("/status")
def status(session: SignedIn, ctx: auth.Ctx) -> dict[str, Any]:
    """Status: the scanner's status strip, with the model's use since 00:00 UTC."""
    now = ctx.now()
    found = ctx.control.status(now=now)
    if not session.user.is_admin:
        found = member_view(found)
    last = found.last_cycle
    feeds = None
    if last is not None and "feeds_ok" in last.stats:
        ok = max(0, int(last.stats.get("feeds_ok", 0)))
        feeds = {"ok": ok, "total": ok + max(0, int(last.stats.get("feeds_failed", 0)))}
    midnight = utc(now).replace(hour=0, minute=0, second=0, microsecond=0)
    usage = ctx.store.model_usage(since=midnight)
    waiting = found.cycle_started is None and found.state in ("running", "paused")
    return {
        "state": found.state,
        "label": STATE_LABELS.get(found.state, f"Scanner {found.state}"),
        "reason": found.reason,
        "interval_minutes": found.interval_minutes,
        "cycle_running_since": _iso(found.cycle_started),
        "next_cycle_at": _iso(found.next_cycle_at) if waiting else None,
        "last_cycle": None
        if last is None
        else {"started": _iso(last.started), "finished": _iso(last.finished), "ok": last.ok, "summary": last.summary},
        "feeds": feeds,
        "model_today": {
            "calls": sum(row.calls for row in usage),
            "input_tokens": sum(row.input_tokens for row in usage),
            "output_tokens": sum(row.output_tokens for row in usage),
            "since": _iso(midnight),
        },
    }


def thesis_side(opp: Opportunity) -> dict[str, Any]:
    """ThesisSide: one analysis of a thesis change, in the reader's currency (opp from Opportunity.in_currency)."""
    return {
        "id": opp.id,
        "created": _iso(opp.created),
        "verdict": opp.analysis.verdict,
        "verdict_label": verdict_label(opp.analysis.verdict),
        "probability_up_6m": int(opp.analysis.probability_up_6m),
        "score": opp.score,
        "score_band": _band(opp.score),
        "currency": opp.currency,
        "entry": money(opp.analysis.entry_price, opp),
        "target": money(opp.analysis.target_price, opp),
    }


@router.get("/thesis-changes")
def thesis_changes_list(request: Request, session: SignedIn, ctx: auth.Ctx) -> dict[str, Any]:
    """ThesisChanges: newer analyses of the last days (1-30, default 7) that undercut an earlier idea that passed the
    reader's rules (pages.thesis_changes), newest first."""
    user = session.user
    days = _choice(request.query_params.get("days"), range(1, MAX_THESIS_DAYS + 1), THESIS_DAYS)
    assert days is not None
    currency = user.settings.currency
    return {
        "days": days,
        "changes": [
            {
                "ticker": change.current.ticker,
                "company": change.current.company,
                "reason": change.reason,
                "current": thesis_side(change.current.in_currency(currency)),
                "previous": thesis_side(change.previous.in_currency(currency)),
            }
            for change in thesis_changes(ctx, user, now=ctx.now(), days=days)
        ],
    }
