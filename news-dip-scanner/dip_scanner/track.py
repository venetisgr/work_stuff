"""Track record: how past opportunities actually played out.

evaluate() replays an opportunity's limit orders against the daily bars that followed it. The rules, precisely:

- Signal day: the UTC date of opp.created. Bars dated before it are ignored, and so are bars more than HORIZON_DAYS
  (183, about 6 months) after it, because the analysis is a 6-month call. Bars carry the exchange's own dates.
- The signal day's bar: when the report's price was taken that same day (the UTC date of opp.stats.as_of is the
  signal day), that bar also covers trading before the report, so only what surely came after it counts: the range
  between the report's price and the day's close. When the price is from an earlier session (the report was written
  before the market opened), the whole bar counts.
- entry_filled: the first day whose low is at or below entry_price (the limit buy would have filled).
- target_hit: the first day AFTER the fill day whose high is at or above target_price (the limit sell). The fill day
  itself doesn't count: a daily bar can't tell whether its low or its high came first. No fill, no target.
- low_breached: the first day whose low is below potential_low, filled or not.
- status, first match wins: "target_hit"; "below_low"; "expired" (more than 183 days since the signal day);
  "open" (filled, waiting for the target); "waiting_entry" (the limit buy hasn't filled).
- last_price is the last close in the window, so once the 6 months are over it stays the 6-month close.
  return_pct, max_gain_pct and max_loss_pct are measured from the report's price (opp.price).
- trade_return_pct is the return of the limit orders: from entry_price to target_price when the target was hit,
  else to last_price; None when the entry never filled.
- up_after_6m: None until more than 183 days have passed; then whether the last close on or before the 6-month date
  is above the report's price (None as well when the bars stop more than a week before that date).
"""

from __future__ import annotations

import logging
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

from .models import VERDICTS, Opportunity, PriceBar, utc
from .report import format_pct, format_price, md_escape, verdict_label

log = logging.getLogger(__name__)

STATUSES = ("waiting_entry", "open", "target_hit", "below_low", "expired")
STATUS_LABELS = {
    "waiting_entry": "waiting for entry",
    "open": "open",
    "target_hit": "target hit",
    "below_low": "below the low",
    "expired": "expired",
}
HORIZON_DAYS = 183  # about 6 months
MAX_DATA_GAP_DAYS = 7  # up_after_6m needs a close at most this many days before the 6-month date
# (label, lowest score, score below which the bucket ends); the same bands as the report's score colours.
SCORE_BUCKETS = (("<50", None, 50.0), ("50-65", 50.0, 65.0), ("65-80", 65.0, 80.0), ("80+", 80.0, None))


@dataclass(frozen=True)
class Outcome:
    """What happened after an opportunity was reported (see the module docstring for the exact rules)."""

    opportunity: Opportunity
    last_price: float
    return_pct: float
    max_gain_pct: float
    max_loss_pct: float
    entry_filled: date | None  # first day low <= entry_price (on/after signal day)
    target_hit: date | None  # first day high >= target_price after the entry filled
    low_breached: date | None  # first day low < potential_low
    days: int
    status: str  # STATUSES; "expired" after 183 days
    up_after_6m: bool | None  # None until 6 months have passed
    trade_return_pct: float | None = None  # limit buy at entry -> target (if hit) or last price; None if not filled


def signal_day(opp: Opportunity) -> date:
    """The UTC date of the opportunity's report."""
    return utc(opp.created).date()


def evaluate(opp: Opportunity, bars: list[PriceBar], *, now: datetime) -> Outcome:
    """The outcome of one opportunity given the daily bars from its signal day onward (extra bars are ignored)."""
    start = signal_day(opp)
    horizon = start + timedelta(days=HORIZON_DAYS)
    window = _window(opp, bars, start, horizon)
    analysis = opp.analysis
    price = opp.price

    entry_filled = next((bar.day for bar in window if bar.low <= analysis.entry_price), None)
    target_hit = None
    if entry_filled is not None:
        target_hit = next(
            (bar.day for bar in window if bar.day > entry_filled and bar.high >= analysis.target_price), None
        )
    low_breached = next((bar.day for bar in window if bar.low < analysis.potential_low), None)

    last_price = window[-1].close if window else price
    days = max(0, (utc(now).date() - start).days)
    matured = days > HORIZON_DAYS
    if target_hit is not None:
        status = "target_hit"
    elif low_breached is not None:
        status = "below_low"
    elif matured:
        status = "expired"
    elif entry_filled is not None:
        status = "open"
    else:
        status = "waiting_entry"

    up_after_6m = None
    if matured and window and (horizon - window[-1].day).days <= MAX_DATA_GAP_DAYS:
        up_after_6m = window[-1].close > price

    trade_return = None
    if entry_filled is not None:
        exit_price = analysis.target_price if target_hit is not None else last_price
        trade_return = _change(exit_price, analysis.entry_price)

    return Outcome(
        opportunity=opp,
        last_price=last_price,
        return_pct=_change(last_price, price),
        max_gain_pct=max(0.0, _change(max(bar.high for bar in window), price)) if window else 0.0,
        max_loss_pct=min(0.0, _change(min(bar.low for bar in window), price)) if window else 0.0,
        entry_filled=entry_filled,
        target_hit=target_hit,
        low_breached=low_breached,
        days=days,
        status=status,
        up_after_6m=up_after_6m,
        trade_return_pct=trade_return,
    )


