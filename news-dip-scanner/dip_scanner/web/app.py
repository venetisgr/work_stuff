"""The website's FastAPI app: create_app() wires the services (AppContext, context.py), the routers, the security
headers, the error pages, the templates and the static files.

Pages are rendered on the server with Jinja2 (autoescaping on; StrictUndefined, so a misspelled variable is an error
in the tests rather than an empty spot on a page) in the reader's own time zone (report.display_zone_as), and use a
little JavaScript only for niceties: everything works without it. The Content-Security-Policy allows no inline
scripts, styles or event handlers, so templates use classes from static/app.css and data-attributes for app.js.

Route modules (account.py, jobs.py, pages.py, admin.py) import render, redirect and flash from here; create_app imports
them in turn, inside the function, so there is no import cycle.
"""

from __future__ import annotations

import base64
import binascii
import functools
import hashlib
import json
import logging
import re
import sqlite3
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Executor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode, urlsplit

import requests
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, StrictUndefined, pass_context
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..accounts import Accounts, UserSettings
from ..config import ScannerConfig, Settings, zone_name
from ..feeds import USER_AGENT
from ..fx import FxRates
from ..models import VERDICTS, Feed, Opportunity, from_iso, utc
from ..netguard import Resolver
from ..pipeline import format_tokens
from ..prices import YahooPrices
from ..report import (
    display_zone_as,
    format_clock,
    format_money,
    format_pct,
    format_price,
    format_when,
    fx_text,
    in_account,
    local_time,
    safe_url,
    score_band,
    verdict_label,
)
from ..store import Store
from . import auth
from .context import AppContext, get_context
from .control import ScannerControl

if TYPE_CHECKING:
    from .jobs import Analyse

log = logging.getLogger(__name__)
access_log = logging.getLogger("dip_scanner.web.access")

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"
APP_NAME = "Dip scanner"
FOOTER = "Not investment advice. The scanner never trades."
FEATURE_CSS = ("pages.css", "admin.css")  # included by base.html when they exist (the pages' and admin's own styles)

CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; object-src 'none'; "
    "frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
}
HSTS = "max-age=31536000"  # no includeSubDomains: BASE_URL may be a subdomain of someone's own domain
STATIC_CACHE = "public, max-age=86400"
MAX_BODY_BYTES = 1_000_000  # a request body over this is refused (413): every form here is a few kilobytes
FLASH_KINDS = ("ok", "error", "info")
MAX_FLASHES = 4
MAX_FLASH_LENGTH = 500
FLASH_LIFETIME = timedelta(minutes=10)
ERROR_TITLES = {
    400: "That request isn't valid",
    403: "Not allowed",
    404: "Page not found",
    405: "Not allowed",
    429: "Too many attempts",
    500: "Something went wrong",
    503: "Not available right now",
}
ERROR_MESSAGES = {
    400: "The request couldn't be understood. Go back and try again.",
    403: "You can't open this page.",
    404: "There is no page at this address, or it was removed.",
    405: "This address doesn't take that kind of request.",
    429: "Too many attempts. Wait a few minutes and try again.",
    500: "The page couldn't be shown because of an error on the server. It was logged; try again in a moment.",
    503: "This part of the site isn't available right now. Try again in a moment.",
}
_TOKEN_PATH = re.compile(r"^/(invite|password)/[^/]+")


def _now() -> datetime:
    return datetime.now(UTC)


# --- templates -----------------------------------------------------------------------------------------------------


