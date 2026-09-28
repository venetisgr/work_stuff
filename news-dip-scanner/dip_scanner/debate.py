"""The two-model debate behind an analysis (LLM_ANALYSIS_MODE=debate): two models (LLM_DEBATERS, by default OpenAI's
and Anthropic's analysis models) analyse a candidate independently, argue where they disagree, and a judge rules.

The protocol for one candidate (debate_candidate):

1. Openings. Both debaters get exactly the single analysis's prompt (prompts.ANALYSIS_SYSTEM / ANALYSIS_PROMPT) with
   the same case, at the same time (two threads). Each reply is validated and sanitised like a single analysis.
2. Agreement check (disagreements), deterministic. The openings disagree when their verdicts differ, their chances
   up are more than [debate] max_probability_gap points apart, their potential lows are more than max_low_gap_pct of
   the price apart, or one of them passes somebody's alert rules (min_score, min_probability, verdicts: any
   recipient's) and the other doesn't. With [debate] when = "disagree" and no disagreement the openings are merged
   (merge) and nothing else runs: mode "agreed".
3. Rebuttals, [debate] rounds of them (1 by default; both debaters at the same time): each sees its own position and
   the other's, called "the other analyst" (never a provider or model name), and replies with its full final
   analysis plus critique, concessions and changed_mind (prompts.DEBATE_REBUTTAL_*).
4. The judge (prompts.DEBATE_JUDGE_*) reads the case and both final positions with their critiques and concessions,
   labelled "Analyst A" and "Analyst B", and replies with the final analysis plus debate_summary, agreement and
   favoured. Which debater is A is fixed per ticker and day by a hash (analyst_order), so the judge can't tell which
   one is its own model. LLM_DEBATE_JUDGE=alternate (the default) picks the judge from the two debaters by another
   hash of ticker and day (judge_index), so neither side always judges; that debater is also the "primary" one whose
   texts a merge keeps.
5. Guardrails on the judge's ruling (guard_ruling), each noted in Analysis.warnings like sanitize's fixes: the
   chance up is kept within 5 points of the debaters' final range, the potential low within 5% of the price of
   theirs, the confidence is capped at "low" when their final verdicts are opposite (temporary_fear against
   fundamental) and at "medium" when they merely differ; then analyze.sanitize's rules.
6. Failures. One debater failing its opening (any error, including LLMSetupError for its provider only) leaves the
   other's analysis standing alone: mode "single", with the reason, and the Scanner sends a system notice (at most
   one per provider every 12 hours). A failed rebuttal keeps that debater's previous position. A failed judge
   leaves the two final positions merged by rule (merge), with the reason. Both openings failing raises like a single
   analysis would: LLMSetupError when both were setup errors, LLMUnavailableError when either was unreachable, else
   LLMError (ConfigError when both were).
7. Metering: every call goes through meter(model, step) with the steps STEP_OPENING, STEP_REBUTTAL and STEP_JUDGE
   ("analysis:opening", ...), so Store.model_usage totals them per model, and [scan] max_analyses_per_day still
   counts one analysis per candidate (Store.analyses_since).

merge (for "agreed", and when the judge fails): the verdict is the shared one (when they differ, the more cautious
of the two, by analyze.VERDICT_FACTORS); the chance up the mean, rounded half up; the potential low the lower one;
the entry the lower one, kept between the low and the price; the target the mean (it stays above the entry); the
confidence the lower one (capped as in step 5 when the verdicts differ); fear, fundamental impact, thesis and
catalysts the primary debater's; risks and checks the primary's followed by the other's that aren't repeats (at most
6 each).
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Any

from . import prompts
from .analyze import (
    MAX_ITEM_CHARS,
    MAX_LIST_ITEMS,
    VERDICT_FACTORS,
    ask_analysis,
    case_fields,
    plain_price,
    sanitize,
    score,
    to_opportunity,
    validate_analysis,
)
from .config import AlertConfig, ConfigError, DebateConfig
from .llm import ChatModel, DebatePanel, DebaterModel, LLMError, LLMSetupError, LLMUnavailableError, complete_json
from .models import (
    AGREEMENTS,
    CONFIDENCES,
    Analysis,
    Article,
    Candidate,
    Debate,
    Fundamentals,
    Opportunity,
    Participant,
    PriceStats,
    utc,
)
from .report import verdict_label

log = logging.getLogger(__name__)

STEP_OPENING = "analysis:opening"
STEP_REBUTTAL = "analysis:rebuttal"
STEP_JUDGE = "analysis:judge"
MAX_POINTS = 5  # critique and concessions kept per rebuttal
PROBABILITY_SLACK = 5  # points the judge may go beyond the debaters' final chances up
LOW_SLACK = 0.05  # of the price: how far the judge's potential low may go beyond the debaters'
OPPOSITE_VERDICTS = frozenset({"temporary_fear", "fundamental"})
LABELS = ("A", "B")

Meter = Callable[[ChatModel, str], ChatModel]  # (model, step) -> the model to call, e.g. a pipeline.MeteredModel

_CONFIDENCE_RANK = {name: rank for rank, name in enumerate(CONFIDENCES)}  # low 0, medium 1, high 2
_FAVOURED = {"a": "A", "analyst a": "A", "b": "B", "analyst b": "B"}
_NEITHER = {"neither", "none", "both", "tie", "equal", "n/a", ""}
_TRUE_WORDS = {"true", "yes", "1"}
_FALSE_WORDS = {"false", "no", "0", ""}


@dataclass(frozen=True)
class DebaterFailure:
    """A call of the debate that failed: stage is "opening", "rebuttal" or "judge"; other is the debater whose
    analysis stood alone after a failed opening (None otherwise)."""

    label: str  # "provider:model"
    stage: str
    error: Exception
    other: str | None = None

    @property
    def provider(self) -> str:
        return self.label.split(":", 1)[0]


@dataclass(frozen=True)
class DebateResult:
    """The opportunity a debate produced (its analysis is the outcome, its debate how it came about) and every call
    that failed on the way."""

    opportunity: Opportunity
    failures: list[DebaterFailure] = field(default_factory=list)


@dataclass
class _Side:
    """One debater during the debate."""

    debater: DebaterModel
    label: str  # "A" / "B"
    opening: Analysis
    final: Analysis
    critique: list[str] = field(default_factory=list)
    concessions: list[str] = field(default_factory=list)
    changed_mind: bool = False

    def participant(self) -> Participant:
        return Participant(
            label=self.label,
            model=self.debater.label,
            opening=self.opening,
            final=self.final,
            critique=list(self.critique),
            concessions=list(self.concessions),
            changed_mind=self.changed_mind,
        )


# --- who is A, who judges ------------------------------------------------------------------------------------------


def _coin(purpose: str, ticker: str, day: date) -> int:
    """0 or 1, fixed for a purpose, ticker and day (the first bit of a SHA-256 hash)."""
    digest = hashlib.sha256(f"{purpose}|{ticker.strip().upper()}|{day.isoformat()}".encode()).digest()
    return digest[0] & 1


def analyst_order(ticker: str, day: date) -> tuple[int, int]:
    """The indexes (into LLM_DEBATERS) of Analyst A and Analyst B for a ticker on a day (UTC)."""
    return (0, 1) if _coin("order", ticker, day) == 0 else (1, 0)


def judge_index(ticker: str, day: date) -> int:
    """The index (into LLM_DEBATERS) of the debater that judges under LLM_DEBATE_JUDGE=alternate, and whose texts a
    merge keeps (the "primary" one), for a ticker on a day (UTC)."""
    return _coin("judge", ticker, day)


# --- the agreement check and the merge -----------------------------------------------------------------------------


def passes(analysis: Analysis, rule: AlertConfig, price: float) -> bool:
    """Whether an analysis would pass one set of alert rules (min_score, min_probability, verdicts)."""
    return (
        score(analysis, price) >= rule.min_score
        and analysis.probability_up_6m >= rule.min_probability
        and analysis.verdict in rule.verdicts
    )


def disagreements(
    first: Analysis,
    second: Analysis,
    *,
    price: float,
    config: DebateConfig,
    rules: Sequence[AlertConfig] = (),
) -> list[str]:
    """Why two analyses disagree materially (see the module docstring), in words; empty when they agree."""
    found = []
    if first.verdict != second.verdict:
        found.append(f"verdicts differ ({verdict_label(first.verdict)} against {verdict_label(second.verdict)})")
    gap = abs(first.probability_up_6m - second.probability_up_6m)
    if gap > config.max_probability_gap:
        found.append(
            f"chances up {first.probability_up_6m}% and {second.probability_up_6m}% are {gap} points apart (more "
            f"than {config.max_probability_gap:g})"
        )
    low_gap = abs(first.potential_low - second.potential_low) / price * 100 if price > 0 else 0.0
    if low_gap > config.max_low_gap_pct:
        found.append(
            f"potential lows {plain_price(first.potential_low)} and {plain_price(second.potential_low)} are "
            f"{low_gap:.1f}% of the price apart (more than {config.max_low_gap_pct:g}%)"
        )
    seen: set[tuple] = set()
    for rule in rules:
        key = (rule.min_score, rule.min_probability, tuple(rule.verdicts))
        if key in seen:
            continue
        seen.add(key)
        if passes(first, rule, price) != passes(second, rule, price):
            found.append(
                f"only one of them passes alert rules (score {score(first, price):.1f} against "
                f"{score(second, price):.1f}; minimum score {rule.min_score:g}, chance {rule.min_probability}%)"
            )
            break
    return found


def agreement_of(first: Analysis, second: Analysis, *, price: float, config: DebateConfig) -> str:
    """A deterministic agreement level for two positions: "low" for opposite verdicts, "medium" for any other
    disagreement (alert rules aside), else "high"."""
    if {first.verdict, second.verdict} == OPPOSITE_VERDICTS:
        return "low"
    return "medium" if disagreements(first, second, price=price, config=config) else "high"


def confidence_cap(verdicts: Sequence[str]) -> str | None:
    """The highest confidence a ruling may have over positions with these verdicts: "low" when they include both
    temporary_fear and fundamental, "medium" when they differ otherwise, None when they are the same."""
    distinct = set(verdicts)
    if distinct >= OPPOSITE_VERDICTS:
        return "low"
    return "medium" if len(distinct) > 1 else None


def _lower_confidence(*values: str) -> str:
    return min(values, key=lambda value: _CONFIDENCE_RANK.get(value, 0))


def _half_up(value: float) -> int:
    return int(math.floor(value + 0.5))


def merge(primary: Analysis, other: Analysis, stats: PriceStats) -> Analysis:
    """The two positions merged by rule (see the module docstring), checked by analyze.sanitize."""
    price = stats.price
    verdict = primary.verdict
    if other.verdict != primary.verdict:  # only when a judge failed: the more cautious reading
        verdict = min((primary.verdict, other.verdict), key=lambda value: VERDICT_FACTORS.get(value, 0.0))
    low = min(primary.potential_low, other.potential_low)
    entry = min(price, max(low, min(primary.entry_price, other.entry_price)))
    confidence = _lower_confidence(primary.confidence, other.confidence)
    cap = confidence_cap([primary.verdict, other.verdict])
    if cap is not None:
        confidence = _lower_confidence(confidence, cap)
    raw = {
        "verdict": verdict,
        "probability_up_6m": _half_up((primary.probability_up_6m + other.probability_up_6m) / 2),
        "potential_low": low,
        "entry_price": entry,
        "target_price": (primary.target_price + other.target_price) / 2,
        "confidence": confidence,
        "fear": primary.fear,
        "fundamental_impact": primary.fundamental_impact,
        "thesis": primary.thesis,
        "risks": _union(primary.risks, other.risks),
        "catalysts": list(primary.catalysts),
        "checks": _union(primary.checks, other.checks),
    }
    return sanitize(raw, stats)


def _union(first: Sequence[str], second: Sequence[str]) -> list[str]:
    """first's items, then second's that aren't repeats (case and spacing ignored), at most MAX_LIST_ITEMS."""
    items: list[str] = []
    seen: set[str] = set()
    for item in [*first, *second]:
        key = " ".join(item.casefold().split()).rstrip(".")
        if key and key not in seen:
            seen.add(key)
            items.append(item)
    return items[:MAX_LIST_ITEMS]