def _window(opp: Opportunity, bars: Sequence[PriceBar], start: date, horizon: date) -> list[PriceBar]:
    """The bars from the signal day to the 6-month date, one per day (the last one given wins), oldest first."""
    by_day = {bar.day: bar for bar in bars if start <= bar.day <= horizon}
    window = [by_day[day] for day in sorted(by_day)]
    if window and window[0].day == start and utc(opp.stats.as_of).date() == start:
        first = window[0]
        window[0] = replace(first, open=opp.price, high=max(opp.price, first.close), low=min(opp.price, first.close))
    return window


def _change(value: float, base: float) -> float:
    return (value / base - 1) * 100 if base else 0.0


# --- summary ---------------------------------------------------------------------------------------------------------


def score_bucket(score: float) -> str:
    """The score bucket label: "<50", "50-65", "65-80" or "80+" (lower bounds inclusive)."""
    for label, lowest, below in SCORE_BUCKETS:
        if (lowest is None or score >= lowest) and (below is None or score < below):
            return label
    return SCORE_BUCKETS[0][0]  # NaN


def summarize(outcomes: Sequence[Outcome]) -> dict:
    """Counts, hit rates and average returns, overall, by verdict and by score bucket.

    Every group has: count; filled / fill_rate (of all); target_hit / target_hit_rate (of the filled ones);
    below_low / below_low_rate (of all); matured (6 months passed with data) / up_after_6m / up_rate_6m (of the
    matured) / predicted_up_6m (their average probability_up_6m, to compare with up_rate_6m); positive /
    positive_rate (return above 0, of all); avg_return_pct, median_return_pct; avg_trade_return_pct (filled ones);
    avg_score. Rates are percentages; a rate or average over nothing is None.

    The whole dict has those overall figures plus "statuses" (a count per status), "by_verdict" (verdicts that
    occur, in VERDICTS order, then any others) and "by_score" (every bucket in SCORE_BUCKETS, empty ones included).
    """
    outcomes = list(outcomes)
    summary = _group(outcomes)
    summary["statuses"] = {status: sum(o.status == status for o in outcomes) for status in STATUSES}
    verdicts = [v for v in VERDICTS if any(o.opportunity.analysis.verdict == v for o in outcomes)]
    verdicts += sorted({o.opportunity.analysis.verdict for o in outcomes} - set(verdicts))
    summary["by_verdict"] = {
        verdict: _group([o for o in outcomes if o.opportunity.analysis.verdict == verdict]) for verdict in verdicts
    }
    summary["by_score"] = {
        label: _group([o for o in outcomes if score_bucket(o.opportunity.score) == label])
        for label, _, _ in SCORE_BUCKETS
    }
    return summary


def _group(outcomes: list[Outcome]) -> dict:
    filled = [o for o in outcomes if o.entry_filled is not None]
    hits = [o for o in filled if o.target_hit is not None]
    breached = [o for o in outcomes if o.low_breached is not None]
    matured = [o for o in outcomes if o.up_after_6m is not None]
    up = [o for o in matured if o.up_after_6m]
    positive = [o for o in outcomes if o.return_pct > 0]
    returns = [o.return_pct for o in outcomes]
    trades = [o.trade_return_pct for o in filled if o.trade_return_pct is not None]
    return {
        "count": len(outcomes),
        "filled": len(filled),
        "fill_rate": _rate(len(filled), len(outcomes)),
        "target_hit": len(hits),
        "target_hit_rate": _rate(len(hits), len(filled)),
        "below_low": len(breached),
        "below_low_rate": _rate(len(breached), len(outcomes)),
        "matured": len(matured),
        "up_after_6m": len(up),
        "up_rate_6m": _rate(len(up), len(matured)),
        "predicted_up_6m": _mean([o.opportunity.analysis.probability_up_6m for o in matured]),
        "positive": len(positive),
        "positive_rate": _rate(len(positive), len(outcomes)),
        "avg_return_pct": _mean(returns),
        "median_return_pct": statistics.median(returns) if returns else None,
        "avg_trade_return_pct": _mean(trades),
        "avg_score": _mean([o.opportunity.score for o in outcomes]),
    }


def _rate(part: int, whole: int) -> float | None:
    return part / whole * 100 if whole else None


def _mean(values: Sequence[float]) -> float | None:
    return statistics.fmean(values) if values else None


# --- Markdown ------------------------------------------------------------------------------------------------------


