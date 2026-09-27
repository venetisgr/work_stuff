"""Tests for dip detection, the severity ranking and candidate selection (with fake prices and a fake store)."""

from __future__ import annotations

from datetime import timedelta

import pytest
import requests
from conftest import NOW, make_article, make_impact, make_opportunity, make_stats

from dip_scanner.config import DipConfig, ScanConfig, ScannerConfig, UniverseConfig
from dip_scanner.detect import dip_reasons, select_candidates, severity
from dip_scanner.models import Article, Impact, PriceStats
from dip_scanner.prices import PriceError

NO_DIP = {"change_1d_pct": -1.0, "change_5d_pct": -2.0, "drawdown_20d_pct": -4.0}


class FakePrices:
    """YahooPrices stand-in: stats from a dict; unknown tickers raise PriceError, errors[ticker] is raised."""

    def __init__(self, stats: dict[str, PriceStats] | None = None, errors: dict[str, Exception] | None = None):
        self.by_ticker = dict(stats or {})
        self.errors = dict(errors or {})
        self.calls: list[tuple[str, object]] = []

    def stats(self, ticker, *, now=None):
        self.calls.append((ticker, now))
        if ticker in self.errors:
            raise self.errors[ticker]
        if ticker not in self.by_ticker:
            raise PriceError(f"No chart data for {ticker}")
        return self.by_ticker[ticker]

    @property
    def tickers(self) -> list[str]:
        return [ticker for ticker, _ in self.calls]


class FakeStore:
    """The part of Store that select_candidates uses."""

    def __init__(self, last=None, valid=None):
        self.last = dict(last or {})
        self.valid = dict(valid or {})
        self.marked: list[tuple[str, bool, object]] = []

    def last_opportunity(self, ticker):
        return self.last.get(ticker)

    def ticker_valid(self, ticker, *, now=None):
        return self.valid.get(ticker)

    def set_ticker_valid(self, ticker, valid, *, checked):
        self.marked.append((ticker, valid, checked))
        self.valid[ticker] = valid


def news(ticker="AMD", *, title=None, hours_ago=1.0, fetched_hours_ago=None, **impact) -> tuple[Impact, Article]:
    """One impact on ticker with its own article, published hours_ago before NOW."""
    published = NOW - timedelta(hours=hours_ago)
    fetched = NOW - timedelta(hours=hours_ago if fetched_hours_ago is None else fetched_hours_ago)
    article = make_article(
        title=title or f"{ticker} news from {hours_ago} hours ago", published=published, fetched=fetched
    )
    return make_impact(ticker=ticker, article_id=article.id, **impact), article


def config(*, scan=None, dip=None, universe=None) -> ScannerConfig:
    return ScannerConfig(
        scan=ScanConfig(**(scan or {})), dip=DipConfig(**(dip or {})), universe=UniverseConfig(**(universe or {}))
    )


def select(impacts, prices, store=None, cfg=None):
    return select_candidates(impacts, prices, store or FakeStore(), cfg or config(), now=NOW)


# --- dip_reasons ---------------------------------------------------------------------------------------------------


def test_dip_reasons_lists_every_threshold_that_is_met():
    # make_stats: 142.50 after 150.00 (-5.0%), -8.0% over 5 days, 165.00 20-day high (-13.6%).
    assert dip_reasons(make_stats(), DipConfig()) == [
        "down 5.0% today",
        "down 8.0% over 5 days",
        "13.6% below its 20-day high",
    ]


def test_no_dip_gives_no_reasons():
    assert dip_reasons(make_stats(**NO_DIP), DipConfig()) == []


@pytest.mark.parametrize(
    ("change", "expected"),
    [(-3.0, ["down 3.0% today"]), (-2.99, []), (-6.24, ["down 6.2% today"]), (2.0, [])],
)
def test_one_day_threshold_is_inclusive(change, expected):
    stats = make_stats(change_1d_pct=change, change_5d_pct=-1.0, drawdown_20d_pct=-1.0)
    assert dip_reasons(stats, DipConfig()) == expected


