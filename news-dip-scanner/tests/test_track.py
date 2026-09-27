from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from conftest import NOW, make_analysis, make_opportunity, make_stats

from dip_scanner.models import PriceBar, Split
from dip_scanner.track import (
    HORIZON_DAYS,
    STATUSES,
    Outcome,
    evaluate,
    quote_day,
    render_track_record,
    score_bucket,
    signal_day,
    summarize,
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
    assert "| Temporary fear | 2 | 2 of 2 (100%) | 1 of 2 (50%) | 1 of 2 (50%) | 1 of 2 (50%) | +2.5% | +8.5% |" in text
    assert "| Mixed | 1 | 0 of 1 (0%) | – | 0 of 1 (0%) | – | +5.0% | – |" in text
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
    assert "| target hit | 2026-09-28 | 2026-09-29 | +15.8% | +19.3% | -8.1% | – |" in row
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
