"""The admin pages with FastAPI's TestClient: who may open them, the scanner's controls (pause, resume, a cycle now,
start again), the recent cycles, the feeds' health, the model's use and its estimated cost, the users (roles with the
last-admin guard, disabling, password links shown once) and the invites (created, emailed, shown once, revoked).

No network (fake HTTP session, fake DNS resolver, fake SMTP server) and no sleeping: the scanner is a fake whose
watch() waits for stop(), and the clock is a settable fake.
"""

from __future__ import annotations

import re
import socket
import threading
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from conftest import FakeSession
from fastapi.testclient import TestClient

from dip_scanner import accounts as accounts_module
from dip_scanner.config import DATABASE_NAME, NotifySettings, ScannerConfig, Settings, WebSettings
from dip_scanner.feeds import FeedState
from dip_scanner.llm import LLMSetupError
from dip_scanner.models import Feed
from dip_scanner.store import Store
from dip_scanner.web import admin
from dip_scanner.web.app import create_app
from dip_scanner.web.control import ScannerControl
from dip_scanner.web.jobs import InlineExecutor

BASE = "https://dips.example.com"
NOW = datetime(2026, 9, 25, 15, 0, tzinfo=UTC)
PASSWORD = "a long enough password"
WAIT = 10  # seconds a test waits for the scanner thread at most (it normally takes milliseconds)
SMTP = NotifySettings(smtp_host="smtp.example.com", smtp_from="Dip Scanner <dips@example.com>")


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


def public_resolver(host: str, port: int) -> list[str]:
    return ["34.120.1.2"]


class FakeScanner:
    """Enough of a Scanner for ScannerControl: watch() waits until stop(), or raises fail."""

    def __init__(self, *, fail: Exception | None = None) -> None:
        self.fail = fail
        self.started = threading.Event()
        self.stopped = threading.Event()
        self.requests = 0
        self.cycle_started = None
        self.next_cycle_at = NOW + timedelta(minutes=5)

    def watch(self, **kwargs) -> None:
        self.started.set()
        if self.fail is not None:
            raise self.fail
        self.stopped.wait(WAIT)

    def stop(self) -> None:
        self.stopped.set()

    def request_cycle_now(self) -> None:
        self.requests += 1


class FakeSMTP:
    """An SMTP server that keeps what it was sent (or refuses the login)."""

    def __init__(self, sent: list, *, fail: bool = False) -> None:
        self.sent = sent
        self.fail = fail

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def starttls(self, **kwargs) -> None:
        if self.fail:
            raise OSError("Connection refused by smtp.example.com")

    def login(self, user, password) -> None:
        pass

    def send_message(self, message, from_addr=None, to_addrs=None):
        self.sent.append((message, list(to_addrs or [])))
        return {}


def smtp_factory(sent: list, *, fail: bool = False):
    return lambda host, port, implicit_tls, timeout: FakeSMTP(sent, fail=fail)


FEEDS = [
    Feed(key="reuters", name="Reuters business", url="https://www.reuters.com/rss"),
    Feed(key="cnbc", name="CNBC markets", url="https://www.cnbc.com/rss"),
    Feed(key="new-one", name="Brand new feed", url="https://example.org/feed"),
    Feed(key="sec-8k", name="SEC 8-K filings", url="https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"),
    Feed(key="off", name="Switched off", url="https://example.net/rss", enabled=False),
]


class Site:
    """An app on a fresh database, its context and a client that doesn't follow redirects."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        web: dict | None = None,
        notify: NotifySettings | None = None,
        feeds: list[Feed] | None = None,
        control=None,
    ) -> None:
        values = {"secret_key": "s" * 40, "base_url": BASE, "cookie_secure": True}
        values.update(web or {})
        self.settings = Settings(
            data_dir=tmp_path / "data", notify=notify or NotifySettings(), web=WebSettings(**values)
        )
        self.clock = Clock()
        self.path = self.settings.data_dir / DATABASE_NAME
        self.app = create_app(
            settings=self.settings,
            config=ScannerConfig(),
            feeds=feeds or [],
            store_path=self.path,
            scanner_control=control(self) if control is not None else None,
            clock=self.clock,
            job_executor=InlineExecutor(),
            http_session=FakeSession({}),
            resolver=public_resolver,
        )
        self.ctx = self.app.state.ctx
        self.accounts = self.ctx.accounts
        self.store = self.ctx.store
        self.client = self.new_client()

    def new_client(self) -> TestClient:
        return TestClient(self.app, base_url=BASE, follow_redirects=False, raise_server_exceptions=False)

    def user(self, email: str = "member@example.com", *, role: str = "member", **kwargs):
        return self.accounts.create_user(email, role=role, password=PASSWORD, **kwargs)

    def signed_in(self, email: str = "member@example.com", *, role: str = "member") -> TestClient:
        if self.accounts.get_user_by_email(email) is None:
            self.user(email, role=role)
        client = self.new_client()
        page = client.get("/login")
        data = {"email": email, "password": PASSWORD, "csrf_token": token_on(page.text), "next": "/"}
        assert client.post("/login", data=data).status_code == 303
        return client

    def admin(self, email: str = "admin@example.com") -> TestClient:
        return self.signed_in(email, role="admin")


def token_on(page: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', page)
    assert match, "no csrf_token on the page"
    return match.group(1)


def post_form(client: TestClient, path: str, data: dict | None = None, *, page: str = "/settings", **kwargs):
    """POST a signed-in form with the session's csrf_token (taken from page)."""
    token = token_on(client.get(page).text)
    return client.post(path, data={"csrf_token": token, **(data or {})}, **kwargs)


