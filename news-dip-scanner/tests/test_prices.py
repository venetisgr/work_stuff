import copy
import json
import math
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import requests
from conftest import NOW, FakeResponse, FakeSession, make_bars

from dip_scanner.models import PriceBar
from dip_scanner.prices import (
    BROWSER_USER_AGENT,
    RATE_LIMIT_BACKOFF,
    PriceError,
    PriceFetchError,
    YahooPrices,
    compute_stats,
    range_for,
    worst_drawdown_pct,
)

FIXTURES = Path(__file__).parent / "fixtures"
QUERY1 = "https://query1.finance.yahoo.com/v8/finance/chart/"
QUERY2 = "https://query2.finance.yahoo.com/v8/finance/chart/"
# The synthetic fixture (see its values in the tests below): 260 sessions to Friday 2026-09-25, 3 all-null holiday
# rows, a 10% drop from 159.50 to 143.55 on the last day, 2.5x the usual volume.
LAST_DAY = date(2026, 9, 25)
CLOSE_TIME = datetime(2026, 9, 25, 20, 0, 1, tzinfo=UTC)  # 16:00:01 in New York


def chart_doc() -> dict:
    return json.loads((FIXTURES / "yahoo_chart_amd.json").read_text())


def result_of(doc: dict) -> dict:
    return doc["chart"]["result"][0]


def fixture_rows(doc: dict) -> list[dict]:
    """The fixture's non-null rows as dicts, oldest first (read straight from the JSON, not through prices.py)."""
    result = result_of(doc)
    quote = result["indicators"]["quote"][0]
    rows = []
    for index, stamp in enumerate(result["timestamp"]):
        if quote["close"][index] is not None:
            rows.append(
                {name: quote[name][index] for name in ("open", "high", "low", "close", "volume")} | {"t": stamp}
            )
    return rows


def drop_last_row(doc: dict) -> None:
    result = result_of(doc)
    result["timestamp"].pop()
    for column in result["indicators"]["quote"][0].values():
        column.pop()


def prices_for(routes: dict, **kwargs) -> tuple[YahooPrices, FakeSession, list[float]]:
    session = FakeSession(routes)
    sleeps: list[float] = []
    return YahooPrices(session, sleep=sleeps.append, clock=lambda: NOW, **kwargs), session, sleeps


def meta_for(bars: list[PriceBar], **overrides) -> dict:
    """Chart meta for make_bars() bars: a New York listing quoted at the last bar's close."""
    last = bars[-1].day
    meta = {
        "currency": "USD",
        "symbol": "TEST",
        "exchangeName": "NMS",
        "gmtoffset": -18000,
        "exchangeTimezoneName": "America/New_York",
        "regularMarketTime": int(datetime(last.year, last.month, last.day, 21, 0, tzinfo=UTC).timestamp()),
        "regularMarketPrice": bars[-1].close,
    }
    meta.update(overrides)
    return meta


def doc_from(meta: dict, rows: list[tuple]) -> dict:
    """A chart response from (timestamp, open, high, low, close, volume) rows."""
    columns = list(zip(*rows, strict=True)) if rows else [[]] * 6
    quote = dict(zip(("open", "high", "low", "close", "volume"), (list(column) for column in columns[1:]), strict=True))
    return {
        "chart": {
            "result": [{"meta": meta, "timestamp": list(columns[0]), "indicators": {"quote": [quote]}}],
            "error": None,
        }
    }


# --- chart ---------------------------------------------------------------------------------------------------------


def test_chart_returns_meta_and_bars_without_the_null_rows():
    prices, session, _ = prices_for({QUERY1 + "AMD": chart_doc()})

    meta, bars = prices.chart("amd")

    assert meta["symbol"] == "AMD"
    assert len(bars) == 260
    assert [bar.day for bar in bars] == sorted({bar.day for bar in bars})
    assert not {date(2025, 11, 27), date(2025, 12, 25), date(2026, 1, 1)} & {bar.day for bar in bars}
    assert bars[0].day == date(2025, 9, 15)
    assert bars[-1] == PriceBar(LAST_DAY, open=159.5, high=160.5, low=142.55, close=143.55, volume=105_000_000)
    [call] = session.calls
    assert call["url"] == QUERY1 + "AMD"
    assert call["params"] == {"range": "2y", "interval": "1d"}
    assert call["headers"]["User-Agent"] == BROWSER_USER_AGENT
    assert call["timeout"] == 15


