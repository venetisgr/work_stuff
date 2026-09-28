"""Who is asking, and whether they may: the session cookie, CSRF protection, the Origin check on every POST, the
dependencies pages use (current_user, require_user, require_admin, checked_form, public_form) and the rate limits of
the sign-in and link pages.

Sessions: the dsid cookie holds a random 32-byte token (HttpOnly, Secure unless COOKIE_SECURE=false, SameSite=Lax,
Path=/, 30 days, renewed on every visit); the database keeps only its SHA-256 (accounts.py). A session whose user was
disabled ends on their next request, and the cookie is cleared.

CSRF, in two layers:

1. every state-changing request is a POST whose form carries a token, compared in constant time: the session's own
   token (Session.csrf) once signed in, else a double-submit token (a random value signed with SECRET_KEY, sent both
   as the dcsrf cookie and as the form's hidden field) on the sign-in, invite and password forms;
2. a POST whose Origin (or, without one, Referer) is another site than BASE_URL (without BASE_URL: the address the
   request came to) is refused.

Cookies are written by the middleware in app.py from what the handlers queue with queue_cookie, start_session and
end_session, so a redirect, a page and an error page all get them the same way.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
from datetime import timedelta
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData

from ..accounts import SESSION_LIFETIME, Session, User, new_token, same_token
from .context import AppContext, get_context

SESSION_COOKIE = "dsid"
FORM_COOKIE = "dcsrf"  # the double-submit token of the forms used before signing in
FLASH_COOKIE = "dflash"  # messages for the next page (app.py), signed like the form token
CSRF_FIELD = "csrf_token"  # the hidden field every form carries
FORM_TOKEN_LIFETIME = timedelta(days=1)
# Visits of invite and password links per IP address: the tokens can't be guessed, this keeps anyone from trying.
TOKEN_PAGE_LIMIT = 30
TOKEN_PAGE_WINDOW = timedelta(minutes=15)
MAX_NEXT_LENGTH = 1000

CROSS_SITE = "This form was sent from another site, so it was refused. Open the page on this site and try again."
FORM_EXPIRED = (
    "This form has expired or didn't come from this site. Go back, reload the page and try again (the site needs "
    "cookies)."
)

_SESSION_KEY = "dip_session"  # request.state: the Session (or None) of this request, once looked up
_FORM_TOKEN_KEY = "dip_form_token"
_COOKIES_KEY = "dip_cookies"  # request.state: {name: (value or None to delete, max_age seconds)}
_MISSING = object()


class LoginRequired(Exception):
    """Raised by require_user: the visitor is sent to /login, and back to next_url after signing in."""

    def __init__(self, next_url: str = "/") -> None:
        super().__init__(next_url)
        self.next_url = next_url


def too_many(message: str, *, retry_after: timedelta | None = None) -> HTTPException:
    """A 429 error for a rate limit, with the message shown to the user (and Retry-After when given)."""
    headers = {"Retry-After": str(int(retry_after.total_seconds()))} if retry_after else None
    return HTTPException(status_code=429, detail=message, headers=headers)


# --- where a request comes from ------------------------------------------------------------------------------------


def client_ip(request: Request) -> str | None:
    """The visitor's IP address: Fly-Client-IP when the app runs on Fly.io (its proxy sets it; X-Forwarded-For can
    be forged by the client), else the address uvicorn reports."""
    ctx = get_context(request)
    if ctx.trust_fly_client_ip:
        value = request.headers.get("fly-client-ip", "").strip()
        if _is_ip(value):
            return value
    return request.client.host if request.client else None


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def safe_next(value: object, default: str = "/") -> str:
    """value when it is a path on this site ("/ideas/3?x=1"), else default: never another site ("//evil.com",
    "https://evil.com", "/\\evil.com"), so the redirect after signing in can't be abused."""
    if not isinstance(value, str):
        return default
    value = value.strip()
    if (
        not value.startswith("/")
        or value.startswith("//")
        or len(value) > MAX_NEXT_LENGTH
        or "\\" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        return default
    try:
        parts = urlsplit(value)
    except ValueError:
        return default
    if parts.scheme or parts.netloc:
        return default
    return value


def _origin_of(url: str) -> tuple[str, str, int] | None:
    """(scheme, host, port) of an http(s) URL or Origin header, None when it isn't one."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        return None
    return scheme, parts.hostname.lower().rstrip("."), port or (443 if scheme == "https" else 80)


def _site_origin(request: Request, ctx: AppContext) -> tuple[str, str, int] | None:
    if ctx.settings.web.base_url:
        return _origin_of(ctx.settings.web.base_url)
    host = request.headers.get("host", "")
    return _origin_of(f"{request.url.scheme}://{host}") if host else None


def check_origin(request: Request) -> None:
    """Refuse (403) a request whose Origin, or else Referer, header names another site than this one. Browsers send
    Origin with every cross-site POST; a request with neither header passes (the CSRF token still has to match)."""
    ctx = get_context(request)
    site = _site_origin(request, ctx)
    origin = request.headers.get("origin")
    if origin is not None:
        if site is None or _origin_of(origin) != site:
            raise HTTPException(status_code=403, detail=CROSS_SITE)
        return
    referer = request.headers.get("referer")
    if referer and (site is None or _origin_of(referer) != site):
        raise HTTPException(status_code=403, detail=CROSS_SITE)


# --- cookies -------------------------------------------------------------------------------------------------------


def queue_cookie(request: Request, name: str, value: str | None, *, max_age: timedelta | None = None) -> None:
    """Have the response set cookie name to value (None: delete it). The middleware writes it with the site's flags:
    HttpOnly, SameSite=Lax, Path=/, and Secure unless COOKIE_SECURE=false. A later call for the same name wins."""
    cookies = getattr(request.state, _COOKIES_KEY, None)
    if cookies is None:
        cookies = {}
        setattr(request.state, _COOKIES_KEY, cookies)
    cookies[name] = (value, int(max_age.total_seconds()) if max_age is not None else None)


def queued_cookies(state: dict) -> dict[str, tuple[str | None, int | None]]:
    """The cookies queued for a request, from its ASGI scope's "state" (for the middleware)."""
    return dict(state.get(_COOKIES_KEY) or {})


def sign(ctx: AppContext, purpose: str, value: str) -> str:
    """An HMAC-SHA256 of value with SECRET_KEY, for purpose ("form", "flash"), URL-safe base64."""
    key = (ctx.settings.web.secret_key or "").encode("utf-8")
    digest = hmac.new(key, f"{purpose}:{value}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def signed(ctx: AppContext, purpose: str, value: str) -> str:
    """value.signature"""
    return f"{value}.{sign(ctx, purpose, value)}"


def unsigned(ctx: AppContext, purpose: str, text: str | None) -> str | None:
    """The value of a value.signature string whose signature is right, else None."""
    if not text or "." not in text:
        return None
    value, _, signature = text.rpartition(".")
    return value if same_token(sign(ctx, purpose, value), signature) else None


# --- sessions ------------------------------------------------------------------------------------------------------


def load_session(request: Request) -> Session | None:
    """The request's session (looked up once per request), or None. A cookie that no longer matches a session (signed
    out, expired, the user disabled) is deleted; a valid one is renewed for another 30 days."""
    found = getattr(request.state, _SESSION_KEY, _MISSING)
    if found is not _MISSING:
        return found
    token = request.cookies.get(SESSION_COOKIE)
    session = None
    if token:
        session = get_context(request).accounts.get_session(token)
        if session is not None:
            queue_cookie(request, SESSION_COOKIE, token, max_age=SESSION_LIFETIME)
        else:
            queue_cookie(request, SESSION_COOKIE, None)
    setattr(request.state, _SESSION_KEY, session)
    return session


def start_session(request: Request, user: User) -> Session:
    """Sign user in: a new session, its cookie set on the response."""
    ctx = get_context(request)
    user_agent = request.headers.get("user-agent")
    token, session = ctx.accounts.create_session(user.id, ip=client_ip(request), user_agent=user_agent)
    queue_cookie(request, SESSION_COOKIE, token, max_age=SESSION_LIFETIME)
    setattr(request.state, _SESSION_KEY, session)
    return session


def end_session(request: Request) -> None:
    """Sign out: the session ends in the database and its cookie is deleted."""
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        get_context(request).accounts.revoke_session(token)
    queue_cookie(request, SESSION_COOKIE, None)
    setattr(request.state, _SESSION_KEY, None)


def assume_signed_out(request: Request) -> None:
    """Treat this request as signed out without looking at the database (the page of a server error)."""
    if getattr(request.state, _SESSION_KEY, _MISSING) is _MISSING:
        setattr(request.state, _SESSION_KEY, None)


def session_token(request: Request) -> str | None:
    """The raw token of the request's session cookie (e.g. revoke_sessions(keep=...))."""
    return request.cookies.get(SESSION_COOKIE)


def form_token(request: Request) -> str:
    """The double-submit token for a form shown before signing in: the dcsrf cookie's when it is valid, else a new
    one (the cookie is set on the response)."""
    found = getattr(request.state, _FORM_TOKEN_KEY, None)
    if found:
        return found
    ctx = get_context(request)
    cookie = request.cookies.get(FORM_COOKIE)
    if cookie and unsigned(ctx, "form", cookie):
        token = cookie
    else:
        token = signed(ctx, "form", new_token())
        queue_cookie(request, FORM_COOKIE, token, max_age=FORM_TOKEN_LIFETIME)
    setattr(request.state, _FORM_TOKEN_KEY, token)
    return token


def csrf_token(request: Request) -> str:
    """The token a form on this page must carry: the session's when signed in, else the double-submit token."""
    session = load_session(request)
    return session.csrf if session is not None else form_token(request)


# --- dependencies --------------------------------------------------------------------------------------------------


def current_session(request: Request) -> Session | None:
    """Dependency: the visitor's session, or None."""
    return load_session(request)


def current_user(request: Request) -> User | None:
    """Dependency: the signed-in user, or None."""
    session = load_session(request)
    return session.user if session is not None else None


def require_user(request: Request) -> User:
    """Dependency: the signed-in user; anyone else is sent to /login (and back here afterwards)."""
    session = load_session(request)
    if session is None:
        raise LoginRequired(_next_after_login(request))
    return session.user


def require_admin(user: Annotated[User, Depends(require_user)]) -> User:
    """Dependency: the signed-in user when they are an admin; 403 for members."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="This page is for admins only.")
    return user


def _next_after_login(request: Request) -> str:
    if request.method in ("GET", "HEAD"):
        query = f"?{request.url.query}" if request.url.query else ""
        return safe_next(request.url.path + query)
    referer = request.headers.get("referer") or ""
    try:
        parts = urlsplit(referer)
    except ValueError:
        return "/"
    site = _site_origin(request, get_context(request))
    if referer and _origin_of(referer) == site:
        return safe_next(parts.path + (f"?{parts.query}" if parts.query else ""))
    return "/"


async def checked_form(request: Request) -> FormData:
    """Dependency for the POST handlers of signed-in pages: the submitted form, once the Origin check passed, the
    visitor is signed in (else they are sent to /login) and its csrf_token is the session's (else 403)."""
    check_origin(request)
    session = await run_in_threadpool(load_session, request)
    if session is None:
        raise LoginRequired(_next_after_login(request))
    form = await request.form()
    if not same_token(session.csrf, _field(form, CSRF_FIELD)):
        raise HTTPException(status_code=403, detail=FORM_EXPIRED)
    return form


async def public_form(request: Request) -> FormData:
    """Dependency for the POST handlers of the sign-in, invite and password forms: the submitted form, once the Origin
    check passed and its csrf_token matches the signed dcsrf cookie (double submit; else 403)."""
    check_origin(request)
    form = await request.form()
    ctx = get_context(request)
    cookie = request.cookies.get(FORM_COOKIE)
    if not cookie or not unsigned(ctx, "form", cookie) or not same_token(cookie, _field(form, CSRF_FIELD)):
        raise HTTPException(status_code=403, detail=FORM_EXPIRED)
    return form


def _field(form: FormData, name: str) -> str | None:
    value = form.get(name)
    return value if isinstance(value, str) else None


def limit_link_pages(request: Request) -> None:
    """Count a visit of an invite or password link; 429 after TOKEN_PAGE_LIMIT from one address in 15 minutes."""
    ctx = get_context(request)
    key = f"tokens:{client_ip(request) or 'unknown'}"
    if ctx.accounts.rate_limited(key, limit=TOKEN_PAGE_LIMIT, window=TOKEN_PAGE_WINDOW):
        raise too_many(
            "Too many invite or password links were opened from your network. Wait 15 minutes and try again.",
            retry_after=TOKEN_PAGE_WINDOW,
        )


# Annotated shortcuts for handler signatures, e.g. def page(request: Request, user: SignedIn, ctx: Ctx).
Ctx = Annotated[AppContext, Depends(get_context)]
CurrentUser = Annotated[User | None, Depends(current_user)]
SignedIn = Annotated[User, Depends(require_user)]
Admin = Annotated[User, Depends(require_admin)]
SignedForm = Annotated[FormData, Depends(checked_form)]
PublicForm = Annotated[FormData, Depends(public_form)]