def flashes(page: str) -> list[str]:
    return re.findall(r'<div class="flash flash-\w+"><p>(.*?)</p>', page)


def shown_link(page: str) -> str | None:
    match = re.search(r'id="new-link-url" type="text" value="([^"]+)"', page)
    return match.group(1) if match else None


@pytest.fixture
def site(tmp_path) -> Site:
    return Site(tmp_path)


# --- who may open the admin pages ----------------------------------------------------------------------------------


ADMIN_PAGES = ("/admin", "/admin/users", "/admin/invites")


def test_members_get_403_on_every_admin_page_and_action(site):
    admin_user = site.user("admin@example.com", role="admin")
    member = site.signed_in()
    target = site.accounts.get_user_by_email("member@example.com")
    for path in ADMIN_PAGES:
        response = member.get(path)
        assert response.status_code == 403, path
        assert "This page is for admins only." in response.text
    actions = [
        ("/admin/scanner/pause", {}),
        ("/admin/scanner/resume", {}),
        ("/admin/scanner/run", {}),
        ("/admin/scanner/restart", {}),
        (f"/admin/users/{target.id}/role", {"role": "admin"}),
        (f"/admin/users/{admin_user.id}/disable", {}),
        (f"/admin/users/{admin_user.id}/enable", {}),
        (f"/admin/users/{admin_user.id}/password-link", {}),
        ("/admin/invites", {"email": "friend@example.com", "role": "admin"}),
        ("/admin/invites/revoke", {"invite": "x"}),
    ]
    for path, data in actions:
        assert post_form(member, path, data).status_code == 403, path
    assert not site.store.scanner_paused()
    assert site.accounts.get_user(target.id).role == "member"
    assert not site.accounts.get_user(admin_user.id).disabled
    assert site.accounts.list_invites(pending_only=False) == []
    assert site.store.query("SELECT COUNT(*) FROM password_tokens")[0][0] == 0


def test_signed_out_visitors_are_sent_to_sign_in(site):
    anonymous = site.new_client()
    for path in ADMIN_PAGES:
        response = anonymous.get(path)
        assert response.status_code == 303
        assert response.headers["location"] == "/login?next=" + path.replace("/", "%2F")
    assert anonymous.post("/admin/scanner/pause", data={}).headers["location"] == "/login"
    assert not site.store.scanner_paused()


def test_admin_forms_need_the_session_token_and_this_site(site):
    client = site.admin()
    assert client.post("/admin/scanner/pause", data={}).status_code == 403
    assert client.post("/admin/scanner/pause", data={"csrf_token": "forged"}).status_code == 403
    token = token_on(client.get("/admin").text)
    cross = client.post("/admin/scanner/pause", data={"csrf_token": token}, headers={"Origin": "https://evil.example"})
    assert cross.status_code == 403 and "sent from another site" in cross.text
    assert not site.store.scanner_paused()
    assert client.post("/admin/scanner/pause", data={"csrf_token": token}).status_code == 303
    assert site.store.scanner_paused()


def test_the_admin_tabs(site):
    client = site.admin()
    for path, label in (("/admin", "Scanner"), ("/admin/users", "Users"), ("/admin/invites", "Invites")):
        page = client.get(path)
        assert page.status_code == 200
        assert f'aria-current="page">{label}</a>' in page.text
        assert 'href="/admin" aria-current="page">Admin</a>' in page.text  # the main menu too
        assert "/static/admin.css?v=" in page.text


# --- the scanner ---------------------------------------------------------------------------------------------------


def live_control(fake: FakeScanner):
    """A scanner_control factory for Site: ScannerControl over fake with its own connection to the database."""

    def build(site: Site) -> ScannerControl:
        site.scanner_store = Store(site.path)
        return ScannerControl(fake, site.scanner_store, interval_minutes=5, clock=site.clock)

    return build


