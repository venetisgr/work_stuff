from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from conftest import NOW, make_analysis, make_debate, make_opportunity, make_stats

from dip_scanner.models import Opportunity, PriceBar, Split
from dip_scanner.track import (
    FINAL_ROW,
    HORIZON_DAYS,
    STATUSES,
    Outcome,
    benchmark_for,
    brier,
    evaluate,
    model_scoreboard,
    quote_day,
    render_track_record,
    score_bucket,
    signal_day,
    summarize,
    with_account_return,
    with_benchmark,
)

# make_opportunity(): AMD reported at NOW (Friday 2026-09-25 15:00 UTC) at $142.50 with the price taken 14:45 UTC the
# same day; make_analysis(): potential low 118, entry 132, target 168.
SIGNAL = date(2026, 9, 25)
LATER = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def bar(day: date, high: float, low: float, close: float, open_: float | None = None) -> PriceBar:
    return PriceBar(day=day, open=close if open_ is None else open_, high=high, low=low, close=close, volume=1000)


def test_signal_day_is_the_utc_date_of_the_report_without_a_time_zone():
    created = datetime(2026, 9, 26, 1, 30, tzinfo=UTC)  # still the 25th in New York
    assert signal_day(make_opportunity(created=created)) == date(2026, 9, 26)
    assert signal_day(make_opportunity()) == SIGNAL


def test_signal_day_is_the_exchange_local_date_when_the_time_zone_is_known():
    """Regression: an ASX report written in the first trading hour (23:30 UTC the day before, AEDT) was matched with
    the previous session, and trading from before the report counted as fills."""
    created = datetime(2026, 1, 13, 23, 30, tzinfo=UTC)  # 10:30 on the 14th in Sydney
    stats = make_stats(ticker="BHP.AX", price=48.0, as_of=created, timezone="Australia/Sydney")
    analysis = make_analysis(entry_price=47.60, potential_low=45.0, target_price=52.0)
    opp = make_opportunity(ticker="BHP.AX", stats=stats, created=created, analysis=analysis)
    assert signal_day(opp) == quote_day(opp) == date(2026, 1, 14)

    bars = [
        bar(date(2026, 1, 13), high=48.5, low=47.58, close=47.9),  # the session before the report: ignored
        bar(date(2026, 1, 14), high=48.6, low=47.3, close=48.2),  # only 48.0..48.2 surely came after the report
    ]
    outcome = evaluate(opp, bars, now=datetime(2026, 1, 20, tzinfo=UTC))
    assert outcome.entry_filled is None
    assert outcome.status == "waiting_entry"


# --- splits: Yahoo's bars are split-adjusted after the fact --------------------------------------------------------


def _scaled(bars: list[PriceBar], factor: float) -> list[PriceBar]:
    return [PriceBar(b.day, b.open * factor, b.high * factor, b.low * factor, b.close * factor, b.volume) for b in bars]


def _comparable(outcome: Outcome) -> tuple:
    return (
        outcome.status,
        outcome.entry_filled,
        outcome.target_hit,
        outcome.low_breached,
        outcome.up_after_6m,
        round(outcome.return_pct, 6),
        round(outcome.max_gain_pct, 6),
        round(outcome.max_loss_pct, 6),
        None if outcome.trade_return_pct is None else round(outcome.trade_return_pct, 6),
    )


SPLIT_SCENARIO = [  # as traded, in the report's (pre-split) units
    bar(SIGNAL, high=145, low=139, close=140, open_=144),
    bar(date(2026, 9, 28), high=141, low=131, close=135),  # fill at 132
    bar(date(2026, 10, 12), high=150, low=133, close=148),  # the split's ex-date
    bar(date(2026, 11, 2), high=170, low=150, close=166),  # target 168
    bar(date(2027, 3, 26), high=160, low=150, close=155),  # the 6-month close
]


@pytest.mark.parametrize("ratio", [10.0, 0.1])  # a 10:1 split and a 1:10 reverse split
def test_a_later_split_gives_the_same_outcome_as_unsplit_prices(ratio):
    """Regression: NVDA's 10:1 split turned a +20% idea into "below the low, -87.5%" because the report's $1,164 was
    compared with Yahoo's split-adjusted $116 bars."""
    now = datetime(2027, 4, 20, tzinfo=UTC)
    stats = make_stats(timezone="America/New_York")
    opp = make_opportunity(stats=stats)
    unsplit = evaluate(opp, SPLIT_SCENARIO, now=now)
    assert unsplit.status == "target_hit" and unsplit.entry_filled == date(2026, 9, 28)

    yahoo = _scaled(SPLIT_SCENARIO, 1 / ratio)  # Yahoo rewrites the whole history on the new basis
    adjusted = evaluate(opp, yahoo, now=now, splits=[Split(date(2026, 10, 12), ratio)])

    assert _comparable(adjusted) == _comparable(unsplit)
    assert adjusted.split_factor == ratio and not adjusted.price_mismatch
    assert adjusted.last_price == pytest.approx(155 / ratio)  # on today's basis

    # Without the split the same bars give nonsense, and the price check catches it.
    wrong = evaluate(opp, yahoo, now=now)
    assert wrong.price_mismatch