def _moment(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return utc(value)
    try:
        return from_iso(str(value))
    except ValueError:
        return None


def relative_time(value: object, now: datetime) -> str:
    """ "just now", "5 min ago", "3 h ago", "2 days ago", "in 4 min"; "–" when there is no time."""
    moment = _moment(value)
    if moment is None:
        return "–"
    seconds = (utc(now) - moment).total_seconds()
    ahead, seconds = seconds < 0, abs(seconds)
    if seconds < 45:
        return "just now"
    if seconds < 90 * 60:
        text = f"{max(1, round(seconds / 60))} min"
    elif seconds < 36 * 3600:
        text = f"{round(seconds / 3600)} h"
    else:
        days = round(seconds / 86400)
        text = f"{days} day{'s' if days != 1 else ''}"
    return f"in {text}" if ahead else f"{text} ago"


def _dash_or(func: Callable[..., str]) -> Callable[..., str]:
    """A filter that shows "–" for a missing value."""

    @functools.wraps(func)
    def wrapped(value: Any, *args: Any, **kwargs: Any) -> str:
        if value is None or value == "":
            return "–"
        return func(value, *args, **kwargs)

    return wrapped


@_dash_or
def _money(value: float, opp: Opportunity) -> str:
    return format_money(float(value), opp)


@_dash_or
def _price(value: float, currency: str | None = None) -> str:
    return format_price(float(value), currency)


def _approx(value: float | None, opp: Opportunity) -> str:
    return in_account(float(value), opp).strip() if value is not None else ""


@_dash_or
def _pct(value: float) -> str:
    return format_pct(float(value))


@_dash_or
def _percent(value: float, digits: int = 0) -> str:
    return f"{float(value):.{digits}f}%"


@_dash_or
def _number(value: float, digits: int = 0) -> str:
    return f"{float(value):,.{digits}f}"


@_dash_or
def _score(value: float) -> str:
    return f"{float(value):.1f}"


def _band(score: float | None) -> str:
    return score_band(float(score))[1] if score is not None else "weak"


def _verdict(value: str | None) -> str:
    return verdict_label(value) if value else "–"


def _when(value: object) -> str:
    moment = _moment(value)
    return format_when(moment) if moment is not None else "–"


def _clock(value: object) -> str:
    moment = _moment(value)
    return format_clock(moment) if moment is not None else "–"


def _day(value: object) -> str:
    moment = _moment(value)
    return f"{local_time(moment):%a %d %b %Y}" if moment is not None else "–"


def _iso(value: object) -> str:
    moment = _moment(value)
    return moment.isoformat(timespec="seconds") if moment is not None else ""


@pass_context
def _ago(context: Any, value: object) -> str:
    return relative_time(value, context.get("now") or _now())


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count:,} {singular if count == 1 else plural or singular + 's'}"


def _fx_note(opp: Opportunity) -> str:
    return fx_text(opp) or ""


def _host(url: object) -> str:
    link = safe_url(url)
    if not link:
        return ""
    host = urlsplit(link).hostname or ""
    return host.removeprefix("www.")


@functools.cache
def _static_version(name: str) -> str | None:
    path = (STATIC_DIR / name).resolve()
    if STATIC_DIR not in path.parents or not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()[:10]


def static_url(name: str) -> str:
    """/static/<name>?v=<content hash>, so a new version of a file is never read from a stale cache."""
    version = _static_version(name)
    return f"/static/{name}" + (f"?v={version}" if version else "")


def url_with(request: Request, **changes: Any) -> str:
    """The current page's path and query with some parameters changed (None, "" or False removes one; a list gives
    it several values): links for filters and pages, e.g. url_with(request, page=2) or url_with(request, days=7,
    page=None)."""
    params = [(key, value) for key, value in request.query_params.multi_items() if key not in changes]
    for key, value in changes.items():
        if value is None or value is False or value == "":
            continue
        if isinstance(value, list | tuple):
            params.extend((key, str(item)) for item in value)
        else:
            params.append((key, str(value)))
    query = urlencode(params)
    return request.url.path + (f"?{query}" if query else "")


FILTERS: dict[str, Callable[..., Any]] = {
    "money": _money,  # {{ value|money(opp) }}: "$132.00 ≈ €115.93" (opp from ctx.view: the reader's currency)
    "price": _price,  # {{ value|price("USD") }}: "$132.00", "245.60p", "1,234.00 JPY"
    "approx": _approx,  # {{ value|approx(opp) }}: "≈ €115.93" in the reader's currency, or ""
    "pct": _pct,  # signed: "+17.9%", "-5.0%"
    "percent": _percent,  # unsigned: "68%" ({{ 68|percent }}), "12.5%" ({{ x|percent(1) }})
    "number": _number,  # "1,234" / "1,234.5" with digits
    "score": _score,  # "72.4"
    "band": _band,  # "strong" | "good" | "fair" | "weak" (report.SCORE_BANDS)
    "verdict": _verdict,  # "Temporary fear"
    "when": _when,  # "2026-09-25 18:00 EEST" in the reader's time zone
    "clock": _clock,  # "18:00 EEST"
    "day": _day,  # "Fri 25 Sep 2026"
    "iso": _iso,  # for <time datetime="...">
    "ago": _ago,  # "5 min ago", "in 3 min"
    "plural": _plural,  # {{ n|plural("idea") }}: "1 idea", "3 ideas"
    "fx_note": _fx_note,  # the exchange rate behind the ≈ amounts, or ""
    "tokens": format_tokens,  # "12.3k"
    "safe_url": safe_url,  # the URL when it is http(s), else None
    "host": _host,  # "reuters.com"
}


