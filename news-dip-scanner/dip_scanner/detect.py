"""Dip detection and candidate selection: which companies hit by the news actually dropped.

select_candidates turns the triaged impacts of a scan cycle into a short, ranked list of candidates for the (costly)
analysis model. Every ticker that doesn't make it is explained in a note, so nothing is dropped silently: the notes
are grouped by reason, one line per reason, naming each ticker with the detail that decided it.
"""

from __future__ import annotations

import logging
import math
from collections import Counter
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from .config import DipConfig, ScannerConfig
from .models import Article, Candidate, Impact, PriceStats, utc
from .prices import PriceError

if TYPE_CHECKING:
    from .prices import YahooPrices
    from .store import Store

log = logging.getLogger(__name__)

# Tolerance for comparing drops with thresholds, so a drop of exactly 3% isn't missed through float rounding.
_EPSILON = 1e-9

# severity() weights, see its docstring.
_RELATION_WEIGHTS = {"direct": 1.0, "indirect": 0.5}
_DIRECTION_WEIGHTS = {"negative": 1.0, "mixed": 0.7}
_OTHER_DIRECTION_WEIGHT = 0.3  # "neutral" / "positive": only matter when [dip] directions includes them
_NEWS_WEIGHT = 1.5
_EXTRA_ARTICLE_BONUS = 0.5
_MAX_EXTRA_ARTICLE_BONUS = 2.0
_MAX_VOLUME_BONUS = 3.0


# --- dips ----------------------------------------------------------------------------------------------------------


def dip_reasons(stats: PriceStats, cfg: DipConfig) -> list[str]:
    """Human-readable reasons the price counts as a dip, e.g. "down 6.2% today"; an empty list means no dip.

    Three independent tests, each against its [dip] threshold (a positive number of percent):
    - the last session's change vs the previous close: "down 6.2% today" (min_drop_1d_pct);
    - the change over 5 sessions: "down 8.1% over 5 days" (min_drop_5d_pct);
    - the distance below the 20-session high: "12.4% below its 20-day high" (min_drawdown_20d_pct).
    A test only counts when the price actually fell, so a threshold of 0 means "any fall".
    """
    reasons = []
    if _dropped(stats.change_1d_pct, cfg.min_drop_1d_pct):
        reasons.append(f"down {-stats.change_1d_pct:.1f}% today")
    if _dropped(stats.change_5d_pct, cfg.min_drop_5d_pct):
        reasons.append(f"down {-stats.change_5d_pct:.1f}% over 5 days")
    if _dropped(stats.drawdown_20d_pct, cfg.min_drawdown_20d_pct):
        reasons.append(f"{-stats.drawdown_20d_pct:.1f}% below its 20-day high")
    return reasons


def _dropped(change_pct: float, threshold_pct: float) -> bool:
    return change_pct < 0 and -change_pct >= abs(threshold_pct) - _EPSILON


def severity(stats: PriceStats, impacts: list[tuple[Impact, Article]]) -> float:
    """Ranking score for candidates: bigger drops and stronger, more direct negative news rank higher.

    severity = drop + news + corroboration + volume, rounded to 2 decimals, where
    - drop = max(0, -change_1d_pct, -change_5d_pct / 1.5, -drawdown_20d_pct / 2): the fall in percent, with the
      slower 5-day and 20-day measures scaled down to be comparable with a one-day drop;
    - news = 1.5 * the strongest impact's magnitude (1-5) * relation weight (direct 1.0, indirect 0.5)
      * direction weight (negative 1.0, mixed 0.7, neutral/positive 0.3);
    - corroboration = 0.5 for every further article about the company, at most 2.0;
    - volume = min(3, volume_ratio - 1) when the last session traded above its 20-day average volume, else 0
      (heavy selling means the market is really reacting).
    Example: a 5% one-day drop that is 13.6% below the 20-day high (drop 6.82), one direct negative magnitude-4
    article (news 6.0) and 2.3x normal volume (volume 1.3) give 14.12.
    """
    drop = max(0.0, -stats.change_1d_pct, -stats.change_5d_pct / 1.5, -stats.drawdown_20d_pct / 2)
    news = max((_impact_weight(impact) for impact, _ in impacts), default=0.0)
    articles = len({article.id for _, article in impacts})
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
) -> tuple[list[Candidate], list[str]]:
    """Candidates sorted by severity (highest first, capped per cycle) and notes about every ticker left out.

    Steps, cheapest first:
    1. Impacts whose article was published before the lookback window ([scan] lookback_hours) are ignored.
    2. Tickers are checked against [universe]: exclude, only_watchlist and allowed_suffixes.
    3. Each ticker's impacts must match [dip]: directions, min_magnitude and include_indirect. Watchlist tickers
       skip the magnitude and relation tests (any relation, magnitude >= 1) but not the direction test.
    4. Cooldown: a ticker analysed less than [scan] cooldown_hours ago is skipped unless one of its qualifying
       articles was published or fetched after that analysis.
    5. Tickers the store marks invalid (no prices recently) are skipped without asking for prices. When the price
       source raises PriceError the ticker is marked invalid; other errors (network) are noted but don't mark it.
    6. Prices below [universe] min_price, and prices that don't pass dip_reasons, are skipped.
    The rest become candidates, sorted by severity; those beyond [scan] max_candidates_per_cycle are left for the
    next cycle and named in a note.
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

        if (cooldown := _cooldown(ticker, qualifying, store, scan.cooldown_hours, now)) is not None:
            notes.add(f"Analysed within the {_hours(scan.cooldown_hours)} cooldown, no new news since", cooldown)
            continue

        stats = _stats(ticker, prices, store, notes, now)
        if stats is None:
            continue
        if stats.price < universe.min_price:
            notes.add(
                f"Price below [universe] min_price {universe.min_price:g}",
                f"{ticker} ({_money(stats.price)} {stats.currency})",
            )
            continue
        reasons = dip_reasons(stats, dip)
        if not reasons:
            notes.add("No dip (price not down enough)", f"{ticker} ({_moves(stats)})")
            continue
        candidates.append(
            Candidate(
                ticker=ticker,
                company=_company(stats, qualifying),
                stats=stats,
                impacts=qualifying,
                dip_reasons=reasons,
                severity=severity(stats, qualifying),
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


def _cooldown(
    ticker: str, impacts: list[tuple[Impact, Article]], store: Store, cooldown_hours: float, now: datetime
) -> str | None:
    """A note item when the ticker is still in its cooldown with nothing new since its last analysis, else None."""
    if cooldown_hours <= 0:
        return None
    last = store.last_opportunity(ticker)
    if last is None:
        return None
    created = utc(last.created)
    if created <= now - timedelta(hours=cooldown_hours):
        return None
    analysed = set(last.article_ids)
    for _, article in impacts:
        newer = max(utc(article.published), utc(article.fetched)) > created
        if newer and article.id not in analysed:
            return None
    ago = max(0.0, (now - created).total_seconds() / 3600)
    return f"{ticker} (analysed {ago:.1f}h ago)"


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