def guard_ruling(raw: dict, finals: Sequence[Analysis], stats: PriceStats) -> Analysis:
    """The judge's validated reply kept near the debaters' final positions (see the module docstring), then
    sanitised; every fix is a warning, before sanitize's own."""
    raw = dict(raw)
    warnings: list[str] = []
    chances = [final.probability_up_6m for final in finals]
    lowest, highest = min(chances) - PROBABILITY_SLACK, max(chances) + PROBABILITY_SLACK
    probability = float(raw["probability_up_6m"])
    if not lowest <= probability <= highest:
        fixed = min(highest, max(lowest, probability))
        warnings.append(
            f"The judge's probability_up_6m {probability:g} was outside the analysts' final range "
            f"({min(chances)}-{max(chances)}%, give or take {PROBABILITY_SLACK} points); used {fixed:g}."
        )
        raw["probability_up_6m"] = fixed
    price = stats.price
    lows = [final.potential_low for final in finals]
    floor, ceiling = min(lows) - LOW_SLACK * price, max(lows) + LOW_SLACK * price
    low = float(raw["potential_low"])
    if not floor <= low <= ceiling:
        fixed = min(ceiling, max(floor, low))
        fixed = round(fixed, 2) if abs(fixed) >= 1 else round(fixed, 4)
        warnings.append(
            f"The judge's potential_low {plain_price(low)} was more than {LOW_SLACK * 100:g}% of the price away from "
            f"the analysts' ({' and '.join(plain_price(value) for value in lows)}); used {plain_price(fixed)}."
        )
        raw["potential_low"] = fixed
    cap = confidence_cap([final.verdict for final in finals])
    if cap is not None and _CONFIDENCE_RANK[raw["confidence"]] > _CONFIDENCE_RANK[cap]:
        why = "opposite" if cap == "low" else "different"
        warnings.append(
            f"The judge's confidence {raw['confidence']} was capped at {cap}: the analysts' final verdicts were {why} "
            f"({' and '.join(verdict_label(final.verdict) for final in finals)})."
        )
        raw["confidence"] = cap
    analysis = sanitize(raw, stats)
    return replace(analysis, warnings=[*warnings, *analysis.warnings])