def test_a_split_on_the_quote_day_is_already_in_the_price():
    """The report was written after the open on the ex-date: its price is post-split, so nothing is scaled."""
    now = datetime(2026, 10, 20, tzinfo=UTC)
    opp = make_opportunity(stats=make_stats(timezone="America/New_York"))
    bars = SPLIT_SCENARIO[:2]
    plain = evaluate(opp, bars, now=now)
    same_day = evaluate(opp, bars, now=now, splits=[Split(SIGNAL, 10.0), Split(date(2026, 9, 1), 2.0)])
    assert same_day.split_factor == 1.0
    assert _comparable(same_day) == _comparable(plain)


def test_price_mismatches_are_left_out_of_the_summary_and_marked():
    now = datetime(2026, 10, 20, tzinfo=UTC)
    good = evaluate(make_opportunity(), SPLIT_SCENARIO[:2], now=now)
    bad = evaluate(make_opportunity(ticker="BAD"), _scaled(SPLIT_SCENARIO[:2], 0.5), now=now)  # a 2:1 split, unreported
    assert bad.price_mismatch and not good.price_mismatch

    summary = summarize([good, bad])
    assert summary["count"] == 1 and summary["price_mismatch"] == 1
    text = render_track_record([good, bad], summary)
    assert "1 opportunity is left out of the figures" in text
    assert "(price mismatch, left out)" in next(line for line in text.splitlines() if "**BAD**" in line)

    split = evaluate(
        make_opportunity(), _scaled(SPLIT_SCENARIO[:2], 0.1), now=now, splits=[Split(SIGNAL.replace(day=30), 10)]
    )
    assert "(after a 10:1 split)" in render_track_record([split], summarize([split]))


def test_tickers_without_prices_are_named_in_the_track_record():
    outcomes = [evaluate(make_opportunity(), SPLIT_SCENARIO[:2], now=LATER)]
    text = render_track_record(outcomes, summarize(outcomes), missing=[("OPAP.AT", 2)])
    assert "Left out: 2 opportunities without prices from Yahoo Finance" in text and "OPAP.AT (2)" in text
    assert "OPAP.AT" in render_track_record([], summarize([]), missing=[("OPAP.AT", 1)])


def test_hand_computed_outcome_with_fill_then_target():
    bars = [
        bar(date(2026, 9, 24), high=150, low=100, close=149),  # before the signal: ignored
        bar(date(2026, 9, 25), high=151, low=128, close=140, open_=150),  # signal day: only 140..142.5 counts
        bar(date(2026, 9, 28), high=141, low=131, close=135),  # low 131 <= entry 132: filled
        bar(date(2026, 9, 29), high=170, low=134, close=165),  # high 170 >= target 168, a day after the fill
        bar(date(2026, 9, 30), high=166, low=160, close=162),
    ]
    outcome = evaluate(make_opportunity(), bars, now=LATER)

    assert outcome.entry_filled == date(2026, 9, 28)
    assert outcome.target_hit == date(2026, 9, 29)
    assert outcome.low_breached is None
    assert outcome.status == "target_hit"
    assert outcome.last_price == 162
    assert outcome.return_pct == pytest.approx((162 / 142.5 - 1) * 100)  # +13.68%
    assert outcome.max_gain_pct == pytest.approx((170 / 142.5 - 1) * 100)  # +19.30%, not the signal day's 151
    assert outcome.max_loss_pct == pytest.approx((131 / 142.5 - 1) * 100)  # -8.07%, not the signal day's 128
    assert outcome.trade_return_pct == pytest.approx((168 / 132 - 1) * 100)  # +27.27%: bought 132, sold 168
    assert outcome.days == 10
    assert outcome.up_after_6m is None


def test_signal_day_counts_only_from_the_report_price_to_the_close():
    # The day's low of 125 may have come before the report; the close of 131 surely came after it.
    bars = [bar(SIGNAL, high=150, low=125, close=131, open_=149)]
    outcome = evaluate(make_opportunity(), bars, now=LATER)
    assert outcome.entry_filled == SIGNAL  # min(142.5, 131) = 131 <= 132
    assert outcome.low_breached is None  # 125 < 118 is false anyway, but 131 is what counts
    assert outcome.max_gain_pct == 0.0  # max(142.5, 131) is the report price
    assert outcome.max_loss_pct == pytest.approx((131 / 142.5 - 1) * 100)
    assert outcome.status == "open"


def test_whole_signal_day_counts_when_the_price_was_from_an_earlier_session():
    # Written at 06:00 UTC before the US open, with Thursday's close: all of Friday's session came after it.
    stats = make_stats(as_of=datetime(2026, 9, 24, 20, 0, tzinfo=UTC))
    opp = make_opportunity(stats=stats, created=datetime(2026, 9, 25, 6, 0, tzinfo=UTC))
    bars = [bar(SIGNAL, high=151, low=128, close=140, open_=150)]
    outcome = evaluate(opp, bars, now=LATER)
    assert outcome.entry_filled == SIGNAL
    assert outcome.max_gain_pct == pytest.approx((151 / 142.5 - 1) * 100)
    assert outcome.max_loss_pct == pytest.approx((128 / 142.5 - 1) * 100)


def test_target_on_the_fill_day_does_not_count():
    bars = [
        bar(date(2026, 9, 28), high=172, low=130, close=150),  # fill and target the same day: order unknown
        bar(date(2026, 9, 29), high=160, low=145, close=155),
    ]
    outcome = evaluate(make_opportunity(), bars, now=LATER)
    assert outcome.entry_filled == date(2026, 9, 28)
    assert outcome.target_hit is None
    assert outcome.status == "open"
    assert outcome.trade_return_pct == pytest.approx((155 / 132 - 1) * 100)

    bars.append(bar(date(2026, 9, 30), high=168.0, low=150, close=160))  # exactly the target counts
    assert evaluate(make_opportunity(), bars, now=LATER).target_hit == date(2026, 9, 30)