def test_pause_resume_and_a_cycle_now(tmp_path):
    fake = FakeScanner()
    site = Site(tmp_path, control=live_control(fake))
    with TestClient(site.app, base_url=BASE):  # the lifespan starts the scanner's loop
        assert fake.started.wait(WAIT)
        client = site.admin()
        page = client.get("/admin").text
        assert "Scanner running" in page and "Run a cycle now" in page and ">Pause</button>" in page
        assert 'data-confirm="Pause the scanner?' in page

        paused = post_form(client, "/admin/scanner/pause", page="/admin")
        assert paused.status_code == 303 and paused.headers["location"] == "/admin"
        assert site.scanner_store.scanner_paused()  # what the scanner's loop reads
        page = client.get("/admin").text
        assert "The scanner is paused: no news is checked until you resume it." in page
        assert "Scanner paused" in page and ">Resume</button>" in page and ">Pause</button>" not in page

        assert post_form(client, "/admin/scanner/run", page="/admin").status_code == 303
        assert fake.requests == 1  # a cycle now, even while paused
        assert "A cycle starts now, even if the scanner is paused." in client.get("/admin").text

        post_form(client, "/admin/scanner/resume", page="/admin")
        assert not site.scanner_store.scanner_paused()
        page = client.get("/admin").text
        assert "The scanner is running again" in page and "Scanner running" in page
    site.scanner_store.close()


def test_a_cycle_now_is_refused_when_the_scanner_is_not_running(site):
    client = site.admin()
    page = client.get("/admin").text
    assert "Scanner off" in page and "Run a cycle now" not in page and ">Pause</button>" not in page
    assert "doesn&#39;t run in this process" in page
    response = post_form(client, "/admin/scanner/run", page="/admin")
    assert response.status_code == 303
    [message] = flashes(client.get("/admin").text)
    assert message.startswith("The scanner isn&#39;t running, so it can&#39;t start a cycle (The scanner doesn")


def test_cycles_now_are_rate_limited(tmp_path):
    fake = FakeScanner()
    site = Site(tmp_path, control=live_control(fake))
    with TestClient(site.app, base_url=BASE):
        assert fake.started.wait(WAIT)
        client = site.admin()
        token = token_on(client.get("/admin").text)
        for _ in range(admin.RUN_NOW_LIMIT):
            assert client.post("/admin/scanner/run", data={"csrf_token": token}).status_code == 303
        blocked = client.post("/admin/scanner/run", data={"csrf_token": token})
        assert blocked.status_code == 429 and "cycles in the last 15 minutes" in blocked.text
        assert fake.requests == admin.RUN_NOW_LIMIT
    site.scanner_store.close()


def test_start_again_after_a_setup_problem(tmp_path):
    fake = FakeScanner(fail=LLMSetupError("OpenAI says there is no credit left (insufficient_quota)."))
    site = Site(tmp_path, control=live_control(fake))
    with TestClient(site.app, base_url=BASE):
        site.ctx.control.join(WAIT)  # watch() raised at once
        client = site.admin()
        page = client.get("/admin").text
        assert "Scanner stopped" in page and "no credit left" in page
        assert ">Start again</button>" in page and "Run a cycle now" not in page

        fake.fail = None  # fixed: credit added
        fake.started.clear()
        response = post_form(client, "/admin/scanner/restart", page="/admin")
        assert response.status_code == 303
        assert fake.started.wait(WAIT)
        page = client.get("/admin").text
        assert "The scanner is starting again." in page and "Scanner running" in page

        again = post_form(client, "/admin/scanner/restart", page="/admin")  # already running
        assert again.status_code == 303
        assert "can&#39;t be started from here" in client.get("/admin").text
    site.scanner_store.close()


def test_recent_cycles_show_their_summary_stats_and_notes(site):
    started = NOW - timedelta(minutes=10)
    site.store.record_cycle(
        started=started,
        finished=started + timedelta(seconds=42),
        summary="Cycle 2026-09-25 14:50 UTC: 18/20 feeds ok, 12 new articles, 1 opportunity",
        notes=["No prices for XYZ (unknown symbol)", "<b>escaped</b> note"],
        stats={"feeds_ok": 18, "feeds_failed": 2, "new_articles": 12, "opportunities": 1, "model_calls": 3},
    )
    site.store.record_cycle(
        started=NOW - timedelta(minutes=5),
        finished=NOW - timedelta(minutes=4),
        summary="Cycle 2026-09-25 14:55 UTC failed: RuntimeError: boom",
        ok=False,
    )
    page = site.admin().get("/admin").text
    assert '<p class="cycle-summary">18/20 feeds ok, 12 new articles, 1 opportunity</p>' in page  # time beside it
    assert "took 42 s" in page and "took 1 min" in page
    assert (
        "<span>18/20 feeds</span>" in page and "<span>12 new articles</span>" in page and "<span>1 idea</span>" in page
    )
    assert '<details class="disclosure cycle-notes">' in page and "<summary>2 notes</summary>" in page
    assert "<li>No prices for XYZ (unknown symbol)</li>" in page
    assert "&lt;b&gt;escaped&lt;/b&gt; note" in page and "<b>escaped</b>" not in page
    assert '<span class="badge badge-bad">Failed</span>' in page
    assert '<p class="cycle-summary">RuntimeError: boom</p>' in page
    assert page.index("RuntimeError: boom") < page.index("18/20 feeds ok, 12 new")  # newest first
    assert "Last cycle that worked" in page  # the newest one failed


