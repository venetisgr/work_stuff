from __future__ import annotations

import logging
import smtplib
import socket
import threading
import time
from datetime import timedelta

import pytest
import requests
from conftest import NOW, FakeResponse, FakeSession, make_analysis, make_debate, make_opportunity

from dip_scanner import notify as notify_module
from dip_scanner.config import ConfigError, NotifySettings
from dip_scanner.notify import (
    ALERT_FOOTER,
    DISCORD_LIMIT,
    MAX_RETRY_WAIT,
    SLACK_LIMIT,
    TELEGRAM_LIMIT,
    TRUNCATED_NOTE,
    EmailNotifier,
    NotifyError,
    TelegramNotifier,
    WebhookNotifier,
    build_notifiers,
    chunk_text,
    discord_text,
    email_missing,
    plain_text,
    short_alert,
    slack_text,
)

SLACK_SECRET = "abcdefghijklmnopqrstuvwx"
SLACK_URL = f"https://hooks.slack.com/services/T000/B000/{SLACK_SECRET}"
DISCORD_URL = "https://discord.com/api/webhooks/123/SECRETTOKEN"
GENERIC_URL = "https://example.com/hooks/dip?key=SECRETKEY"
TOKEN = "123456:SECRET-bot-token"
TELEGRAM_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

MARKDOWN = """# Dip opportunities

_1 opportunity · generated 2026-09-25 15:00 UTC_

| # | Ticker | Score |
|---:|---|---:|
| 1 | **AMD** | 72.4 |

## AMD — Advanced Micro Devices · score 72.4

| Key figures | |
|---|---|
| Price | $142.50 (-5.0% 1 day) |

**Thesis:** Margins & moat intact <for now>.

- [Title with \\[brackets\\]](<https://example.com/a?b=1|2>) · MarketWatch

---

_Not investment advice._
"""


# --- email ---------------------------------------------------------------------------------------------------------


class FakeSMTP:
    def __init__(self, host, port, implicit_tls, timeout, *, fail_login=False, refuse=None):
        self.args = (host, port, implicit_tls, timeout)
        self.calls: list[tuple] = []
        self.message = None
        self._fail_login = fail_login
        self._refuse = refuse or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.calls.append(("quit",))
        return False

    def starttls(self, **kwargs):
        self.calls.append(("starttls",))

    def login(self, user, password):
        self.calls.append(("login", user, password))
        if self._fail_login:
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")

    def send_message(self, message, from_addr=None, to_addrs=None):
        self.calls.append(("send", from_addr, list(to_addrs)))
        self.message = message
        return self._refuse


def smtp_factory(created: list, **options):
    def factory(host, port, implicit_tls, timeout):
        smtp = FakeSMTP(host, port, implicit_tls, timeout, **options)
        created.append(smtp)
        return smtp

    return factory


def email_settings(**overrides) -> NotifySettings:
    values = {
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "smtp_user": "bot@example.com",
        "smtp_password": "app-password",
        "smtp_from": "Dip Scanner <bot@example.com>",
        "email_to": ["me@example.com", "partner@example.com"],
    }
    values.update(overrides)
    return NotifySettings(**values)


def test_email_uses_starttls_on_587_and_sends_text_and_html():
    created: list[FakeSMTP] = []
    notifier = EmailNotifier(email_settings(), smtp_factory=smtp_factory(created))
    notifier.send("3 dip\nopportunities", "# Report\n\ntext part", "<p>html part</p>")

    (smtp,) = created
    assert smtp.args[:3] == ("smtp.example.com", 587, False)
    assert smtp.calls == [
        ("starttls",),
        ("login", "bot@example.com", "app-password"),
        ("send", "bot@example.com", ["me@example.com", "partner@example.com"]),
        ("quit",),
    ]
    message = smtp.message
    assert message["Subject"] == "3 dip opportunities"  # no header injection through a newline
    assert message["From"] == "Dip Scanner <bot@example.com>"
    assert message["To"] == "me@example.com, partner@example.com"
    assert message["Message-ID"].endswith("@example.com>")
    assert message.get_content_type() == "multipart/alternative"
    text, html = message.get_payload()
    assert text.get_content_type() == "text/plain" and "text part" in text.get_content()
    assert html.get_content_type() == "text/html" and "<p>html part</p>" in html.get_content()


