"""Member pages: the ideas (the dashboard), one idea, a ticker, the news and the track record.

Every handler is a plain `def` (they read SQLite and may download prices, so FastAPI runs them in its threadpool)
for signed-in users only, and renders in the reader's time zone and currency (app.render, AppContext.view).

Prices come from Yahoo Finance through ctx.prices (the scanner's own client in `serve`, so their caches are shared).
The idea and ticker pages download one stock's daily closes (kept PAGE_PRICES_SECONDS in ctx.cache); the track record
downloads one history per stock, per benchmark index and per exchange rate, kept an hour (TRACK_PRICES_SECONDS), a few
at a time. An answer that there are no prices for a symbol is kept as long as prices are; a download that fails is
tried again on the next visit. A failure costs only its own part of a page: the chart or the outcome says why, and the
rest is shown.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Hashable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any, TypeVar
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from ..accounts import AccountError, User
from ..config import ScannerConfig
from ..detect import dip_reasons
from ..fx import main_currency, same_money
from ..models import VERDICTS, Article, Impact, Opportunity, PriceBar, PriceStats, Split, utc
from ..pipeline import THESIS_DROP_POINTS, THESIS_WINDOW
from ..prices import PriceError, PriceFetchError
from ..recipients import preferred_symbols
from ..report import superseded_by, verdict_label
from ..symbols import current_symbol, symbol_aliases
from ..track import (
    STATUS_LABELS,
    Outcome,
    benchmark_for,
    evaluate,
    quote_day,
    signal_day,
    split_factor,
    summarize,
    with_account_return,
    with_benchmark,
)
from ..triage import normalise_ticker
from . import auth
from .account import change_watchlist
from .app import paginate, redirect, render
from .charts import Level, PriceChart, day_text, price_chart
from .context import AppContext

log = logging.getLogger(__name__)

router = APIRouter()

T = TypeVar("T")

# The dashboard
DAY_CHOICES = (1, 3, 7, 30)
DEFAULT_DAYS = 7
SCORE_CHOICES = (50, 65, 80)  # the report's score bands
SORTS = {"score": "Best score first", "new": "Newest first"}
PAGE_SIZE = 25
THESIS_DAYS = 7  # thesis changes of this many days are shown on the dashboard
THESIS_SHOWN = 5
# The news
NEWS_HOURS = (6, 24, 72)
DEFAULT_NEWS_HOURS = 24
NEWS_PAGE_SIZE = 30
NEWS_COMPANIES_SHOWN = 24
# The idea and ticker pages
CHART_DAYS = 183  # about 6 months of closes up to today
CHART_CONTEXT_DAYS = 21  # closes before the report's day when the report is older than that
PAGE_PRICES_SECONDS = 600
IDEA_HISTORY = 50  # other analyses of the same stock listed on an idea's page
TICKER_NEWS_DAYS = 30
TICKER_NEWS_SHOWN = 20
TICKER_IDEAS_SHOWN = 50
# The track record
TRACK_DAY_CHOICES = (30, 90, 180, 365, 730)
TRACK_DAY_LABELS = {30: "30 days", 90: "90 days", 180: "6 months", 365: "1 year", 730: "2 years"}
DEFAULT_TRACK_DAYS = 365
TRACK_PAGE_SIZE = 50
TRACK_PRICES_SECONDS = 3600
TRACK_WORKERS = 4  # price downloads at a time
INDEX_MARGIN_DAYS = 10  # index closes from this long before the first quote day

INDEX_NAMES = {
    "^GSPC": "S&P 500",
    "^GDAXI": "DAX",
    "^FCHI": "CAC 40",
    "FTSEMIB.MI": "FTSE MIB",
    "^AEX": "AEX",
    "^IBEX": "IBEX 35",
    "GD.AT": "Athens General",
    "^FTSE": "FTSE 100",
    "^STOXX50E": "Euro Stoxx 50",
    "^SSMI": "SMI",
    "^N225": "Nikkei 225",
    "^HSI": "Hang Seng",
    "^BFX": "BEL 20",
    "PSI20.LS": "PSI",
    "^OMX": "OMX Stockholm 30",
    "^OMXC25": "OMX Copenhagen 25",
    "^OMXH25": "OMX Helsinki 25",
    "OSEBX.OL": "Oslo Børs Benchmark",
    "^ATX": "ATX",
    "^ISEQ": "ISEQ Overall",
    "^GSPTSE": "S&P/TSX Composite",
    "^AXJO": "S&P/ASX 200",
    "^KS11": "KOSPI",
    "^TWII": "Taiwan Weighted",
    "000001.SS": "SSE Composite",
    "^BSESN": "BSE Sensex",
    "^NSEI": "Nifty 50",
    "^STI": "Straits Times",
    "^BVSP": "Bovespa",
    "^MXX": "IPC Mexico",
    "^TA125.TA": "TA-125",
}  # every index of track.BENCHMARKS (a test checks)
STATUS_BADGES = {
    "target_hit": "badge-ok",
    "below_low": "badge-bad",
    "open": "badge-info",
    "waiting_entry": "badge-outline",
    "expired": "badge-outline",
}
_DIRECTION_RANK = {"negative": 0, "mixed": 1, "positive": 2, "neutral": 3}
_MISSING = object()


# --- small helpers -------------------------------------------------------------------------------------------------


def _choice(value: object, choices: Sequence[int], default: int | None) -> int | None:
    """value as one of choices (a query parameter), else default."""
    try:
        number = int(str(value))
    except ValueError:
        return default
    return number if number in choices else default


def _flag(value: object) -> bool:
    return str(value or "").lower() in ("1", "on", "true", "yes")


def _utc_midnight(now: datetime) -> datetime:
    return utc(now).replace(hour=0, minute=0, second=0, microsecond=0)


def watchlist_of(user: User, config: ScannerConfig) -> tuple[str, ...]:
    """The user's watchlist as the scanner reads it (through [universe] preferred_listings)."""
    return preferred_symbols(user.settings.watchlist, config)