def test_cycle_text_drops_what_the_list_shows_beside_it():
    summary = "Cycle 2026-09-28 05:55 EEST: 20/20 feeds ok, 2 ideas; took 8 s; model today: 23 calls"
    assert admin.cycle_text(summary) == "20/20 feeds ok, 2 ideas; model today: 23 calls"
    assert admin.cycle_text("Cycle 2026-09-25 14:55 UTC failed: RuntimeError: boom") == "RuntimeError: boom"
    assert admin.cycle_text("Cycle 2026-09-25 14:55 +03: 1/1 feeds ok") == "1/1 feeds ok"
    assert admin.cycle_text("Something else: took over") == "Something else: took over"
    assert admin.cycle_text("Cycle 2026-09-25 14:55 UTC failed: ") == "Cycle 2026-09-25 14:55 UTC failed:"


def test_cycles_are_paged(site):
    for number in range(admin.CYCLES_PER_PAGE + 5):
        started = NOW - timedelta(minutes=5 * (number + 1))
        site.store.record_cycle(started=started, finished=started, summary=f"Cycle number {number:02d}.")
    client = site.admin()
    first = client.get("/admin").text
    assert "Cycle number 00." in first and "Cycle number 19." in first and "Cycle number 20." not in first
    assert "Page 1 of 2" in first and 'href="/admin?page=2"' in first
    second = client.get("/admin?page=2").text
    assert "Cycle number 20." in second and "Cycle number 24." in second and "Cycle number 00." not in second


def test_no_cycles_yet(site):
    page = site.admin().get("/admin").text
    assert "No cycles yet" in page and "No cycle yet" in page


def test_feed_health(tmp_path):
    site = Site(tmp_path, feeds=FEEDS)
    save = site.store.save_feed_state
    save("reuters", FeedState(), status=200, error=None, fetched=NOW - timedelta(minutes=4))
    save(
        "cnbc", FeedState(), status=403, error="HTTP 403 from www.cnbc.com: blocked", fetched=NOW - timedelta(minutes=4)
    )
    page = site.admin().get("/admin").text
    assert "4 enabled feeds, 1 with problems." in page and "1 more is switched off in feeds.toml." in page
    assert "Switched off" not in page
    assert '<span class="badge badge-bad">Failing</span> <span class="small muted num">HTTP 403</span>' in page
    assert "HTTP 403 from www.cnbc.com: blocked" in page
    assert '<span class="badge badge-ok">OK</span>' in page and "reuters.com" in page
    assert '<span class="badge badge-outline">Not fetched yet</span>' in page
    assert '<span class="badge badge-warn">Skipped</span>' in page  # no SEC_USER_AGENT
    assert "Needs SEC_USER_AGENT (your name and email) in .env or, on Fly.io, as a secret." in page
    order = [page.index(name) for name in ("CNBC markets", "Brand new feed", "SEC 8-K filings", "Reuters business")]
    assert order == sorted(order)  # problems first

    site.clock.advance(hours=2)  # nothing fetched since: the scanner isn't doing its job
    later = site.admin("admin2@example.com").get("/admin").text
    assert '<span class="badge badge-warn">Not fetched lately</span>' in later


def test_a_feed_that_needs_a_contact_user_agent_is_fetched_with_one(tmp_path):
    site = Site(tmp_path, feeds=[Feed(key="sec-8k", name="SEC 8-K filings", url="https://www.sec.gov/x")])
    site.settings = replace(site.settings, sec_user_agent="Jane Doe jane@example.com")
    site.ctx.settings = site.settings
    page = site.admin().get("/admin").text
    assert "Skipped" not in page and '<span class="badge badge-outline">Not fetched yet</span>' in page


# --- model use and its cost ----------------------------------------------------------------------------------------


def test_the_price_table():
    prices = admin.MODEL_PRICES
    assert prices.checked == date(2026, 9, 27)  # the README's "Costs" section: update both together
    assert prices.price_of("gpt-5-mini") == (0.25, 2.00)
    assert prices.price_of("GPT-5") == (1.25, 10.00)
    assert prices.price_of("gpt-5-2025-08-07") == (1.25, 10.00)  # a dated snapshot
    assert prices.price_of("openai/gpt-5-mini") == (0.25, 2.00)  # a gateway's name
    assert prices.price_of("claude-haiku-4-5-20251001") == (1.00, 5.00)
    assert prices.price_of("claude-sonnet-5") == (2.00, 10.00)
    assert prices.price_of("gpt-5-nano") is None  # not "gpt-5"
    assert prices.price_of("my-azure-deployment") is None
    assert prices.cost("gpt-5", 1_000_000, 100_000) == pytest.approx(2.25)
    assert prices.cost("unknown", 10, 10) is None
    assert [admin.usd(value) for value in (None, 0, 0.004, 0.27, 1234.6)] == [
        "–",
        "$0.00",
        "< $0.01",
        "$0.27",
        "$1,235",
    ]