# --- the replies ---------------------------------------------------------------------------------------------------


def _unwrap(data: Any) -> dict:
    """The reply with its analysis fields at the top level, also when a model nested them: {"analysis": {...},
    "critique": [...]}."""
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object, got {type(data).__name__}.")
    if "verdict" in data:
        return data
    nested = [value for value in data.values() if isinstance(value, dict) and "verdict" in value]
    if len(nested) == 1:
        extras = {key: value for key, value in data.items() if not isinstance(value, dict)}
        return {**nested[0], **extras}
    return data


def _points(value: Any, key: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f'"{key}" must be a list of strings.')
    points = [" ".join(item.split()) for item in value if item.strip()]
    return [point if len(point) <= MAX_ITEM_CHARS else point[: MAX_ITEM_CHARS - 1].rstrip() + "…" for point in points][
        :MAX_POINTS
    ]


def _flag(value: Any, key: str) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    word = str(value).strip().lower()
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return False
    raise ValueError(f'"{key}" must be true or false, got {value!r}.')


def validate_rebuttal(data: Any) -> dict:
    """A rebuttal reply: validate_analysis's fields plus critique and concessions (at most MAX_POINTS strings each)
    and changed_mind (a bool; "true"/"false" in words are fine). ValueError says what's wrong."""
    data = _unwrap(data)
    result = validate_analysis(data)
    result["critique"] = _points(data.get("critique"), "critique")
    result["concessions"] = _points(data.get("concessions"), "concessions")
    result["changed_mind"] = _flag(data.get("changed_mind"), "changed_mind")
    return result


