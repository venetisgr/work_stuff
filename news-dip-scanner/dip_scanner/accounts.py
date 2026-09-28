"""The website's accounts: users, invites, sessions, password links, login limits, per-user settings and the jobs of
"Analyse now", kept in the scanner's database (store.py creates the tables; this module reads and writes them).

Nobody signs up: an admin invites people (a single-use link, valid for 7 days), and `dip-scanner users add-admin`
makes the first admin with a link to set a password (valid for 48 hours). Passwords are hashed with scrypt (n=2**14,
r=8, p=1, a 16-byte salt) and stored as scrypt$n$r$p$salt$hash. Invite, password and session tokens are random
(32 bytes, URL-safe); only their SHA-256 is stored, and comparisons are constant-time (hmac.compare_digest). A
session lasts 30 days from the last visit; changing the password ends every session of the user, and so does
disabling the account. Tokens are never logged.

Every method takes the current time from the clock given to Accounts (tests pass a fixed one). Mistakes a person can
fix (a short password, a used invite, an unknown verdict) raise AccountError or SettingsError with a message meant
to be shown to them.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import logging
import re
import secrets
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import WEBHOOK_FORMATS, ConfigError, ScannerConfig, Settings, display_zone, zone_name
from .models import VERDICTS, from_iso, utc
from .netguard import Resolver, UnsafeURLError, check_public_url
from .notify import email_missing
from .store import Store
from .triage import normalise_ticker

log = logging.getLogger(__name__)

ROLES = ("admin", "member")
INVITE_LIFETIME = timedelta(days=7)
PASSWORD_TOKEN_LIFETIME = timedelta(hours=48)
PASSWORD_PURPOSES = ("setup", "reset")
SESSION_LIFETIME = timedelta(days=30)  # from the last visit
SESSION_TOUCH = timedelta(minutes=5)  # a session's last_seen and expiry are written at most this often
LOGIN_LIMIT = 10  # failed logins per IP address, and per email address, within LOGIN_WINDOW
LOGIN_WINDOW = timedelta(minutes=15)
MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 1024
MAX_NAME_LENGTH = 80
MAX_EMAIL_LENGTH = 254
MAX_WATCHLIST = 100
JOB_KINDS = ("analyze",)
JOB_STATUSES = ("queued", "running", "done", "failed")
DAY = timedelta(hours=24)
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1
_SALT_BYTES = 16
_KEY_BYTES = 32
_TOKEN_BYTES = 32
_SCRYPT_MAXMEM = 64 * 1024 * 1024
# Currencies a user can see "≈" amounts in: those Yahoo Finance quotes exchange rates for (EURUSD=X...).
CURRENCIES = (
    "EUR", "USD", "GBP", "CHF", "JPY", "CAD", "AUD", "NZD", "SEK", "NOK", "DKK", "ISK", "PLN", "CZK", "HUF", "RON",
    "BGN", "TRY", "ILS", "ZAR", "HKD", "SGD", "CNY", "INR", "KRW", "TWD", "THB", "MYR", "IDR", "PHP", "BRL", "MXN",
    "CLP", "COP", "AED", "SAR", "QAR",
)  # fmt: skip
_MINOR_CURRENCIES = {"GBX": "GBP", "ZAC": "ZAR", "ILA": "ILS"}
_EMAIL = re.compile(r"[^@\s<>()\[\],;:\"\\]+@(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}")
_TELEGRAM_CHAT = re.compile(r"-?\d{1,20}|@[A-Za-z][A-Za-z0-9_]{4,31}")
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("", "0", "false", "no", "off")


class AccountError(Exception):
    """Something a person asked for can't be done; the message says why, in words meant for them."""


class SettingsError(AccountError):
    """Settings that can't be saved: errors maps each wrong field to what is wrong with it."""

    def __init__(self, errors: Mapping[str, str]) -> None:
        self.errors = dict(errors)
        super().__init__(" ".join(self.errors.values()))


def _now() -> datetime:
    return datetime.now(UTC)


def _ts(dt: datetime) -> str:
    """The store's fixed-width UTC timestamp text."""
    return utc(dt).isoformat(timespec="microseconds")


def _time(value: str | None) -> datetime | None:
    return from_iso(value) if value else None


# --- passwords and tokens ------------------------------------------------------------------------------------------


def _password_bytes(password: str) -> bytes:
    # NFC: the same password typed on a phone and on a computer gives the same bytes (composed accents, e.g. Greek).
    return unicodedata.normalize("NFC", password).encode("utf-8")


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text + "=" * (-len(text) % 4), validate=True)


def hash_password(password: str) -> str:
    """scrypt$n$r$p$salt$hash of a password (a new random salt each time)."""
    salt = secrets.token_bytes(_SALT_BYTES)
    key = hashlib.scrypt(
        _password_bytes(password),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        maxmem=_SCRYPT_MAXMEM,
        dklen=_KEY_BYTES,
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(key)}"


