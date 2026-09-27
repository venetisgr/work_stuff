"""Dip detection and candidate selection: which companies hit by the news actually dropped.

select_candidates turns the triaged impacts of a scan cycle into a short, ranked list of candidates for the (costly)
analysis model. Every ticker that doesn't make it is explained in a note, so nothing is dropped silently: the notes
are grouped by reason, one line per reason, naming each ticker with the detail that decided it.
"""

from __future__ import annotations

import logging
import math
from collections import Counter
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import DipConfig, ScannerConfig
from .models import Article, Candidate, Impact, Opportunity, PriceStats, utc
from .prices import PriceError

if TYPE_CHECKING:
    from .prices import YahooPrices
    from .store import Store

log = logging.getLogger(__name__)

# Tolerance for comparing drops with thresholds, so a drop of exactly 3% isn't missed through float rounding.
_EPSILON = 1e-9
# Yahoo's instrumentType of single-company shares (ADRs included); ETFs, funds and indices aren't candidates.
_EQUITY_TYPES = frozenset({"EQUITY"})
# Currencies Yahoo quotes in hundredths: London pence, Johannesburg cents, Tel Aviv agorot.
_MINOR_UNITS = {"GBp": 100, "GBX": 100, "ZAc": 100, "ILA": 100}


def major_units(price: float, currency: str) -> float:
    """A price in the currency's main unit: 150 GBp (pence) is 1.50 (pounds); other currencies are unchanged."""
    return price / _MINOR_UNITS.get((currency or "").strip(), 1)


# severity() weights, see its docstring.
_RELATION_WEIGHTS = {"direct": 1.0, "indirect": 0.5}
_DIRECTION_WEIGHTS = {"negative": 1.0, "mixed": 0.7}
_OTHER_DIRECTION_WEIGHT = 0.3  # "neutral" / "positive": only matter when [dip] directions includes them
_NEWS_WEIGHT = 1.5
_EXTRA_ARTICLE_BONUS = 0.5
_MAX_EXTRA_ARTICLE_BONUS = 2.0
_MAX_VOLUME_BONUS = 3.0


# --- dips ----------------------------------------------------------------------------------------------------------


def dip_reasons(stats: PriceStats, cfg: DipConfig, *, now: datetime | None = None) -> list[str]:
    """Human-readable reasons the price counts as a dip, e.g. "down 6.2% today"; an empty list means no dip.

    Three independent tests, each against its [dip] threshold (a positive number of percent):
    - the last session's change vs the previous close: "down 6.2% today" (min_drop_1d_pct), or "down 6.2% on Fri
      25 Sep" when that session isn't today on the exchange's calendar (weekends, holidays, before the open; the
      session date is the exchange's, now's too; without now it is taken to be today);
    - the change over 5 sessions: "down 8.1% over 5 days" (min_drop_5d_pct);
    - the distance below the 20-session high: "12.4% below its 20-day high" (min_drawdown_20d_pct).
    A test only counts when the price actually fell, so a threshold of 0 means "any fall".
    """
    reasons = []
    if _dropped(stats.change_1d_pct, cfg.min_drop_1d_pct):
        session = session_day(stats)
        when = "today" if now is None or session == _local_date(now, stats.timezone) else f"on {session:%a %d %b}"
        reasons.append(f"down {-stats.change_1d_pct:.1f}% {when}")
    if _dropped(stats.change_5d_pct, cfg.min_drop_5d_pct):
        reasons.append(f"down {-stats.change_5d_pct:.1f}% over 5 days")
    if _dropped(stats.drawdown_20d_pct, cfg.min_drawdown_20d_pct):
        reasons.append(f"{-stats.drawdown_20d_pct:.1f}% below its 20-day high")
    return reasons


def _dropped(change_pct: float, threshold_pct: float) -> bool:
    return change_pct < 0 and -change_pct >= abs(threshold_pct) - _EPSILON


def session_day(stats: PriceStats) -> date:
    """The date of the latest session in the price data, on the exchange's calendar (UTC without a time zone)."""
    return _local_date(stats.as_of, stats.timezone)


def _local_date(moment: datetime, zone: str | None) -> date:
    if zone:
        try:
            return utc(moment).astimezone(ZoneInfo(zone)).date()
        except (ZoneInfoNotFoundError, ValueError, OSError):
            pass
    return utc(moment).date()