def test_each_threshold_comes_from_the_config():
    cfg = DipConfig(min_drop_1d_pct=6, min_drop_5d_pct=8, min_drawdown_20d_pct=20)
    assert dip_reasons(make_stats(), cfg) == ["down 8.0% over 5 days"]


def test_zero_threshold_means_any_fall_but_never_a_rise():
    cfg = DipConfig(min_drop_1d_pct=0, min_drop_5d_pct=0, min_drawdown_20d_pct=0)
    flat = make_stats(change_1d_pct=1.0, change_5d_pct=0.0, drawdown_20d_pct=0.0)
    assert dip_reasons(flat, cfg) == []
    assert dip_reasons(make_stats(change_1d_pct=-0.4, change_5d_pct=0.0, drawdown_20d_pct=0.0), cfg) == [
        "down 0.4% today"
    ]


# --- severity ------------------------------------------------------------------------------------------------------


def test_severity_hand_computed():
    # drop = max(5.0, 8.0 / 1.5, 13.636 / 2) = 6.818; news = 1.5 * 4 (direct, negative); one article: no
    # corroboration; volume = 2.3 - 1 = 1.3. Total 14.118.
    assert severity(make_stats(), [(make_impact(), make_article())]) == 14.12


def test_severity_weights_relation_direction_and_extra_articles():
    stats = make_stats(change_1d_pct=-4.0, change_5d_pct=-3.0, drawdown_20d_pct=-5.0, volume_ratio=None)
    impacts = [
        news(relation="indirect", direction="negative", magnitude=2),  # 2 * 0.5 * 1.0 = 1.0
        news(hours_ago=2, relation="direct", direction="mixed", magnitude=2),  # 2 * 1.0 * 0.7 = 1.4 (strongest)
        news(hours_ago=3, relation="indirect", direction="positive", magnitude=5),  # 5 * 0.5 * 0.3 = 0.75
    ]
    # drop = max(4, 2, 2.5) = 4; news = 1.5 * 1.4 = 2.1; 2 extra articles = 1.0; no volume data.
    assert severity(stats, impacts) == pytest.approx(7.1)


def test_severity_caps_the_corroboration_and_volume_bonuses():
    stats = make_stats(change_1d_pct=-4.0, change_5d_pct=-3.0, drawdown_20d_pct=-5.0, volume_ratio=10.0)
    impacts = [news(hours_ago=hours, magnitude=2) for hours in range(1, 11)]
    # 4 + 1.5 * 2 + min(2, 0.5 * 9) + min(3, 9)
    assert severity(stats, impacts) == pytest.approx(12.0)


def test_severity_without_news_or_heavy_volume_is_the_drop():
    stats = make_stats(change_1d_pct=-4.0, change_5d_pct=-3.0, drawdown_20d_pct=-5.0, volume_ratio=0.8)
    assert severity(stats, []) == pytest.approx(4.0)


def test_bigger_drops_and_direct_news_rank_higher():
    small, big = make_stats(change_1d_pct=-4.0), make_stats(change_1d_pct=-12.0)
    direct = [news(relation="direct", magnitude=3)]
    indirect = [news(relation="indirect", magnitude=3)]
    assert severity(big, direct) > severity(small, direct)
    assert severity(small, direct) > severity(small, indirect)


# --- select_candidates ---------------------------------------------------------------------------------------------


def test_a_dip_with_news_becomes_a_candidate():
    impacts = [news()]
    prices = FakePrices({"AMD": make_stats()})
    store = FakeStore()

    candidates, notes = select(impacts, prices, store)

    assert notes == []
    [candidate] = candidates
    assert candidate.ticker == "AMD"
    assert candidate.company == "Advanced Micro Devices, Inc."  # Yahoo's name wins
    assert candidate.stats == prices.by_ticker["AMD"]
    assert candidate.impacts == impacts
    assert candidate.dip_reasons == ["down 5.0% today", "down 8.0% over 5 days", "13.6% below its 20-day high"]
    assert candidate.severity == severity(candidate.stats, impacts)
    assert prices.calls == [("AMD", NOW)]
    assert store.marked == [("AMD", True, NOW)]  # an unchecked ticker with prices is remembered as valid