def rule_misses(opp: Opportunity, user: User, config: ScannerConfig) -> list[str]:
    """Why an idea wouldn't alert the user (their score, chance, verdicts and watchlist rules); [] when it would."""
    chosen = user.settings
    misses = []
    if opp.score < chosen.min_score:
        misses.append(f"its score {opp.score:.1f} is under your {chosen.min_score:g}")
    if opp.analysis.probability_up_6m < chosen.min_probability:
        misses.append(f"its chance up of {opp.analysis.probability_up_6m}% is under your {chosen.min_probability}%")
    if opp.analysis.verdict not in chosen.verdicts:
        misses.append(f"you don't alert on “{verdict_label(opp.analysis.verdict)}”")
    if chosen.only_watchlist and opp.ticker not in watchlist_of(user, config):
        misses.append("it isn't on your watchlist, and you alert only on those")
    return misses


def matches_rules(opp: Opportunity, user: User, config: ScannerConfig) -> bool:
    """Whether an idea passes the user's alert rules (what would reach them as an alert)."""
    return not rule_misses(opp, user, config)


def _passes(opp: Opportunity, user: User) -> bool:
    """The user's score, chance and verdict rules, the watchlist aside (as the scanner's thesis changes compare)."""
    chosen = user.settings
    return (
        opp.score >= chosen.min_score
        and opp.analysis.probability_up_6m >= chosen.min_probability
        and opp.analysis.verdict in chosen.verdicts
    )


def newest_per_ticker(opps: Sequence[Opportunity]) -> tuple[list[Opportunity], dict[str, int]]:
    """(the newest analysis of each ticker, newest first; how many analyses each ticker has in opps)."""
    newest: dict[str, Opportunity] = {}
    counts: dict[str, int] = {}
    for opp in opps:
        counts[opp.ticker] = counts.get(opp.ticker, 0) + 1
        current = newest.get(opp.ticker)
        if current is None or (utc(opp.created), opp.id or 0) > (utc(current.created), current.id or 0):
            newest[opp.ticker] = opp
    ordered = sorted(newest.values(), key=lambda opp: (utc(opp.created), opp.id or 0), reverse=True)
    return ordered, counts


def _analyse_info(ctx: AppContext, user: User) -> dict[str, Any]:
    """What the "Analyse now" buttons need: whether analyses run here, and how many the user has left."""
    remaining = ctx.jobs.remaining(user)
    note = None
    if not ctx.jobs.available:
        note = ctx.jobs.unavailable or "Manual analyses aren't available on this server."
    elif remaining == 0:
        limit = ctx.jobs.limit_for(user)
        note = (
            "Manual analyses are switched off for members on this server."
            if not limit
            else f"You have used your {limit} manual analyses of the last 24 hours."
        )
    return {"available": note is None, "note": note, "remaining": remaining, "limit": ctx.jobs.limit_for(user)}


# --- prices, cached ------------------------------------------------------------------------------------------------


def cached(ctx: AppContext, key: Hashable, fetch: Callable[[], T], *, seconds: float) -> T:
    """fetch()'s result, kept in ctx.cache for seconds. A PriceError (no prices for that symbol: asking again won't
    help) is kept too and raised again; a PriceFetchError (Yahoo unreachable) is not kept."""
    hit = ctx.cache.get(key, _MISSING)
    if isinstance(hit, PriceError):
        raise PriceError(str(hit))
    if hit is not _MISSING:
        return hit
    try:
        value = fetch()
    except PriceError as exc:
        ctx.cache.set(key, exc, seconds=seconds)
        raise
    ctx.cache.set(key, value, seconds=seconds)
    return value