def test_bar_days_are_exchange_local_dates():
    # Sydney opens at 10:00 local = 23:00 UTC the day before (summer time): the bar belongs to the local day.
    rows = [(int(datetime(2026, 1, day, 23, 0, tzinfo=UTC).timestamp()), 10, 11, 9, 10.5, 1000) for day in (13, 14)]
    meta = {"symbol": "BHP.AX", "gmtoffset": 39600, "exchangeTimezoneName": "Australia/Sydney"}
    prices, _, _ = prices_for({QUERY1 + "BHP.AX": doc_from(meta, rows)})

    assert [bar.day for bar in prices.chart("BHP.AX")[1]] == [date(2026, 1, 14), date(2026, 1, 15)]

    # Without a usable time zone name, gmtoffset gives the same answer.
    meta = {"symbol": "BHP.AX", "gmtoffset": 39600, "exchangeTimezoneName": "Nowhere/Unknown"}
    prices, _, _ = prices_for({QUERY1 + "BHP.AX": doc_from(meta, rows)})
    assert [bar.day for bar in prices.chart("BHP.AX")[1]] == [date(2026, 1, 14), date(2026, 1, 15)]


def test_chart_fills_partial_rows_drops_bad_ones_and_keeps_the_later_row_of_a_day():
    def stamp(day: int, hour: int = 14) -> int:
        return int(datetime(2026, 9, day, hour, 30, tzinfo=UTC).timestamp())

    rows = [
        (stamp(21), None, None, None, 10.0, None),  # close only: open/high/low from the close, volume 0
        (stamp(22), 10.0, 11.0, 9.0, 0.0, 500),  # a zero close is bad data
        (stamp(23), 10.0, 11.0, 9.0, None, 500),  # null close
        (stamp(24), 10.0, 12.0, 9.5, 11.0, 700),
        (stamp(24, 19), 10.0, 12.5, 9.5, 12.0, 900),  # Yahoo's live row for the same session: it wins
    ]
    meta = {"symbol": "X", "gmtoffset": -14400, "exchangeTimezoneName": "America/New_York"}
    prices, _, _ = prices_for({QUERY1 + "X": doc_from(meta, rows)})

    assert prices.chart("X")[1] == [
        PriceBar(date(2026, 9, 21), 10.0, 10.0, 10.0, 10.0, 0),
        PriceBar(date(2026, 9, 24), 10.0, 12.5, 9.5, 12.0, 900),
    ]


def test_unknown_symbol_is_a_price_error():
    body = {
        "chart": {
            "result": None,
            "error": {"code": "Not Found", "description": "No data found, symbol may be delisted"},
        }
    }
    prices, session, _ = prices_for({QUERY1: FakeResponse(status_code=404, json_data=body)})

    with pytest.raises(PriceError, match="ZZZZ.*No data found, symbol may be delisted"):
        prices.chart("zzzz")
    assert session.urls == [QUERY1 + "ZZZZ"]  # a 404 is an answer: no fallback host


@pytest.mark.parametrize(
    "body",
    [
        {"chart": {"result": None, "error": {"code": "Bad Request", "description": "Invalid input"}}},
        {"chart": {"result": [], "error": None}},
        {"chart": {"result": None, "error": None}},
    ],
)
def test_error_or_empty_result_is_a_price_error(body):
    prices, _, _ = prices_for({QUERY1: body})

    with pytest.raises(PriceError):
        prices.chart("AMD")


def test_falls_back_to_query2_on_server_errors_and_connection_errors():
    for failure in (503, requests.ConnectionError("reset"), requests.Timeout("slow")):
        prices, session, sleeps = prices_for({QUERY1: failure, QUERY2 + "AMD": chart_doc()})

        assert len(prices.chart("AMD")[1]) == 260
        assert session.urls == [QUERY1 + "AMD", QUERY2 + "AMD"]
        assert sleeps == []


def test_retries_once_after_a_short_backoff_when_rate_limited():
    prices, session, sleeps = prices_for({QUERY1 + "AMD": [429, chart_doc()]})

    assert len(prices.chart("AMD")[1]) == 260
    assert session.urls == [QUERY1 + "AMD", QUERY1 + "AMD"]
    assert sleeps == [RATE_LIMIT_BACKOFF]
    assert RATE_LIMIT_BACKOFF <= 5


def test_gives_up_with_a_fetch_error_not_a_price_error():
    """Network trouble must not look like an unknown ticker (callers mark those invalid for days)."""
    prices, _, sleeps = prices_for({QUERY1: 429})
    with pytest.raises(PriceFetchError, match="429") as caught:
        prices.chart("AMD")
    assert not isinstance(caught.value, PriceError)
    assert sleeps == [RATE_LIMIT_BACKOFF]

    prices, _, _ = prices_for({QUERY1: requests.ConnectionError("down"), QUERY2: 502})
    with pytest.raises(PriceFetchError, match=r"query1.*ConnectionError.*query2.*502"):
        prices.chart("AMD")

    for response in (FakeResponse(status_code=403), FakeResponse(content=b"<html>captcha</html>")):
        prices, _, _ = prices_for({QUERY1: response})
        with pytest.raises(PriceFetchError):
            prices.chart("AMD")