def make_templates() -> Jinja2Templates:
    """The Jinja2 environment of the website: autoescaping always on, StrictUndefined, the FILTERS and a few
    globals (static, url_with, app_name, footer, verdict_choices)."""
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(FILTERS)
    env.globals.update(
        static=static_url,
        url_with=url_with,
        app_name=APP_NAME,
        footer=FOOTER,
        verdict_choices=[(verdict, verdict_label(verdict)) for verdict in VERDICTS],
    )
    return Jinja2Templates(env=env)


def render(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> Response:
    """A page from templates/name in the reader's time zone. Call it from `def` handlers (it reads the database).

    Every template gets: request, user (None when signed out), session, is_admin, csrf_token (the token its forms
    must carry), now (UTC), tz and tz_name (the reader's zone), currency (the reader's "≈" currency or None), flashes
    ([(kind, text)] from redirect/flash), nav (the active menu item: "ideas", "news", "track", "settings", "admin"),
    page_title and extra_css; context adds to or overrides them.
    """
    ctx = get_context(request)
    session = auth.load_session(request)
    user = session.user if session is not None else None
    zone = ctx.user_zone(user)
    data: dict[str, Any] = {
        "request": request,
        "user": user,
        "session": session,
        "is_admin": bool(user is not None and user.is_admin),
        "csrf_token": session.csrf if session is not None else auth.form_token(request),
        "now": ctx.now(),
        "tz": zone,
        "tz_name": zone_name(zone),
        "currency": user.settings.currency if user is not None else None,
        "flashes": take_flashes(request),
        "nav": None,
        "page_title": None,
        "extra_css": ctx.extra_css,
    }
    data.update(context or {})
    with display_zone_as(zone):
        return ctx.templates.TemplateResponse(request, name, data, status_code=status_code, headers=headers)


def flash(request: Request, message: str, kind: str = "ok") -> None:
    """Show message on the next page rendered for this visitor (after a redirect). kind: "ok", "error" or "info".
    Kept in a cookie signed with SECRET_KEY for up to 10 minutes; the text is escaped when shown."""
    if kind not in FLASH_KINDS:
        raise ValueError(f"Unknown flash kind {kind!r}; use one of {', '.join(FLASH_KINDS)}.")
    ctx = get_context(request)
    pending = list(getattr(request.state, "dip_flash_out", []))
    pending.append((kind, " ".join(str(message).split())[:MAX_FLASH_LENGTH]))
    pending = pending[-MAX_FLASHES:]
    request.state.dip_flash_out = pending
    payload = base64.urlsafe_b64encode(json.dumps(pending).encode("utf-8")).decode("ascii").rstrip("=")
    auth.queue_cookie(request, auth.FLASH_COOKIE, auth.signed(ctx, "flash", payload), max_age=FLASH_LIFETIME)


def take_flashes(request: Request) -> list[tuple[str, str]]:
    """The messages flashed for this visitor (once: the cookie is deleted when they are shown)."""
    found = getattr(request.state, "dip_flash_in", None)
    if found is not None:
        return found
    messages: list[tuple[str, str]] = []
    cookie = request.cookies.get(auth.FLASH_COOKIE)
    if cookie:
        payload = auth.unsigned(get_context(request), "flash", cookie)
        if payload:
            try:
                items = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            except (ValueError, binascii.Error):
                items = []
            for item in items if isinstance(items, list) else []:
                if isinstance(item, list) and len(item) == 2 and item[0] in FLASH_KINDS and isinstance(item[1], str):
                    messages.append((item[0], item[1][:MAX_FLASH_LENGTH]))
        if not getattr(request.state, "dip_flash_out", None):
            auth.queue_cookie(request, auth.FLASH_COOKIE, None)
    request.state.dip_flash_in = messages
    return messages


def redirect(request: Request, url: str, message: str | None = None, *, kind: str = "ok") -> RedirectResponse:
    """A 303 redirect to a path on this site (after a POST), with an optional flash message for the next page."""
    if message:
        flash(request, message, kind)
    return RedirectResponse(auth.safe_next(url), status_code=303)


# --- pagination ----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Page:
    """One page of a list: number (from 1), size, total items. offset and the has_* flags drive the query and the
    pagination macro (_macros.html: ui.pagination(page))."""

    number: int
    size: int
    total: int

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.size))

    @property
    def offset(self) -> int:
        return (self.number - 1) * self.size

    @property
    def has_previous(self) -> bool:
        return self.number > 1

    @property
    def has_next(self) -> bool:
        return self.number < self.pages

    def slice(self, items: Sequence) -> list:
        return list(items[self.offset : self.offset + self.size])