def stock_history(
    ctx: AppContext, ticker: str, start: date, *, now: datetime, seconds: float
) -> tuple[list[PriceBar], list[Split]]:
    """A stock's daily bars from start to today and its splits (prices.YahooPrices.history_since), cached."""
    return cached(
        ctx,
        ("history", ticker, start.isoformat()),
        lambda: ctx.prices.history_since(ticker, start, now=now),
        seconds=seconds,
    )


def index_bars(ctx: AppContext, symbol: str, start: date, *, now: datetime, seconds: float) -> list[PriceBar]:
    """An index's daily bars from start to today, cached."""
    return cached(
        ctx, ("bars", symbol, start.isoformat()), lambda: ctx.prices.bars_since(symbol, start, now=now), seconds=seconds
    )


def fx_history(
    ctx: AppContext, currency: str, account: str, start: date, *, now: datetime, seconds: float
) -> list[tuple[date, float]]:
    """Daily closing exchange rates from currency into account (fx.FxRates.history), cached."""
    return cached(
        ctx,
        ("fx", main_currency(currency)[0], main_currency(account)[0], currency, start.isoformat()),
        lambda: ctx.fx.history(currency, account, start, now=now),
        seconds=seconds,
    )


def _problem(exc: Exception, what: str) -> str:
    """A download failure in words for a page."""
    if isinstance(exc, PriceError | PriceFetchError):
        return str(exc)
    log.warning("Couldn't get %s: %s", what, exc, exc_info=exc)
    return f"Couldn't get {what} because of an error on the server (it was logged)."


def _each(items: Sequence[T], work: Callable[[T], Any]) -> list[Any]:
    """work(item) for every item, TRACK_WORKERS at a time, in order; an exception is returned in the item's place."""

    def run(item: T) -> Any:
        try:
            return work(item)
        except Exception as exc:  # reported per item by the caller
            return exc

    if len(items) <= 1:
        return [run(item) for item in items]
    with ThreadPoolExecutor(max_workers=min(TRACK_WORKERS, len(items)), thread_name_prefix="dip-prices") as pool:
        return list(pool.map(run, items))


# --- the chart and the outcome of an idea ------------------------------------------------------------------------


def idea_levels(opp: Opportunity, factor: float = 1.0) -> list[Level]:
    """The chart lines of an idea, top to bottom (divided by factor after a split, like the price history)."""
    analysis = opp.analysis
    return [
        Level("target", "Target", analysis.target_price / factor, "the target (limit sell idea)"),
        Level("price", "Reported", opp.price / factor, "the price in the report"),
        Level("entry", "Entry", analysis.entry_price / factor, "the entry (limit buy)"),
        Level("stat-low", "Stat. low", opp.stats.stat_low_6m / factor, "the statistical 6-month low"),
        Level("low", "Low", analysis.potential_low / factor, "the potential low"),
    ]


def ladder(opp: Opportunity) -> list[dict[str, Any]]:
    """The idea's levels for the levels card (it is also the chart's legend), highest first: key, label, value and
    how far it is from the price in the report (None for the price itself)."""
    analysis = opp.analysis

    def change(value: float) -> float:
        return (value / opp.price - 1) * 100 if opp.price else 0.0

    rows = [
        {"key": "target", "label": "Target (limit sell idea)", "value": analysis.target_price},
        {"key": "price", "label": "Price in the report", "value": opp.price},
        {"key": "entry", "label": "Entry (limit buy)", "value": analysis.entry_price},
        {"key": "stat-low", "label": "Statistical 6-month low", "value": opp.stats.stat_low_6m},
        {"key": "low", "label": "Potential low", "value": analysis.potential_low},
    ]
    for row in rows:
        row["change"] = None if row["key"] == "price" else change(row["value"])
    return sorted(rows, key=lambda row: row["value"], reverse=True)


def chart_start(today: date, report_day: date | None = None) -> date:
    """The first day of a chart: about 6 months before today, or a few weeks before an older report."""
    start = today - timedelta(days=CHART_DAYS)
    if report_day is not None:
        start = min(start, report_day - timedelta(days=CHART_CONTEXT_DAYS))
    return start


@dataclass
class IdeaPrices:
    """What the idea page shows from the price history: the chart, the outcome so far and what went wrong."""

    chart: PriceChart | None = None
    outcome: Outcome | None = None
    split: float = 1.0
    problem: str | None = None  # the price history couldn't be loaded
    notes: list[str] = field(default_factory=list)  # the index or the exchange rates couldn't be loaded

    @property
    def split_note(self) -> str | None:
        """What a split since the report changed on the chart, or None."""
        if self.split == 1:
            return None
        split = f"a {self.split:g}:1 split" if self.split > 1 else f"a 1:{1 / self.split:g} reverse split"
        return (
            f"After {split} since the report, its prices and levels are shown divided by {self.split:g}, like "
            "Yahoo Finance's price history."
        )