def severity(stats: PriceStats, impacts: list[tuple[Impact, Article]]) -> float:
    """Ranking score for candidates: bigger drops and stronger, more direct negative news rank higher.

    severity = drop + news + corroboration + volume, rounded to 2 decimals, where
    - drop = max(0, -change_1d_pct, -change_5d_pct / 1.5, -drawdown_20d_pct / 2): the fall in percent, with the
      slower 5-day and 20-day measures scaled down to be comparable with a one-day drop;
    - news = 1.5 * the strongest impact's magnitude (1-5) * relation weight (direct 1.0, indirect 0.5)
      * direction weight (negative 1.0, mixed 0.7, neutral/positive 0.3);
    - corroboration = 0.5 for every further story about the company (articles with the same title_key, e.g.
      syndicated copies, count once), at most 2.0;
    - volume = min(3, volume_ratio - 1) when the last session traded above its 20-day average volume, else 0
      (heavy selling means the market is really reacting).
    Example: a 5% one-day drop that is 13.6% below the 20-day high (drop 6.82), one direct negative magnitude-4
    article (news 6.0) and 2.3x normal volume (volume 1.3) give 14.12.
    """
    drop = max(0.0, -stats.change_1d_pct, -stats.change_5d_pct / 1.5, -stats.drawdown_20d_pct / 2)
    news = max((_impact_weight(impact) for impact, _ in impacts), default=0.0)
    articles = len({article.title_key or article.id for _, article in impacts})
    corroboration = min(_MAX_EXTRA_ARTICLE_BONUS, _EXTRA_ARTICLE_BONUS * max(0, articles - 1))
    ratio = stats.volume_ratio
    volume = min(_MAX_VOLUME_BONUS, max(0.0, ratio - 1)) if ratio is not None and math.isfinite(ratio) else 0.0
    return round(drop + _NEWS_WEIGHT * news + corroboration + volume, 2)


def _impact_weight(impact: Impact) -> float:
    relation = _RELATION_WEIGHTS.get(impact.relation, _RELATION_WEIGHTS["indirect"])
    direction = _DIRECTION_WEIGHTS.get(impact.direction, _OTHER_DIRECTION_WEIGHT)
    return impact.magnitude * relation * direction


# --- candidate selection -------------------------------------------------------------------------------------------


class _Notes:
    """Skip notes grouped by reason: one line per reason naming every ticker it applied to, in first-seen order."""

    def __init__(self) -> None:
        self._groups: dict[str, list[str]] = {}

    def add(self, reason: str, item: str) -> None:
        self._groups.setdefault(reason, []).append(item)

    def lines(self) -> list[str]:
        return [f"{reason}: {', '.join(items)}" for reason, items in self._groups.items()]


