"""The pages of one's own account: signing in and out, accepting an invite, setting a password from a link, and the
settings page (alert rules, watchlist, channels, currency, time zone, test alert, password, other sessions).

Nobody signs up: accounts come from invites (admin pages or `dip-scanner users invite`) and setup links (`dip-scanner
users add-admin`). The sign-in, invite and password forms are protected by the double-submit token (auth.public_form),
every other form by the session's token (auth.checked_form); both check the Origin too.
"""

from __future__ import annotations

import functools
import logging
from datetime import timedelta
from typing import Annotated, Any
from zoneinfo import available_timezones

from fastapi import APIRouter, Query, Request
from fastapi.responses import RedirectResponse, Response

from ..accounts import (
    CURRENCIES,
    MAX_EMAIL_LENGTH,
    MIN_PASSWORD_LENGTH,
    AccountError,
    Accounts,
    SettingsError,
    User,
    clean_name,
    email_ready,
    telegram_ready,
    validate_settings,
)
from ..config import WEBHOOK_FORMATS
from ..notices import one_line, scrub, secrets_of
from ..recipients import send_test, user_recipient
from ..triage import normalise_ticker
from . import auth
from .app import error_page, flash, redirect, render
from .context import AppContext

log = logging.getLogger(__name__)

TEST_LIMIT = 5  # test alerts per user in TEST_WINDOW
TEST_WINDOW = timedelta(minutes=15)
TOO_MANY_LOGINS = "Too many failed sign-ins. Wait 15 minutes and try again."
WRONG_LOGIN = "Wrong email or password."
PASSWORDS_DIFFER = "The two passwords don't match."
_BOOLEAN_FIELDS = ("only_watchlist", "thesis_changes", "email_alerts")
_ZONE_SKIP = ("Etc/", "SystemV/", "posix/", "right/", "US/", "Canada/", "Brazil/", "Chile/", "Mexico/")

router = APIRouter()


# --- signing in and out --------------------------------------------------------------------------------------------


@router.get("/login")
def login_page(request: Request, next_url: Annotated[str, Query(alias="next")] = "/") -> Response:
    target = auth.safe_next(next_url)
    if auth.current_user(request) is not None:
        return RedirectResponse(target, status_code=303)
    return _login_form(request, target)


def _login_form(request: Request, target: str, *, email: str = "", error: str | None = None, status: int = 200):
    return render(
        request,
        "auth/login.html",
        {"next": target, "email": email, "error": error, "page_title": "Sign in"},
        status_code=status,
        headers={"Retry-After": "900"} if status == 429 else None,
    )


@router.post("/login")
def login(request: Request, form: auth.PublicForm, ctx: auth.Ctx) -> Response:
    email = str(form.get("email") or "").strip()[:MAX_EMAIL_LENGTH]
    password = str(form.get("password") or "")
    target = auth.safe_next(form.get("next"))
    keys = Accounts.login_keys(auth.client_ip(request), email)
    if ctx.accounts.too_many_attempts(*keys):
        log.warning("Sign-in refused after too many failed attempts from %s.", auth.client_ip(request) or "?")
        return _login_form(request, target, email=email, error=TOO_MANY_LOGINS, status=429)
    user = ctx.accounts.authenticate(email, password)
    if user is None:
        ctx.accounts.record_login_failure(*keys)
        return _login_form(request, target, email=email, error=WRONG_LOGIN, status=400)
    ctx.accounts.clear_attempts(*(key for key in keys if key.startswith("email:")))
    _replace_session(request, user)
    log.info("Account #%d signed in.", user.id)
    return redirect(request, target)


@router.post("/logout")
def logout(request: Request, form: auth.SignedForm) -> Response:
    auth.end_session(request)
    return redirect(request, "/login", "You're signed out.", kind="info")


def _replace_session(request: Request, user: User) -> None:
    """Sign user in on this browser, ending the session its cookie had (another account's, or a stale one)."""
    old = auth.session_token(request)
    if old:
        auth.get_context(request).accounts.revoke_session(old)
    auth.start_session(request, user)


# --- invite and password links -------------------------------------------------------------------------------------


def _dead_link(request: Request, what: str) -> Response:
    if what == "invite":
        title, message = "This invite link doesn't work", "It has expired or was already used. Ask for a new one."
    else:
        title = "This password link doesn't work"
        message = "It has expired or was already used. Ask an admin for a new one."
    return render(
        request,
        "auth/link_invalid.html",
        {"title": title, "message": message, "page_title": title},
        status_code=404,
    )


@router.get("/invite/{token}")
def invite_page(request: Request, token: str, ctx: auth.Ctx) -> Response:
    auth.limit_link_pages(request)
    invite = ctx.accounts.get_invite(token)
    if invite is None:
        return _dead_link(request, "invite")
    return _invite_form(request, invite, name="", email=invite.email or "")


def _invite_form(request: Request, invite, *, name: str, email: str, error: str | None = None, status: int = 200):
    return render(
        request,
        "auth/invite.html",
        {
            "invite": invite,
            "name": name,
            "email": email,
            "error": error,
            "csrf_token": auth.form_token(request),
            "min_password": MIN_PASSWORD_LENGTH,
            "page_title": "Create your account",
        },
        status_code=status,
    )


@router.post("/invite/{token}")
def accept_invite(request: Request, token: str, form: auth.PublicForm, ctx: auth.Ctx) -> Response:
    auth.limit_link_pages(request)
    invite = ctx.accounts.get_invite(token)
    if invite is None:
        return _dead_link(request, "invite")
    name = clean_name(form.get("name"))
    email = invite.email or str(form.get("email") or "").strip()[:MAX_EMAIL_LENGTH]
    password, confirm = str(form.get("password") or ""), str(form.get("confirm") or "")
    if password != confirm:
        return _invite_form(request, invite, name=name, email=email, error=PASSWORDS_DIFFER, status=400)
    try:
        user = ctx.accounts.accept_invite(token, name=name, password=password, email=email or None)
    except AccountError as exc:
        return _invite_form(request, invite, name=name, email=email, error=str(exc), status=400)
    _replace_session(request, user)
    greeting = f"Welcome, {user.name}!" if user.name else "Welcome!"
    return redirect(request, "/settings", f"{greeting} Choose your watchlist and where your alerts should go.")


@router.get("/password/{token}")
def password_page(request: Request, token: str, ctx: auth.Ctx) -> Response:
    auth.limit_link_pages(request)
    found = ctx.accounts.get_password_token(token)
    if found is None:
        return _dead_link(request, "password")
    return _password_form(request, found)


def _password_form(request: Request, found, *, error: str | None = None, status: int = 200) -> Response:
    title = "Set your password" if found.purpose == "setup" else "Choose a new password"
    return render(
        request,
        "auth/password.html",
        {
            "link": found,
            "email": found.user.email,
            "error": error,
            "csrf_token": auth.form_token(request),
            "min_password": MIN_PASSWORD_LENGTH,
            "page_title": title,
        },
        status_code=status,
    )


@router.post("/password/{token}")
def use_password_link(request: Request, token: str, form: auth.PublicForm, ctx: auth.Ctx) -> Response:
    auth.limit_link_pages(request)
    found = ctx.accounts.get_password_token(token)
    if found is None:
        return _dead_link(request, "password")
    password, confirm = str(form.get("password") or ""), str(form.get("confirm") or "")
    if password != confirm:
        return _password_form(request, found, error=PASSWORDS_DIFFER, status=400)
    try:
        user = ctx.accounts.use_password_token(token, password)
    except AccountError as exc:
        return _password_form(request, found, error=str(exc), status=400)
    _replace_session(request, user)
    if found.purpose == "setup":
        return redirect(request, "/settings", "Your password is set. Choose your watchlist and alerts here.")
    return redirect(request, "/", "Your password was changed, and every other session of yours was signed out.")


# --- settings ------------------------------------------------------------------------------------------------------


@functools.cache
def timezone_choices() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """IANA time zones by region, for the settings page: (("Africa", ("Africa/Abidjan", ...)), ...)."""
    try:
        names = available_timezones()
    except OSError:
        names = set()
    groups: dict[str, list[str]] = {}
    for name in sorted(names):
        if "/" not in name or name.startswith(_ZONE_SKIP):
            continue
        groups.setdefault(name.split("/", 1)[0], []).append(name)
    return (("", ("UTC",)),) + tuple((region, tuple(zones)) for region, zones in groups.items())


def _offered(ctx: AppContext, user: User) -> dict[str, bool]:
    """Which channels the settings page offers: those the server can send, and any the user already has on (so they
    can switch it off)."""
    chosen = user.settings
    return {
        "email": email_ready(ctx.settings) or chosen.email_alerts,
        "email_ready": email_ready(ctx.settings),
        "telegram": telegram_ready(ctx.settings) or bool(chosen.telegram_chat_id),
        "telegram_ready": telegram_ready(ctx.settings),
    }


def _values(user: User) -> dict[str, Any]:
    chosen = user.settings
    return {
        "name": user.name,
        "watchlist": ", ".join(chosen.watchlist),
        "min_score": f"{chosen.min_score:g}",
        "min_probability": str(chosen.min_probability),
        "verdicts": list(chosen.verdicts),
        "only_watchlist": chosen.only_watchlist,
        "thesis_changes": chosen.thesis_changes,
        "email_alerts": chosen.email_alerts,
        "telegram_chat_id": chosen.telegram_chat_id or "",
        "webhook_url": chosen.webhook_url or "",
        "webhook_format": chosen.webhook_format,
        "currency": chosen.currency or "",
        "timezone": chosen.timezone,
    }


def settings_page(
    request: Request,
    ctx: AppContext,
    user: User,
    *,
    values: dict[str, Any] | None = None,
    errors: dict[str, str] | None = None,
    password_error: str | None = None,
    status_code: int = 200,
) -> Response:
    sessions = ctx.accounts.list_sessions(user.id)
    return render(
        request,
        "account/settings.html",
        {
            "values": values if values is not None else _values(user),
            "errors": errors or {},
            "password_error": password_error,
            "offered": _offered(ctx, user),
            "has_channel": user.settings.has_channel,
            "currencies": CURRENCIES,
            "timezones": timezone_choices(),
            "webhook_formats": WEBHOOK_FORMATS,
            "other_sessions": max(0, len(sessions) - 1),
            "jobs_remaining": ctx.jobs.remaining(user),
            "jobs_limit": ctx.jobs.limit_for(user),
            "min_password": MIN_PASSWORD_LENGTH,
            "nav": "settings",
            "page_title": "Settings",
        },
        status_code=status_code,
    )


@router.get("/settings")
def show_settings(request: Request, user: auth.SignedIn, ctx: auth.Ctx) -> Response:
    return settings_page(request, ctx, user)


@router.post("/settings")
def save_settings(request: Request, user: auth.SignedIn, form: auth.SignedForm, ctx: auth.Ctx) -> Response:
    current = user.settings
    offered = _offered(ctx, user)
    data: dict[str, Any] = {
        "watchlist": str(form.get("watchlist") or ""),
        "min_score": str(form.get("min_score") or ""),
        "min_probability": str(form.get("min_probability") or ""),
        "verdicts": [str(value) for value in form.getlist("verdicts")],
        "webhook_format": str(form.get("webhook_format") or current.webhook_format),
        "currency": str(form.get("currency") or ""),
        "timezone": str(form.get("timezone") or current.timezone),
    }
    for name in ("only_watchlist", "thesis_changes"):  # an unticked box isn't sent at all
        data[name] = str(form.get(name) or "")
    if offered["email"]:
        data["email_alerts"] = str(form.get("email_alerts") or "")
    if offered["telegram"]:
        data["telegram_chat_id"] = str(form.get("telegram_chat_id") or "")
    webhook = str(form.get("webhook_url") or "").strip()
    if webhook != (current.webhook_url or ""):  # an unchanged address was checked when it was saved
        data["webhook_url"] = webhook
    name = clean_name(form.get("name"))
    try:
        chosen = validate_settings(data, current=current, settings=ctx.settings, resolver=ctx.resolver)
    except SettingsError as exc:
        shown = {**_values(user), **data, "name": name, "webhook_url": webhook}
        for field in _BOOLEAN_FIELDS:
            if field in data:
                shown[field] = bool(data[field])
        return settings_page(request, ctx, user, values=shown, errors=exc.errors, status_code=400)
    if name != user.name:
        ctx.accounts.set_name(user.id, name)
    ctx.accounts.update_settings(user.id, chosen)
    message = "Settings saved."
    if chosen.has_channel and not current.has_channel:
        message += " Your alerts start now: ideas from before aren't sent."
    return redirect(request, "/settings", message)


@router.post("/settings/test")
def test_alert(request: Request, user: auth.SignedIn, form: auth.SignedForm, ctx: auth.Ctx) -> Response:
    """Send a short test message to each of the user's saved channels and say how each went."""
    if ctx.accounts.rate_limited(f"test:{user.id}", limit=TEST_LIMIT, window=TEST_WINDOW):
        return error_page(
            request,
            429,
            f"You sent {TEST_LIMIT} test alerts in the last 15 minutes. Wait a little and try again.",
        )
    recipient = user_recipient(user, ctx.settings, ctx.config, session=ctx.http, resolver=ctx.resolver)
    if not recipient.notifiers:
        return redirect(
            request, "/settings#channels", "Set up an alert channel and save it first, then send a test.", kind="error"
        )
    secrets = secrets_of(ctx.settings)
    for channel, problem in send_test(recipient, now=ctx.now()):
        label = channel[:1].upper() + channel[1:]
        if problem is None:
            flash(request, f"{label}: test alert sent.")
        else:
            flash(request, f"{label}: the test alert failed: {one_line(scrub(problem, secrets), 300)}", "error")
    return redirect(request, "/settings#channels")


@router.post("/settings/password")
def change_password(request: Request, user: auth.SignedIn, form: auth.SignedForm, ctx: auth.Ctx) -> Response:
    """Change the password (the current one is needed); every other session ends."""
    key = f"email:{user.email}"
    if ctx.accounts.too_many_attempts(key):
        return settings_page(request, ctx, user, password_error=TOO_MANY_LOGINS, status_code=429)
    current, new, confirm = (str(form.get(name) or "") for name in ("current_password", "new_password", "confirm"))
    if not ctx.accounts.verify_password(user.id, current):
        ctx.accounts.record_login_failure(key)
        return settings_page(request, ctx, user, password_error="Your current password isn't right.", status_code=400)
    if new != confirm:
        return settings_page(request, ctx, user, password_error=PASSWORDS_DIFFER, status_code=400)
    try:
        user = ctx.accounts.set_password(user.id, new)
    except AccountError as exc:
        return settings_page(request, ctx, user, password_error=str(exc), status_code=400)
    auth.start_session(request, user)  # set_password ended every session, this one too
    return redirect(request, "/settings", "Password changed. Every other session of yours was signed out.")


@router.post("/settings/sessions")
def end_other_sessions(request: Request, user: auth.SignedIn, form: auth.SignedForm, ctx: auth.Ctx) -> Response:
    ended = ctx.accounts.revoke_sessions(user.id, keep=auth.session_token(request))
    return redirect(request, "/settings", f"Signed out of {ended} other session{'s' if ended != 1 else ''}.")


def change_watchlist(ctx: AppContext, user: User, *, add: str | None = None, remove: str | None = None) -> User:
    """The user's watchlist with a symbol added and/or removed (for "Add to watchlist" buttons); AccountError when the
    symbol isn't one or the list is full."""
    symbols = list(user.settings.watchlist)
    if add is not None:
        symbol = normalise_ticker(add)
        if symbol is None:
            raise AccountError(f"{add.strip()[:20]!r} isn't a Yahoo Finance symbol; write it like AMD or SAP.DE.")
        if symbol not in symbols:
            symbols.append(symbol)
    if remove is not None:
        gone = normalise_ticker(remove) or remove.strip().upper()
        symbols = [symbol for symbol in symbols if symbol != gone]
    chosen = validate_settings({"watchlist": symbols}, current=user.settings, settings=ctx.settings)
    return ctx.accounts.update_settings(user.id, chosen)