def test_email_uses_implicit_tls_on_465():
    created: list[FakeSMTP] = []
    EmailNotifier(email_settings(smtp_port=465), smtp_factory=smtp_factory(created)).send("s", "m", "<p>h</p>")
    (smtp,) = created
    assert smtp.args[2] is True
    assert [call[0] for call in smtp.calls] == ["login", "send", "quit"]


def test_email_without_user_does_not_log_in():
    created: list[FakeSMTP] = []
    settings = email_settings(smtp_user=None, smtp_password=None, smtp_port=25, smtp_starttls=False)
    EmailNotifier(settings, smtp_factory=smtp_factory(created)).send("s", "m", "<p>h</p>")
    assert [call[0] for call in created[0].calls] == ["send", "quit"]


def test_email_sender_defaults_to_the_smtp_user():
    notifier = EmailNotifier(email_settings(smtp_from=None), smtp_factory=smtp_factory([]))
    assert notifier.sender == "bot@example.com"


@pytest.mark.parametrize(
    ("step", "message"),
    [
        ("starttls", "doesn't offer STARTTLS"),
        # Regression: a server without AUTH (or an address needing SMTPUTF8) was also blamed on STARTTLS, with the
        # advice to turn TLS off.
        ("login", "unset SMTP_USER"),
        ("send_message", "SMTPUTF8"),
    ],
)
def test_email_names_the_step_the_server_does_not_support(monkeypatch, step, message):
    created: list[FakeSMTP] = []
    error = {
        "starttls": "STARTTLS extension not supported by server.",
        "login": "SMTP AUTH extension not supported by server.",
        "send_message": "SMTPUTF8 not supported by server",
    }[step]

    def unsupported(*args, **kwargs):
        raise smtplib.SMTPNotSupportedError(error)

    monkeypatch.setattr(FakeSMTP, step, unsupported)
    with pytest.raises(NotifyError, match=message) as caught:
        EmailNotifier(email_settings(), smtp_factory=smtp_factory(created)).send("s", "m", "<p>h</p>")
    if step != "starttls":
        assert "STARTTLS" not in str(caught.value)


def test_email_login_failure_is_a_notify_error():
    notifier = EmailNotifier(email_settings(), smtp_factory=smtp_factory([], fail_login=True))
    with pytest.raises(NotifyError, match="SMTP_PASSWORD") as info:
        notifier.send("s", "m", "<p>h</p>")
    assert "app-password" not in str(info.value)


def test_email_connection_failure_is_a_notify_error():
    def refuse(*args):
        raise ConnectionRefusedError(111, "Connection refused")

    with pytest.raises(NotifyError, match=r"smtp\.example\.com:587"):
        EmailNotifier(email_settings(), smtp_factory=refuse).send("s", "m", "<p>h</p>")


def test_email_partly_refused_recipients_is_logged(caplog):
    created: list[FakeSMTP] = []
    factory = smtp_factory(created, refuse={"partner@example.com": (550, b"no such user")})
    with caplog.at_level(logging.WARNING, logger="dip_scanner.notify"):
        EmailNotifier(email_settings(), smtp_factory=factory).send("s", "m", "<p>h</p>")
    assert "partner@example.com" in caplog.text


def test_email_needs_complete_settings():
    with pytest.raises(ConfigError, match="SMTP_HOST, EMAIL_TO and SMTP_FROM"):
        EmailNotifier(NotifySettings())
    with pytest.raises(ConfigError, match="SMTP_PASSWORD"):
        EmailNotifier(email_settings(smtp_password=None))


# --- webhooks ------------------------------------------------------------------------------------------------------


def test_slack_webhook_posts_mrkdwn_text():
    session = FakeSession({SLACK_URL: "ok"})
    WebhookNotifier(SLACK_URL, "slack", session=session).send("Dip <alert>", MARKDOWN, "<p>ignored</p>")

    (call,) = session.calls
    assert call["method"] == "POST" and call["url"] == SLACK_URL
    payload = call["json"]
    assert payload["unfurl_links"] is False
    text = payload["text"]
    assert text.startswith("*Dip &lt;alert&gt;*\n\n*Dip opportunities*\n")
    assert "*AMD — Advanced Micro Devices · score 72.4*" in text
    assert "*Thesis:* Margins &amp; moat intact &lt;for now&gt;." in text
    assert "- <https://example.com/a?b=1%7C2|Title with [brackets]> · MarketWatch" in text
    assert "Price: $142.50 (-5.0% 1 day)" in text
    assert "# · Ticker · Score" in text  # a table header row, not a heading
    assert "|---" not in text and "**" not in text


