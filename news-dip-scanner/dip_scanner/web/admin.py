"""Admin pages: the scanner (status, pause, resume, a cycle now, start again), its recent cycles, the news feeds'
health and the model's use with an estimated cost (/admin); the accounts (/admin/users: role, status, last sign-in,
alert channels; disable, enable, make admin or member, password links) and the invites (/admin/invites: create, email,
list, revoke).

Every route depends on auth.require_admin (auth.Admin): a member gets the 403 page and a signed-out visitor the sign-in
page. Every change is a POST whose form passed auth.checked_form (auth.SignedForm: the Origin check and the session's
CSRF token).

Invite and password links are shown once. The database keeps only their tokens' hashes, so the POST that creates one
keeps the whole link in this process's memory for the admin's own session (a few minutes at most) and redirects to the
page that shows it and forgets it: reloading that page doesn't show it again.

Costs are estimates from MODEL_PRICES, the list prices the README's "Costs" section quotes, in US dollars.
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from ..accounts import MAX_EMAIL_LENGTH, ROLES, AccountError, Invite, User, email_ready, telegram_ready
from ..config import ConfigError
from ..feeds import needs_contact_user_agent, user_agent_for
from ..models import CycleRecord, ModelUsage, utc
from ..notices import one_line, scrub, secrets_of
from ..notify import EmailNotifier, NotifyError, SmtpFactory
from ..report import display_zone_as, format_when
from . import auth
from .app import APP_NAME, FOOTER, error_page, flash, paginate, redirect, render
from .context import AppContext

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin")

CYCLES_PER_PAGE = 20
USAGE_DAYS = 7  # the model-use table covers today and the 6 UTC days before it
OLD_INVITES_SHOWN = 20  # used, revoked and expired invites listed under the pending ones
SHOWN_LINK_LIFETIME = timedelta(minutes=10)  # how long a new link waits in memory for the page that shows it once
STALE_FEED_INTERVALS = 3  # a feed not fetched for this many intervals (at least 30 minutes) is "not fetched lately"
RUN_NOW_LIMIT = 10  # "Run a cycle now" per admin in RUN_NOW_WINDOW
RUN_NOW_WINDOW = timedelta(minutes=15)
INVITE_MAIL_LIMIT = 20  # invite emails per admin in INVITE_MAIL_WINDOW
INVITE_MAIL_WINDOW = timedelta(hours=1)
MAIL_TIMEOUT = 20.0  # seconds for the SMTP server
SMTP_FACTORY: SmtpFactory | None = None  # how invite emails reach the SMTP server (None: smtplib; tests: a fake)
GONE = "That account doesn't exist (any more)."

_DATE_SUFFIX = re.compile(r"-(?:\d{4}-\d{2}-\d{2}|\d{8})$")
_CYCLE_FACTS = (
    ("new_articles", "new article", "new articles"),
    ("impacts", "company impact", "company impacts"),
    ("candidates", "candidate", "candidates"),
    ("opportunities", "idea", "ideas"),
    ("alerts", "alert", "alerts"),
    ("thesis_changes", "thesis change", "thesis changes"),
    ("model_calls", "model call", "model calls"),
)
_STEP_LABELS = {
    "triage": "Triage",
    "analysis": "Analysis",
    "analysis:opening": "Debate: openings",
    "analysis:rebuttal": "Debate: rebuttals",
    "analysis:judge": "Debate: judge",
}
_WEBHOOK_LABELS = {"slack": "Slack", "discord": "Discord", "generic": "Webhook"}


# --- the price table -----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PriceTable:
    """List prices in US dollars per million tokens, (input, output) by model name, as checked on a date.

    Every cost worked out from it is an estimate: providers change their prices, may bill cached input for less, and
    an Azure deployment or a gateway has its own. A model that isn't in the table has no estimate.
    """

    checked: date
    prices: Mapping[str, tuple[float, float]]
    source: str

    def price_of(self, model: str) -> tuple[float, float] | None:
        """(input, output) per million tokens of a model: its name as the service reported it, in any case, with or
        without a provider prefix ("openai/gpt-5") or a snapshot date ("gpt-5-2025-08-07"); None when unknown."""
        name = str(model).strip().lower().rsplit("/", 1)[-1]
        found = self.prices.get(name)
        if found is None:
            found = self.prices.get(_DATE_SUFFIX.sub("", name))
        return found

    def cost(self, model: str, input_tokens: int, output_tokens: int) -> float | None:
        """The estimated cost in dollars of these tokens on model; None when the model has no price here."""
        price = self.price_of(model)
        if price is None:
            return None
        return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000


# The list prices the README's "Costs" section quotes for the default models (the debate's included), checked on
# 2026-09-28. Update this and the README together when the providers change their prices.
MODEL_PRICES = PriceTable(
    checked=date(2026, 9, 28),
    prices={
        "gpt-5-mini": (0.25, 2.00),
        "gpt-5": (1.25, 10.00),
        "claude-haiku-4-5": (1.00, 5.00),
        "claude-sonnet-5": (2.00, 10.00),
    },
    source="OpenAI's and Anthropic's list prices",
)


def usd(value: float | None) -> str:
    """A cost for people: "$0.27", "< $0.01", "$1,234"; "–" when there is none."""
    if value is None:
        return "–"
    if value == 0:
        return "$0.00"
    if value < 0.01:
        return "< $0.01"
    if value < 1000:
        return f"${value:,.2f}"
    return f"${value:,.0f}"


def _price_text(price: tuple[float, float]) -> str:
    return f"${price[0]:g} in, ${price[1]:g} out"


# --- model use -----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class UsageRow:
    """One step and model's calls over a period, with the estimated cost (None: no price for the model)."""

    step: str
    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    unmetered: int
    cost: float | None

    @property
    def step_label(self) -> str:
        return _STEP_LABELS.get(self.step, self.step.capitalize())

    @property
    def cost_text(self) -> str:
        return usd(self.cost)