def test_company_falls_back_to_the_name_the_triage_used_most():
    impacts = [
        news(hours_ago=1, company="AMD"),
        news(hours_ago=2, company="Advanced Micro Devices"),
        news(hours_ago=3, company="Advanced Micro Devices"),
    ]
    prices = FakePrices({"AMD": make_stats(name=None)})
    [candidate], _ = select(impacts, prices)
    assert candidate.company == "Advanced Micro Devices"

    tie = [news(hours_ago=1, company="AMD"), news(hours_ago=2, company="Advanced Micro Devices")]
    [candidate], _ = select(tie, prices)
    assert candidate.company == "AMD"  # the newest article wins a tie


def test_impacts_are_deduplicated_and_newest_first():
    old, new = news(hours_ago=5), news(hours_ago=1)
    [candidate], _ = select([old, new, old], FakePrices({"AMD": make_stats()}))
    assert candidate.impacts == [new, old]


def test_direction_magnitude_and_relation_filters_skip_tickers_with_a_note():
    impacts = [
        news("AAPL", direction="positive"),
        news("MSFT", hours_ago=2, direction="neutral"),
        news("MSFT", hours_ago=3, magnitude=1),
        news("NVDA", relation="indirect", magnitude=2),
        news("INTC", magnitude=1),
    ]
    prices = FakePrices({ticker: make_stats(ticker=ticker) for ticker in ("AAPL", "MSFT", "NVDA", "INTC")})
    cfg = config(dip={"include_indirect": False})

    candidates, notes = select(impacts, prices, cfg=cfg)

    assert candidates == []
    assert prices.calls == []  # no prices are fetched for tickers without qualifying news
    assert notes == [
        "No qualifying news ([dip] filters): AAPL (positive news), MSFT (neutral news; magnitude 1 < 2), "
        "NVDA (indirect news), INTC (magnitude 1 < 2)"
    ]


def test_watchlist_tickers_skip_magnitude_and_relation_but_not_direction():
    impacts = [news("NVDA", relation="indirect", magnitude=1), news("AAPL", direction="positive")]
    prices = FakePrices({"NVDA": make_stats(ticker="NVDA"), "AAPL": make_stats(ticker="AAPL")})
    cfg = config(dip={"include_indirect": False}, universe={"watchlist": ("NVDA", "AAPL")})

    candidates, notes = select(impacts, prices, cfg=cfg)

    assert [candidate.ticker for candidate in candidates] == ["NVDA"]
    assert notes == ["No qualifying news ([dip] filters): AAPL (positive news)"]


def test_mixed_news_counts_by_default_and_repeated_reasons_are_counted():
    impacts = [
        news("AMD", direction="mixed"),
        news("TSLA", direction="positive"),
        news("TSLA", hours_ago=2, direction="positive"),
    ]
    prices = FakePrices({"AMD": make_stats(), "TSLA": make_stats(ticker="TSLA")})
    candidates, notes = select(impacts, prices)
    assert [candidate.ticker for candidate in candidates] == ["AMD"]
    assert notes == ["No qualifying news ([dip] filters): TSLA (2x positive news)"]