def test_default_session_sends_a_browser_user_agent():
    prices = YahooPrices()
    assert prices.session.headers["User-Agent"] == BROWSER_USER_AGENT
    assert BROWSER_USER_AGENT.startswith("Mozilla/5.0")


# --- compute_stats: the fixture, checked by hand -------------------------------------------------------------------


def test_compute_stats_after_the_close_matches_hand_computed_values():
    doc = chart_doc()
    prices, _, _ = prices_for({QUERY1: doc})
    meta, bars = prices.chart("AMD")

    stats = compute_stats("AMD", meta, bars)

    assert stats.ticker == "AMD"
    assert stats.name == "Advanced Micro Devices, Inc."
    assert stats.currency == "USD"
    assert stats.exchange == "NasdaqGS"
    assert stats.as_of == CLOSE_TIME
    assert stats.price == 143.55
    assert stats.previous_close == 159.5  # not meta.chartPreviousClose (99.0, the close before the range)
    assert stats.change_1d_pct == pytest.approx(-10.0)  # 143.55 / 159.50
    assert stats.change_5d_pct == pytest.approx(-8.857142857)  # 143.55 / 157.50, the close 5 sessions earlier
    assert stats.change_20d_pct == pytest.approx(-4.3)  # 143.55 / 150.00, the close 20 sessions earlier
    assert stats.high_20d == 160.5
    assert stats.high_52w == 160.5
    assert stats.low_52w == 90.0
    assert stats.drawdown_20d_pct == pytest.approx(-10.560747664)  # 143.55 / 160.50
    assert stats.drawdown_52w_pct == pytest.approx(-10.560747664)
    assert stats.above_low_52w_pct == pytest.approx(59.5)  # 143.55 / 90
    assert stats.volume_ratio == pytest.approx(2.5)  # 105M / 42M (40M + 0..4M, each 4 times)
    assert stats.worst_6m_drawdown_pct == pytest.approx(-30.0)  # 130 -> 91 within 39 sessions

    # The averages and the volatility, worked out independently from the raw JSON arrays.
    closes = [row["close"] for row in fixture_rows(doc)]
    assert stats.sma_50 == pytest.approx(sum(closes[-50:]) / 50)
    assert stats.sma_50 == pytest.approx(148.0638)
    assert stats.sma_200 == pytest.approx(sum(closes[-200:]) / 200)
    returns = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - 60, len(closes))]
    mean = sum(returns) / 60
    volatility = math.sqrt(sum((r - mean) ** 2 for r in returns) / 59) * math.sqrt(252) * 100
    assert stats.volatility_pct == pytest.approx(volatility)
    assert 15 < stats.volatility_pct < 30
    assert stats.stat_low_6m == pytest.approx(143.55 * math.exp(-1.645 * volatility / 100 * math.sqrt(0.5)))
    assert stats.stat_low_6m < stats.price


def test_compute_stats_during_the_session_uses_the_live_price():
    doc = chart_doc()
    meta = result_of(doc)["meta"]
    meta["regularMarketTime"] = int(datetime(2026, 9, 25, 14, 45, tzinfo=UTC).timestamp())  # 10:45 in New York
    meta["regularMarketPrice"] = 150.0
    quote = result_of(doc)["indicators"]["quote"][0]
    quote["close"][-1], quote["low"][-1], quote["volume"][-1] = 150.0, 149.0, 21_000_000  # the partial session
    prices, _, _ = prices_for({QUERY1: doc})

    stats = compute_stats("AMD", *prices.chart("AMD"))

    assert stats.as_of == datetime(2026, 9, 25, 14, 45, tzinfo=UTC)
    assert stats.previous_close == 159.5  # yesterday, not today's partial bar
    assert stats.change_1d_pct == pytest.approx((150 / 159.5 - 1) * 100)
    assert stats.change_5d_pct == pytest.approx((150 / 157.5 - 1) * 100)
    assert stats.change_20d_pct == pytest.approx(0.0)
    assert stats.volume_ratio == pytest.approx(0.5)  # 21M so far / 42M