def paginate(total: int, page: object = 1, size: int = 25) -> Page:
    """The Page for a page number from the query string (anything that isn't a valid number means 1; past the end
    means the last page)."""
    try:
        number = int(str(page))
    except ValueError:
        number = 1
    result = Page(number=1, size=max(1, size), total=max(0, total))
    return Page(number=min(max(1, number), result.pages), size=result.size, total=result.total)


# --- errors --------------------------------------------------------------------------------------------------------


def error_page(
    request: Request,
    status: int,
    message: str | None = None,
    *,
    headers: dict[str, str] | None = None,
    signed_out: bool = False,
) -> Response:
    """The friendly page for an HTTP error (errors/error.html): a title, message (our own words, or the default
    for the status) and a way back. Never a stack trace. signed_out: don't look up the visitor (for server errors,
    when the database may be what failed)."""
    title = ERROR_TITLES.get(status, HTTPStatus(status).phrase if status in HTTPStatus._value2member_map_ else "Error")
    text = message or ERROR_MESSAGES.get(status, "Something went wrong.")
    if signed_out:
        auth.assume_signed_out(request)
    try:
        return render(
            request,
            "errors/error.html",
            {"status": status, "title": title, "message": text, "page_title": title},
            status_code=status,
            headers=headers,
        )
    except Exception:
        log.exception("Couldn't render the error page for %s", status)
        return PlainTextResponse(f"{status} {title}\n\n{text}\n", status_code=status, headers=headers)


def _custom_detail(exc: StarletteHTTPException) -> str | None:
    detail = exc.detail
    if not isinstance(detail, str) or not detail:
        return None
    try:
        if detail == HTTPStatus(exc.status_code).phrase:
            return None
    except ValueError:
        pass
    return detail


async def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
    headers = dict(exc.headers) if exc.headers else None
    return await run_in_threadpool(error_page, request, exc.status_code, _custom_detail(exc), headers=headers)


async def _validation_error(request: Request, exc: RequestValidationError) -> Response:
    # A path parameter that isn't what the route takes (/jobs/abc) is a page that doesn't exist.
    in_path = any((error.get("loc") or ("",))[0] == "path" for error in exc.errors())
    return await run_in_threadpool(error_page, request, 404 if in_path else 400)


async def _login_required(request: Request, exc: auth.LoginRequired) -> Response:
    return RedirectResponse(login_url(exc.next_url), status_code=303)


async def _server_error(request: Request, exc: Exception) -> Response:
    log.error("Error on %s %s", request.method, redact_path(request.url.path), exc_info=exc)
    response = await run_in_threadpool(error_page, request, 500, signed_out=True)
    # This response doesn't pass through WebMiddleware (Starlette sends it from its outermost layer).
    _security_headers(response.headers, request.scope)
    return response


# --- middleware ----------------------------------------------------------------------------------------------------


def redact_path(path: str) -> str:
    """A path for the log: the token of an invite or password link replaced by "…" (tokens are never logged)."""
    return _TOKEN_PATH.sub(lambda match: f"/{match.group(1)}/…", path)


