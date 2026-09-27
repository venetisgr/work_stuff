import json
import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest
from conftest import NOW, make_analysis, make_article, make_bars, make_opportunity, make_stats

from dip_scanner.models import (
    CONFIDENCES,
    DIRECTIONS,
    EVENT_TYPES,
    RELATIONS,
    VERDICTS,
    Fundamentals,
    Opportunity,
    analysis_from_dict,
    analysis_to_dict,
    article_from_dict,
    article_to_dict,
    from_iso,
    stats_from_dict,
    stats_to_dict,
    to_iso,
    utc,
)


def test_the_enums_match_the_contract():
    assert RELATIONS == ("direct", "indirect")
    assert DIRECTIONS == ("negative", "positive", "mixed", "neutral")
    assert "m&a" in EVENT_TYPES and "supply_chain" in EVENT_TYPES and EVENT_TYPES[-1] == "other"
    assert VERDICTS == ("temporary_fear", "mixed", "fundamental", "unclear")
    assert CONFIDENCES == ("low", "medium", "high")


# --- time helpers ---


def test_utc_assumes_naive_datetimes_are_utc():
    result = utc(datetime(2026, 9, 25, 15, 0))
    assert result == NOW
    assert result.tzinfo is UTC


def test_utc_converts_other_timezones():
    athens = timezone(timedelta(hours=3))
    result = utc(datetime(2026, 9, 25, 18, 0, tzinfo=athens))
    assert result == NOW
    assert result.utcoffset() == timedelta(0)
    assert (result.hour, result.tzinfo) == (15, UTC)


def test_iso_round_trip_keeps_microseconds_and_accepts_z():
    moment = datetime(2026, 9, 25, 15, 0, 1, 123456, tzinfo=UTC)
    assert to_iso(moment) == "2026-09-25T15:00:01.123456+00:00"
    assert from_iso(to_iso(moment)) == moment
    assert from_iso("2026-09-25T15:00:00Z") == NOW
    assert from_iso("2026-09-25T15:00:00").tzinfo is UTC  # naive text means UTC
    assert from_iso("2026-09-25T17:00:00+02:00") == NOW


# --- PriceStats ---


def test_price_stats_text_has_the_key_numbers():
    text = make_stats().as_text()
    first = "AMD (Advanced Micro Devices, Inc.) on NasdaqGS, prices in USD, as of 2026-09-25 14:45 UTC"
    assert text.splitlines()[0] == first
    assert "Price 142.50 USD (previous close 150.00): 1 day -5.0%, 5 days -8.0%, 20 days -12.0%" in text
    assert "20-day high 165.00 (-13.6% from it)" in text
    assert "52-week range 95.00 - 190.00 (-25.0% from the high, +50.0% above the low)" in text
    assert "50-day average 155.20, 200-day average 140.10" in text
    assert "Volatility 48.0% a year" in text and "2.3x the 20-day average" in text
    low = 142.5 * math.exp(-1.645 * 0.48 * math.sqrt(0.5))
    # It is a percentile of the 6-month end price, not of the lowest price along the way; the text says so.
    assert (
        f"6-month low (5th percentile of the price in 6 months): {low:,.2f} ({(low / 142.5 - 1) * 100:+.1f}% from "
        "the price; the lowest price along the way falls below it about twice as often)"
    ) in text
    assert "Worst 6-month drawdown in the price history: -38.5%" in text


def test_price_stats_text_copes_with_missing_values():
    stats = make_stats(name=None, exchange=None, sma_50=None, sma_200=None, volume_ratio=None, ticker="OPAP.AT")
    text = stats.as_text()
    assert text.startswith("OPAP.AT, prices in USD")
    assert "50-day average n/a, 200-day average n/a" in text
    assert "volume" not in text


def test_price_stats_text_shows_penny_prices_with_significant_digits():
    text = make_stats(price=0.01234, currency="EUR").as_text()
    assert "Price 0.01234 EUR" in text


def test_make_stats_is_internally_consistent():
    stats = make_stats(price=50.0)
    assert stats.price < stats.previous_close
    assert stats.low_52w <= stats.price <= stats.high_20d <= stats.high_52w
    assert stats.drawdown_20d_pct == pytest.approx((50 / stats.high_20d - 1) * 100)
    assert stats.change_1d_pct == pytest.approx(-5.0)
    assert stats.stat_low_6m < stats.price
    dipless = make_stats(change_1d_pct=-1.0, drawdown_20d_pct=-2.0)
    assert dipless.previous_close == pytest.approx(142.5 / 0.99)
    assert dipless.high_20d == pytest.approx(142.5 / 0.98)


