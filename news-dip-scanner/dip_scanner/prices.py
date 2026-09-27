"""Daily prices from the Yahoo Finance chart API and the dip statistics computed from them.

The chart API (https://query1.finance.yahoo.com/v8/finance/chart/<SYMBOL>?range=2y&interval=1d) needs no key, only a
browser-like User-Agent. Its meta block has the live price (regularMarketPrice at regularMarketTime) and the 52-week
range; the timestamp and indicators.quote arrays hold one row per session, with nulls for rows Yahoo has no prices
for. Daily bars are stamped at the session open, so a bar's day is its date in the exchange's time zone.

Beware meta.chartPreviousClose: it is the close before the requested range starts, not yesterday's close.
"""

from __future__ import annotations

import logging
import math
import statistics
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from itertools import pairwise
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from .models import PriceBar, PriceStats, utc

log = logging.getLogger(__name__)

HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_HEADERS = {"User-Agent": BROWSER_USER_AGENT, "Accept": "application/json"}
RATE_LIMIT_BACKOFF = 2.0  # seconds to wait before the one retry after a 429

MIN_BARS = 21  # 20 sessions of history plus the latest one
TRADING_DAYS = 252  # a year of sessions: annualises volatility and is the 52-week window when meta lacks it
VOLATILITY_RETURNS = 60
VOLUME_SESSIONS = 20
DRAWDOWN_WINDOW = 126  # about 6 months of sessions
STAT_LOW_Z = 1.645  # 5th percentile of the normal distribution
# A quote older than this means the stock isn't trading (suspended or delisted); long holidays are shorter.
STALE_AFTER = timedelta(days=14)

# Yahoo's ranges and the number of days each one is sure to cover, smallest first ("max" covers everything).
_RANGES = (("1mo", 28), ("3mo", 89), ("6mo", 181), ("1y", 365), ("2y", 730), ("5y", 1826), ("10y", 3652))
_RANGE_MARGIN_DAYS = 5


class PriceError(Exception):
    """No prices for this symbol (unknown ticker, delisted, or not enough history). Retrying won't help."""


class PriceFetchError(Exception):
    """Yahoo Finance couldn't be reached or kept failing (network, 5xx, 429). The ticker may be fine; try later."""


def _now() -> datetime:
    return datetime.now(UTC)


def _default_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(_HEADERS)
    return session