def select_candidates(
    impacts: list[tuple[Impact, Article]],
    prices: YahooPrices,
    store: Store,
    cfg: ScannerConfig,
    *,
    now: datetime,
    waiting: Callable[[str], str | None] | None = None,
) -> tuple[list[Candidate], list[str]]:
    """Candidates sorted by severity (highest first, capped per cycle) and notes about every ticker left out.

    Steps, cheapest first:
    1. Impacts whose article was published before the lookback window ([scan] lookback_hours) are ignored.
    2. Tickers are checked against [universe]: exclude, only_watchlist and allowed_suffixes.
    3. Each ticker's impacts must match [dip]: directions, min_magnitude and include_indirect. Watchlist tickers
       skip the magnitude and relation tests (any relation, magnitude >= 1) but not the direction test.
    4. Cooldown: a ticker analysed less than [scan] cooldown_hours ago is skipped unless one of its qualifying
       articles was not part of that analysis, or that analysis came before any trading on its news (every
       article newer than its prices) and a newer session has traded since.
    5. waiting(ticker), when given, returns a reason while the ticker's last analysis failed recently (the
       pipeline's backoff); such tickers are skipped here, before the cap, so they don't take a place.
    6. Tickers the store marks invalid (no prices recently) are skipped without asking for prices. When the price
       source raises PriceError the ticker is marked invalid; other errors (network) are noted but don't mark it.
    7. Instruments that aren't company shares (Yahoo's instrumentType ETF, MUTUALFUND, INDEX...) are skipped and
       marked invalid; so are prices below [universe] min_price (in the currency's main unit: pence, cents and
       agorot quotes are divided by 100) and prices that don't pass dip_reasons.
    8. Same session: until a newer session has traded since the ticker's last analysis (evening or weekend news on
       the last close, more news later the same day), news that analysis didn't see re-analyses it at most once
       every [scan] reanalyse_same_session_hours, and the same news on the same prices isn't analysed again (see
       _same_session).
    The rest become candidates, sorted by severity; those beyond [scan] max_candidates_per_cycle are left for the
    next cycle and named in a note. When every qualifying article came out after the latest session in the price
    data (news after the close or at the weekend), the drop can't be a reaction to it, unless the news only reports
    an earlier event or the drop itself: the candidate is marked news_after_session and its reasons say that all of
    this news came out after the last session.
    """
    now = utc(now)
    scan, dip, universe = cfg.scan, cfg.dip, cfg.universe
    notes = _Notes()
    watchlist = set(universe.watchlist)
    excluded = set(universe.exclude)
    since = now - timedelta(hours=scan.lookback_hours)

    by_ticker: dict[str, list[tuple[Impact, Article]]] = {}
    seen: set[tuple[str, str]] = set()
    too_old: Counter[str] = Counter()
    for impact, article in impacts:
        ticker = impact.ticker.strip().upper()
        if (ticker, article.id) in seen:
            continue
        seen.add((ticker, article.id))
        if utc(article.published) < since:
            too_old[ticker] += 1
            continue
        by_ticker.setdefault(ticker, []).append((impact, article))
    for ticker, count in too_old.items():
        if ticker not in by_ticker:
            notes.add(
                f"No news in the last {_hours(scan.lookback_hours)} ([scan] lookback_hours)",
                f"{ticker} ({count} older)",
            )

    candidates: list[Candidate] = []
    for ticker, group in by_ticker.items():
        if ticker in excluded:
            notes.add("Excluded in [universe] exclude", ticker)
            continue
        if universe.only_watchlist and ticker not in watchlist:
            notes.add("Not on the watchlist ([universe] only_watchlist)", ticker)
            continue
        if universe.allowed_suffixes is not None and _suffix(ticker) not in universe.allowed_suffixes:
            allowed = ", ".join(repr(suffix) for suffix in universe.allowed_suffixes) or "none"
            notes.add(f"Exchange not in [universe] allowed_suffixes ({allowed})", ticker)
            continue

        qualifying, rejected = _qualifying(group, dip, on_watchlist=ticker in watchlist)
        if not qualifying:
            notes.add("No qualifying news ([dip] filters)", f"{ticker} ({'; '.join(rejected)})")
            continue
        qualifying.sort(key=lambda pair: (utc(pair[1].published), utc(pair[1].fetched)), reverse=True)

        stats: PriceStats | None = None
        wanted = scan.cooldown_hours > 0 or scan.reanalyse_same_session_hours > 0
        last = store.last_opportunity(ticker) if wanted else None
        if (cooldown := _cooldown(ticker, qualifying, last, scan.cooldown_hours, now)) is not None:
            reacted = False
            if last is not None and last.news_after_session:  # analysed before its news could move the price
                stats = _stats(ticker, prices, store, notes, now)
                if stats is None:
                    continue
                moved = abs(stats.change_1d_pct) >= dip.min_drop_1d_pct - _EPSILON  # the market's answer to it
                reacted = session_day(stats) > session_day(last.stats) and moved
            if not reacted:
                notes.add(f"Analysed within the {_hours(scan.cooldown_hours)} cooldown, no new news since", cooldown)
                continue

        if waiting is not None and (why := waiting(ticker)) is not None:
            notes.add("Analysis failed recently, waiting before trying again", why)
            continue

        if stats is None:
            stats = _stats(ticker, prices, store, notes, now)
        if stats is None:
            continue
        if stats.instrument_type is not None and stats.instrument_type not in _EQUITY_TYPES:
            store.set_ticker_valid(ticker, False, checked=now)
            notes.add("Not a company's shares (a fund, index or other instrument; marked invalid)", ticker)
            continue
        if major_units(stats.price, stats.currency) < universe.min_price:
            notes.add(
                f"Price below [universe] min_price {universe.min_price:g}",
                f"{ticker} ({_money(stats.price)} {stats.currency})",
            )
            continue
        reasons = dip_reasons(stats, dip, now=now)
        if not reasons:
            notes.add("No dip (price not down enough)", f"{ticker} ({_moves(stats)})")
            continue
        if (wait := _same_session(ticker, stats, last, qualifying, cfg, now)) is not None:
            notes.add(*wait)
            continue
        unpriced = news_after_session(stats, qualifying)
        if unpriced:
            reasons.append(f"all of this news came out after the last session ({session_day(stats):%a %d %b})")
        candidates.append(
            Candidate(
                ticker=ticker,
                company=_company(stats, qualifying),
                stats=stats,
                impacts=qualifying,
                dip_reasons=reasons,
                severity=severity(stats, qualifying),
                news_after_session=unpriced,
            )
        )

    candidates.sort(key=lambda candidate: (-candidate.severity, candidate.ticker))
    limit = max(0, scan.max_candidates_per_cycle)
    for candidate in candidates[limit:]:
        notes.add(
            f"Over the limit of {limit} candidates per cycle ([scan] max_candidates_per_cycle), "
            "left for the next cycle",
            f"{candidate.ticker} (severity {candidate.severity:.1f})",
        )
    selected = candidates[:limit]
    lines = notes.lines()
    for line in lines:
        log.debug("%s", line)
    log.info(
        "%d candidate(s) from %d ticker(s) with news in the last %s.",
        len(selected),
        len(by_ticker),
        _hours(scan.lookback_hours),
    )
    return selected, lines