def test_make_bars_skips_weekends_and_keeps_high_low_around_the_close():
    bars = make_bars([10, 11, 9, 9.5])
    assert [bar.day.isoformat() for bar in bars] == ["2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07"]
    assert all(bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high for bar in bars)
    assert bars[2].open == 11 and bars[2].close == 9


# --- Fundamentals ---


def _quarter(end: str, revenue: float | None, net_income: float | None = 1e8, eps: float | None = 0.5) -> dict:
    return {
        "period_end": end,
        "revenue": revenue,
        "gross_profit": None if revenue is None else revenue / 2,
        "operating_income": None,
        "net_income": net_income,
        "eps_diluted": eps,
        "operating_cash_flow": 2.5e8,
    }


def _fundamentals(quarters: list[dict], annual: list[dict] | None = None) -> Fundamentals:
    return Fundamentals(
        ticker="AMD",
        entity="Advanced Micro Devices, Inc.",
        cik="0000002488",
        currency="USD",
        quarters=quarters,
        annual=annual or [],
    )


def test_fundamentals_text_uses_year_over_year_growth_when_the_year_ago_quarter_is_there():
    quarters = [
        _quarter("2026-06-27", 7.685e9, eps=0.53),
        _quarter("2026-03-28", 7.4e9),
        _quarter("2025-12-27", 7.0e9),
        _quarter("2025-09-27", 6.5e9),
        _quarter("2025-06-28", 5.8e9, eps=0.4),
    ]
    lines = _fundamentals(quarters).as_text().splitlines()
    assert lines[0].startswith("AMD: Advanced Micro Devices, Inc. (SEC CIK 0000002488). Amounts in USD millions")
    header = lines.index("Quarters (newest first):") + 1
    assert lines[header].startswith("Quarter ending | Revenue | Gross profit | Operating income | Net income")
    newest = lines[header + 1]
    assert newest.startswith("2026-06-27 | 7,685 (+32.5% y/y) | 3,842 (+32.5% y/y) | n/a | 100 (+0.0% y/y)")
    assert "0.53 (+32.5% y/y)" in newest
    # The older quarters have no year-ago quarter in the list, so they compare with the previous quarter.
    assert lines[header + 2].startswith("2026-03-28 | 7,400 (+5.7% q/q)")
    # The oldest has nothing to compare with.
    assert lines[header + 5].startswith("2025-06-28 | 5,800 | 2,900 | n/a | 100 | 0.40 | 250")


def test_fundamentals_text_leaves_growth_out_when_it_would_be_meaningless():
    quarters = [_quarter("2026-06-27", 5e9, net_income=2e8), _quarter("2026-03-28", None, net_income=-1e8)]
    row = _fundamentals(quarters).as_text().splitlines()[4]
    assert row.startswith("2026-06-27 | 5,000 | 2,500 | n/a | 200 | 0.50 (+0.0% q/q) | 250 (+0.0% q/q)")


def test_fundamentals_text_ignores_gaps_between_quarters():
    quarters = [_quarter("2026-06-27", 5e9), _quarter("2025-12-27", 4e9)]  # 182 days apart: neither y/y nor q/q
    assert "%" not in _fundamentals(quarters).as_text().split("Quarters (newest first):")[1]


def test_fundamentals_text_includes_fiscal_years_with_growth():
    annual = [_quarter("2025-12-27", 3.0e10), _quarter("2024-12-28", 2.5e10), _quarter("2023-12-30", 2.2e10)]
    text = _fundamentals([], annual).as_text()
    assert "Quarters" not in text
    lines = text.splitlines()
    assert lines[2] == "Fiscal years (newest first):"
    assert lines[3].startswith("Year ending | Revenue")
    assert lines[4].startswith("2025-12-27 | 30,000 (+20.0% y/y)")
    assert lines[6].startswith("2023-12-30 | 22,000 | ")


def test_fundamentals_text_without_figures_says_so():
    text = _fundamentals([], []).as_text()
    assert "No income statement figures" in text


# --- Opportunity ---


def test_upside_and_downside_are_relative_to_the_price():
    opp = make_opportunity()
    assert opp.upside_pct() == pytest.approx((168 / 142.5 - 1) * 100)
    assert opp.downside_pct() == pytest.approx((118 / 142.5 - 1) * 100)
    assert opp.downside_pct() < 0 < opp.upside_pct()


