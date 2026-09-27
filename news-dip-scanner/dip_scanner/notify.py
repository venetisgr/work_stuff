"""Notifications: email, chat webhooks (Slack, Discord, generic JSON) and Telegram.

Every notifier has send(subject, markdown, html). Email sends the Markdown as the plain-text part and the HTML as the
rich part. Chat services get the Markdown adapted to what they can show (Slack mrkdwn, Discord Markdown, plain text for
Telegram), split into messages that fit their size limits. A generic webhook gets {"subject", "markdown", "html"}.

build_notifiers makes one notifier per channel whose settings are complete, and logs a warning naming the missing
setting for a channel that is only half set up. Failures raise NotifyError; its message never contains the webhook
URL or the bot token, because both are secrets.
"""

from __future__ import annotations

import logging
import re
import smtplib
import ssl
import time
from collections.abc import Callable, Iterable
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from typing import Any, Protocol
from urllib.parse import urlsplit

import requests

from .config import WEBHOOK_FORMATS, ConfigError, NotifySettings
from .models import Opportunity, utc
from .report import format_money, format_pct, format_price, format_when, safe_url, verdict_label

log = logging.getLogger(__name__)

DISCORD_LIMIT = 2000  # characters per Discord message
SLACK_LIMIT = 3900  # Slack advises at most 4,000 characters per message
TELEGRAM_LIMIT = 4000  # Telegram allows 4,096
MAX_MESSAGES = 10  # chat messages per send; beyond that the text is cut with TRUNCATED_NOTE
TRUNCATED_NOTE = "… (cut short: the full report is in the reports folder)"
MAX_RETRY_WAIT = 10.0  # seconds; a 429 is retried once after the wait the service asks for (capped at this)
ALERT_FOOTER = (
    "Chances and scores are a language model's uncalibrated estimate (see `dip-scanner track`); the potential low "
    "is not a floor or a stop. Not investment advice; check before placing any order."
)
TELEGRAM_API = "https://api.telegram.org"
SMTP_IMPLICIT_TLS_PORT = 465

_WEBHOOK_HINTS = {
    400: "Check that WEBHOOK_FORMAT matches the service.",
    401: "Check WEBHOOK_URL.",
    403: "Check WEBHOOK_URL.",
    404: "Check WEBHOOK_URL (the webhook may have been deleted).",
}
_TELEGRAM_HINTS = {
    400: "Check TELEGRAM_CHAT_ID, and that the chat has started the bot (or added it to the group).",
    401: "Check TELEGRAM_BOT_TOKEN.",
    403: "The bot can't write to that chat: start the bot or add it to the group or channel.",
    404: "Check TELEGRAM_BOT_TOKEN.",
}

# Markdown as written by report.py: [label](<url>) or [label](url) links, \[ \] \| escapes, **bold**, # headings.
_MD_LINK = re.compile(r"\[((?:\\.|[^\]\\])*)\]\((?:<([^<>\s]+)>|([^()\s]+))\)")
_MD_UNESCAPE = re.compile(r"\\([\\\[\]|*_`#<>])")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")
_ITALIC_LINE = re.compile(r"^(\s*)_(\S.*?)_\s*$")
_RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_CELL_SPLIT = re.compile(r"(?<!\\)\|")
_TABLE_SEPARATOR = re.compile(r":?-{3,}:?")


class NotifyError(Exception):
    """A notification couldn't be sent."""


class Notifier(Protocol):
    """Anything that can deliver a report. Tests use a fake."""

    name: str

    def send(self, subject: str, markdown: str, html: str) -> None: ...


# --- email -----------------------------------------------------------------------------------------------------------

# (host, port, implicit_tls, timeout) -> an smtplib.SMTP-like object usable as a context manager.
SmtpFactory = Callable[[str, int, bool, float], Any]


def _smtp_connect(host: str, port: int, implicit_tls: bool, timeout: float) -> smtplib.SMTP:
    if implicit_tls:
        return smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context())
    return smtplib.SMTP(host, port, timeout=timeout)


