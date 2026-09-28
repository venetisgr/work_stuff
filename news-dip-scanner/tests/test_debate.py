"""The two-model debate (debate.py) with scripted fake models: no network, no sleeping."""

from __future__ import annotations

import re
from dataclasses import asdict, replace
from datetime import date, timedelta

import pytest
from conftest import NOW, FakeChatModel, make_analysis, make_candidate, make_stats

from dip_scanner import prompts
from dip_scanner.analyze import sanitize, score
from dip_scanner.config import AlertConfig, ConfigError, DebateConfig
from dip_scanner.debate import (
    MAX_POINTS,
    STEP_JUDGE,
    STEP_OPENING,
    STEP_REBUTTAL,
    agreement_of,
    analyst_order,
    confidence_cap,
    debate_candidate,
    disagreements,
    guard_ruling,
    judge_index,
    merge,
    position_text,
    validate_rebuttal,
    validate_ruling,
)
from dip_scanner.llm import DebatePanel, DebaterModel, LLMError, LLMSetupError, LLMUnavailableError

GPT, CLAUDE = "openai:gpt-5", "anthropic:claude-sonnet-5"
DAY = NOW.date()
PRICE = 142.5  # make_stats()'s
# Words that would tell a debater or the judge which model wrote an analysis.
IDENTITIES = re.compile(r"openai|anthropic|gpt|claude|sonnet|chatgpt", re.IGNORECASE)


def reply(**overrides) -> dict:
    """An analysis reply (make_analysis()'s fields, without the warnings sanitize adds)."""
    values = {key: value for key, value in asdict(make_analysis()).items() if key != "warnings"}
    return {**values, **overrides}


def rebuttal(critique=(), concessions=(), changed_mind=False, **overrides) -> dict:
    return {
        **reply(**overrides),
        "critique": list(critique),
        "concessions": list(concessions),
        "changed_mind": changed_mind,
    }


def ruling(summary="The analysts disagree on the verdict.", agreement="medium", favoured="A", **overrides) -> dict:
    return {**reply(**overrides), "debate_summary": summary, "agreement": agreement, "favoured": favoured}


def model(opening, rebuttals=None, judged=None, *, name: str) -> FakeChatModel:
    """A fake model that answers each kind of prompt: its opening, its rebuttal(s) in order, its ruling. A reply can be
    an exception to raise."""
    queue = list(rebuttals) if isinstance(rebuttals, list) else [rebuttals]

    def answer(system: str, prompt: str, json_mode: bool):
        if system == prompts.ANALYSIS_SYSTEM:
            return opening
        if system == prompts.DEBATE_REBUTTAL_SYSTEM:
            return queue.pop(0) if len(queue) > 1 else queue[0]
        if system == prompts.DEBATE_JUDGE_SYSTEM:
            return judged
        raise AssertionError(f"unexpected system prompt: {system[:60]}")

    return FakeChatModel(answer, name=name)


def panel(gpt: FakeChatModel, claude: FakeChatModel, judge: DebaterModel | None = None) -> DebatePanel:
    return DebatePanel(debaters=(DebaterModel(GPT, gpt), DebaterModel(CLAUDE, claude)), judge=judge)


def steps(fake: FakeChatModel) -> list[str]:
    names = {
        prompts.ANALYSIS_SYSTEM: STEP_OPENING,
        prompts.DEBATE_REBUTTAL_SYSTEM: STEP_REBUTTAL,
        prompts.DEBATE_JUDGE_SYSTEM: STEP_JUDGE,
    }
    return [names[system] for system, _, _ in fake.calls]


def run(debate_panel: DebatePanel, *, candidate=None, **kwargs):
    return debate_candidate(
        debate_panel, candidate or make_candidate(), fundamentals=None, extra_news=[], now=NOW, **kwargs
    )


def labels() -> dict[str, str]:
    """{"A": "provider:model", "B": ...} for AMD today."""
    first, second = analyst_order("AMD", DAY)
    return {"A": (GPT, CLAUDE)[first], "B": (GPT, CLAUDE)[second]}


def judge_label() -> str:
    return (GPT, CLAUDE)[judge_index("AMD", DAY)]


# --- who is A and who judges ---------------------------------------------------------------------------------------


