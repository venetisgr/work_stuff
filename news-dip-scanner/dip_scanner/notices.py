"""System notices: the scanner telling you, through the alert channels, that it stopped or can't do its job.

An unattended scanner (cron, Task Scheduler, a systemd service) would otherwise fail where nobody looks: a revoked key
or an empty prepaid balance makes every run exit with code 2, an unreachable model or a dead network only shows in a
log. Three kinds of notice go to every configured channel:

- STOPPED: `run` or `watch` stopped on a setup problem (LLMSetupError or ConfigError), sent by the command line;
- MODEL_UNAVAILABLE: the model couldn't be used for [alerts] notice_after_cycles cycles in a row;
- FEEDS_FAILING: every feed failed for that many cycles in a row.

Each kind goes out at most once every NOTICE_REPEAT (12 hours). The times are kept in the database, so cron runs (a
new process every 5 minutes) don't repeat a notice either; one that no channel took is tried again after
NOTICE_RETRY. Notices never contain secrets: every configured key, password, token and webhook URL is replaced by ***
(scrub), and so is anything that looks like an API key or a password in a URL.
"""

from __future__ import annotations

import html
import logging
import os
import re
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from .config import Settings
from .models import utc

if TYPE_CHECKING:
    from .notify import Notifier
    from .store import Store

log = logging.getLogger(__name__)

STOPPED = "stopped"
MODEL_UNAVAILABLE = "model_unavailable"
FEEDS_FAILING = "feeds_failing"
NOTICE_REPEAT = timedelta(hours=12)
NOTICE_RETRY = timedelta(hours=1)  # after an attempt no channel took
FOOTER = "A notice of this kind is sent at most once every 12 hours. Set [alerts] system_notices = false to stop them."
MAX_REASON = 300  # characters of an error message in a notice

# Things that look like secrets even when they aren't in the settings: API keys and passwords in URLs.
_KEY_LIKE = re.compile(r"\b(sk-(?:ant-|proj-)?)[A-Za-z0-9_\-]{8,}")
_URL_PASSWORD = re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+@")
_OTHER_SECRET_VARIABLES = ("AZURE_CLIENT_SECRET", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")


def secrets_of(settings: Settings) -> list[str]:
    """Every secret value the settings (and the environment the SDKs read) hold, to be scrubbed from notices."""
    llm, notify = settings.llm, settings.notify
    values = [
        llm.openai_api_key,
        llm.anthropic_api_key,
        llm.foundry_api_key,
        notify.smtp_password,
        notify.telegram_bot_token,
        *(os.environ.get(name) for name in _OTHER_SECRET_VARIABLES),
    ]
    if notify.webhook_url:  # the path and query of a webhook URL are its secret
        parts = urlsplit(notify.webhook_url.strip())
        values += [notify.webhook_url.strip(), parts.path, parts.query]
        values += [part for part in parts.path.split("/") if len(part) >= 12]
    return [value.strip() for value in values if value and len(value.strip()) >= 4]


def scrub(text: str, secrets: Iterable[str] = ()) -> str:
    """text with every secret, API-key-like string and password in a URL replaced by ***."""
    for secret in sorted(set(secrets), key=len, reverse=True):
        text = text.replace(secret, "***")
    text = _KEY_LIKE.sub(r"\1***", text)
    return _URL_PASSWORD.sub("***@", text)


def one_line(text: object, limit: int = MAX_REASON) -> str:
    """An error message on one line, cut to limit characters."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def send_notice(
    notifiers: Sequence[Notifier],
    store: Store,
    *,
    kind: str,
    subject: str,
    lines: Sequence[str],
    now: datetime,
    secrets: Iterable[str] = (),
) -> bool:
    """Send a system notice to every notifier, unless one of this kind was sent within NOTICE_REPEAT or tried within
    NOTICE_RETRY; returns whether any channel took it. Never raises: a failing channel is logged."""
    if not notifiers:
        return False
    now = utc(now)
    last_attempt, last_sent = store.notice_times(kind)
    if last_sent is not None and now - last_sent < NOTICE_REPEAT:
        log.debug("Not repeating the %s notice (sent %s).", kind, f"{last_sent:%Y-%m-%d %H:%M} UTC")
        return False
    if last_attempt is not None and now - last_attempt < NOTICE_RETRY:
        return False
    secrets = list(secrets)
    subject = one_line(scrub(subject, secrets), 200)
    body = [scrub(line, secrets) for line in lines]
    markdown = "\n\n".join([f"# {subject}", *body, f"_{FOOTER}_"]) + "\n"
    page = "".join(f"<p>{html.escape(line)}</p>" for line in body)
    rich = (
        f'<!DOCTYPE html><html><head><meta charset="utf-8"><title>{html.escape(subject)}</title></head>'
        f"<body><h1>{html.escape(subject)}</h1>{page}<p><em>{html.escape(FOOTER)}</em></p></body></html>"
    )
    sent = False
    for notifier in notifiers:
        name = getattr(notifier, "name", type(notifier).__name__)
        try:
            notifier.send(subject, markdown, rich)
        except Exception as exc:  # NotifyError, or a notifier bug: the other channels still get it
            log.warning("Couldn't send the %s notice by %s: %s", kind, name, scrub(str(exc), secrets))
        else:
            sent = True
    store.record_notice(kind, when=now, sent=sent)
    if sent:
        log.info("Sent a system notice: %s", subject)
    return sent


def stopped_lines(command: str, reason: str, now: datetime) -> list[str]:
    """The body of a STOPPED notice for `dip-scanner <command>`."""
    if command == "watch":
        effect = "`dip-scanner watch` has exited: no news is scanned and no alerts are sent until it is started again."
    else:
        effect = (
            f"Every `dip-scanner {command}` stops with this error until it is fixed: no news is scanned and no alerts "
            "are sent."
        )
    return [
        f"dip-scanner {command} stopped at {utc(now):%Y-%m-%d %H:%M} UTC: {reason}",
        effect,
        "Fix the setting the message names (see Troubleshooting in the README), then check with "
        "`dip-scanner run --no-notify`.",
    ]