def test_universe_exclude_only_watchlist_and_suffixes():
    tickers = ("AMD", "BRK-B", "SAP.DE", "7203.T", "TSLA", "GME")
    impacts = [news(ticker) for ticker in tickers]
    prices = FakePrices({ticker: make_stats(ticker=ticker) for ticker in tickers})

    cfg = config(universe={"exclude": ("GME",), "allowed_suffixes": ("", ".DE")})
    candidates, notes = select(impacts, prices, cfg=cfg)
    assert sorted(candidate.ticker for candidate in candidates) == ["AMD", "BRK-B", "SAP.DE", "TSLA"]
    assert notes == [
        "Exchange not in [universe] allowed_suffixes ('', '.DE'): 7203.T",
        "Excluded in [universe] exclude: GME",
    ]

    cfg = config(universe={"only_watchlist": True, "watchlist": ("AMD", "SAP.DE")})
    candidates, notes = select(impacts, prices, cfg=cfg)
    assert sorted(candidate.ticker for candidate in candidates) == ["AMD", "SAP.DE"]
    assert notes == ["Not on the watchlist ([universe] only_watchlist): BRK-B, 7203.T, TSLA, GME"]


def test_cooldown_skips_a_recently_analysed_ticker_without_new_news():
    impacts = [news(hours_ago=3)]
    last = make_opportunity(created=NOW - timedelta(hours=2), article_ids=[impacts[0][1].id])
    prices = FakePrices({"AMD": make_stats()})

    candidates, notes = select(impacts, prices, FakeStore(last={"AMD": last}))

    assert candidates == []
    assert prices.calls == []
    assert notes == ["Analysed within the 24h cooldown, no new news since: AMD (analysed 2.0h ago)"]


@pytest.mark.parametrize(
    ("created_hours_ago", "cooldown_hours", "fetched_hours_ago"),
    [
        (2, 24, 1),  # an article fetched after the last analysis (published before it) is new news
        (30, 24, 40),  # the last analysis is older than the cooldown
        (2, 0, 3),  # no cooldown
    ],
)
def test_cooldown_lets_a_ticker_through(created_hours_ago, cooldown_hours, fetched_hours_ago):
    impacts = [news(hours_ago=max(3, fetched_hours_ago), fetched_hours_ago=fetched_hours_ago)]
    last = make_opportunity(created=NOW - timedelta(hours=created_hours_ago), article_ids=[])
    cfg = config(scan={"cooldown_hours": cooldown_hours, "lookback_hours": 48})

    candidates, notes = select(impacts, FakePrices({"AMD": make_stats()}), FakeStore(last={"AMD": last}), cfg)

    assert [candidate.ticker for candidate in candidates] == ["AMD"]
    assert notes == []


def test_cooldown_ignores_articles_the_last_analysis_already_used():
    impacts = [news(hours_ago=3, fetched_hours_ago=1)]
    last = make_opportunity(created=NOW - timedelta(hours=2), article_ids=[impacts[0][1].id])
    candidates, notes = select(impacts, FakePrices({"AMD": make_stats()}), FakeStore(last={"AMD": last}))
    assert candidates == []
    assert len(notes) == 1 and "AMD" in notes[0]


def test_tickers_marked_invalid_are_skipped_without_fetching_prices():
    prices = FakePrices({"ZZZZ": make_stats(ticker="ZZZZ")})
    store = FakeStore(valid={"ZZZZ": False})
    candidates, notes = select([news("ZZZZ")], prices, store)
    assert candidates == []
    assert prices.calls == []
    assert store.marked == []
    assert notes == ["No prices (marked invalid, rechecked after 7 days): ZZZZ"]


def test_price_error_marks_the_ticker_invalid():
    store = FakeStore()
    candidates, notes = select([news("NOPE")], FakePrices(), store)
    assert candidates == []
    assert store.marked == [("NOPE", False, NOW)]
    assert notes == ["No prices (unknown symbol or no data; marked invalid): NOPE (No chart data for NOPE)"]


def test_network_errors_are_noted_but_do_not_mark_the_ticker_invalid():
    prices = FakePrices(errors={"AMD": requests.ConnectionError("connection reset")})
    store = FakeStore(valid={"AMD": True})
    candidates, notes = select([news()], prices, store)
    assert candidates == []
    assert store.marked == []
    assert notes == ["Couldn't get prices, will retry next cycle: AMD (connection reset)"]


