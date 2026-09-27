import json
import logging
from datetime import date
from pathlib import Path

import pytest
import requests
from conftest import FakeResponse, FakeSession

from dip_scanner.config import ConfigError
from dip_scanner.fundamentals import (
    FACTS_TTL,
    FACTS_URL,
    MIN_INTERVAL,
    TICKERS_TTL,
    TICKERS_URL,
    SecError,
    SecFundamentals,
    parse_company_facts,
    parse_company_tickers,
)

FIXTURES = Path(__file__).parent / "fixtures"
USER_AGENT = "news-dip-scanner tests contact@example.com"
AMD_FACTS = FACTS_URL.format(cik="0000002488")
M = 1_000_000


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def row(period_end: str, revenue, gross_profit, net_income, eps, cash_flow, operating_income=None) -> dict:
    """An expected row, amounts in millions."""

    def millions(value):
        return None if value is None else value * M

    return {
        "period_end": period_end,
        "revenue": millions(revenue),
        "gross_profit": millions(gross_profit),
        "operating_income": millions(operating_income),
        "net_income": millions(net_income),
        "eps_diluted": eps,
        "operating_cash_flow": millions(cash_flow),
    }


def fact(start: str, end: str, value: float, filed: str = "2026-01-01", **extra) -> dict:
    return {"start": start, "end": end, "val": value, "filed": filed, "form": "10-Q", "fy": 2026, "fp": "Q1", **extra}


def facts_doc(taxonomy: str = "us-gaap", **concepts: dict) -> dict:
    """A companyfacts document; concepts maps a concept name to its units dict."""
    return {
        "cik": 1234,
        "entityName": "Test Co",
        "facts": {taxonomy: {name: {"label": name, "units": units} for name, units in concepts.items()}},
    }


