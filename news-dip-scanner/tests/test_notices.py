"""Tests for system notices: rate limiting through the store, retries, and keeping secrets out."""

from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import NOW

from dip_scanner.config import LLMSettings, NotifySettings, Settings, WebSettings
from dip_scanner.notices import (
    NOTICE_REPEAT,
    NOTICE_RETRY,
    STOPPED,
    scrub,
    secrets_of,
    send_notice,
    stopped_lines,
)
from dip_scanner.notify import NotifyError
from dip_scanner.store import Store


class Channel:
    def __init__(self, name: str = "fake", error: Exception | None = None):
        self.name = name
        self.error = error
        self.sent: list[tuple[str, str, str]] = []

    def send(self, subject: str, markdown: str, html: str) -> None:
        self.sent.append((subject, markdown, html))
        if self.error is not None:
            raise self.error


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "scanner.sqlite3") as db:
        yield db


def notice(channels, store, *, when=NOW, subject="dip-scanner stopped: bad key", lines=("It stopped.",), **kwargs):
    return send_notice(channels, store, kind=STOPPED, subject=subject, lines=list(lines), now=when, **kwargs)


def test_a_notice_goes_to_every_channel_at_most_once_every_12_hours(store):
    email, chat = Channel("email"), Channel("slack webhook")
    assert notice([email, chat], store)
    [(subject, markdown, html)] = email.sent
    assert subject == "dip-scanner stopped: bad key" and chat.sent == email.sent
    assert markdown.startswith("# dip-scanner stopped: bad key\n\nIt stopped.")
    assert "at most once every 12 hours" in markdown
    assert html.startswith("<!DOCTYPE html>") and "<p>It stopped.</p>" in html

    assert not notice([email, chat], store, when=NOW + NOTICE_REPEAT - timedelta(minutes=1))
    assert notice([email, chat], store, when=NOW + NOTICE_REPEAT)
    assert len(email.sent) == 2

    # Another kind has its own clock.
    assert send_notice([email], store, kind="feeds_failing", subject="s", lines=[], now=NOW + timedelta(hours=13))


def test_a_notice_no_channel_took_is_tried_again_after_an_hour(store, caplog):
    down = Channel("email", NotifyError("smtp.example.com:587 refused the connection"))
    assert not notice([down], store)
    assert "Couldn't send the stopped notice by email: smtp.example.com:587 refused" in caplog.text
    assert not notice([down], store, when=NOW + NOTICE_RETRY - timedelta(minutes=1))
    assert len(down.sent) == 1

    working = Channel()
    assert notice([down, working], store, when=NOW + NOTICE_RETRY)  # one channel is enough
    assert not notice([working], store, when=NOW + NOTICE_RETRY + timedelta(hours=2))


def test_no_channels_means_nothing_is_recorded(store):
    assert not notice([], store)
    assert store.notice_times(STOPPED) == (None, None)


def test_secrets_are_scrubbed_from_subject_and_body(store, monkeypatch):
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "azure-client-secret-value")
    settings = Settings(
        llm=LLMSettings(openai_api_key="sk-live-1234567890", foundry_api_key="foundrykey42"),
        notify=NotifySettings(
            smtp_password="smtp-pass",
            webhook_url="https://hooks.slack.com/services/T000/B000/XXXXXXXXXXXXXXXXXXXX",
            telegram_bot_token="123456:ABCdefGHI",
        ),
    )
    secrets = secrets_of(settings)
    channel = Channel()
    notice(
        [channel],
        store,
        subject="dip-scanner stopped: key sk-live-1234567890 rejected",
        lines=[
            "Posting to https://hooks.slack.com/services/T000/B000/XXXXXXXXXXXXXXXXXXXX failed",
            "SMTP smtp-pass, Telegram 123456:ABCdefGHI, Foundry foundrykey42, Azure azure-client-secret-value",
            "Gateway https://user:pa55word@gateway.example.com/v1, another key sk-ant-api03-abcdefghijkl",
        ],
        secrets=secrets,
    )
    [(subject, markdown, html)] = channel.sent
    for text in (subject, markdown, html):
        for secret in ("1234567890", "XXXXXXXXXXXX", "smtp-pass", "ABCdefGHI", "foundrykey42", "azure-client", "pa55"):
            assert secret not in text
    assert subject == "dip-scanner stopped: key *** rejected"
    assert "https://***@gateway.example.com/v1" in markdown and "sk-ant-***" in markdown


def test_scrub_leaves_ordinary_text_alone():
    text = "Set OPENAI_API_KEY in .env to use LLM_PROVIDER=openai (see https://platform.openai.com/api-keys)."
    assert scrub(text, ["abc"]) == text  # too short to be a secret: never passed by secrets_of
    assert secrets_of(Settings()) == []


def test_stopped_lines_say_what_stopped_and_what_to_do():
    run = stopped_lines("run", "Configuration problem: Set OPENAI_API_KEY in .env.", NOW)
    assert (
        run[0] == "dip-scanner run stopped at 2026-09-25 15:00 UTC: Configuration problem: Set OPENAI_API_KEY in .env."
    )
    assert "Every `dip-scanner run` stops with this error until it is fixed" in run[1]
    assert "`dip-scanner run --no-notify`" in run[2]
    assert "has exited" in stopped_lines("watch", "x", NOW)[1]


def test_notices_use_the_display_time_zone_and_show_commands_as_code(store):
    from zoneinfo import ZoneInfo

    from dip_scanner.report import set_display_zone

    set_display_zone(ZoneInfo("Europe/Athens"))
    lines = stopped_lines("run", "Configuration problem: x", NOW)
    assert lines[0].startswith("dip-scanner run stopped at 2026-09-25 18:00 EEST:")
    channel = Channel()
    assert notice([channel], store, lines=lines)
    [(_, markdown, html)] = channel.sent
    assert "`dip-scanner run --no-notify`" in markdown
    assert ">dip-scanner run --no-notify</code>" in html and "`" not in html


def test_the_websites_secret_key_is_a_secret_too():
    key = "website-secret-key-" + "x" * 30
    settings = Settings(web=WebSettings(secret_key=key))
    assert key in secrets_of(settings)
    assert scrub(f"failed with {key} in it", secrets_of(settings)) == "failed with *** in it"
