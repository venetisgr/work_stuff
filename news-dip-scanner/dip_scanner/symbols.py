"""Find the current Yahoo Finance symbol of a company whose symbol has no prices (renamed, or mistyped by triage).

A triage model knows the symbols of its training data: OPAP.AT for what is now Allwyn AG (ALWN.AT), MYTIL.AT for
Metlen (MTLN.AT). Yahoo answers "no data" for those, and without help the story would only end up in a skip note.
Yahoo's search endpoint (https://query2.finance.yahoo.com/v1/finance/search?q=<name>&quotesCount=6&newsCount=0) finds
listings by company name: its quotes[] entries carry symbol, exchange, quoteType, shortname and longname.

resolve() searches for the company names the triage gave and accepts a result only when it is a company's shares
(quoteType EQUITY), on the same exchange as the bad symbol (the same Yahoo suffix, or a main US exchange when there
is none) and its name matches the one searched for (see similar_names). Every lookup is kept in the store for
LOOKUP_TTL (7 days), found or not, so a symbol is looked up once a week per company name at most.

It only works with the current name: Yahoo's search no longer knows "OPAP" or "Mytilineos", so a story the model
filed as "OPAP" with OPAP.AT stays unresolved, while "Allwyn" with OPAP.AT becomes ALWN.AT.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any

import requests

from .feeds import company_core, strip_legal_forms
from .models import utc
from .prices import BROWSER_USER_AGENT

if TYPE_CHECKING:
    from .store import Store

log = logging.getLogger(__name__)

SEARCH_URLS = (
    "https://query2.finance.yahoo.com/v1/finance/search",
    "https://query1.finance.yahoo.com/v1/finance/search",
)
_HEADERS = {"User-Agent": BROWSER_USER_AGENT, "Accept": "application/json"}
QUOTES_COUNT = 6
MAX_QUERIES = 3  # company names searched per symbol and cycle (the triage can name a company several ways)
# Yahoo's exchange codes of the main US markets (Nasdaq GS/GM/CM, NYSE, NYSE American, NYSE Arca, Cboe BZX). A
# symbol without a suffix is only replaced by one of these, never by an OTC line (PNK, OQB...) or a fund.
US_EXCHANGES = frozenset({"NMS", "NGM", "NCM", "NAS", "NYQ", "NYS", "ASE", "PCX", "BTS"})
# Words that say which name is the old one: "Allwyn (formerly OPAP)", "Metlen / Mytilineos", "Metlen (ex-Mytilineos)".
_NAME_PARTS = re.compile(r"[()/;]|\b(?:formerly|previously|ex)\b", re.IGNORECASE)
_ACRONYM_SKIP = frozenset({"of", "and", "the", "de", "des", "du", "la", "le", "di", "del"})
_SIMILARITY = 0.85


class SymbolSearchError(Exception):
    """Yahoo's search couldn't be reached or gave no usable answer. Try again later."""


@dataclass(frozen=True)
class Resolution:
    """The symbol found for a company whose symbol had no prices."""

    symbol: str  # e.g. ALWN.AT
    name: str  # Yahoo's name for the listing, e.g. Allwyn AG
    query: str  # the company name that found it


class SymbolResolver:
    """Looks up replacement symbols with Yahoo's search and remembers every answer in the store for LOOKUP_TTL."""

    def __init__(self, session, store: Store, *, timeout: float = 10) -> None:
        self.session = session if session is not None else requests
        self.store = store
        self.timeout = timeout

    def known(self, ticker: str, *, now: datetime) -> Resolution | None:
        """The replacement found for ticker in the last 7 days, from the store only (no request)."""
        found = self.store.resolved_symbol(_symbol(ticker), now=utc(now))
        return Resolution(*found) if found is not None else None

    def resolve(self, ticker: str, companies: Sequence[str], *, now: datetime) -> Resolution | None:
        """The listing Yahoo's search finds for one of the company names, on ticker's exchange, or None.

        Names are tried in the order given (the most used first), each also split into its parts ("Allwyn (formerly
        OPAP)" is searched as "Allwyn" and "OPAP"), at most MAX_QUERIES searches; lookups from the last 7 days are
        answered from the store. Raises SymbolSearchError when no search got an answer and nothing was found (the
        next attempt asks again: failures aren't remembered).
        """
        now = utc(now)
        symbol = _symbol(ticker)
        failure: SymbolSearchError | None = None
        for query in search_queries(companies, symbol)[:MAX_QUERIES]:
            cached = self.store.symbol_lookup(symbol, query, now=now)
            if cached is not None:
                resolved, name = cached
            else:
                try:
                    quotes = self._search(query)
                except SymbolSearchError as exc:
                    log.warning("Couldn't search Yahoo Finance for %r (%s has no prices): %s", query, symbol, exc)
                    failure = exc
                    continue
                match = best_match(quotes, query, symbol)
                resolved, name = (match.symbol, match.name) if match is not None else (None, None)
                self.store.save_symbol_lookup(symbol, query, resolved, name, checked=now)
                log.info(
                    "Yahoo search for %r (%s has no prices): %s",
                    query,
                    symbol,
                    f"{resolved} ({name})" if resolved else "no listing on the same exchange with that name",
                )
            if resolved:
                return Resolution(symbol=resolved, name=name or resolved, query=query)
        if failure is not None:
            raise failure
        return None

    def _search(self, query: str) -> list[dict]:
        """quotes[] of Yahoo's search for query: the second host is asked when the first fails."""
        problems: list[str] = []
        params = {"q": query, "quotesCount": QUOTES_COUNT, "newsCount": 0}
        for url in SEARCH_URLS:
            try:
                response = self.session.get(url, params=params, headers=_HEADERS, timeout=self.timeout)
            except requests.RequestException as exc:
                problems.append(f"{type(exc).__name__}: {exc}")
                continue
            if response.status_code != 200:
                problems.append(f"HTTP {response.status_code}")
                continue
            try:
                data = response.json()
            except ValueError:
                problems.append("the answer isn't JSON")
                continue
            quotes = data.get("quotes") if isinstance(data, dict) else None
            return [quote for quote in quotes if isinstance(quote, dict)] if isinstance(quotes, list) else []
        raise SymbolSearchError("; ".join(problems))