def test_no_target_without_a_fill():
    bars = [bar(date(2026, 9, 28), high=175, low=140, close=170)]  # never down to 132
    outcome = evaluate(make_opportunity(), bars, now=LATER)
    assert outcome.entry_filled is None
    assert outcome.target_hit is None
    assert outcome.status == "waiting_entry"
    assert outcome.trade_return_pct is None
    assert outcome.max_gain_pct == pytest.approx((175 / 142.5 - 1) * 100)


def test_below_low_then_target_hit_takes_precedence():
    bars = [bar(date(2026, 9, 28), high=136, low=117, close=120)]  # 117 < 118: breached (and filled)
    outcome = evaluate(make_opportunity(), bars, now=LATER)
    assert outcome.entry_filled == date(2026, 9, 28)
    assert outcome.low_breached == date(2026, 9, 28)
    assert outcome.status == "below_low"
    assert outcome.trade_return_pct == pytest.approx((120 / 132 - 1) * 100)

    bars.append(bar(date(2026, 9, 29), high=170, low=121, close=166))
    outcome = evaluate(make_opportunity(), bars, now=LATER)
    assert outcome.low_breached == date(2026, 9, 28)
    assert outcome.target_hit == date(2026, 9, 29)
    assert outcome.status == "target_hit"


def test_low_at_exactly_potential_low_is_not_a_breach():
    outcome = evaluate(make_opportunity(), [bar(date(2026, 9, 28), high=130, low=118.0, close=125)], now=LATER)
    assert outcome.low_breached is None
    assert outcome.entry_filled == date(2026, 9, 28)


def test_expired_after_six_months_uses_the_six_month_close():
    horizon = SIGNAL + timedelta(days=HORIZON_DAYS)
    assert horizon == date(2027, 3, 27)  # a Saturday
    bars = [
        bar(date(2026, 9, 28), high=145, low=135, close=140),
        bar(date(2027, 3, 26), high=151, low=149, close=150),  # last session on or before the 6-month date
        bar(date(2027, 4, 15), high=200, low=100, close=100),  # after the window: ignored
    ]
    outcome = evaluate(make_opportunity(), bars, now=datetime(2027, 4, 20, tzinfo=UTC))
    assert outcome.status == "expired"
    assert outcome.days == 207
    assert outcome.last_price == 150
    assert outcome.up_after_6m is True
    assert outcome.return_pct == pytest.approx((150 / 142.5 - 1) * 100)
    assert outcome.max_gain_pct == pytest.approx((151 / 142.5 - 1) * 100)
    assert outcome.max_loss_pct == pytest.approx((135 / 142.5 - 1) * 100)
    assert outcome.entry_filled is None


def test_expired_open_position_and_lower_after_six_months():
    bars = [bar(date(2026, 10, 1), high=135, low=130, close=131), bar(date(2027, 3, 25), high=130, low=125, close=126)]
    outcome = evaluate(make_opportunity(), bars, now=datetime(2027, 6, 1, tzinfo=UTC))
    assert outcome.entry_filled == date(2026, 10, 1)
    assert outcome.status == "expired"
    assert outcome.up_after_6m is False


def test_six_months_means_more_than_183_days():
    bars = [bar(date(2027, 3, 26), high=150, low=145, close=149)]
    on_the_day = evaluate(make_opportunity(), bars, now=datetime(2027, 3, 27, 23, 0, tzinfo=UTC))
    assert on_the_day.days == HORIZON_DAYS
    assert on_the_day.status == "waiting_entry"
    assert on_the_day.up_after_6m is None
    day_after = evaluate(make_opportunity(), bars, now=datetime(2027, 3, 28, tzinfo=UTC))
    assert day_after.status == "expired"
    assert day_after.up_after_6m is True


def test_up_after_6m_unknown_when_bars_stop_early():
    bars = [bar(date(2026, 12, 1), high=150, low=140, close=149)]
    outcome = evaluate(make_opportunity(), bars, now=datetime(2027, 4, 20, tzinfo=UTC))
    assert outcome.status == "expired"
    assert outcome.up_after_6m is None


def test_no_bars_yet():
    outcome = evaluate(make_opportunity(), [], now=NOW)
    assert outcome.last_price == 142.5
    assert outcome.return_pct == 0.0
    assert outcome.max_gain_pct == 0.0
    assert outcome.max_loss_pct == 0.0
    assert outcome.days == 0
    assert outcome.status == "waiting_entry"
    assert (outcome.entry_filled, outcome.target_hit, outcome.low_breached) == (None, None, None)
    assert not outcome.priced and outcome.last_day is None