def record_calls(store: Store, when: datetime, step: str, model: str, calls: int, tokens_in: int, tokens_out: int):
    for _ in range(calls):
        store.record_model_call(
            when=when,
            step=step,
            model=model,
            ticker="AMD" if step == "analysis" else None,
            input_tokens=tokens_in // calls,
            output_tokens=tokens_out // calls,
        )


def test_model_use_today_and_this_week_with_an_estimated_cost(site):
    today = NOW - timedelta(hours=2)
    yesterday = NOW - timedelta(days=1)
    record_calls(site.store, today, "triage", "gpt-5-mini", 4, 1_000_000, 100_000)  # $0.25 + $0.20
    record_calls(site.store, today, "analysis", "gpt-5", 2, 200_000, 30_000)  # $0.25 + $0.30
    record_calls(site.store, yesterday, "triage", "gpt-5-mini", 2, 2_000_000, 500_000)  # $0.50 + $1.00
    site.store.record_model_call(when=today, step="triage", model="my-deployment", input_tokens=None, output_tokens=5)
    site.store.record_model_call(when=NOW - timedelta(days=8), step="triage", model="gpt-5", input_tokens=10**7)
    page = site.admin().get("/admin").text

    assert "7 model calls today" in page  # the status strip
    stats = re.findall(r'<span class="stat-value">(.*?)</span>', page)
    assert stats == ["$1.00", "$2.50", "≈ $45.00"]  # today, 7 days, 30 days at yesterday's $1.50
    assert "at the average of 1 full day" in page
    assert '<td class="num" data-label="Est. cost">$0.45</td>' in page
    assert '<td class="num" data-label="Est. cost">$0.55</td>' in page
    assert 'Analysis <span class="mono small muted">gpt-5</span>' in page
    assert "1 call came back without token counts: its tokens aren" in page
    assert "No price is known for my-deployment: its calls aren" in page
    assert "Thu 24 Sep" in page and "Fri 25 Sep" in page and "(so far)" in page
    assert "Sat 19 Sep" in page and "Fri 18 Sep" not in page  # 7 UTC days
    assert "$1.00+" in page  # today had calls to a model without a price
    assert "checked on 27 Sep 2026" in page and '<span class="mono">gpt-5-mini</span> $0.25 in, $2 out' in page
    assert "An estimate at list prices, in US dollars." in page


def test_no_model_use_yet(site):
    page = site.admin().get("/admin").text
    assert "No model calls." in page and "A 30-day month" not in page
    assert re.findall(r'<span class="stat-value">(.*?)</span>', page) == ["$0.00", "$0.00"]


# --- users ---------------------------------------------------------------------------------------------------------


def test_the_users_list(tmp_path):
    site = Site(tmp_path, notify=NotifySettings(telegram_bot_token="123:abc"))
    client = site.admin()
    member = site.user("member@example.com", name="Eleni <b>")
    settings = accounts_module.validate_settings(
        {"webhook_url": "https://hooks.example.com/x", "webhook_format": "discord", "telegram_chat_id": "42"},
        current=member.settings,
        settings=site.settings,
        resolver=public_resolver,
    )
    site.accounts.update_settings(member.id, replace(settings, email_alerts=True))
    site.accounts.create_user("new@example.com")  # no password yet
    page = client.get("/admin/users").text
    assert "3 accounts, 1 active admin." in page
    assert "Eleni &lt;b&gt;" in page and "Eleni <b>" not in page
    assert '<span class="badge badge-outline">you</span>' in page
    assert (
        '<span class="badge badge-info">Discord</span>' in page
        and '<span class="badge badge-info">Telegram</span>' in page
    )
    assert "Email (not sent)" in page  # the server has no SMTP settings
    assert '<span class="badge badge-warn">No password yet</span>' in page and ">Setup link</button>" in page
    assert "never" in page and "Joined Fri 25 Sep 2026" in page
    assert "the only admin who can sign in" in page
    assert 'data-confirm="Make member@example.com an admin?' in page


def test_disable_and_enable_an_account(site):
    client = site.admin()
    member_client = site.signed_in()
    member = site.accounts.get_user_by_email("member@example.com")
    response = post_form(client, f"/admin/users/{member.id}/disable", page="/admin/users")
    assert response.status_code == 303 and response.headers["location"] == "/admin/users"
    assert site.accounts.get_user(member.id).disabled
    assert "member@example.com is disabled and was signed out everywhere." in client.get("/admin/users").text
    assert member_client.get("/settings").status_code == 303  # signed out at once

    post_form(client, f"/admin/users/{member.id}/enable", page="/admin/users")
    assert not site.accounts.get_user(member.id).disabled
    assert "member@example.com is enabled again and can sign in." in client.get("/admin/users").text