class Clock:
    def __init__(self, now: float = 1_790_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def sec_for(routes: dict, *, clock: Clock | None = None, cache_dir: Path | None = None):
    session = FakeSession(routes)
    sleeps: list[float] = []
    sec = SecFundamentals(USER_AGENT, session=session, cache_dir=cache_dir, sleep=sleeps.append, clock=clock or Clock())
    return sec, session, sleeps


def sec_routes() -> dict:
    return {TICKERS_URL: load("sec_company_tickers.json"), AMD_FACTS: load("sec_companyfacts_small.json")}


# --- parse_company_facts on the synthetic fixture ------------------------------------------------------------------


def test_parse_company_facts_quarters_and_years():
    fundamentals = parse_company_facts("AMD", "2488", load("sec_companyfacts_small.json"))

    assert fundamentals.ticker == "AMD"
    assert fundamentals.entity == "ADVANCED MICRO DEVICES INC"
    assert fundamentals.cik == "0000002488"
    assert fundamentals.currency == "USD"
    assert fundamentals.quarters == [
        # The 10-Q/A filed after the 10-Q corrects revenue from 7,900 to 7,950: the later filing wins.
        # Cash flow is year-to-date in the filings: 2,400 (H1) - 1,100 (Q1) = 1,300.
        row("2026-06-27", 7950, 4000, 1000, 0.61, 1300),
        row("2026-03-28", 7500, 3800, 900, 0.55, 1100),
        # Q4 is never filed on its own: revenue 26,000 - (6,000 + 6,200 + 6,800) = 7,000; gross profit
        # 13,100 - 9,550 = 3,550; net income 2,750 - 1,950 = 800; cash flow 4,500 - 3,100 = 1,400; EPS isn't additive.
        row("2025-12-27", 7000, 3550, 800, None, 1400),
        row("2025-09-27", 6800, 3450, 700, 0.43, 1200),  # cash flow 3,100 - 1,900
        row("2025-06-28", 6200, 3100, 650, 0.40, 1000),  # cash flow 1,900 - 900
    ]
    assert fundamentals.annual == [
        row("2025-12-27", 26000, 13100, 2750, 1.70, 4500),
        row("2024-12-28", 22000, 11000, 1900, 1.18, 3600),
        # The old "Revenues" concept also has FY2023 (19,000), but the concept with current figures wins.
        row("2023-12-30", 20000, 10000, 1500, 0.93, 3000),
    ]


def test_parsed_fundamentals_render_as_text():
    text = parse_company_facts("AMD", "2488", load("sec_companyfacts_small.json")).as_text()

    assert "ADVANCED MICRO DEVICES INC (SEC CIK 0000002488)" in text
    assert "2026-06-27 | 7,950 (+28.2% y/y)" in text  # vs the 2025-06-28 quarter


def test_q4_is_derived_from_the_year_and_the_three_reported_quarters():
    doc = facts_doc(
        GrossProfit={
            "USD": [
                fact("2025-01-01", "2025-03-31", 100),
                fact("2025-04-01", "2025-06-30", 110),
                fact("2025-07-01", "2025-09-30", 120),
                fact("2025-01-01", "2025-12-31", 460, form="10-K"),
            ]
        },
        EarningsPerShareDiluted={
            "USD/shares": [fact("2025-07-01", "2025-09-30", 0.3), fact("2025-01-01", "2025-12-31", 1.2)]
        },
    )

    fundamentals = parse_company_facts("TEST", "1234", doc)

    assert [(q["period_end"], q["gross_profit"], q["eps_diluted"]) for q in fundamentals.quarters] == [
        ("2025-12-31", 130.0, None),
        ("2025-09-30", 120.0, 0.3),
        ("2025-06-30", 110.0, None),
        ("2025-03-31", 100.0, None),
    ]


def test_quarters_are_derived_from_year_to_date_totals_without_a_first_quarter():
    doc = facts_doc(
        NetCashProvidedByUsedInOperatingActivities={
            "USD": [
                fact("2025-01-01", "2025-06-30", 500),  # half year
                fact("2025-01-01", "2025-09-30", 800),  # nine months
                fact("2025-01-01", "2025-12-31", 1200),  # year
            ]
        }
    )

    quarters = parse_company_facts("TEST", "1234", doc).quarters

    assert [(q["period_end"], q["operating_cash_flow"]) for q in quarters] == [
        ("2025-12-31", 400.0),
        ("2025-09-30", 300.0),
    ]


def test_a_reported_quarter_is_never_replaced_by_a_derived_one():
    doc = facts_doc(
        Revenues={
            "USD": [
                fact("2025-01-01", "2025-09-30", 300),
                fact("2025-10-01", "2025-12-31", 90),  # Q4 reported (e.g. a transition period filing)
                fact("2025-01-01", "2025-12-31", 400),
            ]
        }
    )

    assert parse_company_facts("TEST", "1234", doc).quarters[0]["revenue"] == 90.0


def test_latest_filing_wins_and_odd_periods_and_bad_entries_are_ignored():
    doc = facts_doc(
        NetIncomeLoss={
            "USD": [
                fact("2025-04-01", "2025-06-30", 50, filed="2025-08-01"),
                fact("2025-04-01", "2025-06-30", 55, filed="2026-08-01"),  # restated in a later filing
                fact("2025-04-02", "2025-06-30", 54, filed="2025-09-01"),  # same end, older filing
                fact("2025-01-01", "2025-02-28", 10),  # two months: neither a quarter nor a year
                fact("2024-07-01", "2025-06-30", 200, filed="2025-09-01", form="10-KT"),  # a 12-month transition year
                {"end": "2025-06-30", "val": 7},  # an instant, no start
                {"start": "2025-04-01", "end": "not a date", "val": 1},
                {"start": "2025-04-01", "end": "2025-06-30", "val": "12"},
                "garbage",
            ]
        }
    )

    fundamentals = parse_company_facts("TEST", "1234", doc)

    assert [(q["period_end"], q["net_income"]) for q in fundamentals.quarters] == [("2025-06-30", 55.0)]
    assert [(y["period_end"], y["net_income"]) for y in fundamentals.annual] == [("2025-06-30", 200.0)]


def test_keeps_the_five_newest_quarters_and_three_newest_years():
    quarters = [
        fact(f"{year}-{m:02d}-01", f"{year}-{m + 2:02d}-28", year * 10 + m)
        for year in range(2020, 2026)
        for m in (1, 4, 7, 10)
    ]
    years = [fact(f"{year}-01-01", f"{year}-12-31", year) for year in range(2019, 2026)]
    doc = facts_doc(Revenues={"USD": quarters + years})

    fundamentals = parse_company_facts("TEST", "1234", doc)

    assert [q["period_end"] for q in fundamentals.quarters] == [
        "2025-12-28",
        "2025-09-28",
        "2025-06-28",
        "2025-03-28",
        "2024-12-28",
    ]
    assert [y["period_end"] for y in fundamentals.annual] == ["2025-12-31", "2024-12-31", "2023-12-31"]


def test_reporting_currency_is_the_one_most_facts_use():
    # A foreign filer using us-gaap in euros, with one convenience figure in dollars.
    doc = facts_doc(
        RevenueFromContractWithCustomerExcludingAssessedTax={
            "EUR": [fact("2024-01-01", "2024-12-31", 28000), fact("2025-01-01", "2025-12-31", 32000)],
            "USD": [fact("2025-01-01", "2025-12-31", 35000)],
        },
        EarningsPerShareDiluted={"EUR/shares": [fact("2025-01-01", "2025-12-31", 24.7)]},
    )

    fundamentals = parse_company_facts("ASML", "937966", doc)

    assert fundamentals.currency == "EUR"
    assert fundamentals.annual[0]["revenue"] == 32000.0
    assert fundamentals.annual[0]["eps_diluted"] == 24.7
    assert fundamentals.quarters == []


def test_ifrs_filers_are_read_from_ifrs_full():
    doc = facts_doc(
        "ifrs-full",
        Revenue={"EUR": [fact("2025-01-01", "2025-12-31", 36800)]},
        ProfitLossFromOperatingActivities={"EUR": [fact("2025-01-01", "2025-12-31", 9617)]},
        ProfitLossAttributableToOwnersOfParent={"EUR": [fact("2025-01-01", "2025-12-31", 7161)]},
        ProfitLoss={"EUR": [fact("2025-01-01", "2025-12-31", 7326)]},
        DilutedEarningsLossPerShare={"EUR/shares": [fact("2025-01-01", "2025-12-31", 6.1)]},
        CashFlowsFromUsedInOperatingActivities={"EUR": [fact("2025-01-01", "2025-12-31", 9156)]},
    )

    fundamentals = parse_company_facts("SAP", "1000184", doc)

    assert fundamentals.currency == "EUR"
    assert fundamentals.annual == [
        {
            "period_end": "2025-12-31",
            "revenue": 36800.0,
            "gross_profit": None,
            "operating_income": 9617.0,
            "net_income": 7161.0,  # attributable to shareholders, preferred over ProfitLoss
            "eps_diluted": 6.1,
            "operating_cash_flow": 9156.0,
        }
    ]


def test_proxy_statement_figures_never_replace_the_10k():
    """Regression (live VZ, GM, ORCL, JPM): the DEF 14A pay-versus-performance table, filed after the 10-K, tags
    rounded or differently based net income; "latest filing wins" let it replace the audited figure and the Q4
    derived from it."""
    doc = facts_doc(
        NetIncomeLoss={
            "USD": [
                fact("2025-01-01", "2025-03-31", 4000, filed="2025-04-25"),
                fact("2025-04-01", "2025-06-30", 5000, filed="2025-07-25"),
                fact("2025-07-01", "2025-09-30", 5832, filed="2025-10-25"),
                fact("2025-01-01", "2025-12-31", 17174, filed="2026-02-17", form="10-K"),
                fact("2025-01-01", "2025-12-31", 17608, filed="2026-04-06", form="DEF 14A"),
                fact("2024-01-01", "2024-12-31", 17949, filed="2026-04-06", form="DEF 14A"),  # only in the proxy
                fact("2024-01-01", "2024-12-31", 17506, filed="2025-02-10", form="10-K"),
            ]
        }
    )

    fundamentals = parse_company_facts("VZ", "732712", doc)

    assert [(y["period_end"], y["net_income"]) for y in fundamentals.annual] == [
        ("2025-12-31", 17174.0),
        ("2024-12-31", 17506.0),
    ]
    assert fundamentals.quarters[0] == {**fundamentals.quarters[0], "period_end": "2025-12-31", "net_income": 2342.0}


def test_a_filer_that_moved_to_ifrs_is_read_from_its_current_taxonomy():
    """Regression (live TM, SONY, HMC): old us-gaap facts from before the switch were shown as the newest figures."""
    doc = {
        "cik": 715153,
        "entityName": "HONDA MOTOR CO LTD",
        "facts": {
            "us-gaap": {
                "Revenues": {"units": {"JPY": [fact("2013-04-01", "2014-03-31", 11_842_451, form="20-F")]}},
                "NetIncomeLoss": {"units": {"JPY": [fact("2013-04-01", "2014-03-31", 574_107, form="20-F")]}},
            },
            "ifrs-full": {
                "Revenue": {"units": {"JPY": [fact("2024-04-01", "2025-03-31", 21_688_767, form="20-F")]}},
                "ProfitLossAttributableToOwnersOfParent": {
                    "units": {"JPY": [fact("2024-04-01", "2025-03-31", 835_837, form="20-F")]}
                },
            },
        },
    }

    fundamentals = parse_company_facts("HMC", "715153", doc)

    assert fundamentals.currency == "JPY"
    assert [(y["period_end"], y["revenue"], y["net_income"]) for y in fundamentals.annual] == [
        ("2025-03-31", 21_688_767.0, 835_837.0)
    ]

    # With equally recent figures us-gaap still wins.
    doc["facts"]["us-gaap"]["Revenues"]["units"]["JPY"].append(fact("2024-04-01", "2025-03-31", 1, form="20-F"))
    assert parse_company_facts("HMC", "715153", doc).annual[0]["revenue"] == 1.0


def test_net_income_falls_back_to_the_amount_available_to_common_stockholders():
    """Regression (live F, AMT, O): these filers stopped tagging NetIncomeLoss, so the newest quarters and year showed
    net income as n/a although the filings have it as NetIncomeLossAvailableToCommonStockholdersBasic."""
    doc = facts_doc(
        NetIncomeLoss={
            "USD": [
                fact("2025-01-01", "2025-03-31", 471, filed="2025-05-01"),
                fact("2025-04-01", "2025-06-30", -36, filed="2025-07-31"),
            ]
        },
        NetIncomeLossAvailableToCommonStockholdersBasic={
            "USD": [
                fact("2025-07-01", "2025-09-30", 2448, filed="2025-10-24"),
                fact("2025-01-01", "2025-12-31", -8182, filed="2026-02-11", form="10-K"),
                fact("2026-01-01", "2026-03-31", 2548, filed="2026-04-30"),
            ]
        },
    )

    fundamentals = parse_company_facts("F", "37996", doc)

    assert [(q["period_end"], q["net_income"]) for q in fundamentals.quarters] == [
        ("2026-03-31", 2548.0),
        ("2025-12-31", -8182.0 - (471 - 36 + 2448)),  # Q4 derived from the year
        ("2025-09-30", 2448.0),
        ("2025-06-30", -36.0),
        ("2025-03-31", 471.0),
    ]
    assert [(y["period_end"], y["net_income"]) for y in fundamentals.annual] == [("2025-12-31", -8182.0)]

    # A company tagging both for the same periods keeps NetIncomeLoss (before preferred dividends).
    both = facts_doc(
        NetIncomeLoss={"USD": [fact("2026-01-01", "2026-03-31", 100)]},
        NetIncomeLossAvailableToCommonStockholdersBasic={"USD": [fact("2026-01-01", "2026-03-31", 90)]},
    )
    assert parse_company_facts("T", "1", both).quarters[0]["net_income"] == 100.0


def test_old_fundamentals_say_how_old_they_are():
    fundamentals = parse_company_facts(
        "OLD", "1", facts_doc(Revenues={"USD": [fact("2013-04-01", "2014-03-31", 100, form="10-K")]})
    )
    text = fundamentals.as_text(today=date(2026, 9, 27))
    assert "Note: the newest figures are for the period ending 2014-03-31, about 150 months ago" in text
    assert "Note:" not in fundamentals.as_text(today=date(2015, 6, 30))  # 15 months: a normal 20-F lag
    assert "Note:" not in fundamentals.as_text()


def test_a_16_week_fourth_quarter_is_derived():
    """Regression (live COST, PEP): 12/12/12/16-week calendars; the 112-day Q4 was neither shown nor derived."""
    doc = facts_doc(
        Revenues={
            "USD": [
                fact("2024-09-02", "2024-11-24", 62_151),  # 83 days each
                fact("2024-11-25", "2025-02-16", 63_723),
                fact("2025-02-17", "2025-05-11", 63_205),
                fact("2024-09-02", "2025-05-11", 189_079),  # nine months, 251 days
                fact("2024-09-02", "2025-08-31", 275_235, form="10-K"),  # 363 days
            ]
        }
    )

    quarters = parse_company_facts("COST", "909832", doc).quarters

    assert quarters[0]["period_end"] == "2025-08-31"
    assert quarters[0]["revenue"] == 275_235 - 189_079


def test_a_16_week_first_quarter_is_kept():
    """Regression (live KR): the directly reported 111-day Q1 was treated as a year-to-date total and dropped."""
    doc = facts_doc(
        Revenues={
            "USD": [
                fact("2026-02-01", "2026-05-23", 46_121),  # 16 weeks
                fact("2026-05-24", "2026-08-15", 33_900),  # 12 weeks
                fact("2026-02-01", "2026-08-15", 80_021),  # half year, 195 days
            ]
        }
    )

    quarters = parse_company_facts("KR", "56873", doc).quarters

    assert [(q["period_end"], q["revenue"]) for q in quarters] == [("2026-08-15", 33_900.0), ("2026-05-23", 46_121.0)]


def test_a_company_without_figures_parses_to_empty_lists():
    fundamentals = parse_company_facts("SPAC", "999", {"cik": 999, "entityName": "Blank Check Corp", "facts": {}})

    assert (fundamentals.entity, fundamentals.cik, fundamentals.currency) == ("Blank Check Corp", "0000000999", "USD")
    assert fundamentals.quarters == fundamentals.annual == []


def test_parse_company_tickers():
    assert parse_company_tickers(load("sec_company_tickers.json")) == {
        "AMD": "0000002488",
        "BRK-B": "0001067983",
        "SAP": "0001000184",
    }
    with pytest.raises(SecError):
        parse_company_tickers({"error": "nope"})


# --- SecFundamentals -----------------------------------------------------------------------------------------------


def test_needs_a_user_agent():
    with pytest.raises(ConfigError, match="SEC_USER_AGENT"):
        SecFundamentals("  ")


def test_cik_accepts_class_share_spellings_and_downloads_the_list_once():
    sec, session, _ = sec_for(sec_routes())

    assert sec.cik("AMD") == "0000002488"
    assert sec.cik("brk.b") == "0001067983"
    assert sec.cik("BRK-B") == "0001067983"
    assert sec.cik("$BRK.B") == "0001067983"
    assert sec.cik("SAP.DE") is None  # SAP (the US listing) is there, the Xetra line isn't
    assert sec.cik("OPAP.AT") is None
    assert session.urls == [TICKERS_URL]
    assert session.calls[0]["headers"]["User-Agent"] == USER_AGENT


def test_get_returns_fundamentals_and_remembers_them():
    sec, session, _ = sec_for(sec_routes())

    fundamentals = sec.get("amd")

    assert fundamentals is not None
    assert fundamentals.ticker == "AMD"
    assert fundamentals.quarters[0]["revenue"] == 7950 * M
    assert session.urls == [TICKERS_URL, AMD_FACTS]
    assert all(call["headers"]["User-Agent"] == USER_AGENT for call in session.calls)
    assert sec.get("AMD") == fundamentals
    assert len(session.calls) == 2


def test_get_is_none_for_tickers_the_sec_does_not_list():
    sec, session, _ = sec_for(sec_routes())

    assert sec.get("SAP.DE") is None
    assert sec.get("7203.T") is None
    assert session.urls == [TICKERS_URL]


def test_get_is_none_when_the_company_has_no_facts(caplog):
    routes = sec_routes()
    routes[AMD_FACTS] = FakeResponse(status_code=404, content=b"<Error><Code>NoSuchKey</Code></Error>")
    sec, session, _ = sec_for(routes)

    with caplog.at_level(logging.INFO, logger="dip_scanner.fundamentals"):
        assert sec.get("AMD") is None
        assert sec.get("AMD") is None  # remembered: no second download
    assert session.urls == [TICKERS_URL, AMD_FACTS]
    assert "no usable income statement figures" in caplog.text


@pytest.mark.parametrize(
    "failure",
    [requests.ConnectionError("no route to host"), 403, 429, 500, FakeResponse(content=b"<html>maintenance</html>")],
)
def test_get_never_raises_on_network_trouble(failure, caplog):
    sec, _, _ = sec_for({TICKERS_URL: failure})

    with caplog.at_level(logging.WARNING, logger="dip_scanner.fundamentals"):
        assert sec.get("AMD") is None
    assert "Couldn't get SEC fundamentals for AMD" in caplog.text
    if failure in (403, 429):
        assert "SEC_USER_AGENT" in caplog.text


def test_get_never_raises_on_unexpected_documents():
    routes = sec_routes()
    routes[AMD_FACTS] = {"facts": "not a dict"}
    sec, _, _ = sec_for(routes)
    assert sec.get("AMD") is None

    routes[AMD_FACTS] = {"facts": {"us-gaap": {"Revenues": {"units": {"USD": "nope"}}}}}
    sec, _, _ = sec_for(routes)
    assert sec.get("AMD") is None


def test_requests_are_spaced_out():
    clock = Clock()
    sec, _, sleeps = sec_for(sec_routes(), clock=clock)

    sec.get("AMD")  # two requests at the same instant

    assert sleeps == [pytest.approx(MIN_INTERVAL)]

    clock.now += FACTS_TTL + 1  # much later: no wait needed
    sec.get("AMD")
    assert len(sleeps) == 1


def test_disk_cache_is_reused_by_a_new_instance(tmp_path):
    clock = Clock()
    sec, session, _ = sec_for(sec_routes(), clock=clock, cache_dir=tmp_path)
    first = sec.get("AMD")
    assert sorted(path.name for path in (tmp_path / "sec").iterdir()) == ["CIK0000002488.json", "company_tickers.json"]

    clock.now += FACTS_TTL - 60
    sec, session, _ = sec_for({}, clock=clock, cache_dir=tmp_path)  # every URL would 404
    assert sec.get("AMD") == first
    assert session.calls == []

    clock.now += 120  # the figures are now older than 12 hours, the ticker list isn't a day old
    sec, session, _ = sec_for(sec_routes(), clock=clock, cache_dir=tmp_path)
    assert sec.get("AMD") == first
    assert session.urls == [AMD_FACTS]

    clock.now += TICKERS_TTL
    sec, session, _ = sec_for(sec_routes(), clock=clock, cache_dir=tmp_path)
    sec.get("AMD")
    assert session.urls == [TICKERS_URL, AMD_FACTS]


def test_stale_cache_is_used_when_the_sec_is_unreachable(tmp_path, caplog):
    clock = Clock()
    sec, _, _ = sec_for(sec_routes(), clock=clock, cache_dir=tmp_path)
    first = sec.get("AMD")

    clock.now += 3 * TICKERS_TTL
    sec, session, _ = sec_for({"https://": requests.ConnectionError("offline")}, clock=clock, cache_dir=tmp_path)
    with caplog.at_level(logging.WARNING, logger="dip_scanner.fundamentals"):
        assert sec.get("AMD") == first
    assert session.urls == [TICKERS_URL, AMD_FACTS]
    assert "cached at" in caplog.text


def test_broken_cache_files_are_ignored(tmp_path):
    (tmp_path / "sec").mkdir()
    (tmp_path / "sec" / "company_tickers.json").write_text("{not json")
    (tmp_path / "sec" / "CIK0000002488.json").write_text(json.dumps({"fetched": Clock().now, "fundamentals": {"x": 1}}))
    sec, session, _ = sec_for(sec_routes(), cache_dir=tmp_path)

    assert sec.get("AMD") is not None
    assert session.urls == [TICKERS_URL, AMD_FACTS]


def test_the_same_company_under_another_ticker_is_not_downloaded_twice():
    routes = sec_routes()
    routes[TICKERS_URL] = {
        "0": {"cik_str": 1067983, "ticker": "BRK-B", "title": "BERKSHIRE HATHAWAY INC"},
        "1": {"cik_str": 1067983, "ticker": "BRK-A", "title": "BERKSHIRE HATHAWAY INC"},
    }
    routes[FACTS_URL.format(cik="0001067983")] = load("sec_companyfacts_small.json")
    sec, session, _ = sec_for(routes)

    assert sec.get("BRK-A").ticker == "BRK-A"
    assert sec.get("BRK.B").ticker == "BRK.B"
    assert len(session.calls) == 2