def test_before_any_trading_after_the_report_the_figures_show_dashes():
    """Regression (recheck): right after a weekend report `track` said "Higher now than when reported 0 of 9 (0%)",
    "Average return +0.0%" and "Max loss +0.0%", which read like results."""
    created = datetime(2026, 9, 27, 20, 40, tzinfo=UTC)  # Sunday; the price is Friday's close
    stats = make_stats(as_of=datetime(2026, 9, 25, 20, 0, tzinfo=UTC), timezone="America/New_York")
    weekend = make_opportunity(stats=stats, created=created)
    friday_bar = bar(date(2026, 9, 25), high=150, low=140, close=142.5)
    waiting = evaluate(weekend, [friday_bar], now=created)
    assert not waiting.priced
    summary = summarize([waiting])
    assert summary["priced"] == 0 and summary["avg_return_pct"] is None and summary["positive_rate"] is None
    text = render_track_record([waiting], summary)
    assert "1 opportunity has had no trading since the report yet" in text
    assert "| Higher now than when reported | – |" in text
    assert "| Entry filled (limit buy reached) | – |" in text
    assert "| Average return since the report | – |" in text
    row = next(line for line in text.splitlines() if "**AMD**" in line)
    assert row.endswith("| waiting for entry | – | – | – | – | – | – | – | – |")

    # Monday's session is the first after the report: from then on it counts.
    monday = evaluate(weekend, [friday_bar, bar(date(2026, 9, 28), high=146, low=141, close=145)], now=LATER)
    assert monday.priced and monday.return_pct == pytest.approx((145 / 142.5 - 1) * 100)
    mixed = summarize([waiting, monday])
    assert (mixed["count"], mixed["priced"], mixed["positive"]) == (2, 1, 1)
    assert "| Higher now than when reported | 1 of 1 (100%) |" in render_track_record([waiting, monday], mixed)


def test_the_report_days_own_bar_counts_when_it_traded_after_the_report():
    # Written after the close: the day's bar closes at the report's price, nothing has traded since.
    after_close = make_opportunity(stats=make_stats(as_of=NOW - timedelta(minutes=15)))
    assert not evaluate(after_close, [bar(SIGNAL, high=150, low=140, close=142.5)], now=NOW).priced
    # Written during the session (session_elapsed set): the day's close came after it.
    running = make_opportunity(stats=make_stats(session_elapsed=0.4))
    assert evaluate(running, [bar(SIGNAL, high=150, low=140, close=142.5)], now=LATER).priced
    # Or the close simply differs from the report's price.
    assert evaluate(after_close, [bar(SIGNAL, high=150, low=140, close=141.0)], now=LATER).priced


def test_bars_are_sorted_and_the_last_duplicate_wins():
    bars = [
        bar(date(2026, 9, 30), high=150, low=145, close=148),
        bar(date(2026, 9, 28), high=150, low=140, close=141),
        bar(date(2026, 9, 28), high=150, low=131, close=133),  # corrected bar for the same day
    ]
    outcome = evaluate(make_opportunity(), bars, now=LATER)
    assert outcome.entry_filled == date(2026, 9, 28)
    assert outcome.last_price == 148


def test_naive_created_is_taken_as_utc():
    opp = make_opportunity(created=datetime(2026, 9, 25, 15, 0))
    outcome = evaluate(opp, [bar(date(2026, 9, 28), high=150, low=131, close=140)], now=datetime(2026, 10, 5))
    assert outcome.entry_filled == date(2026, 9, 28)
    assert outcome.days == 10


# --- summary -------------------------------------------------------------------------------------------------------


def outcome(
    *,
    score: float,
    verdict: str,
    probability: int,
    filled: bool = False,
    hit: bool = False,
    breached: bool = False,
    return_pct: float = 0.0,
    trade: float | None = None,
    up: bool | None = None,
    status: str = "waiting_entry",
    ticker: str = "AMD",
    created: datetime = NOW,
) -> Outcome:
    opp = make_opportunity(
        ticker=ticker,
        score=score,
        created=created,
        analysis=make_analysis(verdict=verdict, probability_up_6m=probability),
    )
    return Outcome(
        opportunity=opp,
        last_price=opp.price * (1 + return_pct / 100),
        return_pct=return_pct,
        max_gain_pct=max(0.0, return_pct),
        max_loss_pct=min(0.0, return_pct),
        entry_filled=date(2026, 9, 28) if filled else None,
        target_hit=date(2026, 10, 2) if hit else None,
        low_breached=date(2026, 9, 29) if breached else None,
        days=200 if up is not None else 10,
        status=status,
        up_after_6m=up,
        trade_return_pct=trade,
    )


def sample_outcomes() -> list[Outcome]:
    return [
        outcome(
            score=85,
            verdict="temporary_fear",
            probability=70,
            filled=True,
            hit=True,
            return_pct=20.0,
            trade=27.0,
            up=True,
            status="target_hit",
            ticker="AAA",
        ),
        outcome(
            score=70,
            verdict="temporary_fear",
            probability=60,
            filled=True,
            breached=True,
            return_pct=-15.0,
            trade=-10.0,
            up=False,
            status="below_low",
            ticker="BBB",
        ),
        outcome(
            score=55, verdict="mixed", probability=50, return_pct=5.0, ticker="CCC", created=NOW - timedelta(days=3)
        ),
        outcome(
            score=40,
            verdict="fundamental",
            probability=40,
            return_pct=-2.0,
            status="expired",
            ticker="DDD",
            created=NOW - timedelta(days=30),
        ),
    ]


def test_score_buckets_include_their_lower_bound():
    assert score_bucket(0) == "<50"
    assert score_bucket(49.9) == "<50"
    assert score_bucket(50) == "50-65"
    assert score_bucket(64.99) == "50-65"
    assert score_bucket(65) == "65-80"
    assert score_bucket(79.9) == "65-80"
    assert score_bucket(80) == "80+"
    assert score_bucket(100) == "80+"