def _security_headers(headers: MutableHeaders, scope: Scope) -> None:
    for name, value in SECURITY_HEADERS.items():
        if name not in headers:
            headers[name] = value
    if scope.get("scheme") == "https" and "strict-transport-security" not in headers:
        headers["Strict-Transport-Security"] = HSTS
    if "cache-control" not in headers:
        headers["Cache-Control"] = STATIC_CACHE if str(scope.get("path", "")).startswith("/static/") else "no-store"


def _write_cookies(headers: MutableHeaders, scope: Scope) -> None:
    queued = auth.queued_cookies(scope.get("state") or {})
    if not queued:
        return
    app = scope.get("app")
    ctx: AppContext | None = getattr(getattr(app, "state", None), "ctx", None)
    secure = ctx.settings.web.cookie_secure if ctx is not None else True
    already = {value.split("=", 1)[0].strip() for value in headers.getlist("set-cookie")}
    for name, (value, max_age) in queued.items():
        if name in already:
            continue
        cookie = Response()
        if value is None:
            cookie.delete_cookie(name, path="/", secure=secure, httponly=True, samesite="lax")
        else:
            cookie.set_cookie(name, value, max_age=max_age, path="/", secure=secure, httponly=True, samesite="lax")
        headers.append("set-cookie", cookie.headers["set-cookie"])