def test_entry_upside_and_downside_are_what_the_limit_orders_would_make_or_lose():
    opp = make_opportunity()  # entry 132, target 168, low 118
    assert opp.entry_upside_pct() == pytest.approx((168 / 132 - 1) * 100)  # +27.3%
    assert opp.entry_downside_pct() == pytest.approx((118 / 132 - 1) * 100)  # -10.6%


def test_the_exchange_rate_round_trips_and_older_records_have_none():
    opp = make_opportunity(account_currency="EUR", fx_rate=0.8783)
    assert Opportunity.from_dict(json.loads(json.dumps(opp.to_dict()))) == opp
    data = make_opportunity().to_dict()
    del data["account_currency"], data["fx_rate"]  # stored before [account] existed
    old = Opportunity.from_dict(data)
    assert (old.account_currency, old.fx_rate) == (None, None)
    for wrong in (0, -1.0, "0.9", float("nan"), float("inf"), True):
        assert Opportunity.from_dict({**opp.to_dict(), "fx_rate": wrong}).fx_rate is None


def test_price_stats_text_in_another_time_zone():
    from zoneinfo import ZoneInfo

    text = make_stats().as_text(ZoneInfo("Europe/Athens"))
    assert "as of 2026-09-25 17:45 EEST" in text.splitlines()[0]
    assert "as of 2026-09-25 14:45 UTC" in make_stats().as_text()  # what the model sees


def test_opportunity_round_trips_through_json():
    opp = make_opportunity(
        id=7,
        created=datetime(2026, 9, 25, 15, 0, 0, 250000, tzinfo=UTC),
        analysis=make_analysis(warnings=["entry was above the price; moved to the price"]),
        stats=make_stats(sma_200=None, volume_ratio=None),
    )
    data = opp.to_dict()
    text = json.dumps(data)  # JSON-safe
    restored = Opportunity.from_dict(json.loads(text))
    assert restored == opp
    assert restored.created.tzinfo is UTC
    assert restored.stats.as_of == opp.stats.as_of
    assert data["created"] == "2026-09-25T15:00:00.250000+00:00"
    assert data["stats"]["as_of"] == "2026-09-25T14:45:00+00:00"
    assert data["analysis"]["warnings"] == ["entry was above the price; moved to the price"]
    assert data["id"] == 7


def test_opportunity_round_trip_without_id():
    opp = make_opportunity()
    assert Opportunity.from_dict(opp.to_dict()) == opp
    assert opp.to_dict()["id"] is None


def test_to_dict_copies_lists_so_the_opportunity_stays_unchanged():
    opp = make_opportunity()
    data = opp.to_dict()
    data["article_ids"].append("x")
    data["headlines"][0]["title"] = "changed"
    data["analysis"]["risks"].append("new risk")
    assert opp.article_ids == [make_article().id]
    assert opp.headlines[0]["title"] == make_article().title
    assert opp.analysis.risks == ["Hyperscalers cut capex further"]


def test_to_dict_turns_datetimes_in_headlines_into_text():
    opp = make_opportunity(headlines=[{"title": "t", "published": NOW, "day": NOW.date()}])
    data = opp.to_dict()
    assert data["headlines"] == [{"title": "t", "published": "2026-09-25T15:00:00+00:00", "day": "2026-09-25"}]
    json.dumps(data)


def test_from_dict_ignores_unknown_keys_and_defaults_missing_lists():
    data = make_opportunity().to_dict()
    data["future_field"] = 1
    data["stats"]["future_stat"] = 2
    data["analysis"]["future"] = 3
    for name in ("risks", "catalysts", "checks", "warnings"):
        del data["analysis"][name]
    restored = Opportunity.from_dict(data)
    assert restored.analysis.risks == [] and restored.analysis.warnings == []
    assert restored.stats == make_stats()


def test_opportunities_compare_by_value():
    assert make_opportunity() == make_opportunity()
    assert replace(make_opportunity(), score=10.0) != make_opportunity()


# --- helpers ---


def test_stats_and_analysis_helpers_round_trip():
    stats = make_stats(ticker="SAP.DE", currency="EUR", exchange="XETRA", as_of=datetime(2026, 9, 25, 15, 30))
    assert stats_from_dict(json.loads(json.dumps(stats_to_dict(stats)))) == replace(stats, as_of=utc(stats.as_of))
    analysis = make_analysis(verdict="fundamental", risks=["a", "b"])
    assert analysis_from_dict(json.loads(json.dumps(analysis_to_dict(analysis)))) == analysis


def test_article_helpers_round_trip():
    article = make_article()
    data = article_to_dict(article)
    assert data["published"] == "2026-09-25T14:00:00+00:00"
    assert article_from_dict(json.loads(json.dumps(data))) == article