def test_summarize_overall_figures():
    summary = summarize(sample_outcomes())
    assert summary["count"] == 4
    assert summary["filled"] == 2
    assert summary["fill_rate"] == pytest.approx(50.0)
    assert summary["target_hit"] == 1
    assert summary["target_hit_rate"] == pytest.approx(50.0)  # of the filled ones
    assert summary["below_low"] == 1
    assert summary["below_low_rate"] == pytest.approx(25.0)
    assert summary["matured"] == 2
    assert summary["up_after_6m"] == 1
    assert summary["up_rate_6m"] == pytest.approx(50.0)
    assert summary["predicted_up_6m"] == pytest.approx(65.0)  # (70 + 60) / 2, the matured ones only
    assert summary["positive"] == 2
    assert summary["positive_rate"] == pytest.approx(50.0)
    assert summary["avg_return_pct"] == pytest.approx(2.0)  # (20 - 15 + 5 - 2) / 4
    assert summary["median_return_pct"] == pytest.approx(1.5)  # between -2 and 5
    assert summary["avg_trade_return_pct"] == pytest.approx(8.5)  # (27 - 10) / 2
    assert summary["avg_score"] == pytest.approx(62.5)
    assert summary["statuses"] == {"waiting_entry": 1, "open": 0, "target_hit": 1, "below_low": 1, "expired": 1}
    assert list(summary["statuses"]) == list(STATUSES)


def test_summarize_groups_by_verdict_and_score():
    summary = summarize(sample_outcomes())
    assert list(summary["by_verdict"]) == ["temporary_fear", "mixed", "fundamental"]
    fear = summary["by_verdict"]["temporary_fear"]
    assert (fear["count"], fear["filled"], fear["target_hit"], fear["below_low"]) == (2, 2, 1, 1)
    assert fear["avg_return_pct"] == pytest.approx(2.5)
    assert summary["by_verdict"]["mixed"]["fill_rate"] == 0.0
    assert summary["by_verdict"]["mixed"]["target_hit_rate"] is None  # nothing filled

    assert list(summary["by_score"]) == ["<50", "50-65", "65-80", "80+"]
    assert {label: group["count"] for label, group in summary["by_score"].items()} == {
        "<50": 1,
        "50-65": 1,
        "65-80": 1,
        "80+": 1,
    }
    assert summary["by_score"]["80+"]["target_hit"] == 1


def test_summarize_nothing():
    summary = summarize([])
    assert summary["count"] == 0
    assert summary["fill_rate"] is None
    assert summary["avg_return_pct"] is None
    assert summary["median_return_pct"] is None
    assert summary["by_verdict"] == {}
    assert all(group["count"] == 0 for group in summary["by_score"].values())


# --- Markdown ------------------------------------------------------------------------------------------------------


def test_render_track_record():
    outcomes = sample_outcomes()
    text = render_track_record(outcomes, summarize(outcomes))
    assert text.startswith("# Track record\n")
    assert "_4 opportunities reported 2026-08-26 to 2026-09-25 · 2 with 6 months of results_" in text
    assert "| Entry filled (limit buy reached) | 2 of 4 (50%) |" in text
    assert "| Target hit after the fill | 1 of 2 (50%) |" in text
    assert "| Higher after 6 months | 1 of 2 (50%); the model said 65% on average |" in text
    assert "| Average return since the report | +2.0% (median +1.5%) |" in text
    assert "| Average return of filled limit orders | +8.5% |" in text
    assert "Status: 1 waiting for entry · 1 target hit · 1 below the low · 1 expired" in text
    assert "## By verdict" in text
    assert (
        "| Temporary fear | 2 | 2 of 2 (100%) | 1 of 2 (50%) | 1 of 2 (50%) | 1 of 2 (50%) | +2.5% | – | +8.5% |"
    ) in text
    assert "| Mixed | 1 | 0 of 1 (0%) | – | 0 of 1 (0%) | – | +5.0% | – | – |" in text
    assert "| Average return of the benchmark index over the same days | – |" in text  # none given here
    assert "## By score" in text
    assert "| 80+ | 1 |" in text
    # Newest first; AAA and BBB share a timestamp, so they keep their order.
    rows = [line for line in text.splitlines() if line.startswith("| 2026-")]
    assert [row.split(" | ")[1] for row in rows] == ["**AAA**", "**BBB**", "**CCC**", "**DDD**"]
    assert (
        "| 2026-09-25 | **AAA** | 85.0 | Temporary fear | $142.50 | $132.00 | $168.00 | $118.00 | target hit |" in text
    )
    assert "not investment advice" in text


def test_render_track_record_end_to_end_from_bars():
    bars = [
        bar(date(2026, 9, 28), high=141, low=131, close=135),
        bar(date(2026, 9, 29), high=170, low=134, close=165),
    ]
    outcomes = [evaluate(make_opportunity(), bars, now=LATER)]
    text = render_track_record(outcomes, summarize(outcomes))
    row = next(line for line in text.splitlines() if "**AMD**" in line)
    assert "| target hit | 2026-09-28 | 2026-09-29 | +15.8% | – | – | +19.3% | -8.1% | – |" in row  # no index given
    assert "| Average return of filled limit orders | +27.3% |" in text