def _qualifying(
    group: list[tuple[Impact, Article]], dip: DipConfig, *, on_watchlist: bool
) -> tuple[list[tuple[Impact, Article]], list[str]]:
    """Split a ticker's impacts into those that pass the [dip] news filters and the reasons for the others."""
    qualifying: list[tuple[Impact, Article]] = []
    rejected: Counter[str] = Counter()
    for impact, article in group:
        reason = _rejection(impact, dip, on_watchlist=on_watchlist)
        if reason is None:
            qualifying.append((impact, article))
        else:
            rejected[reason] += 1
    return qualifying, [f"{count}x {reason}" if count > 1 else reason for reason, count in rejected.items()]


def _rejection(impact: Impact, dip: DipConfig, *, on_watchlist: bool) -> str | None:
    if impact.direction not in dip.directions:
        return f"{impact.direction} news"
    if on_watchlist:
        return None
    if impact.relation == "indirect" and not dip.include_indirect:
        return "indirect news"
    if impact.magnitude < dip.min_magnitude:
        return f"magnitude {impact.magnitude} < {dip.min_magnitude}"
    return None


def news_after_session(stats: PriceStats, impacts: list[tuple[Impact, Article]]) -> bool:
    """Whether every article came out after the latest session in the price data, which has closed (so the drop
    can't be a reaction to them). While a session is running the quote is live, so this is False."""
    if not impacts or stats.session_elapsed is not None:
        return False
    return all(utc(article.published) > utc(stats.as_of) for _, article in impacts)


def _cooldown(
    ticker: str, impacts: list[tuple[Impact, Article]], last: Opportunity | None, cooldown_hours: float, now: datetime
) -> str | None:
    """A note item when the ticker is still in its cooldown with nothing new since its last analysis, else None."""
    if cooldown_hours <= 0 or last is None:
        return None
    created = utc(last.created)
    if created <= now - timedelta(hours=cooldown_hours):
        return None
    analysed = set(last.article_ids)
    for _, article in impacts:
        if article.id in analysed:
            continue
        # Qualifying news the last analysis never saw, however old it is: it may have been triaged a cycle late
        # (the model was unavailable). Records from before article_ids existed fall back to the time test.
        if analysed or max(utc(article.published), utc(article.fetched)) > created:
            return None
    ago = max(0.0, (now - created).total_seconds() / 3600)
    return f"{ticker} (analysed {ago:.1f}h ago)"


