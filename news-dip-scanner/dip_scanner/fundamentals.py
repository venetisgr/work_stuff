"""Optional enrichment: recent income statement figures from SEC XBRL company facts (US filers only).

Two SEC endpoints, both free but only with a User-Agent that names you and gives an email address (SEC_USER_AGENT):
- https://www.sec.gov/files/company_tickers.json maps tickers (BRK-B style) to CIK numbers;
- https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json has every XBRL fact the company ever filed, as
  facts -> taxonomy (us-gaap, or ifrs-full for foreign filers) -> concept -> units (USD, USD/shares...) -> a list of
  {start, end, val, accn, fy, fp, form, filed, frame}.

Things to know about those facts: fy/fp describe the filing, not the period (a 10-Q repeats last year's quarter as a
comparative, with this year's fy), so periods come from start/end only. Q4 is never reported on its own: the 10-K has
the fiscal year, so Q4 = year - (Q1 + Q2 + Q3). Cash flow statements are year-to-date, so only Q1 has a 3-month cash
flow figure; the later quarters are differences of year-to-date totals. Per-share figures don't add up, so they are
never derived.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import requests

from .config import ConfigError
from .models import FUNDAMENTAL_METRICS, Fundamentals

log = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
TICKERS_TTL = 24 * 3600  # seconds
FACTS_TTL = 12 * 3600
MIN_INTERVAL = 0.2  # seconds between requests: at most 5 a second (the SEC allows 10)

QUARTER_DAYS = (80, 100)
YEAR_DAYS = (350, 380)  # 52/53-week fiscal years included
QUARTERS = 5
YEARS = 3
_SLACK = timedelta(days=3)  # how far apart a period's end and the next one's start may be

# Concepts per metric, in order of preference. When a company has switched concepts over the years, the one with the
# most recent figures wins and the others only fill periods it lacks.
CONCEPTS: dict[str, dict[str, tuple[str, ...]]] = {
    "us-gaap": {
        "revenue": (
            "Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "SalesRevenueNet",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
        ),
        "gross_profit": ("GrossProfit",),
        "operating_income": ("OperatingIncomeLoss",),
        "net_income": ("NetIncomeLoss",),
        "eps_diluted": ("EarningsPerShareDiluted",),
        "operating_cash_flow": ("NetCashProvidedByUsedInOperatingActivities",),
    },
    "ifrs-full": {  # foreign private issuers filing 20-F (e.g. SAP); usually annual figures only
        "revenue": ("Revenue", "RevenueFromContractsWithCustomers"),
        "gross_profit": ("GrossProfit",),
        "operating_income": ("ProfitLossFromOperatingActivities",),
        "net_income": ("ProfitLossAttributableToOwnersOfParent", "ProfitLoss"),
        "eps_diluted": ("DilutedEarningsLossPerShare",),
        "operating_cash_flow": ("CashFlowsFromUsedInOperatingActivities",),
    },
}
PER_SHARE = frozenset({"eps_diluted"})


class SecError(Exception):
    """The SEC couldn't be reached or refused the request."""