def test_reported_range_uses_the_same_dates_as_the_rows():
    """Regression (live smoke): a Hong Kong report written at 20:34 UTC on the 27th (04:34 on the 28th in Hong Kong)
    made the header say "reported 2026-09-27 to 2026-09-28" while every row said 2026-09-27."""
    created = datetime(2026, 9, 27, 20, 34, tzinfo=UTC)
    stats = make_stats(ticker="1211.HK", price=78.1, as_of=created - timedelta(days=2), timezone="Asia/Hong_Kong")
    analysis = make_analysis(potential_low=59.0, entry_price=71.4, target_price=92.0)
    outcomes = [
        evaluate(make_opportunity(ticker="1211.HK", stats=stats, created=created, analysis=analysis), [], now=created),
        evaluate(make_opportunity(created=created), [], now=created),
    ]
    text = render_track_record(outcomes, summarize(outcomes))
    assert "_2 opportunities reported on 2026-09-27 · 0 with 6 months of results_" in text
    assert "| 2026-09-27 | **1211.HK** |" in text


def test_render_track_record_escapes_and_handles_empty():
    assert "No stored opportunities to track yet." in render_track_record([], summarize([]))
    odd = [outcome(score=70, verdict="mixed", probability=55, ticker="A|B")]
    assert "**A\\|B**" in render_track_record(odd, summarize(odd))


# --- benchmark index and account currency --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ticker", "index"),
    [
        ("AMD", "^GSPC"),
        ("BRK-B", "^GSPC"),
        ("SAP.DE", "^GDAXI"),
        ("MC.PA", "^FCHI"),
        ("ASML.AS", "^AEX"),
        ("ENI.MI", "FTSEMIB.MI"),
        ("ITX.MC", "^IBEX"),
        ("ALWN.AT", "GD.AT"),
        ("VOD.L", "^FTSE"),
        ("7203.T", "^N225"),
        ("PKO.WA", "^STOXX50E"),  # another European exchange
        ("XYZ.QQ", "^GSPC"),  # anything else
    ],
)
def test_each_exchange_has_its_benchmark_index(ticker, index):
    assert benchmark_for(ticker) == index


# AMD bought at 142.50 on Fri 25 Sep; on Tue 29 Sep it closes at 150 (+5.3%) while the S&P 500 went 7,000 -> 7,210.
GAIN = [bar(date(2026, 9, 28), high=146, low=141, close=145), bar(date(2026, 9, 29), high=151, low=144, close=150)]
SP500 = [
    bar(date(2026, 9, 24), high=6990, low=6990, close=6990),
    bar(SIGNAL, high=7000, low=7000, close=7000),
    bar(date(2026, 9, 28), high=7100, low=7100, close=7100),
    bar(date(2026, 9, 29), high=7210, low=7210, close=7210),
    bar(date(2026, 9, 30), high=7300, low=7300, close=7300),  # after the last price's day: not used
]


def test_the_benchmark_covers_the_same_days():
    outcome = with_benchmark(evaluate(make_opportunity(), GAIN, now=LATER), "^GSPC", SP500)
    assert outcome.last_day == date(2026, 9, 29)
    assert outcome.benchmark == "^GSPC"
    assert outcome.benchmark_return_pct == pytest.approx(3.0)  # 7,000 on the quote day -> 7,210
    assert outcome.excess_return_pct == pytest.approx((150 / 142.5 - 1) * 100 - 3.0)

    missing = with_benchmark(evaluate(make_opportunity(), GAIN, now=LATER), "GD.AT", [])
    assert missing.benchmark_return_pct is None and missing.excess_return_pct is None
    waiting = with_benchmark(evaluate(make_opportunity(), [], now=NOW), "^GSPC", SP500)
    assert waiting.benchmark_return_pct is None


MONDAY, TUESDAY = date(2026, 9, 21), date(2026, 9, 22)
# The market and AMD both fall 2% from 11:00 New York time into Monday's close, then stay flat on Tuesday.
MARKET_FALLS = [bar(MONDAY, high=5000, low=4900, close=4900), bar(TUESDAY, high=4900, low=4900, close=4900)]
AMD_FALLS = [bar(MONDAY, high=100, low=98, close=98), bar(TUESDAY, high=98, low=98, close=98)]


def _reported_at_eleven(**overrides) -> Opportunity:
    when = datetime(2026, 9, 21, 15, 0, tzinfo=UTC)  # 11:00 in New York, the session running
    stats = make_stats(price=100.0, as_of=when, timezone="America/New_York", session_elapsed=0.23)
    analysis = make_analysis(potential_low=80.0, entry_price=90.0, target_price=120.0)
    return make_opportunity(stats=stats, created=when, analysis=analysis, **overrides)