def test_the_order_and_the_judge_are_fixed_per_ticker_and_day_and_take_turns():
    tickers = [f"T{number}" for number in range(40)]
    orders = {ticker: analyst_order(ticker, DAY) for ticker in tickers}
    judges = {ticker: judge_index(ticker, DAY) for ticker in tickers}
    assert orders == {ticker: analyst_order(ticker, DAY) for ticker in tickers}  # deterministic
    assert set(orders.values()) == {(0, 1), (1, 0)}  # either debater can be Analyst A...
    assert set(judges.values()) == {0, 1}  # ...and either can judge: neither side always does
    assert analyst_order("amd", DAY) == analyst_order(" AMD ", DAY)
    later = [analyst_order("AMD", DAY + timedelta(days=offset)) for offset in range(20)]
    assert len(set(later)) == 2  # the order changes from day to day


# --- the agreement check and the merge -----------------------------------------------------------------------------


def test_disagreements_are_decided_by_fixed_rules():
    config = DebateConfig()
    base = make_analysis(probability_up_6m=60, confidence="medium")
    assert disagreements(base, base, price=PRICE, config=config) == []
    assert disagreements(base, replace(base, probability_up_6m=75), price=PRICE, config=config) == []  # 15: not more
    [gap] = disagreements(base, replace(base, probability_up_6m=76), price=PRICE, config=config)
    assert "60% and 76% are 16 points apart" in gap
    [verdicts] = disagreements(base, replace(base, verdict="mixed"), price=PRICE, config=config)
    assert verdicts == "verdicts differ (Temporary fear against Mixed)"
    # 118 against 103.5: 10.2% of 142.50 apart.
    [lows] = disagreements(base, replace(base, potential_low=103.5), price=PRICE, config=config)
    assert "potential lows 118.00 and 103.50 are 10.2% of the price apart" in lows
    assert disagreements(base, replace(base, potential_low=104.0), price=PRICE, config=config) == []  # 9.8%


def test_one_opening_passing_somebodys_alert_rules_is_a_disagreement():
    config = DebateConfig()
    low = make_analysis(probability_up_6m=62, confidence="medium")
    high = replace(low, probability_up_6m=75, confidence="high")
    assert score(low, PRICE) < 65 <= score(high, PRICE)
    lenient, strict = AlertConfig(), AlertConfig(min_score=95)
    [found] = disagreements(low, high, price=PRICE, config=config, rules=[strict, lenient])
    assert found.startswith("only one of them passes alert rules")
    assert disagreements(low, high, price=PRICE, config=config, rules=[strict]) == []  # neither passes these
    # Somebody alerting on mixed verdicts too makes no difference when both are temporary fear.
    assert disagreements(low, high, price=PRICE, config=config, rules=[AlertConfig(min_score=40)]) == []


def test_merge_takes_the_mean_chance_and_target_and_the_cautious_rest():
    stats = make_stats()
    primary = make_analysis(
        probability_up_6m=61, potential_low=120.0, entry_price=131.0, target_price=166.0, confidence="high",
        risks=["Capex cuts", "Rates"], checks=["Read the call"], catalysts=["Earnings"], fear="Primary's fear",
    )  # fmt: skip
    other = make_analysis(
        probability_up_6m=64, potential_low=117.0, entry_price=134.0, target_price=171.0, confidence="medium",
        risks=["capex  cuts.", "Export rules"], checks=["Check orders"], catalysts=["A buyback"], fear="Other's fear",
    )  # fmt: skip
    merged = merge(primary, other, stats)
    assert merged.probability_up_6m == 63  # 62.5, rounded half up
    assert (merged.potential_low, merged.entry_price, merged.target_price) == (117.0, 131.0, 168.5)
    assert merged.confidence == "medium" and merged.verdict == "temporary_fear"
    assert merged.fear == "Primary's fear" and merged.catalysts == ["Earnings"]
    assert merged.risks == ["Capex cuts", "Rates", "Export rules"]  # "capex  cuts." is a repeat
    assert merged.checks == ["Read the call", "Check orders"]
    assert merged.warnings == []

    # When a judge failed, the verdicts may differ: the more cautious one, with the confidence capped.
    opposite = merge(primary, replace(other, verdict="fundamental", confidence="high"), stats)
    assert (opposite.verdict, opposite.confidence) == ("fundamental", "low")
    different = merge(replace(primary, verdict="mixed"), replace(other, confidence="high"), stats)
    assert (different.verdict, different.confidence) == ("mixed", "medium")
    # An entry above the price is kept at the price.
    high_entries = merge(replace(primary, entry_price=150.0), replace(other, entry_price=149.0), stats)
    assert high_entries.entry_price == PRICE