def email_missing(settings: NotifySettings) -> list[str]:
    """The settings email still needs, by environment variable name (empty when email can be sent)."""
    missing = []
    if not settings.smtp_host:
        missing.append("SMTP_HOST")
    if not settings.email_to:
        missing.append("EMAIL_TO")
    if not (settings.smtp_from or (settings.smtp_user and "@" in settings.smtp_user)):
        missing.append("SMTP_FROM")
    if settings.smtp_user and not settings.smtp_password:
        missing.append("SMTP_PASSWORD")
    return missing


class EmailNotifier:
    """Sends the report as a multipart/alternative (text + HTML) email over SMTP.

    Port 465 uses implicit TLS (SMTP_SSL); any other port connects in plain text and upgrades with STARTTLS unless
    SMTP_STARTTLS is false. It logs in only when SMTP_USER is set. SMTP_FROM defaults to SMTP_USER when that is an
    email address.
    """

    name = "email"

    def __init__(
        self, settings: NotifySettings, *, smtp_factory: SmtpFactory | None = None, timeout: float = 30
    ) -> None:
        missing = email_missing(settings)
        if missing:
            raise ConfigError(f"Email notifications need {_and(missing)} in .env.")
        self.host: str = settings.smtp_host or ""
        self.port = settings.smtp_port
        self.sender: str = settings.smtp_from or settings.smtp_user or ""
        self.recipients = list(settings.email_to)
        self.user = settings.smtp_user
        self._password = settings.smtp_password or ""
        self.implicit_tls = self.port == SMTP_IMPLICIT_TLS_PORT
        self.starttls = settings.smtp_starttls and not self.implicit_tls
        self._connect = smtp_factory or _smtp_connect
        self._timeout = timeout
        if self.user and not (self.implicit_tls or self.starttls):
            log.warning("SMTP_STARTTLS is off, so the SMTP password goes to %s unencrypted.", self.host)

    def build_message(self, subject: str, markdown: str, html: str) -> EmailMessage:
        """The email: Markdown as the text/plain part, HTML as the text/html alternative."""
        address = parseaddr(self.sender)[1] or self.sender
        message = EmailMessage()
        message["Subject"] = _one_line(subject)
        message["From"] = self.sender
        message["To"] = ", ".join(self.recipients)
        message["Date"] = formatdate(usegmt=True)
        message["Message-ID"] = make_msgid(domain=address.rpartition("@")[2] or None)
        message.set_content(markdown)
        message.add_alternative(html, subtype="html")
        return message

    def send(self, subject: str, markdown: str, html: str) -> None:
        """Send one email to every EMAIL_TO address."""
        message = self.build_message(subject, markdown, html)
        where = f"{self.host}:{self.port}"
        try:
            with self._connect(self.host, self.port, self.implicit_tls, self._timeout) as smtp:
                if self.starttls:
                    try:
                        smtp.starttls(context=ssl.create_default_context())
                    except smtplib.SMTPNotSupportedError as exc:
                        raise NotifyError(
                            f"{where} doesn't offer STARTTLS. Use port 465 for implicit TLS, or set "
                            "SMTP_STARTTLS=false only for a server on a trusted network."
                        ) from exc
                if self.user:
                    smtp.login(self.user, self._password)
                refused = smtp.send_message(
                    message, from_addr=parseaddr(self.sender)[1] or self.sender, to_addrs=self.recipients
                )
        except smtplib.SMTPAuthenticationError as exc:
            raise NotifyError(
                f"{where} rejected the login for {self.user} ({exc.smtp_code}). Check SMTP_USER and SMTP_PASSWORD "
                "(many providers need an app password)."
            ) from exc
        except smtplib.SMTPNotSupportedError as exc:  # from login (no AUTH offered) or sending (needs SMTPUTF8)
            raise NotifyError(
                f"{where} doesn't support what this email needs: {exc} If it offers no login (AUTH), unset "
                "SMTP_USER or use the provider's submission port (587 or 465)."
            ) from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise NotifyError(f"Couldn't send the email through {where}: {exc}") from exc
        if refused:
            log.warning("%s refused some recipients: %s", where, ", ".join(sorted(refused)))
        log.info("Emailed %s to %d recipient(s).", _one_line(subject), len(self.recipients) - len(refused or {}))