@dataclass(frozen=True)
class UsagePeriod:
    """The model's use since a time: one row per step and model, and the totals."""

    label: str
    since: datetime
    rows: list[UsageRow]

    @property
    def calls(self) -> int:
        return sum(row.calls for row in self.rows)

    @property
    def input_tokens(self) -> int:
        return sum(row.input_tokens for row in self.rows)

    @property
    def output_tokens(self) -> int:
        return sum(row.output_tokens for row in self.rows)

    @property
    def unmetered(self) -> int:
        return sum(row.unmetered for row in self.rows)

    @property
    def cost(self) -> float:
        """The estimated cost of the rows with a price (see unpriced)."""
        return sum(row.cost for row in self.rows if row.cost is not None)

    @property
    def cost_text(self) -> str:
        return usd(self.cost) if self.rows else usd(0)

    @property
    def unpriced(self) -> list[str]:
        """The models without a price in MODEL_PRICES (their calls aren't in the cost)."""
        return sorted({row.model for row in self.rows if row.cost is None})


@dataclass(frozen=True)
class UsageDay:
    """One UTC day's model use."""

    day: date
    calls: int
    input_tokens: int
    output_tokens: int
    cost: float
    unpriced: bool  # some calls were to a model without a price

    @property
    def cost_text(self) -> str:
        return usd(self.cost) + ("+" if self.unpriced else "")


def usage_period(rows: Iterable[ModelUsage], *, label: str, since: datetime) -> UsagePeriod:
    """The rows of Store.model_usage with their estimated costs."""
    return UsagePeriod(
        label=label,
        since=since,
        rows=[
            UsageRow(
                step=row.step,
                model=row.model,
                calls=row.calls,
                input_tokens=row.input_tokens,
                output_tokens=row.output_tokens,
                unmetered=row.unmetered,
                cost=MODEL_PRICES.cost(row.model, row.input_tokens, row.output_tokens),
            )
            for row in rows
        ],
    )


def _stamp(moment: datetime) -> str:
    """A time as the store writes it (fixed-width ISO 8601 UTC), for comparing with stored text."""
    return utc(moment).isoformat(timespec="microseconds")