def check_password(stored: str | None, password: str) -> bool:
    """Whether password matches a stored scrypt$... hash (constant-time comparison; False for anything malformed)."""
    if not stored or not isinstance(password, str):
        return False
    try:
        scheme, n, r, p, salt, key = stored.split("$")
        n, r, p = int(n), int(r), int(p)
        expected = _unb64(key)
        if scheme != "scrypt" or not (2 <= n <= 2**20 and 1 <= r <= 32 and 1 <= p <= 16) or not expected:
            return False
        actual = hashlib.scrypt(
            _password_bytes(password),
            salt=_unb64(salt),
            n=n,
            r=r,
            p=p,
            maxmem=_SCRYPT_MAXMEM,
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


_dummy_hash: str | None = None


def _waste_time(password: str) -> None:
    """Hash like a real login would, so an unknown email takes as long as a wrong password."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password(secrets.token_urlsafe(16))
    check_password(_dummy_hash, password)


def new_token() -> str:
    """A random URL-safe token (32 bytes of entropy): what an invite, password or session link carries."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def token_hash(token: str) -> str:
    """The SHA-256 (hex) of a token, which is all the database keeps of it."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def same_token(expected: str | None, given: str | None) -> bool:
    """A constant-time comparison of two tokens (CSRF tokens, hashes); False when either is missing."""
    if not expected or not isinstance(given, str) or not given:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), given.encode("utf-8"))


def check_new_password(password: object, *, email: str | None = None) -> str:
    """The password when it is acceptable (at least 10 characters, not the email address); AccountError otherwise."""
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise AccountError(f"Choose a password of at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise AccountError(f"That password is too long (over {MAX_PASSWORD_LENGTH} characters).")
    if not password.strip():
        raise AccountError("The password can't be only spaces.")
    if email and password.strip().casefold() == email.strip().casefold():
        raise AccountError("The password can't be your email address.")
    return password


def normalise_email(text: object) -> str:
    """An email address, trimmed and in lowercase; AccountError when it doesn't look like one."""
    email = str(text or "").strip().lower()
    if not email:
        raise AccountError("Enter an email address.")
    if len(email) > MAX_EMAIL_LENGTH or not _EMAIL.fullmatch(email):
        raise AccountError(f"{email[:80]!r} isn't an email address.")
    return email


def clean_name(text: object) -> str:
    """A display name: control characters removed, spaces collapsed, at most 80 characters (may be empty)."""
    name = "".join(char for char in str(text or "") if unicodedata.category(char)[0] != "C")
    return " ".join(name.split())[:MAX_NAME_LENGTH]


# --- per-user settings ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class UserSettings:
    """What a website user chose: which ideas alert them, where, and how amounts and times are shown.

    Stored as JSON in users.settings, only what the user saved: the rest follows the defaults (scanner.toml's
    [alerts] and DISPLAY_TZ, see defaults()), so a changed scanner.toml reaches everyone who never changed them.
    """

    watchlist: tuple[str, ...] = ()  # Yahoo symbols written the way triage writes them (BRK.B -> BRK-B)
    min_score: float = 65
    min_probability: int = 60
    verdicts: tuple[str, ...] = ("temporary_fear", "mixed")
    only_watchlist: bool = False  # alert only on watchlist tickers
    thesis_changes: bool = True  # "thesis change" notices for ideas that alerted this user
    email_alerts: bool = False  # to the account's email, through the server's SMTP settings
    telegram_chat_id: str | None = None  # through the server's Telegram bot
    webhook_url: str | None = None  # Slack, Discord or generic JSON (checked by netguard)
    webhook_format: str = "slack"
    currency: str | None = None  # "≈" amounts in this currency; None: the trading currency only
    timezone: str = "UTC"  # IANA name

    @classmethod
    def defaults(cls, config: ScannerConfig, settings: Settings) -> UserSettings:
        """A new user's settings: scanner.toml's [alerts] rules and DISPLAY_TZ."""
        alerts = config.alerts
        return cls(
            min_score=float(alerts.min_score),
            min_probability=int(alerts.min_probability),
            verdicts=tuple(alerts.verdicts),
            timezone=zone_name(settings.display_tz),
        )

    @property
    def has_channel(self) -> bool:
        """Whether the user asked for alerts anywhere (email, Telegram or a webhook)."""
        return bool(self.email_alerts or self.telegram_chat_id or self.webhook_url)

    def to_dict(self) -> dict[str, Any]:
        """A JSON-safe dict (tuples as lists)."""
        values = {f.name: getattr(self, f.name) for f in fields(self)}
        return {name: list(value) if isinstance(value, tuple) else value for name, value in values.items()}

    @classmethod
    def from_dict(cls, data: object, defaults: UserSettings | None = None) -> UserSettings:
        """Settings from stored JSON: unknown keys are ignored, and a missing or damaged value keeps the default (the
        values were checked when they were saved; nothing is looked up on the network here)."""
        base = defaults or cls()
        if not isinstance(data, Mapping):
            return base
        values: dict[str, Any] = {}
        for name, value in data.items():
            if name not in _FIELDS:
                continue
            try:
                values[name] = _stored_value(name, value)
            except (TypeError, ValueError, ConfigError):
                log.debug("Ignoring a stored setting %s=%r", name, value)
        return replace(base, **values)


_FIELDS = {f.name for f in fields(UserSettings)}


def _stored_value(name: str, value: Any) -> Any:
    if name in ("watchlist", "verdicts"):
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise TypeError(name)
        items = tuple(dict.fromkeys(value))
        if name == "verdicts" and not set(items) <= set(VERDICTS):
            raise ValueError(name)
        return items
    if name in ("min_score", "min_probability"):
        if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 100:
            raise ValueError(name)
        return float(value) if name == "min_score" else int(value)
    if name in ("only_watchlist", "thesis_changes", "email_alerts"):
        if not isinstance(value, bool):
            raise TypeError(name)
        return value
    if name == "webhook_format":
        if value not in WEBHOOK_FORMATS:
            raise ValueError(name)
        return value
    if name == "timezone":
        return zone_name(display_zone(str(value)))
    if name in ("telegram_chat_id", "webhook_url", "currency"):
        if value is not None and not isinstance(value, str):
            raise TypeError(name)
        return value or None
    raise TypeError(name)  # a field without a loader: add one here


def validate_settings(
    data: Mapping[str, Any],
    *,
    current: UserSettings,
    settings: Settings,
    resolver: Resolver | None = None,
) -> UserSettings:
    """current with the values in data (a submitted form, or a dict of typed values) checked and applied; keys not
    in data keep their current value, unknown keys are ignored. SettingsError names every wrong field.

    Accepted forms: watchlist as a list or as text separated by commas, spaces or new lines; verdicts as a list or
    comma-separated text; numbers as numbers or text ("65", "65.5" or "65,5"); booleans as True/False or a form value
    ("on", "true", "1", "yes" / "", "off", "false", "0", "no"); "" for "none" in telegram_chat_id, webhook_url and
    currency. The webhook URL is checked with netguard (its host is looked up with resolver), email alerts need the
    server's SMTP settings and Telegram the server's bot.
    """
    errors: dict[str, str] = {}
    values: dict[str, Any] = {}

    def take(name: str, parse: Callable[[Any], Any]) -> None:
        if name not in data:
            return
        try:
            values[name] = parse(data[name])
        except AccountError as exc:
            errors[name] = str(exc)
        except (TypeError, ValueError):  # a value of the wrong type altogether (not from a form)
            errors[name] = f"The {name.replace('_', ' ')} setting isn't valid."

    take("watchlist", _parse_watchlist)
    take("min_score", lambda value: _number(value, "The minimum score", whole=False))
    take("min_probability", lambda value: _number(value, "The minimum chance", whole=True))
    take("verdicts", _parse_verdicts)
    for name in ("only_watchlist", "thesis_changes", "email_alerts"):
        take(name, lambda value, name=name: _flag(value, name))
    take("telegram_chat_id", _parse_chat_id)
    take("webhook_url", lambda value: _parse_webhook(value, resolver))
    take("webhook_format", _parse_format)
    take("currency", _parse_currency)
    take("timezone", _parse_timezone)

    result = replace(current, **values)
    # A channel the server can't serve is refused when it is asked for (not when other settings are saved).
    if values.get("email_alerts") and not email_ready(settings):
        errors["email_alerts"] = "Email alerts aren't available: the server has no email (SMTP) settings."
    if values.get("telegram_chat_id") and not telegram_ready(settings):
        errors["telegram_chat_id"] = "Telegram alerts aren't available: the server has no Telegram bot."
    if errors:
        raise SettingsError(errors)
    return result


def email_ready(settings: Settings) -> bool:
    """Whether the server can email users their alerts (SMTP_HOST and a sender are set)."""
    return not email_missing(settings.notify, recipients=False)


def telegram_ready(settings: Settings) -> bool:
    """Whether the server can send users Telegram messages (TELEGRAM_BOT_TOKEN is set)."""
    return bool(settings.notify.telegram_bot_token)


def _parse_watchlist(value: Any) -> tuple[str, ...]:
    items = re.split(r"[\s,;]+", value) if isinstance(value, str) else list(value or [])
    symbols, wrong = [], []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            continue
        symbol = normalise_ticker(item)
        (symbols if symbol else wrong).append(symbol or item.strip()[:20])
    if wrong:
        shown = ", ".join(wrong[:5]) + (" ..." if len(wrong) > 5 else "")
        raise AccountError(f"Not a company's Yahoo Finance symbol: {shown}. Write them like AMD, SAP.DE or ALWN.AT.")
    symbols = list(dict.fromkeys(symbols))
    if len(symbols) > MAX_WATCHLIST:
        raise AccountError(f"The watchlist can hold at most {MAX_WATCHLIST} symbols (got {len(symbols)}).")
    return tuple(symbols)


def _number(value: Any, label: str, *, whole: bool) -> float | int:
    if isinstance(value, bool):
        number = None
    elif isinstance(value, int | float):
        number = float(value)
    else:
        try:
            number = float(str(value).strip().replace(",", "."))
        except ValueError:
            number = None
    if number is None or not 0 <= number <= 100 or (whole and not number.is_integer()):
        kind = "a whole number" if whole else "a number"
        raise AccountError(f"{label} must be {kind} from 0 to 100.")
    return int(number) if whole else number


def _parse_verdicts(value: Any) -> tuple[str, ...]:
    items = value.split(",") if isinstance(value, str) else list(value or [])
    verdicts = tuple(dict.fromkeys(str(item).strip().lower() for item in items if str(item).strip()))
    wrong = [item for item in verdicts if item not in VERDICTS]
    if wrong:
        raise AccountError(f"Unknown verdict {', '.join(wrong)}; choose from {', '.join(VERDICTS)}.")
    if not verdicts:
        raise AccountError("Choose at least one verdict to be alerted about.")
    return verdicts


def _flag(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value if value is not None else "").strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise AccountError(f"{name.replace('_', ' ').capitalize()} must be on or off.")


def _parse_chat_id(value: Any) -> str | None:
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    if not _TELEGRAM_CHAT.fullmatch(text):
        raise AccountError(
            "A Telegram chat id is a number (negative for groups, e.g. -100123) or a public channel's @name."
        )
    return text


def _parse_webhook(value: Any, resolver: Resolver | None) -> str | None:
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    try:
        return check_public_url(text, resolver=resolver)
    except UnsafeURLError as exc:
        raise AccountError(str(exc)) from None


def _parse_format(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text not in WEBHOOK_FORMATS:
        raise AccountError(f"The webhook format must be one of {', '.join(WEBHOOK_FORMATS)}.")
    return text


def _parse_currency(value: Any) -> str | None:
    code = str(value if value is not None else "").strip().upper()
    if not code:
        return None
    if code in _MINOR_CURRENCIES:
        raise AccountError(f"{code} is a hundredth of a currency; choose {_MINOR_CURRENCIES[code]}.")
    if code not in CURRENCIES:
        raise AccountError(f"Choose a currency from the list (e.g. EUR, USD, GBP); {code[:10]!r} isn't one of them.")
    return code


def _parse_timezone(value: Any) -> str:
    text = str(value or "").strip()
    try:
        return zone_name(display_zone(text or None))
    except ConfigError:
        raise AccountError(
            f"{text[:60]!r} isn't a time zone name; choose one like Europe/Athens or America/New_York."
        ) from None


# --- records -------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class User:
    id: int
    email: str
    name: str
    role: str  # ROLES
    created: datetime
    last_login: datetime | None
    disabled: bool
    has_password: bool  # False until the setup link has been used
    settings: UserSettings
    alerts_since: datetime | None  # alerts count from here (when the first channel was set up)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def label(self) -> str:
        """The name, or the email address when there is none."""
        return self.name or self.email

    @property
    def recipient_key(self) -> str:
        """The key of this user's alert state (alert_deliveries.recipient)."""
        return f"user:{self.id}"


@dataclass(frozen=True)
class Invite:
    token_hash: str  # identifies the invite in admin pages (revoke_invite); not the token in the link
    email: str | None
    role: str
    created_by: int | None
    created: datetime
    expires: datetime
    used_by: int | None
    used_at: datetime | None
    revoked: bool

    def status(self, now: datetime) -> str:
        """ "used", "revoked", "expired" or "pending"."""
        if self.used_by is not None:
            return "used"
        if self.revoked:
            return "revoked"
        return "expired" if utc(now) >= self.expires else "pending"


@dataclass(frozen=True)
class Session:
    token_hash: str
    user: User
    csrf: str  # the per-session token every state-changing form carries (check with same_token)
    created: datetime
    expires: datetime
    last_seen: datetime
    ip: str | None
    user_agent: str | None


@dataclass(frozen=True)
class PasswordToken:
    token_hash: str
    user: User
    purpose: str  # "setup" (first password) or "reset"
    created: datetime
    expires: datetime


@dataclass(frozen=True)
class Job:
    id: int
    user_id: int
    kind: str  # JOB_KINDS
    ticker: str
    status: str  # JOB_STATUSES
    created: datetime
    finished: datetime | None
    opportunity_id: int | None
    error: str | None

    @property
    def done(self) -> bool:
        return self.status in ("done", "failed")


_USER_COLUMNS = (
    "id, email, name, role, created, last_login, disabled, password_hash IS NOT NULL AS has_password, settings, "
    "alerts_since"
)


# --- the accounts --------------------------------------------------------------------------------------------------


class Accounts:
    """Users, invites, sessions, password links, rate limits and jobs in a Store's database.

    defaults are a new user's settings (UserSettings.defaults(config, settings)); clock gives the current time.
    """

    def __init__(
        self,
        store: Store,
        *,
        defaults: UserSettings | None = None,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.store = store
        self.defaults = defaults or UserSettings()
        self._clock = clock

    def now(self) -> datetime:
        return utc(self._clock())

    # --- users ---

    def create_user(self, email: str, *, name: str = "", role: str = "member", password: str | None = None) -> User:
        """A new user (AccountError when the email is taken or something is invalid). Without a password the user
        can't sign in until they use a setup link (create_password_token)."""
        email = normalise_email(email)
        _check_role(role)
        hashed = hash_password(check_new_password(password, email=email)) if password is not None else None
        now = _ts(self.now())
        with self.store.transaction() as conn:
            if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
                raise AccountError(f"There is already an account for {email}.")
            cursor = conn.execute(
                "INSERT INTO users (email, name, password_hash, role, created, settings, alerts_since) "
                "VALUES (?, ?, ?, ?, ?, '{}', ?)",
                (email, clean_name(name), hashed, role, now, now),
            )
            user_id = int(cursor.lastrowid)
        log.info("Created the %s account #%d.", role, user_id)
        return self._user(user_id)

    def get_user(self, user_id: int) -> User | None:
        rows = self.store.query(f"SELECT {_USER_COLUMNS} FROM users WHERE id = ?", (int(user_id),))
        return self._to_user(rows[0]) if rows else None

    def get_user_by_email(self, email: str) -> User | None:
        try:
            address = normalise_email(email)
        except AccountError:
            return None
        rows = self.store.query(f"SELECT {_USER_COLUMNS} FROM users WHERE email = ?", (address,))
        return self._to_user(rows[0]) if rows else None

    def list_users(self) -> list[User]:
        """Every user, oldest first."""
        return [self._to_user(row) for row in self.store.query(f"SELECT {_USER_COLUMNS} FROM users ORDER BY id")]

    def active_users(self) -> list[User]:
        """The users who can sign in (not disabled, with a password), oldest first."""
        return [user for user in self.list_users() if not user.disabled and user.has_password]

    def authenticate(self, email: str, password: str) -> User | None:
        """The user when email and password match an enabled account (their last login is updated), else None. An
        unknown email takes as long as a wrong password. Rate limits are the caller's (too_many_attempts)."""
        rows = []
        with contextlib.suppress(AccountError):  # not an email address: no such user
            address = normalise_email(email)
            rows = self.store.query("SELECT id, password_hash, disabled FROM users WHERE email = ?", (address,))
        if not rows or not rows[0]["password_hash"]:
            _waste_time(str(password))
            return None
        if not check_password(rows[0]["password_hash"], str(password)) or rows[0]["disabled"]:
            return None
        with self.store.transaction() as conn:
            conn.execute("UPDATE users SET last_login = ? WHERE id = ?", (_ts(self.now()), rows[0]["id"]))
        return self._user(rows[0]["id"])

    def verify_password(self, user_id: int, password: str) -> bool:
        """Whether password is the user's current one (e.g. before changing it)."""
        rows = self.store.query("SELECT password_hash FROM users WHERE id = ?", (int(user_id),))
        return bool(rows) and check_password(rows[0]["password_hash"], str(password))

    def set_password(self, user_id: int, password: str) -> User:
        """Set a new password (checked with check_new_password); every session of the user ends and every unused
        password link of theirs stops working."""
        user = self._user(user_id)
        hashed = hash_password(check_new_password(password, email=user.email))
        now = _ts(self.now())
        with self.store.transaction() as conn:
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hashed, user.id))
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user.id,))
            conn.execute("UPDATE password_tokens SET used_at = ? WHERE user_id = ? AND used_at IS NULL", (now, user.id))
        log.info("The password of account #%d was changed; its sessions were ended.", user.id)
        return self._user(user.id)

    def set_name(self, user_id: int, name: str) -> User:
        with self.store.transaction() as conn:
            conn.execute("UPDATE users SET name = ? WHERE id = ?", (clean_name(name), int(user_id)))
        return self._user(user_id)

    def set_role(self, user_id: int, role: str) -> User:
        """Make a user an admin or a member. The last admin who can sign in can't be made a member."""
        _check_role(role)
        user = self._user(user_id)
        if user.role == "admin" and role != "admin":
            self._guard_last_admin(user, "made a member")
        with self.store.transaction() as conn:
            conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user.id))
        log.info("Account #%d is now a%s %s.", user.id, "n" if role == "admin" else "", role)
        return self._user(user.id)

    def set_disabled(self, user_id: int, disabled: bool) -> User:
        """Disable a user (every session ends at once and they can't sign in) or enable them again. The last admin
        who can sign in can't be disabled."""
        user = self._user(user_id)
        if disabled and user.role == "admin":
            self._guard_last_admin(user, "disabled")
        with self.store.transaction() as conn:
            conn.execute("UPDATE users SET disabled = ? WHERE id = ?", (int(bool(disabled)), user.id))
            if disabled:
                conn.execute("DELETE FROM sessions WHERE user_id = ?", (user.id,))
        log.info("Account #%d was %s.", user.id, "disabled" if disabled else "enabled")
        return self._user(user.id)

    def update_settings(self, user_id: int, settings: UserSettings) -> User:
        """Save a user's settings (already checked with validate_settings). When they set up their first alert
        channel, their alerts start now: ideas from before aren't sent to them late."""
        user = self._user(user_id)
        now = _ts(self.now())
        with self.store.transaction() as conn:
            conn.execute("UPDATE users SET settings = ? WHERE id = ?", (json.dumps(settings.to_dict()), user.id))
            if settings.has_channel and not user.settings.has_channel:
                conn.execute("UPDATE users SET alerts_since = ? WHERE id = ?", (now, user.id))
        return self._user(user.id)

    def _guard_last_admin(self, user: User, what: str) -> None:
        if user.disabled or not user.has_password:
            return
        others = self.store.query(
            "SELECT COUNT(*) FROM users WHERE role = 'admin' AND disabled = 0 AND password_hash IS NOT NULL "
            "AND id != ?",
            (user.id,),
        )[0][0]
        if not others:
            raise AccountError(
                f"{user.email} is the only admin, so they can't be {what}; make someone else an admin first."
            )

    def _user(self, user_id: int) -> User:
        user = self.get_user(user_id)
        if user is None:
            raise AccountError("That account doesn't exist (any more).")
        return user

    def _to_user(self, row) -> User:
        try:
            stored = json.loads(row["settings"] or "{}")
        except ValueError:
            stored = {}
        return User(
            id=row["id"],
            email=row["email"],
            name=row["name"] or "",
            role=row["role"],
            created=from_iso(row["created"]),
            last_login=_time(row["last_login"]),
            disabled=bool(row["disabled"]),
            has_password=bool(row["has_password"]),
            settings=UserSettings.from_dict(stored, self.defaults),
            alerts_since=_time(row["alerts_since"]),
        )

    # --- invites ---

    def create_invite(self, *, created_by: int | None, email: str | None = None, role: str = "member") -> str:
        """A single-use invite, valid for 7 days; returns the token for the link (/invite/<token>), which is not
        stored. With an email, only that address can use it."""
        _check_role(role)
        address = normalise_email(email) if email else None
        if address and self.get_user_by_email(address) is not None:
            raise AccountError(f"There is already an account for {address}.")
        token = new_token()
        now = self.now()
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO invites (token_hash, email, role, created_by, created, expires) VALUES (?, ?, ?, ?, ?, ?)",
                (token_hash(token), address, role, created_by, _ts(now), _ts(now + INVITE_LIFETIME)),
            )
        log.info(
            "Created an invite for %s%s.",
            "an admin" if role == "admin" else "a member",
            " (one address)" if address else "",
        )
        return token

    def get_invite(self, token: str) -> Invite | None:
        """The invite a link's token belongs to, while it can still be used (else None)."""
        invite = self._invite(token)
        return invite if invite is not None and invite.status(self.now()) == "pending" else None

    def list_invites(self, *, pending_only: bool = True) -> list[Invite]:
        """Invites, newest first (only the ones that can still be used, unless pending_only=False)."""
        rows = self.store.query("SELECT * FROM invites ORDER BY created DESC")
        invites = [_to_invite(row) for row in rows]
        now = self.now()
        return [invite for invite in invites if not pending_only or invite.status(now) == "pending"]

    def revoke_invite(self, invite_hash: str) -> bool:
        """Stop an unused invite from working (by Invite.token_hash); False when there was none to revoke."""
        with self.store.transaction() as conn:
            done = conn.execute(
                "UPDATE invites SET revoked = 1 WHERE token_hash = ? AND used_by IS NULL AND revoked = 0",
                (str(invite_hash),),
            ).rowcount
        return bool(done)

    def accept_invite(self, token: str, *, name: str, password: str, email: str | None = None) -> User:
        """Create the account an invite is for (email: the invite's, else the one given) and use up the invite."""
        invite = self.get_invite(token)
        if invite is None:
            raise AccountError("This invite link has expired or was already used. Ask for a new one.")
        address = invite.email or normalise_email(email)
        check_new_password(password, email=address)
        hashed = hash_password(password)
        now = _ts(self.now())
        with self.store.transaction() as conn:
            if conn.execute("SELECT 1 FROM users WHERE email = ?", (address,)).fetchone():
                raise AccountError(f"There is already an account for {address}; sign in instead.")
            cursor = conn.execute(
                "INSERT INTO users (email, name, password_hash, role, created, settings, alerts_since) "
                "VALUES (?, ?, ?, ?, ?, '{}', ?)",
                (address, clean_name(name), hashed, invite.role, now, now),
            )
            user_id = int(cursor.lastrowid)
            used = conn.execute(
                "UPDATE invites SET used_by = ?, used_at = ? WHERE token_hash = ? AND used_by IS NULL AND revoked = 0 "
                "AND expires > ?",
                (user_id, now, invite.token_hash, now),
            ).rowcount
            if used != 1:  # someone used it a moment ago: the whole transaction is rolled back
                raise AccountError("This invite link has expired or was already used. Ask for a new one.")
        log.info("Invite used: created the %s account #%d.", invite.role, user_id)
        return self._user(user_id)

    def _invite(self, token: str) -> Invite | None:
        if not isinstance(token, str) or not token:
            return None
        hashed = token_hash(token)
        rows = self.store.query("SELECT * FROM invites WHERE token_hash = ?", (hashed,))
        if not rows or not same_token(rows[0]["token_hash"], hashed):
            return None
        return _to_invite(rows[0])

    # --- sessions ---

    def create_session(
        self, user_id: int, *, ip: str | None = None, user_agent: str | None = None
    ) -> tuple[str, Session]:
        """Sign a user in: (the token for the session cookie, the session). The cookie's token is not stored."""
        user = self._user(user_id)
        if user.disabled:
            raise AccountError("This account is disabled.")
        token = new_token()
        now = self.now()
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO sessions (token_hash, user_id, csrf, created, expires, last_seen, ip, user_agent) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    token_hash(token),
                    user.id,
                    new_token(),
                    _ts(now),
                    _ts(now + SESSION_LIFETIME),
                    _ts(now),
                    (ip or None) and ip[:64],
                    (user_agent or None) and user_agent[:300],
                ),
            )
        session = self.get_session(token)
        assert session is not None
        return token, session

    def get_session(self, token: str | None) -> Session | None:
        """The session a cookie's token belongs to, or None when it is unknown, expired, or its user is disabled (the
        user's sessions then end). A session in use is extended to 30 days from now (written every few minutes)."""
        if not isinstance(token, str) or not token:
            return None
        hashed = token_hash(token)
        rows = self.store.query("SELECT * FROM sessions WHERE token_hash = ?", (hashed,))
        if not rows or not same_token(rows[0]["token_hash"], hashed):
            return None
        row = rows[0]
        now = self.now()
        user = self.get_user(row["user_id"])
        if user is None or user.disabled or from_iso(row["expires"]) <= now:
            with self.store.transaction() as conn:
                if user is not None and user.disabled:
                    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user.id,))
                else:
                    conn.execute("DELETE FROM sessions WHERE token_hash = ?", (hashed,))
            return None
        last_seen, expires = from_iso(row["last_seen"]), from_iso(row["expires"])
        if now - last_seen >= SESSION_TOUCH:
            last_seen, expires = now, now + SESSION_LIFETIME
            with self.store.transaction() as conn:
                conn.execute(
                    "UPDATE sessions SET last_seen = ?, expires = ? WHERE token_hash = ?",
                    (_ts(last_seen), _ts(expires), hashed),
                )
        return Session(
            token_hash=hashed,
            user=user,
            csrf=row["csrf"],
            created=from_iso(row["created"]),
            expires=expires,
            last_seen=last_seen,
            ip=row["ip"],
            user_agent=row["user_agent"],
        )

    def revoke_session(self, token: str | None) -> bool:
        """End the session a cookie's token belongs to (logout); False when there was none."""
        if not isinstance(token, str) or not token:
            return False
        with self.store.transaction() as conn:
            return bool(conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),)).rowcount)

    def revoke_sessions(self, user_id: int, *, keep: str | None = None) -> int:
        """End every session of a user, except the one whose cookie token is keep; returns how many ended."""
        with self.store.transaction() as conn:
            return conn.execute(
                "DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
                (int(user_id), token_hash(keep) if keep else ""),
            ).rowcount

    def list_sessions(self, user_id: int) -> list[Session]:
        """A user's sessions that haven't expired, most recently used first."""
        rows = self.store.query(
            "SELECT * FROM sessions WHERE user_id = ? AND expires > ? ORDER BY last_seen DESC",
            (int(user_id), _ts(self.now())),
        )
        user = self._user(user_id)
        return [
            Session(
                token_hash=row["token_hash"],
                user=user,
                csrf=row["csrf"],
                created=from_iso(row["created"]),
                expires=from_iso(row["expires"]),
                last_seen=from_iso(row["last_seen"]),
                ip=row["ip"],
                user_agent=row["user_agent"],
            )
            for row in rows
        ]

    # --- password links ---

    def create_password_token(self, user_id: int, purpose: str | None = None) -> str:
        """A single-use link token to set a password (/password/<token>), valid for 48 hours. purpose defaults to
        "setup" for a user without a password, else "reset"."""
        user = self._user(user_id)
        purpose = purpose or ("reset" if user.has_password else "setup")
        if purpose not in PASSWORD_PURPOSES:
            raise ValueError(f"Unknown purpose {purpose!r}; use one of {', '.join(PASSWORD_PURPOSES)}.")
        token = new_token()
        now = self.now()
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO password_tokens (token_hash, user_id, purpose, created, expires) VALUES (?, ?, ?, ?, ?)",
                (token_hash(token), user.id, purpose, _ts(now), _ts(now + PASSWORD_TOKEN_LIFETIME)),
            )
        log.info("Created a password %s link for account #%d.", purpose, user.id)
        return token

    def get_password_token(self, token: str) -> PasswordToken | None:
        """The password link a token belongs to while it can be used (unused, not expired, user enabled)."""
        if not isinstance(token, str) or not token:
            return None
        hashed = token_hash(token)
        rows = self.store.query("SELECT * FROM password_tokens WHERE token_hash = ?", (hashed,))
        if not rows or not same_token(rows[0]["token_hash"], hashed):
            return None
        row = rows[0]
        user = self.get_user(row["user_id"])
        if row["used_at"] or from_iso(row["expires"]) <= self.now() or user is None or user.disabled:
            return None
        return PasswordToken(
            token_hash=hashed,
            user=user,
            purpose=row["purpose"],
            created=from_iso(row["created"]),
            expires=from_iso(row["expires"]),
        )

    def use_password_token(self, token: str, password: str) -> User:
        """Set the password of the link's user and use the link up (set_password: every session ends)."""
        found = self.get_password_token(token)
        if found is None:
            raise AccountError("This password link has expired or was already used. Ask an admin for a new one.")
        check_new_password(password, email=found.user.email)
        with self.store.transaction() as conn:
            used = conn.execute(
                "UPDATE password_tokens SET used_at = ? WHERE token_hash = ? AND used_at IS NULL",
                (_ts(self.now()), found.token_hash),
            ).rowcount
        if used != 1:
            raise AccountError("This password link has expired or was already used. Ask an admin for a new one.")
        return self.set_password(found.user.id, password)

    # --- rate limits (login_attempts) ---

    @staticmethod
    def login_keys(ip: str | None, email: str | None) -> list[str]:
        """The rate-limit keys of a login attempt: per IP address and per email address."""
        keys = [f"ip:{ip}"] if ip else []
        if email and email.strip():
            keys.append(f"email:{email.strip().lower()[:MAX_EMAIL_LENGTH]}")
        return keys

    def record_login_failure(self, *keys: str) -> None:
        """Count a failed attempt under each key (e.g. login_keys(ip, email))."""
        now = _ts(self.now())
        with self.store.transaction() as conn:
            conn.executemany("INSERT INTO login_attempts (key, at) VALUES (?, ?)", [(key, now) for key in keys])
            conn.execute("DELETE FROM login_attempts WHERE at < ?", (_ts(self.now() - DAY),))

    def too_many_attempts(self, *keys: str, limit: int = LOGIN_LIMIT, window: timedelta = LOGIN_WINDOW) -> bool:
        """Whether any key has limit or more attempts within window (10 in 15 minutes for logins)."""
        since = _ts(self.now() - window)
        for key in keys:
            count = self.store.query("SELECT COUNT(*) FROM login_attempts WHERE key = ? AND at >= ?", (key, since))
            if count[0][0] >= limit:
                return True
        return False

    def clear_attempts(self, *keys: str) -> None:
        """Forget the attempts under these keys (e.g. an email's after a successful login)."""
        with self.store.transaction() as conn:
            conn.executemany("DELETE FROM login_attempts WHERE key = ?", [(key,) for key in keys])

    def rate_limited(self, key: str, *, limit: int, window: timedelta) -> bool:
        """For actions with a limit (test alerts, token pages): True when key already had limit attempts within
        window; otherwise the attempt is counted and False returned."""
        if self.too_many_attempts(key, limit=limit, window=window):
            return True
        self.record_login_failure(key)
        return False

    # --- jobs ("Analyse now") ---

    def create_job(self, user_id: int, ticker: str, *, kind: str = "analyze") -> Job:
        """A queued job for a user."""
        if kind not in JOB_KINDS:
            raise ValueError(f"Unknown job kind {kind!r}; use one of {', '.join(JOB_KINDS)}.")
        with self.store.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO jobs (user_id, kind, ticker, status, created) VALUES (?, ?, ?, 'queued', ?)",
                (int(user_id), kind, ticker.strip().upper()[:32], _ts(self.now())),
            )
            job_id = int(cursor.lastrowid)
        return self._job(job_id)

    def get_job(self, job_id: int) -> Job | None:
        rows = self.store.query("SELECT * FROM jobs WHERE id = ?", (int(job_id),))
        return _to_job(rows[0]) if rows else None

    def start_job(self, job_id: int) -> Job:
        """Mark a queued job as running."""
        with self.store.transaction() as conn:
            conn.execute("UPDATE jobs SET status = 'running' WHERE id = ? AND status = 'queued'", (int(job_id),))
        return self._job(job_id)

    def finish_job(self, job_id: int, *, opportunity_id: int | None = None, error: str | None = None) -> Job:
        """Mark a job done (with the opportunity it made) or failed (with an error for the user)."""
        status = "failed" if error else "done"
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE jobs SET status = ?, finished = ?, opportunity_id = ?, error = ? WHERE id = ?",
                (status, _ts(self.now()), opportunity_id, (error or None) and error[:500], int(job_id)),
            )
        return self._job(job_id)

    def list_jobs(self, *, user_id: int | None = None, limit: int = 20) -> list[Job]:
        """Jobs, newest first (optionally one user's)."""
        sql, params = "SELECT * FROM jobs", []
        if user_id is not None:
            sql += " WHERE user_id = ?"
            params.append(int(user_id))
        rows = self.store.query(sql + " ORDER BY created DESC, id DESC LIMIT ?", [*params, max(0, limit)])
        return [_to_job(row) for row in rows]

    def count_jobs(self, user_id: int, *, since: datetime | None = None) -> int:
        """How many jobs a user started since the given time (default: the last 24 hours), failed ones included."""
        since = since if since is not None else self.now() - DAY
        rows = self.store.query(
            "SELECT COUNT(*) FROM jobs WHERE user_id = ? AND created >= ?", (int(user_id), _ts(since))
        )
        return int(rows[0][0])

    def fail_unfinished_jobs(self, reason: str = "Interrupted by a restart of the website; start it again.") -> int:
        """Mark every queued or running job failed (a restarted website can't finish them); returns how many."""
        with self.store.transaction() as conn:
            return conn.execute(
                "UPDATE jobs SET status = 'failed', finished = ?, error = ? WHERE status IN ('queued', 'running')",
                (_ts(self.now()), reason),
            ).rowcount

    def _job(self, job_id: int) -> Job:
        job = self.get_job(job_id)
        if job is None:
            raise AccountError("That job doesn't exist (any more).")
        return job


def _check_role(role: str) -> None:
    if role not in ROLES:
        raise AccountError(f"The role must be one of {', '.join(ROLES)} (got {role!r}).")


def _to_invite(row) -> Invite:
    return Invite(
        token_hash=row["token_hash"],
        email=row["email"],
        role=row["role"],
        created_by=row["created_by"],
        created=from_iso(row["created"]),
        expires=from_iso(row["expires"]),
        used_by=row["used_by"],
        used_at=_time(row["used_at"]),
        revoked=bool(row["revoked"]),
    )


def _to_job(row) -> Job:
    return Job(
        id=row["id"],
        user_id=row["user_id"],
        kind=row["kind"],
        ticker=row["ticker"],
        status=row["status"],
        created=from_iso(row["created"]),
        finished=_time(row["finished"]),
        opportunity_id=row["opportunity_id"],
        error=row["error"],
    )