def idea_prices(ctx: AppContext, opp: Opportunity, user: User, *, now: datetime) -> IdeaPrices:
    """The chart and the outcome of an idea (track.evaluate, with its index and the user's currency)."""
    found = IdeaPrices()
    report_day = signal_day(opp)
    start = chart_start(utc(now).date(), report_day)
    try:
        bars, splits = stock_history(ctx, opp.ticker, min(start, quote_day(opp)), now=now, seconds=PAGE_PRICES_SECONDS)
    except Exception as exc:  # the rest of the page still shows
        found.problem = _problem(exc, f"the prices of {opp.ticker}")
        return found
    found.split = split_factor(opp, splits)
    found.chart = price_chart(
        [(bar.day, bar.close) for bar in bars if bar.day >= start],
        currency=opp.currency,
        levels=idea_levels(opp, found.split),
        marker=report_day,
        marker_value=opp.price / found.split,
        name=opp.ticker,
        chart_id=f"idea-{opp.id}",
    )
    outcome = evaluate(opp, bars, now=now, splits=splits)
    if outcome.priced and not outcome.price_mismatch:
        symbol = benchmark_for(opp.ticker)
        try:
            index = index_bars(
                ctx, symbol, quote_day(opp) - timedelta(days=INDEX_MARGIN_DAYS), now=now, seconds=PAGE_PRICES_SECONDS
            )
        except Exception as exc:
            found.notes.append(f"No prices for the index {index_name(symbol)}: {_problem(exc, symbol)}")
            index = []
        outcome = with_benchmark(outcome, symbol, index)
        currency = user.settings.currency
        if currency:
            rates = None
            if not same_money(opp.currency, currency):
                try:
                    rates = fx_history(
                        ctx, opp.currency, currency, quote_day(opp), now=now, seconds=PAGE_PRICES_SECONDS
                    )
                except Exception as exc:
                    main = main_currency(opp.currency)[0]
                    found.notes.append(f"No {main}/{currency} exchange rates: {_problem(exc, 'exchange rates')}")
            outcome = with_account_return(outcome, currency, rates)
    found.outcome = outcome
    return found


def index_name(symbol: str | None) -> str:
    """ "S&P 500" for ^GSPC; the symbol for indices without a known name."""
    return INDEX_NAMES.get(symbol or "", symbol or "the index")


# --- thesis changes ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ThesisChange:
    """A newer analysis that undercuts an earlier idea the user's rules liked (see the scanner's thesis changes)."""

    previous: Opportunity
    current: Opportunity
    reason: str


def thesis_changes(ctx: AppContext, user: User, *, now: datetime, days: int = THESIS_DAYS) -> list[ThesisChange]:
    """The tickers analysed in the last days whose newest analysis undercuts an earlier one (within 6 months) that
    passed the user's rules: it no longer passes them, or its chance of being higher fell by THESIS_DROP_POINTS or
    more. Newest first. The same test as the scanner's "thesis change" notices, whether or not the user has alerts."""
    recent, _ = newest_per_ticker(ctx.store.opportunities(since=now - timedelta(days=days)))
    changes = []
    for current in recent:
        earlier = [
            opp
            for opp in ctx.store.opportunities(ticker=current.ticker, since=now - THESIS_WINDOW, limit=IDEA_HISTORY)
            if (utc(opp.created), opp.id or 0) < (utc(current.created), current.id or 0)
        ]
        previous = next((opp for opp in earlier if _passes(opp, user)), None)
        if previous is None:
            continue
        drop = previous.analysis.probability_up_6m - current.analysis.probability_up_6m
        if not _passes(current, user):
            reason = "no longer passes your alert rules"
        elif drop >= THESIS_DROP_POINTS:
            reason = f"its chance of being higher fell by {drop} points"
        else:
            continue
        changes.append(ThesisChange(previous=previous, current=current, reason=reason))
    return changes


# --- the dashboard -------------------------------------------------------------------------------------------------