def daily_usage(ctx: AppContext, *, first_day: date, days: int) -> list[UsageDay]:
    """The model's use on each UTC day from first_day, oldest first (days without calls included)."""
    start = datetime(first_day.year, first_day.month, first_day.day, tzinfo=UTC)
    rows = ctx.store.query(
        """
        SELECT substr(created, 1, 10) AS day, model, COUNT(*) AS calls,
               COALESCE(SUM(input_tokens), 0) AS input_tokens, COALESCE(SUM(output_tokens), 0) AS output_tokens
        FROM model_calls WHERE created >= ? GROUP BY day, model
        """,
        (_stamp(start),),
    )
    totals: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = totals.setdefault(row["day"], {"calls": 0, "in": 0, "out": 0, "cost": 0.0, "unpriced": False})
        entry["calls"] += row["calls"]
        entry["in"] += row["input_tokens"]
        entry["out"] += row["output_tokens"]
        cost = MODEL_PRICES.cost(row["model"], row["input_tokens"], row["output_tokens"])
        if cost is None:
            entry["unpriced"] = True
        else:
            entry["cost"] += cost
    result = []
    for offset in range(days):
        day = first_day + timedelta(days=offset)
        entry = totals.get(day.isoformat(), {"calls": 0, "in": 0, "out": 0, "cost": 0.0, "unpriced": False})
        result.append(
            UsageDay(
                day=day,
                calls=entry["calls"],
                input_tokens=entry["in"],
                output_tokens=entry["out"],
                cost=entry["cost"],
                unpriced=entry["unpriced"],
            )
        )
    return result


@dataclass(frozen=True)
class MonthEstimate:
    """30 days at the average estimated cost of the full UTC days (not today) that had model calls."""

    cost: float
    days: int  # how many days the average is of

    @property
    def cost_text(self) -> str:
        return usd(self.cost)


def monthly_estimate(days: Sequence[UsageDay], today: date) -> MonthEstimate | None:
    """The MonthEstimate of days, or None when no full day had model calls."""
    full = [day for day in days if day.day < today and day.calls]
    if not full:
        return None
    return MonthEstimate(cost=sum(day.cost for day in full) / len(full) * 30, days=len(full))


# --- cycles and feeds ----------------------------------------------------------------------------------------------