def test_discord_webhook_splits_into_2000_char_messages_without_mentions():
    lines = [f"- line {n:03d} @everyone " + "x" * 80 for n in range(60)]
    markdown = "\n".join(lines)
    session = FakeSession({DISCORD_URL: 204})
    WebhookNotifier(DISCORD_URL, "discord", session=session).send("Alert", markdown, "")

    assert len(session.calls) > 2
    contents = [call["json"]["content"] for call in session.calls]
    assert all(len(content) <= DISCORD_LIMIT for content in contents)
    assert all(call["json"]["allowed_mentions"] == {"parse": []} for call in session.calls)
    assert contents[0].startswith("**Alert**\n\n- line 000")
    assert "\n".join(contents).count("- line ") == 60


def test_generic_webhook_posts_subject_markdown_and_html():
    session = FakeSession({GENERIC_URL: {"received": True}})
    WebhookNotifier(GENERIC_URL, session=session).send("Subject", "# md", "<p>html</p>")
    assert session.calls[0]["json"] == {"subject": "Subject", "markdown": "# md", "html": "<p>html</p>"}


def test_webhook_error_status_is_a_notify_error_without_the_secret():
    body = f"internal error for token {SLACK_SECRET} " * 20
    session = FakeSession({SLACK_URL: FakeResponse(status_code=500, content=body)})
    with pytest.raises(NotifyError) as info:
        WebhookNotifier(SLACK_URL, "slack", session=session).send("s", "m", "h")
    message = str(info.value)
    assert message.startswith("The slack webhook at hooks.slack.com answered 500: internal error")
    assert SLACK_SECRET not in message
    assert len(message) < 300


def test_webhook_404_hints_at_the_url():
    session = FakeSession({GENERIC_URL: FakeResponse(status_code=404, json_data={"message": "Unknown Webhook"})})
    with pytest.raises(NotifyError, match=r"answered 404: Unknown Webhook\. Check WEBHOOK_URL"):
        WebhookNotifier(GENERIC_URL, session=session).send("s", "m", "h")


def test_webhook_retries_once_after_429():
    waits: list[float] = []
    session = FakeSession({SLACK_URL: [FakeResponse(status_code=429, headers={"Retry-After": "2"}), "ok"]})
    WebhookNotifier(SLACK_URL, "slack", session=session, sleep=waits.append).send("s", "m", "h")
    assert waits == [2.0]
    assert len(session.calls) == 2


def test_webhook_gives_up_after_a_second_429_and_caps_the_wait():
    waits: list[float] = []
    busy = FakeResponse(status_code=429, json_data={"message": "You are being rate limited.", "retry_after": 600})
    session = FakeSession({DISCORD_URL: busy})
    with pytest.raises(NotifyError, match="429"):
        WebhookNotifier(DISCORD_URL, "discord", session=session, sleep=waits.append).send("s", "m", "h")
    assert waits == [MAX_RETRY_WAIT]
    assert len(session.calls) == 2


def test_webhook_connection_error_hides_the_secret():
    error = requests.ConnectionError(
        "HTTPSConnectionPool(host='hooks.slack.com', port=443): Max retries exceeded with url: "
        f"/services/T000/B000/{SLACK_SECRET}"
    )
    session = FakeSession({SLACK_URL: error})
    with pytest.raises(NotifyError, match="Couldn't reach the slack webhook at hooks.slack.com") as info:
        WebhookNotifier(SLACK_URL, "slack", session=session).send("s", "m", "h")
    assert SLACK_SECRET not in str(info.value)


def test_webhook_rejects_bad_settings():
    with pytest.raises(ConfigError, match="WEBHOOK_FORMAT"):
        WebhookNotifier(SLACK_URL, "teams")
    with pytest.raises(ConfigError, match="http"):
        WebhookNotifier("ftp://example.com/hook")