class YahooPrices:
    """A small client for Yahoo's chart API with a per-ticker cache of PriceStats.

    sleep and clock are there for tests: sleep waits before the retry after a 429, clock gives "now" when stats()
    isn't told the time.
    """

    def __init__(
        self,
        session=None,
        *,
        timeout: float = 15,
        cache_seconds: float = 600,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.session = session if session is not None else _default_session()
        self.timeout = timeout
        self.cache_seconds = cache_seconds
        self._sleep = sleep
        self._clock = clock
        self._cache: dict[str, tuple[datetime, PriceStats]] = {}
        self._lock = threading.Lock()

    def chart(self, ticker: str, *, range_: str = "2y", interval: str = "1d") -> tuple[dict, list[PriceBar]]:
        """The chart meta and daily bars of a ticker, oldest first (rows with nulls dropped).

        Raises PriceError when Yahoo has no data for the symbol and PriceFetchError when Yahoo can't be reached.
        """
        symbol = _symbol(ticker)
        result = self._fetch(symbol, {"range": range_, "interval": interval})
        meta = result.get("meta")
        if not isinstance(meta, dict):
            raise PriceError(f"Yahoo Finance returned no quote details for {symbol}.")
        return meta, parse_bars(result, _exchange_tz(meta))

    def stats(self, ticker: str, *, now: datetime | None = None) -> PriceStats:
        """PriceStats for a ticker, cached for cache_seconds.

        Raises PriceError for unknown symbols, too little history, or a quote older than STALE_AFTER (the stock isn't
        trading), and PriceFetchError when Yahoo can't be reached.
        """
        symbol = _symbol(ticker)
        now = utc(now) if now is not None else utc(self._clock())
        with self._lock:
            cached = self._cache.get(symbol)
        if cached is not None and 0 <= (now - cached[0]).total_seconds() < self.cache_seconds:
            return cached[1]
        meta, bars = self.chart(symbol)
        stats = compute_stats(symbol, meta, bars)
        if now - stats.as_of > STALE_AFTER:
            raise PriceError(
                f"{symbol} hasn't traded since {stats.as_of:%Y-%m-%d} according to Yahoo Finance "
                "(suspended or delisted?)."
            )
        with self._lock:
            self._cache[symbol] = (now, stats)
        return stats

    def bars_since(self, ticker: str, start: date, *, now: datetime | None = None) -> list[PriceBar]:
        """Daily bars from start (inclusive) to today, fetched with the smallest Yahoo range that covers start."""
        today = utc(now if now is not None else self._clock()).date()
        _, bars = self.chart(ticker, range_=range_for(start, today))
        return [bar for bar in bars if bar.day >= start]

    def _fetch(self, symbol: str, params: dict[str, str]) -> dict:
        """The chart result for symbol: query1 first, query2 on connection errors and 5xx, one retry after a 429."""
        problems: list[str] = []
        retried = False
        index = 0
        while index < len(HOSTS):
            host = HOSTS[index]
            url = f"https://{host}/v8/finance/chart/{quote(symbol, safe='')}"
            try:
                response = self.session.get(url, params=params, headers=_HEADERS, timeout=self.timeout)
            except requests.RequestException as exc:
                problems.append(f"{host}: {exc.__class__.__name__}: {exc}")
                index += 1
                continue
            status = response.status_code
            if status == 429:
                problems.append(f"{host}: 429 Too Many Requests")
                if retried:
                    break
                retried = True
                log.info("Yahoo Finance is rate limiting; retrying %s in %.0f s", symbol, RATE_LIMIT_BACKOFF)
                self._sleep(RATE_LIMIT_BACKOFF)
                continue
            if status >= 500:
                problems.append(f"{host}: HTTP {status}")
                index += 1
                continue
            return _chart_result(symbol, response)
        raise PriceFetchError(f"Couldn't get prices for {symbol} from Yahoo Finance ({'; '.join(problems)}).")


def _symbol(ticker: str) -> str:
    symbol = ticker.strip().upper()
    if not symbol:
        raise PriceError("No ticker given.")
    return symbol


def _chart_result(symbol: str, response: Any) -> dict:
    """The first chart result in a Yahoo response, or PriceError/PriceFetchError explaining why there is none."""
    try:
        data = response.json()
    except ValueError:
        data = None
    chart = data.get("chart") if isinstance(data, dict) else None
    chart = chart if isinstance(chart, dict) else {}
    error = chart.get("error")
    status = response.status_code
    if status == 404 or (error and status in (200, 400)):
        detail = (error.get("description") or error.get("code")) if isinstance(error, dict) else None
        reason = f": {detail}" if detail else ""
        raise PriceError(f"Yahoo Finance has no prices for {symbol}{reason}.")
    if status >= 400:
        raise PriceFetchError(f"Yahoo Finance returned HTTP {status} for {symbol}.")
    if data is None:
        raise PriceFetchError(f"Yahoo Finance sent something other than JSON for {symbol}.")
    results = chart.get("result")
    if not results or not isinstance(results[0], dict):
        raise PriceError(f"Yahoo Finance returned no data for {symbol}.")
    return results[0]


def _exchange_tz(meta: dict) -> tzinfo:
    """The exchange's time zone: its IANA name when known (handles DST), else the fixed gmtoffset from meta."""
    name = meta.get("exchangeTimezoneName")
    if isinstance(name, str) and name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, OSError):  # unknown name, or no tz database on this machine
            pass
    offset = meta.get("gmtoffset")
    seconds = int(offset) if _is_number(offset) else 0
    return timezone(timedelta(seconds=seconds))


def parse_bars(result: dict, tz: tzinfo) -> list[PriceBar]:
    """Daily bars from a chart result, oldest first.

    Rows without a (positive) close are dropped; a missing open/high/low becomes the close and a missing volume 0.
    When Yahoo sends two rows for the same day (it happens during the session), the later one wins.
    """
    timestamps = result.get("timestamp") or []
    quotes = ((result.get("indicators") or {}).get("quote") or [{}])[0] or {}
    columns = {name: quotes.get(name) or [] for name in ("open", "high", "low", "close", "volume")}

    def value(name: str, index: int) -> float | None:
        column = columns[name]
        item = column[index] if index < len(column) else None
        return float(item) if _is_number(item) and math.isfinite(item) else None

    by_day: dict[date, PriceBar] = {}
    for index, stamp in enumerate(timestamps):
        close = value("close", index)
        if not _is_number(stamp) or close is None or close <= 0:
            continue
        volume = value("volume", index)
        day = datetime.fromtimestamp(stamp, UTC).astimezone(tz).date()
        by_day[day] = PriceBar(
            day=day,
            open=value("open", index) or close,
            high=value("high", index) or close,
            low=value("low", index) or close,
            close=close,
            volume=int(volume) if volume and volume > 0 else 0,
        )
    return [by_day[day] for day in sorted(by_day)]


def range_for(start: date, today: date) -> str:
    """The smallest Yahoo range whose history reaches back to start."""
    days = (today - start).days + _RANGE_MARGIN_DAYS
    return next((name for name, span in _RANGES if span >= days), "max")