def test_known_valid_tickers_are_not_marked_again():
    store = FakeStore(valid={"AMD": True})
    candidates, _ = select([news()], FakePrices({"AMD": make_stats()}), store)
    assert len(candidates) == 1
    assert store.marked == []


def test_prices_below_min_price_are_skipped():
    prices = FakePrices({"PENNY": make_stats(ticker="PENNY", price=0.45)})
    candidates, notes = select([news("PENNY")], prices)
    assert candidates == []
    assert notes == ["Price below [universe] min_price 1: PENNY (0.45 USD)"]


def test_no_dip_is_noted_with_the_moves():
    prices = FakePrices({"AAPL": make_stats(ticker="AAPL", **NO_DIP)})
    candidates, notes = select([news("AAPL")], prices)
    assert candidates == []
    assert notes == ["No dip (price not down enough): AAPL (1d -1.0%, 5d -2.0%, -4.0% from the 20-day high)"]


def test_candidates_are_sorted_by_severity_and_capped_with_a_note():
    drops = {"AAA": -4.0, "BBB": -8.0, "CCC": -12.0}
    prices = FakePrices({ticker: make_stats(ticker=ticker, change_1d_pct=drop) for ticker, drop in drops.items()})
    impacts = [news(ticker) for ticker in drops]

    candidates, notes = select(impacts, prices, cfg=config(scan={"max_candidates_per_cycle": 2}))

    assert [candidate.ticker for candidate in candidates] == ["CCC", "BBB"]
    assert candidates[0].severity > candidates[1].severity
    dropped = severity(prices.by_ticker["AAA"], [impacts[0]])
    assert notes == [
        "Over the limit of 2 candidates per cycle ([scan] max_candidates_per_cycle), left for the next cycle: "
        f"AAA (severity {dropped:.1f})"
    ]

    candidates, notes = select(impacts, prices, cfg=config(scan={"max_candidates_per_cycle": 0}))
    assert candidates == []
    assert len(notes) == 1 and all(ticker in notes[0] for ticker in drops)


def test_news_older_than_the_lookback_is_ignored():
    impacts = [news("OLD", hours_ago=50), news("OLD", hours_ago=60), news("AMD", hours_ago=47)]
    prices = FakePrices({"AMD": make_stats(), "OLD": make_stats(ticker="OLD")})
    candidates, notes = select(impacts, prices)
    assert [candidate.ticker for candidate in candidates] == ["AMD"]
    assert candidates[0].impacts == [impacts[2]]
    assert notes == ["No news in the last 48h ([scan] lookback_hours): OLD (2 older)"]


def test_every_skipped_ticker_is_named_in_the_notes():
    impacts = [
        news("AMD"),  # candidate
        news("GME"),  # excluded
        news("AAPL", direction="positive"),  # no qualifying news
        news("NVDA", hours_ago=3),  # cooldown
        news("ZZZZ"),  # marked invalid
        news("NOPE"),  # no prices
        news("PENNY"),  # below min_price
        news("MSFT"),  # no dip
        news("0700.HK"),  # suffix not allowed
        news("OLD", hours_ago=100),  # too old
    ]
    prices = FakePrices(
        {
            "AMD": make_stats(),
            "NVDA": make_stats(ticker="NVDA"),
            "PENNY": make_stats(ticker="PENNY", price=0.2),
            "MSFT": make_stats(ticker="MSFT", **NO_DIP),
        }
    )
    store = FakeStore(
        last={"NVDA": make_opportunity(ticker="NVDA", created=NOW - timedelta(hours=1))}, valid={"ZZZZ": False}
    )
    cfg = config(universe={"exclude": ("GME",), "allowed_suffixes": ("",)})

    candidates, notes = select(impacts, prices, store, cfg)

    assert [candidate.ticker for candidate in candidates] == ["AMD"]
    text = "\n".join(notes)
    for ticker in ("GME", "AAPL", "NVDA", "ZZZZ", "NOPE", "PENNY", "MSFT", "0700.HK", "OLD"):
        assert ticker in text, ticker
    assert "AMD" not in text