# --- Telegram ------------------------------------------------------------------------------------------------------


def test_telegram_sends_plain_text_without_previews():
    session = FakeSession({TELEGRAM_URL: {"ok": True, "result": {"message_id": 1}}})
    TelegramNotifier(TOKEN, "-1001234", session=session).send("Dip alert", MARKDOWN, "<p>ignored</p>")

    (call,) = session.calls
    payload = call["json"]
    assert payload["chat_id"] == "-1001234"
    assert payload["disable_web_page_preview"] is True
    assert payload["link_preview_options"] == {"is_disabled": True}
    text = payload["text"]
    assert text.startswith("Dip alert\n\nDip opportunities\n\n1 opportunity · generated")
    assert "Thesis: Margins & moat intact <for now>." in text
    assert "- Title with [brackets] (https://example.com/a?b=1|2) · MarketWatch" in text
    assert "Price: $142.50 (-5.0% 1 day)" in text
    assert "**" not in text and "](" not in text and "\n---" not in text


def test_telegram_splits_long_reports():
    markdown = "\n".join(f"Line {n}: " + "y" * 150 for n in range(80))
    session = FakeSession({TELEGRAM_URL: {"ok": True}})
    TelegramNotifier(TOKEN, "42", session=session).send("Alert", markdown, "")
    texts = [call["json"]["text"] for call in session.calls]
    assert len(texts) == 4
    assert all(len(text) <= TELEGRAM_LIMIT for text in texts)


def test_telegram_error_names_the_setting_and_hides_the_token():
    reply = FakeResponse(status_code=400, json_data={"ok": False, "description": "Bad Request: chat not found"})
    session = FakeSession({TELEGRAM_URL: reply})
    with pytest.raises(NotifyError, match="chat not found") as info:
        TelegramNotifier(TOKEN, "42", session=session).send("s", "m", "h")
    assert "TELEGRAM_CHAT_ID" in str(info.value)
    assert TOKEN not in str(info.value)


def test_telegram_ok_false_is_an_error_even_with_status_200():
    session = FakeSession({TELEGRAM_URL: {"ok": False, "description": "something odd"}})
    with pytest.raises(NotifyError, match="something odd"):
        TelegramNotifier(TOKEN, "42", session=session).send("s", "m", "h")


def test_telegram_connection_error_hides_the_token():
    session = FakeSession({TELEGRAM_URL: requests.ConnectionError(f"Max retries exceeded with url: /bot{TOKEN}/x")})
    with pytest.raises(NotifyError, match="Couldn't reach Telegram") as info:
        TelegramNotifier(TOKEN, "42", session=session).send("s", "m", "h")
    assert TOKEN not in str(info.value)


# --- text helpers --------------------------------------------------------------------------------------------------


def test_chunk_text_keeps_lines_together():
    text = "\n".join(f"line {n} " + "z" * 20 for n in range(20))
    chunks = chunk_text(text, 100)
    assert len(chunks) > 1
    assert all(len(chunk) <= 100 for chunk in chunks)
    assert "\n".join(chunks) == text
    assert chunk_text("short", 100) == ["short"]
    assert chunk_text("\n\n", 100) == []


def test_chunk_text_splits_long_lines_at_spaces_or_anywhere():
    words = " ".join(f"word{n}" for n in range(100))
    chunks = chunk_text(words, 50, max_chunks=None)
    assert all(len(chunk) <= 50 for chunk in chunks)
    assert " ".join(chunks) == words
    assert chunk_text("x" * 120, 50) == ["x" * 50, "x" * 50, "x" * 20]


def test_chunk_text_never_exceeds_a_limit_smaller_than_the_note():
    chunks = chunk_text(" ".join(f"word{n}" for n in range(100)), 20, max_chunks=2)
    assert len(chunks) == 2
    assert all(len(chunk) <= 20 for chunk in chunks)


def test_chunk_text_truncates_after_max_chunks():
    text = "\n".join("w" * 60 for _ in range(30))
    chunks = chunk_text(text, 100, max_chunks=3)
    assert len(chunks) == 3
    assert chunks[-1].endswith(TRUNCATED_NOTE)
    assert all(len(chunk) <= 100 for chunk in chunks)
    assert len(chunk_text(text, 100, max_chunks=None)) == 30


