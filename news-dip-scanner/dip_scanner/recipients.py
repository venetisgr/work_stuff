"""Who gets alerts: the command line's "default" recipient (the .env channels and scanner.toml's [alerts]) and, on
the website, each user with an alert channel of their own.

A Recipient carries its own alert rules, watchlist, channels, currency and time zone. The Scanner (pipeline.py) asks
for the recipients at every cycle, so a changed setting applies from the next cycle without a restart, and keeps
each recipient's alert state (sent, not sent, repeats, thesis changes) apart in the alert_deliveries table.

Website users' channels use the server's settings: email goes through the server's SMTP settings to the user's own
address, Telegram through the server's bot to the user's chat id, and a webhook to the user's URL, which is checked
again (netguard) before every message and never followed through a redirect.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, tzinfo

from .accounts import Accounts, User, UserSettings, email_ready, telegram_ready
from .config import AlertConfig, ConfigError, ScannerConfig, Settings, display_zone
from .netguard import Resolver, check_public_url
from .notify import EmailNotifier, Notifier, TelegramNotifier, WebhookNotifier
from .store import DEFAULT_RECIPIENT, Store

log = logging.getLogger(__name__)

TEST_SUBJECT = "dip-scanner: test alert"
_USER_WEBHOOK_HINTS = {
    400: "Check that the webhook format in your settings matches the service.",
    401: "Check the webhook address in your settings.",
    403: "Check the webhook address in your settings.",
    404: "Check the webhook address in your settings (the webhook may have been deleted).",
}
_USER_TELEGRAM_HINTS = {
    400: "Check the Telegram chat id in your settings, and that you have started a chat with the bot.",
    403: "The bot can't write to that chat: start a chat with the bot (or add it to the group) first.",
}


@dataclass(frozen=True)
class Recipient:
    """One destination of alerts with its own rules.

    alerts holds the rules of an alert (min_score, min_probability, verdicts) and of repeats (repeat_hours,
    min_score_change); with only_watchlist only tickers on the watchlist alert. currency and tz are how amounts and
    times are shown in its messages. since: only opportunities created from then on count for it (a website user's
    alerts start when they set up their first channel; None: no limit). admin: it also gets system notices ("default"
    always does).
    """

    key: str  # DEFAULT_RECIPIENT, or "user:<id>"
    label: str  # for logs and notes: "default" or the user's email address
    alerts: AlertConfig
    watchlist: tuple[str, ...]
    only_watchlist: bool
    notifiers: list[Notifier] = field(default_factory=list)
    currency: str | None = None
    tz: tzinfo = UTC
    thesis_changes: bool = True
    admin: bool = False
    since: datetime | None = None

    @property
    def gets_notices(self) -> bool:
        """Whether system notices (stopped, model unavailable, feeds failing) go to it."""
        return self.admin or self.key == DEFAULT_RECIPIENT


def default_recipient(settings: Settings, config: ScannerConfig, notifiers: Sequence[Notifier]) -> Recipient:
    """The command line's recipient: scanner.toml's [alerts], the .env channels, [account] currency and DISPLAY_TZ,
    alerting on any ticker ([universe] only_watchlist limits the candidates, not the alerts)."""
    return Recipient(
        key=DEFAULT_RECIPIENT,
        label=DEFAULT_RECIPIENT,
        alerts=config.alerts,
        watchlist=config.universe.watchlist,
        only_watchlist=False,
        notifiers=list(notifiers),
        currency=config.account.currency,
        tz=settings.display_tz,
    )


def user_notifiers(user: User, settings: Settings, *, session=None, resolver: Resolver | None = None) -> list[Notifier]:
    """The channels a user set up that the server can serve: email (with the server's SMTP settings, to the user's
    address), Telegram (with the server's bot) and a webhook (checked with netguard before every message)."""
    chosen = user.settings
    notifiers: list[Notifier] = []
    if chosen.email_alerts and email_ready(settings):
        notifiers.append(EmailNotifier(replace(settings.notify, email_to=[user.email])))
    if chosen.telegram_chat_id and telegram_ready(settings):
        token = settings.notify.telegram_bot_token or ""
        notifiers.append(TelegramNotifier(token, chosen.telegram_chat_id, session=session, hints=_USER_TELEGRAM_HINTS))
    if chosen.webhook_url:
        try:
            notifiers.append(
                WebhookNotifier(
                    chosen.webhook_url,
                    chosen.webhook_format,
                    session=session,
                    check_url=lambda url: check_public_url(url, resolver=resolver),
                    follow_redirects=False,
                    show_replies=False,
                    hints=_USER_WEBHOOK_HINTS,
                )
            )
        except ConfigError as exc:  # a stored URL that isn't http(s) any more: the others still work
            log.warning("Skipping the webhook of %s: %s", user.email, exc)
    return notifiers


def user_recipient(
    user: User,
    settings: Settings,
    config: ScannerConfig,
    *,
    session=None,
    resolver: Resolver | None = None,
) -> Recipient:
    """A website user as a recipient (notifiers may be empty when they set up no channel the server can serve)."""
    chosen = user.settings
    try:
        tz = display_zone(chosen.timezone)
    except ConfigError:
        tz = settings.display_tz
    return Recipient(
        key=user.recipient_key,
        label=user.email,
        alerts=replace(
            config.alerts,
            min_score=chosen.min_score,
            min_probability=chosen.min_probability,
            verdicts=tuple(chosen.verdicts),
        ),
        watchlist=preferred_symbols(chosen.watchlist, config),
        only_watchlist=chosen.only_watchlist,
        notifiers=user_notifiers(user, settings, session=session, resolver=resolver),
        currency=chosen.currency,
        tz=tz,
        thesis_changes=chosen.thesis_changes,
        admin=user.is_admin,
        since=user.alerts_since,
    )


def build_user_recipients(
    store: Store,
    settings: Settings,
    config: ScannerConfig,
    session=None,
    *,
    resolver: Resolver | None = None,
) -> list[Recipient]:
    """A recipient for every active user (enabled, with a password) with at least one working channel."""
    accounts = Accounts(store, defaults=UserSettings.defaults(config, settings))
    recipients = []
    for user in accounts.active_users():
        recipient = user_recipient(user, settings, config, session=session, resolver=resolver)
        if recipient.notifiers:
            recipients.append(recipient)
    return recipients


def preferred_symbols(symbols: Iterable[str], config: ScannerConfig) -> tuple[str, ...]:
    """Symbols read through [universe] preferred_listings ("ASML" -> "ASML.AS"), without repeats."""
    preferred = config.universe.preferred_listings
    return tuple(dict.fromkeys(preferred.get(symbol, symbol) for symbol in symbols))


def active_watchlist(store: Store, config: ScannerConfig) -> tuple[str, ...]:
    """Every active user's watchlist together (through preferred_listings): the scanner treats these tickers like
    scanner.toml's watchlist when it looks for candidates."""
    accounts = Accounts(store)
    return preferred_symbols((symbol for user in accounts.active_users() for symbol in user.settings.watchlist), config)


def active_currencies(store: Store) -> tuple[str, ...]:
    """The currencies active users chose for "≈" amounts: each analysis stores its exchange rates into all of them."""
    accounts = Accounts(store)
    return tuple(dict.fromkeys(user.settings.currency for user in accounts.active_users() if user.settings.currency))


def service_hooks(
    store: Store,
    settings: Settings,
    config: ScannerConfig,
    session=None,
    *,
    default_notifiers: Sequence[Notifier] = (),
    resolver: Resolver | None = None,
) -> dict[str, Callable[[datetime], object]]:
    """The Scanner's recipients, watchlist and currencies arguments for the website: `Scanner(...,
    **service_hooks(store, settings, config, session, default_notifiers=build_notifiers(settings.notify)))`.

    Each is asked at every cycle, so changed settings apply at once. The recipients are "default" (the .env channels)
    when default_notifiers isn't empty, then every user with a working channel. store must be the scanner's own.
    """

    def recipients(now: datetime) -> list[Recipient]:
        found = [default_recipient(settings, config, default_notifiers)] if default_notifiers else []
        return found + build_user_recipients(store, settings, config, session, resolver=resolver)

    def watchlist(now: datetime) -> tuple[str, ...]:
        return active_watchlist(store, config)

    def currencies(now: datetime) -> tuple[str, ...]:
        return active_currencies(store)

    return {"recipients": recipients, "watchlist": watchlist, "currencies": currencies}


def mark_manual_analysis(store: Store, opportunity_ids: Iterable[int], *, viewer: str | None, when: datetime) -> None:
    """Record a manual analysis ("Analyse now" on the website) the way `dip-scanner analyze` does: it is never sent to
    anybody as an alert, and it counts as alerted to the viewer (a recipient key such as "user:3"), so their repeats
    and thesis changes compare with what they read."""
    ids = list(opportunity_ids)
    store.mark_notified(ids, when=when, sent=False)
    if viewer:
        store.record_deliveries(viewer, ids, "alert", when=when, sent=True)


def send_test(recipient: Recipient, *, now: datetime) -> list[tuple[str, str | None]]:
    """Send a short test message to each of a recipient's channels ("Send test alert" on the settings page); returns
    (channel name, None when it arrived, else why not) per channel. Never raises."""
    lines = [
        f"This is a test from dip-scanner, sent at {now.astimezone(recipient.tz):%Y-%m-%d %H:%M %Z}.",
        "Dip alerts that pass your rules will arrive here. Not investment advice.",
    ]
    markdown = "\n\n".join([f"# {TEST_SUBJECT}", *lines]) + "\n"
    html = (
        f'<!DOCTYPE html><html><head><meta charset="utf-8"><title>{TEST_SUBJECT}</title></head><body>'
        f"<h1>{TEST_SUBJECT}</h1>" + "".join(f"<p>{line}</p>" for line in lines) + "</body></html>"
    )
    results: list[tuple[str, str | None]] = []
    for notifier in recipient.notifiers:
        name = getattr(notifier, "name", type(notifier).__name__)
        try:
            notifier.send(TEST_SUBJECT, markdown, html)
        except Exception as exc:  # NotifyError, or a notifier bug: reported to the user, never raised
            results.append((name, str(exc) or type(exc).__name__))
        else:
            results.append((name, None))
    return results
