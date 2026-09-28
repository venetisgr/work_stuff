"""`dip-scanner serve`'s wiring: the scanner's watch loop in a thread started and stopped by the app's lifespan, its
controls (pause, resume, a cycle now, start again), /healthz, and what happens when the scanner can't run.

The scanner is a fake whose watch() waits for stop(): no network, no models, no sleeping.
"""

from __future__ import annotations

import socket
import threading
from datetime import UTC, datetime, timedelta

import pytest
import requests
from conftest import FakeSession, make_opportunity
from fastapi.testclient import TestClient

from dip_scanner import accounts as accounts_module
from dip_scanner.config import DATABASE_NAME, ConfigError, NotifySettings, ScannerConfig, Settings, WebSettings
from dip_scanner.fx import FxRates
from dip_scanner.llm import LLMSetupError
from dip_scanner.netguard import refused_by_guard
from dip_scanner.notices import stopped_lines
from dip_scanner.prices import YahooPrices
from dip_scanner.store import Store
from dip_scanner.web import server
from dip_scanner.web.control import ScannerControl
from dip_scanner.web.jobs import InlineExecutor

BASE = "https://dips.example.com"
NOW = datetime(2026, 9, 25, 15, 0, tzinfo=UTC)
HOOK = "https://hooks.example.com/services/T000/B000/very-secret-hook-token"
WAIT = 10  # seconds a test waits for the scanner thread at most (it normally takes milliseconds)


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


def public(host: str, port: int) -> list[str]:
    return ["34.120.1.2"]


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class FakeScanner:
    """Enough of a Scanner for serve: watch() waits until stop(), or raises fail."""

    def __init__(self, *, fail: Exception | None = None, session=None) -> None:
        self.fail = fail
        self.started = threading.Event()
        self.stopped = threading.Event()
        self.watch_calls: list[dict] = []
        self.requests = 0
        self.cycle_started = None
        self.next_cycle_at = NOW + timedelta(minutes=5)
        self.prices = YahooPrices(session or FakeSession({}), sleep=lambda _: None)
        self.fx = FxRates(self.prices)

    def watch(self, **kwargs) -> None:
        self.watch_calls.append(kwargs)
        self.started.set()
        if self.fail is not None:
            raise self.fail
        self.stopped.wait(WAIT)

    def stop(self) -> None:
        self.stopped.set()

    def request_cycle_now(self) -> None:
        self.requests += 1

    def analyze_ticker(self, ticker: str, now: datetime):
        return make_opportunity(ticker=ticker, created=now)


def settings_for(tmp_path, *, notify: NotifySettings | None = None) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        notify=notify or NotifySettings(),
        web=WebSettings(secret_key="k" * 40, base_url=BASE),
    )


def build(tmp_path, scanner=None, *, error: Exception | None = None, enabled=True, notify=None, http=None, clock=None):
    """make_server_app with a factory that returns scanner (or raises error); returns (app, the factory's calls)."""
    calls: list[dict] = []

    def factory(settings, config, feeds, *, store, session, clock, resolver, webhook_session):
        calls.append({"store": store, "session": session, "feeds": feeds, "webhook_session": webhook_session})
        if error is not None:
            raise error
        return scanner

    app = server.make_server_app(
        settings_for(tmp_path, notify=notify),
        ScannerConfig(),
        [],
        scanner_enabled=enabled,
        session=http if http is not None else FakeSession({}),
        scanner_factory=factory,
        clock=clock or Clock(),
        trust_fly_client_ip=False,
        job_executor=InlineExecutor(),
        resolver=public,
    )
    return app, calls