def test_slack_limit_is_respected():
    markdown = "\n".join("paragraph " + "q" * 300 for _ in range(40))
    session = FakeSession({SLACK_URL: "ok"})
    WebhookNotifier(SLACK_URL, "slack", session=session).send("s", markdown, "")
    assert all(len(call["json"]["text"]) <= SLACK_LIMIT for call in session.calls)
    assert len(session.calls) > 1


def test_text_conversions_leave_plain_text_alone():
    alert = short_alert([make_opportunity()])
    assert plain_text(alert) == alert
    assert discord_text(alert) == alert
    assert slack_text(alert) == alert


# --- build_notifiers -----------------------------------------------------------------------------------------------


def test_build_notifiers_with_nothing_configured(caplog):
    with caplog.at_level(logging.WARNING, logger="dip_scanner.notify"):
        assert build_notifiers(NotifySettings()) == []
    assert caplog.text == ""


def test_build_notifiers_with_everything_configured():
    session = FakeSession({})
    settings = email_settings(
        webhook_url=DISCORD_URL,
        webhook_format="discord",
        telegram_bot_token=TOKEN,
        telegram_chat_id="42",
    )
    notifiers = build_notifiers(settings, session=session)
    assert [type(n) for n in notifiers] == [EmailNotifier, WebhookNotifier, TelegramNotifier]
    assert [n.name for n in notifiers] == ["email", "discord webhook", "telegram"]
    assert notifiers[1].format == "discord"
    assert notifiers[1]._session is session and notifiers[2]._session is session
    assert session.calls == []  # building touches nothing


@pytest.mark.parametrize(
    ("overrides", "missing"),
    [
        ({"smtp_from": None, "smtp_user": "not-an-address"}, "SMTP_FROM"),
        ({"smtp_password": None}, "SMTP_PASSWORD"),
        ({"email_to": []}, "EMAIL_TO"),
        ({"smtp_host": None}, "SMTP_HOST"),
    ],
)
def test_build_notifiers_skips_half_configured_email(caplog, overrides, missing):
    with caplog.at_level(logging.WARNING, logger="dip_scanner.notify"):
        assert build_notifiers(email_settings(**overrides)) == []
    assert f"Email notifications are off: set {missing}" in caplog.text


def test_build_notifiers_skips_half_configured_telegram_and_bad_webhook(caplog):
    with caplog.at_level(logging.WARNING, logger="dip_scanner.notify"):
        assert build_notifiers(NotifySettings(telegram_bot_token=TOKEN)) == []
        assert build_notifiers(NotifySettings(telegram_chat_id="42")) == []
        assert build_notifiers(NotifySettings(webhook_url="file:///etc/passwd")) == []
    assert "set TELEGRAM_CHAT_ID" in caplog.text
    assert "set TELEGRAM_BOT_TOKEN" in caplog.text
    assert "WEBHOOK_URL must start with https://" in caplog.text
    assert TOKEN not in caplog.text


# --- short_alert ---------------------------------------------------------------------------------------------------


def test_short_alert_one_line_per_opportunity_best_first():
    amd = make_opportunity()
    sap = make_opportunity(
        ticker="SAP.DE",
        company="SAP SE",
        currency="EUR",
        score=81.25,
        analysis=make_analysis(verdict="mixed", confidence="high", probability_up_6m=74),
    )
    text = short_alert([amd, sap])
    lines = text.splitlines()
    assert lines[0] == "2 new dip opportunities:"
    assert lines[1].startswith("- SAP.DE (SAP SE) · score 81.2 · 74% chance up in 6m · price €142.50")
    assert lines[1].endswith("Mixed, high confidence")
    assert lines[2] == (
        "- AMD (Advanced Micro Devices) · score 72.4 · 68% chance up in 6m · price $142.50 · entry $132.00 · "
        "target $168.00 (+17.9% from price, +27.3% from entry) · low $118.00 · Temporary fear, medium confidence"
    )
    # The numbers are the model's uncalibrated estimate, and the low is no floor: the alert says so itself.
    assert lines[3] == ALERT_FOOTER and "uncalibrated" in ALERT_FOOTER and "not a floor" in ALERT_FOOTER
    assert "Not investment advice" in ALERT_FOOTER
    assert len(lines) == 4