def _same_session(
    ticker: str,
    stats: PriceStats,
    last: Opportunity | None,
    impacts: list[tuple[Impact, Article]],
    cfg: ScannerConfig,
    now: datetime,
) -> tuple[str, str] | None:
    """(note reason, note item) while the ticker's last analysis was made on the same session's prices, else None.

    Until a newer session trades (evening or weekend news on the last close, or more news later in the same session):
    - news the last analysis didn't see analyses the ticker again at most once every [scan]
      reanalyse_same_session_hours; until then it waits, and is picked up after the next session or that time;
    - without such news (the cooldown ran out) and with nothing traded since, the same news on the same prices isn't
      analysed again at all.
    Nothing waits when the price fell by another [dip] min_drop_1d_pct since the last analysis (it was made while the
    session was running), or when reanalyse_same_session_hours is 0.
    """
    hours = cfg.scan.reanalyse_same_session_hours
    if last is None or hours <= 0 or session_day(stats) != session_day(last.stats):
        return None
    further = 1 - cfg.dip.min_drop_1d_pct / 100
    if last.stats.currency == stats.currency and stats.price <= last.stats.price * further + _EPSILON:
        return None
    created = utc(last.created)
    ago = max(0.0, (now - created).total_seconds() / 3600)
    item = f"{ticker} (analysed {ago:.1f}h ago, session {session_day(stats):%a %d %b})"
    analysed = set(last.article_ids)
    unseen = any(
        article.id not in analysed if analysed else max(utc(article.published), utc(article.fetched)) > created
        for _, article in impacts
    )
    if not unseen:
        if utc(stats.as_of) <= utc(last.stats.as_of):  # nothing has traded since: the very same inputs
            return (
                "Nothing new since the last analysis (the same news, no trading since), waiting for the next session",
                item,
            )
        return None
    if created > now - timedelta(hours=hours):
        return (
            "Already analysed on the latest session's prices, so new news waits for the next session or "
            f"{_hours(hours)} after that analysis ([scan] reanalyse_same_session_hours)",
            item,
        )
    return None


def _stats(ticker: str, prices: YahooPrices, store: Store, notes: _Notes, now: datetime) -> PriceStats | None:
    """The ticker's price statistics, or None (with a note) when there are none."""
    valid = store.ticker_valid(ticker, now=now)
    if valid is False:
        notes.add("No prices (marked invalid, rechecked after 7 days)", ticker)
        return None
    try:
        stats = prices.stats(ticker, now=now)
    except PriceError as exc:
        store.set_ticker_valid(ticker, False, checked=now)
        notes.add("No prices (unknown symbol or no data; marked invalid)", f"{ticker} ({_short(exc)})")
        return None
    except Exception as exc:  # network trouble etc.: not the ticker's fault, try again next cycle
        log.warning("Couldn't get prices for %s: %s", ticker, exc, exc_info=log.isEnabledFor(logging.DEBUG))
        notes.add("Couldn't get prices, will retry next cycle", f"{ticker} ({_short(exc)})")
        return None
    if valid is None:
        store.set_ticker_valid(ticker, True, checked=now)
    return stats


def _company(stats: PriceStats, impacts: list[tuple[Impact, Article]]) -> str:
    """The company name: Yahoo's name for the listing, else the name the triage used most (newest wins ties)."""
    if stats.name and stats.name.strip():
        return stats.name.strip()
    names = Counter(impact.company.strip() for impact, _ in impacts if impact.company.strip())
    return names.most_common(1)[0][0] if names else stats.ticker


def _suffix(ticker: str) -> str:
    """The Yahoo exchange suffix of a ticker (".DE" for "SAP.DE"), "" for US listings like "AMD" or "BRK-B"."""
    _, dot, suffix = ticker.rpartition(".")
    return f".{suffix.upper()}" if dot else ""


def _moves(stats: PriceStats) -> str:
    return (
        f"1d {stats.change_1d_pct:+.1f}%, 5d {stats.change_5d_pct:+.1f}%, "
        f"{stats.drawdown_20d_pct:+.1f}% from the 20-day high"
    )


def _hours(hours: float) -> str:
    return f"{hours:g}h"


def _money(value: float) -> str:
    return f"{value:.2f}" if abs(value) >= 1 else f"{value:.4g}"


def _short(exc: Exception, limit: int = 120) -> str:
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text if len(text) <= limit else text[: limit - 1] + "…"
