"""The "fear or fundamentals" verdict: ask the analysis model about a candidate and score its answer.

The model gets the price picture, the fundamentals (when there are any) and the news, and replies with the Analysis
JSON. validate_analysis checks the reply's shape and types (a failure makes complete_json ask once more), sanitize
makes the numbers consistent with each other and with the price (noting every fix in Analysis.warnings), and score
turns the result into one 0-100 number for ranking and alerts.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import date, datetime
from typing import Any

from . import prompts
from .llm import ChatModel, complete_json
from .models import (
    CONFIDENCES,
    VERDICTS,
    Analysis,
    Article,
    Candidate,
    Fundamentals,
    Impact,
    Opportunity,
    PriceStats,
    to_iso,
    utc,
)

log = logging.getLogger(__name__)

NEWS_CHARS = 12_000  # the news block is cut to about this many characters, oldest articles first
NEWS_SUMMARY_CHARS = 600  # per article
MAX_LIST_ITEMS = 6  # risks, catalysts, checks
MAX_ITEM_CHARS = 300
NO_FUNDAMENTALS = (
    "Not available (not a US SEC filer, or the SEC data couldn't be fetched). Don't assume any figures; "
    "put what to look up in checks."
)
# Listings outside the US (a Yahoo suffix: SAP.DE, ALWN.AT) never have SEC figures. Without this the model rates its
# confidence "low" for missing data, and the score's 0.85 factor keeps such stocks below the default alert threshold.
NON_US_FUNDAMENTALS = (
    "Not provided for this listing: company figures come only from US SEC filings, and {ticker} is listed outside "
    "the US, so none were expected. This is not a gap in the case and says nothing about the company: judge the "
    "verdict and your confidence on the news and the price data, and don't lower your confidence because figures "
    "are missing. Don't assume any figures; put what to look up in checks."
)
NO_NEWS = "No news articles were provided."

# score() factors.
VERDICT_FACTORS = {"temporary_fear": 1.0, "mixed": 0.85, "unclear": 0.7, "fundamental": 0.5}
CONFIDENCE_FACTORS = {"high": 1.0, "medium": 0.93, "low": 0.85}

_TEXT_FIELDS = ("fear", "fundamental_impact", "thesis")
_LIST_FIELDS = ("risks", "catalysts", "checks")
_PRICE_FIELDS = ("potential_low", "entry_price", "target_price")
_REQUIRED = ("verdict", "probability_up_6m", *_PRICE_FIELDS, "confidence", *_TEXT_FIELDS)

# z-scores of the lower tail of a normal distribution, for the price-block anchors.
_Z_10TH_PERCENTILE = 1.2816
_TRADING_DAYS = 252
_HALF_YEAR = 0.5
# sanitize caps a target above both the 52-week high and this many standard deviations of 6-month volatility above
# the price (2: the 97.7th percentile of the 6-month price): a slipped decimal or a units mix-up, not a realistic call.
_TARGET_SIGMAS = 2.0


# --- the prompt ----------------------------------------------------------------------------------------------------


def price_block(stats: PriceStats) -> str:
    """The price picture for the prompt: PriceStats.as_text() plus the anchors the model needs for its numbers.

    The extra lines put the last move in proportion (typical daily move from the annualised volatility) and give
    three reference prices for potential_low: the 10th and 5th percentile 6-month prices of a zero-drift lognormal
    model, and the price after a repeat of the worst 6-month drawdown in the history. The percentiles are of the price
    at the 6-month mark, not of the lowest price along the way (which is below them about twice as often, by the
    reflection principle), and the block says so.
    """
    lines = [stats.as_text()]
    daily = stats.volatility_pct / math.sqrt(_TRADING_DAYS)
    if daily > 0:
        lines.append(
            f"Typical daily move: about {daily:.1f}% (one standard deviation); the last session's move of "
            f"{stats.change_1d_pct:+.1f}% is {abs(stats.change_1d_pct) / daily:.1f}x that."
        )
    sigma = stats.volatility_pct / 100 * math.sqrt(_HALF_YEAR)
    low_10 = stats.price * math.exp(-_Z_10TH_PERCENTILE * sigma)
    repeat = stats.price * (1 + min(0.0, stats.worst_6m_drawdown_pct) / 100)
    lines.append(
        f"Anchors for potential_low ({stats.currency}): 10th-percentile 6-month price {_money(low_10)}, "
        f"5th-percentile {_money(stats.stat_low_6m)}, after a repeat of the worst 6-month drawdown {_money(repeat)}. "
        "The percentiles are of the price at the 6-month mark; the lowest price along the way falls below each of "
        "them about twice as often."
    )
    return "\n".join(lines)


def news_block(impacts: list[tuple[Impact, Article]], extra: list[Article]) -> str:
    """The news for the prompt: newest first, deduplicated by title, cut to about NEWS_CHARS characters.

    Each article is a <news> element with its date and source. Articles the triage flagged carry kind="flagged"
    and the triage's direction, magnitude, relation, event type and rationale; extra per-ticker headlines carry
    kind="context" (not triaged). When the same story appears in both, the flagged copy is kept. Angle brackets in
    article text are replaced so an article can't close its element (a prompt-injection guard).
    """
    entries: list[tuple[Article, Impact | None]] = []
    seen: set[str] = set()
    for impact, article in impacts:
        if _dedup_keys(article) & seen:
            continue
        seen |= _dedup_keys(article)
        entries.append((article, impact))
    for article in extra:
        if _dedup_keys(article) & seen:
            continue
        seen |= _dedup_keys(article)
        entries.append((article, None))
    if not entries:
        return NO_NEWS
    entries.sort(key=lambda entry: (utc(entry[0].published), utc(entry[0].fetched)), reverse=True)

    blocks: list[str] = []
    size = 0
    for article, impact in entries:
        block = _news_item(article, impact)
        if blocks and size + len(block) > NEWS_CHARS:
            break
        blocks.append(block)
        size += len(block) + 2
    left_out = len(entries) - len(blocks)
    if left_out:
        blocks.append(f"[{left_out} older article(s) left out for length]")
    return "\n\n".join(blocks)


def _dedup_keys(article: Article) -> set[str]:
    keys = {f"id:{article.id}"}
    title = article.title_key or " ".join(article.title.casefold().split())
    if title:
        keys.add(f"title:{title}")
    return keys


def _news_item(article: Article, impact: Impact | None) -> str:
    attributes = {
        "date": f"{utc(article.published):%Y-%m-%d %H:%M} UTC",
        "source": article.source_name or article.source,
    }
    if impact is None:
        attributes["kind"] = "context"
    else:
        attributes.update(
            kind="flagged",
            direction=impact.direction,
            magnitude=f"{impact.magnitude} of 5",
            relation=impact.relation,
            event=impact.event_type,
        )
    rendered = " ".join(f'{key}="{_safe(value).replace(chr(34), chr(39))}"' for key, value in attributes.items())
    title = _safe(" ".join(article.title.split()))
    lines = [title]
    summary = _summary(article)
    if summary:
        lines.append(_safe(summary))
    if impact is not None and impact.rationale.strip():
        lines.append(f"Triage note: {_safe(' '.join(impact.rationale.split()))}")
    body = "\n".join(lines)
    return f"<news {rendered}>\n{body}\n</news>"


def _summary(article: Article) -> str:
    """The article summary without a repeated title (Google News), cut to NEWS_SUMMARY_CHARS."""
    summary = " ".join(article.summary.split())
    title = " ".join(article.title.split())
    if summary.casefold().startswith(title.casefold()):
        summary = summary[len(title) :].strip(" -–—|:")
        if len(summary) < 40:  # just the source name
            return ""
    if len(summary) > NEWS_SUMMARY_CHARS:
        summary = summary[:NEWS_SUMMARY_CHARS].rsplit(" ", 1)[0].rstrip(",;:") + " …"
    return summary


def _safe(text: str) -> str:
    return text.replace("<", "‹").replace(">", "›")


def fundamentals_block(ticker: str, fundamentals: Fundamentals | None, *, today: date) -> str:
    """The fundamentals for the prompt, or why there are none: a listing outside the US (a Yahoo exchange suffix such
    as .DE or .AT) never has SEC figures, which the model must not count against its confidence."""
    if fundamentals is not None:
        return fundamentals.as_text(today=today)
    if not is_us_listing(ticker):
        return NON_US_FUNDAMENTALS.format(ticker=ticker)
    return NO_FUNDAMENTALS


def is_us_listing(ticker: str) -> bool:
    """Whether a Yahoo symbol is a US listing: those have no exchange suffix (AMD, BRK-B; SAP.DE and ALWN.AT do)."""
    return "." not in ticker.strip()


# --- the model's reply ---------------------------------------------------------------------------------------------


def validate_analysis(data: Any) -> dict:
    """Check the model's analysis reply and return its fields normalised; ValueError says what's wrong.

    Strict on shape, types and enum values, tolerant of harmless variations: numbers written as strings ("12.5",
    "$132", "1,234.5", "68%"), a probability given as a fraction (0.68 -> 68), enum values in another case or with
    spaces ("Temporary fear"), a single string instead of a list, a missing or null list, and a reply wrapped in one
    extra object ({"analysis": {...}}). Unknown keys are ignored. Ranges are not checked here: sanitize fixes them.
    """
    if isinstance(data, dict) and "verdict" not in data and len(data) == 1:
        (inner,) = data.values()
        if isinstance(inner, dict):
            data = inner
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object with the analysis fields, got {type(data).__name__}.")
    missing = [key for key in _REQUIRED if data.get(key) is None]
    if missing:
        raise ValueError(f"Missing field{'s' if len(missing) > 1 else ''}: {', '.join(missing)}.")

    result: dict[str, Any] = {
        "verdict": _choice(data["verdict"], "verdict", VERDICTS),
        "probability_up_6m": _probability(data["probability_up_6m"]),
        "confidence": _choice(data["confidence"], "confidence", CONFIDENCES),
    }
    for key in _PRICE_FIELDS:
        result[key] = _number(data[key], key)
    for key in _TEXT_FIELDS:
        value = data[key]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f'"{key}" must be a non-empty string.')
        result[key] = " ".join(value.split())
    for key in _LIST_FIELDS:
        result[key] = _string_list(data.get(key), key)
    return result


_NUMBER = re.compile(r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|[-+]?\.\d+")
_CURRENCY_SIGNS = "$€£¥"


def _number(value: Any, key: str, *, percent: bool = False) -> float:
    """A finite float from a JSON number or a numeric string."""
    if isinstance(value, bool):
        raise ValueError(f'"{key}" must be a number, got {_repr(value)}.')
    if isinstance(value, int | float):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip().lstrip(_CURRENCY_SIGNS).strip()
        if percent:
            text = text.removesuffix("%").strip()
        if not _NUMBER.fullmatch(text):
            raise ValueError(f'"{key}" must be a plain number, got {_repr(value)}.')
        number = float(text.replace(",", ""))
    else:
        raise ValueError(f'"{key}" must be a number, got {_repr(value)}.')
    if not math.isfinite(number):
        raise ValueError(f'"{key}" must be a finite number, got {_repr(value)}.')
    return number


def _probability(value: Any) -> float:
    """probability_up_6m in percent. Fractions are converted: 0.68 or "0.68" -> 68, 1.0 -> 100 (1 and "1%" stay 1)."""
    number = _number(value, "probability_up_6m", percent=True)
    explicit_percent = isinstance(value, str) and value.strip().endswith("%")
    if not explicit_percent and (0 < number < 1 or (number == 1 and isinstance(value, float))):
        number *= 100
    return number


def _choice(value: Any, key: str, allowed: tuple[str, ...]) -> str:
    word = re.sub(r"[\s-]+", "_", value.strip().lower()) if isinstance(value, str) else None
    if word not in allowed:
        raise ValueError(f'"{key}" must be one of {", ".join(allowed)}; got {_repr(value)}.')
    return word


def _string_list(value: Any, key: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f'"{key}" must be a list of strings.')
    return [" ".join(item.split()) for item in value if item.strip()]


def _repr(value: Any) -> str:
    """A short representation of a reply value for error messages."""
    text = repr(value)
    return text if len(text) <= 80 else text[:77] + "..."


# --- consistent numbers --------------------------------------------------------------------------------------------


def sanitize(raw: dict, stats: PriceStats) -> Analysis:
    """An Analysis with numbers clamped into a consistent range; each fix-up adds a warning.

    raw is validate_analysis's result. With price = stats.price:
    - probability_up_6m is rounded to a whole number and clamped to 0..100;
    - potential_low must be above 0 and below the price: at or above the price it becomes
      min(stats.stat_low_6m, price * 0.97); below price * 0.3 (including zero or negative) it is raised to
      price * 0.3;
    - entry_price is clamped into [potential_low, price];
    - target_price must be above entry_price, else it becomes entry * (1 + step) with
      step = max(0.05, volatility_pct / 100 * 0.5);
    - target_price can't be above max(high_52w, price * exp(2 * volatility_pct / 100 * sqrt(0.5)), entry * (1 + step)):
      a higher one (a slipped decimal, pence for pounds, a hallucination) is lowered to that ceiling, so it can't
      inflate the score's reward/risk;
    - risks, catalysts and checks keep at most MAX_LIST_ITEMS non-empty strings each.
    """
    price = stats.price
    warnings: list[str] = []

    probability = float(raw["probability_up_6m"])
    clamped = min(100.0, max(0.0, probability))
    if clamped != probability:
        warnings.append(f"probability_up_6m {probability:g} was outside 0-100; used {clamped:.0f}.")
    probability_up = round(clamped)

    low = float(raw["potential_low"])
    if low >= price:
        statistical = stats.stat_low_6m <= price * 0.97
        fixed = _round_price(stats.stat_low_6m if statistical else price * 0.97)
        warnings.append(
            f"potential_low {_money(low)} was not below the price {_money(price)}; used {_money(fixed)}, "
            f"{'the statistical 6-month low' if statistical else '3% below the price'}."
        )
        low = fixed
    floor = price * 0.3
    if low < floor:
        fixed = _round_price(floor)
        warnings.append(
            f"potential_low {_money(low)} was more than 70% below the price {_money(price)}; used {_money(fixed)}."
        )
        low = fixed

    entry = float(raw["entry_price"])
    if not low <= entry <= price:
        fixed = min(price, max(low, entry))
        where = "above the price" if entry > price else "below potential_low"
        warnings.append(f"entry_price {_money(entry)} was {where}; used {_money(fixed)}.")
        entry = fixed

    target = float(raw["target_price"])
    step = max(0.05, stats.volatility_pct / 100 * 0.5)
    if target <= entry:
        fixed = _round_price(entry * (1 + step))
        warnings.append(
            f"target_price {_money(target)} was not above entry_price {_money(entry)}; used {_money(fixed)} "
            f"({step * 100:.0f}% above the entry)."
        )
        target = fixed
    sigma = max(0.0, stats.volatility_pct) / 100 * math.sqrt(_HALF_YEAR)
    ceiling = max(stats.high_52w, price * math.exp(_TARGET_SIGMAS * sigma), entry * (1 + step))
    if target > ceiling:
        fixed = _round_price(ceiling)
        warnings.append(
            f"target_price {_money(target)} was implausibly high (above the 52-week high {_money(stats.high_52w)} "
            f"and {_TARGET_SIGMAS:g} standard deviations of 6-month volatility above the price); used {_money(fixed)}."
        )
        target = fixed

    lists = {key: _trim_list(raw.get(key)) for key in _LIST_FIELDS}
    return Analysis(
        verdict=raw["verdict"],
        probability_up_6m=probability_up,
        potential_low=low,
        entry_price=entry,
        target_price=target,
        confidence=raw["confidence"],
        fear=_text(raw["fear"]),
        fundamental_impact=_text(raw["fundamental_impact"]),
        thesis=_text(raw["thesis"]),
        warnings=warnings,
        **lists,
    )


def _trim_list(value: Any) -> list[str]:
    items = [" ".join(str(item).split()) for item in value or []]
    return [_cut(item, MAX_ITEM_CHARS) for item in items if item][:MAX_LIST_ITEMS]


def _text(value: Any) -> str:
    return " ".join(str(value).split())


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _round_price(value: float) -> float:
    """Prices the sanitizer makes up: cents for normal prices, 4 decimals for penny stocks."""
    return round(value, 2) if abs(value) >= 1 else round(value, 4)


def _money(value: float) -> str:
    """A price as a plain number (no thousands separators, so the model copies it as a number)."""
    return f"{value:.2f}" if abs(value) >= 1 else f"{value:.4g}"


# --- score ---------------------------------------------------------------------------------------------------------


def score(analysis: Analysis, price: float) -> float:
    """Composite 0..100 score from probability, reward/risk, verdict and confidence, rounded to 1 decimal.

        prob = probability_up_6m / 100
        up = max(0, target_price / price - 1); down = max(0.01, 1 - potential_low / price)
        reward_risk = up / (up + down)           # 0..1, 0.5 = as much upside as downside
        score = 100 * (0.7 * prob + 0.3 * reward_risk) * VERDICT_FACTORS[verdict] * CONFIDENCE_FACTORS[confidence]

    VERDICT_FACTORS: temporary_fear 1.0, mixed 0.85, unclear 0.7, fundamental 0.5. CONFIDENCE_FACTORS: high 1.0,
    medium 0.93, low 0.85. An unknown verdict or confidence gets the lowest factor.
    """
    if price <= 0:
        return 0.0
    prob = min(1.0, max(0.0, analysis.probability_up_6m / 100))
    up = max(0.0, analysis.target_price / price - 1)
    down = max(0.01, 1 - analysis.potential_low / price)
    reward_risk = up / (up + down)
    verdict = VERDICT_FACTORS.get(analysis.verdict, min(VERDICT_FACTORS.values()))
    confidence = CONFIDENCE_FACTORS.get(analysis.confidence, min(CONFIDENCE_FACTORS.values()))
    return round(100 * (0.7 * prob + 0.3 * reward_risk) * verdict * confidence, 1)


# --- one candidate -------------------------------------------------------------------------------------------------


def analyze_candidate(
    model: ChatModel,
    candidate: Candidate,
    *,
    fundamentals: Fundamentals | None,
    extra_news: list[Article],
    now: datetime,
) -> Opportunity:
    """Ask the model for a verdict on one candidate and turn it into a scored Opportunity.

    Raises LLMError when the reply is unusable even after a corrective retry (LLMSetupError when no call can work).
    """
    now = utc(now)
    stats = candidate.stats
    prompt = prompts.ANALYSIS_PROMPT.format(
        ticker=candidate.ticker,
        company=candidate.company,
        today=f"{now:%Y-%m-%d} ({now:%A})",
        price_block=price_block(stats),
        fundamentals_block=fundamentals_block(candidate.ticker, fundamentals, today=now.date()),
        news_block=news_block(candidate.impacts, extra_news),
        dip_reasons="; ".join(candidate.dip_reasons) or "manual analysis (no dip thresholds applied)",
        currency=stats.currency,
        price=_money(stats.price),
    )
    raw = complete_json(model, prompts.ANALYSIS_SYSTEM, prompt, validate=validate_analysis)
    analysis = sanitize(raw, stats)
    for warning in analysis.warnings:
        log.info("%s: fixed the analysis: %s", candidate.ticker, warning)
    opportunity = Opportunity(
        ticker=candidate.ticker,
        company=candidate.company,
        created=now,
        price=stats.price,
        currency=stats.currency,
        score=score(analysis, stats.price),
        analysis=analysis,
        stats=stats,
        article_ids=list(dict.fromkeys(article.id for _, article in candidate.impacts)),
        headlines=_headlines(candidate.impacts),
        dip_reasons=list(candidate.dip_reasons),
        model=model.name,
        news_after_session=candidate.news_after_session,
    )
    log.info(
        "%s: %s (%s confidence), %d%% chance up in 6 months, score %.1f.",
        candidate.ticker,
        analysis.verdict,
        analysis.confidence,
        analysis.probability_up_6m,
        opportunity.score,
    )
    return opportunity


def _headlines(impacts: list[tuple[Impact, Article]]) -> list[dict]:
    """The flagged articles for reports, one per article, in the candidate's (newest first) order."""
    headlines: list[dict] = []
    seen: set[str] = set()
    for impact, article in impacts:
        if article.id in seen:
            continue
        seen.add(article.id)
        headlines.append(
            {
                "title": article.title,
                "link": article.link,
                "source": article.source_name or article.source,
                "published": to_iso(article.published),
                "direction": impact.direction,
                "magnitude": impact.magnitude,
            }
        )
    return headlines