def validate_ruling(data: Any) -> dict:
    """A judge's reply: validate_analysis's fields plus debate_summary (a non-empty string), agreement (high, medium
    or low) and favoured ("A", "B" or "neither"; "Analyst A" and a missing value are fine)."""
    data = _unwrap(data)
    result = validate_analysis(data)
    summary = data.get("debate_summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError('"debate_summary" must be a non-empty string.')
    result["debate_summary"] = " ".join(summary.split())
    agreement = str(data.get("agreement") or "").strip().lower()
    if agreement not in AGREEMENTS:
        raise ValueError(f'"agreement" must be one of {", ".join(AGREEMENTS)}; got {data.get("agreement")!r}.')
    result["agreement"] = agreement
    favoured = " ".join(str(data.get("favoured") or "").strip().lower().split())
    if favoured in _FAVOURED:
        result["favoured"] = _FAVOURED[favoured]
    elif favoured in _NEITHER:
        result["favoured"] = None
    else:
        raise ValueError(f'"favoured" must be "A", "B" or "neither"; got {data.get("favoured")!r}.')
    return result


# --- positions as the prompts show them ------------------------------------------------------------------------------


def position_text(
    analysis: Analysis,
    currency: str,
    *,
    critique: Sequence[str] = (),
    concessions: Sequence[str] = (),
    critique_heading: str = "Its critique of the other analysis",
    concessions_heading: str = "What it accepted from the other analysis",
    hide: Sequence[str] = (),
) -> str:
    """An analysis (and its critique and concessions) as plain text for a rebuttal or the judge's prompt. Angle
    brackets become ‹ › so the text can't close its <analysis> element, and the names in hide (the debaters' models)
    are replaced by "the analyst", so no position gives away which model wrote it."""
    lines = [
        f"Verdict: {analysis.verdict} (confidence: {analysis.confidence})",
        f"probability_up_6m: {analysis.probability_up_6m}",
        f"potential_low: {plain_price(analysis.potential_low)}; entry_price: {plain_price(analysis.entry_price)}; "
        f"target_price: {plain_price(analysis.target_price)} ({currency})",
        f"Fear: {analysis.fear}",
        f"Fundamental impact: {analysis.fundamental_impact}",
        f"Thesis: {analysis.thesis}",
    ]
    for heading, items in (
        ("Risks", analysis.risks),
        ("Catalysts", analysis.catalysts),
        ("Checks", analysis.checks),
        (critique_heading, critique),
        (concessions_heading, concessions),
    ):
        if items:
            lines.append(f"{heading}:")
            lines += [f"- {item}" for item in items]
    text = "\n".join(lines).replace("<", "‹").replace(">", "›")
    for name in sorted({name for name in hide if name}, key=len, reverse=True):
        text = re.sub(re.escape(name), "the analyst", text, flags=re.IGNORECASE)
    return text


# --- the debate ------------------------------------------------------------------------------------------------------


def debate_candidate(
    panel: DebatePanel,
    candidate: Candidate,
    *,
    fundamentals: Fundamentals | None,
    extra_news: list[Article],
    now: datetime,
    sec_ticker: str | None = None,
    config: DebateConfig | None = None,
    alert_rules: Sequence[AlertConfig] = (),
    meter: Meter | None = None,
) -> DebateResult:
    """Debate one candidate (see the module docstring) and return the scored Opportunity with its Debate, and the
    calls that failed. alert_rules are the recipients' rules for the agreement check (default: [alerts]'s defaults);
    meter wraps every model call with its step (default: none).

    Raises like analyze.analyze_candidate when both openings fail (LLMSetupError, LLMUnavailableError, LLMError).
    """
    now = utc(now)
    config = config or DebateConfig()
    rules = list(alert_rules) or [AlertConfig()]
    stats = candidate.stats
    fields = case_fields(candidate, fundamentals=fundamentals, extra_news=extra_news, now=now, sec_ticker=sec_ticker)
    day = now.date()
    debaters = panel.debaters
    order = analyst_order(candidate.ticker, day)
    label_of = {index: LABELS[position] for position, index in enumerate(order)}
    primary_index = judge_index(candidate.ticker, day)
    hide = [name for debater in debaters for name in (debater.label, debater.model_name)]
    if panel.judge is not None:
        hide += [panel.judge.label, panel.judge.model_name]
    failures: list[DebaterFailure] = []

    def call(debater: DebaterModel, step: str) -> ChatModel:
        return meter(debater.model, step) if meter is not None else debater.model

    # 1. The openings, both at once.
    prompt = prompts.ANALYSIS_PROMPT.format(**fields)
    jobs = []
    for debater in debaters:
        model = call(debater, STEP_OPENING)
        jobs.append(lambda model=model: ask_analysis(model, prompts.ANALYSIS_SYSTEM, prompt, candidate))
    openings = _both(jobs)
    failed = [index for index, (_, error) in enumerate(openings) if error is not None]
    if len(failed) == 2:
        raise _both_failed([(debaters[index], openings[index][1]) for index in failed])
    if failed:
        loser, winner = debaters[failed[0]], debaters[1 - failed[0]]
        error = openings[failed[0]][1]
        analysis = openings[1 - failed[0]][0]
        assert error is not None and analysis is not None
        failures.append(DebaterFailure(loser.label, "opening", error, other=winner.label))
        _log_failure(candidate.ticker, loser.label, "opening", error)
        side = _Side(winner, label_of[1 - failed[0]], analysis, analysis)
        debate = Debate(
            mode="single",
            reason=f"{loser.label} failed, so {winner.label} analysed it alone: {_one_line(error)}",
            rounds=0,
            participants=[side.participant()],
            judge=None,
            summary=None,
            agreement=None,
            favoured=None,
        )
        model = f"{winner.model_name} alone ({loser.model_name} unavailable)"
        return DebateResult(to_opportunity(candidate, analysis, now=now, model=model, debate=debate), failures)

    sides = [
        _Side(debater, label_of[index], opening, opening)
        for index, (debater, (opening, _)) in enumerate(zip(debaters, openings, strict=True))
    ]
    names = " vs ".join(debater.model_name for debater in debaters)
    primary, secondary = sides[primary_index], sides[1 - primary_index]

    # 2. The agreement check.
    found = disagreements(sides[0].opening, sides[1].opening, price=stats.price, config=config, rules=rules)
    if config.when == "disagree" and not found:
        analysis = merge(primary.opening, secondary.opening, stats)
        summary = (
            f"Both analysts called it {verdict_label(analysis.verdict)} with close numbers (chances up of "
            f"{sides[0].opening.probability_up_6m}% and {sides[1].opening.probability_up_6m}%), so their analyses "
            "were merged without a rebuttal or a judge."
        )
        debate = _record("agreed", sides, rounds=0, summary=summary, agreement="high")
        log.info("%s: the two analysts agreed; merged without a judge.", candidate.ticker)
        return DebateResult(
            to_opportunity(candidate, analysis, now=now, model=f"debate: {names}, agreed", debate=debate), failures
        )
    if found:
        log.info("%s: the analysts disagree: %s.", candidate.ticker, "; ".join(found))

    # 3. The rebuttals.
    rounds = 0
    for number in range(1, config.rounds + 1):
        rounds = number
        jobs = []
        for me, other in ((sides[0], sides[1]), (sides[1], sides[0])):
            text = prompts.DEBATE_REBUTTAL_PROMPT.format(
                **fields,
                round=number,
                rounds=config.rounds,
                own_position=position_text(me.final, stats.currency, hide=hide),
                other_position=position_text(
                    other.final,
                    stats.currency,
                    critique=other.critique,
                    concessions=other.concessions,
                    critique_heading="Its critique of your analysis",
                    concessions_heading="What it accepted from your analysis",
                    hide=hide,
                ),
            )
            model = call(me.debater, STEP_REBUTTAL)
            jobs.append(
                lambda model=model, text=text: complete_json(
                    model, prompts.DEBATE_REBUTTAL_SYSTEM, text, validate=validate_rebuttal
                )
            )
        for side, (reply, error) in zip(sides, _both(jobs), strict=True):
            if error is not None:
                failures.append(DebaterFailure(side.debater.label, "rebuttal", error))
                _log_failure(candidate.ticker, side.debater.label, f"rebuttal {number}", error)
                continue
            assert reply is not None
            final = sanitize(reply, stats)
            side.changed_mind = side.changed_mind or reply["changed_mind"] or final.verdict != side.final.verdict
            side.final, side.critique, side.concessions = final, reply["critique"], reply["concessions"]

    # 4. The judge.
    judge = panel.judge or debaters[primary_index]
    by_label = {side.label: side for side in sides}
    positions = {
        label: position_text(
            side.final,
            stats.currency,
            critique=side.critique,
            concessions=side.concessions,
            critique_heading=f"Its critique of Analyst {_other_label(label)}",
            concessions_heading=f"What it accepted from Analyst {_other_label(label)}",
            hide=hide,
        )
        for label, side in by_label.items()
    }
    text = prompts.DEBATE_JUDGE_PROMPT.format(**fields, position_a=positions["A"], position_b=positions["B"])
    finals = [side.final for side in sides]
    try:
        ruling = complete_json(call(judge, STEP_JUDGE), prompts.DEBATE_JUDGE_SYSTEM, text, validate=validate_ruling)
    except Exception as error:  # LLMError, LLMSetupError, a bug: the positions are merged by rule instead
        failures.append(DebaterFailure(judge.label, "judge", error))
        _log_failure(candidate.ticker, judge.label, "judge", error)
        analysis = merge(primary.final, secondary.final, stats)
        agreement = agreement_of(finals[0], finals[1], price=stats.price, config=config)
        summary = (
            f"The judge wasn't available, so the two final positions were merged by rule: "
            f"{verdict_label(analysis.verdict)}, {analysis.probability_up_6m}% chance up."
        )
        debate = _record(
            "debate",
            sides,
            rounds=rounds,
            summary=summary,
            agreement=agreement,
            reason=f"The judge {judge.label} failed: {_one_line(error)}",
        )
        model = f"debate: {names}, merged without a judge"
        return DebateResult(to_opportunity(candidate, analysis, now=now, model=model, debate=debate), failures)

    # 5. The guardrails.
    analysis = guard_ruling(ruling, finals, stats)
    for warning in analysis.warnings:
        log.info("%s: fixed the ruling: %s", candidate.ticker, warning)
    favoured = by_label[ruling["favoured"]].debater.label if ruling["favoured"] else None
    debate = _record(
        "debate",
        sides,
        rounds=rounds,
        summary=ruling["debate_summary"],
        agreement=ruling["agreement"],
        judge=judge.label,
        favoured=favoured,
    )
    model = f"debate: {names}, judged by {judge.model_name}"
    return DebateResult(to_opportunity(candidate, analysis, now=now, model=model, debate=debate), failures)


def _record(
    mode: str,
    sides: Sequence[_Side],
    *,
    rounds: int,
    summary: str | None,
    agreement: str | None,
    judge: str | None = None,
    favoured: str | None = None,
    reason: str | None = None,
) -> Debate:
    return Debate(
        mode=mode,
        reason=reason,
        rounds=rounds,
        participants=[side.participant() for side in sides],  # in LLM_DEBATERS order; label says which is A
        judge=judge,
        summary=summary,
        agreement=agreement,
        favoured=favoured,
    )


def _other_label(label: str) -> str:
    return "B" if label == "A" else "A"


def _both(jobs: Sequence[Callable[[], Any]]) -> list[tuple[Any, Exception | None]]:
    """Run two calls at the same time; (result, None) or (None, the error) for each, in order."""
    with ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix="dip-debate") as pool:
        futures = [pool.submit(job) for job in jobs]
        outcomes: list[tuple[Any, Exception | None]] = []
        for future in futures:
            try:
                outcomes.append((future.result(), None))
            except Exception as error:
                outcomes.append((None, error))
        return outcomes


def _both_failed(failed: Sequence[tuple[DebaterModel, Exception]]) -> Exception:
    """The error of an analysis whose two openings failed, with both reasons."""
    errors = [error for _, error in failed]
    text = "both debaters failed: " + "; ".join(f"{debater.label}: {error}" for debater, error in failed)
    if all(isinstance(error, LLMSetupError) for error in errors):
        return LLMSetupError(text)
    if any(isinstance(error, LLMUnavailableError) for error in errors):
        return LLMUnavailableError(text)
    if all(isinstance(error, ConfigError) for error in errors):
        return ConfigError(text)
    if any(isinstance(error, LLMError | LLMSetupError) for error in errors):
        return LLMError(text)
    return errors[0]


def _log_failure(ticker: str, label: str, stage: str, error: Exception) -> None:
    expected = isinstance(error, LLMError | LLMSetupError | ConfigError)
    log.warning("%s: the debate's %s failed its %s: %s", ticker, label, stage, error, exc_info=not expected)


def _one_line(error: Exception) -> str:
    text = " ".join(str(error).split()) or type(error).__name__
    return text if len(text) <= 300 else text[:299].rstrip() + "…"
