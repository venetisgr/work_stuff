"""The website behind its front door (the Next.js app on Vercel): PROXY_SECRET on every request but /healthz, the
visitor's address from x-dip-client-ip only with the secret, TRUSTED_ORIGINS for the Origin check, and redirects that
stay relative when the request reaches the Fly address (Host) on behalf of the Vercel one (X-Forwarded-Host).

The front door is simulated with TestClient's default headers, as frontend/src/lib/forward.ts sends them. No network,
no sleeping.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta

import pytest
from conftest import make_opportunity, make_stats
from fastapi.testclient import TestClient
from test_web_api import error_of, problems
from test_web_pages import (  # noqa: F401 (fast_scrypt and no_network are autouse fixtures)
    NOW,
    PASSWORD,
    Site,
    fast_scrypt,
    no_network,
    token_on,
)

from dip_scanner.config import DATABASE_NAME, ConfigError, ScannerConfig, Settings, WebSettings
from dip_scanner.web import app as app_module
from dip_scanner.web.app import create_app, relative_location

FLY = "https://my-dips.fly.dev"
VERCEL = "https://dips.example.com"
PREVIEW = "https://my-dips-git-new-chart-my-team.vercel.app"
SECRET = "front-door-secret-0123456789-abcdefghij"
VISITOR = "203.0.113.7"


def proxied_site(tmp_path, **web) -> Site:
    values = {"base_url": VERCEL, "proxy_secret": SECRET, **web}

    def analyse(ticker, now):
        return make_opportunity(ticker=ticker, created=now, stats=make_stats(ticker=ticker, as_of=now))

    return Site(tmp_path, web=values, analyse=analyse)


def front_door(site: Site, *, secret: str | None = SECRET, client_ip: str | None = VISITOR, **extra) -> TestClient:
    """A client that reaches the Fly address with the headers the front door adds (and a browser's Origin)."""
    headers = {"x-forwarded-host": "dips.example.com", "x-forwarded-proto": "https", "origin": VERCEL, **extra}
    if secret is not None:
        headers["x-dip-proxy-secret"] = secret
    if client_ip is not None:
        headers["x-dip-client-ip"] = client_ip
    return TestClient(site.app, base_url=FLY, follow_redirects=False, headers=headers)


def sign_in(client: TestClient, site: Site, email: str = "member@example.com", *, role: str = "member"):
    if site.accounts.get_user_by_email(email) is None:
        site.accounts.create_user(email, role=role, password=PASSWORD)
    page = client.get("/login")
    assert page.status_code == 200, page.text
    return client.post("/login", data={"email": email, "password": PASSWORD, "csrf_token": token_on(page.text)})


def location(response) -> str:
    assert response.status_code in (301, 302, 303, 307, 308), (response.status_code, response.text[:200])
    value = response.headers["location"]
    assert value.startswith("/") and not value.startswith("//"), value
    assert "fly.dev" not in value and "://" not in value, value
    return value


@pytest.fixture
def site(tmp_path) -> Site:
    return proxied_site(tmp_path)


# --- the secret ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/login", "/ideas/1", "/static/app.css", "/favicon.ico", "/robots.txt", "/x"])
@pytest.mark.parametrize("secret", [None, "wrong", SECRET[:-1], SECRET + "x", ""])
def test_without_the_secret_nothing_answers(site, path, secret):
    response = front_door(site, secret=secret).get(path)
    assert response.status_code == 403
    assert response.headers["content-type"].startswith("text/html")
    assert f'<a href="{VERCEL}">{VERCEL}</a>' in response.text
    assert "only answers through the website's front door" in response.text
    assert response.headers["x-content-type-options"] == "nosniff" and response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers


def test_without_the_secret_the_api_answers_json(site):
    response = front_door(site, secret=None).get("/api/v1/me")
    error = error_of(response, 403, "forbidden")
    assert VERCEL in error["message"]


def test_without_the_secret_posts_are_refused_before_anything_happens(site):
    site.accounts.create_user("member@example.com", password=PASSWORD)
    response = front_door(site, secret=None).post("/login", data={"email": "member@example.com", "password": PASSWORD})
    assert response.status_code == 403 and "set-cookie" not in response.headers
    assert site.accounts.list_sessions(site.user().id) == []


def test_two_secret_headers_are_refused(site):
    client = TestClient(site.app, base_url=FLY, follow_redirects=False)
    response = client.get("/login", headers=[("x-dip-proxy-secret", SECRET), ("x-dip-proxy-secret", "other")])
    assert response.status_code == 403


def test_healthz_answers_without_the_secret(site):
    response = TestClient(site.app, base_url="http://10.0.0.5:8080").get("/healthz")
    assert response.status_code == 200 and response.json()["status"] == "ok"


def test_the_secret_is_compared_in_constant_time(site, monkeypatch):
    compared = []
    real = app_module.hmac.compare_digest

    def spy(given, wanted):
        compared.append((given, wanted))
        return real(given, wanted)

    monkeypatch.setattr(app_module.hmac, "compare_digest", spy)
    assert front_door(site).get("/login").status_code == 200
    assert front_door(site, secret="nope").get("/login").status_code == 403
    assert (SECRET.encode(), SECRET.encode()) in compared and (b"nope", SECRET.encode()) in compared


def test_with_the_secret_the_site_works(site):
    client = front_door(site)
    assert location(sign_in(client, site)) == "/"
    assert client.get("/").status_code == 200
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/api/v1/me").json()["user"]["email"] == "member@example.com"


def test_without_proxy_secret_nothing_is_asked_for(tmp_path):
    site = Site(tmp_path)
    assert TestClient(site.app, base_url=FLY).get("/login").status_code == 200


def test_a_front_door_needs_base_url(tmp_path):
    settings = Settings(data_dir=tmp_path, web=WebSettings(secret_key="s" * 40, proxy_secret=SECRET))
    with pytest.raises(ConfigError, match="set BASE_URL to the front door's address"):
        create_app(
            settings=settings, config=ScannerConfig(), feeds=[], store_path=tmp_path / DATABASE_NAME, prices=object()
        )


# --- the visitor's address -----------------------------------------------------------------------------------------


def session_ip(site: Site, email: str = "member@example.com") -> str | None:
    return site.accounts.list_sessions(site.user(email).id)[0].ip


def test_the_visitors_address_comes_from_the_front_door(site):
    sign_in(front_door(site, client_ip="2001:DB8::1"), site)
    assert session_ip(site) == "2001:db8::1"


@pytest.mark.parametrize("value", ["", "not-an-ip", "203.0.113.7, 10.0.0.1", "203.0.113.7:443", "999.1.1.1"])
def test_an_unusable_address_is_ignored(site, value):
    sign_in(front_door(site, client_ip=value), site)
    assert session_ip(site) == "testclient"  # the connection's (Vercel's, in production)


def test_behind_the_front_door_on_fly_the_fly_address_is_the_fallback(site):
    site.ctx.trust_fly_client_ip = True
    sign_in(front_door(site, client_ip=None, **{"fly-client-ip": "76.76.21.21"}), site)
    assert session_ip(site) == "76.76.21.21"


def test_the_address_header_counts_only_with_the_secret(tmp_path):
    site = Site(tmp_path)  # no PROXY_SECRET: nobody may name an address
    client = TestClient(site.app, base_url=FLY, follow_redirects=False, headers={"x-dip-client-ip": VISITOR})
    sign_in(client, site)
    assert session_ip(site) == "testclient"


def test_sign_in_limits_count_each_visitor_not_the_front_door(site):
    site.accounts.create_user("member@example.com", password=PASSWORD)
    attacker = front_door(site, client_ip="198.51.100.9")
    for _ in range(10):
        page = attacker.get("/login")
        data = {"email": f"x{_}@example.com", "password": "wrong password", "csrf_token": token_on(page.text)}
        assert attacker.post("/login", data=data).status_code == 400
    page = attacker.get("/login")
    data = {"email": "member@example.com", "password": PASSWORD, "csrf_token": token_on(page.text)}
    assert attacker.post("/login", data=data).status_code == 429
    assert location(sign_in(front_door(site, client_ip="203.0.113.50"), site)) == "/"


def test_the_access_log_names_the_visitor(site, caplog):
    with caplog.at_level(logging.INFO, logger="dip_scanner.web.access"):
        front_door(site).get("/login")
        front_door(site, secret=None, client_ip="192.0.2.99").get("/login")
    lines = [record.getMessage() for record in caplog.records if record.name == "dip_scanner.web.access"]
    assert any(line.startswith(f"{VISITOR} GET /login 200") for line in lines), lines
    assert any(line.startswith("testclient GET /login 403") for line in lines), lines  # an unproven name is ignored


# --- redirects -----------------------------------------------------------------------------------------------------


def test_redirects_stay_relative_through_the_front_door(site):
    anonymous = front_door(site)
    assert location(anonymous.get("/")) == "/login"
    assert location(anonymous.get("/ideas/5?x=1")) == "/login?next=%2Fideas%2F5%3Fx%3D1"
    client = front_door(site)
    assert location(sign_in(client, site, role="admin")) == "/"
    assert location(client.get("/login?next=/track")) == "/track"
    assert location(client.get("/login?next=https://evil.example/")) == "/"
    assert location(client.get("/settings/")) == "/settings"  # Starlette's trailing-slash redirect
    assert location(client.get("/tickers?symbol=amd")) == "/tickers/AMD"
    token = client.get("/api/v1/me").json()["csrf"]
    assert location(client.post("/tickers/AMD/watchlist", data={"csrf_token": token, "action": "add"})) == (
        "/tickers/AMD"
    )
    job = location(client.post("/analyze", data={"csrf_token": token, "ticker": "AMD", "next": "/"}))
    assert re.fullmatch(r"/jobs/\d+", job)
    assert re.fullmatch(r"/ideas/\d+", location(client.get(job)))
    assert location(client.post("/admin/scanner/pause", data={"csrf_token": token})) == "/admin"
    assert location(client.post("/logout", data={"csrf_token": token})) == "/login"


def test_relative_location():
    scope = {"headers": [(b"host", b"my-dips.fly.dev")], "server": ("10.0.0.5", 8080)}
    assert relative_location("https://my-dips.fly.dev/settings", scope) == "/settings"
    assert relative_location("http://my-dips.fly.dev/a?b=1#c", scope) == "/a?b=1#c"
    assert relative_location("http://MY-DIPS.fly.dev", scope) == "/"
    assert relative_location("http://10.0.0.5:8080/x", scope) == "/x"
    assert relative_location("https://my-dips.fly.dev//evil.example/x", scope) == "/evil.example/x"
    for other in ("https://dips.example.com/x", "/login?next=%2F", "https://evil.example/", "mailto:a@b.c"):
        assert relative_location(other, scope) == other


# --- trusted origins -----------------------------------------------------------------------------------------------


@pytest.fixture
def previews(tmp_path) -> Site:
    return proxied_site(
        tmp_path,
        trusted_origins=("https://my-dips-git-main-my-team.vercel.app", "https://my-dips-git-*-my-team.vercel.app"),
    )


@pytest.mark.parametrize(
    ("origin", "allowed"),
    [
        (VERCEL, True),
        (PREVIEW, True),
        ("https://my-dips-git-main-my-team.vercel.app", True),
        ("https://my-dips-git-new-chart-my-team.vercel.app:443", True),
        ("https://evil-git-x-my-team.vercel.app", False),
        ("https://my-dips-git-x-other-team.vercel.app", False),
        ("https://my-dips-git-x-my-team.vercel.app.evil.example", False),
        ("http://my-dips-git-x-my-team.vercel.app", False),
        ("https://my-dips-git-x.y-my-team.vercel.app", False),
        ("https://vercel.app", False),
        ("null", False),
    ],
)
def test_posts_from_trusted_origins(previews, origin, allowed):
    client = front_door(previews)
    sign_in(client, previews)  # on the main address
    token = client.get("/api/v1/me").json()["csrf"]
    client.headers["origin"] = origin
    api = client.post("/api/v1/ideas/999/reanalyse", headers={"x-csrf-token": token})
    if allowed:
        error_of(api, 404, "not_found")  # past the Origin check: there is no idea 999
    else:
        error_of(api, 403, "origin")
    response = client.post("/logout", data={"csrf_token": token})
    assert response.status_code == (303 if allowed else 403), response.text[:200]


def test_a_referer_from_a_trusted_preview_passes(previews):
    client = front_door(previews)
    client.headers.pop("origin")
    sign_in(client, previews)
    token = client.get("/api/v1/me").json()["csrf"]
    previews.add(created=NOW - timedelta(hours=1))
    ok = client.post("/api/v1/ideas/1/reanalyse", headers={"x-csrf-token": token, "referer": f"{PREVIEW}/ideas/1"})
    assert ok.status_code == 202 and problems(ok.json(), "ReanalyseAccepted") == []
    refused = client.post(
        "/api/v1/ideas/1/reanalyse", headers={"x-csrf-token": token, "referer": "https://evil.example/ideas/1"}
    )
    error_of(refused, 403, "origin")


def test_without_trusted_origins_a_preview_is_another_site(site):
    client = front_door(site)
    sign_in(client, site)
    token = client.get("/api/v1/me").json()["csrf"]
    client.headers["origin"] = PREVIEW
    assert client.post("/logout", data={"csrf_token": token}).status_code == 403
