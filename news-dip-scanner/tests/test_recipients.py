"""Recipients: the command line's default one, and website users with their own rules and channels."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import NOW, FakeSession, make_opportunity

from dip_scanner import accounts as accounts_module
from dip_scanner.accounts import Accounts, UserSettings
from dip_scanner.config import AccountConfig, AlertConfig, NotifySettings, ScannerConfig, Settings, UniverseConfig
from dip_scanner.notify import EmailNotifier, NotifyError, TelegramNotifier, WebhookNotifier
from dip_scanner.recipients import (
    TEST_SUBJECT,
    Recipient,
    active_currencies,
    active_watchlist,
    build_user_recipients,
    default_recipient,
    mark_manual_analysis,
    send_test,
    service_hooks,
    user_recipient,
)
from dip_scanner.store import DEFAULT_RECIPIENT, Store

PASSWORD = "correct horse battery"
SERVER = Settings(
    notify=NotifySettings(
        smtp_host="smtp.example.com",
        smtp_from="dips@example.com",
        email_to=["owner@example.com"],
        telegram_bot_token="123:SERVER-TOKEN",
    ),
    display_tz=ZoneInfo("Europe/Athens"),
)
CONFIG = ScannerConfig(
    alerts=AlertConfig(min_score=65, min_probability=60, repeat_hours=12, min_score_change=5),
    universe=UniverseConfig(watchlist=("ETE.AT",), preferred_listings={"ASML": "ASML.AS"}),
    account=AccountConfig(currency="EUR"),
)


def public_dns(host: str, port: int) -> list[str]:
    return ["34.120.1.2"]


@pytest.fixture(autouse=True)
def fast_scrypt(monkeypatch):
    monkeypatch.setattr(accounts_module, "SCRYPT_N", 2**4)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "scanner.sqlite3") as db:
        yield db


@pytest.fixture
def accounts(store):
    return Accounts(store, defaults=UserSettings.defaults(CONFIG, SERVER), clock=lambda: NOW)


def user(accounts, email, *, role="member", **chosen):
    created = accounts.create_user(email, role=role, password=PASSWORD)
    return accounts.update_settings(created.id, replace(created.settings, **chosen))


class FakeNotifier:
    def __init__(self, name="fake", error=None):
        self.name = name
        self.error = error
        self.sent = []

    def send(self, subject, markdown, html):
        self.sent.append((subject, markdown, html))
        if self.error is not None:
            raise self.error


def test_the_default_recipient_is_the_command_lines_setup():
    notifier = FakeNotifier()
    recipient = default_recipient(SERVER, CONFIG, [notifier])
    assert recipient == Recipient(
        key=DEFAULT_RECIPIENT,
        label="default",
        alerts=CONFIG.alerts,
        watchlist=("ETE.AT",),
        only_watchlist=False,
        notifiers=[notifier],
        currency="EUR",
        tz=ZoneInfo("Europe/Athens"),
    )
    assert recipient.gets_notices and recipient.since is None and recipient.thesis_changes


def test_a_user_recipient_has_their_rules_and_the_servers_channels(accounts):
    chosen = user(
        accounts,
        "jane@example.com",
        watchlist=("ASML", "AMD"),
        min_score=55.0,
        min_probability=70,
        verdicts=("mixed",),
        only_watchlist=True,
        thesis_changes=False,
        email_alerts=True,
        telegram_chat_id="-10042",
        webhook_url="https://hooks.slack.com/services/T0/B0/SECRET",
        webhook_format="slack",
        currency="GBP",
        timezone="America/New_York",
    )
    session = FakeSession()
    recipient = user_recipient(chosen, SERVER, CONFIG, session=session, resolver=public_dns)
    assert (recipient.key, recipient.label, recipient.admin, recipient.since) == (
        f"user:{chosen.id}",
        "jane@example.com",
        False,
        NOW,
    )
    # Their thresholds, the server's repeat rules; the watchlist read through preferred_listings.
    assert recipient.alerts == replace(CONFIG.alerts, min_score=55.0, min_probability=70, verdicts=("mixed",))
    assert recipient.watchlist == ("ASML.AS", "AMD") and recipient.only_watchlist and not recipient.thesis_changes
    assert (recipient.currency, recipient.tz) == ("GBP", ZoneInfo("America/New_York"))
    assert not recipient.gets_notices
    email, telegram, webhook = recipient.notifiers
    assert isinstance(email, EmailNotifier) and email.recipients == ["jane@example.com"]  # not EMAIL_TO
    assert email.host == "smtp.example.com" and email.sender == "dips@example.com"
    assert isinstance(telegram, TelegramNotifier) and telegram.chat_id == "-10042"
    assert isinstance(webhook, WebhookNotifier) and webhook.format == "slack"


def test_a_user_webhook_is_checked_again_when_sending_and_redirects_are_not_followed(accounts):
    chosen = user(accounts, "jane@example.com", webhook_url="https://hooks.example.com/abc", webhook_format="generic")
    answers = {"now": ["34.120.1.2"]}
    session = FakeSession({"https://hooks.example.com/": "ok"})
    [webhook] = user_recipient(
        chosen, Settings(), CONFIG, session=session, resolver=lambda host, port: answers["now"]
    ).notifiers
    webhook.send("Subject", "text", "html")
    assert session.calls[0]["allow_redirects"] is False
    answers["now"] = ["10.0.0.8"]  # the name now points inside the network (DNS rebinding)
    with pytest.raises(NotifyError, match="isn't allowed"):
        webhook.send("Subject", "text", "html")
    assert len(session.calls) == 1


def test_channels_the_server_can_not_serve_are_left_out(accounts):
    chosen = user(accounts, "jane@example.com", email_alerts=True, telegram_chat_id="42")
    assert user_recipient(chosen, Settings(), CONFIG).notifiers == []
    only_email = replace(SERVER, notify=replace(SERVER.notify, telegram_bot_token=None))
    [email] = user_recipient(chosen, only_email, CONFIG).notifiers
    assert isinstance(email, EmailNotifier)


def test_a_damaged_time_zone_falls_back_to_display_tz(accounts, store):
    chosen = user(accounts, "jane@example.com")
    broken = replace(chosen, settings=replace(chosen.settings, timezone="Nowhere/Land"))
    assert user_recipient(broken, SERVER, CONFIG).tz == ZoneInfo("Europe/Athens")
    assert user_recipient(chosen, Settings(), CONFIG).tz is not None


def test_only_active_users_with_a_working_channel_are_recipients(accounts, store):
    owner = user(accounts, "owner@example.com", role="admin", telegram_chat_id="1")
    user(accounts, "quiet@example.com")  # no channel
    disabled = user(accounts, "gone@example.com", telegram_chat_id="2")
    accounts.set_disabled(disabled.id, True)
    pending = accounts.create_user("pending@example.com")  # no password yet
    accounts.update_settings(pending.id, replace(pending.settings, telegram_chat_id="3"))
    user(accounts, "email@example.com", email_alerts=True)

    recipients = build_user_recipients(store, SERVER, CONFIG)
    assert [(r.label, r.admin) for r in recipients] == [("owner@example.com", True), ("email@example.com", False)]
    assert recipients[0].key == owner.recipient_key
    assert build_user_recipients(store, Settings(), CONFIG) == []  # no SMTP, no bot: nobody can be reached


def test_the_active_users_watchlists_and_currencies(accounts, store):
    user(accounts, "a@example.com", watchlist=("AMD", "ASML"), currency="GBP")
    user(accounts, "b@example.com", watchlist=("AMD", "SAP.DE"), currency="EUR")
    gone = user(accounts, "c@example.com", watchlist=("TSLA",), currency="CHF")
    accounts.set_disabled(gone.id, True)
    user(accounts, "d@example.com", currency="GBP")
    assert active_watchlist(store, CONFIG) == ("AMD", "ASML.AS", "SAP.DE")
    assert active_currencies(store) == ("GBP", "EUR")


def test_service_hooks_are_asked_again_at_every_cycle(accounts, store):
    default_notifier = FakeNotifier("email")
    hooks = service_hooks(store, SERVER, CONFIG, default_notifiers=[default_notifier])
    assert set(hooks) == {"recipients", "watchlist", "currencies"}
    [default] = hooks["recipients"](NOW)
    assert default.key == DEFAULT_RECIPIENT and default.notifiers == [default_notifier]

    user(accounts, "jane@example.com", telegram_chat_id="7", watchlist=("NVDA",), currency="CHF")
    assert [r.label for r in hooks["recipients"](NOW)] == ["default", "jane@example.com"]
    assert hooks["watchlist"](NOW) == ("NVDA",) and hooks["currencies"](NOW) == ("CHF",)
    assert [r.label for r in service_hooks(store, SERVER, CONFIG)["recipients"](NOW)] == ["jane@example.com"]


def test_send_test_reports_each_channel():
    good, bad = FakeNotifier("email"), FakeNotifier("telegram", NotifyError("Telegram answered 400: chat not found"))
    recipient = replace(default_recipient(SERVER, CONFIG, [good, bad]), tz=UTC)
    results = send_test(recipient, now=NOW + timedelta(hours=1))
    assert results == [("email", None), ("telegram", "Telegram answered 400: chat not found")]
    subject, markdown, html = good.sent[0]
    assert subject == TEST_SUBJECT and "sent at 2026-09-25 16:00 UTC" in markdown and "Not investment advice" in html
    assert send_test(replace(recipient, notifiers=[]), now=NOW) == []


def test_a_manual_analysis_is_never_an_alert_and_counts_as_seen_by_its_viewer(store):
    opp = store.add_opportunity(make_opportunity())
    mark_manual_analysis(store, [opp.id], viewer="user:3", when=NOW)
    assert store.unnotified() == [] and store.unnotified(recipient="user:1") == []
    assert store.last_alerted("AMD", recipient="user:3") == opp
    assert store.last_alerted("AMD", recipient="user:1") is None and store.last_alerted("AMD") is None
    other = store.add_opportunity(make_opportunity(created=NOW + timedelta(hours=1)))
    mark_manual_analysis(store, [other.id], viewer=None, when=NOW)
    assert store.deliveries(opportunity_id=other.id) == []