# --- chat webhooks ---------------------------------------------------------------------------------------------------


class WebhookNotifier:
    """Posts the report to a Slack, Discord or generic JSON webhook.

    - slack: {"text": ...} in Slack's mrkdwn (links as <url|text>, *bold*), messages of at most SLACK_LIMIT chars;
    - discord: {"content": ...} in Discord Markdown, split into DISCORD_LIMIT-char messages, with mentions disabled so
      a headline containing @everyone can't ping anyone;
    - generic: one POST of {"subject", "markdown", "html"}.
    """

    name = "webhook"

    def __init__(
        self,
        url: str,
        fmt: str = "generic",
        *,
        session=None,
        timeout: float = 20,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not safe_url(url):
            raise ConfigError("WEBHOOK_URL must be an http(s) URL.")
        fmt = (fmt or "generic").strip().lower()
        if fmt not in WEBHOOK_FORMATS:
            raise ConfigError(f"WEBHOOK_FORMAT must be one of {', '.join(WEBHOOK_FORMATS)} (got {fmt!r}).")
        self.url = url.strip()
        self.format = fmt
        self.name = f"{fmt} webhook"
        self._session = session if session is not None else requests.Session()
        self._timeout = timeout
        self._sleep = sleep
        parts = urlsplit(self.url)
        self._label = f"the {fmt} webhook at {parts.hostname}"
        # The path and query of a webhook URL are its secret; long path segments are the token itself.
        self._secrets = [
            self.url,
            parts.path,
            parts.query,
            *(part for part in parts.path.split("/") if len(part) >= 12),
        ]

    def payloads(self, subject: str, markdown: str, html: str) -> list[dict]:
        """The JSON bodies send() posts, in order."""
        if self.format == "slack":
            text = f"*{_slack_escape(_one_line(subject))}*\n\n{slack_text(markdown)}"
            return [{"text": chunk, "unfurl_links": False} for chunk in chunk_text(text, SLACK_LIMIT)]
        if self.format == "discord":
            text = f"**{_one_line(subject)}**\n\n{discord_text(markdown)}"
            return [{"content": chunk, "allowed_mentions": {"parse": []}} for chunk in chunk_text(text, DISCORD_LIMIT)]
        return [{"subject": _one_line(subject), "markdown": markdown, "html": html}]

    def send(self, subject: str, markdown: str, html: str) -> None:
        """Post the report (split into several messages where the service has a size limit)."""
        payloads = self.payloads(subject, markdown, html)
        for payload in payloads:
            _post(
                self._session,
                self.url,
                payload,
                label=self._label,
                secrets=self._secrets,
                hints=_WEBHOOK_HINTS,
                timeout=self._timeout,
                sleep=self._sleep,
            )
        log.info("Posted %s to the %s (%d message(s)).", _one_line(subject), self.name, len(payloads))


class TelegramNotifier:
    """Sends the report through a Telegram bot (Bot API sendMessage) as plain text, in messages of at most
    TELEGRAM_LIMIT characters, without link previews."""

    name = "telegram"

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        session=None,
        timeout: float = 20,
        sleep: Callable[[float], None] = time.sleep,
        api_url: str = TELEGRAM_API,
    ) -> None:
        if not token or not chat_id:
            raise ConfigError("Telegram notifications need TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env.")
        self.chat_id = str(chat_id).strip()
        self._token = token.strip()
        self._url = f"{api_url.rstrip('/')}/bot{self._token}/sendMessage"
        self._session = session if session is not None else requests.Session()
        self._timeout = timeout
        self._sleep = sleep

    def messages(self, subject: str, markdown: str) -> list[str]:
        """The message texts send() delivers, in order."""
        return chunk_text(f"{_one_line(subject)}\n\n{plain_text(markdown)}", TELEGRAM_LIMIT)

    def send(self, subject: str, markdown: str, html: str) -> None:
        """Send the report as one or more messages of at most TELEGRAM_LIMIT characters."""
        texts = self.messages(subject, markdown)
        for text in texts:
            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "disable_web_page_preview": True,  # older Bot API name
                "link_preview_options": {"is_disabled": True},  # Bot API 7.0+
            }
            response = _post(
                self._session,
                self._url,
                payload,
                label="Telegram",
                secrets=[self._token],
                hints=_TELEGRAM_HINTS,
                timeout=self._timeout,
                sleep=self._sleep,
            )
            data = _json_or_none(response)
            if isinstance(data, dict) and data.get("ok") is False:
                description = _scrub(str(data.get("description") or "no reason given"), [self._token])
                raise NotifyError(f"Telegram refused the message: {description}")
        log.info("Sent %s to Telegram (%d message(s)).", _one_line(subject), len(texts))