def test_serve_runs_the_scanner_loop_in_a_thread_and_stops_it_on_shutdown(tmp_path):
    fake = FakeScanner()
    app, calls = build(tmp_path, fake)
    control = app.state.ctx.control
    assert control.status().state == "stopped"  # not started before the lifespan
    with TestClient(app, base_url=BASE) as client:
        assert fake.started.wait(WAIT)
        assert fake.watch_calls == [{"interval_minutes": 5.0}]
        assert control.alive()
        assert client.get("/healthz").json() == {"status": "ok", "db": "ok", "scanner": "running", "last_cycle": None}
        assert control.run_now() and fake.requests == 1
        assert client.get("/login").status_code == 200
    assert fake.stopped.is_set() and not control.alive()  # shutdown waited for the loop
    assert calls[0]["store"].path == tmp_path / "data" / DATABASE_NAME


def test_pause_and_resume_are_kept_in_the_database(tmp_path):
    fake = FakeScanner()
    app, _ = build(tmp_path, fake)
    ctx = app.state.ctx
    with TestClient(app, base_url=BASE) as client:
        fake.started.wait(WAIT)
        ctx.control.pause()
        assert client.get("/healthz").json()["scanner"] == "paused"
        with Store(tmp_path / "data" / DATABASE_NAME) as store:
            assert store.scanner_paused()  # what the scanner's watch loop reads
        ctx.control.resume()
        assert client.get("/healthz").json()["scanner"] == "running"


def test_a_scanner_without_finished_cycles_for_three_intervals_is_stalled(tmp_path):
    fake = FakeScanner()
    clock = Clock()
    app, _ = build(tmp_path, fake, clock=clock)
    ctx = app.state.ctx
    with TestClient(app, base_url=BASE) as client:
        fake.started.wait(WAIT)
        clock.now = NOW + timedelta(minutes=14)
        assert client.get("/healthz").json()["scanner"] == "running"
        clock.now = NOW + timedelta(minutes=16)
        health = client.get("/healthz")
        assert health.status_code == 200 and health.json()["scanner"] == "stalled"
        assert "no cycle has finished for over 15 minutes" in ctx.control.status().reason
        finished = clock.now - timedelta(minutes=1)
        ctx.store.record_cycle(started=finished - timedelta(minutes=1), finished=finished, summary="Cycle ok")
        body = client.get("/healthz").json()
        assert body["scanner"] == "running" and body["last_cycle"] == "2026-09-25T15:15:00+00:00"


def test_a_setup_problem_stops_only_the_scanner_and_says_so(tmp_path):
    fake = FakeScanner(fail=LLMSetupError("OpenAI rejected the credentials (401). Check OPENAI_API_KEY."))
    http = FakeSession({HOOK: {"ok": True}})
    notify = NotifySettings(webhook_url=HOOK, webhook_format="generic")
    app, _ = build(tmp_path, fake, notify=notify, http=http)
    ctx = app.state.ctx
    with TestClient(app, base_url=BASE) as client:
        ctx.control.join(WAIT)  # watch() raised at once
        status = ctx.control.status()
        assert status.reason == (
            "The language model can't be used: OpenAI rejected the credentials (401). Check OPENAI_API_KEY."
        )
        assert status.can_restart and not status.can_run_now
        assert client.get("/healthz").json()["scanner"] == "stopped"
        assert client.get("/login").status_code == 200  # the website keeps serving
        [post] = [call["json"] for call in http.calls if call["url"] == HOOK]
        assert post["subject"].startswith("dip-scanner stopped: The language model can't be used: OpenAI rejected")
        assert "The website keeps running, but its scanner has stopped" in post["markdown"]
        assert "very-secret-hook-token" not in str(post)

        fake.fail = None  # fixed (e.g. credit added): an admin starts it again
        fake.started.clear()
        assert ctx.control.restart()
        assert fake.started.wait(WAIT)
        assert ctx.control.status().state == "running" and ctx.control.status().reason is None
    assert len(fake.watch_calls) == 2