def test_compute_stats_before_yahoo_sends_the_latest_bar():
    """The quote is from today but the bars stop yesterday: the changes still compare with yesterday's close."""
    doc = chart_doc()
    drop_last_row(doc)
    prices, _, _ = prices_for({QUERY1: doc})
    meta, bars = prices.chart("AMD")
    assert bars[-1].day == date(2026, 9, 24)

    stats = compute_stats("AMD", meta, bars)

    assert stats.previous_close == 159.5
    assert stats.change_1d_pct == pytest.approx(-10.0)
    assert stats.change_5d_pct == pytest.approx(-8.857142857)
    assert stats.change_20d_pct == pytest.approx(-4.3)
    assert stats.high_20d == 160.5
    assert stats.volume_ratio is None  # no volume for today yet


def test_compute_stats_the_morning_after_uses_the_last_session():
    """Before the next open, the quote and the last bar are both yesterday's: nothing changes."""
    prices, _, _ = prices_for({QUERY1: chart_doc()})
    after_close = compute_stats("AMD", *prices.chart("AMD"))

    stats = prices.stats("AMD", now=datetime(2026, 9, 28, 12, 0, tzinfo=UTC))  # Monday, before the open

    assert stats == after_close


def test_52_week_range_comes_from_meta_else_from_the_last_252_sessions():
    doc = chart_doc()
    meta = result_of(doc)["meta"]
    meta["fiftyTwoWeekHigh"], meta["fiftyTwoWeekLow"] = 175.0, 80.0
    prices, _, _ = prices_for({QUERY1: doc})
    stats = compute_stats("AMD", *prices.chart("AMD"))
    assert (stats.high_52w, stats.low_52w) == (175.0, 80.0)

    del meta["fiftyTwoWeekHigh"], meta["fiftyTwoWeekLow"]
    prices, _, _ = prices_for({QUERY1: doc})
    stats = compute_stats("AMD", *prices.chart("AMD"))
    rows = fixture_rows(doc)[-252:]
    assert stats.high_52w == max(row["high"] for row in rows) == 160.5
    assert stats.low_52w == min(row["low"] for row in rows) == 90.0

    # A stale meta high below the recent highs is lifted, so the 52-week drawdown is never smaller than the 20-day one.
    meta["fiftyTwoWeekHigh"], meta["fiftyTwoWeekLow"] = 150.0, 150.0
    prices, _, _ = prices_for({QUERY1: doc})
    stats = compute_stats("AMD", *prices.chart("AMD"))
    assert stats.high_52w == 160.5
    assert stats.low_52w == 143.55


def test_compute_stats_needs_21_bars():
    bars = make_bars([100 + i for i in range(20)])
    with pytest.raises(PriceError, match="only 20 days"):
        compute_stats("NEW", meta_for(bars), bars)

    bars = make_bars([100 + i for i in range(21)])
    stats = compute_stats("NEW", meta_for(bars), bars)
    assert stats.change_20d_pct == pytest.approx((120 / 100 - 1) * 100)
    assert stats.sma_50 is None
    assert stats.sma_200 is None


def test_moving_averages_need_50_and_200_sessions():
    bars = make_bars([50 + (i % 7) for i in range(199)])
    stats = compute_stats("X", meta_for(bars), bars)
    assert stats.sma_50 == pytest.approx(sum(bar.close for bar in bars[-50:]) / 50)
    assert stats.sma_200 is None

    bars = make_bars([50 + (i % 7) for i in range(200)])
    assert compute_stats("X", meta_for(bars), bars).sma_200 == pytest.approx(sum(bar.close for bar in bars) / 200)


def test_meta_gaps_fall_back_to_the_bars():
    bars = make_bars([20 + i * 0.1 for i in range(30)])
    meta = {"regularMarketTime": None, "shortName": "  Some   Corp   I ", "exchangeName": "NYQ"}

    stats = compute_stats("X", meta, bars)

    assert stats.price == bars[-1].close
    assert stats.previous_close == bars[-2].close
    assert stats.name == "Some Corp I"
    assert stats.currency == "?"
    assert stats.exchange == "NYQ"
    assert stats.as_of.date() == bars[-1].day


def test_volume_ratio_is_none_without_volume_data():
    bars = make_bars([10 + i % 3 for i in range(30)], volume=0)
    assert compute_stats("^GSPC", meta_for(bars), bars).volume_ratio is None


# --- worst 6-month drawdown ----------------------------------------------------------------------------------------


def brute_force_worst(closes: list[float], window: int) -> float:
    worst = 0.0
    for j in range(len(closes)):
        for i in range(max(0, j - window + 1), j + 1):
            worst = min(worst, closes[j] / closes[i] - 1)
    return worst * 100


