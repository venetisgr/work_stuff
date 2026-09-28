"""Exchange rates for the [account] currency, from Yahoo's currency pairs (EURUSD=X...)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from conftest import FakeSession, chart_json

from dip_scanner.fx import FxRates, main_currency, major_units, pair_symbol, rate_on, same_money
from dip_scanner.prices import PriceError, PriceFetchError, YahooPrices

HOSTS = ("https://query1.finance.yahoo.com/v8/finance/chart/", "https://query2.finance.yahoo.com/v8/finance/chart/")
NOW = datetime(2026, 9, 27, 21, 0, tzinfo=UTC)


def rates(routes: dict) -> tuple[FxRates, FakeSession]:
    """FxRates over a fake Yahoo that serves routes ({symbol: reply}) from both chart hosts."""
    session = FakeSession(
        {host + symbol.replace("=", "%3D"): reply for symbol, reply in routes.items() for host in HOSTS}
    )
    return FxRates(YahooPrices(session, sleep=lambda _: None), clock=lambda: NOW), session


def test_minor_units_and_currencies():
    assert main_currency("GBp") == ("GBP", 100) and main_currency("ZAc") == ("ZAR", 100)
    assert main_currency("ILA") == ("ILS", 100) and main_currency("usd") == ("USD", 1)
    assert major_units(150, "GBp") == 1.5 and major_units(150, "EUR") == 150
    assert same_money("GBp", "GBP") and same_money("EUR", "EUR") and not same_money("USD", "EUR")
    assert not same_money("USD", None)
    assert pair_symbol("EUR", "USD") == "EURUSD=X"


def test_the_account_currency_pair_is_inverted():
    """EURUSD=X is 1.1386 dollars per euro (checked live): a dollar is 0.8783 euros."""
    fx, session = rates({"EURUSD=X": chart_json("EURUSD=X", [1.14, 1.1386], currency="USD")})
    assert fx.rate("USD", "EUR", now=NOW) == pytest.approx(1 / 1.1386)
    assert fx.rate("USD", "EUR", now=NOW) == pytest.approx(0.87827, abs=1e-5)
    assert len(session.calls) == 1  # cached
    assert fx.rate("USD", "EUR", now=NOW + timedelta(minutes=11)) == pytest.approx(1 / 1.1386)
    assert len(session.calls) == 2  # the cache lasts 10 minutes, like the prices'
    assert fx.rate("EUR", "EUR") == 1.0 and len(session.calls) == 2  # nothing to fetch


def test_the_reverse_pair_is_used_when_there_is_no_other():
    fx, session = rates({"XYZEUR=X": chart_json("XYZEUR=X", [0.25], currency="EUR")})  # EURXYZ=X: 404
    assert fx.rate("XYZ", "EUR", now=NOW) == pytest.approx(0.25)
    assert [url.rsplit("/", 1)[1] for url in session.urls] == ["EURXYZ%3DX", "XYZEUR%3DX"]


def test_a_pair_quoted_in_the_wrong_currency_is_never_guessed():
    fx, _ = rates({"EURUSD=X": chart_json("EURUSD=X", [1.14], currency="EUR")})  # claims euros: not EUR/USD
    with pytest.raises(PriceError, match="EURUSD=X is quoted in EUR, not USD"):
        fx.rate("USD", "EUR", now=NOW)


def test_pence_are_converted_through_pounds():
    fx, _ = rates({"EURGBP=X": chart_json("EURGBP=X", [0.8596], currency="GBP")})
    assert fx.rate("GBp", "EUR", now=NOW) == pytest.approx(1 / 0.8596 / 100)
    assert fx.rate("GBp", "GBP", now=NOW) == 0.01  # the same money: only the unit


def test_no_pair_and_no_network_raise():
    fx, _ = rates({})
    with pytest.raises(PriceError, match="no USD/EUR exchange rate"):
        fx.rate("USD", "EUR", now=NOW)
    fx, _ = rates({"EURUSD=X": 503})
    with pytest.raises(PriceFetchError):
        fx.rate("USD", "EUR", now=NOW)


def test_history_is_inverted_and_starts_a_little_early():
    closes = [1.10, 1.12, 1.15, 1.20]  # Tue 1 Sep to Fri 4 Sep
    fx, session = rates({"EURUSD=X": chart_json("EURUSD=X", closes, currency="USD")})
    history = fx.history("USD", "EUR", date(2026, 9, 3), now=NOW)
    assert [day for day, _ in history] == [date(2026, 9, day) for day in (1, 2, 3, 4)]
    assert history[-1][1] == pytest.approx(1 / 1.20)
    assert session.calls[0]["params"]["range"] == "3mo"  # from 10 days before the start: a rate on that day
    assert rate_on(history, date(2026, 9, 6)) == pytest.approx(1 / 1.20)  # a Sunday: Friday's close
    assert rate_on(history, date(2026, 8, 31)) is None
    assert fx.history("EUR", "EUR", date(2026, 9, 3)) == [(date(2026, 8, 24), 1.0)]


def test_rates_into_several_currencies_report_each_failure_on_its_own():
    fx, _ = rates(
        {
            "EURUSD=X": chart_json("EURUSD=X", [1.14], currency="USD"),
            "GBPUSD=X": chart_json("GBPUSD=X", [1.25], currency="USD"),
        }
    )
    found, problems = fx.rates("USD", ["eur", "GBP", " ", "XYZ", "USD", "EUR"], now=NOW)
    assert found == {"EUR": pytest.approx(1 / 1.14), "GBP": pytest.approx(1 / 1.25), "USD": 1.0}
    assert list(problems) == ["XYZ"] and isinstance(problems["XYZ"], PriceError)
    assert fx.rates("GBp", ["GBP"], now=NOW) == ({"GBP": 0.01}, {})
    assert fx.rates("USD", [], now=NOW) == ({}, {})