def test_an_admin_cannot_disable_themselves(site):
    client = site.admin()
    site.admin("second@example.com")  # another admin: the last-admin guard isn't what stops it
    me = site.accounts.get_user_by_email("admin@example.com")
    post_form(client, f"/admin/users/{me.id}/disable", page="/admin/users")
    assert not site.accounts.get_user(me.id).disabled
    assert "You can&#39;t disable your own account" in client.get("/admin/users").text


def test_promote_and_demote(site):
    client = site.admin()
    member = site.user()
    post_form(client, f"/admin/users/{member.id}/role", {"role": "admin"}, page="/admin/users")
    assert site.accounts.get_user(member.id).is_admin
    assert "member@example.com is now an admin" in client.get("/admin/users").text
    post_form(client, f"/admin/users/{member.id}/role", {"role": "member"}, page="/admin/users")
    assert not site.accounts.get_user(member.id).is_admin
    assert "member@example.com is now a member." in client.get("/admin/users").text
    post_form(client, f"/admin/users/{member.id}/role", {"role": "owner"}, page="/admin/users")
    assert "Choose admin or member." in client.get("/admin/users").text
    post_form(client, "/admin/users/999/role", {"role": "admin"}, page="/admin/users")
    assert "That account doesn&#39;t exist (any more)." in client.get("/admin/users").text
    assert client.get("/admin/users/abc/role").status_code in (404, 405)


def test_the_last_admin_can_be_neither_demoted_nor_disabled(site):
    client = site.admin()
    me = site.accounts.get_user_by_email("admin@example.com")
    page = client.get("/admin/users").text
    assert ">Make member</button>" not in page and ">Disable</button>" not in page  # not offered...
    response = post_form(client, f"/admin/users/{me.id}/role", {"role": "member"}, page="/admin/users")
    assert response.status_code == 303  # ...and refused when asked anyway
    assert site.accounts.get_user(me.id).is_admin
    assert "admin@example.com is the only admin, so they can&#39;t be made a member" in client.get("/admin/users").text

    # A disabled admin can't sign in, so it doesn't count: the guard still holds.
    other = site.user("other-admin@example.com", role="admin")
    site.accounts.set_disabled(other.id, True)
    post_form(client, f"/admin/users/{me.id}/role", {"role": "member"}, page="/admin/users")
    assert site.accounts.get_user(me.id).is_admin
    page = client.get("/admin/users").text
    assert f'action="/admin/users/{me.id}/role"' not in page and f'action="/admin/users/{other.id}/role"' in page


def test_an_admin_who_makes_themselves_a_member_leaves_the_admin_pages(site):
    client = site.admin()
    site.admin("second@example.com")
    me = site.accounts.get_user_by_email("admin@example.com")
    page = client.get("/admin/users").text
    assert 'data-confirm="Make yourself a member? You lose access to the admin pages."' in page
    response = post_form(client, f"/admin/users/{me.id}/role", {"role": "member"}, page="/admin/users")
    assert response.status_code == 303 and response.headers["location"] == "/"
    assert not site.accounts.get_user(me.id).is_admin
    assert client.get("/admin").status_code == 403


def test_a_reset_link_is_shown_once_and_works(site):
    client = site.admin()
    member_client = site.signed_in()
    member = site.accounts.get_user_by_email("member@example.com")
    response = post_form(client, f"/admin/users/{member.id}/password-link", page="/admin/users")
    assert response.status_code == 303 and response.headers["location"] == "/admin/users#new-link"

    page = client.get("/admin/users").text
    link = shown_link(page)
    assert link is not None and link.startswith(f"{BASE}/password/")
    assert "New password reset link" in page and 'data-copy="#new-link-url"' in page
    assert "A password reset link for member@example.com is ready. Copy it now: it is shown only once." in page
    assert "It is shown only this once" in page and "until <time" in page
    token = link.rsplit("/", 1)[1]
    [stored] = site.store.query("SELECT token_hash FROM password_tokens")
    assert stored[0] == accounts_module.token_hash(token)  # only the hash is kept

    assert shown_link(client.get("/admin/users").text) is None  # once
    other_admin = site.admin("second@example.com")
    assert shown_link(other_admin.get("/admin/users").text) is None  # and only to the admin who made it

    visitor = site.new_client()
    assert visitor.get(f"/password/{token}").status_code == 200  # it works
    assert member_client.get("/settings").status_code == 200  # the old password still works until it is used


def test_a_setup_link_for_an_account_without_a_password(site):
    client = site.admin()
    newcomer = site.accounts.create_user("new@example.com")
    post_form(client, f"/admin/users/{newcomer.id}/password-link", page="/admin/users")
    page = client.get("/admin/users").text
    assert "New setup link" in page and "set their password" in page
    token = shown_link(page).rsplit("/", 1)[1]
    assert "Set your password" in site.new_client().get(f"/password/{token}").text