def test_a_scanner_that_cannot_be_built_leaves_the_website_up(tmp_path):
    http = FakeSession({HOOK: {"ok": True}})
    notify = NotifySettings(webhook_url=HOOK, webhook_format="generic")
    error = ConfigError("Set OPENAI_API_KEY in .env to use LLM_PROVIDER=openai.")
    app, _ = build(tmp_path, error=error, notify=notify, http=http)
    ctx = app.state.ctx
    with TestClient(app, base_url=BASE) as client:
        status = ctx.control.status()
        assert status.state == "stopped"
        assert status.reason == "Configuration problem: Set OPENAI_API_KEY in .env to use LLM_PROVIDER=openai."
        assert not status.can_restart
        assert client.get("/healthz").json()["scanner"] == "stopped"
        [post] = [call["json"] for call in http.calls if call["url"] == HOOK]
        assert post["subject"] == (
            "dip-scanner stopped: Configuration problem: Set OPENAI_API_KEY in .env to use LLM_PROVIDER=openai."
        )
        assert not ctx.jobs.available
        assert ctx.jobs.unavailable.startswith("Manual analyses aren't available: Configuration problem")


def test_without_the_scanner_the_pages_are_served_and_analyse_now_still_works(tmp_path):
    fake = FakeScanner()
    app, _ = build(tmp_path, fake, enabled=False)
    ctx = app.state.ctx
    with TestClient(app, base_url=BASE, follow_redirects=False) as client:
        assert client.get("/healthz").json()["scanner"] == "disabled"
        assert not fake.started.is_set() and fake.watch_calls == []
        assert ctx.control.status().label.startswith("Off: The scanner doesn't run in this process")
        user = ctx.accounts.create_user("admin@example.com", role="admin", password="a long enough password")
        job = ctx.jobs.submit(user, "amd")
        assert ctx.accounts.get_job(job.id).status == "done"
        assert ctx.prices is fake.prices  # the scanner's caches are shared


def test_the_stop_notice_goes_to_admins_channels_too(tmp_path):
    http = FakeSession({HOOK: {"ok": True}, "https://hooks.example.com/admin": {"ok": True}})
    settings = settings_for(tmp_path, notify=NotifySettings(webhook_url=HOOK, webhook_format="generic"))
    config = ScannerConfig()
    store = Store(settings.data_dir / DATABASE_NAME)
    accounts = accounts_module.Accounts(store, defaults=accounts_module.UserSettings.defaults(config, settings))
    admin = accounts.create_user("admin@example.com", role="admin", password="a long enough password")
    member = accounts.create_user("member@example.com", password="a long enough password")
    for user, url in ((admin, "https://hooks.example.com/admin"), (member, "https://hooks.example.com/member")):
        chosen = accounts_module.validate_settings(
            {"webhook_url": url, "webhook_format": "generic"},
            current=user.settings,
            settings=settings,
            resolver=public,
        )
        accounts.update_settings(user.id, chosen)
    send = server.stop_notice_sender(settings, config, store, http, clock=lambda: NOW, resolver=public)
    send("The language model can't be used: no credit left.")
    urls = [call["url"] for call in http.calls]
    assert HOOK in urls and "https://hooks.example.com/admin" in urls
    assert "https://hooks.example.com/member" not in urls  # members don't get system notices
    send("again")  # at most once every 12 hours
    assert len(http.calls) == len(urls)
    store.close()


def test_the_stop_notice_can_be_switched_off(tmp_path):
    from dip_scanner.config import AlertConfig

    http = FakeSession({HOOK: {"ok": True}})
    settings = settings_for(tmp_path, notify=NotifySettings(webhook_url=HOOK, webhook_format="generic"))
    with Store(settings.data_dir / DATABASE_NAME) as store:
        config = ScannerConfig(alerts=AlertConfig(system_notices=False))
        server.stop_notice_sender(settings, config, store, http)("stopped")
    assert http.calls == []