def test_many_risks_are_cut_to_six():
    stats = make_stats()
    primary = make_analysis(risks=[f"Risk {n}" for n in range(5)])
    other = make_analysis(risks=[f"Other risk {n}" for n in range(5)])
    assert merge(primary, other, stats).risks == [*primary.risks, "Other risk 0"]


def test_confidence_caps_and_agreement_levels():
    assert confidence_cap(["temporary_fear", "fundamental"]) == "low"
    assert confidence_cap(["temporary_fear", "mixed"]) == "medium"
    assert confidence_cap(["unclear", "unclear"]) is None
    config = DebateConfig()
    base = make_analysis()
    assert agreement_of(base, base, price=PRICE, config=config) == "high"
    assert agreement_of(base, replace(base, probability_up_6m=90), price=PRICE, config=config) == "medium"
    assert agreement_of(base, replace(base, verdict="fundamental"), price=PRICE, config=config) == "low"


# --- the guardrails ------------------------------------------------------------------------------------------------


def test_the_ruling_stays_near_the_final_positions():
    stats = make_stats()
    finals = [make_analysis(probability_up_6m=60, potential_low=118.0), make_analysis(probability_up_6m=70)]
    # Chance: within 55-75. Low: within 118 +/- 7.125 (5% of 142.50).
    guarded = guard_ruling(reply(probability_up_6m=90, potential_low=100.0), finals, stats)
    assert guarded.probability_up_6m == 75 and guarded.potential_low == pytest.approx(110.875, abs=0.01)
    assert guarded.warnings[0] == (
        "The judge's probability_up_6m 90 was outside the analysts' final range (60-70%, give or take 5 points); "
        "used 75."
    )
    assert guarded.warnings[1].startswith("The judge's potential_low 100.00 was more than 5% of the price away")
    assert guard_ruling(reply(probability_up_6m=40), finals, stats).probability_up_6m == 55
    kept = guard_ruling(reply(probability_up_6m=74, potential_low=124.0), finals, stats)
    assert (kept.probability_up_6m, kept.potential_low, kept.warnings) == (74, 124.0, [])


def test_the_ruling_confidence_is_capped_when_the_final_verdicts_differ():
    stats = make_stats()
    tf, fundamental, mixed = (make_analysis(verdict=verdict) for verdict in ("temporary_fear", "fundamental", "mixed"))
    opposite = guard_ruling(reply(confidence="high"), [tf, fundamental], stats)
    assert opposite.confidence == "low"
    assert opposite.warnings == [
        "The judge's confidence high was capped at low: the analysts' final verdicts were opposite (Temporary fear "
        "and Fundamental damage)."
    ]
    assert guard_ruling(reply(confidence="high"), [tf, mixed], stats).confidence == "medium"
    assert guard_ruling(reply(confidence="low"), [tf, mixed], stats).warnings == []  # already below the cap
    assert guard_ruling(reply(confidence="high"), [tf, tf], stats).confidence == "high"


def test_the_ruling_still_gets_sanitizes_checks_after_the_guardrails():
    finals = [make_analysis(), make_analysis()]
    guarded = guard_ruling(reply(entry_price=150.0), finals, make_stats())
    assert guarded.entry_price == PRICE
    assert guarded.warnings == ["entry_price 150.00 was above the price; used 142.50."]


# --- the replies ---------------------------------------------------------------------------------------------------


def test_rebuttal_replies_are_validated_with_some_tolerance():
    parsed = validate_rebuttal(rebuttal(["Invents a revenue figure"], ["Fair on guidance"], True))
    assert (parsed["critique"], parsed["concessions"], parsed["changed_mind"]) == (
        ["Invents a revenue figure"],
        ["Fair on guidance"],
        True,
    )
    nested = {"analysis": reply(verdict="Mixed"), "critique": "One point", "changed_mind": "no"}
    parsed = validate_rebuttal(nested)
    assert (parsed["verdict"], parsed["critique"], parsed["concessions"], parsed["changed_mind"]) == (
        "mixed",
        ["One point"],
        [],
        False,
    )
    many = validate_rebuttal(rebuttal([f"Point {n}" for n in range(8)]))
    assert len(many["critique"]) == MAX_POINTS
    with pytest.raises(ValueError, match='"critique" must be a list of strings'):
        validate_rebuttal(rebuttal([{"point": 1}]))
    with pytest.raises(ValueError, match='"changed_mind" must be true or false'):
        validate_rebuttal(rebuttal(changed_mind="perhaps"))
    with pytest.raises(ValueError, match="Missing field"):
        validate_rebuttal({"critique": ["x"]})


