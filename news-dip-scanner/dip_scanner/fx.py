"""Exchange rates for an account in another currency ([account] currency in scanner.toml), from Yahoo's chart API.

Yahoo serves currency pairs like any other symbol: EURUSD=X is the price of one euro in dollars (1.1386 USD on
2026-09-27; its meta currency is USD), so one dollar is 1 / 1.1386 = 0.8783 EUR. The pair is asked for with the
account's currency first (EURUSD=X, EURJPY=X, EURKRW=X): its price is then at least 1 for most currencies, where
Yahoo's four decimals lose nothing (JPYEUR=X is quoted as 0.0056), and the thin pairs (ILSEUR=X had one daily bar in a
month) have full histories that way round. When that pair doesn't exist, the reverse one is tried; the meta currency
says which way a pair runs, so a rate is never inverted by mistake.

Prices quoted in hundredths (London pence GBp/GBX, Johannesburg cents ZAc, Tel Aviv agorot ILA) are converted through
their main currency: a rate here is always per unit of the currency as quoted, so price * rate is in the account's
currency.
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

from .models import utc
from .prices import PriceError, range_for

if TYPE_CHECKING:
    from .prices import YahooPrices

log = logging.getLogger(__name__)

# Currencies Yahoo quotes in hundredths of the main unit: (main currency, quoted units per main unit).
MINOR_UNITS = {"GBp": ("GBP", 100), "GBX": ("GBP", 100), "ZAc": ("ZAR", 100), "ILA": ("ILS", 100)}
HISTORY_MARGIN_DAYS = 10  # history() starts this much earlier, so a rate on or before the first day is there


def _now() -> datetime:
    return datetime.now(UTC)


def main_currency(currency: str | None) -> tuple[str, float]:
    """(the main currency, how many quoted units make one of it): ("GBP", 100) for GBp pence, ("USD", 1) for USD."""
    code = (currency or "").strip()
    if code in MINOR_UNITS:
        return MINOR_UNITS[code]
    return code.upper(), 1


def major_units(price: float, currency: str | None) -> float:
    """A price in the currency's main unit: 150 GBp (pence) is 1.50 (pounds); other currencies are unchanged."""
    return price / main_currency(currency)[1]


def same_money(currency: str | None, account: str | None) -> bool:
    """Whether prices in currency need no conversion for the account: the same main currency (GBp for GBP too)."""
    return bool(account) and main_currency(currency)[0] == main_currency(account)[0]


def pair_symbol(base: str, quote: str) -> str:
    """Yahoo's symbol of a currency pair: the price of one base in quote units, e.g. EURUSD=X."""
    return f"{base}{quote}=X"


class FxRates:
    """Exchange rates into the account's currency, cached per currency for cache_seconds (like the prices).

    prices is the YahooPrices client the rest of the scanner uses (its session, retries and rate-limit handling).
    """

    def __init__(
        self,
        prices: YahooPrices,
        *,
        cache_seconds: float = 600,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.prices = prices
        self.cache_seconds = cache_seconds
        self._clock = clock
        self._cache: dict[tuple[str, str], tuple[datetime, float]] = {}
        self._lock = threading.Lock()

    def rate(self, currency: str, account: str, *, now: datetime | None = None) -> float:
        """Account-currency units per unit of currency as quoted (1.0 when no conversion is needed).

        Raises PriceError when Yahoo has no rate for the pair and PriceFetchError when Yahoo can't be reached.
        """
        main, factor = main_currency(currency)
        target = main_currency(account)[0]
        if not main or not target:
            raise PriceError(f"No exchange rate without a currency (got {currency!r} and {account!r}).")
        if main == target:
            return 1 / factor
        now = utc(now) if now is not None else utc(self._clock())
        key = (main, target)
        with self._lock:
            cached = self._cache.get(key)
        if cached is not None and 0 <= (now - cached[0]).total_seconds() < self.cache_seconds:
            return cached[1] / factor
        meta, bars, invert = self._pair(main, target, range_="5d")
        price = meta.get("regularMarketPrice")
        if not (isinstance(price, int | float) and math.isfinite(price) and price > 0):
            if not bars:
                raise PriceError(f"Yahoo Finance has no {main}/{target} rate.")
            price = bars[-1].close
        value = 1 / price if invert else float(price)
        with self._lock:
            self._cache[key] = (now, value)
        return value / factor

    def rates(
        self, currency: str, targets: Iterable[str], *, now: datetime | None = None
    ) -> tuple[dict[str, float], dict[str, Exception]]:
        """(rates, problems): the rate from currency into each target (as rate()), and the error of each target Yahoo
        had no rate for. One target's failure never costs the others."""
        found: dict[str, float] = {}
        problems: dict[str, Exception] = {}
        for target in dict.fromkeys(code.strip().upper() for code in targets if code and code.strip()):
            try:
                found[target] = self.rate(currency, target, now=now)
            except Exception as exc:  # PriceError, PriceFetchError, or a bug: reported per currency
                problems[target] = exc
        return found, problems

    def history(
        self, currency: str, account: str, start: date, *, now: datetime | None = None
    ) -> list[tuple[date, float]]:
        """Daily closing rates (account-currency units per unit of currency as quoted) from a little before start to
        today, oldest first. Same errors as rate()."""
        main, factor = main_currency(currency)
        target = main_currency(account)[0]
        today = utc(now if now is not None else self._clock()).date()
        if main == target:
            return [(start - timedelta(days=HISTORY_MARGIN_DAYS), 1 / factor)]
        first = start - timedelta(days=HISTORY_MARGIN_DAYS)
        _, bars, invert = self._pair(main, target, range_=range_for(first, today))
        return [(bar.day, (1 / bar.close if invert else bar.close) / factor) for bar in bars if bar.day >= first]

    def _pair(self, main: str, target: str, *, range_: str) -> tuple[dict, list, bool]:
        """(meta, bars, whether the prices must be inverted) of the pair between main and target: TARGETMAIN=X
        first (its price is main units per target unit, so it is inverted), else MAINTARGET=X."""
        problems: list[str] = []
        for symbol, quoted_in, invert in (
            (pair_symbol(target, main), main, True),
            (pair_symbol(main, target), target, False),
        ):
            try:
                meta, bars = self.prices.chart(symbol, range_=range_)
            except PriceError as exc:
                problems.append(str(exc))
                continue
            currency = str(meta.get("currency") or "").strip().upper()
            if currency and currency != quoted_in:  # not the pair it claims to be: never guess the direction
                problems.append(f"{symbol} is quoted in {currency}, not {quoted_in}.")
                continue
            return meta, bars, invert
        raise PriceError(f"Yahoo Finance has no {main}/{target} exchange rate ({' '.join(problems)})")


def rate_on(rates: Sequence[tuple[date, float]], day: date) -> float | None:
    """The last rate on or before day in (day, rate) pairs sorted oldest first; None when there is none."""
    found = None
    for when, value in rates:
        if when > day:
            break
        found = value
    return found