def _post(
    session,
    url: str,
    payload: dict,
    *,
    label: str,
    secrets: Iterable[str],
    hints: dict[int, str],
    timeout: float,
    sleep: Callable[[float], None],
):
    """POST JSON; retry once after a 429; NotifyError (secrets scrubbed) on a connection error or non-2xx status."""
    retried = False
    while True:
        try:
            response = session.post(url, json=payload, timeout=timeout)
        except requests.RequestException as exc:
            raise NotifyError(f"Couldn't reach {label}: {_scrub(str(exc), secrets)}") from exc
        status = response.status_code
        if status == 429 and not retried:
            retried = True
            wait = _retry_after(response)
            log.info("%s is rate limiting; retrying in %.1f s.", label, wait)
            sleep(wait)
            continue
        if not 200 <= status < 300:
            message = f"{label[:1].upper()}{label[1:]} answered {status}: {_short_body(response, secrets)}"
            hint = hints.get(status)
            raise NotifyError(f"{message.rstrip('.')}. {hint}" if hint else message)
        return response


def _retry_after(response) -> float:
    """Seconds to wait from a 429: the Retry-After header, or retry_after in a Discord/Telegram JSON body."""
    candidates: list[Any] = [response.headers.get("Retry-After")]
    data = _json_or_none(response)
    if isinstance(data, dict):
        candidates.append(data.get("retry_after"))
        parameters = data.get("parameters")
        if isinstance(parameters, dict):
            candidates.append(parameters.get("retry_after"))
    for value in candidates:
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            continue
        return min(MAX_RETRY_WAIT, max(0.0, seconds))
    return 1.0


