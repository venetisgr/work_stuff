"""Shared test helpers: a fake chat model, a fake HTTP session, and builders for the shared data types.

Nothing here touches the network or sleeps. Builders take keyword overrides for any dataclass field and keep the
fields they derive consistent with the ones you pass (e.g. make_stats(price=50) scales the highs and lows too).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from collections import deque
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import requests
from requests.structures import CaseInsensitiveDict

from dip_scanner.models import Analysis, Article, Candidate, Impact, Opportunity, PriceBar, PriceStats

NOW = datetime(2026, 9, 25, 15, 0, tzinfo=UTC)


# --- chat model ----------------------------------------------------------------------------------------------------


class FakeChatModel:
    """A ChatModel that answers from canned replies and records every call as (system, prompt, json_mode).

    replies can be:
    - a callable (system, prompt, json_mode) -> reply, called for every request;
    - a str or dict: the same reply every time;
    - a list/tuple: replies used in order (AssertionError when they run out).
    A reply that is a dict or list is sent as JSON text; an exception (instance or class) is raised instead.
    """

    def __init__(self, replies: Any = None, *, name: str = "fake-model"):
        self.name = name
        self.calls: list[tuple[str, str, bool]] = []
        self._lock = threading.Lock()
        self._fn: Callable[[str, str, bool], Any] | None = None
        self._constant: Any = None
        self._queue: deque | None = None
        if callable(replies) and not _is_exception(replies):
            self._fn = replies
        elif isinstance(replies, list | tuple):
            self._queue = deque(replies)
        else:
            self._constant = "{}" if replies is None else replies

    @property
    def prompts(self) -> list[str]:
        return [prompt for _, prompt, _ in self.calls]

    def complete(self, system: str, prompt: str, *, json_mode: bool = False) -> str:
        with self._lock:
            self.calls.append((system, prompt, json_mode))
            if self._queue is not None:
                if not self._queue:
                    raise AssertionError(f"FakeChatModel ran out of replies after {len(self.calls) - 1} call(s)")
                reply = self._queue.popleft()
            elif self._fn is not None:
                reply = self._fn(system, prompt, json_mode)
            else:
                reply = self._constant
        if _is_exception(reply):
            raise reply
        if isinstance(reply, dict | list):
            return json.dumps(reply)
        return reply


def _is_exception(value: Any) -> bool:
    return isinstance(value, BaseException) or (isinstance(value, type) and issubclass(value, BaseException))


# --- HTTP ----------------------------------------------------------------------------------------------------------


class FakeResponse:
    """Enough of requests.Response for the modules under test."""

    def __init__(
        self,
        url: str = "",
        status_code: int = 200,
        *,
        json_data: Any = None,
        content: bytes | str = b"",
        headers: dict[str, str] | None = None,
    ):
        self.url = url
        self.status_code = status_code
        self.reason = "OK" if status_code < 400 else "Error"
        self.headers = CaseInsensitiveDict(headers or {})
        if json_data is not None:
            self.content = json.dumps(json_data).encode()
            self.headers.setdefault("Content-Type", "application/json")
        else:
            self.content = content.encode() if isinstance(content, str) else content
        self.encoding = "utf-8"

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def json(self) -> Any:
        try:
            return json.loads(self.content)
        except json.JSONDecodeError as exc:
            raise requests.exceptions.JSONDecodeError(exc.msg, exc.doc, exc.pos) from exc

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} {self.reason} for url: {self.url}", response=self)

    def close(self) -> None:
        pass


class FakeSession:
    """A requests.Session stand-in that serves canned responses by URL prefix and records every call.

    routes maps a URL prefix to a route; the longest matching prefix wins (query parameters passed as params= are
    recorded, not matched). A route can be:
    - bytes or str: a 200 response with that body;
    - dict: a 200 JSON response;
    - int: that status code with an empty body;
    - a FakeResponse: returned as is (its url is filled in when empty);
    - an exception (instance or class): raised, e.g. requests.ConnectionError("down");
    - a callable (method, url, call) -> route, where call is the recorded dict;
    - a list of routes: used one per request, the last one repeats (e.g. [429, {"ok": True}]).
    Unrouted URLs get a 404.
    """

    def __init__(self, routes: dict[str, Any] | None = None):
        self.routes: dict[str, Any] = dict(routes or {})
        self.calls: list[dict[str, Any]] = []
        self.headers: dict[str, str] = {}
        self._lock = threading.Lock()
        self._used: dict[str, int] = {}

    @property
    def urls(self) -> list[str]:
        return [call["url"] for call in self.calls]

    def get(self, url, headers=None, params=None, timeout=None, **kwargs) -> FakeResponse:
        return self.request("GET", url, headers=headers, params=params, timeout=timeout, **kwargs)

    def post(self, url, json=None, data=None, timeout=None, headers=None, **kwargs) -> FakeResponse:
        return self.request("POST", url, json=json, data=data, timeout=timeout, headers=headers, **kwargs)

    def request(self, method: str, url: str, **kwargs) -> FakeResponse:
        call = {"method": method.upper(), "url": url, **kwargs}
        with self._lock:
            self.calls.append(call)
            prefix = max((key for key in self.routes if url.startswith(key)), key=len, default=None)
            route: Any = 404 if prefix is None else self.routes[prefix]
            if isinstance(route, list):
                index = self._used.get(prefix, 0)
                self._used[prefix] = index + 1
                route = route[min(index, len(route) - 1)]
        if callable(route) and not _is_exception(route):
            route = route(method.upper(), url, call)
        return _response(url, route)

    def close(self) -> None:
        pass

    def __enter__(self) -> FakeSession:
        return self

    def __exit__(self, *exc_info: object) -> None:
        pass


def _response(url: str, route: Any) -> FakeResponse:
    if _is_exception(route):
        raise route
    if isinstance(route, FakeResponse):
        if not route.url:
            route.url = url
        return route
    if isinstance(route, int) and not isinstance(route, bool):
        return FakeResponse(url, route)
    if isinstance(route, bytes | str):
        return FakeResponse(url, content=route)
    return FakeResponse(url, json_data=route)


# --- builders ------------------------------------------------------------------------------------------------------


def make_bars(
    closes: Sequence[float],
    start: date = date(2025, 1, 2),
    *,
    volumes: Sequence[int] | None = None,
    volume: int = 1_000_000,
    spread_pct: float = 1.0,
) -> list[PriceBar]:
    """Daily bars on weekdays from start: open = previous close, high/low spread_pct around the open/close range."""
    bars = []
    day = start
    previous = float(closes[0]) if closes else 0.0
    for index, close in enumerate(closes):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        close = float(close)
        bars.append(
            PriceBar(
                day=day,
                open=previous,
                high=max(previous, close) * (1 + spread_pct / 100),
                low=min(previous, close) * (1 - spread_pct / 100),
                close=close,
                volume=int(volumes[index]) if volumes is not None else volume,
            )
        )
        previous = close
        day += timedelta(days=1)
    return bars


_BASE_PRICE = 142.5
_STAT_LEVELS = {  # levels relative to the default price; scaled when price is overridden
    "previous_close": 150.0,
    "high_20d": 165.0,
    "high_52w": 190.0,
    "low_52w": 95.0,
    "sma_50": 155.2,
    "sma_200": 140.1,
}


def make_stats(**overrides: Any) -> PriceStats:
    """A plausible PriceStats for AMD after a 5% drop. Derived fields follow the values you override:

    - price scales the default previous close, highs, lows and averages;
    - change_1d_pct / drawdown_20d_pct / drawdown_52w_pct without the matching level set that level;
    - the percentages and stat_low_6m are computed from the levels unless given.
    """
    price = float(overrides.get("price", _BASE_PRICE))
    scale = price / _BASE_PRICE
    levels = {name: overrides.get(name, value * scale) for name, value in _STAT_LEVELS.items()}
    for level, pct in (
        ("previous_close", "change_1d_pct"),
        ("high_20d", "drawdown_20d_pct"),
        ("high_52w", "drawdown_52w_pct"),
    ):
        if level not in overrides and pct in overrides:
            levels[level] = price / (1 + overrides[pct] / 100)
    if "high_52w" not in overrides:
        levels["high_52w"] = max(levels["high_52w"], levels["high_20d"], price)
    if "low_52w" not in overrides:
        levels["low_52w"] = min(levels["low_52w"], price)
    volatility = float(overrides.get("volatility_pct", 48.0))
    values: dict[str, Any] = {
        "ticker": "AMD",
        "name": "Advanced Micro Devices, Inc.",
        "currency": "USD",
        "exchange": "NasdaqGS",
        "as_of": NOW - timedelta(minutes=15),
        "price": price,
        **levels,
        "change_1d_pct": (price / levels["previous_close"] - 1) * 100,
        "change_5d_pct": -8.0,
        "change_20d_pct": -12.0,
        "drawdown_20d_pct": min(0.0, (price / levels["high_20d"] - 1) * 100),
        "drawdown_52w_pct": min(0.0, (price / levels["high_52w"] - 1) * 100),
        "above_low_52w_pct": max(0.0, (price / levels["low_52w"] - 1) * 100),
        "volatility_pct": volatility,
        "volume_ratio": 2.3,
        "stat_low_6m": price * math.exp(-1.645 * (volatility / 100) * math.sqrt(0.5)),
        "worst_6m_drawdown_pct": -38.5,
    }
    values.update(overrides)
    return PriceStats(**values)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")


def make_article(**overrides: Any) -> Article:
    """A MarketWatch article about AMD published an hour before NOW. The id follows the link (sha1)."""
    title = overrides.get("title", "AMD shares slide after weak data-center guidance")
    link = overrides.get("link", f"https://www.example.com/news/{_slug(title)}")
    values: dict[str, Any] = {
        "id": hashlib.sha1(link.encode()).hexdigest(),
        "source": "marketwatch",
        "source_name": "MarketWatch",
        "title": title,
        "link": link,
        "summary": "Advanced Micro Devices cut its data-center revenue outlook, citing slower cloud spending.",
        "published": NOW - timedelta(hours=1),
        "fetched": NOW - timedelta(minutes=55),
        "title_key": " ".join(re.sub(r"[^\w\s]", " ", title.casefold()).split()),
    }
    values.update(overrides)
    return Article(**values)


def make_impact(**overrides: Any) -> Impact:
    """A direct, negative, magnitude 4 guidance impact on AMD, linked to make_article()'s id by default."""
    values: dict[str, Any] = {
        "article_id": make_article().id,
        "ticker": "AMD",
        "company": "Advanced Micro Devices",
        "relation": "direct",
        "direction": "negative",
        "magnitude": 4,
        "event_type": "guidance",
        "rationale": "Lower data-center guidance cuts expected revenue growth.",
    }
    values.update(overrides)
    return Impact(**values)