@router.get("/")
def dashboard(request: Request, user: auth.SignedIn, ctx: auth.Ctx) -> Response:
    """The ideas: the newest analysis of every stock analysed in the last days, filtered and ranked, with the
    scanner's status and recent thesis changes."""
    now = ctx.now()
    query = request.query_params
    days = _choice(query.get("days"), DAY_CHOICES, DEFAULT_DAYS)
    min_score = _choice(query.get("score"), SCORE_CHOICES, None)
    verdict = query.get("verdict") if query.get("verdict") in VERDICTS else None
    only_watchlist = _flag(query.get("watchlist"))
    only_rules = _flag(query.get("rules"))
    sort = query.get("sort") if query.get("sort") in SORTS else "score"

    ideas, counts = newest_per_ticker(ctx.store.opportunities(since=now - timedelta(days=days)))
    total = len(ideas)
    watchlist = watchlist_of(user, ctx.config)
    if min_score is not None:
        ideas = [opp for opp in ideas if opp.score >= min_score]
    if verdict:
        ideas = [opp for opp in ideas if opp.analysis.verdict == verdict]
    if only_watchlist:
        ideas = [opp for opp in ideas if opp.ticker in watchlist]
    if only_rules:
        ideas = [opp for opp in ideas if matches_rules(opp, user, ctx.config)]
    if sort == "score":
        ideas.sort(key=lambda opp: opp.score, reverse=True)
    page = paginate(len(ideas), query.get("page"), PAGE_SIZE)
    currency = user.settings.currency
    narrowed = any(value for value in (min_score, verdict, only_watchlist, only_rules)) or sort != "score"
    return render(
        request,
        "pages/dashboard.html",
        {
            "status": ctx.control.status(now=now),
            "calls_today": sum(row.calls for row in ctx.store.model_usage(since=_utc_midnight(now))),
            "ideas": [opp.in_currency(currency) for opp in page.slice(ideas)],
            "page": page,
            "counts": counts,
            "total": total,
            "shown": len(ideas),
            "watchlist": watchlist,
            "matching": {opp.id for opp in ideas if matches_rules(opp, user, ctx.config)},
            "changes": thesis_changes(ctx, user, now=now)[:THESIS_SHOWN],
            "filters": {
                "days": days,
                "score": min_score,
                "verdict": verdict,
                "watchlist": only_watchlist,
                "rules": only_rules,
                "sort": sort,
                "open": narrowed,
                "active": narrowed or days != DEFAULT_DAYS,
            },
            "day_choices": DAY_CHOICES,
            "default_days": DEFAULT_DAYS,
            "score_choices": SCORE_CHOICES,
            "sorts": SORTS,
            "nav": "ideas",
            "page_title": "Ideas",
        },
    )


# --- an idea -------------------------------------------------------------------------------------------------------


@router.get("/ideas/{opportunity_id}")
def idea(request: Request, opportunity_id: int, user: auth.SignedIn, ctx: auth.Ctx) -> Response:
    """Everything about one idea: verdict, score, chance, levels (and ≈ in the user's currency), the chart, the
    outcome so far, the analysis, headlines and the other analyses of the stock."""
    opp = ctx.store.get_opportunity(opportunity_id)
    if opp is None:
        raise HTTPException(status_code=404, detail="There is no idea with that number. It may have been removed.")
    now = ctx.now()
    view = ctx.view(opp, user)
    currency = user.settings.currency
    history = ctx.store.opportunities(ticker=opp.ticker, limit=IDEA_HISTORY)  # newest first
    newest = history[0] if history else opp
    prices = idea_prices(ctx, opp, user, now=now)
    return render(
        request,
        "pages/idea.html",
        {
            "opp": view,
            "newer": newest if newest.id != opp.id else None,
            "history": [item.in_currency(currency) for item in history],
            "ladder": ladder(opp),
            "prices": prices,
            "outcome": prices.outcome,
            "status_labels": STATUS_LABELS,
            "status_badges": STATUS_BADGES,
            "index_name": index_name(prices.outcome.benchmark if prices.outcome else None),
            "misses": rule_misses(opp, user, ctx.config),
            "on_watchlist": opp.ticker in user.settings.watchlist,
            "analyse": _analyse_info(ctx, user),
            "nav": "ideas",
            "page_title": f"{opp.ticker}: {verdict_label(opp.analysis.verdict)}",
        },
    )


# --- a ticker ------------------------------------------------------------------------------------------------------


def _symbol_or_404(symbol: str) -> str:
    wanted = normalise_ticker(symbol)
    if wanted is None:
        raise HTTPException(status_code=404, detail=not_a_symbol(symbol))
    return wanted


def not_a_symbol(text: str) -> str:
    return f"{text.strip()[:20]!r} isn't a Yahoo Finance symbol; write it like AMD, SAP.DE or ALWN.AT."


def ticker_path(symbol: str) -> str:
    return f"/tickers/{quote(symbol, safe='')}"


@router.get("/tickers")
def find_ticker(request: Request, user: auth.SignedIn, symbol: str = "") -> Response:
    """The "Look up a ticker" form: goes to the ticker's page."""
    if not symbol.strip():
        return redirect(request, "/", "Type a Yahoo Finance symbol, like AMD, SAP.DE or ALWN.AT.", kind="error")
    wanted = normalise_ticker(symbol)
    if wanted is None:
        return redirect(request, "/", not_a_symbol(symbol), kind="error")
    return redirect(request, ticker_path(wanted))