@pytest.mark.parametrize(
    ("favoured", "expected"), [("A", "A"), ("analyst b", "B"), ("Neither", None), ("none", None), (None, None)]
)
def test_rulings_name_the_favoured_analyst(favoured, expected):
    assert validate_ruling(ruling(favoured=favoured))["favoured"] == expected


def test_rulings_need_a_summary_and_an_agreement():
    assert validate_ruling(ruling(agreement="HIGH"))["agreement"] == "high"
    with pytest.raises(ValueError, match='"debate_summary" must be a non-empty string'):
        validate_ruling(ruling(summary=" "))
    with pytest.raises(ValueError, match='"agreement" must be one of high, medium, low'):
        validate_ruling(ruling(agreement="total"))
    with pytest.raises(ValueError, match='"favoured" must be "A", "B" or "neither"'):
        validate_ruling(ruling(favoured="C"))


def test_positions_are_escaped_and_never_name_a_model():
    analysis = make_analysis(fear="As GPT-5 I see <b>fear</b>", thesis="claude-sonnet-5 disagrees")
    text = position_text(analysis, "USD", critique=["Invented"], hide=[GPT, "gpt-5", CLAUDE, "claude-sonnet-5"])
    assert "‹b›fear‹/b›" in text and "<" not in text
    assert not IDENTITIES.search(text)
    assert "As the analyst I see" in text and "the analyst disagrees" in text
    assert "Its critique of the other analysis:\n- Invented" in text
    assert "potential_low: 118.00; entry_price: 132.00; target_price: 168.00 (USD)" in text


# --- whole debates -------------------------------------------------------------------------------------------------


def test_openings_that_agree_are_merged_without_a_rebuttal_or_a_judge():
    gpt = model(reply(probability_up_6m=60, confidence="high", fear="GPT's fear"), name="gpt-5")
    claude = model(
        reply(probability_up_6m=65, potential_low=121.0, entry_price=130.0, target_price=170.0, fear="Claude's fear"),
        name="claude-sonnet-5",
    )
    result = run(panel(gpt, claude))

    assert steps(gpt) == steps(claude) == [STEP_OPENING]  # nothing else ran
    opp, debate = result.opportunity, result.opportunity.debate
    assert result.failures == []
    assert (debate.mode, debate.rounds, debate.judge, debate.agreement, debate.favoured) == (
        "agreed",
        0,
        None,
        "high",
        None,
    )
    assert "merged without a rebuttal or a judge" in debate.summary
    assert [side.model for side in debate.participants] == [GPT, CLAUDE]  # LLM_DEBATERS order
    assert {side.label: side.model for side in debate.participants} == labels()
    analysis = opp.analysis
    assert (analysis.probability_up_6m, analysis.potential_low, analysis.entry_price, analysis.target_price) == (
        63,
        118.0,
        130.0,
        169.0,
    )
    assert analysis.confidence == "medium"
    primary = judge_label()
    assert analysis.fear == ("GPT's fear" if primary == GPT else "Claude's fear")
    assert opp.model == "debate: gpt-5 vs claude-sonnet-5, agreed"
    assert opp.score == score(analysis, PRICE)


def test_always_debates_even_when_the_openings_agree():
    same = reply()
    gpt = model(same, rebuttal(), ruling(), name="gpt-5")
    claude = model(same, rebuttal(), ruling(), name="claude-sonnet-5")
    result = run(panel(gpt, claude), config=DebateConfig(when="always"))
    judge = gpt if judge_label() == GPT else claude
    other = claude if judge is gpt else gpt
    assert steps(judge) == [STEP_OPENING, STEP_REBUTTAL, STEP_JUDGE]
    assert steps(other) == [STEP_OPENING, STEP_REBUTTAL]
    assert result.opportunity.debate.mode == "debate" and result.opportunity.debate.rounds == 1