def test_serve_runs_uvicorn_behind_the_proxy(tmp_path, monkeypatch):
    seen = {}

    def run(app, **kwargs):
        seen.update(kwargs, app=app)

    fake = FakeScanner()
    monkeypatch.setattr(server, "build_scanner", lambda *args, **kwargs: fake)
    monkeypatch.setenv("FLY_APP_NAME", "my-dips")
    server.serve(
        settings_for(tmp_path), ScannerConfig(), [], host="0.0.0.0", port=8080, session=FakeSession({}), run=run
    )
    assert seen["host"] == "0.0.0.0" and seen["port"] == 8080
    assert seen["proxy_headers"] is True and seen["forwarded_allow_ips"] == "*"
    assert seen["access_log"] is False and seen["server_header"] is False and seen["log_config"] is None
    assert seen["app"].state.ctx.trust_fly_client_ip  # on Fly.io: Fly-Client-IP for rate limits
    assert seen["app"].state.ctx.prices is fake.prices


def test_serve_needs_a_secret_key(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", web=WebSettings(secret_key="short"))
    with pytest.raises(ConfigError, match="SECRET_KEY is too short"):
        server.make_server_app(settings, ScannerConfig(), [], scanner_factory=lambda *a, **k: FakeScanner())


def test_build_scanner_wires_the_websites_recipients(tmp_path, monkeypatch):
    from conftest import FakeChatModel

    models = (FakeChatModel(name="triage"), FakeChatModel(name="analysis"))
    monkeypatch.setattr(server, "build_models", lambda llm: models)
    settings = settings_for(tmp_path, notify=NotifySettings(webhook_url=HOOK, webhook_format="generic"))
    with Store(settings.data_dir / DATABASE_NAME) as store:
        scanner = server.build_scanner(
            settings, ScannerConfig(), [], store=store, session=FakeSession({}), resolver=public
        )
        assert scanner.triage_model is models[0] and scanner.notify
        assert [recipient.key for recipient in scanner._recipients_of(NOW)] == ["default"]
        assert scanner._watchlist_of(NOW) == () and scanner._currencies_of(NOW) == ()
        assert scanner.fundamentals is None  # no SEC_USER_AGENT


def test_control_without_a_scanner_or_disabled(tmp_path):
    with Store(tmp_path / "db.sqlite3") as store:
        off = ScannerControl(None, store, enabled=False)
        off.start()
        assert off.status().state == "disabled" and not off.run_now() and not off.restart()
        broken = ScannerControl(None, store, reason="The language model can't be used: sk-abcdefghijklmnop")
        assert broken.status().reason == "The language model can't be used: sk-***"  # secrets scrubbed
        told = []
        broken._on_stop = told.append
        broken.start()
        broken.start()
        assert told == ["The language model can't be used: sk-***"]  # once


def test_the_serve_stop_notice_text():
    lines = stopped_lines("serve", "Configuration problem: x", NOW)
    assert lines[0] == "dip-scanner serve stopped at 2026-09-25 15:00 UTC: Configuration problem: x"
    assert 'clicks "Start again" on the admin page or the website restarts' in lines[1]


def test_members_webhooks_go_through_a_session_pinned_to_public_addresses(tmp_path):
    seen = []

    def factory(settings, config, feeds, *, store, session, clock, resolver, webhook_session):
        seen.append((session, webhook_session))
        return FakeScanner()

    app = server.make_server_app(
        settings_for(tmp_path),
        ScannerConfig(),
        [],
        scanner_factory=factory,
        resolver=lambda host, port: ["10.0.0.8"],  # the name now points inside the network
        job_executor=InlineExecutor(),
    )
    [(shared, hooks)] = seen
    ctx = app.state.ctx
    assert ctx.webhook_http is hooks and hooks is not shared and ctx.http is shared
    with pytest.raises(requests.ConnectionError) as error:
        hooks.post("https://hooks.example.com/abc", json={}, timeout=1)
    assert refused_by_guard(error.value)
    ctx.store.close()