@router.get("/tickers/{symbol}")
def ticker(request: Request, symbol: str, user: auth.SignedIn, ctx: auth.Ctx) -> Response:
    """A stock: its price statistics and whether they count as a dip, the chart, its news and its ideas; watchlist
    and "Analyse now" buttons."""
    wanted = _symbol_or_404(symbol)
    if wanted != symbol:
        return redirect(request, ticker_path(wanted))
    now = ctx.now()
    preferred = ctx.config.universe.preferred_listings
    stats: PriceStats | None = None
    price_problem = None
    try:
        stats = ctx.prices.stats(wanted, now=now)
    except Exception as exc:
        price_problem = _problem(exc, f"the prices of {wanted}")
    dip = dip_reasons(stats, ctx.config.dip, now=now) if stats is not None else []
    ideas = ctx.store.opportunities(ticker=wanted, limit=TICKER_IDEAS_SHOWN)
    news = ctx.store.news(
        now - timedelta(days=TICKER_NEWS_DAYS), wanted, also=symbol_aliases(ctx.store, wanted, preferred, now=now)
    )[:TICKER_NEWS_SHOWN]

    chart = None
    chart_problem = None
    caption = None
    latest = ideas[0] if ideas else None
    today = utc(now).date()
    start = chart_start(today)
    try:
        bars, splits = stock_history(ctx, wanted, start, now=now, seconds=PAGE_PRICES_SECONDS)
    except Exception as exc:
        chart_problem = _problem(exc, f"the prices of {wanted}")
    else:
        levels: list[Level] = []
        marker = None
        marker_value = None
        if latest is not None and signal_day(latest) >= start:
            factor = split_factor(latest, splits)
            levels = [level for level in idea_levels(latest, factor) if level.key != "stat-low"]
            marker, marker_value = signal_day(latest), latest.price / factor
            caption = (
                f"The lines are the levels of the newest idea, reported {day_text(marker)}: its target, price, "
                "entry and potential low."
            )
        chart = price_chart(
            [(bar.day, bar.close) for bar in bars],
            currency=stats.currency if stats is not None else (latest.currency if latest else None),
            levels=levels,
            marker=marker,
            marker_value=marker_value,
            name=wanted,
            chart_id=f"ticker-{wanted}",
        )
    company = (stats.name if stats is not None else None) or (latest.company if latest else None)
    if not company:
        company = next((impact.company for _, impacts in news for impact in impacts if impact.company), None)
    currency = user.settings.currency
    return render(
        request,
        "pages/ticker.html",
        {
            "symbol": wanted,
            "company": company,
            "stats": stats,
            "price_problem": price_problem,
            "dip": dip,
            "chart": chart,
            "chart_problem": chart_problem,
            "chart_caption": caption,
            "dip_rules": ctx.config.dip,
            "ideas": [opp.in_currency(currency) for opp in ideas],
            "news": news,
            "news_days": TICKER_NEWS_DAYS,
            "preferred": preferred.get(wanted),
            "on_watchlist": wanted in user.settings.watchlist,
            "analyse": _analyse_info(ctx, user),
            "nav": "ideas",
            "page_title": wanted,
        },
    )


@router.post("/tickers/{symbol}/watchlist")
def change_watchlist_page(
    request: Request, symbol: str, user: auth.SignedIn, form: auth.SignedForm, ctx: auth.Ctx
) -> Response:
    """Add a ticker to the user's watchlist or take it off (form field action: "add" or "remove")."""
    wanted = _symbol_or_404(symbol)
    back = auth.safe_next(form.get("next"), default=ticker_path(wanted))
    action = form.get("action")
    try:
        if action == "add":
            change_watchlist(ctx, user, add=wanted)
            message = f"{wanted} is on your watchlist now: any negative news about it can make it a candidate."
        elif action == "remove":
            change_watchlist(ctx, user, remove=wanted)
            message = f"{wanted} is off your watchlist."
        else:
            return redirect(request, back, "Choose to add the ticker or to remove it.", kind="error")
    except AccountError as exc:
        return redirect(request, back, str(exc), kind="error")
    return redirect(request, back, message)


# --- the news ------------------------------------------------------------------------------------------------------


def _relabelled(impacts: list[Impact], current: Callable[[str], str]) -> list[Impact]:
    """The impacts under the symbols a scan reads them as, one per symbol (the first)."""
    result: dict[str, Impact] = {}
    for impact in impacts:
        symbol = current(impact.ticker)
        if symbol not in result:
            result[symbol] = impact if symbol == impact.ticker else replace(impact, ticker=symbol)
    return list(result.values())