def disagreeing_panel(**judge_overrides):
    """GPT reads a temporary fear, Claude fundamental damage; after the rebuttal GPT says mixed."""
    gpt = model(
        reply(probability_up_6m=72, fear="As GPT-5 I read fear."),
        rebuttal(
            ["Assumes a 30% revenue fall; the input gives no revenue figures", "claude-sonnet-5 ignores the volume"],
            ["The guidance cut is real"],
            True,
            verdict="mixed",
            probability_up_6m=64,
            fear="As GPT-5 I read fear.",
        ),
        ruling(**judge_overrides),
        name="gpt-5",
    )
    claude = model(
        reply(verdict="fundamental", probability_up_6m=40, potential_low=105.0, entry_price=120.0, confidence="medium"),
        rebuttal(
            ["Ignores the guidance cut"],
            [],
            False,
            verdict="fundamental",
            probability_up_6m=45,
            potential_low=108.0,
            entry_price=120.0,
            confidence="medium",
        ),
        ruling(**judge_overrides),
        name="claude-sonnet-5",
    )
    return gpt, claude


def test_disagreeing_openings_go_through_a_rebuttal_and_the_judge():
    gpt, claude = disagreeing_panel(verdict="mixed", probability_up_6m=55, potential_low=112.0, confidence="high")
    result = run(panel(gpt, claude))

    opp, debate = result.opportunity, result.opportunity.debate
    judge, judge_model = (gpt, "gpt-5") if judge_label() == GPT else (claude, "claude-sonnet-5")
    assert steps(judge)[-1] == STEP_JUDGE
    assert [step for fake in (gpt, claude) for step in steps(fake)].count(STEP_JUDGE) == 1
    # Each rebuttal saw its own opening and the other one, and never a model's name.
    _, gpt_rebuttal, _ = gpt.calls[1]
    own, other = gpt_rebuttal.split("## The other analyst's analysis")
    assert "Verdict: temporary_fear" in own.split("## Your analysis")[1] and "Verdict: fundamental" in other
    assert "This is rebuttal round 1 of 1." in gpt_rebuttal
    assert not IDENTITIES.search(gpt_rebuttal) and not IDENTITIES.search(claude.calls[1][1])

    # The debate as recorded.
    assert (debate.mode, debate.rounds, debate.judge, debate.agreement) == ("debate", 1, judge_label(), "medium")
    assert debate.summary == "The analysts disagree on the verdict."
    assert debate.favoured == labels()["A"]
    gpt_side, claude_side = debate.participants
    assert (gpt_side.opening.verdict, gpt_side.final.verdict, gpt_side.changed_mind) == (
        "temporary_fear",
        "mixed",
        True,
    )
    assert gpt_side.critique[0] == "Assumes a 30% revenue fall; the input gives no revenue figures"
    assert gpt_side.concessions == ["The guidance cut is real"]
    assert (claude_side.final.probability_up_6m, claude_side.changed_mind) == (45, False)

    # The ruling, with its confidence capped (mixed against fundamental).
    analysis = opp.analysis
    assert (analysis.verdict, analysis.probability_up_6m, analysis.potential_low) == ("mixed", 55, 112.0)
    assert analysis.confidence == "medium"
    assert analysis.warnings[0].startswith("The judge's confidence high was capped at medium")
    assert opp.model == f"debate: gpt-5 vs claude-sonnet-5, judged by {judge_model}"
    assert opp.score == score(analysis, PRICE)


def test_the_judge_sees_anonymous_analysts_in_a_fixed_order():
    gpt, claude = disagreeing_panel()
    run(panel(gpt, claude))
    judge = gpt if judge_label() == GPT else claude
    system, prompt, _ = judge.calls[-1]
    assert system == prompts.DEBATE_JUDGE_SYSTEM
    assert not IDENTITIES.search(prompt), IDENTITIES.search(prompt)
    assert not IDENTITIES.search(system)
    section_a = prompt.split("## Analyst A")[1].split("## Analyst B")[0]
    section_b = prompt.split("## Analyst B")[1]
    gpt_section = section_a if labels()["A"] == GPT else section_b
    assert "Verdict: mixed" in gpt_section and "Assumes a 30% revenue fall" in gpt_section
    assert "As the analyst I read fear." in gpt_section  # "GPT-5" hidden
    assert "the analyst ignores the volume" in gpt_section
    other = "B" if gpt_section is section_a else "A"
    assert f"Its critique of Analyst {other}:" in gpt_section
    # The same ticker on the same day always gives the same prompt.
    again_gpt, again_claude = disagreeing_panel()
    run(panel(again_gpt, again_claude))
    assert (again_gpt if judge is gpt else again_claude).calls[-1][1] == prompt


