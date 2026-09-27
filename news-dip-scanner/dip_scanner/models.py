"""Shared data types. Every module passes these around; store.py persists them."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, date, datetime
from typing import Any

RELATIONS = ("direct", "indirect")
DIRECTIONS = ("negative", "positive", "mixed", "neutral")
EVENT_TYPES = (
    "earnings",
    "guidance",
    "analyst",
    "regulation",
    "legal",
    "macro",
    "product",
    "management",
    "m&a",
    "supply_chain",
    "competition",
    "accident",
    "other",
)
VERDICTS = ("temporary_fear", "mixed", "fundamental", "unclear")
CONFIDENCES = ("low", "medium", "high")

# Keys of each row in Fundamentals.quarters / Fundamentals.annual, in display order.
FUNDAMENTAL_METRICS = (
    "revenue",
    "gross_profit",
    "operating_income",
    "net_income",
    "eps_diluted",
    "operating_cash_flow",
)
# Fundamentals whose newest period ended longer ago than this carry a warning (a 20-F filer's latest fiscal year can
# legitimately be 12-16 months old, so the limit is above that).
STALE_FUNDAMENTALS_DAYS = 548
# A quarterly filer's newest quarter older than this means a later one is probably missing: a 10-Q is due 40-45 days
# after the quarter and a 10-K 60-90 days after the year, so even the third quarter before a late 10-K is about 180
# days old.
STALE_QUARTER_DAYS = 200
_METRIC_LABELS = {
    "revenue": "Revenue",
    "gross_profit": "Gross profit",
    "operating_income": "Operating income",
    "net_income": "Net income",
    "eps_diluted": "EPS (diluted)",
    "operating_cash_flow": "Operating cash flow",
}


@dataclass(frozen=True)
class Feed:
    key: str
    name: str
    url: str
    enabled: bool = True
    category: str = "markets"  # free text label shown in reports
    # A headline seen again (from any feed, this one included) is the same story. Off for feeds whose titles are
    # formulaic, like the SEC's "8-K - APPLE INC (0000320193) (Filer)" for every new filing of a company.
    dedup_titles: bool = True
    # Items tagged with another language (dc:language) are dropped: the wires publish machine translations of every
    # release. Lowercase codes; "en" also matches "en-us". () keeps every language.
    languages: tuple[str, ...] = ("en",)


@dataclass(frozen=True)
class Article:
    id: str  # sha1 hex of the canonical link (or of guid when no link)
    source: str  # Feed.key, or "ticker:<SYM>" for per-ticker context news
    source_name: str
    title: str
    link: str
    summary: str  # plain text, HTML stripped/unescaped, whitespace collapsed, <= 1500 chars
    published: datetime  # UTC; the fetch time if the feed gives none
    fetched: datetime  # UTC
    title_key: str  # normalised title for cross-source dedup (see feeds.title_key)


@dataclass(frozen=True)
class Impact:
    article_id: str
    ticker: str  # Yahoo Finance symbol, uppercase: AMD, ASML, SAP.DE, OPAP.AT, 7203.T
    company: str
    relation: str  # RELATIONS
    direction: str  # DIRECTIONS
    magnitude: int  # 1 (negligible) .. 5 (major) expected share-price impact
    event_type: str  # EVENT_TYPES
    rationale: str  # one sentence


@dataclass(frozen=True)
class PriceBar:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass(frozen=True)
class Split:
    """A stock split: from day (the exchange-local ex-date) on, one old share is ratio new shares (10:1 -> 10.0,
    a 1:10 reverse split -> 0.1). Yahoo divides every price before day by ratio."""

    day: date
    ratio: float


@dataclass(frozen=True)
class PriceStats:
    ticker: str
    name: str | None
    currency: str
    exchange: str | None
    as_of: datetime  # regularMarketTime, UTC
    price: float  # regularMarketPrice
    previous_close: float
    change_1d_pct: float  # price vs previous_close
    change_5d_pct: float  # price vs close 5 trading days before the latest session
    change_20d_pct: float
    high_20d: float  # max daily high over the last 20 sessions (incl. latest)
    high_52w: float
    low_52w: float
    drawdown_20d_pct: float  # (price / high_20d - 1) * 100, <= 0
    drawdown_52w_pct: float  # (price / high_52w - 1) * 100, <= 0
    above_low_52w_pct: float  # (price / low_52w - 1) * 100, >= 0
    sma_50: float | None
    sma_200: float | None
    volatility_pct: float  # annualised stdev of daily log returns over the last 60 sessions, in %
    # Latest session volume / mean volume of the 20 sessions before it. During the session (session_elapsed set) the
    # expected volume is pro-rated to the part of the session that has passed, so it is the pace so far.
    volume_ratio: float | None
    # 5th-percentile price at the 6-month mark (not the lowest price along the way), zero drift lognormal:
    # price * exp(-1.645 * (volatility_pct/100) * sqrt(0.5))
    stat_low_6m: float
    worst_6m_drawdown_pct: float  # worst peak-to-trough decline within any 126-session window of the history, <= 0
    # The share (0..1) of the regular session that had passed at the quote, when that session was still running at
    # the time of the price check; None otherwise (after the close, at weekends, before the open).
    session_elapsed: float | None = None
    timezone: str | None = None  # the exchange's IANA time zone (e.g. America/New_York), when Yahoo gives it
    instrument_type: str | None = None  # Yahoo's instrumentType: EQUITY, ETF, MUTUALFUND, INDEX...

    def as_text(self) -> str:
        """A compact block of the price picture, readable by people and by the analysis model."""
        cur = self.currency
        title = self.ticker
        if self.name:
            title += f" ({self.name})"
        if self.exchange:
            title += f" on {self.exchange}"
        averages = ", ".join(
            f"{label} {_price(value)}" if value is not None else f"{label} n/a"
            for label, value in (("50-day average", self.sma_50), ("200-day average", self.sma_200))
        )
        volume = ""
        if self.volume_ratio is not None and self.session_elapsed is not None:
            volume = (
                f"; volume so far today at about {self.volume_ratio:.1f}x the normal pace "
                f"(session {self.session_elapsed * 100:.0f}% done, an estimate)"
            )
        elif self.volume_ratio is not None:
            volume = f"; latest volume {self.volume_ratio:.1f}x the 20-day average"
        return "\n".join(
            [
                f"{title}, prices in {cur}, as of {utc(self.as_of):%Y-%m-%d %H:%M} UTC",
                f"Price {_price(self.price)} {cur} (previous close {_price(self.previous_close)}): "
                f"1 day {_pct(self.change_1d_pct)}, 5 days {_pct(self.change_5d_pct)}, "
                f"20 days {_pct(self.change_20d_pct)}",
                f"20-day high {_price(self.high_20d)} ({_pct(self.drawdown_20d_pct)} from it)",
                f"52-week range {_price(self.low_52w)} - {_price(self.high_52w)} "
                f"({_pct(self.drawdown_52w_pct)} from the high, {_pct(self.above_low_52w_pct)} above the low)",
                averages,
                f"Volatility {self.volatility_pct:.1f}% a year (annualised, last 60 sessions){volume}",
                f"Statistical 6-month low (5th percentile of the price in 6 months): {_price(self.stat_low_6m)} "
                f"({_pct(_change(self.stat_low_6m, self.price))} from the price; the lowest price along the way "
                "falls below it about twice as often)",
                f"Worst 6-month drawdown in the price history: {_pct(self.worst_6m_drawdown_pct)}",
            ]
        )


@dataclass(frozen=True)
class Fundamentals:
    ticker: str
    entity: str
    cik: str
    currency: str
    # Newest first; keys: period_end (YYYY-MM-DD) plus FUNDAMENTAL_METRICS (float | None each).
    quarters: list[dict]
    annual: list[dict]  # newest first, same keys, fiscal years

    def as_text(self, today: date | None = None) -> str:
        """Table-like text of recent quarters and fiscal years, with growth where it can be worked out.

        Quarter growth is year over year (y/y) when the same quarter a year earlier is in the list, else quarter
        over quarter (q/q) against the previous quarter. Growth is left out when the earlier figure is zero or
        negative, because a percentage would be meaningless. Given today, a note warns when the newest period ended
        more than STALE_FUNDAMENTALS_DAYS (about 18 months) earlier, or else when the newest quarter ended more than
        STALE_QUARTER_DAYS earlier (a later quarter may be missing).
        """
        header = (
            f"{self.ticker}: {self.entity} (SEC CIK {self.cik}). Amounts in {self.currency} millions except EPS; "
            "growth in brackets."
        )
        if not self.quarters and not self.annual:
            return f"{header}\nNo income statement figures were found in the SEC filings."
        lines = [header]
        newest = max(filter(None, (_period(row) for row in [*self.quarters, *self.annual])), default=None)
        newest_quarter = max(filter(None, (_period(row) for row in self.quarters)), default=None)
        if today is not None and newest is not None and (today - newest).days > STALE_FUNDAMENTALS_DAYS:
            lines.append(
                f"Note: the newest figures are for the period ending {newest:%Y-%m-%d}, about "
                f"{_months(newest, today)} months ago; they may not reflect the business today."
            )
        elif today is not None and newest_quarter is not None and (today - newest_quarter).days > STALE_QUARTER_DAYS:
            lines.append(
                f"Note: the newest quarter ends {newest_quarter:%Y-%m-%d}, about {_months(newest_quarter, today)} "
                "months ago; a later one may be missing."
            )
        if self.quarters:
            lines += ["", "Quarters (newest first):", _table_header("Quarter ending")]
            for index, row in enumerate(self.quarters):
                earlier, label = _earlier_quarter(self.quarters, index)
                lines.append(_table_row(row, earlier, label))
        if self.annual:
            lines += ["", "Fiscal years (newest first):", _table_header("Year ending")]
            for index, row in enumerate(self.annual):
                earlier = _row_near(self.annual[index + 1 :], row, 330, 400)
                lines.append(_table_row(row, earlier, "y/y"))
        return "\n".join(lines)


@dataclass(frozen=True)
class Analysis:
    verdict: str  # VERDICTS
    probability_up_6m: int  # 0..100: chance the price is ABOVE today's price 6 months from now
    potential_low: float  # plausible worst price over the next 6 months (same currency as price)
    entry_price: float  # suggested limit-buy price, potential_low <= entry_price <= price
    target_price: float  # realistic 6-month price if the thesis plays out (limit-sell idea), > entry_price
    confidence: str  # CONFIDENCES
    fear: str  # what the market is afraid of, 1-2 sentences
    fundamental_impact: str  # whether/how revenue, margins, balance sheet, moat are really affected
    thesis: str  # why it should (or should not) recover within 6 months
    risks: list[str] = field(default_factory=list)
    catalysts: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)  # what the human should verify before buying
    warnings: list[str] = field(default_factory=list)  # added by analyze.sanitize when numbers were fixed up


@dataclass(frozen=True)
class Opportunity:
    ticker: str
    company: str
    created: datetime
    price: float
    currency: str
    score: float  # 0..100 composite, see analyze.score
    analysis: Analysis
    stats: PriceStats
    article_ids: list[str]
    headlines: list[dict]  # [{"title", "link", "source", "published" (ISO str), "direction", "magnitude"}]
    dip_reasons: list[str]
    model: str  # model/deployment that wrote the analysis
    id: int | None = None  # set by the store
    # Every flagged article came out after the last session in the price data: the price hadn't reacted to the
    # news yet when this was analysed (see detect.select_candidates).
    news_after_session: bool = False

    def upside_pct(self) -> float:
        """How far the target price is above the price at the time of the analysis, in %."""
        return _change(self.analysis.target_price, self.price)

    def downside_pct(self) -> float:
        """How far the potential low is below the price at the time of the analysis, in % (<= 0)."""
        return _change(self.analysis.potential_low, self.price)

    def to_dict(self) -> dict:
        """A JSON-safe dict (datetimes and dates as ISO 8601 strings); from_dict turns it back."""
        return {
            "id": self.id,
            "ticker": self.ticker,
            "company": self.company,
            "created": to_iso(self.created),
            "price": self.price,
            "currency": self.currency,
            "score": self.score,
            "analysis": analysis_to_dict(self.analysis),
            "stats": stats_to_dict(self.stats),
            "article_ids": list(self.article_ids),
            "headlines": [_json_safe(headline) for headline in self.headlines],
            "dip_reasons": list(self.dip_reasons),
            "model": self.model,
            "news_after_session": self.news_after_session,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Opportunity:
        """The inverse of to_dict. Unknown keys are ignored so older or newer records still load."""
        return cls(
            ticker=data["ticker"],
            company=data["company"],
            created=from_iso(data["created"]),
            price=data["price"],
            currency=data["currency"],
            score=data["score"],
            analysis=analysis_from_dict(data["analysis"]),
            stats=stats_from_dict(data["stats"]),
            article_ids=list(data.get("article_ids") or []),
            headlines=[dict(headline) for headline in data.get("headlines") or []],
            dip_reasons=list(data.get("dip_reasons") or []),
            model=data.get("model") or "",
            id=data.get("id"),
            news_after_session=bool(data.get("news_after_session", False)),
        )


@dataclass(frozen=True)
class ModelUsage:
    """The calls one model answered for one step (triage or analysis) over a period, with the tokens reported."""

    step: str
    model: str
    calls: int
    input_tokens: int  # the sum of the calls that reported tokens
    output_tokens: int
    unmetered: int = 0  # calls the service answered without token counts (their tokens aren't in the sums)


@dataclass(frozen=True)
class Candidate:
    ticker: str
    company: str
    stats: PriceStats
    impacts: list[tuple[Impact, Article]]  # newest first
    dip_reasons: list[str]
    severity: float  # for ordering; higher = bigger/more newsworthy drop
    news_after_session: bool = False  # every qualifying article is newer than stats.as_of (see Opportunity)


def utc(dt: datetime) -> datetime:
    """The same moment as a timezone-aware UTC datetime. Naive datetimes are taken to be UTC already."""
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_iso(dt: datetime) -> str:
    """ISO 8601 text of a datetime in UTC, e.g. 2026-09-25T15:00:00+00:00 (microseconds kept when present)."""
    return utc(dt).isoformat()


def from_iso(text: str) -> datetime:
    """Parse ISO 8601 text (a trailing Z or any offset is fine; none means UTC) into an aware UTC datetime."""
    return utc(datetime.fromisoformat(text))


def stats_to_dict(stats: PriceStats) -> dict:
    """PriceStats as a JSON-safe dict."""
    data = asdict(stats)
    data["as_of"] = to_iso(stats.as_of)
    return data


def stats_from_dict(data: dict) -> PriceStats:
    """The inverse of stats_to_dict (unknown keys are ignored)."""
    values = _known_fields(PriceStats, data)
    if isinstance(values.get("as_of"), str):
        values["as_of"] = from_iso(values["as_of"])
    return PriceStats(**values)


def analysis_to_dict(analysis: Analysis) -> dict:
    """Analysis as a JSON-safe dict."""
    return asdict(analysis)


def analysis_from_dict(data: dict) -> Analysis:
    """The inverse of analysis_to_dict (unknown keys are ignored, missing lists become empty)."""
    values = _known_fields(Analysis, data)
    for name in ("risks", "catalysts", "checks", "warnings"):
        values[name] = [str(item) for item in values.get(name) or []]
    return Analysis(**values)


def article_to_dict(article: Article) -> dict:
    """Article as a JSON-safe dict."""
    data = asdict(article)
    data["published"] = to_iso(article.published)
    data["fetched"] = to_iso(article.fetched)
    return data


def article_from_dict(data: dict) -> Article:
    """The inverse of article_to_dict (unknown keys are ignored)."""
    values = _known_fields(Article, data)
    for name in ("published", "fetched"):
        if isinstance(values.get(name), str):
            values[name] = from_iso(values[name])
    return Article(**values)


def _known_fields(cls: type, data: dict) -> dict[str, Any]:
    names = {f.name for f in fields(cls)}
    return {key: value for key, value in data.items() if key in names}


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return to_iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    return value


def _change(value: float, base: float) -> float:
    return (value / base - 1) * 100 if base else 0.0


def _pct(value: float) -> str:
    return f"{value:+.1f}%"


def _price(value: float) -> str:
    """Prices with 2 decimals, or 4 significant digits for penny stocks."""
    return f"{value:,.2f}" if abs(value) >= 1 else f"{value:.4g}"


# --- Fundamentals.as_text helpers ---------------------------------------------------------------------------------


def _months(earlier: date, later: date) -> int:
    return (later.year - earlier.year) * 12 + later.month - earlier.month


def _period(row: dict) -> date | None:
    try:
        return date.fromisoformat(str(row.get("period_end")))
    except ValueError:
        return None


def _row_near(rows: list[dict], row: dict, min_days: int, max_days: int) -> dict | None:
    """The first of rows whose period ended between min_days and max_days before row's period."""
    end = _period(row)
    if end is None:
        return None
    for other in rows:
        other_end = _period(other)
        if other_end is not None and min_days <= (end - other_end).days <= max_days:
            return other
    return None


def _earlier_quarter(quarters: list[dict], index: int) -> tuple[dict | None, str]:
    older = quarters[index + 1 :]
    year_ago = _row_near(older, quarters[index], 350, 380)  # 52/53-week fiscal years move the date a little
    if year_ago is not None:
        return year_ago, "y/y"
    return _row_near(older, quarters[index], 60, 120), "q/q"


def _table_header(first: str) -> str:
    return " | ".join([first] + [_METRIC_LABELS[name] for name in FUNDAMENTAL_METRICS])


def _table_row(row: dict, earlier: dict | None, label: str) -> str:
    cells = [str(row.get("period_end") or "?")]
    for name in FUNDAMENTAL_METRICS:
        value = row.get(name)
        if value is None:
            cells.append("n/a")
            continue
        text = f"{value:.2f}" if name == "eps_diluted" else _millions(value)
        previous = earlier.get(name) if earlier else None
        if previous is not None and previous > 0:
            text += f" ({_pct(_change(value, previous))} {label})"
        cells.append(text)
    return " | ".join(cells)


def _millions(value: float) -> str:
    millions = value / 1_000_000
    return f"{millions:,.0f}" if abs(millions) >= 100 else f"{millions:,.1f}"
