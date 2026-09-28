"""What every page handler works with: the website's settings, database, accounts, prices, exchange rates, scanner
control and job runner, gathered in one AppContext that create_app (app.py) stores on the FastAPI app.

A handler gets it with `ctx: AppContext = Depends(get_context)` (or get_context(request)). Everything here is safe to
use from the thread a `def` handler runs in: the Store serialises its connection with a lock, and YahooPrices, FxRates
and TimedCache have locks of their own.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import TYPE_CHECKING, Any, TypeVar

from fastapi import Request

from ..accounts import Accounts, User
from ..config import ConfigError, ScannerConfig, Settings, display_zone
from ..fx import FxRates
from ..models import Feed, Opportunity, utc
from ..netguard import Resolver
from ..prices import YahooPrices
from ..report import for_currency
from ..store import Store

if TYPE_CHECKING:
    from starlette.templating import Jinja2Templates

    from .control import ScannerControl
    from .jobs import JobRunner

T = TypeVar("T")


class TimedCache:
    """A small thread-safe cache whose entries expire after their own number of seconds (e.g. an hour of price
    downloads for the track record). Values are computed outside the lock, so a slow download doesn't block other
    keys; two requests for the same missing key may both compute it, and the last one is kept."""

    def __init__(self, *, max_entries: int = 512, monotonic: Callable[[], float] = time.monotonic) -> None:
        self.max_entries = max_entries
        self._monotonic = monotonic
        self._entries: dict[Hashable, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: Hashable, default: Any = None) -> Any:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return default
            if entry[0] <= self._monotonic():
                del self._entries[key]
                return default
            return entry[1]

    def set(self, key: Hashable, value: Any, *, seconds: float) -> None:
        with self._lock:
            if len(self._entries) >= self.max_entries and key not in self._entries:
                now = self._monotonic()
                for stale in [k for k, (expires, _) in self._entries.items() if expires <= now]:
                    del self._entries[stale]
                if len(self._entries) >= self.max_entries:  # still full: drop the entry that expires first
                    del self._entries[min(self._entries, key=lambda k: self._entries[k][0])]
            self._entries[key] = (self._monotonic() + seconds, value)

    def get_or_set(self, key: Hashable, factory: Callable[[], T], *, seconds: float) -> T:
        """The cached value of key, else factory()'s, kept for seconds. An exception from factory isn't cached."""
        missing = object()
        value = self.get(key, missing)
        if value is missing:
            value = factory()
            self.set(key, value, seconds=seconds)
        return value

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


@dataclass
class AppContext:
    """The website's services. store is the website's own connection to the database (the scanner thread has
    another); accounts works on it. http is the HTTP session for test alerts and other outgoing requests."""

    settings: Settings
    config: ScannerConfig
    feeds: list[Feed]
    store: Store
    accounts: Accounts
    prices: YahooPrices
    fx: FxRates
    control: ScannerControl
    jobs: JobRunner
    templates: Jinja2Templates
    http: Any  # a requests.Session (or a test's fake)
    clock: Callable[[], datetime]
    resolver: Resolver | None = None  # webhook host lookups (None: the system's DNS)
    trust_fly_client_ip: bool = False  # take the client's address from Fly-Client-IP (set by Fly's proxy)
    extra_css: tuple[str, ...] = ()  # pages.css / admin.css when they exist
    cache: TimedCache = field(default_factory=TimedCache)

    def now(self) -> datetime:
        """The current time (aware, UTC)."""
        return utc(self.clock())

    def user_zone(self, user: User | None) -> tzinfo:
        """The time zone a user reads times in: their setting, else DISPLAY_TZ."""
        if user is not None:
            try:
                return display_zone(user.settings.timezone)
            except ConfigError:
                pass
        return self.settings.display_tz

    def view(self, opp: Opportunity, user: User | None) -> Opportunity:
        """opp as the user should see it: "≈" amounts in their currency at the rate stored with the analysis, else at
        today's rate (fetched once and cached, labelled "at today's rate"), else in the trading currency only."""
        currency = user.settings.currency if user is not None else None
        return for_currency(opp, currency, fx=self.fx, now=self.now())


def get_context(request: Request) -> AppContext:
    """The AppContext of the app serving the request (a FastAPI dependency; the annotation must stay resolvable at
    runtime, so Request is imported for real above)."""
    return request.app.state.ctx