def _duration(seconds: float) -> str:
    seconds = max(0, round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {rest} s" if rest else f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min"


def _count(number: int, singular: str, plural: str) -> str:
    return f"{number:,} {singular if number == 1 else plural}"


@dataclass(frozen=True)
class CycleView:
    """A stored cycle as the admin page lists it."""

    record: CycleRecord
    duration: str | None  # "42 s"; None for a cycle without a finish time
    facts: list[str]  # "18/20 feeds", "12 new articles", ...
    summary: str  # the summary without what the list shows beside it (cycle_text)


# "Cycle 2026-09-25 14:50 EEST: " or "Cycle 2026-09-25 14:55 UTC failed: " (Scanner._record_cycle), and "; took 42 s"
_CYCLE_PREFIX = re.compile(r"^Cycle \d{4}-\d{2}-\d{2} \d{2}:\d{2}(?: \S+)?(?: failed)?: ")
_CYCLE_TOOK = re.compile(r"; took [^;]+")


def cycle_text(summary: str) -> str:
    """A cycle's summary without its start time, "failed" and "took ..." (the list shows them next to it)."""
    return _CYCLE_TOOK.sub("", _CYCLE_PREFIX.sub("", summary, count=1), count=1).strip() or summary.strip()


def cycle_view(record: CycleRecord) -> CycleView:
    stats = record.stats
    facts = []
    if "feeds_ok" in stats:
        facts.append(f"{stats['feeds_ok']}/{stats['feeds_ok'] + stats.get('feeds_failed', 0)} feeds")
    for key, singular, plural in _CYCLE_FACTS:
        if stats.get(key) or (key == "new_articles" and key in stats):  # zeros only for the new articles
            facts.append(_count(int(stats[key]), singular, plural))
    duration = None
    if record.finished is not None:
        duration = _duration((record.finished - record.started).total_seconds())
    return CycleView(record=record, duration=duration, facts=facts, summary=cycle_text(record.summary))


@dataclass(frozen=True)
class FeedRow:
    """A feed of feeds.toml and how its last fetch went."""

    key: str
    name: str
    category: str
    url: str
    state: str  # "failing", "stale", "never", "skipped" or "ok"
    last_fetch: datetime | None
    last_status: int | None
    error: str | None
    articles: int

    @property
    def label(self) -> str:
        return {
            "failing": "Failing",
            "stale": "Not fetched lately",
            "never": "Not fetched yet",
            "skipped": "Skipped",
            "ok": "OK",
        }[self.state]

    @property
    def badge(self) -> str:
        return {"failing": "bad", "stale": "warn", "never": "outline", "skipped": "warn", "ok": "ok"}[self.state]


_FEED_ORDER = {"failing": 0, "stale": 1, "never": 2, "skipped": 3, "ok": 4}


def feed_rows(ctx: AppContext) -> tuple[list[FeedRow], int]:
    """The enabled feeds with their health, problems first, and how many feeds.toml switches off."""
    now = ctx.now()
    health = {entry["key"]: entry for entry in ctx.store.feed_health()}
    stale_after = timedelta(minutes=max(30.0, ctx.config.scan.interval_minutes * STALE_FEED_INTERVALS))
    agents = {"sec.gov": ctx.settings.sec_user_agent}
    secrets = secrets_of(ctx.settings)
    rows = []
    for feed in ctx.feeds:
        if not feed.enabled:
            continue
        entry = health.get(feed.key) or {}
        last_fetch, error = entry.get("last_fetch"), entry.get("last_error")
        if needs_contact_user_agent(feed.url) and not user_agent_for(feed.url, agents):
            state, error = "skipped", "Needs SEC_USER_AGENT (your name and email) in .env or, on Fly.io, as a secret."
        elif last_fetch is None:
            state = "never"
        elif error:
            state = "failing"
        elif now - last_fetch > stale_after:
            state = "stale"
        else:
            state = "ok"
        rows.append(
            FeedRow(
                key=feed.key,
                name=feed.name,
                category=feed.category,
                url=feed.url,
                state=state,
                last_fetch=last_fetch,
                last_status=entry.get("last_status"),
                error=one_line(scrub(error, secrets), 300) if error else None,
                articles=int(entry.get("articles") or 0),
            )
        )
    rows.sort(key=lambda row: (_FEED_ORDER[row.state], row.name.lower()))
    return rows, sum(1 for feed in ctx.feeds if not feed.enabled)


# --- one-time links ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class OneTimeLink:
    """An invite or password link just created, waiting for the page that shows it once."""

    kind: str  # "invite" or "password"
    url: str
    expires: datetime
    email: str | None  # who it is for; None: anyone with the link (an invite without an address)
    role: str | None = None  # an invite's role
    purpose: str | None = None  # a password link's: "setup" or "reset"
    emailed: bool = False  # the invite was emailed to email


def _link_key(request: Request, kind: str) -> tuple[str, str, str] | None:
    session = auth.load_session(request)
    return ("admin-link", kind, session.token_hash) if session is not None else None


def keep_link(request: Request, ctx: AppContext, link: OneTimeLink) -> None:
    """Keep link for the next page this admin's session opens (in memory only, never in the database)."""
    key = _link_key(request, link.kind)
    if key is not None:
        ctx.cache.set(key, link, seconds=SHOWN_LINK_LIFETIME.total_seconds())


def take_link(request: Request, ctx: AppContext, kind: str) -> OneTimeLink | None:
    """The link kept for this admin's session, once: it is forgotten as it is taken."""
    key = _link_key(request, kind)
    if key is None:
        return None
    link = ctx.cache.get(key)
    if link is not None:
        ctx.cache.set(key, None, seconds=0)  # expires at once
    return link if isinstance(link, OneTimeLink) else None


def absolute_link(request: Request, ctx: AppContext, path: str) -> str:
    """path on BASE_URL, or on the address this page was opened at when BASE_URL isn't set."""
    try:
        return ctx.settings.web.link(path)
    except ConfigError:
        return f"{request.url.scheme}://{request.url.netloc}{path}"


# --- invite emails -------------------------------------------------------------------------------------------------


def invite_email(*, link: str, inviter: User, role: str, expires: datetime) -> tuple[str, str, str]:
    """(subject, text, HTML) of the email that sends an invite link. Times are in DISPLAY_TZ."""
    who = inviter.label
    as_role = " as an admin" if role == "admin" else ""
    until = format_when(expires)
    subject = f"Your invite to {APP_NAME}"
    text = (
        f"{who} invited you{as_role} to {APP_NAME}, a website that reads financial news, finds shares that fell on "
        "it and has a language model judge whether each drop is a temporary fear or real damage.\n\n"
        f"Create your account with this link. It works once, until {until}:\n\n{link}\n\n"
        "If you didn't expect this invite, ignore this email: nothing happens without the link.\n\n"
        f"{FOOTER}\n"
    )
    body = (
        f"<p>{html.escape(who)} invited you{as_role} to {html.escape(APP_NAME)}, a website that reads financial "
        "news, finds shares that fell on it and has a language model judge whether each drop is a temporary fear or "
        "real damage.</p>"
        f"<p>Create your account with this link. It works once, until {html.escape(until)}:</p>"
        f'<p><a href="{html.escape(link, quote=True)}">{html.escape(link)}</a></p>'
        "<p>If you didn't expect this invite, ignore this email: nothing happens without the link.</p>"
        f"<p><small>{html.escape(FOOTER)}</small></p>"
    )
    return subject, text, f"<!DOCTYPE html><html><body>{body}</body></html>"


def send_invite_email(ctx: AppContext, invite: Invite, link: str, inviter: User) -> None:
    """Email an invite link to the invite's address through the server's SMTP settings (SMTP_*); NotifyError when it
    couldn't be sent."""
    if not invite.email:
        raise NotifyError("This invite is for anyone with the link, so there is no address to email it to.")
    notifier = EmailNotifier(
        replace(ctx.settings.notify, email_to=[invite.email]), smtp_factory=SMTP_FACTORY, timeout=MAIL_TIMEOUT
    )
    with display_zone_as(ctx.settings.display_tz):
        subject, text, body = invite_email(link=link, inviter=inviter, role=invite.role, expires=invite.expires)
    notifier.send(subject, text, body)


# --- the scanner ---------------------------------------------------------------------------------------------------


@router.get("")
def overview(request: Request, user: auth.Admin, ctx: auth.Ctx) -> Response:
    now = ctx.now()
    status = ctx.control.status(now=now)
    today_start = datetime(now.year, now.month, now.day, tzinfo=UTC)  # the model's day counts from 00:00 UTC
    week_start = today_start - timedelta(days=USAGE_DAYS - 1)
    today = usage_period(ctx.store.model_usage(since=today_start), label="Today", since=today_start)
    week = usage_period(ctx.store.model_usage(since=week_start), label=f"Last {USAGE_DAYS} days", since=week_start)
    days = daily_usage(ctx, first_day=week_start.date(), days=USAGE_DAYS)

    total = int(ctx.store.query("SELECT COUNT(*) FROM cycles")[0][0])
    page = paginate(total, request.query_params.get("page"), size=CYCLES_PER_PAGE)
    records = ctx.store.cycles(limit=page.offset + page.size)[page.offset :]
    last_ok = ctx.store.last_cycle(ok=True) if status.last_cycle is not None and not status.last_cycle.ok else None
    feeds, feeds_off = feed_rows(ctx)
    return render(
        request,
        "admin/index.html",
        {
            "status": status,
            "paused": ctx.control.paused(),
            "last_ok": last_ok,
            "today": today,
            "week": week,
            "days": list(reversed(days)),
            "month": monthly_estimate(days, today_start.date()),
            "prices": MODEL_PRICES,
            "price_list": [(name, _price_text(price)) for name, price in MODEL_PRICES.prices.items()],
            "cycles": [cycle_view(record) for record in records],
            "page": page,
            "feeds": feeds,
            "feeds_off": feeds_off,
            "feed_problems": sum(1 for row in feeds if row.state in ("failing", "stale")),
            "admin_tab": "scanner",
            "nav": "admin",
            "page_title": "Admin",
        },
    )


@router.post("/scanner/pause")
def pause_scanner(request: Request, user: auth.Admin, form: auth.SignedForm, ctx: auth.Ctx) -> Response:
    ctx.control.pause()
    log.info("Account #%d paused the scanner.", user.id)
    return redirect(
        request,
        "/admin",
        "The scanner is paused: no news is checked until you resume it. A cycle that was running finishes first.",
        kind="info",
    )


@router.post("/scanner/resume")
def resume_scanner(request: Request, user: auth.Admin, form: auth.SignedForm, ctx: auth.Ctx) -> Response:
    ctx.control.resume()
    log.info("Account #%d resumed the scanner.", user.id)
    return redirect(request, "/admin", "The scanner is running again: the next cycle starts at the next interval.")


@router.post("/scanner/run")
def run_cycle_now(request: Request, user: auth.Admin, form: auth.SignedForm, ctx: auth.Ctx) -> Response:
    if ctx.accounts.rate_limited(f"run-now:{user.id}", limit=RUN_NOW_LIMIT, window=RUN_NOW_WINDOW):
        return error_page(
            request,
            429,
            f"You asked for {RUN_NOW_LIMIT} cycles in the last 15 minutes. The scanner runs on its own every "
            f"{ctx.control.interval_minutes:g} minutes; wait a little before asking again.",
        )
    if not ctx.control.run_now():
        status = ctx.control.status(now=ctx.now())
        reason = f" ({status.reason})" if status.reason else ""
        message = f"The scanner isn't running, so it can't start a cycle{reason}."
        return redirect(request, "/admin", message, kind="error")
    log.info("Account #%d asked for a cycle now.", user.id)
    return redirect(
        request, "/admin", "A cycle starts now, even if the scanner is paused. Reload this page in a minute to see it."
    )


@router.post("/scanner/restart")
def restart_scanner(request: Request, user: auth.Admin, form: auth.SignedForm, ctx: auth.Ctx) -> Response:
    if not ctx.control.restart():
        return redirect(
            request,
            "/admin",
            "The scanner can't be started from here: it is running, switched off in this process, or couldn't be "
            "set up at all (then fix the problem it names and restart the website).",
            kind="error",
        )
    log.info("Account #%d started the scanner again.", user.id)
    return redirect(
        request,
        "/admin",
        "The scanner is starting again. If the problem is still there, it stops again and says so here.",
    )


# --- users ---------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class UserRow:
    """An account as the users page lists it."""

    user: User
    status: str  # "active", "disabled" or "no password"
    channels: list[tuple[str, bool]]  # (label, whether the server can send it)
    is_me: bool
    only_admin: bool  # the last admin who can sign in: can't be made a member or disabled


def _channels(user: User, *, email_ok: bool, telegram_ok: bool) -> list[tuple[str, bool]]:
    chosen = user.settings
    channels = []
    if chosen.email_alerts:
        channels.append(("Email", email_ok))
    if chosen.telegram_chat_id:
        channels.append(("Telegram", telegram_ok))
    if chosen.webhook_url:
        channels.append((_WEBHOOK_LABELS.get(chosen.webhook_format, "Webhook"), True))
    return channels


def user_rows(ctx: AppContext, me: User) -> list[UserRow]:
    users = ctx.accounts.list_users()
    admins = [user for user in users if user.is_admin and not user.disabled and user.has_password]
    email_ok, telegram_ok = email_ready(ctx.settings), telegram_ready(ctx.settings)
    return [
        UserRow(
            user=user,
            status="disabled" if user.disabled else "active" if user.has_password else "no password",
            channels=_channels(user, email_ok=email_ok, telegram_ok=telegram_ok),
            is_me=user.id == me.id,
            only_admin=len(admins) == 1 and admins[0].id == user.id,
        )
        for user in users
    ]


@router.get("/users")
def users_page(request: Request, user: auth.Admin, ctx: auth.Ctx) -> Response:
    rows = user_rows(ctx, user)
    return render(
        request,
        "admin/users.html",
        {
            "rows": rows,
            "admins": sum(1 for row in rows if row.user.is_admin and row.status == "active"),
            "shown_link": take_link(request, ctx, "password"),
            "base_url_set": bool(ctx.settings.web.base_url),
            "admin_tab": "users",
            "nav": "admin",
            "page_title": "Users · Admin",
        },
    )


@router.post("/users/{user_id}/disable")
def disable_user(request: Request, user_id: int, user: auth.Admin, form: auth.SignedForm, ctx: auth.Ctx) -> Response:
    target = ctx.accounts.get_user(user_id)
    if target is None:
        return redirect(request, "/admin/users", GONE, kind="error")
    if target.id == user.id:
        return redirect(request, "/admin/users", "You can't disable your own account; another admin can.", kind="error")
    try:
        target = ctx.accounts.set_disabled(target.id, True)
    except AccountError as exc:
        return redirect(request, "/admin/users", str(exc), kind="error")
    log.info("Account #%d disabled account #%d.", user.id, target.id)
    return redirect(
        request, "/admin/users", f"{target.email} is disabled and was signed out everywhere. Their alerts stop too."
    )


@router.post("/users/{user_id}/enable")
def enable_user(request: Request, user_id: int, user: auth.Admin, form: auth.SignedForm, ctx: auth.Ctx) -> Response:
    target = ctx.accounts.get_user(user_id)
    if target is None:
        return redirect(request, "/admin/users", GONE, kind="error")
    target = ctx.accounts.set_disabled(target.id, False)
    log.info("Account #%d enabled account #%d.", user.id, target.id)
    if target.has_password:
        return redirect(request, "/admin/users", f"{target.email} is enabled again and can sign in.")
    return redirect(
        request,
        "/admin/users",
        f"{target.email} is enabled again. They have no password yet: create a setup link for them.",
    )


@router.post("/users/{user_id}/role")
def change_role(request: Request, user_id: int, user: auth.Admin, form: auth.SignedForm, ctx: auth.Ctx) -> Response:
    role = str(form.get("role") or "")
    if role not in ROLES:
        return redirect(request, "/admin/users", "Choose admin or member.", kind="error")
    target = ctx.accounts.get_user(user_id)
    if target is None:
        return redirect(request, "/admin/users", GONE, kind="error")
    if target.role == role:
        return redirect(request, "/admin/users", f"{target.email} is already a{'n' if role == 'admin' else ''} {role}.")
    try:
        target = ctx.accounts.set_role(target.id, role)
    except AccountError as exc:
        return redirect(request, "/admin/users", str(exc), kind="error")
    log.info("Account #%d made account #%d a%s %s.", user.id, target.id, "n" if role == "admin" else "", role)
    if target.id == user.id:  # no admin pages for them any more
        return redirect(request, "/", "You're a member now: the admin pages are for admins only.", kind="info")
    if role == "admin":
        return redirect(
            request, "/admin/users", f"{target.email} is now an admin: they can manage users, invites and the scanner."
        )
    return redirect(request, "/admin/users", f"{target.email} is now a member.")


@router.post("/users/{user_id}/password-link")
def password_link(request: Request, user_id: int, user: auth.Admin, form: auth.SignedForm, ctx: auth.Ctx) -> Response:
    target = ctx.accounts.get_user(user_id)
    if target is None:
        return redirect(request, "/admin/users", GONE, kind="error")
    if target.disabled:
        return redirect(
            request,
            "/admin/users",
            f"{target.email} is disabled, so a password link wouldn't work: enable the account first.",
            kind="error",
        )
    token = ctx.accounts.create_password_token(target.id)
    found = ctx.accounts.get_password_token(token)
    if found is None:  # can't happen for an enabled user; never show a dead link
        return redirect(request, "/admin/users", "The link couldn't be created. Try again.", kind="error")
    keep_link(
        request,
        ctx,
        OneTimeLink(
            kind="password",
            url=absolute_link(request, ctx, f"/password/{token}"),
            expires=found.expires,
            email=target.email,
            purpose=found.purpose,
        ),
    )
    what = "setup" if found.purpose == "setup" else "password reset"
    log.info("Account #%d created a password %s link for account #%d.", user.id, found.purpose, target.id)
    return redirect(
        request,
        "/admin/users#new-link",
        f"A {what} link for {target.email} is ready. Copy it now: it is shown only once.",
    )


# --- invites -------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class InviteRow:
    """An invite as the invites page lists it."""

    invite: Invite
    status: str  # Invite.status: "pending", "used", "revoked" or "expired"
    created_by: str | None  # the admin's label; None when made with `dip-scanner users invite`
    used_by: str | None  # the account it created

    @property
    def badge(self) -> str:
        return {"pending": "info", "used": "ok", "revoked": "outline", "expired": "outline"}.get(self.status, "outline")


def invite_rows(ctx: AppContext) -> tuple[list[InviteRow], list[InviteRow], int]:
    """(pending invites, the newest others, how many older ones aren't listed)."""
    now = ctx.now()
    labels = {user.id: user.label for user in ctx.accounts.list_users()}
    rows = [
        InviteRow(
            invite=invite,
            status=invite.status(now),
            created_by=labels.get(invite.created_by) if invite.created_by is not None else None,
            used_by=labels.get(invite.used_by, "a deleted account") if invite.used_by is not None else None,
        )
        for invite in ctx.accounts.list_invites(pending_only=False)
    ]
    pending = [row for row in rows if row.status == "pending"]
    others = [row for row in rows if row.status != "pending"]
    return pending, others[:OLD_INVITES_SHOWN], max(0, len(others) - OLD_INVITES_SHOWN)


def invites_page(
    request: Request,
    ctx: AppContext,
    *,
    values: dict[str, Any] | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    pending, others, hidden = invite_rows(ctx)
    can_email = email_ready(ctx.settings)
    return render(
        request,
        "admin/invites.html",
        {
            "pending": pending,
            "others": others,
            "hidden": hidden,
            "values": values or {"email": "", "role": "member", "send_email": True},
            "error": error,
            "can_email": can_email,
            "roles": ROLES,
            "shown_link": take_link(request, ctx, "invite"),
            "base_url_set": bool(ctx.settings.web.base_url),
            "admin_tab": "invites",
            "nav": "admin",
            "page_title": "Invites · Admin",
        },
        status_code=status_code,
    )


@router.get("/invites")
def show_invites(request: Request, user: auth.Admin, ctx: auth.Ctx) -> Response:
    return invites_page(request, ctx)


@router.post("/invites")
def create_invite(request: Request, user: auth.Admin, form: auth.SignedForm, ctx: auth.Ctx) -> Response:
    email = str(form.get("email") or "").strip()[:MAX_EMAIL_LENGTH]
    role = str(form.get("role") or "member")
    wants_email = bool(form.get("send_email"))
    values = {"email": email, "role": role, "send_email": wants_email}
    if role not in ROLES:
        return invites_page(request, ctx, values=values, error="Choose admin or member.", status_code=400)
    try:
        token = ctx.accounts.create_invite(created_by=user.id, email=email or None, role=role)
    except AccountError as exc:
        return invites_page(request, ctx, values=values, error=str(exc), status_code=400)
    invite = ctx.accounts.get_invite(token)
    if invite is None:  # can't happen for a new invite; never show a dead link
        return redirect(request, "/admin/invites", "The invite couldn't be created. Try again.", kind="error")
    link = absolute_link(request, ctx, f"/invite/{token}")
    emailed = False
    if wants_email and invite.email and email_ready(ctx.settings):
        emailed = _email_invite(request, ctx, invite, link, user)
    keep_link(
        request,
        ctx,
        OneTimeLink(
            kind="invite",
            url=link,
            expires=invite.expires,
            email=invite.email,
            role=invite.role,
            emailed=emailed,
        ),
    )
    who = invite.email or "anyone with the link"
    sent = f" and emailed to {invite.email}" if emailed else ""
    return redirect(
        request,
        "/admin/invites#new-link",
        f"Invite for {who} created{sent}. Copy the link now: it is shown only once.",
    )


def _email_invite(request: Request, ctx: AppContext, invite: Invite, link: str, inviter: User) -> bool:
    """Email the invite; say on the next page why it wasn't sent. True when it was."""
    if ctx.accounts.rate_limited(f"invite-mail:{inviter.id}", limit=INVITE_MAIL_LIMIT, window=INVITE_MAIL_WINDOW):
        flash(
            request,
            f"The invite wasn't emailed: you sent {INVITE_MAIL_LIMIT} invite emails in the last hour. Copy the link "
            "below and send it yourself.",
            "error",
        )
        return False
    try:
        send_invite_email(ctx, invite, link, inviter)
    except (NotifyError, ConfigError) as exc:
        problem = one_line(scrub(str(exc), secrets_of(ctx.settings)), 300)
        log.warning("Couldn't email an invite: %s", problem)
        flash(request, f"The invite couldn't be emailed: {problem} Copy the link below and send it yourself.", "error")
        return False
    log.info("Account #%d emailed an invite.", inviter.id)
    return True


@router.post("/invites/revoke")
def revoke_invite(request: Request, user: auth.Admin, form: auth.SignedForm, ctx: auth.Ctx) -> Response:
    invite_hash = str(form.get("invite") or "")
    if not invite_hash or not ctx.accounts.revoke_invite(invite_hash):
        return redirect(
            request,
            "/admin/invites",
            "That invite can't be revoked: it was already used or revoked, or doesn't exist.",
            kind="error",
        )
    log.info("Account #%d revoked an invite.", user.id)
    return redirect(request, "/admin/invites", "Invite revoked: its link no longer works.")
