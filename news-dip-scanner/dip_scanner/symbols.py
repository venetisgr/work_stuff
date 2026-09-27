"""Find the current Yahoo Finance symbol of a company whose symbol has no prices (renamed, or mistyped by triage).

A triage model knows the symbols of its training data: OPAP.AT for what is now Allwyn AG (ALWN.AT), MYTIL.AT for
Metlen (MTLN.AT). Yahoo answers "no data" for those, and without help the story would only end up in a skip note.
Yahoo's search endpoint (https://query2.finance.yahoo.com/v1/finance/search?q=<name>&quotesCount=6&newsCount=0) finds
listings by company name: its quotes[] entries carry symbol, exchange, quoteType, shortname and longname.

resolve() searches for the company names the triage gave and accepts a result only when it is a company's shares
(quoteType EQUITY, and no fund, note, partnership or warrant by its name), on the same exchange as the bad symbol (the
same Yahoo suffix, or a main US exchange when there is none), and the same company (see best_match): the same name
or its initials, or a longer name that starts with it when no other company on Yahoo's list starts the same way and
a story names it. Acquired or delisted companies are the usual trap: Yahoo's search for "Hess" lists Hess Midstream
LP, and for "Credit Suisse" a bond fund whose old short name still says Credit Suisse; neither is taken. Every lookup
is kept in the store for LOOKUP_TTL (7 days), found or not, so a symbol is looked up once a week per company name at
most.

It only works with the current name: Yahoo's search no longer knows "OPAP" or "Mytilineos", so a story the model
filed as "OPAP" with OPAP.AT stays unresolved, while "Allwyn" with OPAP.AT becomes ALWN.AT.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
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
# Words in a listing's name that say it is a fund, whatever it is typed as (Yahoo types some closed-end funds and
# ETFs as EQUITY: DHY "UBS Asset Management High Yield Credit Fund").
FUND_WORDS = frozenset({"fund", "funds", "etf", "etfs", "etn", "etp", "ucits", "sicav"})
# ...and words that make a listing something other than a company's plain shares, unless the name searched for has
# them too: "Credit Suisse High Yield Bond F" (DHY's old short name), "Hess Midstream LP", "Bed Bath & Beyond, Inc. WT".
_NOT_SHARES = FUND_WORDS | frozenset(
    {
        "trust", "note", "notes", "bond", "bonds", "income", "yield", "portfolio", "index", "lp", "partners",
        "partnership", "warrant", "warrants", "wt", "ws", "unit", "units", "rights",
    }
)  # fmt: skip
# The padding and line marker at the end of Yahoo's short names of German listings: "SIEMENS AG                    N".
_LINE_MARK = re.compile(r"\s{2,}\S{1,3}\s*$")


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

    def resolve(
        self, ticker: str, companies: Sequence[str], *, now: datetime, texts: Sequence[str] = ()
    ) -> Resolution | None:
        """The listing Yahoo's search finds for one of the company names, on ticker's exchange, or None.

        Names are tried in the order given (the most used first), each also split into its parts ("Allwyn (formerly
        OPAP)" is searched as "Allwyn" and "OPAP"), at most MAX_QUERIES searches; lookups from the last 7 days are
        answered from the store. texts are the stories filed under ticker (headline and summary): a listing found by
        a longer name ("Metlen" -> "Metlen Energy & Metals PLC") is only taken once one of them names it (see
        best_match), and from then on it counts like any other. Raises SymbolSearchError when no search got an
        answer and nothing was found (the next attempt asks again: failures aren't remembered).
        """
        now = utc(now)
        symbol = _symbol(ticker)
        failure: SymbolSearchError | None = None
        for query in search_queries(companies, symbol)[:MAX_QUERIES]:
            cached = self.store.symbol_lookup(symbol, query, now=now)
            if cached is not None:
                resolved, name, longer = cached
                found = Resolution(symbol=resolved, name=name or resolved, query=query) if resolved else None
            else:
                try:
                    quotes = self._search(query)
                except SymbolSearchError as exc:
                    log.warning("Couldn't search Yahoo Finance for %r (%s has no prices): %s", query, symbol, exc)
                    failure = exc
                    continue
                found, longer = candidate(quotes, query, symbol)
                self.store.save_symbol_lookup(
                    symbol,
                    query,
                    found.symbol if found else None,
                    found.name if found else None,
                    checked=now,
                    unconfirmed=longer,
                )
                log.info(
                    "Yahoo search for %r (%s has no prices): %s",
                    query,
                    symbol,
                    f"{found.symbol} ({found.name})" if found else "no listing of that company on the same exchange",
                )
            if found is None:
                continue
            if longer:
                if not named_in(found.name, texts):
                    log.info(
                        "%s (%s) starts with %r, but no story filed under %s names it: not taken.",
                        found.symbol,
                        found.name,
                        query,
                        symbol,
                    )
                    continue
                self.store.confirm_symbol_lookup(symbol, query)
            return found
        if failure is not None:
            raise failure
        return None

    def _search(self, query: str) -> list[dict]:
        """quotes[] of Yahoo's search for query: the second host is asked when the first fails. Yahoo refuses some
        queries for good ("Invalid Search Query", HTTP 400, e.g. a name in Greek letters only): no listings."""
        problems: list[str] = []
        params = {"q": query, "quotesCount": QUOTES_COUNT, "newsCount": 0}
        for url in SEARCH_URLS:
            try:
                response = self.session.get(url, params=params, headers=_HEADERS, timeout=self.timeout)
            except requests.RequestException as exc:
                problems.append(f"{type(exc).__name__}: {exc}")
                continue
            if response.status_code == 400:
                log.debug("Yahoo's search refuses the query %r (HTTP 400).", query)
                return []
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


def symbol_aliases(store: Store, symbol: str, preferred: Mapping[str, str], *, now: datetime) -> set[str]:
    """symbol and the tickers whose news belongs to it: [universe] preferred_listings keys that point at it ("ASML"
    for "ASML.AS"), and old symbols whose replacement found in the last 7 days is it ("OPAP.AT" for "ALWN.AT")."""
    symbol = _symbol(symbol)
    names = {symbol} | {old for old, new in preferred.items() if new == symbol}
    return names | set(store.renamed_to(symbol, now=utc(now)))


def current_symbol(store: Store, ticker: str, preferred: Mapping[str, str], *, now: datetime) -> str:
    """The symbol a ticker's news belongs to, as a scan cycle reads it: its preferred listing, else the replacement
    found for it in the last 7 days, else the ticker itself."""
    ticker = _symbol(ticker)
    if ticker in preferred:
        return preferred[ticker]
    found = store.resolved_symbol(ticker, now=utc(now))
    return found[0] if found is not None else ticker


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


def best_match(
    quotes: Sequence[dict[str, Any]], query: str, ticker: str, *, texts: Sequence[str] = ()
) -> Resolution | None:
    """The listing of Yahoo's quotes that replaces ticker (see candidate), or None. One found by a longer name counts
    only when one of texts (the stories filed under ticker) names it: "Toshiba" finds Toshiba Tec, and only a story
    about Toshiba Tec should go there."""
    found, longer = candidate(quotes, query, ticker)
    if found is not None and longer and not named_in(found.name, texts):
        return None
    return found


def candidate(quotes: Sequence[dict[str, Any]], query: str, ticker: str) -> tuple[Resolution | None, bool]:
    """(the quote that can replace ticker, whether it was found by a longer name), or (None, False).

    Only company shares (EQUITY, and no fund, note, partnership or warrant by their names unless query says so:
    "Hess Midstream LP" isn't Hess), not ticker itself, on the same exchange (the same suffix; without one, a main US
    exchange). Among those, in Yahoo's order (best first):
    1. the same company, anywhere in the list: the same name without legal forms (the full name, or a short name
       that is exactly query), its initials ("PPC" and "Public Power Corporation S.A."), or names of two words or more
       spelled almost alike (see same_name). "Siemens" is Siemens AG, not Siemens Energy listed above it.
    2. else a longer full name that starts with query ("Metlen" -> "Metlen Energy & Metals PLC"), when no other
       company on Yahoo's whole list has a name starting that way ("Marathon": Marathon Petroleum and Marathon
       Bancorp). The short name doesn't count here: it can be stale (DHY's still says "Credit Suisse High Yield").
    """
    suffix = _suffix(ticker)
    listings: list[tuple[str, str, str]] = []
    for quote in quotes:
        symbol = str(quote.get("symbol") or "").strip().upper()
        if not symbol or symbol == ticker or str(quote.get("quoteType") or "").upper() != "EQUITY":
            continue
        if _suffix(symbol) != suffix or (not suffix and str(quote.get("exchange") or "").upper() not in US_EXCHANGES):
            continue
        longname, shortname = _yahoo_name(quote.get("longname")), _yahoo_name(quote.get("shortname"))
        if not _not_shares(query, longname, shortname):
            listings.append((symbol, longname, shortname))
    for symbol, longname, shortname in listings:
        if same_name(query, longname) or (shortname and _core(shortname) == _core(query)):
            return Resolution(symbol=symbol, name=longname or shortname, query=query), False
    for symbol, longname, shortname in listings:
        name = longname or shortname
        if _starts_with(query, name) and not _rivals(quotes, query, name):
            return Resolution(symbol=symbol, name=name, query=query), True
    return None, False


def same_name(a: str, b: str) -> bool:
    """Whether two company names are the same company: equal without legal forms ("Allwyn" and "Allwyn AG", "Block"
    and "Block, Inc."), or one is a single word of at least 3 letters made of the initials of the other's first words
    ("PPC" and "Public Power Corporation S.A.", "NBG" and "National Bank of Greece"), or both have two words or more
    and are spelled almost alike ("Hellenic Telecommunications Organisation" and "Hellenic Telecommunication
    Organization SA"; "Shell" and "Shelly" are different companies). A shared first word isn't enough: "Hitachi" isn't
    "Hitachi Metals", nor "Eurobank Ergasias" "Eurobank"."""
    words_a, words_b = _core(a), _core(b)
    if not words_a or not words_b:
        return False
    if words_a == words_b:
        return True
    (short, _), (long, long_name) = sorted(((words_a, a), (words_b, b)), key=lambda pair: len(pair[0]))
    if len(short) == 1 and len(short[0]) >= 3 and short[0] in _initials(long_name):
        return True
    return len(short) >= 2 and SequenceMatcher(None, " ".join(words_a), " ".join(words_b)).ratio() >= _SIMILARITY


def named_in(name: str, texts: Sequence[str]) -> bool:
    """Whether one of texts names the company: its name without legal forms, as whole words in any case ("Metlen
    Energy & Metals PLC" in "Metlen Energy and Metals raises its guidance")."""
    core = " ".join(_core(name))
    if not core:
        return False
    pattern = re.compile(rf"\b{re.escape(core)}\b")
    return any(pattern.search(_plain(text)) for text in texts if isinstance(text, str))


def fund_name(name: str | None) -> bool:
    """Whether a listing's name says it is a fund (FUND_WORDS), whatever Yahoo types it as."""
    return bool(name) and bool(set(_words(name)) & FUND_WORDS)


def _starts_with(query: str, name: str) -> bool:
    """Whether name is longer than query and starts with its words ("Metlen" and "Metlen Energy & Metals PLC")."""
    words, full = _core(query), _core(name)
    return bool(words) and len(full) > len(words) and full[: len(words)] == words


def _rivals(quotes: Sequence[dict[str, Any]], query: str, name: str) -> bool:
    """Whether Yahoo's list has shares of another company whose name also starts with query (on any exchange)."""
    words, full = _core(query), _core(name)
    for quote in quotes:
        if str(quote.get("quoteType") or "").upper() != "EQUITY":
            continue
        longname, shortname = _yahoo_name(quote.get("longname")), _yahoo_name(quote.get("shortname"))
        other = _core(longname or shortname)
        if other[: len(words)] == words and other[: len(full)] != full and not _not_shares(query, longname, shortname):
            return True
    return False


def _not_shares(query: str, *names: str) -> bool:
    """Whether a listing's names say it is a fund, a note, a partnership or a warrant, and query doesn't."""
    asked = set(_words(query))
    return any((set(_words(name)) - asked) & _NOT_SHARES for name in names if name)


def _yahoo_name(value: Any) -> str:
    """Yahoo's name without padding and the line marker of German listings ("SIEMENS AG                    N")."""
    return " ".join(_LINE_MARK.sub("", value).split()) if _text(value) else ""


def _core(name: str) -> list[str]:
    """A company name's words without legal forms and punctuation, casefolded ("Block, Inc." -> ["block"])."""
    return company_core(name).casefold().split()


def _words(name: str) -> list[str]:
    return re.findall(r"\w+", name.casefold().replace(".", ""))  # "L.P." -> lp


def _plain(text: str) -> str:
    """Text as company_core writes names: "&" as "and", punctuation as spaces, casefolded."""
    return " ".join(re.sub(r"[^\w\s]", " ", text.replace("&", " and ")).casefold().split())


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