def test_no_password_link_for_a_disabled_account(site):
    client = site.admin()
    member = site.user()
    site.accounts.set_disabled(member.id, True)
    post_form(client, f"/admin/users/{member.id}/password-link", page="/admin/users")
    page = client.get("/admin/users").text
    assert shown_link(page) is None and "enable the account first" in page
    assert site.store.query("SELECT COUNT(*) FROM password_tokens")[0][0] == 0


# --- invites -------------------------------------------------------------------------------------------------------


def test_an_invite_link_is_shown_once_and_creates_the_account(site):
    client = site.admin()
    page = client.get("/admin/invites").text
    assert "Create invite link" in page and 'name="send_email"' not in page  # no SMTP settings
    assert "No pending invites" in page
    response = post_form(
        client, "/admin/invites", {"email": "Friend@Example.com", "role": "admin"}, page="/admin/invites"
    )
    assert response.status_code == 303 and response.headers["location"] == "/admin/invites#new-link"

    page = client.get("/admin/invites").text
    link = shown_link(page)
    assert link is not None and link.startswith(f"{BASE}/invite/")
    assert "Invite for friend@example.com created. Copy the link now: it is shown only once." in page
    assert "For friend@example.com, as an admin." in page and 'data-copy="#new-link-url"' in page
    assert '<td class="primary">friend@example.com</td>' in page and "by admin@example.com" in page
    token = link.rsplit("/", 1)[1]
    assert token not in str(site.store.query("SELECT * FROM invites")[0][:])  # only the hash is kept
    assert shown_link(client.get("/admin/invites").text) is None  # once

    visitor = site.new_client()
    form = visitor.get(f"/invite/{token}")
    assert form.status_code == 200 and 'value="friend@example.com"' in form.text
    data = {"csrf_token": token_on(form.text), "name": "Friend", "password": PASSWORD, "confirm": PASSWORD}
    assert visitor.post(f"/invite/{token}", data=data).status_code == 303
    assert site.accounts.get_user_by_email("friend@example.com").is_admin

    page = client.get("/admin/invites").text
    assert "No pending invites" in page
    assert '<span class="badge badge-ok">Used</span>' in page and "by Friend" in page


def test_an_invite_for_anyone_with_the_link(site):
    client = site.admin()
    post_form(client, "/admin/invites", {"email": "", "role": "member"}, page="/admin/invites")
    page = client.get("/admin/invites").text
    assert "Invite for anyone with the link created." in page and "For anyone with the link." in page
    assert '<td class="primary">Anyone with the link</td>' in page
    [invite] = site.accounts.list_invites()
    assert invite.email is None and invite.role == "member" and invite.created_by is not None


def test_invite_mistakes_are_shown_on_the_form(site):
    client = site.admin()
    site.user("taken@example.com")
    bad = post_form(client, "/admin/invites", {"email": "not an address", "role": "member"}, page="/admin/invites")
    assert bad.status_code == 400 and "isn&#39;t an email address." in bad.text
    assert 'value="not an address"' in bad.text  # what was typed stays
    taken = post_form(client, "/admin/invites", {"email": "taken@example.com"}, page="/admin/invites")
    assert taken.status_code == 400 and "There is already an account for taken@example.com." in taken.text
    role = post_form(client, "/admin/invites", {"email": "", "role": "owner"}, page="/admin/invites")
    assert role.status_code == 400 and "Choose admin or member." in role.text
    assert site.accounts.list_invites(pending_only=False) == []


def test_revoking_an_invite_stops_its_link(site):
    client = site.admin()
    token = site.accounts.create_invite(created_by=None, email="friend@example.com")
    [invite] = site.accounts.list_invites()
    page = client.get("/admin/invites").text
    assert f'name="invite" value="{invite.token_hash}"' in page and token not in page
    assert 'data-confirm="Revoke the invite for friend@example.com? Its link stops working at once."' in page

    response = post_form(client, "/admin/invites/revoke", {"invite": invite.token_hash}, page="/admin/invites")
    assert response.status_code == 303 and response.headers["location"] == "/admin/invites"
    page = client.get("/admin/invites").text
    assert "Invite revoked: its link no longer works." in page
    assert '<span class="badge badge-outline">Revoked</span>' in page and "Revoked before it was used" in page
    assert site.accounts.get_invite(token) is None
    assert site.new_client().get(f"/invite/{token}").status_code == 404

    post_form(client, "/admin/invites/revoke", {"invite": invite.token_hash}, page="/admin/invites")
    assert "That invite can&#39;t be revoked" in client.get("/admin/invites").text