def news_companies(items: Sequence[tuple[Article, list[Impact]]]) -> list[dict[str, Any]]:
    """The companies in the news, most worrying first (the digest's order: negative news, then mixed, positive and
    neutral; the biggest impact, then the most articles): ticker, company, direction, magnitude, count."""
    groups: dict[str, list[Impact]] = {}
    for _, impacts in items:
        for impact in impacts:
            groups.setdefault(impact.ticker, []).append(impact)
    rows = []
    for ticker, impacts in groups.items():
        lead = min(_DIRECTION_RANK.get(impact.direction, 4) for impact in impacts)
        strongest = max(
            (impact for impact in impacts if _DIRECTION_RANK.get(impact.direction, 4) == lead),
            key=lambda impact: impact.magnitude,
        )
        company = next((impact.company for impact in impacts if impact.company), "")
        rows.append(
            {
                "ticker": ticker,
                "company": company,
                "direction": strongest.direction,
                "magnitude": strongest.magnitude,
                "count": len(impacts),
                "rank": (lead, -strongest.magnitude, -len(impacts), ticker),
            }
        )
    return sorted(rows, key=lambda row: row["rank"])


@router.get("/news")
def news(request: Request, user: auth.SignedIn, ctx: auth.Ctx) -> Response:
    """The news digest ("newsletter"): the articles of the last hours with the companies the triage found in them,
    newest first, and the companies in the news, most worrying first."""
    now = ctx.now()
    query = request.query_params
    hours = _choice(query.get("hours"), NEWS_HOURS, DEFAULT_NEWS_HOURS)
    only_companies = _flag(query.get("companies"))
    typed = (query.get("ticker") or "").strip()[:30]
    ticker = normalise_ticker(typed) if typed else None
    problem = None
    if typed and ticker is None:
        problem = f"{not_a_symbol(typed)} Showing every company."
    preferred = ctx.config.universe.preferred_listings
    if ticker in preferred:
        ticker = preferred[ticker]
    since = now - timedelta(hours=hours)
    if ticker:
        items = ctx.store.news(since, ticker, also=symbol_aliases(ctx.store, ticker, preferred, now=now))
    else:
        symbols: dict[str, str] = {}

        def current(symbol: str) -> str:
            if symbol not in symbols:
                symbols[symbol] = current_symbol(ctx.store, symbol, preferred, now=now)
            return symbols[symbol]

        items = [(article, _relabelled(impacts, current)) for article, impacts in ctx.store.news(since)]
    everything = len(items)
    sources = {article.source_name or article.source for article, _ in items}
    if only_companies:
        items = [item for item in items if item[1]]
    companies = news_companies(items)
    page = paginate(len(items), query.get("page"), NEWS_PAGE_SIZE)
    return render(
        request,
        "pages/news.html",
        {
            "items": page.slice(items),
            "page": page,
            "everything": everything,
            "sources": len(sources),
            "companies": companies[:NEWS_COMPANIES_SHOWN],
            "more_companies": max(0, len(companies) - NEWS_COMPANIES_SHOWN),
            "company_count": len(companies),
            "hours": hours,
            "hour_choices": NEWS_HOURS,
            "default_hours": DEFAULT_NEWS_HOURS,
            "ticker": ticker,
            "typed": typed,
            "only_companies": only_companies,
            "problem": problem,
            "nav": "news",
            "page_title": "News",
        },
    )


# --- the track record ----------------------------------------------------------------------------------------------


@dataclass
class TrackRecord:
    """The outcomes of the stored ideas with their summary (track.summarize) and what couldn't be measured."""

    outcomes: list[Outcome]
    summary: dict
    missing: list[tuple[str, int]] = field(default_factory=list)  # tickers without prices: (ticker, ideas)
    problems: list[str] = field(default_factory=list)  # downloads that failed this time
    notes: list[str] = field(default_factory=list)  # an index or exchange rates without prices


