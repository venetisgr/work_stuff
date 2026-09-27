"""Tests for finding the current symbol of a renamed company with Yahoo's search (fake HTTP, in-memory store)."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
import requests
from conftest import NOW, FakeResponse, FakeSession

from dip_scanner.store import Store
from dip_scanner.symbols import (
    MAX_QUERIES,
    SEARCH_URLS,
    Resolution,
    SymbolResolver,
    SymbolSearchError,
    best_match,
    search_queries,
    similar_names,
)

FIXTURES = Path(__file__).parent / "fixtures"
# Yahoo's answer to q=Allwyn on 2026-09-27: ALWN.AT (Athens) first, then the OTC ADR and German listings.
ALLWYN = json.loads((FIXTURES / "yahoo_search_allwyn.json").read_text(encoding="utf-8"))
QUERY2, QUERY1 = SEARCH_URLS


@pytest.fixture
def store():
    with Store(":memory:") as db:
        yield db


def quote(symbol: str, name: str, *, exchange: str = "ATH", kind: str = "EQUITY", shortname: str | None = None):
    return {"symbol": symbol, "exchange": exchange, "quoteType": kind, "longname": name, "shortname": shortname or name}


def searched(session: FakeSession) -> list[str]:
    return [call["params"]["q"] for call in session.calls]


# --- resolve -------------------------------------------------------------------------------------------------------


def test_resolve_finds_the_renamed_listing_on_the_same_exchange_and_remembers_it(store):
    """Regression (live): OPAP.AT has no prices since OPAP became Allwyn AG (ALWN.AT); a triage model trained on
    older data still writes OPAP.AT, and the story was lost."""
    session = FakeSession({QUERY2: ALLWYN})
    resolver = SymbolResolver(session, store)

    found = resolver.resolve("OPAP.AT", ["Allwyn", "Allwyn AG"], now=NOW)

    assert found == Resolution(symbol="ALWN.AT", name="Allwyn AG", query="Allwyn")
    [call] = session.calls  # "Allwyn AG" is the same search
    assert call["url"] == QUERY2 and call["params"] == {"q": "Allwyn", "quotesCount": 6, "newsCount": 0}
    assert call["headers"]["User-Agent"].startswith("Mozilla/5.0")
    # Answered from the store for 7 days, also without a company name (known).
    assert resolver.resolve("opap.at", ["Allwyn"], now=NOW + timedelta(days=6)) == found
    assert resolver.known("OPAP.AT", now=NOW + timedelta(days=6)) == found
    assert len(session.calls) == 1
    later = NOW + timedelta(days=7, minutes=1)
    assert resolver.known("OPAP.AT", now=later) is None
    assert resolver.resolve("OPAP.AT", ["Allwyn"], now=later) == found
    assert len(session.calls) == 2


def test_resolve_only_accepts_company_shares_on_the_same_exchange_with_a_similar_name(store):
    session = FakeSession({QUERY2: ALLWYN})
    resolver = SymbolResolver(session, store)

    # No suffix means a main US exchange: the OTC ADR (GOFPY on PNK) and the German lines don't count.
    assert resolver.resolve("OPAPY", ["Allwyn"], now=NOW) is None
    # Frankfurt has a line of its own.
    assert resolver.resolve("OPAP.F", ["Allwyn"], now=NOW).symbol == "GF8.F"
    # Nothing on Xetra.
    assert resolver.resolve("OPAP.DE", ["Allwyn"], now=NOW) is None
    # Not found is remembered as well: no new request within 7 days.
    assert resolver.resolve("OPAPY", ["Allwyn"], now=NOW + timedelta(days=1)) is None
    assert len(session.calls) == 3


def test_resolve_leaves_out_names_that_are_the_symbol_and_stops_after_max_queries(store):
    session = FakeSession({QUERY2: {"quotes": []}})
    resolver = SymbolResolver(session, store)
    names = ["OPAP.AT", "Gaming Co One", "Gaming Co Two", "Gaming Co Three", "Gaming Co Four"]

    assert resolver.resolve("OPAP.AT", names, now=NOW) is None
    assert searched(session) == ["Gaming Co One", "Gaming Co Two", "Gaming Co Three"][:MAX_QUERIES]
    assert resolver.resolve("OPAP.AT", [], now=NOW) is None and len(session.calls) == MAX_QUERIES


def test_resolve_tries_the_second_host_and_the_next_name_when_a_search_fails(store):
    def route(method, url, call):
        return requests.ConnectionError("down") if call["params"]["q"] == "Broken" else ALLWYN

    session = FakeSession({QUERY2: [500, ALLWYN], QUERY1: route})
    resolver = SymbolResolver(session, store)

    assert resolver.resolve("OPAP.AT", ["Allwyn"], now=NOW).symbol == "ALWN.AT"
    assert [call["url"] for call in session.calls] == [QUERY2, QUERY1]

    session = FakeSession({QUERY2: route, QUERY1: route})
    resolver = SymbolResolver(session, Store(":memory:"))
    assert resolver.resolve("OPAP.AT", ["Broken", "Allwyn"], now=NOW).symbol == "ALWN.AT"
    assert searched(session) == ["Broken", "Broken", "Allwyn"]


@pytest.mark.parametrize(
    "route",
    [
        requests.ConnectionError("no network"),
        429,
        FakeResponse(content=b"<html>rate limited</html>", headers={"Content-Type": "text/html"}),
    ],
)
def test_resolve_raises_when_no_search_got_an_answer_and_remembers_nothing(store, route):
    session = FakeSession({QUERY2: route, QUERY1: route})
    resolver = SymbolResolver(session, store)

    with pytest.raises(SymbolSearchError):
        resolver.resolve("OPAP.AT", ["Allwyn"], now=NOW)
    assert store.symbol_lookup("OPAP.AT", "Allwyn", now=NOW) is None  # asked again next time
    assert len(session.calls) == 2


def test_an_answer_without_quotes_is_no_match(store):
    for answer in ({"count": 0}, {"quotes": "none"}, {"quotes": [None, 3]}, FakeResponse(json_data=[])):
        session = FakeSession({QUERY2: answer})
        assert SymbolResolver(session, Store(":memory:")).resolve("OPAP.AT", ["Allwyn"], now=NOW) is None


# --- the parts -----------------------------------------------------------------------------------------------------


def test_search_queries_split_names_and_drop_legal_forms():
    names = [
        "Allwyn (formerly OPAP)",
        "Allwyn AG",
        "Metlen Energy & Metals PLC",  # "&" stays: Yahoo doesn't find "Metlen Energy and Metals"
        "Metlen (ex-Mytilineos)",
        "Piraeus Bank / Piraeus Financial Holdings",
        "OPAP.AT",
        "  ",
    ]
    assert search_queries(names, "OPAP.AT") == [
        "Allwyn",
        "OPAP",
        "Metlen Energy & Metals",
        "Metlen",
        "Mytilineos",
        "Piraeus Bank",
        "Piraeus Financial",
    ]


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("Allwyn", "Allwyn AG", True),
        ("National Bank of Greece", "National Bank of Greece S.A.", True),
        ("Eurobank Ergasias", "Eurobank S.A.", True),
        ("Motor Oil", "Motor Oil (Hellas) Corinth Refineries S.A.", True),
        ("OTE", "OTE S.A.", True),
        ("NBG", "National Bank of Greece S.A.", True),  # initials
        ("PPC", "Public Power Corporation S.A.", True),
        ("Metlen Energy & Metals", "METLEN ENERGY & METALS PLC", True),
        # Spelled almost alike.
        ("Hellenic Telecommunications Organisation", "Hellenic Telecommunication Organization SA", True),
        ("OPAP", "Allwyn AG", False),
        ("Bank", "Piraeus Bank S.A.", False),  # a shared word isn't enough
        ("AB", "Alpha Bank", False),  # initials need 3 letters
        ("Jumbo", "Mumbo Jumbo Holdings", False),
        ("", "Allwyn AG", False),
    ],
)
def test_similar_names(a, b, same):
    assert similar_names(a, b) is same
    assert similar_names(b, a) is same


def test_best_match_skips_the_bad_symbol_funds_other_exchanges_and_other_names():
    quotes = [
        quote("OPAP.AT", "OPAP S.A."),  # the symbol that has no prices
        quote("ALWNX.AT", "Allwyn Gaming ETF", kind="ETF"),
        quote("GF8.F", "Allwyn AG", exchange="FRA"),
        quote("BYLOT.AT", "Bally's Intralot S.A."),  # the same exchange, but another company
        quote("ALWN.AT", "Allwyn AG", shortname="ALLWYN AG     I"),
        quote("ALWB.AT", "Allwyn Bond Holdings"),  # a similar name further down Yahoo's list
    ]
    assert best_match(quotes, "Allwyn AG", "OPAP.AT") == Resolution("ALWN.AT", "Allwyn AG", "Allwyn AG")
    assert best_match(quotes[:4], "Allwyn AG", "OPAP.AT") is None
    us = [quote("META", "Meta Platforms, Inc.", exchange="NMS"), quote("FBOK", "Meta Platforms", exchange="PNK")]
    assert best_match(us, "Meta Platforms", "FB").symbol == "META"
    assert best_match(us[1:], "Meta Platforms", "FB") is None  # OTC


# --- the store -----------------------------------------------------------------------------------------------------


def test_the_store_keeps_symbol_lookups_for_seven_days_and_prune_drops_old_ones(store):
    store.save_symbol_lookup("opap.at", "OPAP", None, None, checked=NOW - timedelta(days=2))
    store.save_symbol_lookup("OPAP.AT", "Allwyn", "alwn.at", "Allwyn AG", checked=NOW - timedelta(days=1))
    store.save_symbol_lookup("MYTIL.AT", "Metlen", "MTLN.AT", "Metlen Energy & Metals PLC", checked=NOW - timedelta(8))

    assert store.symbol_lookup("OPAP.AT", "OPAP", now=NOW) == (None, None)
    assert store.symbol_lookup("OPAP.AT", "Allwyn", now=NOW) == ("ALWN.AT", "Allwyn AG")
    assert store.symbol_lookup("OPAP.AT", "Jumbo", now=NOW) is None
    assert store.resolved_symbol("OPAP.AT", now=NOW) == ("ALWN.AT", "Allwyn AG", "Allwyn")
    assert store.resolved_symbol("MYTIL.AT", now=NOW) is None  # older than 7 days
    assert store.resolved_symbol("MYTIL.AT", now=NOW - timedelta(days=2)) is not None

    store.prune(older_than=NOW - timedelta(days=30))
    assert store.resolved_symbol("MYTIL.AT", now=NOW - timedelta(days=2)) is not None
    store.prune(older_than=NOW - timedelta(days=3))
    assert store.resolved_symbol("MYTIL.AT", now=NOW - timedelta(days=2)) is None
    assert store.resolved_symbol("OPAP.AT", now=NOW) is not None