class SecFundamentals:
    """Looks up a ticker's CIK and reads its company facts from data.sec.gov, with a small disk cache.

    cache_dir (e.g. <DATA_DIR>/cache) keeps the ticker list for a day and each company's parsed figures for 12 hours,
    in a "sec" subfolder; without it the cache lives in memory only. sleep and clock (epoch seconds) are for tests.
    """

    def __init__(
        self,
        user_agent: str,
        *,
        session=None,
        cache_dir: Path | None = None,
        timeout: float = 20,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not user_agent or not user_agent.strip():
            raise ConfigError(
                "The SEC needs a User-Agent with your name and email address: set SEC_USER_AGENT, "
                'e.g. "Jane Doe jane@example.com".'
            )
        self.user_agent = user_agent.strip()
        self.session = session if session is not None else requests.Session()
        self.cache_dir = Path(cache_dir) / "sec" if cache_dir is not None else None
        self.timeout = timeout
        self._sleep = sleep
        self._clock = clock
        self._headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
        }
        self._lock = threading.Lock()
        self._last_request: float | None = None
        self._tickers: dict[str, str] | None = None
        self._tickers_fetched = 0.0
        self._facts: dict[str, tuple[float, Fundamentals | None]] = {}

    def cik(self, ticker: str) -> str | None:
        """The 10-digit CIK of a ticker, or None if the SEC doesn't list it. BRK.B and BRK-B both work.

        Raises SecError when the ticker list can't be downloaded and there is no cached copy.
        """
        tickers = self._ticker_map()
        return next((tickers[name] for name in _variants(ticker) if name in tickers), None)

    def get(self, ticker: str) -> Fundamentals | None:
        """Recent quarterly and annual figures, or None for non-US tickers, unknown ones and network errors.

        Never raises: problems are logged.
        """
        symbol = _normalise(ticker)
        try:
            cik = self.cik(symbol)
            if cik is None:
                log.info("%s isn't in the SEC's ticker list (not a US filer?); no fundamentals", symbol)
                return None
            fundamentals = self._fundamentals(cik, symbol)
        except Exception as exc:  # optional enrichment: never let it break a scan
            log.warning("Couldn't get SEC fundamentals for %s: %s", symbol, exc)
            return None
        if fundamentals is None:
            log.info("The SEC has no usable income statement figures for %s (CIK %s)", symbol, cik)
            return None
        return dataclasses.replace(fundamentals, ticker=symbol)

    # --- ticker list -----------------------------------------------------------------------------------------------

    def _ticker_map(self) -> dict[str, str]:
        now = self._clock()
        if self._tickers is not None and now - self._tickers_fetched < TICKERS_TTL:
            return self._tickers
        cached = self._read_cache("company_tickers.json")
        tickers = cached.get("tickers") if cached else None
        if isinstance(tickers, dict) and now - cached["fetched"] < TICKERS_TTL:
            self._tickers, self._tickers_fetched = tickers, cached["fetched"]
            return tickers
        try:
            fresh = parse_company_tickers(self._get_json(TICKERS_URL))
        except SecError as exc:
            if not isinstance(tickers, dict):
                raise
            log.warning("Using the SEC ticker list cached at %s: %s", time.ctime(cached["fetched"]), exc)
            fresh, now = tickers, cached["fetched"]
        else:
            self._write_cache("company_tickers.json", {"fetched": now, "tickers": fresh})
        self._tickers, self._tickers_fetched = fresh, now
        return fresh

    # --- company facts ---------------------------------------------------------------------------------------------

    def _fundamentals(self, cik: str, symbol: str) -> Fundamentals | None:
        """The parsed figures of a CIK (None = the SEC has none): from memory, the disk cache or data.sec.gov."""
        now = self._clock()
        known = self._facts.get(cik) or self._cached_facts(cik)
        if known is not None and now - known[0] < FACTS_TTL:
            self._facts[cik] = known
            return known[1]
        try:
            facts = self._get_json(FACTS_URL.format(cik=cik))
        except SecError as exc:
            if known is None:
                raise
            log.warning("Using SEC figures for %s cached at %s: %s", symbol, time.ctime(known[0]), exc)
            return known[1]
        fundamentals = None
        if isinstance(facts, dict):  # None = 404: the company has no XBRL facts
            parsed = parse_company_facts(symbol, cik, facts)
            fundamentals = parsed if parsed.quarters or parsed.annual else None
        self._facts[cik] = (now, fundamentals)
        self._write_cache(f"CIK{cik}.json", {"fetched": now, "fundamentals": _to_cache(fundamentals)})
        return fundamentals

    def _cached_facts(self, cik: str) -> tuple[float, Fundamentals | None] | None:
        cached = self._read_cache(f"CIK{cik}.json")
        if cached is None or "fundamentals" not in cached:
            return None
        try:
            return cached["fetched"], _from_cache(cached["fundamentals"])
        except TypeError:  # written by a different version
            return None

    # --- HTTP and cache --------------------------------------------------------------------------------------------

    def _get_json(self, url: str) -> Any:
        """The JSON at url, or None for a 404. Raises SecError for anything else that isn't JSON."""
        self._throttle()
        try:
            response = self.session.get(url, headers=self._headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise SecError(f"Couldn't reach {url} ({exc.__class__.__name__}: {exc}).") from exc
        status = response.status_code
        if status == 404:
            return None
        if status in (403, 429):
            raise SecError(
                f"The SEC refused {url} ({status}). It wants SEC_USER_AGENT to name you and give an email address "
                '(e.g. "Jane Doe jane@example.com") and allows at most 10 requests a second.'
            )
        if status >= 400:
            raise SecError(f"{url} returned HTTP {status}.")
        try:
            return response.json()
        except ValueError as exc:
            raise SecError(f"{url} didn't return JSON.") from exc

    def _throttle(self) -> None:
        """Keep at least MIN_INTERVAL seconds between requests."""
        with self._lock:
            now = self._clock()
            if self._last_request is not None:
                wait = self._last_request + MIN_INTERVAL - now
                if wait > 0:
                    self._sleep(wait)
                    now += wait
            self._last_request = now

    def _read_cache(self, name: str) -> dict | None:
        if self.cache_dir is None:
            return None
        try:
            data = json.loads((self.cache_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("fetched"), int | float):
            return None
        return data

    def _write_cache(self, name: str, data: dict) -> None:
        if self.cache_dir is None:
            return
        path = self.cache_dir / name
        temp: str | None = None
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            handle, temp = tempfile.mkstemp(dir=self.cache_dir, prefix=f".{name}.", suffix=".tmp")
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                json.dump(data, file)
            os.replace(temp, path)  # atomic: a reader never sees half a file
        except OSError as exc:
            log.warning("Couldn't write the SEC cache file %s: %s", path, exc)
            if temp is not None:
                Path(temp).unlink(missing_ok=True)


def _normalise(ticker: str) -> str:
    return ticker.strip().upper().lstrip("$")


def _variants(ticker: str) -> list[str]:
    """The ways the SEC might spell a ticker: class shares are BRK-B there, BRK.B elsewhere."""
    symbol = _normalise(ticker)
    return list(dict.fromkeys([symbol, symbol.replace(".", "-"), symbol.replace("-", ".")]))


def parse_company_tickers(data: Any) -> dict[str, str]:
    """{TICKER: 10-digit CIK} from company_tickers.json ({"0": {"cik_str": 320193, "ticker": "AAPL", ...}, ...})."""
    rows = data.values() if isinstance(data, dict) else data if isinstance(data, list) else ()
    tickers = {}
    for row in rows:
        try:
            tickers[str(row["ticker"]).strip().upper()] = f"{int(row['cik_str']):010d}"
        except (KeyError, TypeError, ValueError):
            continue
    if not tickers:
        raise SecError(f"{TICKERS_URL} didn't contain a ticker list.")
    return tickers


def _to_cache(fundamentals: Fundamentals | None) -> dict | None:
    return dataclasses.asdict(fundamentals) if fundamentals is not None else None


def _from_cache(data: dict | None) -> Fundamentals | None:
    return Fundamentals(**data) if data is not None else None


# --- parsing company facts -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Fact:
    start: date
    end: date
    value: float
    filed: str  # YYYY-MM-DD; later filings (restatements, comparatives) win

    @property
    def days(self) -> int:
        return (self.end - self.start).days


def parse_company_facts(ticker: str, cik: str, facts: dict) -> Fundamentals:
    """Fundamentals from a companyfacts JSON document.

    Quarters are facts lasting 80-100 days and fiscal years 350-380 days; when several facts end on the same day the
    latest filing wins. Missing quarters of flow items (not EPS) are derived from the fiscal year or year-to-date
    totals: Q4 = year - (Q1 + Q2 + Q3), and for cash flow Q2 = H1 - Q1 and so on. Keeps the 5 newest quarters and 3
    newest years, newest first; a metric the company doesn't report is None.
    """
    taxonomies = facts.get("facts") if isinstance(facts.get("facts"), dict) else {}
    taxonomy = next(
        (name for name, concepts in CONCEPTS.items() if _has_any(taxonomies.get(name), concepts.values())),
        "us-gaap",
    )
    concepts = CONCEPTS[taxonomy]
    source = taxonomies.get(taxonomy) or {}
    currency = _currency(source, (names for metric, names in concepts.items() if metric not in PER_SHARE))
    quarters: dict[str, dict[date, _Fact]] = {}
    years: dict[str, dict[date, _Fact]] = {}
    for metric in FUNDAMENTAL_METRICS:
        unit = f"{currency}/shares" if metric in PER_SHARE else currency
        found = _concept_facts(source, concepts.get(metric, ()), unit)
        quarters[metric], years[metric] = _periods(found, derive=metric not in PER_SHARE)
    entity = facts.get("entityName")
    return Fundamentals(
        ticker=ticker,
        entity=" ".join(entity.split()) if isinstance(entity, str) and entity.strip() else ticker,
        cik=f"{int(cik):010d}" if str(cik).strip().isdigit() else str(cik),
        currency=currency,
        quarters=_rows(quarters, QUARTERS),
        annual=_rows(years, YEARS),
    )


def _has_any(source: Any, concept_lists: Iterable[tuple[str, ...]]) -> bool:
    return isinstance(source, dict) and any(name in source for names in concept_lists for name in names)


def _currency(source: dict, concept_lists: Iterable[tuple[str, ...]]) -> str:
    """The reporting currency: the unit most flow facts use (USD when tied or when there are none)."""
    counts: Counter[str] = Counter()
    for names in concept_lists:
        for name in names:
            units = (source.get(name) or {}).get("units") or {}
            for unit, items in units.items():
                if "/" not in unit and isinstance(items, list):
                    counts[unit] += len(items)
    if not counts:
        return "USD"
    return max(counts, key=lambda unit: (counts[unit], unit == "USD"))


def _concept_facts(source: dict, names: tuple[str, ...], unit: str) -> list[_Fact]:
    """Facts of a metric in one unit, merged over its concepts (freshest concept first, then list order)."""
    ranked = []
    for order, name in enumerate(names):
        items = ((source.get(name) or {}).get("units") or {}).get(unit) or []
        found = [fact for fact in map(_fact, items) if fact is not None]
        if found:
            ranked.append((max(fact.end for fact in found), -order, found))
    ranked.sort(key=lambda entry: entry[:2], reverse=True)
    merged: dict[tuple[date, date], _Fact] = {}
    for _, _, found in ranked:
        own: dict[tuple[date, date], _Fact] = {}
        for fact in found:
            key = (fact.start, fact.end)
            if key not in own or fact.filed >= own[key].filed:
                own[key] = fact
        for key, fact in own.items():
            merged.setdefault(key, fact)
    return list(merged.values())


def _fact(item: Any) -> _Fact | None:
    """A duration fact, or None for instants (balance sheet items) and malformed entries."""
    if not isinstance(item, dict):
        return None
    try:
        start, end = date.fromisoformat(item["start"]), date.fromisoformat(item["end"])
        value = item["val"]
    except (KeyError, TypeError, ValueError):
        return None
    if not isinstance(value, int | float) or isinstance(value, bool) or end <= start:
        return None
    return _Fact(start, end, float(value), str(item.get("filed") or ""))


def _periods(found: list[_Fact], *, derive: bool) -> tuple[dict[date, _Fact], dict[date, _Fact]]:
    """(quarters by end date, fiscal years by end date); the latest filing wins for each end date."""
    quarters: dict[date, _Fact] = {}
    years: dict[date, _Fact] = {}
    totals: list[_Fact] = []  # longer than a quarter and starting a fiscal year: half years, 9 months, years
    for fact in sorted(found, key=lambda fact: fact.filed):
        if QUARTER_DAYS[0] <= fact.days <= QUARTER_DAYS[1]:
            quarters[fact.end] = fact
        elif fact.days > QUARTER_DAYS[1]:
            totals.append(fact)
            if YEAR_DAYS[0] <= fact.days <= YEAR_DAYS[1]:
                years[fact.end] = fact
    if derive:
        _derive_quarters(quarters, totals)
    return quarters, years


def _derive_quarters(quarters: dict[date, _Fact], totals: list[_Fact]) -> None:
    """Add the quarters a year-to-date total implies: total minus the quarters (or total) before it.

    Totals are processed shortest period first, so a quarter derived from the half year helps derive the next one.
    """
    for total in sorted(totals, key=lambda fact: (fact.end, fact.start)):
        if any(abs(end - total.end) <= _SLACK for end in quarters):
            continue  # reported (or already derived)
        base = _chain(quarters, total.start, total.end) or _earlier_total(totals, total)
        if base is None:
            continue
        base_end, base_value = base
        quarters[total.end] = _Fact(base_end + timedelta(days=1), total.end, total.value - base_value, total.filed)


def _chain(quarters: dict[date, _Fact], start: date, end: date) -> tuple[date, float] | None:
    """(end, sum) of back-to-back quarters from start that stop one quarter short of end, if there are such."""
    position, total, last_end = start, 0.0, None
    while True:
        step = next(
            (q for q in quarters.values() if abs(q.start - position) <= _SLACK and q.end < end),
            None,
        )
        if step is None:
            return None
        total += step.value
        last_end = step.end
        if QUARTER_DAYS[0] <= (end - last_end).days <= QUARTER_DAYS[1]:
            return last_end, total
        position = last_end + timedelta(days=1)


def _earlier_total(totals: list[_Fact], total: _Fact) -> tuple[date, float] | None:
    """(end, value) of a total from the same start that ends one quarter earlier (e.g. 9 months before the year)."""
    earlier = [
        other
        for other in totals
        if other.start == total.start and QUARTER_DAYS[0] <= (total.end - other.end).days <= QUARTER_DAYS[1]
    ]
    if not earlier:
        return None
    best = max(earlier, key=lambda other: other.filed)
    return best.end, best.value


def _rows(series: dict[str, dict[date, _Fact]], count: int) -> list[dict]:
    """The newest count periods (by end date) with every metric's value, newest first."""
    ends = sorted({end for by_end in series.values() for end in by_end}, reverse=True)[:count]
    rows = []
    for end in ends:
        row: dict[str, Any] = {"period_end": end.isoformat()}
        for metric in FUNDAMENTAL_METRICS:
            fact = series.get(metric, {}).get(end)
            row[metric] = fact.value if fact is not None else None
        rows.append(row)
    return rows