def search_queries(companies: Sequence[str], ticker: str) -> list[str]:
    """The distinct names to search for, in order: each company name without legal forms, split into its parts
    ("Allwyn (formerly OPAP)" -> "Allwyn", "OPAP"). Names that are just the symbol ("OPAP.AT") are left out. "&"
    stays: Yahoo finds "Metlen Energy & Metals" but not "Metlen Energy and Metals"."""
    queries: dict[str, str] = {}
    for company in companies:
        if not isinstance(company, str) or company.strip().upper() == ticker:
            continue
        for part in _NAME_PARTS.split(company):
            query = strip_legal_forms(part.strip(" ,;:-&")).strip(" ,;:-&")
            if query and query.casefold() not in queries:
                queries[query.casefold()] = query
    return list(queries.values())


def best_match(quotes: Sequence[dict[str, Any]], query: str, ticker: str) -> Resolution | None:
    """The first of Yahoo's quotes (they come best first) that can replace ticker: company shares, not ticker itself,
    on the same exchange (the same suffix; without one, a main US exchange), with a name like query."""
    suffix = _suffix(ticker)
    for quote in quotes:
        symbol = str(quote.get("symbol") or "").strip().upper()
        if not symbol or symbol == ticker or str(quote.get("quoteType") or "").upper() != "EQUITY":
            continue
        if _suffix(symbol) != suffix or (not suffix and str(quote.get("exchange") or "").upper() not in US_EXCHANGES):
            continue
        names = [" ".join(name.split()) for name in (quote.get("longname"), quote.get("shortname")) if _text(name)]
        if any(similar_names(query, name) for name in names):
            return Resolution(symbol=symbol, name=names[0], query=query)
    return None


def similar_names(a: str, b: str) -> bool:
    """Whether two company names are the same company, loosely: without legal forms, the shorter name's words all
    appear in the longer one and both start with the same word ("Eurobank" and "Eurobank Ergasias Services and
    Holdings S.A."), or the shorter is a single word of at least 3 letters made of the initials of the other's first
    words ("PPC" and "Public Power Corporation S.A.", "NBG" and "National Bank of Greece"), or they are spelled almost
    alike ("Metlen Energy & Metals" and "METLEN ENERGY & METALS PLC")."""
    words_a = company_core(a).casefold().split()
    words_b = company_core(b).casefold().split()
    if not words_a or not words_b:
        return False
    (short, _), (long, long_name) = sorted(((words_a, a), (words_b, b)), key=lambda pair: len(pair[0]))
    if short[0] == long[0] and set(short) <= set(long):
        return True
    if len(short) == 1 and len(short[0]) >= 3 and short[0] in _initials(long_name):
        return True
    return SequenceMatcher(None, " ".join(words_a), " ".join(words_b)).ratio() >= _SIMILARITY


def _initials(name: str) -> set[str]:
    """The initials of the first 2, 3... words of a name, legal forms included ("Public Power Corporation S.A." ->
    pp, ppc, ppcs, ppcsa), leaving out "of", "and" and the like."""
    words = [word for word in re.sub(r"[^\w\s]", " ", name).casefold().split() if word not in _ACRONYM_SKIP]
    return {"".join(word[0] for word in words[:count]) for count in range(2, len(words) + 1)}


def _symbol(ticker: str) -> str:
    return ticker.strip().upper()


def _suffix(symbol: str) -> str:
    _, dot, suffix = symbol.rpartition(".")
    return f".{suffix}" if dot else ""


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())