def test_a_fixed_judge_rules_and_is_named():
    gpt, claude = disagreeing_panel()
    judge = FakeChatModel(ruling(favoured="neither"), name="claude-opus-5")
    result = run(panel(gpt, claude, judge=DebaterModel("anthropic:claude-opus-5", judge)))
    assert [step for fake in (gpt, claude) for step in steps(fake)].count(STEP_JUDGE) == 0
    assert len(judge.calls) == 1 and not IDENTITIES.search(judge.calls[0][1])
    debate = result.opportunity.debate
    assert (debate.judge, debate.favoured) == ("anthropic:claude-opus-5", None)
    assert result.opportunity.model == "debate: gpt-5 vs claude-sonnet-5, judged by claude-opus-5"


def test_zero_rounds_go_straight_to_the_judge_and_two_rounds_carry_the_critique():
    gpt, claude = disagreeing_panel()
    result = run(panel(gpt, claude), config=DebateConfig(rounds=0))
    assert STEP_REBUTTAL not in steps(gpt) + steps(claude)
    assert result.opportunity.debate.rounds == 0 and result.opportunity.debate.participants[0].critique == []

    gpt, claude = disagreeing_panel()
    result = run(panel(gpt, claude), config=DebateConfig(rounds=2))
    assert steps(gpt).count(STEP_REBUTTAL) == steps(claude).count(STEP_REBUTTAL) == 2
    second = claude.calls[2][1]  # Claude's second rebuttal sees GPT's critique of it
    assert "This is rebuttal round 2 of 2." in second
    assert "Its critique of your analysis:\n- Assumes a 30% revenue fall" in second
    assert result.opportunity.debate.rounds == 2


def test_metering_wraps_every_call_with_its_step():
    gpt, claude = disagreeing_panel()
    seen: list[tuple[str, str]] = []

    def meter(inner, step):
        seen.append((inner.name, step))
        return inner

    run(panel(gpt, claude), meter=meter)
    judge = "gpt-5" if judge_label() == GPT else "claude-sonnet-5"
    assert sorted(seen) == sorted(
        [
            ("gpt-5", STEP_OPENING),
            ("claude-sonnet-5", STEP_OPENING),
            ("gpt-5", STEP_REBUTTAL),
            ("claude-sonnet-5", STEP_REBUTTAL),
            (judge, STEP_JUDGE),
        ]
    )


# --- failures ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [LLMSetupError("Anthropic rejected the credentials (401)."), LLMError("claude-sonnet-5 returned an empty reply.")],
)
def test_one_debater_failing_leaves_the_other_alone(error):
    gpt = model(reply(probability_up_6m=66), name="gpt-5")
    claude = model(error, name="claude-sonnet-5")
    result = run(panel(gpt, claude))

    opp, debate = result.opportunity, result.opportunity.debate
    assert steps(gpt) == [STEP_OPENING]
    assert (debate.mode, debate.rounds, debate.judge, debate.summary, debate.agreement) == (
        "single",
        0,
        None,
        None,
        None,
    )
    assert [side.model for side in debate.participants] == [GPT]
    assert debate.reason == f"{CLAUDE} failed, so {GPT} analysed it alone: {error}"
    assert opp.analysis.probability_up_6m == 66 and opp.model == "gpt-5 alone (claude-sonnet-5 unavailable)"
    [failure] = result.failures
    assert (failure.label, failure.provider, failure.stage, failure.other, failure.error) == (
        CLAUDE,
        "anthropic",
        "opening",
        GPT,
        error,
    )


def test_an_unusable_opening_is_retried_once_before_it_counts_as_failed():
    gpt = model(reply(), name="gpt-5")
    claude = FakeChatModel(["not json", "still not json"], name="claude-sonnet-5")
    result = run(panel(gpt, claude))
    assert len(claude.calls) == 2 and result.opportunity.debate.mode == "single"
    assert isinstance(result.failures[0].error, LLMError)