def test_short_alert_shows_amounts_in_the_account_currency():
    """A euro investor sees what the limit orders mean in euros; a euro stock needs no conversion."""
    amd = make_opportunity(account_currency="EUR", fx_rate=0.8783)
    sap = make_opportunity(ticker="SAP.DE", currency="EUR", score=60.0, account_currency="EUR", fx_rate=1.0)
    lines = short_alert([amd, sap]).splitlines()
    assert (
        "· price $142.50 ≈ €125.16 · entry $132.00 ≈ €115.94 · target $168.00 ≈ €147.55 (+17.9% from price"
        in (lines[1])
    )
    assert "low $118.00 ·" in lines[1]  # the low stays in dollars: the orders are what you place
    assert "≈" not in lines[2] and "price €142.50 · entry €132.00" in lines[2]


def test_short_alert_labels_alerts_that_could_not_be_sent_earlier():
    """Regression: a 20-hour-old retried alert was listed as "new", next to the newer analysis of the same stock."""
    now = NOW + timedelta(hours=20)
    retried = make_opportunity(ticker="NVDA", score=70.0)  # analysed at NOW
    fresh = make_opportunity(created=now)
    lines = short_alert([retried, fresh], now=now).splitlines()
    assert lines[0] == "1 new dip opportunity, 1 not sent earlier:"
    assert lines[1].startswith("- AMD") and "not sent earlier" not in lines[1]
    assert lines[2].endswith("· not sent earlier: analysed 20.0h ago (2026-09-25 15:00 UTC)")
    assert short_alert([retried], now=now).splitlines()[0] == "1 dip opportunity not sent earlier:"


def test_short_alert_empty_and_single():
    assert short_alert([]) == "No new dip opportunities."
    assert short_alert([make_opportunity(company="Multi\nline")]).splitlines()[0] == "1 new dip opportunity:"
    assert "(Multi line)" in short_alert([make_opportunity(company="Multi\nline")])


# --- options for a website user's own channels ---------------------------------------------------------------------


def test_a_user_webhook_is_checked_before_every_message_and_never_follows_redirects():
    session = FakeSession({"https://hooks.example.com/": FakeResponse(status_code=302, headers={"Location": "x"})})
    checked = []

    def check_url(url):
        checked.append(url)

    hook = WebhookNotifier(
        "https://hooks.example.com/abc",
        "generic",
        session=session,
        check_url=check_url,
        follow_redirects=False,
        show_replies=False,
    )
    with pytest.raises(NotifyError) as error:
        hook.send("Subject", "text", "<p>html</p>")
    assert checked == ["https://hooks.example.com/abc"]
    assert session.calls[0]["allow_redirects"] is False
    assert str(error.value) == (
        "The generic webhook at hooks.example.com answered 302 (a redirect, which isn't followed)."
    )


def test_a_user_webhook_that_now_resolves_privately_is_not_posted_to():
    from dip_scanner.netguard import check_public_url

    session = FakeSession({"https://rebind.example.com/": "ok"})
    hook = WebhookNotifier(
        "https://rebind.example.com/hook",
        "slack",
        session=session,
        check_url=lambda url: check_public_url(url, resolver=lambda host, port: ["169.254.169.254"]),
    )
    with pytest.raises(NotifyError, match="isn't allowed: The address must be on the public internet"):
        hook.send("Subject", "text", "html")
    assert session.calls == []


def test_a_user_webhook_error_leaves_the_reply_out_and_uses_the_hints_given():
    reply = FakeResponse(status_code=404, content="<html>internal admin page: secret stuff</html>")
    hook = WebhookNotifier(
        "https://hooks.example.com/abc",
        "discord",
        session=FakeSession({"https://hooks.example.com/": reply}),
        show_replies=False,
        hints={404: "Check the webhook address in your settings."},
    )
    with pytest.raises(NotifyError) as error:
        hook.send("Subject", "text", "html")
    assert str(error.value) == (
        "The discord webhook at hooks.example.com answered 404. Check the webhook address in your settings."
    )
    assert "secret" not in str(error.value)
    # The command line's own webhook keeps showing the reply and the .env hint.
    own = WebhookNotifier(
        "https://hooks.example.com/abc", "discord", session=FakeSession({"https://hooks.example.com/": reply})
    )
    with pytest.raises(NotifyError, match=r"answered 404: <html>internal admin page.*Check WEBHOOK_URL"):
        own.send("Subject", "text", "html")