def _json_or_none(response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _short_body(response, secrets: Iterable[str], limit: int = 200) -> str:
    data = _json_or_none(response)
    if isinstance(data, dict) and isinstance(data.get("description"), str):
        text = data["description"]  # Telegram
    elif isinstance(data, dict) and isinstance(data.get("message"), str):
        text = data["message"]  # Discord
    else:
        text = response.text or ""
    text = _scrub(_one_line(text), secrets)
    if not text:
        return "(empty reply)"
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _scrub(text: str, secrets: Iterable[str]) -> str:
    for secret in sorted((s for s in secrets if s and s != "/"), key=len, reverse=True):
        text = text.replace(secret, "***")
    return text


# --- text for chat services ------------------------------------------------------------------------------------------


def chunk_text(text: str, limit: int, *, max_chunks: int | None = MAX_MESSAGES) -> list[str]:
    """Split text into messages of at most limit characters.

    Breaks between lines where it can, else at a space, else anywhere. Blank messages are dropped. With more than
    max_chunks messages, the rest is dropped and the last message ends with TRUNCATED_NOTE.
    """
    pieces: list[str] = []
    for line in text.strip("\n").split("\n"):
        while len(line) > limit:
            cut = line.rfind(" ", 0, limit + 1)
            if cut <= 0:
                cut = limit
            pieces.append(line[:cut].rstrip())
            line = line[cut:].lstrip()
        pieces.append(line)
    chunks: list[str] = []
    current: str | None = None
    for piece in pieces:
        if current is None:
            current = piece
        elif len(current) + 1 + len(piece) <= limit:
            current += "\n" + piece
        else:
            chunks.append(current)
            current = piece
    if current is not None:
        chunks.append(current)
    chunks = [chunk.strip("\n") for chunk in chunks if chunk.strip()]
    if max_chunks and len(chunks) > max_chunks:
        chunks = chunks[:max_chunks]
        room = limit - len(TRUNCATED_NOTE) - 1
        chunks[-1] = chunks[-1][:room].rstrip() + "\n" + TRUNCATED_NOTE if room > 0 else TRUNCATED_NOTE[:limit]
    return chunks


def slack_text(markdown: str) -> str:
    """Markdown as Slack mrkdwn: headings and **bold** as *bold*, links as <url|text>, &, < and > escaped."""

    def text(segment: str) -> str:
        return _BOLD.sub(r"*\1*", _slack_escape(_unescape(segment)))

    def link(label: str, url: str) -> str:
        safe = url.replace("|", "%7C").replace("<", "%3C").replace(">", "%3E")
        return f"<{safe}|{_slack_escape(label)}>"

    lines = []
    for line, table_row in _chat_lines(markdown):
        heading = None if table_row else _HEADING.match(line)
        if heading:
            line = f"**{heading.group(1)}**"
        lines.append(_inline(line, text, link))
    return "\n".join(lines)


def discord_text(markdown: str) -> str:
    """Markdown for Discord: the same, except tables (which Discord can't show) become one line per row."""
    return "\n".join(line for line, _ in _chat_lines(markdown))


def plain_text(markdown: str) -> str:
    """Markdown as plain text (for Telegram): no markup, links as "text (url)", tables as one line per row."""

    def text(segment: str) -> str:
        return _unescape(_BOLD.sub(r"\1", segment))

    def link(label: str, url: str) -> str:
        return f"{label} ({url})"

    lines = []
    for line, table_row in _chat_lines(markdown):
        if not table_row:
            heading = _HEADING.match(line)
            if heading:
                line = heading.group(1)
            italic = _ITALIC_LINE.match(line)
            if italic:
                line = italic.group(1) + italic.group(2)
        lines.append(_inline(line, text, link))
    return "\n".join(lines)


def _chat_lines(markdown: str) -> list[tuple[str, bool]]:
    """(line, is_table_row) for the Markdown's lines: tables flattened to one line per row ("label: value", or the
    cells joined by " · "), separator rows and horizontal rules dropped, runs of blank lines cut to one."""
    lines: list[tuple[str, bool]] = []
    for line in markdown.strip("\n").split("\n"):
        stripped = line.strip()
        if _RULE.match(line):
            line, stripped = "", ""
        if len(stripped) >= 2 and stripped.startswith("|") and stripped.endswith("|"):
            cells = [cell.strip() for cell in _CELL_SPLIT.split(stripped[1:-1])]
            if all(not cell or _TABLE_SEPARATOR.fullmatch(cell) for cell in cells):
                continue
            filled = [cell for cell in cells if cell]
            row = f"{filled[0]}: {filled[1]}" if len(cells) == 2 and len(filled) == 2 else " · ".join(filled)
            lines.append((row, True))
        elif stripped or (lines and lines[-1][0].strip()):
            lines.append((line, False))
    while lines and not lines[-1][0].strip():
        lines.pop()
    return lines


def _inline(line: str, text: Callable[[str], str], link: Callable[[str, str], str]) -> str:
    """Rewrite one line: links through link(label, url), the text between them through text(segment)."""
    out = []
    position = 0
    for match in _MD_LINK.finditer(line):
        out.append(text(line[position : match.start()]))
        out.append(link(_unescape(match.group(1)), match.group(2) or match.group(3)))
        position = match.end()
    out.append(text(line[position:]))
    return "".join(out)


def _unescape(text: str) -> str:
    return _MD_UNESCAPE.sub(r"\1", text)


def _slack_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _one_line(text: object) -> str:
    return " ".join(str(text).split())


def _and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


# --- setup and short alerts ------------------------------------------------------------------------------------------


def build_notifiers(settings: NotifySettings, *, session=None) -> list[Notifier]:
    """A notifier for every channel whose settings are complete.

    A channel that isn't configured at all is skipped quietly; one that is half configured is skipped with a warning
    that names the missing setting. The webhook and Telegram notifiers share one HTTP session.
    """
    notifiers: list[Notifier] = []
    email_any = any(
        (settings.smtp_host, settings.email_to, settings.smtp_from, settings.smtp_user, settings.smtp_password)
    )
    missing = email_missing(settings)
    if not email_any:
        log.debug("Email notifications are off (SMTP_HOST and EMAIL_TO aren't set).")
    elif missing:
        log.warning("Email notifications are off: set %s in .env.", _and(missing))
    else:
        notifiers.append(EmailNotifier(settings))

    needs_http = settings.webhook_url or settings.telegram_bot_token or settings.telegram_chat_id
    if session is None and needs_http:
        session = requests.Session()
    if settings.webhook_url:
        if safe_url(settings.webhook_url):
            notifiers.append(WebhookNotifier(settings.webhook_url, settings.webhook_format, session=session))
        else:
            log.warning("Webhook notifications are off: WEBHOOK_URL must start with https:// (or http://).")

    token, chat_id = settings.telegram_bot_token, settings.telegram_chat_id
    if token and chat_id:
        notifiers.append(TelegramNotifier(token, chat_id, session=session))
    elif token or chat_id:
        log.warning(
            "Telegram notifications are off: set %s in .env.", "TELEGRAM_CHAT_ID" if token else "TELEGRAM_BOT_TOKEN"
        )
    return notifiers


def _opportunities(count: int) -> str:
    return "opportunity" if count == 1 else "opportunities"


def short_alert(opps: list[Opportunity], *, now: datetime | None = None) -> str:
    """Compact text for chat notifiers: a count line, then one line per opportunity (best score first), e.g.

    - AMD (Advanced Micro Devices) · score 72.4 · 68% chance up in 6m · price $142.50 · entry $132.00 ·
      target $168.00 (+17.9% from price, +27.3% from entry) · low $118.00 · Temporary fear, medium confidence

    (on one line), and a reminder that the numbers are an uncalibrated model estimate and not investment advice.
    Price, entry and target carry their value in the [account] currency when it differs ("$132.00 ≈ €115.93").
    Given now, opportunities analysed before it (alerts that couldn't be sent earlier) say how old they are.
    """
    if not opps:
        return "No new dip opportunities."
    ranked = sorted(opps, key=lambda opp: opp.score, reverse=True)
    earlier = [opp for opp in ranked if now is not None and utc(opp.created) < utc(now)]
    fresh = len(ranked) - len(earlier)
    if not earlier:
        head = f"{fresh} new dip {_opportunities(fresh)}"
    elif fresh:
        head = f"{fresh} new dip {_opportunities(fresh)}, {len(earlier)} not sent earlier"
    else:
        head = f"{len(earlier)} dip {_opportunities(len(earlier))} not sent earlier"
    lines = [f"{head}:"]
    for opp in ranked:
        analysis, currency = opp.analysis, opp.currency
        parts = [
            f"{_one_line(opp.ticker)} ({_one_line(opp.company)})" if opp.company else _one_line(opp.ticker),
            f"score {opp.score:.1f}",
            f"{analysis.probability_up_6m}% chance up in 6m",
            f"price {format_money(opp.price, opp)}",
            f"entry {format_money(analysis.entry_price, opp)}",
            f"target {format_money(analysis.target_price, opp)} ({format_pct(opp.upside_pct())} from price, "
            f"{format_pct(opp.entry_upside_pct())} from entry)",
            f"low {format_price(analysis.potential_low, currency)}",
            f"{verdict_label(analysis.verdict)}, {analysis.confidence} confidence",
        ]
        if opp in earlier:
            hours = (utc(now) - utc(opp.created)).total_seconds() / 3600
            parts.append(f"not sent earlier: analysed {hours:.1f}h ago ({format_when(opp.created)})")
        lines.append("- " + " · ".join(parts))
    lines.append(ALERT_FOOTER)
    return "\n".join(lines)