def compute_stats(ticker: str, meta: dict, bars: list[PriceBar]) -> PriceStats:
    """PriceStats from chart meta and daily bars (needs at least 21 bars, else PriceError).

    The latest session is the exchange-local day of regularMarketTime. Its close is the live price (regularMarketPrice)
    whether or not Yahoo has sent a bar for it yet, so the same numbers come out during the session, after the close
    and before the next open. previous_close is the close of the last bar before that day; the 5- and 20-day changes
    compare the price with the close 5 and 20 sessions before the latest one.
    """
    bars = sorted(bars, key=lambda bar: bar.day)
    if len(bars) < MIN_BARS:
        raise PriceError(f"{ticker} has only {len(bars)} days of prices; at least {MIN_BARS} are needed.")
    tz = _exchange_tz(meta)
    price = _positive(meta.get("regularMarketPrice")) or bars[-1].close
    market_time = meta.get("regularMarketTime")
    if _is_number(market_time) and market_time > 0:
        as_of = datetime.fromtimestamp(market_time, UTC)
    else:  # no quote time: take the last bar's session
        as_of = datetime.combine(bars[-1].day, datetime.min.time(), tzinfo=tz).astimezone(UTC)
    market_day = as_of.astimezone(tz).date()

    before = [bar for bar in bars if bar.day < market_day]  # completed sessions before the latest one
    latest = bars[-1] if bars[-1].day >= market_day else None  # the latest session's bar, if Yahoo sent it
    if len(before) < MIN_BARS - 1:
        raise PriceError(
            f"{ticker} has only {len(before)} sessions of prices before {market_day}; at least {MIN_BARS - 1} are "
            "needed."
        )
    # One entry per session, the latest last; the latest close is the live price.
    closes = [bar.close for bar in before] + [price]
    highs = [bar.high for bar in before] + [max(latest.high, price) if latest else price]
    lows = [bar.low for bar in before] + [min(latest.low, price) if latest else price]

    previous_close = before[-1].close
    high_20d = max(highs[-20:])
    high_52w = _positive(meta.get("fiftyTwoWeekHigh")) or max(highs[-TRADING_DAYS:])
    low_52w = _positive(meta.get("fiftyTwoWeekLow")) or min(lows[-TRADING_DAYS:])
    high_52w = max(high_52w, high_20d)  # keeps drawdown_52w <= drawdown_20d <= 0
    low_52w = min(low_52w, price)
    volatility = _volatility_pct(closes[-(VOLATILITY_RETURNS + 1) :])
    base_volume = statistics.fmean(bar.volume for bar in before[-VOLUME_SESSIONS:])
    name = meta.get("longName") or meta.get("shortName")
    name = " ".join(name.split()) if isinstance(name, str) else ""  # shortName can end in padding
    return PriceStats(
        ticker=ticker,
        name=name or None,
        currency=str(meta.get("currency") or "").strip() or "?",
        exchange=meta.get("fullExchangeName") or meta.get("exchangeName") or None,
        as_of=as_of,
        price=price,
        previous_close=previous_close,
        change_1d_pct=_change(price, previous_close),
        change_5d_pct=_change(price, before[-5].close),
        change_20d_pct=_change(price, before[-20].close),
        high_20d=high_20d,
        high_52w=high_52w,
        low_52w=low_52w,
        drawdown_20d_pct=_change(price, high_20d),
        drawdown_52w_pct=_change(price, high_52w),
        above_low_52w_pct=_change(price, low_52w),
        sma_50=statistics.fmean(closes[-50:]) if len(closes) >= 50 else None,
        sma_200=statistics.fmean(closes[-200:]) if len(closes) >= 200 else None,
        volatility_pct=volatility,
        volume_ratio=latest.volume / base_volume if latest is not None and base_volume > 0 else None,
        stat_low_6m=price * math.exp(-STAT_LOW_Z * (volatility / 100) * math.sqrt(0.5)),
        worst_6m_drawdown_pct=worst_drawdown_pct(closes, DRAWDOWN_WINDOW),
    )


def _volatility_pct(closes: Sequence[float]) -> float:
    """Annualised sample standard deviation of daily log returns, in %."""
    returns = [math.log(current / previous) for previous, current in pairwise(closes)]
    return statistics.stdev(returns) * math.sqrt(TRADING_DAYS) * 100


def worst_drawdown_pct(closes: Sequence[float], window: int) -> float:
    """The worst fall from a close to a later close at most window - 1 sessions after it, in % (<= 0).

    A sliding-window maximum (a deque of indices with falling closes) keeps this O(n).
    """
    worst = 0.0
    peaks: deque[int] = deque()
    for index, close in enumerate(closes):
        while peaks and peaks[0] <= index - window:
            peaks.popleft()
        while peaks and closes[peaks[-1]] <= close:
            peaks.pop()
        peaks.append(index)
        worst = min(worst, close / closes[peaks[0]] - 1)
    return worst * 100


def _change(value: float, base: float) -> float:
    return (value / base - 1) * 100


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _positive(value: Any) -> float | None:
    return float(value) if _is_number(value) and math.isfinite(value) and value > 0 else None