def test_worst_drawdown_only_counts_falls_within_the_window():
    # A slow slide from 200 to 100 over 300 sessions: the whole fall (-50%) is longer than 6 months.
    closes = [200 - i * 100 / 299 for i in range(300)]
    worst = worst_drawdown_pct(closes, 126)
    assert worst == pytest.approx(brute_force_worst(closes, 126))
    assert worst == pytest.approx((closes[-1] / closes[-126] - 1) * 100)  # the steepest window is the last one
    assert -50 < worst < -20


def test_worst_drawdown_matches_brute_force_on_a_random_walk():
    seed, closes = 12345, [100.0]
    for _ in range(400):
        seed = (seed * 1103515245 + 12345) % 2**31
        closes.append(closes[-1] * (1 + (seed / 2**31 - 0.5) * 0.08))

    for window in (5, 126, 1000):
        assert worst_drawdown_pct(closes, window) == pytest.approx(brute_force_worst(closes, window))
    assert worst_drawdown_pct([1.0, 2.0, 3.0], 126) == 0.0


def test_compute_stats_drawdown_includes_the_live_price():
    bars = make_bars([100.0] * 30)
    stats = compute_stats("X", meta_for(bars, regularMarketPrice=80.0), bars)
    assert stats.worst_6m_drawdown_pct == pytest.approx(-20.0)
    assert stats.change_1d_pct == pytest.approx(-20.0)


# --- stats (cache) and bars_since ----------------------------------------------------------------------------------


def test_stats_are_cached_per_ticker_for_cache_seconds():
    prices, session, _ = prices_for({QUERY1 + "AMD": chart_doc()}, cache_seconds=600)

    first = prices.stats("AMD", now=NOW)
    assert prices.stats("amd", now=NOW + timedelta(minutes=9)) is first
    assert len(session.calls) == 1

    prices.stats("AMD", now=NOW + timedelta(minutes=10))
    assert len(session.calls) == 2


def test_stats_uses_the_clock_when_not_told_the_time():
    now = [NOW]
    session = FakeSession({QUERY1: chart_doc()})
    prices = YahooPrices(session, clock=lambda: now[0])

    prices.stats("AMD")
    prices.stats("AMD")
    assert len(session.calls) == 1
    now[0] += timedelta(hours=1)
    prices.stats("AMD")
    assert len(session.calls) == 2


def test_stats_rejects_a_quote_that_stopped_trading():
    prices, _, _ = prices_for({QUERY1: chart_doc()})

    assert prices.stats("AMD", now=CLOSE_TIME + timedelta(days=10)).price == 143.55  # a long holiday is fine
    with pytest.raises(PriceError, match="hasn't traded since 2026-09-25"):
        prices.stats("AMD", now=CLOSE_TIME + timedelta(days=15))


def test_stats_does_not_cache_errors():
    prices, _, _ = prices_for({QUERY1: [503, chart_doc()], QUERY2: 503})

    with pytest.raises(PriceFetchError):
        prices.stats("AMD", now=NOW)
    assert prices.stats("AMD", now=NOW).price == 143.55


@pytest.mark.parametrize(
    ("start", "expected"),
    [
        (date(2026, 9, 25), "1mo"),
        (date(2026, 9, 5), "1mo"),
        (date(2026, 8, 25), "3mo"),
        (date(2026, 6, 1), "6mo"),
        (date(2026, 1, 2), "1y"),
        (date(2025, 1, 2), "2y"),
        (date(2022, 1, 3), "5y"),
        (date(2019, 1, 2), "10y"),
        (date(2010, 1, 4), "max"),
    ],
)
def test_range_for_picks_the_smallest_range_that_covers_start(start, expected):
    assert range_for(start, date(2026, 9, 25)) == expected


def test_bars_since_fetches_a_covering_range_and_drops_earlier_bars():
    prices, session, _ = prices_for({QUERY1: chart_doc()})

    bars = prices.bars_since("AMD", date(2026, 9, 21), now=NOW)

    assert [bar.day for bar in bars] == [date(2026, 9, day) for day in (21, 22, 23, 24, 25)]
    assert session.calls[0]["params"] == {"range": "1mo", "interval": "1d"}

    prices.bars_since("AMD", date(2026, 2, 2))  # the clock says NOW
    assert session.calls[1]["params"]["range"] == "1y"


def test_compute_stats_does_not_touch_its_inputs():
    doc = chart_doc()
    prices, _, _ = prices_for({QUERY1: doc})
    meta, bars = prices.chart("AMD")
    before = (copy.deepcopy(meta), list(bars))

    compute_stats("AMD", meta, bars)

    assert (meta, bars) == before