def test_the_index_starts_where_the_stock_does_for_a_report_during_the_session():
    """Regression: a report at 11:00 started the stock at its 11:00 price and the index at that day's close, so a
    stock that moved exactly with the market showed -2.00% vs index."""
    stored = _reported_at_eleven(benchmark="^GSPC", benchmark_level=5000.0)
    outcome = with_benchmark(evaluate(stored, AMD_FALLS, now=LATER), "^GSPC", MARKET_FALLS)
    assert outcome.return_pct == pytest.approx(-2.0)
    assert outcome.benchmark_return_pct == pytest.approx(-2.0)  # from the stored 5,000
    assert outcome.excess_return_pct == pytest.approx(0.0)

    # An older record without a stored level: the index starts at Monday's close, and for the comparison so does
    # the stock (its return itself is still from the report's price).
    legacy = with_benchmark(evaluate(_reported_at_eleven(), AMD_FALLS, now=LATER), "^GSPC", MARKET_FALLS)
    assert legacy.return_pct == pytest.approx(-2.0) and legacy.benchmark_return_pct == pytest.approx(0.0)
    assert legacy.excess_return_pct == pytest.approx(0.0)

    # A level stored for another index isn't used.
    other = _reported_at_eleven(benchmark="^NDX", benchmark_level=20000.0)
    assert with_benchmark(evaluate(other, AMD_FALLS, now=LATER), "^GSPC", MARKET_FALLS).excess_return_pct == (
        pytest.approx(0.0)
    )
    # After the close the report's price is the close: nothing to align.
    closed = evaluate(make_opportunity(), GAIN, now=LATER)
    assert closed.session_close is None and with_benchmark(closed, "^GSPC", SP500).index_aligned_return_pct is None


def test_the_account_currency_return_adds_the_exchange_rate_move():
    """A euro investor: +5.3% in dollars, but the dollar fell from 0.88 to 0.85 euros."""
    rates = [(date(2026, 9, 24), 0.87), (SIGNAL, 0.88), (date(2026, 9, 29), 0.85)]
    local = evaluate(make_opportunity(), GAIN, now=LATER)
    outcome = with_account_return(local, "EUR", rates)
    assert outcome.account_return_pct == pytest.approx(((150 / 142.5) * 0.85 / 0.88 - 1) * 100)  # +1.7%
    assert outcome.account_currency == "EUR"

    # The rate stored with the report wins over the day's close.
    stored = evaluate(make_opportunity(account_currency="EUR", fx_rate=0.86), GAIN, now=LATER)
    assert with_account_return(stored, "EUR", rates).account_return_pct == pytest.approx(
        ((150 / 142.5) * 0.85 / 0.86 - 1) * 100
    )
    # A euro stock needs no rates; missing rates give None, and so does no trading yet.
    euro = evaluate(make_opportunity(ticker="SAP.DE", currency="EUR"), GAIN, now=LATER)
    assert with_account_return(euro, "EUR", None).account_return_pct == pytest.approx(euro.return_pct)
    assert with_account_return(local, "EUR", None).account_return_pct is None
    assert with_account_return(local, "EUR", [(date(2026, 9, 29), 0.85)]).account_return_pct is None  # none before
    waiting = evaluate(make_opportunity(), [], now=NOW)
    assert with_account_return(waiting, "EUR", rates).account_return_pct is None


def test_the_track_record_shows_index_excess_and_account_returns():
    rates = [(SIGNAL, 0.88), (date(2026, 9, 29), 0.85)]
    amd = with_account_return(
        with_benchmark(evaluate(make_opportunity(), GAIN, now=LATER), "^GSPC", SP500), "EUR", rates
    )
    summary = summarize([amd])
    assert summary["account_currency"] == "EUR"
    assert summary["avg_benchmark_return_pct"] == pytest.approx(3.0)
    assert summary["avg_excess_return_pct"] == pytest.approx(amd.excess_return_pct)
    text = render_track_record([amd], summary)
    assert "| Average return in EUR (exchange-rate moves included) | +1.7% |" in text
    assert "| Average return of the benchmark index over the same days | +3.0% |" in text
    assert "| Average excess return (return minus the index's) | +2.3% |" in text
    assert "| Return | In EUR | Index | vs index |" in text
    row = next(line for line in text.splitlines() if "**AMD**" in line)
    assert "| +5.3% | +1.7% | +3.0% (^GSPC) | +2.3% |" in row
    assert "| Temporary fear | 1 | 0 of 1 (0%) | – | 0 of 1 (0%) | – | +5.3% | +2.3% | – |" in text
    assert "The EUR return adds the exchange-rate move" in text

    plain = render_track_record([evaluate(make_opportunity(), GAIN, now=LATER)], summarize([amd]))
    assert "In EUR" in plain  # the summary says which account
    assert "In EUR" not in render_track_record([amd], summarize([evaluate(make_opportunity(), GAIN, now=LATER)]))


def test_the_reported_dates_follow_the_display_time_zone():
    from zoneinfo import ZoneInfo

    from dip_scanner.report import set_display_zone

    created = datetime(2026, 9, 25, 22, 30, tzinfo=UTC)  # already the 26th in Athens
    outcomes = [evaluate(make_opportunity(created=created), GAIN, now=LATER)]
    assert "| 2026-09-25 | **AMD** |" in render_track_record(outcomes, summarize(outcomes))
    set_display_zone(ZoneInfo("Europe/Athens"))
    text = render_track_record(outcomes, summarize(outcomes))
    assert "| 2026-09-26 | **AMD** |" in text and "reported on 2026-09-26" in text


def test_a_users_currency_return_starts_from_the_rate_stored_for_that_currency():
    """The website stores rates into every user's currency with an analysis; a GBP user's return starts from the
    GBP rate of the analysis, not from the EUR account's."""
    rates = [(SIGNAL, 0.75), (date(2026, 9, 29), 0.74)]
    opp = make_opportunity(account_currency="EUR", fx_rate=0.86, fx_rates={"EUR": 0.86, "GBP": 0.73})
    outcome = with_account_return(evaluate(opp, GAIN, now=LATER), "GBP", rates)
    assert outcome.account_return_pct == pytest.approx(((150 / 142.5) * 0.74 / 0.73 - 1) * 100)
    missing = with_account_return(evaluate(make_opportunity(fx_rates={"EUR": 0.86}), GAIN, now=LATER), "GBP", rates)
    assert missing.account_return_pct == pytest.approx(((150 / 142.5) * 0.74 / 0.75 - 1) * 100)  # the day's close