def test_telegram_hints_can_speak_to_a_website_user():
    session = FakeSession(
        {TELEGRAM_URL: FakeResponse(status_code=400, json_data={"ok": False, "description": "chat not found"})}
    )
    telegram = TelegramNotifier(TOKEN, "42", session=session, hints={400: "Check the chat id in your settings."})
    with pytest.raises(NotifyError, match="chat not found. Check the chat id in your settings."):
        telegram.send("Subject", "text", "html")


def test_email_for_website_users_needs_no_email_to():
    server = NotifySettings(smtp_host="smtp.example.com", smtp_from="dips@example.com")
    assert email_missing(server) == ["EMAIL_TO"]
    assert email_missing(server, recipients=False) == []
    assert email_missing(NotifySettings(), recipients=False) == ["SMTP_HOST", "SMTP_FROM"]


def test_short_alert_adds_a_line_for_a_debate():
    """A debated analysis gets a second line in chat alerts: each model's final chance, then the outcome's."""
    debated = make_opportunity(debate=make_debate(), analysis=make_analysis(probability_up_6m=64))
    lines = short_alert([debated]).splitlines()
    assert lines[1].startswith("- AMD (Advanced Micro Devices) · score 72.4 · 64% chance up in 6m")
    assert lines[2] == "  Debate: GPT-5 66% · Claude Sonnet 5 58% → 64% (medium agreement)"
    assert lines[3] == ALERT_FOOTER
    assert "Debate" not in short_alert([make_opportunity()])


# --- a member's webhook can't hold the scanner ----------------------------------------------------------------------


class BlockingSession:
    """A session whose post() waits until release is set (a server that never finishes answering)."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.calls = 0

    def post(self, url, **kwargs):
        self.calls += 1
        self.release.wait(10)
        return FakeResponse(url, 200)


@pytest.fixture
def blocking():
    session = BlockingSession()
    yield session
    session.release.set()  # let the abandoned threads finish


def test_a_webhook_with_a_deadline_gives_up_on_a_server_that_never_answers(blocking):
    hook = WebhookNotifier("https://tarpit.example.com/hook", "slack", session=blocking, deadline=0.2)
    started = time.monotonic()
    with pytest.raises(NotifyError, match=r"^The slack webhook at tarpit.example.com didn't answer within 0.2 s"):
        hook.send("Subject", "text", "html")
    assert time.monotonic() - started < 1
    # The first send still hangs in the background: the next one doesn't start another.
    started = time.monotonic()
    with pytest.raises(NotifyError, match="is still busy with the previous message"):
        hook.send("Subject", "text", "html")
    assert time.monotonic() - started < 0.2 and blocking.calls == 1
    blocking.release.set()  # the server finally answers
    for _ in range(100):
        if not notify_module._IN_FLIGHT:
            break
        time.sleep(0.01)
    hook.send("Subject", "text", "html")
    assert blocking.calls == 2


def test_a_webhook_without_a_deadline_is_sent_in_the_callers_thread():
    threads = []

    class Recording(FakeSession):
        def post(self, url, **kwargs):
            threads.append(threading.current_thread())
            return super().post(url, **kwargs)

    hook = WebhookNotifier(SLACK_URL, "slack", session=Recording({SLACK_URL: "ok"}))
    hook.send("Subject", "text", "html")
    assert threads == [threading.current_thread()]
    with pytest.raises(NotifyError, match="answered 404"):  # errors come back as before, deadline or not
        WebhookNotifier(SLACK_URL, "slack", session=FakeSession({}), deadline=5).send("Subject", "text", "html")


def local_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False  # straight to 127.0.0.1, whatever proxy the environment names
    return session


def trickling_server(head: bytes, *, delay: float, body: bytes = b"", trickle_head: bool = False):
    """A local HTTP server that answers every request with head (the status line and headers) then body, one byte
    every delay seconds (the head too with trickle_head). Returns (url, stop, connections)."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    stop = threading.Event()
    connections = []

    def serve(conn):
        with conn:
            conn.settimeout(5)
            data = b""
            while b"\r\n\r\n" not in data:
                data += conn.recv(65536)
            try:
                if trickle_head:
                    for byte in head:
                        if stop.wait(delay):
                            return
                        conn.sendall(bytes([byte]))
                else:
                    conn.sendall(head)
                if not delay:
                    conn.sendall(body)
                for byte in body if delay else b"":
                    if stop.wait(delay):
                        return
                    conn.sendall(bytes([byte]))
            except OSError:
                return

    def accept():
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            connections.append(conn)
            threading.Thread(target=serve, args=(conn,), daemon=True).start()

    threading.Thread(target=accept, daemon=True).start()

    def close():
        stop.set()
        listener.close()

    return f"http://127.0.0.1:{listener.getsockname()[1]}/hook", close, connections