def test_expired_and_older_invites(site):
    client = site.admin()
    site.accounts.create_invite(created_by=None, email="late@example.com")
    site.clock.advance(days=8)
    site.accounts.create_invite(created_by=None, email="fresh@example.com")
    page = client.get("/admin/invites").text
    assert '<span class="badge badge-outline">Expired</span>' in page and "unused" in page
    assert "on the command line" in page  # `dip-scanner users invite` made them
    assert page.index("Pending invites") < page.index("fresh@example.com") < page.index("Earlier invites")
    assert page.index("Earlier invites") < page.index("late@example.com")


def test_invites_are_emailed_when_the_server_has_smtp(tmp_path, monkeypatch):
    sent: list = []
    monkeypatch.setattr(admin, "SMTP_FACTORY", smtp_factory(sent))
    site = Site(tmp_path, notify=SMTP)
    client = site.admin()
    site.accounts.set_name(site.accounts.get_user_by_email("admin@example.com").id, "Nikos <admin>")
    page = client.get("/admin/invites").text
    assert 'name="send_email" value="on" checked' in page
    post_form(
        client,
        "/admin/invites",
        {"email": "friend@example.com", "role": "member", "send_email": "on"},
        page="/admin/invites",
    )
    page = client.get("/admin/invites").text
    assert "Invite for friend@example.com created and emailed to friend@example.com." in page
    assert "It was emailed to friend@example.com" in page
    link = shown_link(page)
    [(message, to)] = sent
    assert to == ["friend@example.com"] and message["To"] == "friend@example.com"
    assert message["Subject"] == "Your invite to Dip scanner"
    text = message.get_body(("plain",)).get_content()
    body = message.get_body(("html",)).get_content()
    assert link in text and "Nikos <admin> invited you to Dip scanner" in text and "until 2026-10-02 15:00 UTC" in text
    assert f'<a href="{link}">' in body and "Nikos &lt;admin&gt;" in body and "<admin>" not in body

    post_form(client, "/admin/invites", {"email": "quiet@example.com", "role": "member"}, page="/admin/invites")
    assert len(sent) == 1  # the box unticked: not emailed
    post_form(client, "/admin/invites", {"email": "", "send_email": "on"}, page="/admin/invites")
    assert len(sent) == 1  # no address to email


def test_a_failed_invite_email_still_shows_the_link(tmp_path, monkeypatch):
    sent: list = []
    monkeypatch.setattr(admin, "SMTP_FACTORY", smtp_factory(sent, fail=True))
    site = Site(tmp_path, notify=SMTP)
    client = site.admin()
    post_form(client, "/admin/invites", {"email": "friend@example.com", "send_email": "on"}, page="/admin/invites")
    page = client.get("/admin/invites").text
    [error, ok] = flashes(page)
    assert error.startswith("The invite couldn&#39;t be emailed: Couldn&#39;t send the email through smtp.example.com")
    assert error.endswith("Copy the link below and send it yourself.")
    assert ok == "Invite for friend@example.com created. Copy the link now: it is shown only once."
    assert shown_link(page) is not None and "Send it to them yourself" in page
    assert sent == []


def test_invite_emails_are_rate_limited(tmp_path, monkeypatch):
    sent: list = []
    monkeypatch.setattr(admin, "SMTP_FACTORY", smtp_factory(sent))
    monkeypatch.setattr(admin, "INVITE_MAIL_LIMIT", 2)
    site = Site(tmp_path, notify=SMTP)
    client = site.admin()
    for number in range(3):
        data = {"email": f"friend{number}@example.com", "send_email": "on"}
        post_form(client, "/admin/invites", data, page="/admin/invites")
    assert len(sent) == 2
    assert "you sent 2 invite emails in the last hour" in client.get("/admin/invites").text
    assert len(site.accounts.list_invites()) == 3  # the invite itself was still created


def test_links_use_the_address_of_the_page_without_base_url(tmp_path):
    site = Site(tmp_path, web={"base_url": None})
    client = site.admin()
    post_form(client, "/admin/invites", {"email": ""}, page="/admin/invites")
    page = client.get("/admin/invites").text
    assert shown_link(page).startswith(f"{BASE}/invite/")
    assert "BASE_URL isn't set, so the link uses the address of this page." in page


def test_invite_email_text():
    inviter = accounts_module.User(
        id=1,
        email="owner@example.com",
        name="",
        role="admin",
        created=NOW,
        last_login=None,
        disabled=False,
        has_password=True,
        settings=accounts_module.UserSettings(),
        alerts_since=None,
    )
    subject, text, body = admin.invite_email(
        link="https://dips.example.com/invite/abc", inviter=inviter, role="admin", expires=NOW
    )
    assert subject == "Your invite to Dip scanner"
    assert "owner@example.com invited you as an admin to Dip scanner" in text
    assert "https://dips.example.com/invite/abc" in text and "Not investment advice." in text
    assert body.startswith("<!DOCTYPE html>") and 'href="https://dips.example.com/invite/abc"' in body