@pytest.mark.parametrize(
    ("errors", "raised"),
    [
        ((LLMSetupError("no quota"), LLMSetupError("bad key")), LLMSetupError),
        ((LLMSetupError("bad key"), LLMUnavailableError("overloaded")), LLMUnavailableError),
        ((LLMError("refused"), LLMUnavailableError("timed out")), LLMUnavailableError),
        ((LLMError("refused"), LLMSetupError("bad key")), LLMError),
        ((LLMError("refused"), LLMError("filtered")), LLMError),
        ((ConfigError("broken"), ConfigError("broken too")), ConfigError),
    ],
)
def test_both_debaters_failing_fails_like_a_single_analysis(errors, raised):
    gpt, claude = model(errors[0], name="gpt-5"), model(errors[1], name="claude-sonnet-5")
    with pytest.raises(raised) as error:
        run(panel(gpt, claude))
    assert type(error.value) is raised
    assert str(error.value) == f"both debaters failed: {GPT}: {errors[0]}; {CLAUDE}: {errors[1]}"


def test_a_failed_judge_leaves_the_final_positions_merged_by_rule():
    failing = LLMError("the judge's reply was unusable")
    gpt, claude = disagreeing_panel()
    for fake in (gpt, claude):
        fake._fn = _failing_judge(fake._fn, failing)
    result = run(panel(gpt, claude))

    opp, debate = result.opportunity, result.opportunity.debate
    assert (debate.mode, debate.judge, debate.favoured) == ("debate", None, None)
    assert debate.reason == f"The judge {judge_label()} failed: the judge's reply was unusable"
    assert debate.agreement == "medium"  # mixed against fundamental
    assert debate.summary.startswith("The judge wasn't available, so the two final positions were merged by rule")
    # The merge of the finals: mixed 64 (low 118) and fundamental 45 (low 108).
    analysis = opp.analysis
    assert (analysis.verdict, analysis.probability_up_6m, analysis.potential_low) == ("fundamental", 55, 108.0)
    assert analysis.confidence == "medium"
    assert opp.model == "debate: gpt-5 vs claude-sonnet-5, merged without a judge"
    [failure] = result.failures
    assert (failure.stage, failure.label, failure.error) == ("judge", judge_label(), failing)


def _failing_judge(answer, error):
    def wrapped(system, prompt, json_mode):
        return error if system == prompts.DEBATE_JUDGE_SYSTEM else answer(system, prompt, json_mode)

    return wrapped


def test_a_failed_rebuttal_keeps_the_earlier_position():
    gpt, _ = disagreeing_panel()
    claude = model(
        reply(verdict="fundamental", probability_up_6m=40, potential_low=105.0, confidence="medium"),
        LLMUnavailableError("Anthropic had a server error (529)"),
        ruling(),
        name="claude-sonnet-5",
    )
    result = run(panel(gpt, claude))
    gpt_side, claude_side = result.opportunity.debate.participants
    assert claude_side.final == claude_side.opening and claude_side.critique == []
    assert gpt_side.final.verdict == "mixed"
    assert [(failure.label, failure.stage) for failure in result.failures] == [(CLAUDE, "rebuttal")]
    assert result.opportunity.debate.judge == judge_label()  # the judge still ruled


def test_the_debate_keeps_its_stats_and_news_like_a_single_analysis():
    gpt, claude = disagreeing_panel()
    candidate = make_candidate(dip_reasons=["down 7.0% today"])
    opp = run(panel(gpt, claude), candidate=candidate).opportunity
    assert opp.dip_reasons == ["down 7.0% today"] and opp.article_ids == [candidate.impacts[0][1].id]
    assert opp.created == NOW and opp.price == PRICE and opp.stats == candidate.stats
    # Every call saw the same case.
    for fake in (gpt, claude):
        for _, prompt, _ in fake.calls:
            assert "Why it was flagged: down 7.0% today" in prompt


def test_sanitize_fixes_rebuttal_numbers_too():
    gpt, claude = disagreeing_panel()
    claude = model(
        reply(verdict="fundamental", probability_up_6m=40),
        rebuttal(verdict="fundamental", probability_up_6m=140),
        ruling(),
        name="claude-sonnet-5",
    )
    result = run(panel(gpt, claude))
    final = result.opportunity.debate.participants[1].final
    assert final == sanitize({**rebuttal(verdict="fundamental", probability_up_6m=140)}, make_stats())
    assert final.probability_up_6m == 100 and final.warnings


def test_the_day_decides_the_order_not_the_time():
    assert analyst_order("AMD", date(2026, 9, 25)) == analyst_order("AMD", NOW.date())