def track_record(ctx: AppContext, opps: Sequence[Opportunity], *, currency: str | None, now: datetime) -> TrackRecord:
    """How the ideas played out (as `dip-scanner track`), next to their exchange's index and in currency."""
    by_ticker: dict[str, list[Opportunity]] = {}
    for opp in opps:
        by_ticker.setdefault(opp.ticker, []).append(opp)
    tickers = list(by_ticker)

    def history(ticker: str) -> tuple[list[PriceBar], list[Split]]:
        start = min(quote_day(opp) for opp in by_ticker[ticker])
        return stock_history(ctx, ticker, start, now=now, seconds=TRACK_PRICES_SECONDS)

    record = TrackRecord(outcomes=[], summary={})
    for ticker, found in zip(tickers, _each(tickers, history), strict=True):
        group = by_ticker[ticker]
        if isinstance(found, PriceError):  # delisted, renamed...: named, never silently dropped
            record.missing.append((ticker, len(group)))
        elif isinstance(found, Exception):
            record.problems.append(f"{ticker} ({_ideas(len(group))}): {_problem(found, f'the prices of {ticker}')}")
        else:
            bars, splits = found
            record.outcomes.extend(evaluate(opp, bars, now=now, splits=splits) for opp in group)

    outcomes = record.outcomes
    by_index: dict[str, list[int]] = {}
    for position, outcome in enumerate(outcomes):
        by_index.setdefault(benchmark_for(outcome.opportunity.ticker), []).append(position)
    wanted = [symbol for symbol, positions in by_index.items() if any(outcomes[i].priced for i in positions)]

    def index(symbol: str) -> list[PriceBar]:
        first = min(quote_day(outcomes[i].opportunity) for i in by_index[symbol])
        return index_bars(ctx, symbol, first - timedelta(days=INDEX_MARGIN_DAYS), now=now, seconds=TRACK_PRICES_SECONDS)

    for symbol, found in zip(wanted, _each(wanted, index), strict=True):
        bars: list[PriceBar] = []
        if isinstance(found, Exception):
            count = len(by_index[symbol])
            record.notes.append(
                f"No prices for the index {index_name(symbol)} ({symbol}), so {_ideas(count)} show – next to it: "
                f"{_problem(found, symbol)}"
            )
        else:
            bars = found
        for position in by_index[symbol]:
            outcomes[position] = with_benchmark(outcomes[position], symbol, bars)
    for symbol, positions in by_index.items():
        if symbol not in wanted:
            for position in positions:
                outcomes[position] = with_benchmark(outcomes[position], symbol, [])

    if currency:
        by_currency: dict[str, list[int]] = {}
        for position, outcome in enumerate(outcomes):
            by_currency.setdefault(outcome.opportunity.currency, []).append(position)
        needed = [
            code
            for code, positions in by_currency.items()
            if not same_money(code, currency) and any(outcomes[i].priced for i in positions)
        ]

        def rates(code: str) -> list[tuple[date, float]]:
            first = min(quote_day(outcomes[i].opportunity) for i in by_currency[code])
            return fx_history(ctx, code, currency, first, now=now, seconds=TRACK_PRICES_SECONDS)

        found_rates = dict(zip(needed, _each(needed, rates), strict=True))
        for code, positions in by_currency.items():
            table = found_rates.get(code)
            if isinstance(table, Exception):
                record.notes.append(
                    f"No {main_currency(code)[0]}/{currency} exchange rates, so {_ideas(len(positions))} show – in "
                    f"{currency}: {_problem(table, 'exchange rates')}"
                )
                table = None
            for position in positions:
                outcomes[position] = with_account_return(outcomes[position], currency, table)
    record.summary = summarize(outcomes)
    return record


def _ideas(count: int) -> str:
    return f"{count} idea{'s' if count != 1 else ''}"


def _group_rows(summary: dict) -> tuple[list[tuple[str, dict]], list[tuple[str, dict]]]:
    by_verdict = [(verdict_label(verdict), group) for verdict, group in (summary.get("by_verdict") or {}).items()]
    by_score = [(label, group) for label, group in (summary.get("by_score") or {}).items() if group.get("count")]
    return by_verdict, by_score


@router.get("/track")
def track(request: Request, user: auth.SignedIn, ctx: auth.Ctx) -> Response:
    """The track record: how the ideas of the last days played out against their limit orders, their exchange's
    index and in the user's currency (prices downloaded at most once an hour)."""
    now = ctx.now()
    query = request.query_params
    days = _choice(query.get("days"), TRACK_DAY_CHOICES, DEFAULT_TRACK_DAYS)
    currency = user.settings.currency
    opps = ctx.store.opportunities(since=now - timedelta(days=days))
    record = track_record(ctx, opps, currency=currency, now=now) if opps else TrackRecord(outcomes=[], summary={})
    ordered = sorted(record.outcomes, key=lambda o: (utc(o.opportunity.created), o.opportunity.id or 0), reverse=True)
    newer = superseded_by([outcome.opportunity for outcome in ordered])
    page = paginate(len(ordered), query.get("page"), TRACK_PAGE_SIZE)
    rows = [
        {"outcome": outcome, "newer": newer.get(page.offset + index)}
        for index, outcome in enumerate(page.slice(ordered))
    ]
    by_verdict, by_score = _group_rows(record.summary)
    return render(
        request,
        "pages/track.html",
        {
            "record": record,
            "summary": record.summary,
            "rows": rows,
            "page": page,
            "stored": len(opps),
            "waiting": sum(1 for o in record.outcomes if not o.priced and not o.price_mismatch),
            "by_verdict": by_verdict,
            "by_score": by_score,
            "days": days,
            "since": now - timedelta(days=days),
            "day_choices": TRACK_DAY_CHOICES,
            "day_labels": TRACK_DAY_LABELS,
            "default_days": DEFAULT_TRACK_DAYS,
            "account": currency,
            "status_labels": STATUS_LABELS,
            "status_badges": STATUS_BADGES,
            "index_names": INDEX_NAMES,
            "nav": "track",
            "page_title": "Track record",
        },
    )