class WebMiddleware:
    """Adds the security headers (CSP, nosniff, Referrer-Policy, Permissions-Policy, frame blocking; HSTS over
    https), Cache-Control (no-store for pages), the cookies the handlers queued (auth.queue_cookie), refuses bodies
    over MAX_BODY_BYTES (413), and logs one line per request without query strings or link tokens."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        status = 500
        length = dict(scope.get("headers") or []).get(b"content-length", b"0")
        if not length.isdigit() or int(length) > MAX_BODY_BYTES:
            status = 413
            response = PlainTextResponse("That request is too large.\n", status_code=413)
            _security_headers(response.headers, scope)
            await response(scope, receive, send)
            _log_access(scope, status, started)
            return

        async def send_with_headers(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = MutableHeaders(scope=message)
                _security_headers(headers, scope)
                _write_cookies(headers, scope)
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        finally:
            _log_access(scope, status, started)


def _log_access(scope: Scope, status: int, started: float) -> None:
    path = str(scope.get("path", ""))
    quiet = path.startswith("/static/") or path in ("/healthz", "/favicon.ico", "/robots.txt")
    if quiet and status < 400:
        return
    headers = dict(scope.get("headers") or [])
    app = scope.get("app")
    ctx: AppContext | None = getattr(getattr(app, "state", None), "ctx", None)
    client = (headers.get(b"fly-client-ip") or b"").decode("latin-1") if ctx and ctx.trust_fly_client_ip else ""
    if not client and scope.get("client"):
        client = str(scope["client"][0])
    elapsed = (time.monotonic() - started) * 1000
    access_log.info("%s %s %s %d %.0f ms", client or "-", scope.get("method", "?"), redact_path(path), status, elapsed)


# --- the app -------------------------------------------------------------------------------------------------------


def _http_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    return session


def create_app(
    *,
    settings: Settings,
    config: ScannerConfig,
    feeds: Sequence[Feed],
    store_path: Path | str,
    scanner_control: ScannerControl | None = None,
    prices: YahooPrices | None = None,
    fx: FxRates | None = None,
    clock: Callable[[], datetime] | None = None,
    analyse: Analyse | None = None,
    analyse_unavailable: str | None = None,
    job_executor: Executor | None = None,
    http_session: Any = None,
    resolver: Resolver | None = None,
    trust_fly_client_ip: bool = False,
    on_shutdown: Sequence[Callable[[], None]] = (),
) -> FastAPI:
    """The website. Everything it talks to can be passed in (tests pass fakes); what isn't is built from settings.

    store_path: the SQLite database (the website opens its own connection). scanner_control: the scanner thread's
    control (server.py builds it; None: the scanner is "disabled" here). prices / fx: Yahoo clients (the scanner's in
    `serve`, so their caches are shared). analyse(ticker, now) -> Opportunity: what "Analyse now" runs (the
    scanner's analyze_ticker; None: not available, analyse_unavailable says why). job_executor: where the analyses
    run (default: one background thread). http_session: for test alerts. resolver: webhook host lookups.
    trust_fly_client_ip: take the visitor's address from Fly-Client-IP (on Fly.io). on_shutdown: called when the
    app stops, after the scanner's loop.

    Raises ConfigError when SECRET_KEY is missing or too short.
    """
    settings.web.require_secret_key()
    clock = clock or _now
    store = Store(Path(store_path))
    accounts = Accounts(store, defaults=UserSettings.defaults(config, settings), clock=clock)
    http = http_session if http_session is not None else _http_session()
    prices = prices if prices is not None else YahooPrices(http, clock=clock)
    fx = fx if fx is not None else FxRates(prices, clock=clock)
    if scanner_control is None:
        scanner_control = ScannerControl(
            None, store, enabled=False, interval_minutes=config.scan.interval_minutes, clock=clock
        )
    from .jobs import JobRunner

    jobs = JobRunner(
        accounts,
        analyse=analyse,
        settings=settings,
        clock=clock,
        executor=job_executor,
        unavailable=analyse_unavailable,
    )
    ctx = AppContext(
        settings=settings,
        config=config,
        feeds=list(feeds),
        store=store,
        accounts=accounts,
        prices=prices,
        fx=fx,
        control=scanner_control,
        jobs=jobs,
        templates=make_templates(),
        http=http,
        clock=clock,
        resolver=resolver,
        trust_fly_client_ip=trust_fly_client_ip,
        extra_css=tuple(name for name in FEATURE_CSS if (STATIC_DIR / name).is_file()),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await run_in_threadpool(_start, ctx)
        try:
            yield
        finally:
            await run_in_threadpool(_stop, ctx, on_shutdown)

    app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.ctx = ctx
    app.add_middleware(WebMiddleware)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(auth.LoginRequired, _login_required)
    app.add_exception_handler(Exception, _server_error)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.add_api_route("/healthz", healthz, methods=["GET"], include_in_schema=False)
    app.add_api_route("/robots.txt", robots, methods=["GET"], include_in_schema=False)
    app.add_api_route("/favicon.ico", favicon, methods=["GET"], include_in_schema=False)

    from . import account, admin, pages
    from . import jobs as job_pages

    for router in (account.router, job_pages.router, pages.router, admin.router):
        app.include_router(router)
    return app


def _start(ctx: AppContext) -> None:
    failed = ctx.accounts.fail_unfinished_jobs()
    if failed:
        log.info("Marked %d unfinished manual analyses as failed (the website restarted).", failed)
    ctx.control.start()


def _stop(ctx: AppContext, on_shutdown: Sequence[Callable[[], None]]) -> None:
    ctx.control.shutdown()
    ctx.jobs.shutdown()
    for callback in on_shutdown:
        try:
            callback()
        except Exception as exc:  # the others still run
            log.warning("A shutdown step failed: %s", exc)
    ctx.store.close()


def healthz(request: Request) -> JSONResponse:
    """For Fly's health check and uptime monitors: 200 unless the database can't be read (503).

    {"status": "ok", "db": "ok", "scanner": "running|paused|stopped|disabled|stalled", "last_cycle": ISO time|null}
    """
    ctx = get_context(request)
    try:
        ctx.store.query("SELECT COUNT(*) FROM app_state")
        db_ok = True
    except sqlite3.Error as exc:
        log.error("Health check: the database can't be read: %s", exc)
        db_ok = False
    status = ctx.control.status(now=ctx.now())
    last = status.last_cycle_at
    body = {
        "status": "ok" if db_ok else "error",
        "db": "ok" if db_ok else "error",
        "scanner": status.state,
        "last_cycle": last.isoformat(timespec="seconds") if last is not None else None,
    }
    return JSONResponse(body, status_code=200 if db_ok else 503)


def robots(request: Request) -> PlainTextResponse:
    """An invite-only site: no search engine needs to index it."""
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


def favicon(request: Request) -> FileResponse:
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")


def login_url(next_url: str) -> str:
    """/login?next=<next_url> (quoted), or /login for the home page."""
    return "/login" if next_url in ("", "/") else "/login?" + urlencode({"next": next_url})
