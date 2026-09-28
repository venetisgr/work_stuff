"""`dip-scanner serve`: the website and the scanner in one process (one Fly Machine).

make_server_app() builds what `dip-scanner watch` builds (the models, prices, SEC figures, the .env notifiers, the
database) plus the website's recipients, watchlists and currencies (recipients.service_hooks), and the app
(create_app) with a ScannerControl that runs the scanner's watch loop in a background thread from the app's
lifespan. serve() runs it with uvicorn.

A setup problem (no API key, a rejected key, no credit left, a broken setting) stops the scanner only: the website
keeps serving and shows "Stopped: <reason>", and the "dip-scanner stopped" notice goes to the .env channels and to
every admin's own channels (at most once every 12 hours, like the command line's).

Behind Fly.io's proxy, uvicorn takes the scheme and client address from X-Forwarded-Proto and X-Forwarded-For from
any address (forwarded_allow_ips="*"). That is only safe because a Fly Machine can be reached through Fly's proxy
alone; on another host, put the site behind a proxy that overwrites those headers. Rate limits use Fly-Client-IP
instead when FLY_APP_NAME is set (Fly sets both), since the client can put anything first in X-Forwarded-For.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Sequence
from concurrent.futures import Executor
from datetime import UTC, datetime
from typing import Any

import requests
from fastapi import FastAPI

from ..config import DATABASE_NAME, ConfigError, ScannerConfig, Settings
from ..feeds import USER_AGENT
from ..fundamentals import SecFundamentals
from ..fx import FxRates
from ..llm import LLMSetupError, build_models
from ..models import Feed, utc
from ..netguard import Resolver, public_https_session
from ..notices import STOPPED, one_line, scrub, secrets_of, send_notice, stopped_lines
from ..notify import build_notifiers
from ..pipeline import Scanner
from ..prices import YahooPrices
from ..recipients import service_hooks
from ..store import Store
from ..symbols import SymbolResolver
from .app import create_app
from .control import ScannerControl

log = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
GRACEFUL_SHUTDOWN_SECONDS = 15

ScannerFactory = Callable[..., Scanner]


def _now() -> datetime:
    return datetime.now(UTC)


def make_session() -> requests.Session:
    """The HTTP session shared by the scanner and the website (feeds, prices, the SEC, notifications)."""
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    return session


def build_scanner(
    settings: Settings,
    config: ScannerConfig,
    feeds: Sequence[Feed],
    *,
    store: Store,
    session: Any,
    clock: Callable[[], datetime] = _now,
    resolver: Resolver | None = None,
    webhook_session: Any = None,
) -> Scanner:
    """The scanner `serve` runs: like `dip-scanner watch`'s, with the website's users as recipients too (resolver
    looks up their webhooks' hosts before each message; None: the system's DNS; their webhooks go through
    webhook_session, see recipients.user_notifiers). Raises ConfigError or LLMSetupError when the models can't be
    set up (e.g. no API key)."""
    triage_model, analysis_model = build_models(settings.llm)
    fundamentals = None
    if settings.sec_user_agent:
        fundamentals = SecFundamentals(settings.sec_user_agent, session=session, cache_dir=settings.data_dir / "cache")
    notifiers = build_notifiers(settings.notify, session=session)
    return Scanner(
        settings=settings,
        config=config,
        feeds=list(feeds),
        store=store,
        triage_model=triage_model,
        analysis_model=analysis_model,
        prices=YahooPrices(session, clock=clock),
        fundamentals=fundamentals,
        notifiers=notifiers,
        session=session,
        notify=True,
        clock=clock,
        symbols=SymbolResolver(session, store),
        **service_hooks(
            store,
            settings,
            config,
            session,
            default_notifiers=notifiers,
            webhook_session=webhook_session,
            resolver=resolver,
        ),
    )


def stop_notice_sender(
    settings: Settings,
    config: ScannerConfig,
    store: Store,
    session: Any,
    *,
    clock: Callable[[], datetime] = _now,
    resolver: Resolver | None = None,
    webhook_session: Any = None,
) -> Callable[[str], None]:
    """on_stop for ScannerControl: sends "dip-scanner stopped: <reason>" to the .env channels and every admin's own
    (unless [alerts] system_notices = false); at most once every 12 hours (notices.send_notice)."""

    def send(reason: str) -> None:
        if not config.alerts.system_notices:
            return
        now = utc(clock())
        default = build_notifiers(settings.notify, session=session)
        hooks = service_hooks(
            store,
            settings,
            config,
            session,
            default_notifiers=default,
            webhook_session=webhook_session,
            resolver=resolver,
        )
        people = [person for person in hooks["recipients"](now) if person.gets_notices]
        notifiers = [notifier for person in people for notifier in person.notifiers]
        if not notifiers:
            log.info("No admin or .env alert channel is set up to be told that the scanner stopped.")
            return
        send_notice(
            notifiers,
            store,
            kind=STOPPED,
            subject=f"dip-scanner stopped: {one_line(reason, 150)}",
            lines=stopped_lines("serve", one_line(reason), now),
            now=now,
            secrets=secrets_of(settings),
        )

    return send


def make_server_app(
    settings: Settings,
    config: ScannerConfig,
    feeds: Sequence[Feed],
    *,
    scanner_enabled: bool = True,
    session: Any = None,
    scanner_factory: ScannerFactory | None = None,
    clock: Callable[[], datetime] = _now,
    trust_fly_client_ip: bool | None = None,
    job_executor: Executor | None = None,
    resolver: Resolver | None = None,
    webhook_session: Any = None,
) -> FastAPI:
    """The app `serve` runs: the website with the scanner's loop (started and stopped by the app's lifespan), sharing
    the scanner's price and exchange-rate caches, and "Analyse now" running the scanner's analyze_ticker.

    scanner_enabled=False (SCANNER_ENABLED=false or --no-scanner) serves the pages only; "Analyse now" still works
    when the models can be set up. trust_fly_client_ip defaults to whether FLY_APP_NAME is set (on Fly.io). resolver
    looks up webhook hosts (None: the system's DNS). webhook_session: the HTTP session for members' webhooks
    (default: netguard.public_https_session, or session when a test passes one). scanner_factory(settings, config,
    feeds, store=, session=, clock=, resolver=, webhook_session=) builds the scanner (default: build_scanner). Raises
    ConfigError when SECRET_KEY is missing.
    """
    settings.web.require_secret_key()
    if webhook_session is None:
        webhook_session = session if session is not None else public_https_session(resolver)
    session = session if session is not None else make_session()
    database = settings.data_dir / DATABASE_NAME
    scanner_store = Store(database)
    secrets = secrets_of(settings)
    scanner: Scanner | None = None
    reason = None
    factory = scanner_factory or build_scanner
    try:
        scanner = factory(
            settings,
            config,
            feeds,
            store=scanner_store,
            session=session,
            clock=clock,
            resolver=resolver,
            webhook_session=webhook_session,
        )
    except ConfigError as exc:
        reason = f"Configuration problem: {exc}"
    except LLMSetupError as exc:
        reason = f"The language model can't be used: {exc}"
    if reason:
        reason = one_line(scrub(reason, secrets))
    control = ScannerControl(
        scanner,
        scanner_store,
        enabled=scanner_enabled,
        interval_minutes=config.scan.interval_minutes,
        reason=reason,
        on_stop=stop_notice_sender(
            settings, config, scanner_store, session, clock=clock, resolver=resolver, webhook_session=webhook_session
        ),
        clock=clock,
        secrets=secrets,
    )
    prices = scanner.prices if scanner is not None else YahooPrices(session, clock=clock)
    fx = scanner.fx if scanner is not None else FxRates(prices, clock=clock)
    if trust_fly_client_ip is None:
        trust_fly_client_ip = bool(os.environ.get("FLY_APP_NAME"))
    return create_app(
        settings=settings,
        config=config,
        feeds=feeds,
        store_path=database,
        scanner_control=control,
        prices=prices,
        fx=fx,
        clock=clock,
        analyse=scanner.analyze_ticker if scanner is not None else None,
        analyse_unavailable=f"Manual analyses aren't available: {reason}" if reason else None,
        job_executor=job_executor,
        http_session=session,
        webhook_session=webhook_session,
        resolver=resolver,
        trust_fly_client_ip=trust_fly_client_ip,
        on_shutdown=(scanner_store.close, session.close, webhook_session.close),
    )


def serve(
    settings: Settings,
    config: ScannerConfig,
    feeds: Sequence[Feed],
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    scanner_enabled: bool = True,
    session: Any = None,
    run: Callable[..., None] | None = None,
) -> None:
    """Run the website (and the scanner) with uvicorn until it is stopped (Ctrl+C, or SIGTERM from Fly)."""
    app = make_server_app(settings, config, feeds, scanner_enabled=scanner_enabled, session=session)
    for name in ("uvicorn", "uvicorn.error"):  # "Uvicorn running on ..." through the command line's log format
        logging.getLogger(name).setLevel(logging.INFO)
    if run is None:
        import uvicorn

        run = uvicorn.run
    log.info(
        "Serving the website on %s:%d%s.", host, port, "" if scanner_enabled else " without the scanner (pages only)"
    )
    run(
        app,
        host=host,
        port=port,
        proxy_headers=True,
        forwarded_allow_ips="*",  # see the module docstring: only Fly's proxy can reach the Machine
        server_header=False,
        access_log=False,  # WebMiddleware logs requests without link tokens or query strings
        log_config=None,  # keep the command line's log format (and DISPLAY_TZ times)
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )
