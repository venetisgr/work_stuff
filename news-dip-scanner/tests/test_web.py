"""The website's foundation with FastAPI's TestClient: signing in, sessions, CSRF and Origin checks, rate limits,
invite and password links, the settings page, "Analyse now" jobs, error pages, security headers and the templates.

No network (fake HTTP session, fake DNS resolver) and no sleeping: analyses run inline, the clock is a settable fake.
"""

from __future__ import annotations

import logging
import re
import socket
import threading
import time
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import requests
from conftest import FakeSession, make_analysis, make_opportunity
from fastapi.testclient import TestClient

from dip_scanner import accounts as accounts_module
from dip_scanner.config import DATABASE_NAME, NotifySettings, ScannerConfig, Settings, WebSettings
from dip_scanner.prices import PriceError
from dip_scanner.report import display_zone_as
from dip_scanner.store import Store
from dip_scanner.web import app as web_app
from dip_scanner.web import auth
from dip_scanner.web.app import CSP, create_app, make_templates, paginate, relative_time, static_url, url_with
from dip_scanner.web.jobs import InlineExecutor

BASE = "https://dips.example.com"
NOW = datetime(2026, 9, 25, 15, 0, tzinfo=UTC)
PASSWORD = "a long enough password"
HOOK = "https://hooks.example.com/services/T000/B000/very-secret-hook-token"
TEMPLATES = Path(web_app.__file__).resolve().parent / "templates"


@pytest.fixture(autouse=True)
def fast_scrypt(monkeypatch):
    monkeypatch.setattr(accounts_module, "SCRYPT_N", 2**4)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any DNS lookup or connection is a test bug (webhook checks must use the fake resolver)."""

    def refuse(*args, **kwargs):
        raise AssertionError(f"a test tried to use the network: {args[:2]}")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


class Parked(Executor):
    """An executor that keeps the jobs until run() (so a test sees them waiting)."""

    def __init__(self) -> None:
        self.calls: list = []

    def submit(self, fn, /, *args, **kwargs) -> Future:
        self.calls.append((fn, args, kwargs))
        return Future()

    def run(self) -> None:
        calls, self.calls = self.calls, []
        for fn, args, kwargs in calls:
            fn(*args, **kwargs)


def public_resolver(host: str, port: int) -> list[str]:
    return {"private.example.com": ["10.0.0.7"]}.get(host, ["34.120.1.2"])


class Site:
    """An app on a fresh database, its context and a client that doesn't follow redirects."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        web: dict | None = None,
        notify: NotifySettings | None = None,
        analyse=None,
        executor: Executor | None = None,
        http: FakeSession | None = None,
        trust_fly: bool = False,
        base_url: str = BASE,
        shutdown_budget: float = 5.0,
    ) -> None:
        values = {"secret_key": "s" * 40, "base_url": BASE, "cookie_secure": True}
        values.update(web or {})
        self.settings = Settings(
            data_dir=tmp_path / "data", notify=notify or NotifySettings(), web=WebSettings(**values)
        )
        self.clock = Clock()
        self.http = http if http is not None else FakeSession({})
        self.app = create_app(
            settings=self.settings,
            config=ScannerConfig(),
            feeds=[],
            store_path=self.settings.data_dir / DATABASE_NAME,
            clock=self.clock,
            analyse=analyse,
            job_executor=executor or InlineExecutor(),
            http_session=self.http,
            resolver=public_resolver,
            trust_fly_client_ip=trust_fly,
            shutdown_budget=shutdown_budget,
        )
        self.ctx = self.app.state.ctx
        self.accounts = self.ctx.accounts
        self.store = self.ctx.store
        self.base_url = base_url
        self.client = self.new_client()

    def new_client(self) -> TestClient:
        return TestClient(self.app, base_url=self.base_url, follow_redirects=False, raise_server_exceptions=False)

    def user(self, email: str = "member@example.com", *, role: str = "member", **kwargs):
        return self.accounts.create_user(email, role=role, password=PASSWORD, **kwargs)

    def sign_in(self, email: str, password: str = PASSWORD, *, client: TestClient | None = None, next_url="/"):
        client = client or self.client
        page = client.get("/login")
        data = {"email": email, "password": password, "csrf_token": token_on(page.text), "next": next_url}
        return client.post("/login", data=data)

    def signed_in(self, email: str = "member@example.com", *, role: str = "member") -> TestClient:
        if self.accounts.get_user_by_email(email) is None:
            self.user(email, role=role)
        client = self.new_client()
        assert self.sign_in(email, client=client).status_code == 303
        return client


def token_on(page: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', page)
    assert match, "no csrf_token on the page"
    return match.group(1)


def cookies_set(response) -> dict[str, str]:
    """{name: the whole Set-Cookie header} of a response."""
    return {value.split("=", 1)[0]: value for value in response.headers.get_list("set-cookie")}


def post_form(client: TestClient, path: str, data: dict | None = None, *, page: str = "/settings", **kwargs):
    """POST a signed-in form with the session's csrf_token (taken from page)."""
    token = token_on(client.get(page).text)
    return client.post(path, data={"csrf_token": token, **(data or {})}, **kwargs)


@pytest.fixture
def site(tmp_path) -> Site:
    return Site(tmp_path)


# --- signing in and sessions ---------------------------------------------------------------------------------------


def test_pages_need_signing_in_and_come_back_after(site):
    site.user()
    response = site.client.get("/settings?tab=1")
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Fsettings%3Ftab%3D1"

    signed = site.sign_in("member@example.com", next_url="/settings?tab=1")
    assert signed.status_code == 303 and signed.headers["location"] == "/settings?tab=1"
    assert site.client.get("/settings").status_code == 200
    assert site.client.get("/login").headers["location"] == "/"  # already signed in


def test_the_session_cookie_is_random_http_only_secure_and_lax(site):
    site.user()
    response = site.sign_in("member@example.com")
    cookie = cookies_set(response)["dsid"]
    token = cookie.split(";", 1)[0].split("=", 1)[1]
    assert len(token) >= 43
    flags = [part.strip().lower() for part in cookie.split(";")[1:]]
    assert {"httponly", "secure", "samesite=lax", "path=/", "max-age=2592000"} <= set(flags)
    stored = site.store.query("SELECT token_hash FROM sessions")
    assert [row[0] for row in stored] == [accounts_module.token_hash(token)]  # only the hash is kept

    # Every visit renews the cookie (the session slides with it).
    renewed = cookies_set(site.client.get("/settings"))["dsid"]
    assert token in renewed and "Max-Age=2592000" in renewed


def test_cookies_travel_over_http_when_cookie_secure_is_off(tmp_path):
    site = Site(
        tmp_path, web={"cookie_secure": False, "base_url": "http://localhost:8080"}, base_url="http://localhost:8080"
    )
    site.user()
    response = site.sign_in("member@example.com")
    assert "secure" not in cookies_set(response)["dsid"].lower()
    assert "strict-transport-security" not in response.headers  # no HSTS over http
    assert site.client.get("/settings").status_code == 200


def test_a_wrong_password_is_refused_and_ten_lock_the_address_for_15_minutes(site):
    site.user()
    for _ in range(10):
        response = site.sign_in("member@example.com", "wrong password!")
        assert response.status_code == 400
        assert "Wrong email or password." in response.text
    blocked = site.sign_in("member@example.com")  # even the right password
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"] == "900"
    assert "Too many failed sign-ins. Wait 15 minutes and try again." in blocked.text
    assert "dsid" not in cookies_set(blocked)

    site.clock.advance(minutes=16)
    assert site.sign_in("member@example.com").status_code == 303


def test_failed_sign_ins_count_per_address_across_emails(site):
    for number in range(10):
        site.sign_in(f"nobody{number}@example.com", "wrong password!")
    site.user("real@example.com")
    assert site.sign_in("real@example.com").status_code == 429  # the same IP address


def test_failures_from_other_addresses_never_lock_the_owner_out(tmp_path):
    """Anybody who knows an address can send wrong passwords for it: that mustn't keep its owner from signing in."""
    site = Site(tmp_path, trust_fly=True)
    site.user()
    client = site.client
    for number in range(10):
        client.headers["Fly-Client-IP"] = f"203.0.113.{number}"
        assert site.sign_in("member@example.com", "wrong password!").status_code == 400
    client.headers["Fly-Client-IP"] = "198.51.100.1"
    assert site.sign_in("member@example.com").status_code == 303
    keys = {row[0] for row in site.store.query("SELECT key FROM login_attempts")}
    assert "ip:203.0.113.4" in keys  # the attackers' own addresses still count
    assert not any(key.startswith(("email:", "email_ip:")) for key in keys)  # forgotten after signing in


def test_ten_failures_from_one_address_lock_that_address_and_email(tmp_path):
    site = Site(tmp_path, trust_fly=True)
    site.user()
    site.client.headers["Fly-Client-IP"] = "203.0.113.7"
    for _ in range(10):
        site.sign_in("member@example.com", "wrong password!")
    assert site.sign_in("member@example.com").status_code == 429
    site.client.headers["Fly-Client-IP"] = "2001:db8:1:2:aaaa::1"
    for _ in range(10):
        site.sign_in("member@example.com", "wrong password!")
    site.client.headers["Fly-Client-IP"] = "2001:db8:1:2:bbbb::9"  # the same /64: the same household
    assert site.sign_in("member@example.com").status_code == 429
    site.client.headers["Fly-Client-IP"] = "198.51.100.1"
    assert site.sign_in("member@example.com").status_code == 303


def test_guessing_spread_over_many_addresses_hits_a_ceiling_per_email(tmp_path):
    site = Site(tmp_path, trust_fly=True)
    site.user()
    for number in range(100):
        site.client.headers["Fly-Client-IP"] = f"203.0.{number // 250}.{number % 250 + 1}"
        site.sign_in("member@example.com", "wrong password!")
    site.client.headers["Fly-Client-IP"] = "198.51.100.1"
    assert site.sign_in("member@example.com").status_code == 429
    site.clock.advance(minutes=61)  # the ceiling counts the last hour
    assert site.sign_in("member@example.com").status_code == 303


def test_sign_in_failures_elsewhere_never_block_a_password_change(tmp_path):
    site = Site(tmp_path, trust_fly=True)
    client = site.signed_in()
    attacker = site.new_client()
    for number in range(100):
        attacker.headers["Fly-Client-IP"] = f"203.0.{number // 250}.{number % 250 + 1}"
        site.sign_in("member@example.com", "wrong password!", client=attacker)
    form = {"current_password": PASSWORD, "new_password": "y" * 12, "confirm": "y" * 12}
    assert post_form(client, "/settings/password", form).status_code == 303


def test_a_password_link_forgets_the_failed_sign_ins(tmp_path):
    site = Site(tmp_path, trust_fly=True)
    user = site.user()
    for number in range(100):
        site.client.headers["Fly-Client-IP"] = f"203.0.{number // 250}.{number % 250 + 1}"
        site.sign_in("member@example.com", "wrong password!")
    link = site.accounts.create_password_token(user.id, "reset")
    site.accounts.use_password_token(link, "a brand new password")
    keys = [row[0] for row in site.store.query("SELECT key FROM login_attempts")]
    assert keys and not any(key.startswith(("email:", "email_ip:")) for key in keys)
    site.client.headers["Fly-Client-IP"] = "198.51.100.1"
    assert site.sign_in("member@example.com", "a brand new password").status_code == 303


def test_fly_client_ip_is_only_trusted_on_fly(site):
    site.client.headers["Fly-Client-IP"] = "203.0.113.9"
    site.sign_in("nobody@example.com", "wrong password!")
    keys = {row[0] for row in site.store.query("SELECT key FROM login_attempts")}
    assert "ip:testclient" in keys and "ip:203.0.113.9" not in keys


def test_sign_in_needs_the_double_submit_token(site):
    site.user()
    page = site.client.get("/login")
    token = token_on(page.text)
    assert cookies_set(page)["dcsrf"].startswith(f"dcsrf={token};")
    data = {"email": "member@example.com", "password": PASSWORD}

    assert site.client.post("/login", data=data).status_code == 403  # no token
    assert site.client.post("/login", data={**data, "csrf_token": "x" + token}).status_code == 403
    fresh = site.new_client()  # the right token without its cookie
    assert fresh.post("/login", data={**data, "csrf_token": token}).status_code == 403
    forged = "made-up.value"  # a cookie and field the attacker chose: the signature doesn't match
    fresh.cookies.set("dcsrf", forged, domain="dips.example.com")
    refused = fresh.post("/login", data={**data, "csrf_token": forged})
    assert refused.status_code == 403
    assert "reload the page and try again" in refused.text
    assert site.client.post("/login", data={**data, "csrf_token": token}).status_code == 303


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
        {"Origin": "http://dips.example.com"},  # another scheme is another site
        {"Referer": "https://evil.example/page"},
    ],
)
def test_cross_origin_posts_are_refused(site, headers):
    client = site.signed_in()
    token = token_on(client.get("/settings").text)
    response = client.post("/settings", data={"csrf_token": token, "min_score": "10"}, headers=headers)
    assert response.status_code == 403
    assert "sent from another site" in response.text
    assert site.accounts.get_user_by_email("member@example.com").settings.min_score == 65
    login = site.new_client()
    page = login.get("/login")
    data = {"email": "member@example.com", "password": PASSWORD, "csrf_token": token_on(page.text)}
    assert login.post("/login", data=data, headers=headers).status_code == 403


def test_same_origin_posts_pass(site):
    client = site.signed_in()
    same = {"Origin": BASE, "Referer": f"{BASE}/settings"}
    response = post_form(client, "/settings/sessions", headers=same)
    assert response.status_code == 303


def test_without_base_url_the_origin_must_be_the_requested_host(tmp_path):
    site = Site(tmp_path, web={"base_url": None})
    client = site.signed_in()
    token = token_on(client.get("/settings").text)
    ok = client.post("/settings/sessions", data={"csrf_token": token}, headers={"Origin": BASE})
    assert ok.status_code == 303
    bad = client.post("/settings/sessions", data={"csrf_token": token}, headers={"Origin": "https://evil.example"})
    assert bad.status_code == 403


def test_signed_in_forms_need_the_session_token(site):
    client = site.signed_in()
    other = site.signed_in("other@example.com")
    other_token = token_on(other.get("/settings").text)
    for data in ({}, {"csrf_token": "nope"}, {"csrf_token": other_token}):
        response = client.post("/settings", data={**data, "min_score": "10"})
        assert response.status_code == 403
    assert site.accounts.get_user_by_email("member@example.com").settings.min_score == 65
    # Signed out, a form sends you to sign in instead.
    assert site.new_client().post("/settings", data={"min_score": "10"}).headers["location"] == "/login"


def test_logout_ends_the_session_and_clears_the_cookie(site):
    client = site.signed_in()
    old = client.cookies.get("dsid")
    assert client.post("/logout", data={}).status_code == 403  # without the token
    response = post_form(client, "/logout")
    assert response.status_code == 303 and response.headers["location"] == "/login"
    assert cookies_set(response)["dsid"].startswith('dsid="";') or "Max-Age=0" in cookies_set(response)["dsid"]
    assert site.store.query("SELECT COUNT(*) FROM sessions")[0][0] == 0
    page = client.get("/login")
    assert "You&#39;re signed out." in page.text
    replay = site.new_client()
    replay.cookies.set("dsid", old, domain="dips.example.com")
    assert replay.get("/settings").status_code == 303  # the old cookie is dead


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/ideas/3?x=1", "/ideas/3?x=1"),
        ("/settings#channels", "/settings#channels"),
        ("//evil.com", "/"),
        ("///evil.com", "/"),
        ("https://evil.com/", "/"),
        ("/\\evil.com", "/"),
        ("\\\\evil.com", "/"),
        ("javascript:alert(1)", "/"),
        ("evil.com", "/"),
        ("/ok\r\nSet-Cookie: x", "/"),
        ("", "/"),
        (None, "/"),
        ("/" + "a" * 2000, "/"),
    ],
)
def test_safe_next_only_allows_paths_on_this_site(value, expected):
    assert auth.safe_next(value) == expected


def test_sign_in_never_redirects_to_another_site(site):
    site.user()
    for target in ("//evil.com", "https://evil.com", "/\\evil.com"):
        assert site.sign_in("member@example.com", next_url=target, client=site.new_client()).headers["location"] == "/"


def test_a_disabled_user_is_signed_out_on_the_next_request(site):
    client = site.signed_in()
    user = site.accounts.get_user_by_email("member@example.com")
    site.accounts.create_user("admin@example.com", role="admin", password=PASSWORD)
    site.accounts.set_disabled(user.id, True)
    response = client.get("/settings")
    assert response.status_code == 303 and response.headers["location"].startswith("/login")
    assert "dsid" in cookies_set(response) and "Max-Age=0" in cookies_set(response)["dsid"]
    assert site.sign_in("member@example.com", client=site.new_client()).status_code == 400


def test_sessions_expire_after_30_days_without_a_visit(site):
    client = site.signed_in()
    site.clock.advance(days=29)
    assert client.get("/settings").status_code == 200  # a visit renews it
    site.clock.advance(days=29)
    assert client.get("/settings").status_code == 200
    site.clock.advance(days=31)
    assert client.get("/settings").status_code == 303


def test_admin_pages_are_for_admins_only(site):
    member = site.signed_in()
    response = member.get("/admin")
    assert response.status_code == 403
    assert "This page is for admins only." in response.text
    assert ">Admin</a>" not in member.get("/").text
    admin = site.signed_in("admin@example.com", role="admin")
    assert admin.get("/admin").status_code == 200
    assert ">Admin</a>" in admin.get("/").text
    assert site.new_client().get("/admin").status_code == 303  # signed out: sign in first


# --- invites and password links ------------------------------------------------------------------------------------


def test_an_invite_creates_the_account_once_and_signs_it_in(site, caplog):
    caplog.set_level(logging.DEBUG)
    token = site.accounts.create_invite(created_by=None)
    client = site.client
    page = client.get(f"/invite/{token}")
    assert page.status_code == 200 and "Create your account" in page.text
    assert '<meta name="referrer" content="strict-origin">' in page.text  # the token never reaches a Referer
    data = {
        "csrf_token": token_on(page.text),
        "email": "Friend@Example.com",
        "name": "Friend <b>",
        "password": PASSWORD,
        "confirm": PASSWORD,
    }
    response = client.post(f"/invite/{token}", data=data)
    assert response.status_code == 303 and response.headers["location"] == "/settings"
    assert "dsid" in cookies_set(response)
    settings = client.get("/settings")
    assert "Welcome, Friend &lt;b&gt;!" in settings.text  # escaped
    assert site.accounts.get_user_by_email("friend@example.com").role == "member"

    again = site.new_client()
    assert again.get(f"/invite/{token}").status_code == 404
    reused = again.post(f"/invite/{token}", data={**data, "email": "second@example.com"})
    assert reused.status_code in (403, 404)  # no form cookie on this client; either way no account
    assert site.accounts.get_user_by_email("second@example.com") is None
    ours = "\n".join(record.getMessage() for record in caplog.records if record.name.startswith("dip_scanner"))
    assert "Invite used" in ours and token not in ours  # tokens are never logged


def test_an_invite_for_an_address_uses_that_address(site):
    token = site.accounts.create_invite(created_by=None, email="named@example.com", role="admin")
    page = site.client.get(f"/invite/{token}")
    assert 'value="named@example.com"' in page.text and "readonly" in page.text
    data = {"csrf_token": token_on(page.text), "email": "other@example.com", "password": PASSWORD, "confirm": PASSWORD}
    assert site.client.post(f"/invite/{token}", data=data).status_code == 303
    assert site.accounts.get_user_by_email("named@example.com").is_admin
    assert site.accounts.get_user_by_email("other@example.com") is None


def test_invite_mistakes_are_shown_on_the_form(site):
    token = site.accounts.create_invite(created_by=None)
    page = site.client.get(f"/invite/{token}")
    base = {"csrf_token": token_on(page.text), "email": "new@example.com", "name": "New"}
    differ = site.client.post(f"/invite/{token}", data={**base, "password": PASSWORD, "confirm": PASSWORD + "!"})
    assert differ.status_code == 400 and "The two passwords don&#39;t match." in differ.text
    short = site.client.post(f"/invite/{token}", data={**base, "password": "short", "confirm": "short"})
    assert short.status_code == 400 and "at least 10 characters" in short.text
    assert site.accounts.get_invite(token) is not None  # still usable


def test_invites_expire_after_7_days(site):
    token = site.accounts.create_invite(created_by=None)
    site.clock.advance(days=7, minutes=1)
    response = site.client.get(f"/invite/{token}")
    assert response.status_code == 404
    assert "This invite link doesn&#39;t work" in response.text


def test_a_setup_link_sets_the_password_once_and_signs_in(site):
    user = site.accounts.create_user("owner@example.com", role="admin")
    token = site.accounts.create_password_token(user.id)
    page = site.client.get(f"/password/{token}")
    assert page.status_code == 200 and "Set your password" in page.text and "owner@example.com" in page.text
    assert '<meta name="referrer" content="strict-origin">' in page.text
    data = {"csrf_token": token_on(page.text), "password": PASSWORD, "confirm": PASSWORD}
    # Regression: with "no-referrer" on this page, a browser sends "Origin: null" with the form and it was refused
    assert site.client.post(f"/password/{token}", data=data, headers={"Origin": "null"}).status_code == 403
    response = site.client.post(f"/password/{token}", data=data, headers={"Origin": BASE, "Referer": BASE + "/"})
    assert response.status_code == 303 and response.headers["location"] == "/settings"
    assert site.client.get("/admin").status_code == 200
    assert site.new_client().get(f"/password/{token}").status_code == 404  # used up
    assert site.client.post(f"/password/{token}", data=data).status_code == 404


def test_a_reset_link_ends_every_other_session(site):
    phone = site.signed_in()
    user = site.accounts.get_user_by_email("member@example.com")
    token = site.accounts.create_password_token(user.id)
    laptop = site.new_client()
    page = laptop.get(f"/password/{token}")
    assert "Choose a new password" in page.text
    new = "an even longer new password"
    data = {"csrf_token": token_on(page.text), "password": new, "confirm": new}
    assert laptop.post(f"/password/{token}", data=data).headers["location"] == "/"
    assert phone.get("/settings").status_code == 303  # signed out
    assert laptop.get("/settings").status_code == 200
    assert site.sign_in("member@example.com", new, client=site.new_client()).status_code == 303


def test_password_links_expire_after_48_hours(site):
    user = site.user()
    token = site.accounts.create_password_token(user.id)
    site.clock.advance(hours=48, minutes=1)
    assert site.client.get(f"/password/{token}").status_code == 404


def test_link_pages_are_rate_limited_per_address(site):
    for _ in range(auth.TOKEN_PAGE_LIMIT):
        assert site.client.get("/invite/guess").status_code == 404
    blocked = site.client.get("/invite/guess")
    assert blocked.status_code == 429 and "Too many invite or password links" in blocked.text
    assert site.client.get("/password/guess").status_code == 429
    site.clock.advance(minutes=16)
    assert site.client.get("/password/guess").status_code == 404


# --- settings ------------------------------------------------------------------------------------------------------


def full_form(**changes) -> dict:
    data = {
        "name": "Eleni",
        "min_score": "55,5",
        "min_probability": "70",
        "verdicts": ["temporary_fear", "mixed", "unclear"],
        "only_watchlist": "on",
        "watchlist": "ete.at, BRK.B\nAMD",
        "webhook_url": HOOK,
        "webhook_format": "discord",
        "currency": "EUR",
        "timezone": "Europe/Athens",
    }
    data.update(changes)
    return data


def test_settings_saves_every_field(site):
    client = site.signed_in()
    page = client.get("/settings")
    assert page.status_code == 200 and 'aria-current="page">Settings' in page.text
    response = post_form(client, "/settings", full_form())
    assert response.status_code == 303 and response.headers["location"] == "/settings"
    user = site.accounts.get_user_by_email("member@example.com")
    chosen = user.settings
    assert user.name == "Eleni"
    assert chosen.min_score == 55.5 and chosen.min_probability == 70
    assert chosen.verdicts == ("temporary_fear", "mixed", "unclear")
    assert chosen.only_watchlist and not chosen.thesis_changes  # an unticked box is "off"
    assert chosen.watchlist == ("ETE.AT", "BRK-B", "AMD")
    assert (chosen.webhook_url, chosen.webhook_format) == (HOOK, "discord")
    assert (chosen.currency, chosen.timezone) == ("EUR", "Europe/Athens")
    assert user.alerts_since == NOW
    shown = client.get("/settings").text
    assert "Settings saved. Your alerts start now" in shown
    assert "Times in Europe/Athens." in shown and "ETE.AT, BRK-B, AMD" in shown


def test_settings_errors_are_shown_next_to_the_fields_and_nothing_is_saved(site):
    client = site.signed_in()
    response = post_form(
        client,
        "/settings",
        full_form(min_score="lots", watchlist="AMD, not a symbol!", verdicts=[], webhook_url="http://127.0.0.1/x"),
    )
    assert response.status_code == 400
    text = response.text
    assert "Nothing was saved: 4 settings need a fix" in text
    assert "The minimum score must be a number from 0 to 100." in text
    assert "Choose at least one verdict to be alerted about." in text
    assert "Not a company&#39;s Yahoo Finance symbol" in text
    assert "must start with https://" in text
    assert 'value="lots"' in text  # what was typed stays in the form
    assert site.accounts.get_user_by_email("member@example.com").settings.min_score == 65


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("https://127.0.0.1/hook", "public internet"),
        ("https://private.example.com/hook", "public internet"),
        ("https://169.254.169.254/latest", "public internet"),
        ("https://[fdaa::3]/hook", "public internet"),
        ("https://user:pw@hooks.example.com/x", "user name or password"),
        ("http://hooks.example.com/x", "https://"),
    ],
)
def test_webhooks_must_be_public_https_addresses(site, url, message):
    client = site.signed_in()
    response = post_form(client, "/settings", full_form(webhook_url=url))
    assert response.status_code == 400 and message in response.text


def test_channels_are_offered_only_when_the_server_has_them(tmp_path):
    bare = Site(tmp_path / "bare")
    client = bare.signed_in()
    page = client.get("/settings").text
    assert 'name="email_alerts"' not in page and 'name="telegram_chat_id"' not in page
    assert "Email and Telegram alerts aren't set up on this server" in page
    post_form(client, "/settings", full_form(email_alerts="on", telegram_chat_id="42", webhook_url=""))
    chosen = bare.accounts.get_user_by_email("member@example.com").settings
    assert not chosen.email_alerts and chosen.telegram_chat_id is None  # ignored: not offered

    notify = NotifySettings(smtp_host="smtp.example.com", smtp_from="dips@example.com", telegram_bot_token="123:abc")
    served = Site(tmp_path / "served", notify=notify)
    client = served.signed_in()
    page = client.get("/settings").text
    assert 'name="email_alerts"' in page and 'name="telegram_chat_id"' in page
    post_form(client, "/settings", full_form(email_alerts="on", telegram_chat_id="-100123", webhook_url=""))
    chosen = served.accounts.get_user_by_email("member@example.com").settings
    assert chosen.email_alerts and chosen.telegram_chat_id == "-100123"


def test_an_unchanged_webhook_is_not_looked_up_again(site):
    client = site.signed_in()
    post_form(client, "/settings", full_form())
    site.ctx.resolver = lambda host, port: []  # DNS trouble now: other settings can still be saved
    assert post_form(client, "/settings", full_form(min_score="70")).status_code == 303
    assert site.accounts.get_user_by_email("member@example.com").settings.min_score == 70


def test_send_test_alert_reports_each_channel_and_is_rate_limited(tmp_path):
    http = FakeSession({HOOK: 200})
    site = Site(tmp_path, http=http)
    client = site.signed_in()
    assert post_form(client, "/settings/test").headers["location"] == "/settings"
    assert "Set up an alert channel first" in client.get("/settings").text
    post_form(client, "/settings", full_form(webhook_format="slack"))

    response = post_form(client, "/settings/test")
    assert response.status_code == 303
    [call] = [call for call in http.calls if call["url"] == HOOK]
    assert "dip-scanner: test alert" in str(call["json"])
    assert "Slack webhook: test alert sent." in client.get("/settings").text

    http.routes[HOOK] = 404
    post_form(client, "/settings/test")
    [flash] = re.findall(r'<div class="flash flash-error"><p>(.*?)</p>', client.get("/settings").text)
    assert flash.startswith("Slack webhook: the test alert failed") and "very-secret-hook-token" not in flash
    for _ in range(3):
        post_form(client, "/settings/test")
    blocked = post_form(client, "/settings/test")  # the 6th in 15 minutes (the first had no channel)
    assert blocked.status_code == 429 and "test alerts in the last 15 minutes" in blocked.text


def test_the_test_alert_result_is_where_the_page_opens(tmp_path):
    """The redirect used to jump to #channels, a screen or two below the result at the top of the page: on a phone
    the button seemed to do nothing."""
    site = Site(tmp_path, http=FakeSession({HOOK: 200}))
    client = site.signed_in()
    post_form(client, "/settings", full_form(webhook_format="slack"))
    response = post_form(client, "/settings/test")
    assert response.status_code == 303 and response.headers["location"] == "/settings"
    page = client.get("/settings").text
    assert page.index("Slack webhook: test alert sent.") < page.index('id="alerts"')  # above the first card


def test_the_test_alert_button_saves_and_tests_what_is_on_the_screen(tmp_path):
    """A webhook typed but not saved used to be thrown away, and the test went to the old one."""
    other = "https://hooks.example.com/services/T000/B000/the-new-hook"
    http = FakeSession({HOOK: 200, other: 200})
    site = Site(tmp_path, http=http)
    client = site.signed_in()
    post_form(client, "/settings", full_form(webhook_format="generic"))
    page = client.get("/settings").text
    assert 'formaction="/settings/test"' in page  # the button is part of the settings form
    response = post_form(client, "/settings/test", full_form(webhook_url=other, webhook_format="generic"))
    assert response.headers["location"] == "/settings"
    assert [call["url"] for call in http.calls] == [other]
    assert site.accounts.get_user_by_email("member@example.com").settings.webhook_url == other
    shown = client.get("/settings").text
    assert "Settings saved." in shown and "Generic webhook: test alert sent." in shown
    bad = post_form(client, "/settings/test", full_form(webhook_url="http://127.0.0.1/x"))
    assert bad.status_code == 400 and "Nothing was saved" in bad.text and len(http.calls) == 1


def test_a_discord_address_gets_the_discord_format(tmp_path):
    """Slack's payload is an empty message to Discord (400): every alert would fail, unseen, for a day."""
    discord = "https://discord.com/api/webhooks/123/SECRET-TOKEN"
    site = Site(tmp_path)
    client = site.signed_in()
    post_form(client, "/settings", full_form(webhook_url=discord, webhook_format="slack"))
    assert site.accounts.get_user_by_email("member@example.com").settings.webhook_format == "discord"
    assert "The webhook format is Discord, to match the webhook&#39;s address." in client.get("/settings").text
    post_form(
        client, "/settings", full_form(webhook_url=HOOK, webhook_format="generic")
    )  # any other address: as chosen
    assert site.accounts.get_user_by_email("member@example.com").settings.webhook_format == "generic"


def test_a_failed_test_alert_says_nothing_about_the_servers_network(tmp_path):
    raw = requests.ConnectionError(
        "HTTPSConnectionPool(host='hooks.example.com', port=443): Max retries exceeded (Caused by NewConnectionError("
        "'Failed to establish a new connection: [Errno 111] Connection refused'))"
    )
    http = FakeSession({HOOK: raw})
    site = Site(tmp_path, http=http)
    client = site.signed_in()
    post_form(client, "/settings", full_form(webhook_format="slack"))
    post_form(client, "/settings/test")
    [flash] = re.findall(r'<div class="flash flash-error"><p>(.*?)</p>', client.get("/settings").text)
    assert flash == (
        "Slack webhook: the test alert failed: Couldn&#39;t reach the slack webhook at hooks.example.com. Check the "
        "address, or try again later."
    )


def test_changing_the_password_signs_out_the_other_sessions(site):
    client = site.signed_in()
    other = site.signed_in()
    wrong = post_form(
        client, "/settings/password", {"current_password": "nope", "new_password": "x" * 12, "confirm": "x" * 12}
    )
    assert wrong.status_code == 400 and "Your current password isn&#39;t right." in wrong.text
    differ = post_form(
        client, "/settings/password", {"current_password": PASSWORD, "new_password": "x" * 12, "confirm": "y" * 12}
    )
    assert differ.status_code == 400 and "don&#39;t match" in differ.text
    new = "a brand new password"
    done = post_form(client, "/settings/password", {"current_password": PASSWORD, "new_password": new, "confirm": new})
    assert done.status_code == 303 and "dsid" in cookies_set(done)
    assert client.get("/settings").status_code == 200  # this browser stays signed in
    assert other.get("/settings").status_code == 303  # the other one is signed out
    assert site.sign_in("member@example.com", client=site.new_client()).status_code == 400
    assert site.sign_in("member@example.com", new, client=site.new_client()).status_code == 303


def test_wrong_current_passwords_are_rate_limited(site):
    client = site.signed_in()
    for _ in range(10):
        post_form(
            client, "/settings/password", {"current_password": "nope", "new_password": "x" * 12, "confirm": "x" * 12}
        )
    blocked = post_form(
        client, "/settings/password", {"current_password": PASSWORD, "new_password": "x" * 12, "confirm": "x" * 12}
    )
    assert blocked.status_code == 429 and "Too many failed sign-ins" in blocked.text


def test_sign_out_everywhere_else(site):
    client = site.signed_in()
    other = site.signed_in()
    assert "also signed in on 1 other device or browser" in client.get("/settings").text
    post_form(client, "/settings/sessions")
    assert "Signed out of 1 other session." in client.get("/settings").text
    assert other.get("/settings").status_code == 303


def test_change_watchlist_adds_and_removes_symbols(site):
    from dip_scanner.web.account import change_watchlist

    user = site.user()
    user = change_watchlist(site.ctx, user, add="brk.b")
    user = change_watchlist(site.ctx, user, add="AMD")
    assert user.settings.watchlist == ("BRK-B", "AMD")
    assert change_watchlist(site.ctx, user, remove="BRK.B").settings.watchlist == ("AMD",)
    with pytest.raises(accounts_module.AccountError):
        change_watchlist(site.ctx, user, add="not a symbol!")


# --- "Analyse now" -------------------------------------------------------------------------------------------------


def analyse_ok(ticker: str, now: datetime):
    return make_opportunity(ticker=ticker, created=now)


def test_analyse_now_runs_a_job_and_goes_to_the_idea(tmp_path):
    site = Site(tmp_path, analyse=analyse_ok)
    client = site.signed_in()
    response = post_form(client, "/analyze", {"ticker": "amd"})
    assert response.status_code == 303 and response.headers["location"] == "/jobs/1"
    job = site.accounts.get_job(1)
    assert (job.status, job.ticker) == ("done", "AMD")
    done = client.get("/jobs/1")
    assert done.status_code == 303 and done.headers["location"] == f"/ideas/{job.opportunity_id}"
    user = site.accounts.get_user_by_email("member@example.com")
    [delivery] = site.store.deliveries(recipient=user.recipient_key)
    assert (delivery["opportunity_id"], delivery["kind"], delivery["sent"]) == (job.opportunity_id, "alert", True)
    assert site.store.unnotified() == []  # never alerted to anybody later


def test_a_job_page_reloads_while_the_analysis_waits(tmp_path):
    parked = Parked()
    site = Site(tmp_path, analyse=analyse_ok, executor=parked)
    client = site.signed_in()
    post_form(client, "/analyze", {"ticker": "AMD"})
    post_form(client, "/analyze", {"ticker": "AMD"})  # the same ticker again: the same job
    assert len(parked.calls) == 1
    other = site.signed_in("other@example.com")
    post_form(other, "/analyze", {"ticker": "NVDA"})
    waiting = other.get("/jobs/2")
    assert waiting.status_code == 200
    assert '<meta http-equiv="refresh" content="5">' in waiting.text
    assert "1 analysis is ahead of it." in waiting.text
    parked.run()
    assert client.get("/jobs/1").status_code == 303


def test_a_running_analysis_says_how_long_a_debate_takes(tmp_path):
    """A debate is three rounds of model calls (openings, rebuttals, a ruling): minutes, not "under a minute"."""
    parked = Parked()
    site = Site(tmp_path, analyse=analyse_ok, executor=parked)
    client = site.signed_in()
    post_form(client, "/analyze", {"ticker": "AMD"})
    site.accounts.start_job(1)
    assert "this usually takes under a minute" in client.get("/jobs/1").text  # one model
    site.ctx.settings = replace(site.settings, llm=replace(site.settings.llm, analysis_mode="debate"))
    page = client.get("/jobs/1").text
    assert "this usually takes a few minutes" in page and "under a minute" not in page


def test_a_failed_analysis_says_why(tmp_path):
    def no_prices(ticker, now):
        raise PriceError(f"No prices for {ticker} (unknown symbol).")

    site = Site(tmp_path, analyse=no_prices)
    client = site.signed_in()
    post_form(client, "/analyze", {"ticker": "XYZ"})
    page = client.get("/jobs/1")
    assert page.status_code == 200
    assert "No prices for XYZ (unknown symbol). It doesn't count toward your limit." in page.text
    assert "http-equiv" not in page.text
    assert "Try again" not in page.text  # it would only fail the same way


def test_failures_before_the_model_is_asked_do_not_use_up_the_limit(tmp_path):
    """No prices, Yahoo down, a setup problem, a restart: a member's retries cost nothing, so they can't cost one of
    their analyses either. A failure after the model answered (paid for) does count."""
    from dip_scanner.llm import LLMError, LLMSetupError
    from dip_scanner.prices import PriceFetchError

    outcomes = {"now": PriceError("Yahoo Finance has no prices for ZZZZQ.")}

    def analyse(ticker, now):
        if isinstance(outcomes["now"], Exception):
            raise outcomes["now"]
        return analyse_ok(ticker, now)

    site = Site(tmp_path, analyse=analyse, web={"analyze_limit_per_user": 2})
    client = site.signed_in()
    user = site.accounts.get_user_by_email("member@example.com")
    for number in range(5):
        assert post_form(client, "/analyze", {"ticker": f"ZZ{number}"}).status_code == 303
    assert site.ctx.jobs.remaining(user) == 2
    outcomes["now"] = PriceFetchError("Yahoo Finance couldn't be reached.")
    post_form(client, "/analyze", {"ticker": "AMD"})
    assert "Try again" in client.get("/jobs/6").text  # Yahoo may answer in a minute
    outcomes["now"] = LLMSetupError("The model can't be used.")
    post_form(client, "/analyze", {"ticker": "NVDA"})
    assert site.ctx.jobs.remaining(user) == 2
    outcomes["now"] = LLMError("The model's reply was unusable.")
    post_form(client, "/analyze", {"ticker": "INTC"})
    assert site.ctx.jobs.remaining(user) == 1  # the model was asked: it counts
    outcomes["now"] = None
    assert post_form(client, "/analyze", {"ticker": "AAPL"}).status_code == 303
    assert site.ctx.jobs.remaining(user) == 0
    last = client.get("/jobs/8").text  # the LLMError one, with nothing left: no "Try again" leading to a 429
    assert "didn't finish" in last and "Try again" not in last


def test_the_command_lines_hint_reads_as_the_websites_on_a_job_page(tmp_path):
    def renamed(ticker, now):
        raise PriceError(
            f"Yahoo Finance has no prices for {ticker}. Yahoo's search finds ALWN.AT (Allwyn) for OPAP: try "
            "`dip-scanner analyze ALWN.AT`."
        )

    site = Site(tmp_path, analyse=renamed)
    client = site.signed_in()
    post_form(client, "/analyze", {"ticker": "OPAP.AT"})
    page = client.get("/jobs/1").text
    assert "open ALWN.AT&#39;s page to analyse it" in page and "dip-scanner analyze" not in page


def test_an_unexpected_failure_is_logged_but_not_shown(tmp_path, caplog):
    def broken(ticker, now):
        raise RuntimeError("internal detail /srv/secret")

    site = Site(tmp_path, analyse=broken)
    client = site.signed_in()
    post_form(client, "/analyze", {"ticker": "AMD"})
    page = client.get("/jobs/1").text
    assert "error on the server" in page and "internal detail" not in page
    assert "internal detail" in caplog.text


def test_members_have_a_daily_limit_and_admins_none(tmp_path):
    site = Site(tmp_path, analyse=analyse_ok, web={"analyze_limit_per_user": 2})
    member = site.signed_in()
    for ticker in ("AMD", "NVDA"):
        assert post_form(member, "/analyze", {"ticker": ticker}).status_code == 303
    blocked = post_form(member, "/analyze", {"ticker": "INTC"})
    assert blocked.status_code == 429 and "You have used your 2 manual analyses" in blocked.text
    site.clock.advance(hours=24, minutes=1)
    assert post_form(member, "/analyze", {"ticker": "INTC"}).status_code == 303

    admin = site.signed_in("admin@example.com", role="admin")
    for ticker in ("AMD", "NVDA", "INTC", "AAPL"):
        assert post_form(admin, "/analyze", {"ticker": ticker}).status_code == 303
    assert "No limit for admins." in admin.get("/settings").text


def test_a_burst_of_analyses_is_limited_for_everyone(tmp_path):
    site = Site(tmp_path, analyse=analyse_ok)
    admin = site.signed_in("admin@example.com", role="admin")
    for number in range(10):
        assert post_form(admin, "/analyze", {"ticker": f"T{number}"}).status_code == 303
    assert post_form(admin, "/analyze", {"ticker": "T10"}).status_code == 429


def test_analyse_now_refuses_what_it_cannot_do(tmp_path):
    site = Site(tmp_path, analyse=None)
    client = site.signed_in()
    response = post_form(client, "/analyze", {"ticker": "AMD", "next": "/ideas/1"})
    assert response.headers["location"] == "/ideas/1"
    assert "Manual analyses aren&#39;t available right now." in client.get("/settings").text  # a member's words
    admin = site.signed_in("admin@example.com", role="admin")
    post_form(admin, "/analyze", {"ticker": "AMD"})
    assert "Manual analyses aren&#39;t available on this server." in admin.get("/settings").text
    works = Site(tmp_path / "other", analyse=analyse_ok)
    client = works.signed_in()
    bad = post_form(client, "/analyze", {"ticker": "not a symbol!", "next": "https://evil.com"})
    assert bad.headers["location"] == "/"
    assert "isn&#39;t a Yahoo Finance symbol" in client.get("/settings").text


def test_jobs_of_other_users_are_not_found(tmp_path):
    site = Site(tmp_path, analyse=analyse_ok, executor=Parked())
    owner = site.signed_in()
    post_form(owner, "/analyze", {"ticker": "AMD"})
    assert owner.get("/jobs/1").status_code == 200
    assert site.signed_in("other@example.com").get("/jobs/1").status_code == 404
    assert site.signed_in("admin@example.com", role="admin").get("/jobs/1").status_code == 200
    assert owner.get("/jobs/99").status_code == 404
    assert owner.get("/jobs/abc").status_code == 404


def test_unfinished_jobs_are_marked_failed_when_the_website_starts(tmp_path):
    site = Site(tmp_path, analyse=analyse_ok, executor=Parked())
    client = site.signed_in()
    post_form(client, "/analyze", {"ticker": "AMD"})
    with TestClient(site.app, base_url=BASE):
        job = site.accounts.get_job(1)
        user = site.accounts.get_user_by_email("member@example.com")
        assert site.ctx.jobs.remaining(user) == site.settings.web.analyze_limit_per_user
    assert job.status == "failed" and "restart" in job.error
    assert not job.counts and job.can_retry  # a restart isn't the member's doing


def slow_analysis():
    """An analysis that starts and then waits for release (a debate's model calls taking their time)."""
    started, release = threading.Event(), threading.Event()

    def analyse(ticker, now):
        started.set()
        release.wait(10)
        return analyse_ok(ticker, now)

    return analyse, started, release


def test_a_shutdown_waits_for_the_running_analysis_before_closing_the_database(tmp_path):
    """SIGTERM on a deploy: the paid analysis that is running is finished and stored, not cut off under a closed
    database (and its usage lost), and no new one starts."""
    analyse, started, release = slow_analysis()
    site = Site(tmp_path, analyse=analyse, executor=ThreadPoolExecutor(max_workers=1))
    user = site.user()
    client = TestClient(site.app, base_url=BASE)
    client.__enter__()
    job = site.ctx.jobs.submit(user, "AMD")
    assert started.wait(5)
    closer = threading.Thread(target=client.__exit__, args=(None, None, None))
    closer.start()
    time.sleep(0.3)
    assert closer.is_alive()  # the shutdown waits for it
    with pytest.raises(accounts_module.AccountError, match="restarting"):
        site.ctx.jobs.submit(user, "NVDA")
    release.set()
    closer.join(10)
    assert not closer.is_alive()
    with Store(site.settings.data_dir / DATABASE_NAME) as store:
        done = accounts_module.Accounts(store).get_job(job.id)
        assert done.status == "done" and store.get_opportunity(done.opportunity_id) is not None


def test_an_analysis_that_outlasts_the_shutdown_budget_is_failed_and_not_counted(tmp_path, caplog):
    analyse, started, release = slow_analysis()
    site = Site(tmp_path, analyse=analyse, executor=ThreadPoolExecutor(max_workers=1), shutdown_budget=0.2)
    user = site.user()
    try:
        with TestClient(site.app, base_url=BASE):
            job = site.ctx.jobs.submit(user, "AMD")
            assert started.wait(5)
        with Store(site.settings.data_dir / DATABASE_NAME) as store:
            accounts = accounts_module.Accounts(store, clock=site.clock)
            failed = accounts.get_job(job.id)
            assert (failed.status, failed.failure) == ("failed", "restart") and not failed.counts
            assert "doesn't count toward your limit" in failed.error
            assert accounts.count_jobs(user.id) == 0
        assert "Cannot operate on a closed database" not in caplog.text
    finally:
        release.set()
        site.ctx.jobs._pool().shutdown(wait=True)  # the abandoned analysis ends (on the closed database)


# --- errors, headers, static files ---------------------------------------------------------------------------------


def test_every_response_carries_the_security_headers(site):
    client = site.signed_in()
    for path in ("/login", "/settings", "/healthz", "/static/app.css", "/nope", "/robots.txt"):
        response = client.get(path)
        headers = response.headers
        assert headers["content-security-policy"] == CSP, path
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["referrer-policy"] == "same-origin"
        assert "camera=()" in headers["permissions-policy"]
        assert headers["x-frame-options"] == "DENY"
        assert headers["strict-transport-security"] == "max-age=31536000"
    assert client.get("/settings").headers["cache-control"] == "no-store"
    assert "max-age" in client.get("/static/app.css").headers["cache-control"]
    # A missing static file is an error page with the visitor's name and form token (and maybe a renewed session
    # cookie): never cacheable.
    missing = client.get("/static/nope-x.css")
    assert missing.status_code == 404 and missing.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in CSP and "script-src 'self'" in CSP and "unsafe-inline" not in CSP


def test_healthz_says_503_when_the_database_is_unusable(site):
    assert site.client.get("/healthz").json() == {
        "status": "ok",
        "db": "ok",
        "scanner": "disabled",
        "last_cycle": None,
    }
    site.store.close()
    response = site.client.get("/healthz")
    assert response.status_code == 503
    assert response.json() == {"status": "error", "db": "error", "scanner": "disabled", "last_cycle": None}


def test_error_pages_are_friendly(site):
    client = site.signed_in()
    missing = client.get("/no/such/page")
    assert missing.status_code == 404 and "Page not found" in missing.text and "Back to the ideas" in missing.text
    method = client.get("/logout")
    assert method.status_code == 405 and "Not allowed" in method.text
    assert client.get("/ideas/999").status_code == 404
    anonymous = site.new_client().get("/nope")
    assert anonymous.status_code == 404 and "Go to the home page" in anonymous.text


def test_a_server_error_shows_no_details(site, caplog):
    def explode():
        raise RuntimeError("secret internals at /srv/app")

    site.app.add_api_route("/explode", explode)
    response = site.client.get("/explode")
    assert response.status_code == 500
    assert "Something went wrong" in response.text
    assert "secret internals" not in response.text and "Traceback" not in response.text
    assert response.headers["content-security-policy"] == CSP
    assert "secret internals" in caplog.text


def test_oversized_requests_are_refused(site):
    response = site.client.post("/login", content=b"x" * (web_app.MAX_BODY_BYTES + 1))
    assert response.status_code == 413 and response.headers["content-security-policy"] == CSP
    assert site.client.post("/login", headers={"content-length": "abc"}, content=b"").status_code == 413


def test_the_access_log_has_no_tokens_or_queries(site, caplog):
    caplog.set_level(logging.INFO, logger="dip_scanner.web.access")
    token = site.accounts.create_invite(created_by=None)
    site.client.get(f"/invite/{token}?x=secret-query")
    site.client.get("/login?next=%2Fsecret-path")
    assert "GET /invite/… 200" in caplog.text and "GET /login 200" in caplog.text
    assert token not in caplog.text and "secret-query" not in caplog.text and "secret-path" not in caplog.text


def test_static_files_robots_and_favicon(site):
    css = site.client.get(static_url("app.css"))
    assert css.status_code == 200 and "--accent" in css.text
    assert re.fullmatch(r"/static/app\.js\?v=[0-9a-f]{10}", static_url("app.js"))
    assert site.client.get("/static/../app.py").status_code == 404
    assert site.client.get("/robots.txt").text == "User-agent: *\nDisallow: /\n"
    assert site.client.get("/favicon.ico").headers["content-type"] == "image/svg+xml"


def test_the_base_layout(site):
    client = site.signed_in()
    page = client.get("/").text
    for text in ("Dip scanner", "Not investment advice. The scanner never trades.", "Track record", "Sign out"):
        assert text in page
    assert 'href="/static/app.css?v=' in page and 'src="/static/app.js?v=' in page
    login = site.new_client().get("/login").text
    assert "Not investment advice. The scanner never trades." in login and "Sign out" not in login


# --- templates -----------------------------------------------------------------------------------------------------


def test_templates_have_no_inline_scripts_styles_or_handlers():
    """The Content-Security-Policy would block them; pages use classes and app.js's data-attributes instead."""
    for path in TEMPLATES.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"<script(?![^>]*\bsrc=)", text), path
        assert "<style" not in text, path
        assert not re.search(r"\sstyle\s*=", text), path
        assert not re.search(r"\son[a-z]+\s*=", text), path
        assert not re.search(r"\|\s*safe\b(?!_)", text) and "autoescape false" not in text, path


def test_untrusted_text_is_escaped(site):
    evil = "<script>alert(1)</script>"
    analysis = make_analysis(thesis=f"Thesis {evil}")
    site.store.add_opportunity(make_opportunity(company=f"Evil {evil} Inc.", analysis=analysis))
    client = site.signed_in()
    user = site.accounts.get_user_by_email("member@example.com")
    site.accounts.set_name(user.id, f"<img src=x onerror=alert(1)>{evil}")
    for path in ("/", "/ideas/1", "/settings"):
        text = client.get(path).text
        assert evil not in text and "<img src=x" not in text, path
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in client.get("/ideas/1").text


def test_pages_are_shown_in_the_readers_time_zone_and_currency(site):
    site.store.add_opportunity(make_opportunity(fx_rates={"EUR": 0.9}))
    client = site.signed_in()
    user = site.accounts.get_user_by_email("member@example.com")
    site.accounts.update_settings(user.id, replace(user.settings, timezone="Europe/Athens", currency="EUR"))
    page = client.get("/ideas/1").text
    assert "2026-09-25 18:00 EEST" in page
    assert "$142.50" in page and "≈ €128.25" in page
    assert "1 USD = 0.9 EUR at the analysis" in page


def test_flash_messages_are_signed(site):
    client = site.signed_in()
    client.cookies.set("dflash", "W1sib2siLCAiaGFja2VkIl1d.forged", domain="dips.example.com")
    assert "hacked" not in client.get("/settings").text


def test_filters_and_helpers():
    now = NOW
    assert relative_time(now - timedelta(seconds=20), now) == "just now"
    assert relative_time(now - timedelta(minutes=5), now) == "5 min ago"
    assert relative_time(now + timedelta(minutes=3), now) == "in 3 min"
    assert relative_time(now - timedelta(hours=3), now) == "3 h ago"
    assert relative_time(now - timedelta(days=1), now) == "24 h ago"
    assert relative_time(now - timedelta(days=3), now) == "3 days ago"
    assert relative_time((now - timedelta(minutes=10)).isoformat(), now) == "10 min ago"
    assert relative_time(None, now) == "–"

    filters = web_app.FILTERS
    opp = make_opportunity(fx_rates={"EUR": 0.9}).in_currency("EUR")
    assert filters["money"](132, opp) == "$132.00 ≈ €118.80"
    assert filters["approx"](132, opp) == "≈ €118.80"
    assert filters["money"](None, opp) == "–"
    assert filters["pct"](17.94) == "+17.9%" and filters["pct"](-0.04) == "+0.0%"
    assert filters["percent"](68) == "68%"
    assert [filters["band"](score) for score in (85, 70, 55, 20)] == ["strong", "good", "fair", "weak"]
    assert filters["verdict"]("temporary_fear") == "Temporary fear"
    assert filters["plural"](1, "idea") == "1 idea" and filters["plural"](3, "idea") == "3 ideas"
    assert filters["host"]("https://www.reuters.com/x") == "reuters.com" and filters["host"]("javascript:x") == ""
    from zoneinfo import ZoneInfo

    with display_zone_as(ZoneInfo("Europe/Athens")):
        assert filters["when"](NOW) == "2026-09-25 18:00 EEST"
        assert filters["day"](NOW) == "Fri 25 Sep 2026"

    page = paginate(51, "3", size=25)
    assert (page.number, page.pages, page.offset, page.has_previous, page.has_next) == (3, 3, 50, True, False)
    assert paginate(10, "x").number == 1 and paginate(10, "99").number == 1 and paginate(0, 1).pages == 1

    class FakeRequest:
        class url:
            path = "/news"

        class query_params:
            @staticmethod
            def multi_items():
                return [("hours", "24"), ("page", "2"), ("ticker", "AMD")]

    assert url_with(FakeRequest, page=None, hours=72) == "/news?ticker=AMD&hours=72"
    assert url_with(FakeRequest, ticker=["A", "B"]) == "/news?hours=24&page=2&ticker=A&ticker=B"


def test_percentages_are_coloured_as_they_are_shown():
    macros = make_templates().env.from_string('{% import "_macros.html" as ui %}{{ ui.pct(value) }}')
    assert macros.render(value=2.34) == '<span class="num up">+2.3%</span>'
    assert macros.render(value=-0.04) == '<span class="num">+0.0%</span>'  # not a red "+0.0%"
    assert macros.render(value=-0.06) == '<span class="num down">-0.1%</span>'
    assert macros.render(value=None) == '<span class="num">–</span>'


def test_the_app_needs_a_secret_key(tmp_path):
    from dip_scanner.config import ConfigError

    with pytest.raises(ConfigError, match="SECRET_KEY isn't set"):
        create_app(
            settings=Settings(data_dir=tmp_path),
            config=ScannerConfig(),
            feeds=[],
            store_path=tmp_path / DATABASE_NAME,
        )