@pytest.mark.parametrize("trickle_head", [False, True], ids=["body", "headers"])
def test_a_trickling_reply_is_cut_off_at_the_deadline(trickle_head):
    """requests' timeout only limits each read: a byte every 0.3 s would pass a 20 s timeout for ever."""
    url, close, connections = trickling_server(
        b"HTTP/1.1 200 OK\r\nContent-Length: 50\r\nX-Slow: aaaaaaaaaaaaaaaaaaaa\r\n\r\n",
        body=b"x" * 50,
        delay=0.3,
        trickle_head=trickle_head,
    )
    try:
        hook = WebhookNotifier(url, "generic", session=local_session(), timeout=1, deadline=1)
        started = time.monotonic()
        with pytest.raises(NotifyError, match="didn't answer within 1 s"):
            hook.send("Subject", "text", "html")
        assert time.monotonic() - started < 2
        with pytest.raises(NotifyError, match="still busy"):
            hook.send("Subject", "text", "html")
        assert len(connections) == 1  # no second connection while the first is stuck
    finally:
        close()


def test_only_the_start_of_a_members_webhook_reply_is_read():
    """A huge reply is dropped unread instead of filling the machine's memory."""
    url, close, _ = trickling_server(
        b"HTTP/1.1 200 OK\r\nContent-Length: 5000000\r\n\r\n", body=b"x" * 5_000_000, delay=0
    )
    options = {"label": "the generic webhook at 127.0.0.1", "secrets": [], "hints": {}, "timeout": 5}
    try:
        reply = notify_module._post(local_session(), url, {}, sleep=time.sleep, max_reply_bytes=1000, **options)
        assert len(reply.content) == 1000
        full = notify_module._post(local_session(), url, {}, sleep=time.sleep, **options)
        assert len(full.content) == 5_000_000  # the .env webhook reads the whole reply, as before
    finally:
        close()


def test_a_members_webhook_error_names_no_connection_details(caplog):
    """The raw exception would tell a member which of the server's private ports are open (and what answers there)."""
    raw = requests.ConnectionError(
        "HTTPSConnectionPool(host='127.0.0.1', port=18517): Max retries exceeded with url: /hook (Caused by "
        "SSLError(SSLError(1, '[SSL: WRONG_VERSION_NUMBER] wrong version number')); NewConnectionError [Errno 111]"
    )
    session = FakeSession({"https://127.0.0.1:18517/": raw})
    hook = WebhookNotifier("https://127.0.0.1:18517/hook", "slack", session=session, show_replies=False)
    with caplog.at_level(logging.WARNING, logger="dip_scanner.notify"), pytest.raises(NotifyError) as error:
        hook.send("Subject", "text", "html")
    assert str(error.value) == "Couldn't reach the slack webhook at 127.0.0.1. Check the address, or try again later."
    for detail in ("SSLError", "Errno", "NewConnectionError", "WRONG_VERSION", "18517"):
        assert detail not in str(error.value)
    assert "WRONG_VERSION_NUMBER" in caplog.text  # the details are in the server's log
    # The owner's own .env webhook still says what went wrong.
    own = WebhookNotifier("https://127.0.0.1:18517/hook", "slack", session=session)
    with pytest.raises(NotifyError, match="WRONG_VERSION_NUMBER"):
        own.send("Subject", "text", "html")