# --- the debate's model scoreboard ---------------------------------------------------------------------------------


def debated(gpt: tuple[int, int], claude: tuple[int, int], outcome: int, *, up: bool | None, **fields) -> Outcome:
    """An outcome of a debated opportunity: (opening, final) chances per model and the debate's own."""
    base = make_debate()
    gpt_side, claude_side = (
        replace(side, opening=make_analysis(probability_up_6m=opening), final=make_analysis(probability_up_6m=final))
        for side, (opening, final) in zip(base.participants, (gpt, claude), strict=True)
    )
    opp = make_opportunity(
        debate=replace(base, participants=[gpt_side, claude_side]),
        analysis=make_analysis(probability_up_6m=outcome),
    )
    values = {
        "opportunity": opp,
        "last_price": 150.0,
        "return_pct": 5.3,
        "max_gain_pct": 8.0,
        "max_loss_pct": -3.0,
        "entry_filled": None,
        "target_hit": None,
        "low_breached": None,
        "days": 200 if up is not None else 40,
        "status": "expired" if up is not None else "waiting_entry",
        "up_after_6m": up,
        **fields,
    }
    return Outcome(**values)


def test_the_scoreboard_brier_scores_are_hand_computed():
    outcomes = [
        debated((80, 70), (50, 60), 64, up=True),
        debated((40, 40), (20, 30), 35, up=False),
        debated((90, 90), (90, 90), 90, up=None),  # 6 months haven't passed: waiting
        debated((10, 10), (10, 10), 10, up=True, price_mismatch=True),  # left out entirely
        Outcome(**{**vars(debated((50, 50), (50, 50), 50, up=True)), "opportunity": make_opportunity()}),  # no debate
    ]
    board = {row["model"]: row for row in model_scoreboard(outcomes)}
    assert list(board) == ["anthropic:claude-sonnet-5", "openai:gpt-5", FINAL_ROW]

    gpt = board["openai:gpt-5"]
    # Finals 70% (higher) and 40% (not): ((0.7 - 1)² + (0.4 - 0)²) / 2 = (0.09 + 0.16) / 2
    assert gpt["brier"] == pytest.approx(0.125)
    # Openings 80% and 40%: ((0.8 - 1)² + 0.4²) / 2 = (0.04 + 0.16) / 2
    assert gpt["brier_opening"] == pytest.approx(0.10)
    assert (gpt["label"], gpt["debates"], gpt["scored"], gpt["waiting"]) == ("GPT-5", 3, 2, 1)
    assert (gpt["mean_probability"], gpt["hit_rate"]) == (55.0, 50.0)

    claude = board["anthropic:claude-sonnet-5"]
    assert claude["brier"] == pytest.approx((0.4**2 + 0.3**2) / 2)  # 0.125
    assert claude["brier_opening"] == pytest.approx((0.5**2 + 0.2**2) / 2)  # 0.145
    assert claude["label"] == "Claude Sonnet 5"

    final = board[FINAL_ROW]
    assert final["brier"] == pytest.approx(((0.64 - 1) ** 2 + 0.35**2) / 2)  # 0.12605
    assert final["brier_opening"] is None and final["label"] == "After the debate"
    assert (final["debates"], final["scored"], final["waiting"]) == (3, 2, 1)


def test_the_scoreboard_waits_for_six_months_and_is_empty_without_debates():
    waiting = model_scoreboard([debated((70, 70), (60, 60), 65, up=None)])
    assert all(row["brier"] is None and row["hit_rate"] is None and row["scored"] == 0 for row in waiting)
    assert model_scoreboard([]) == []
    assert model_scoreboard(sample_outcomes()) == []  # nothing debated
    assert brier([]) is None
    assert brier([(100, True), (0, False)]) == 0.0 and brier([(50, True), (50, False)]) == 0.25


def test_the_track_record_shows_the_scoreboard_once_there_are_debates():
    outcomes = [debated((80, 70), (50, 60), 64, up=True), debated((40, 40), (20, 30), 35, up=False)]
    summary = summarize(outcomes)
    assert [row["model"] for row in summary["scoreboard"]] == ["anthropic:claude-sonnet-5", "openai:gpt-5", "final"]
    text = render_track_record(outcomes, summary)
    assert "## Model scoreboard" in text and "0.25 is what always saying 50% scores" in text
    assert "| GPT-5 | 2 | 2 | 0.125 | 0.100 | 55% | 50% |" in text
    assert "| Claude Sonnet 5 | 2 | 2 | 0.125 | 0.145 | 45% | 50% |" in text
    assert "| After the debate | 2 | 2 | 0.126 | – | 50% | 50% |" in text
    waiting = render_track_record([debated((70, 70), (60, 60), 65, up=None)], summarize([]))
    assert "## Model scoreboard" not in waiting  # the summary given has no scoreboard
    early = [debated((70, 70), (60, 60), 65, up=None)]
    assert "| GPT-5 | 1 | 0 | – | – | – | – |" in render_track_record(early, summarize(early))
    plain = sample_outcomes()
    assert "Model scoreboard" not in render_track_record(plain, summarize(plain))