def render_track_record(outcomes: Sequence[Outcome], summary: dict) -> str:
    """The track record as Markdown: the headline numbers, tables by verdict and by score, then every opportunity
    (newest first) with its orders, status and returns."""
    lines = ["# Track record", ""]
    outcomes = list(outcomes)
    if not outcomes:
        lines.append("No stored opportunities to track yet.")
        return "\n".join(lines) + "\n"

    days = sorted(signal_day(o.opportunity) for o in outcomes)
    lines.append(
        f"_{_plural(len(outcomes), 'opportunity', 'opportunities')} reported {days[0]:%Y-%m-%d} to "
        f"{days[-1]:%Y-%m-%d} · {summary.get('matured', 0)} with 6 months of results_"
    )

    predicted = summary.get("predicted_up_6m")
    up_line = _of(summary.get("up_after_6m", 0), summary.get("matured", 0))
    if predicted is not None:
        up_line += f"; the model said {predicted:.0f}% on average"
    median = summary.get("median_return_pct")
    average = _pct_or_na(summary.get("avg_return_pct"))
    lines += [
        "",
        "## Summary",
        "",
        "| Measure | Result |",
        "|---|---|",
        f"| Entry filled (limit buy reached) | {_of(summary.get('filled', 0), summary.get('count', 0))} |",
        f"| Target hit after the fill | {_of(summary.get('target_hit', 0), summary.get('filled', 0))} |",
        f"| Fell below the potential low | {_of(summary.get('below_low', 0), summary.get('count', 0))} |",
        f"| Higher after 6 months | {up_line} |",
        f"| Higher now than when reported | {_of(summary.get('positive', 0), summary.get('count', 0))} |",
        f"| Average return since the report | {average}"
        + (f" (median {format_pct(median)})" if median is not None else "")
        + " |",
        f"| Average return of filled limit orders | {_pct_or_na(summary.get('avg_trade_return_pct'))} |",
    ]
    statuses = summary.get("statuses") or {}
    tally = [f"{count} {STATUS_LABELS.get(status, status)}" for status, count in statuses.items() if count]
    if tally:
        lines += ["", "Status: " + " · ".join(tally)]

    group_header = [
        "| {first} | Count | Filled | Target hit | Below low | Higher after 6m | Avg return | Avg limit-order return |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    by_verdict = summary.get("by_verdict") or {}
    if by_verdict:
        lines += ["", "## By verdict", "", group_header[0].format(first="Verdict"), group_header[1]]
        lines += [_group_row(verdict_label(verdict), group) for verdict, group in by_verdict.items()]
    by_score = {label: group for label, group in (summary.get("by_score") or {}).items() if group.get("count")}
    if by_score:
        lines += ["", "## By score", "", group_header[0].format(first="Score"), group_header[1]]
        lines += [_group_row(label, group) for label, group in by_score.items()]

    lines += [
        "",
        "## Opportunities",
        "",
        "| Reported | Ticker | Score | Verdict | Price | Entry | Target | Low | Status | Filled | Target hit "
        "| Return | Max gain | Max loss | Up after 6m |",
        "|---|---|---:|---|---:|---:|---:|---:|---|---|---|---:|---:|---:|---|",
    ]
    ordered = sorted(outcomes, key=lambda o: utc(o.opportunity.created), reverse=True)
    lines += [_outcome_row(outcome) for outcome in ordered]
    lines += [
        "",
        "_Returns are from the price in the report to the last close within 6 months; limit-order returns from the "
        "entry to the target (when hit) or that close. A target only counts on a day after the entry filled. "
        "Past results say little about future ones, and this is not investment advice._",
    ]
    return "\n".join(lines) + "\n"


def _group_row(label: str, group: dict) -> str:
    cells = [
        md_escape(label),
        str(group.get("count", 0)),
        _of(group.get("filled", 0), group.get("count", 0)),
        _of(group.get("target_hit", 0), group.get("filled", 0)),
        _of(group.get("below_low", 0), group.get("count", 0)),
        _of(group.get("up_after_6m", 0), group.get("matured", 0)),
        _pct_or_na(group.get("avg_return_pct")),
        _pct_or_na(group.get("avg_trade_return_pct")),
    ]
    return "| " + " | ".join(cells) + " |"


def _outcome_row(outcome: Outcome) -> str:
    opp = outcome.opportunity
    analysis, currency = opp.analysis, opp.currency
    up = {None: "–", True: "yes", False: "no"}[outcome.up_after_6m]
    cells = [
        f"{utc(opp.created):%Y-%m-%d}",
        f"**{md_escape(opp.ticker)}**",
        f"{opp.score:.1f}",
        verdict_label(analysis.verdict),
        format_price(opp.price, currency),
        format_price(analysis.entry_price, currency),
        format_price(analysis.target_price, currency),
        format_price(analysis.potential_low, currency),
        STATUS_LABELS.get(outcome.status, outcome.status),
        _day(outcome.entry_filled),
        _day(outcome.target_hit),
        format_pct(outcome.return_pct),
        format_pct(outcome.max_gain_pct),
        format_pct(outcome.max_loss_pct),
        up,
    ]
    return "| " + " | ".join(cells) + " |"


def _of(part: int, whole: int) -> str:
    if not whole:
        return "–"
    return f"{part} of {whole} ({part / whole * 100:.0f}%)"


def _pct_or_na(value: float | None) -> str:
    return "–" if value is None else format_pct(value)


def _day(value: date | None) -> str:
    return "–" if value is None else f"{value:%Y-%m-%d}"


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"