def make_analysis(**overrides: Any) -> Analysis:
    """A consistent "temporary fear" analysis for make_stats()'s price of 142.5 (low < entry < price < target)."""
    values: dict[str, Any] = {
        "verdict": "temporary_fear",
        "probability_up_6m": 68,
        "potential_low": 118.0,
        "entry_price": 132.0,
        "target_price": 168.0,
        "confidence": "medium",
        "fear": "Investors fear a slowdown in AI data-center spending.",
        "fundamental_impact": "One quarter of softer guidance; the product roadmap and balance sheet are intact.",
        "thesis": "The drop prices in a lasting slowdown the guidance doesn't support.",
        "risks": ["Hyperscalers cut capex further"],
        "catalysts": ["Next quarter's earnings"],
        "checks": ["Read the earnings call transcript"],
        "warnings": [],
    }
    values.update(overrides)
    return Analysis(**values)


def make_candidate(**overrides: Any) -> Candidate:
    """A Candidate for AMD with one negative direct impact and make_stats()'s 5% drop."""
    ticker = overrides.get("ticker", "AMD")
    article = make_article()
    values: dict[str, Any] = {
        "ticker": ticker,
        "company": "Advanced Micro Devices",
        "stats": overrides.get("stats") or make_stats(ticker=ticker),
        "impacts": [(make_impact(ticker=ticker, article_id=article.id), article)],
        "dip_reasons": ["down 5.0% today", "13.6% below its 20-day high"],
        "severity": 7.5,
    }
    values.update(overrides)
    return Candidate(**values)


def make_opportunity(**overrides: Any) -> Opportunity:
    """An Opportunity for AMD built from make_stats(), make_analysis() and make_article()."""
    ticker = overrides.get("ticker", "AMD")
    stats = overrides.get("stats") or make_stats(ticker=ticker)
    article = make_article()
    values: dict[str, Any] = {
        "ticker": ticker,
        "company": "Advanced Micro Devices",
        "created": NOW,
        "price": stats.price,
        "currency": stats.currency,
        "score": 72.4,
        "analysis": make_analysis(),
        "stats": stats,
        "article_ids": [article.id],
        "headlines": [
            {
                "title": article.title,
                "link": article.link,
                "source": article.source_name,
                "published": article.published.isoformat(),
                "direction": "negative",
                "magnitude": 4,
            }
        ],
        "dip_reasons": ["down 5.0% today", "13.6% below its 20-day high"],
        "model": "fake-model",
        "id": None,
    }
    values.update(overrides)
    return Opportunity(**values)
