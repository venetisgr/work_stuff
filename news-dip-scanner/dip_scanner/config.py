"""Settings: environment variables (usually from .env), the feed list in feeds.toml and thresholds in scanner.toml."""

from __future__ import annotations

import ipaddress
import os
import re
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from datetime import UTC, tzinfo
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from .models import DIRECTIONS, VERDICTS, Feed

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATABASE_NAME = "scanner.sqlite3"  # inside DATA_DIR, next to reports/ and cache/

LLM_PROVIDERS = ("openai", "azure", "anthropic")
_PROVIDER_ALIASES = {"foundry": "azure", "azure_foundry": "azure", "azure-foundry": "azure", "claude": "anthropic"}
WEBHOOK_FORMATS = ("slack", "discord", "generic")

# (triage model, analysis model) per provider. Azure has no default: deployment names are yours to choose.
DEFAULT_MODELS = {"openai": ("gpt-5-mini", "gpt-5"), "anthropic": ("claude-haiku-4-5", "claude-sonnet-5")}

# LLM_ANALYSIS_MODE: "single" asks one analysis model per dip (LLM_PROVIDER's); "debate" has two models (LLM_DEBATERS)
# argue it out, with a judge where they disagree (see debate.py).
ANALYSIS_MODES = ("single", "debate")
# The two debaters when LLM_DEBATERS isn't set: each provider's default analysis model.
DEFAULT_DEBATERS = (f"openai:{DEFAULT_MODELS['openai'][1]}", f"anthropic:{DEFAULT_MODELS['anthropic'][1]}")
JUDGE_ALTERNATE = "alternate"  # LLM_DEBATE_JUDGE default: the debaters' models take turns, by ticker and date
DEBATE_WHEN = ("disagree", "always")  # [debate] when
MAX_DEBATE_ROUNDS = 3

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


class ConfigError(Exception):
    """A setting is missing or wrong. The message says what to fix."""


# --- environment settings ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMSettings:
    provider: str = "openai"  # "openai" | "azure" | "anthropic" (LLM_PROVIDER)
    triage_model: str | None = None  # LLM_TRIAGE_MODEL (azure: deployment name)
    analysis_model: str | None = None  # LLM_ANALYSIS_MODEL (azure: deployment name)
    openai_api_key: str | None = None  # OPENAI_API_KEY
    openai_base_url: str | None = None  # OPENAI_BASE_URL (optional, OpenAI-compatible gateways)
    anthropic_api_key: str | None = None  # ANTHROPIC_API_KEY
    foundry_endpoint: str | None = None  # FOUNDRY_ENDPOINT: resource name or endpoint URL
    foundry_api_key: str | None = None  # FOUNDRY_API_KEY (empty -> Entra ID)
    foundry_deployment: str | None = None  # FOUNDRY_DEPLOYMENT: fallback for both azure models
    reasoning_effort: str | None = None  # LLM_REASONING_EFFORT: both models, unless one of the two below is set
    triage_reasoning_effort: str | None = None  # LLM_TRIAGE_REASONING_EFFORT
    analysis_reasoning_effort: str | None = None  # LLM_ANALYSIS_REASONING_EFFORT
    max_output_tokens: int | None = None  # LLM_MAX_OUTPUT_TOKENS
    analysis_mode: str = "single"  # LLM_ANALYSIS_MODE: "single" | "debate"
    # LLM_DEBATERS: the two debaters as "provider:model" (azure: "azure:<deployment>"), in the order given.
    debaters: tuple[str, str] = DEFAULT_DEBATERS
    # LLM_DEBATE_JUDGE: "alternate", or the "provider:model" that judges every debate (a bare provider in the
    # variable names the debater of that provider).
    debate_judge: str = JUDGE_ALTERNATE


@dataclass(frozen=True)
class NotifySettings:
    smtp_host: str | None = None  # SMTP_HOST
    smtp_port: int = 587  # SMTP_PORT (465 = implicit TLS)
    smtp_user: str | None = None  # SMTP_USER
    smtp_password: str | None = None  # SMTP_PASSWORD
    smtp_from: str | None = None  # SMTP_FROM
    smtp_starttls: bool = True  # SMTP_STARTTLS
    email_to: list[str] = field(default_factory=list)  # EMAIL_TO, comma or semicolon separated
    webhook_url: str | None = None  # WEBHOOK_URL
    webhook_format: str = "generic"  # WEBHOOK_FORMAT: "slack" | "discord" | "generic"
    telegram_bot_token: str | None = None  # TELEGRAM_BOT_TOKEN
    telegram_chat_id: str | None = None  # TELEGRAM_CHAT_ID


SECRET_KEY_MIN_LENGTH = 32
GENERATE_SECRET_KEY = 'python -c "import secrets; print(secrets.token_urlsafe(48))"'
PROXY_SECRET_MIN_LENGTH = 32
_ORIGIN_HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*")
_LABEL_PART = re.compile(r"[a-z0-9-]+")
_DEFAULT_PORTS = {"http": 80, "https": 443}


@dataclass(frozen=True)
class WebSettings:
    """The website (`dip-scanner serve`) and the account links the command line prints."""

    secret_key: str | None = None  # SECRET_KEY: required by `serve` only (see require_secret_key)
    base_url: str | None = None  # BASE_URL, e.g. https://my-dips.fly.dev (no trailing slash): invite and reset links
    cookie_secure: bool = True  # COOKIE_SECURE: the session cookie only travels over https (false for local http)
    analyze_limit_per_user: int = 5  # ANALYZE_LIMIT_PER_USER: manual analyses per member in 24 hours (admins: none)
    scanner_enabled: bool = True  # SCANNER_ENABLED: `serve` runs the scanner too (`serve --no-scanner` overrides)
    # PROXY_SECRET: set when a front door (the Next.js app on Vercel) is the only way in. Every request but /healthz
    # must then carry it in x-dip-proxy-secret, and only such requests may name the visitor's address
    # (x-dip-client-ip). The front door's DIP_PROXY_SECRET has the same value.
    proxy_secret: str | None = None
    # TRUSTED_ORIGINS: origins (scheme://host[:port]) whose form posts are accepted besides BASE_URL's, normalised,
    # and at most one pattern with a * for preview deployments (see origin_pattern).
    trusted_origins: tuple[str, ...] = ()

    def link(self, path: str) -> str:
        """An absolute link to a page of the website, e.g. link("/invite/abc") -> https://my-dips.fly.dev/invite/abc.

        Raises ConfigError when BASE_URL isn't set: a link without it would point nowhere.
        """
        if not self.base_url:
            raise ConfigError(
                "Set BASE_URL in .env (or the environment) to the website's address, e.g. https://my-dips.fly.dev, "
                "so the link points at it."
            )
        return f"{self.base_url}/{path.lstrip('/')}"

    def require_secret_key(self) -> str:
        """SECRET_KEY, which `serve` needs; a ConfigError with the command that generates one when it is missing or
        too short to be secret."""
        key = self.secret_key or ""
        if len(key) < SECRET_KEY_MIN_LENGTH:
            problem = "isn't set" if not key else f"is too short ({len(key)} characters)"
            raise ConfigError(
                f"SECRET_KEY {problem}: the website needs a random secret of at least {SECRET_KEY_MIN_LENGTH} "
                f"characters. Generate one with {GENERATE_SECRET_KEY} and set it in .env (on Fly.io: fly secrets set "
                "SECRET_KEY=...)."
            )
        return key

    def require_front_door(self) -> None:
        """With PROXY_SECRET, BASE_URL must be the front door's address: the refusal page and every link point
        there. A ConfigError says so when it is missing."""
        if self.proxy_secret and not self.base_url:
            raise ConfigError(
                "PROXY_SECRET is set, so the website is only reachable through its front door: set BASE_URL to the "
                "front door's address (the Vercel domain, e.g. https://dips.example.com)."
            )


@dataclass(frozen=True)
class Settings:
    llm: LLMSettings = field(default_factory=LLMSettings)
    notify: NotifySettings = field(default_factory=NotifySettings)
    sec_user_agent: str | None = None  # SEC_USER_AGENT, e.g. "Jane Doe jane@example.com"; None -> fundamentals off
    data_dir: Path = field(default_factory=lambda: Path.cwd() / "data")  # DATA_DIR: database, reports/, cache/
    # DISPLAY_TZ, e.g. Europe/Athens: the time zone of every time people read (reports, alerts, summaries, notes and
    # the log). Everything is still stored and compared in UTC.
    display_tz: tzinfo = UTC
    web: WebSettings = field(default_factory=WebSettings)


def load_settings(env: Mapping[str, str] | None = None, *, problems: list[ConfigError] | None = None) -> Settings:
    """Read settings from the environment. Missing values are fine here; they're reported when they're needed.

    Values that are set but wrong (an unknown LLM_PROVIDER, a port that isn't a number...) raise ConfigError. Given a
    problems list, an unknown DISPLAY_TZ is added to it instead (and UTC used): the alert channels don't depend on
    it, so the command line can still send `run` and `watch`'s "dip-scanner stopped" notice about it.
    """
    env = os.environ if env is None else env

    def get(name: str, default: str | None = None) -> str | None:
        value = (env.get(name) or "").strip()
        return value or default

    provider = (get("LLM_PROVIDER") or "openai").lower()
    provider = _PROVIDER_ALIASES.get(provider, provider)
    if provider not in LLM_PROVIDERS:
        raise ConfigError(f"LLM_PROVIDER must be one of {', '.join(LLM_PROVIDERS)} (got {get('LLM_PROVIDER')!r}).")

    webhook_format = (get("WEBHOOK_FORMAT") or "generic").lower()
    if webhook_format not in WEBHOOK_FORMATS:
        raise ConfigError(
            f"WEBHOOK_FORMAT must be one of {', '.join(WEBHOOK_FORMATS)} (got {get('WEBHOOK_FORMAT')!r})."
        )

    analysis_mode = (get("LLM_ANALYSIS_MODE") or "single").lower()
    if analysis_mode not in ANALYSIS_MODES:
        raise ConfigError(
            f"LLM_ANALYSIS_MODE must be one of {', '.join(ANALYSIS_MODES)} (got {get('LLM_ANALYSIS_MODE')!r})."
        )
    debaters = parse_debaters(get("LLM_DEBATERS"))
    judge = parse_judge(get("LLM_DEBATE_JUDGE"), debaters)

    smtp_port = _positive_int(get("SMTP_PORT"), "SMTP_PORT")
    if smtp_port is not None and smtp_port > 65535:
        raise ConfigError(f"SMTP_PORT must be a port number between 1 and 65535 (got {smtp_port}).")

    try:
        display_tz = display_zone(get("DISPLAY_TZ"))
    except ConfigError as exc:
        if problems is None:
            raise
        problems.append(exc)
        display_tz = UTC

    return Settings(
        llm=LLMSettings(
            provider=provider,
            triage_model=get("LLM_TRIAGE_MODEL"),
            analysis_model=get("LLM_ANALYSIS_MODEL"),
            openai_api_key=get("OPENAI_API_KEY"),
            openai_base_url=get("OPENAI_BASE_URL"),
            anthropic_api_key=get("ANTHROPIC_API_KEY"),
            foundry_endpoint=get("FOUNDRY_ENDPOINT"),
            foundry_api_key=get("FOUNDRY_API_KEY"),
            foundry_deployment=get("FOUNDRY_DEPLOYMENT"),
            reasoning_effort=_lower(get("LLM_REASONING_EFFORT")),
            triage_reasoning_effort=_lower(get("LLM_TRIAGE_REASONING_EFFORT")),
            analysis_reasoning_effort=_lower(get("LLM_ANALYSIS_REASONING_EFFORT")),
            max_output_tokens=_positive_int(get("LLM_MAX_OUTPUT_TOKENS"), "LLM_MAX_OUTPUT_TOKENS"),
            analysis_mode=analysis_mode,
            debaters=debaters,
            debate_judge=judge,
        ),
        notify=NotifySettings(
            smtp_host=get("SMTP_HOST"),
            smtp_port=smtp_port or 587,
            smtp_user=get("SMTP_USER"),
            smtp_password=get("SMTP_PASSWORD"),
            smtp_from=get("SMTP_FROM"),
            smtp_starttls=_bool(get("SMTP_STARTTLS"), "SMTP_STARTTLS", default=True),
            email_to=_address_list(get("EMAIL_TO")),
            webhook_url=get("WEBHOOK_URL"),
            webhook_format=webhook_format,
            telegram_bot_token=get("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=get("TELEGRAM_CHAT_ID"),
        ),
        sec_user_agent=get("SEC_USER_AGENT"),
        data_dir=_data_dir(get("DATA_DIR")),
        display_tz=display_tz,
        web=WebSettings(
            secret_key=get("SECRET_KEY"),
            base_url=_base_url(get("BASE_URL")),
            cookie_secure=_bool(get("COOKIE_SECURE"), "COOKIE_SECURE", default=True),
            analyze_limit_per_user=_whole_number(get("ANALYZE_LIMIT_PER_USER"), "ANALYZE_LIMIT_PER_USER", default=5),
            scanner_enabled=_bool(get("SCANNER_ENABLED"), "SCANNER_ENABLED", default=True),
            proxy_secret=_proxy_secret(get("PROXY_SECRET")),
            trusted_origins=parse_trusted_origins(get("TRUSTED_ORIGINS")),
        ),
    )


def model_entry(value: str, setting: str) -> tuple[str, str]:
    """(provider, model) of a "provider:model" entry of LLM_DEBATERS or LLM_DEBATE_JUDGE: "openai:gpt-5" ->
    ("openai", "gpt-5"); the provider's case and aliases are forgiven ("Claude:claude-sonnet-5" -> anthropic)."""
    provider, colon, model = value.strip().partition(":")
    provider = provider.strip().lower()
    provider = _PROVIDER_ALIASES.get(provider, provider)
    model = model.strip()
    if not colon or provider not in LLM_PROVIDERS or not model or any(char.isspace() for char in model):
        raise ConfigError(
            f"{setting} entries are provider:model, with a provider from {', '.join(LLM_PROVIDERS)}, e.g. "
            f"{DEFAULT_DEBATERS[0]} or azure:my-deployment (got {value.strip()!r})."
        )
    return provider, model


def parse_debaters(value: str | None) -> tuple[str, str]:
    """LLM_DEBATERS: exactly two different "provider:model" entries, comma-separated; the defaults when unset."""
    if value is None:
        return DEFAULT_DEBATERS
    entries = [entry for entry in value.split(",") if entry.strip()]
    if len(entries) != 2:
        raise ConfigError(
            f"LLM_DEBATERS must name exactly two models, comma-separated, e.g. {','.join(DEFAULT_DEBATERS)} (got "
            f"{value!r})."
        )
    first, second = (":".join(model_entry(entry, "LLM_DEBATERS")) for entry in entries)
    if first == second:
        raise ConfigError(f"LLM_DEBATERS names {first} twice; a debate needs two different models.")
    return first, second


def parse_judge(value: str | None, debaters: tuple[str, str]) -> str:
    """LLM_DEBATE_JUDGE: "alternate" (the default), a provider (the debater of that provider) or "provider:model"."""
    if value is None or value.strip().lower() == JUDGE_ALTERNATE:
        return JUDGE_ALTERNATE
    word = value.strip().lower()
    provider = _PROVIDER_ALIASES.get(word, word)
    if provider in LLM_PROVIDERS:
        matching = [entry for entry in debaters if entry.split(":", 1)[0] == provider]
        if len(matching) != 1:
            which = "neither debater is" if not matching else "both debaters are"
            raise ConfigError(
                f"LLM_DEBATE_JUDGE={value.strip()} names a provider, but {which} from {provider} (LLM_DEBATERS: "
                f"{', '.join(debaters)}); write the judge as provider:model, or use alternate."
            )
        return matching[0]
    try:
        return ":".join(model_entry(value, "LLM_DEBATE_JUDGE"))
    except ConfigError:
        raise ConfigError(
            f"LLM_DEBATE_JUDGE must be alternate, a provider ({', '.join(LLM_PROVIDERS)}) or provider:model, e.g. "
            f"{DEFAULT_DEBATERS[1]} (got {value.strip()!r})."
        ) from None


def _lower(value: str | None) -> str | None:
    return value.lower() if value else None


def _whole_number(value: str | None, name: str, *, default: int) -> int:
    """A setting that is a whole number of 0 or more."""
    if value is None:
        return default
    try:
        number = int(value.replace("_", ""))
    except ValueError:
        raise ConfigError(f"{name} must be a whole number (got {value!r}).") from None
    if number < 0:
        raise ConfigError(f"{name} can't be negative (got {number}).")
    return number


def _base_url(value: str | None) -> str | None:
    """BASE_URL without a trailing slash; it must be an http(s) address without a query or fragment."""
    if value is None:
        return None
    url = value.rstrip("/")
    try:
        parts = urlsplit(url)
        port_ok = parts.port is None or parts.port > 0
    except ValueError:
        parts, port_ok = None, False
    if (
        parts is None
        or not port_ok
        or parts.scheme.lower() not in ("http", "https")
        or not parts.hostname
        or parts.query
        or parts.fragment
        or "@" in parts.netloc
        or any(char.isspace() for char in url)
    ):
        raise ConfigError(f"BASE_URL must be the website's address, like https://my-dips.fly.dev (got {value!r}).")
    return url


def _proxy_secret(value: str | None) -> str | None:
    """PROXY_SECRET: at least PROXY_SECRET_MIN_LENGTH printable ASCII characters without spaces (it travels in a
    header)."""
    if value is None:
        return None
    if len(value) < PROXY_SECRET_MIN_LENGTH or not all("!" <= char <= "~" for char in value):
        raise ConfigError(
            f"PROXY_SECRET must be a random secret of at least {PROXY_SECRET_MIN_LENGTH} characters without spaces "
            f"(got {len(value)} characters). Generate one with {GENERATE_SECRET_KEY} and set the same value as "
            "DIP_PROXY_SECRET in Vercel."
        )
    return value


def origin_text(value: str) -> str | None:
    """An http(s) origin as "scheme://host[:port]": lower case, without the default port and without a trailing
    slash; None when value isn't an origin (a path, query, fragment, user name or anything but a DNS name, IPv4 or
    [IPv6] address as the host)."""
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").rstrip(".")
    if (
        scheme not in _DEFAULT_PORTS
        or not host
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or "@" in parts.netloc
        or any(char.isspace() for char in value.strip())
    ):
        return None
    if ":" in host:  # an IPv6 address
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            return None
        host = f"[{host}]"
    elif not _ORIGIN_HOST.fullmatch(host):
        return None
    suffix = f":{port}" if port is not None and port != _DEFAULT_PORTS[scheme] else ""
    return f"{scheme}://{host}{suffix}"


def origin_pattern(entry: str) -> re.Pattern[str]:
    """The regular expression of a TRUSTED_ORIGINS pattern such as https://my-dips-git-*-my-team.vercel.app (every
    branch's preview deployment of one Vercel project). Only a narrow pattern is accepted: https without a port, one
    *, inside the host's first label with fixed text on both sides of it (the project name before, the team after), and
    at least two labels after that one; the * then matches letters, digits and hyphens only, never a dot, so it can't
    reach into another label. "https://*.vercel.app" or "https://my-dips-*.vercel.app" would let any Vercel project
    in: a ConfigError says so."""
    text = entry.strip().lower().rstrip("/")
    problem = (
        f"TRUSTED_ORIGINS allows one pattern with a * in the first part of the host between fixed text, for a Vercel "
        f"project's preview deployments, e.g. https://my-dips-git-*-my-team.vercel.app (got {entry.strip()!r})."
    )
    scheme, separator, host = text.partition("://")
    if scheme != "https" or not separator or host.count("*") != 1 or any(char in host for char in "/:@?#"):
        raise ConfigError(problem)
    first, _, rest = host.partition(".")
    before, _, after = first.partition("*")
    labels = rest.split(".") if rest else []
    if (
        not before
        or not after
        or len(labels) < 2
        or not all(_LABEL_PART.fullmatch(part) for part in (before, after))
        or not all(_ORIGIN_HOST.fullmatch(label) for label in labels)
        or not before[0].isalnum()
        or not after[-1].isalnum()
        or len(before) + len(after) > 62
    ):
        raise ConfigError(problem)
    width = 63 - len(before) - len(after)  # a DNS label has at most 63 characters
    return re.compile(
        "https://" + re.escape(before) + f"[a-z0-9-]{{1,{width}}}" + re.escape(after) + r"\." + re.escape(rest)
    )


def parse_trusted_origins(value: str | None) -> tuple[str, ...]:
    """TRUSTED_ORIGINS: comma-separated origins (https://my-dips-git-main-my-team.vercel.app) and at most one pattern
    (origin_pattern), each normalised (origin_text); () when unset."""
    if value is None:
        return ()
    found: list[str] = []
    patterns = 0
    for entry in (part.strip() for part in value.split(",")):
        if not entry:
            continue
        if "*" in entry:
            patterns += 1
            if patterns > 1:
                raise ConfigError("TRUSTED_ORIGINS can hold only one pattern with a *; list the other origins in full.")
            origin_pattern(entry)
            normalised: str | None = entry.lower().rstrip("/")
        else:
            normalised = origin_text(entry)
            if normalised is None:
                raise ConfigError(
                    "TRUSTED_ORIGINS entries are origins like https://my-dips-git-main-my-team.vercel.app: http(s), "
                    f"a host and optionally a port, without a path (got {entry!r})."
                )
        if normalised not in found:
            found.append(normalised)
    return tuple(found)


def _positive_int(value: str | None, name: str) -> int | None:
    if value is None:
        return None
    try:
        number = int(value.replace("_", ""))
    except ValueError:
        raise ConfigError(f"{name} must be a whole number (got {value!r}).") from None
    if number <= 0:
        raise ConfigError(f"{name} must be greater than zero (got {number}).")
    return number


def _bool(value: str | None, name: str, *, default: bool) -> bool:
    if value is None:
        return default
    if value.lower() in _TRUE:
        return True
    if value.lower() in _FALSE:
        return False
    raise ConfigError(f"{name} must be true or false (got {value!r}).")


def _address_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]


def display_zone(name: str | None) -> tzinfo:
    """The time zone named by DISPLAY_TZ (an IANA name like Europe/Athens; the case doesn't matter), UTC when unset.

    An unknown name is a ConfigError. Windows has no time zone database of its own: there the tzdata package provides
    it (a dependency of this project on Windows).
    """
    if name is None or name.upper() in ("UTC", "Z", "ETC/UTC", "GMT"):
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        pass
    try:
        known = {zone.casefold(): zone for zone in available_timezones()}
    except OSError:
        known = {}
    if name.casefold() in known:
        return ZoneInfo(known[name.casefold()])
    hint = ""
    if not known:
        hint = " No time zone database was found: install it with pip install tzdata."
    elif sys.platform == "win32":
        hint = " On Windows the names come from the tzdata package (pip install tzdata)."
    raise ConfigError(
        f"DISPLAY_TZ must be an IANA time zone name such as Europe/Athens, Europe/Berlin or America/New_York (got "
        f"{name!r}).{hint}"
    )


def zone_name(zone: tzinfo) -> str:
    """The IANA name of a time zone from display_zone ("Europe/Athens"; "UTC" for UTC)."""
    return getattr(zone, "key", None) or "UTC"


def _data_dir(value: str | None) -> Path:
    path = Path.cwd() / Path(value).expanduser() if value else Path.cwd() / "data"
    if path.exists() and not path.is_dir():
        raise ConfigError(f"DATA_DIR must be a folder, but {path} is a file.")
    return path


# --- scanner.toml --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ScanConfig:
    """[scan]: how often and how much to scan."""

    interval_minutes: float = 5
    max_article_age_hours: float = 24  # older articles are stored but never sent to the LLM
    lookback_hours: float = 48  # how far back impacts count towards a candidate
    triage_batch_size: int = 20
    max_triage_attempts: int = 3
    max_candidates_per_cycle: int = 8  # bounds LLM cost; the rest are logged, not silently dropped
    cooldown_hours: float = 24  # don't re-analyse a ticker unless new negative news arrived since
    # Until a new session has traded since a ticker's last analysis (weekend or evening news on an unchanged price,
    # or more news later the same day), new news re-analyses it at most once in this many hours; 0 turns this off.
    reanalyse_same_session_hours: float = 12
    max_analyses_per_day: int = 40  # analyses in any 24 hours, all tickers together; 0 = no limit
    context_news: bool = True  # fetch per-ticker Yahoo/Google headlines for analysis
    workers: int = 8  # feed fetch threads
    retention_days: int = 30  # prune articles older than this (opportunities are kept)


@dataclass(frozen=True)
class DipConfig:
    """[dip]: what counts as a dip, and which news can make a company a candidate. Drops are positive numbers."""

    min_drop_1d_pct: float = 3.0  # dip if change_1d <= -3
    min_drop_5d_pct: float = 6.0
    min_drawdown_20d_pct: float = 10.0
    min_magnitude: int = 2
    directions: tuple[str, ...] = ("negative", "mixed")
    include_indirect: bool = True


@dataclass(frozen=True)
class UniverseConfig:
    """[universe]: which tickers may become candidates."""

    watchlist: tuple[str, ...] = ()  # always eligible: magnitude >= 1, any relation
    only_watchlist: bool = False
    exclude: tuple[str, ...] = ()
    min_price: float = 1.0
    allowed_suffixes: tuple[str, ...] | None = None  # e.g. ("", ".DE", ".AT"); "" = no suffix (US). None = all
    # The listing to use for a company listed in several places, e.g. {"ASML": "ASML.AS"} for a euro account: news the
    # triage files under the key goes to the value, and watchlist and exclude entries are read the same way.
    preferred_listings: dict[str, str] = field(default_factory=dict)

    def sec_symbol(self, ticker: str) -> str:
        """The US symbol a preferred listing's company files with the SEC under (ASML.AS -> ASML, when "ASML" =
        "ASML.AS"), else ticker. Only a key without an exchange suffix counts: "SAP.F" has no SEC filings either."""
        us = (key for key, value in self.preferred_listings.items() if value == ticker and "." not in key)
        return next(us, ticker)


@dataclass(frozen=True)
class AlertConfig:
    """[alerts]: which opportunities are sent as notifications."""

    min_score: float = 65
    min_probability: int = 60
    verdicts: tuple[str, ...] = ("temporary_fear", "mixed")
    # A ticker alerted within repeat_hours isn't alerted again unless the score rose by min_score_change, the
    # verdict changed or the price fell by another [dip] min_drop_1d_pct.
    repeat_hours: float = 24
    min_score_change: float = 10
    # System notices to the same channels: the scanner stopped (bad key, no credit, broken config), the model was
    # unavailable or every feed failed for notice_after_cycles cycles in a row. Each kind at most every 12 hours.
    system_notices: bool = True
    notice_after_cycles: int = 6


@dataclass(frozen=True)
class AccountConfig:
    """[account]: the currency of your broker account. When set, reports and alerts show prices in it too (≈ €) for
    stocks that trade in another currency, and `track` shows the returns in it (exchange-rate moves included)."""

    currency: str | None = None  # ISO code, e.g. "EUR"; None = trading currencies only


@dataclass(frozen=True)
class DebateConfig:
    """[debate]: how the two models of LLM_ANALYSIS_MODE=debate argue (ignored in single mode; see debate.py).

    With when = "disagree" the rebuttals and the judge only run when the two openings disagree materially: different
    verdicts, chances up more than max_probability_gap points apart, potential lows more than max_low_gap_pct of the
    price apart, or one of them passing somebody's alert rules and the other not. Otherwise the openings are merged.
    """

    when: str = "disagree"  # "disagree" | "always"
    rounds: int = 1  # rebuttal rounds before the judge (0: the judge reads the openings)
    max_probability_gap: float = 15  # points of probability_up_6m
    max_low_gap_pct: float = 10  # the potential lows' distance, in % of the price


@dataclass(frozen=True)
class ScannerConfig:
    scan: ScanConfig = field(default_factory=ScanConfig)
    dip: DipConfig = field(default_factory=DipConfig)
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    alerts: AlertConfig = field(default_factory=AlertConfig)
    account: AccountConfig = field(default_factory=AccountConfig)
    debate: DebateConfig = field(default_factory=DebateConfig)


_SECTIONS: dict[str, type] = {
    "scan": ScanConfig,
    "dip": DipConfig,
    "universe": UniverseConfig,
    "alerts": AlertConfig,
    "account": AccountConfig,
    "debate": DebateConfig,
}
# Codes Yahoo uses for hundredths of a currency (pence, cents, agorot) once uppercased; an account is in the main unit.
_MINOR_ACCOUNT_CODES = {"GBX": "GBP", "ZAC": "ZAR", "ILA": "ILS"}


def load_scanner_config(path: Path | None) -> ScannerConfig:
    """Read scanner.toml. No path or a missing file gives the defaults; every key in the file is optional.

    Unknown sections or keys (usually typos) raise ConfigError naming them, so a setting is never silently ignored.
    (The command line insists that a file named with --config or SCANNER_CONFIG exists.)
    """
    if path is None or not Path(path).exists():
        return ScannerConfig()
    data = _read_toml(Path(path), "Scanner config")
    unknown = [name for name in data if name not in _SECTIONS]
    if unknown:
        raise ConfigError(
            f"Unknown section{_plural(unknown)} {', '.join(f'[{name}]' for name in unknown)} in {path}. "
            f"Use {', '.join(f'[{name}]' for name in _SECTIONS)}."
        )
    sections = {name: _load_section(cls, name, data.get(name, {}), path) for name, cls in _SECTIONS.items()}
    config = ScannerConfig(**sections)
    _check_scanner_config(config, path)
    return config


def _load_section(cls: type, name: str, raw: Any, path: Path) -> Any:
    if not isinstance(raw, dict):
        raise ConfigError(f"[{name}] in {path} should be a table of settings.")
    known = {f.name: f for f in fields(cls)}
    unknown = [key for key in raw if key not in known]
    if unknown:
        raise ConfigError(
            f"Unknown setting{_plural(unknown)} {', '.join(repr(key) for key in unknown)} in [{name}] of {path}. "
            f"Known settings: {', '.join(known)}."
        )
    values = {key: _convert(value, str(known[key].type), f"{name}.{key}", path) for key, value in raw.items()}
    return _normalise(cls(**values), path)


def _convert(value: Any, kind: str, where: str, path: Path) -> Any:
    """Check a TOML value against a dataclass field type (a string, because of `from __future__ import annotations`)."""
    if kind == "bool":
        if isinstance(value, bool):
            return value
        raise ConfigError(f"{where} in {path} must be true or false (got {value!r}).")
    if kind == "int":
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        raise ConfigError(f"{where} in {path} must be a whole number (got {value!r}).")
    if kind == "float":
        if isinstance(value, int | float) and not isinstance(value, bool):
            return float(value)
        raise ConfigError(f"{where} in {path} must be a number (got {value!r}).")
    if kind.startswith("tuple[str, ...]"):
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return tuple(item.strip() for item in value)
        raise ConfigError(f'{where} in {path} must be a list of strings, e.g. ["a", "b"] (got {value!r}).')
    if kind == "str":
        if isinstance(value, str):
            return value.strip()
        raise ConfigError(f"{where} in {path} must be a string (got {value!r}).")
    if kind == "str | None":
        if isinstance(value, str):
            return value.strip() or None
        raise ConfigError(f'{where} in {path} must be a string, e.g. "EUR" (got {value!r}).')
    if kind == "dict[str, str]":
        if isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
            return dict(value)
        raise ConfigError(
            f'{where} in {path} must be a table of symbols, e.g. {{ "ASML" = "ASML.AS" }} (got {value!r}).'
        )
    raise AssertionError(f"No converter for field type {kind}")  # a new field type in this module


def _normalise(section: Any, path: Path) -> Any:
    if isinstance(section, DipConfig):
        return replace(
            section,
            min_drop_1d_pct=abs(section.min_drop_1d_pct),
            min_drop_5d_pct=abs(section.min_drop_5d_pct),
            min_drawdown_20d_pct=abs(section.min_drawdown_20d_pct),
            directions=tuple(item.lower() for item in section.directions),
        )
    if isinstance(section, UniverseConfig):
        suffixes = section.allowed_suffixes
        preferred = _listings(section.preferred_listings, path)
        return replace(
            section,
            watchlist=_tickers(section.watchlist, preferred),
            exclude=_tickers(section.exclude, preferred),
            allowed_suffixes=None if suffixes is None else tuple(dict.fromkeys(_suffix(item) for item in suffixes)),
            preferred_listings=preferred,
        )
    if isinstance(section, AlertConfig):
        return replace(section, verdicts=tuple(item.lower() for item in section.verdicts))
    if isinstance(section, AccountConfig):
        return replace(section, currency=_account_currency(section.currency, path))
    if isinstance(section, DebateConfig):
        return replace(section, when=section.when.lower())
    return section


def _tickers(values: tuple[str, ...], preferred: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Symbols written the way triage writes them ("BRK.B" -> "BRK-B", "NASDAQ:TSLA" -> "TSLA"), so they match, and
    moved to the preferred listing when there is one ("ASML" -> "ASML.AS")."""
    from .triage import normalise_ticker  # here: triage imports this module

    preferred = preferred or {}
    symbols = (normalise_ticker(value) or value.strip().upper() for value in values if value.strip())
    return tuple(dict.fromkeys(preferred.get(symbol, symbol) for symbol in symbols))


def _listings(values: Mapping[str, str], path: Path) -> dict[str, str]:
    """[universe] preferred_listings with both sides written the way triage writes symbols ("asml" -> "ASML")."""
    from .triage import normalise_ticker

    listings: dict[str, str] = {}
    for key, value in values.items():
        source, target = normalise_ticker(key), normalise_ticker(value)
        if source is None or target is None:
            wrong = key if source is None else value
            raise ConfigError(
                f"universe.preferred_listings in {path}: {wrong!r} isn't a company's Yahoo Finance symbol; write "
                'each entry like "ASML" = "ASML.AS".'
            )
        if source != target:
            listings[source] = target
    return listings


def _account_currency(value: str | None, path: Path) -> str | None:
    if value is None:
        return None
    code = value.strip().upper()
    if code in _MINOR_ACCOUNT_CODES:
        raise ConfigError(
            f"account.currency in {path} is {value!r}, a hundredth of a currency; use {_MINOR_ACCOUNT_CODES[code]}."
        )
    if not re.fullmatch(r"[A-Z]{3}", code):
        raise ConfigError(
            f'account.currency in {path} must be a three-letter currency code like "EUR" (got {value!r}).'
        )
    return code


def _listing_suffix(symbol: str) -> str:
    """The Yahoo exchange suffix of a symbol (".AS" for "ASML.AS"), "" for a US listing."""
    _, dot, suffix = symbol.rpartition(".")
    return f".{suffix.upper()}" if dot else ""


def _suffix(value: str) -> str:
    value = value.strip().upper()
    return value if not value or value.startswith(".") else f".{value}"


def _check_scanner_config(config: ScannerConfig, path: Path) -> None:
    scan, dip, universe, alerts, debate = config.scan, config.dip, config.universe, config.alerts, config.debate
    checks = [
        (scan.interval_minutes > 0, "scan.interval_minutes must be greater than zero"),
        (scan.max_article_age_hours > 0, "scan.max_article_age_hours must be greater than zero"),
        (scan.lookback_hours > 0, "scan.lookback_hours must be greater than zero"),
        (scan.triage_batch_size >= 1, "scan.triage_batch_size must be at least 1"),
        (scan.max_triage_attempts >= 1, "scan.max_triage_attempts must be at least 1"),
        (scan.max_candidates_per_cycle >= 0, "scan.max_candidates_per_cycle can't be negative"),
        (scan.cooldown_hours >= 0, "scan.cooldown_hours can't be negative"),
        (scan.reanalyse_same_session_hours >= 0, "scan.reanalyse_same_session_hours can't be negative"),
        (scan.max_analyses_per_day >= 0, "scan.max_analyses_per_day can't be negative"),
        (scan.workers >= 1, "scan.workers must be at least 1"),
        (scan.retention_days >= 1, "scan.retention_days must be at least 1"),
        (1 <= dip.min_magnitude <= 5, "dip.min_magnitude must be between 1 and 5"),
        (universe.min_price >= 0, "universe.min_price can't be negative"),
        (0 <= alerts.min_score <= 100, "alerts.min_score must be between 0 and 100"),
        (0 <= alerts.min_probability <= 100, "alerts.min_probability must be between 0 and 100"),
        (alerts.repeat_hours >= 0, "alerts.repeat_hours can't be negative"),
        (alerts.min_score_change >= 0, "alerts.min_score_change can't be negative"),
        (alerts.notice_after_cycles >= 1, "alerts.notice_after_cycles must be at least 1"),
        (debate.when in DEBATE_WHEN, f'debate.when must be "disagree" or "always" (got {debate.when!r})'),
        (
            0 <= debate.rounds <= MAX_DEBATE_ROUNDS,
            f"debate.rounds must be between 0 and {MAX_DEBATE_ROUNDS} (each round is two more model calls)",
        ),
        (0 <= debate.max_probability_gap <= 100, "debate.max_probability_gap must be between 0 and 100"),
        (debate.max_low_gap_pct >= 0, "debate.max_low_gap_pct can't be negative"),
    ]
    for ok, message in checks:
        if not ok:
            raise ConfigError(f"{message} (in {path}).")
    if universe.allowed_suffixes is not None:
        # A preferred listing on an exchange that isn't allowed would only drop the company's news, every cycle.
        blocked = [
            f'"{key}" = "{value}"'
            for key, value in universe.preferred_listings.items()
            if _listing_suffix(value) not in universe.allowed_suffixes
        ]
        if blocked:
            raise ConfigError(
                f"universe.preferred_listings in {path} moves news to exchanges that universe.allowed_suffixes leaves "
                f"out, so that news would be dropped: {', '.join(blocked)}. Add their suffixes to allowed_suffixes or "
                "remove those entries."
            )
    for where, values, allowed in (
        ("dip.directions", dip.directions, DIRECTIONS),
        ("alerts.verdicts", alerts.verdicts, VERDICTS),
    ):
        wrong = [value for value in values if value not in allowed]
        if wrong:
            raise ConfigError(
                f"{where} in {path} has unknown value{_plural(wrong)} {', '.join(map(repr, wrong))}; "
                f"choose from {', '.join(allowed)}."
            )


# --- feeds.toml ----------------------------------------------------------------------------------------------------

_FEED_KEYS = ("name", "url", "enabled", "category", "dedup_titles", "languages", "exclude_titles")


def load_feeds(path: Path) -> list[Feed]:
    """Read the feed list: one [feeds.<key>] table per source with url and optional name, enabled, category,
    dedup_titles (default true), languages (default ["en"]; [] keeps every language) and exclude_titles (regular
    expressions; items whose headline matches one are dropped, see _title_patterns).

    Disabled feeds are returned too (with enabled=False) so they can be listed; callers skip them when fetching.
    """
    data = _read_toml(Path(path), "Feed list")
    entries = data.get("feeds")
    if not isinstance(entries, dict) or not entries:
        raise ConfigError(f"{path} doesn't define any feeds; add a [feeds.<key>] table with a url for each one.")
    feeds = []
    for key, entry in entries.items():
        if not isinstance(entry, dict):
            raise ConfigError(f'Feed "{key}" in {path} should be a table of settings.')
        if ":" in key or not key.strip():
            raise ConfigError(f'Feed key "{key}" in {path} can\'t be empty or contain ":".')
        unknown = [name for name in entry if name not in _FEED_KEYS]
        if unknown:
            raise ConfigError(
                f'Unknown setting{_plural(unknown)} {", ".join(map(repr, unknown))} for feed "{key}" in {path}. '
                f"Known settings: {', '.join(_FEED_KEYS)}."
            )
        url = entry.get("url")
        if not isinstance(url, str) or not url.strip().lower().startswith(("http://", "https://")):
            raise ConfigError(f'Feed "{key}" in {path} needs a url starting with http:// or https://.')
        for name in ("enabled", "dedup_titles"):
            if not isinstance(entry.get(name, True), bool):
                raise ConfigError(f'{name} for feed "{key}" in {path} must be true or false (got {entry[name]!r}).')
        for name in ("name", "category"):
            if name in entry and not isinstance(entry[name], str):
                raise ConfigError(f'{name} for feed "{key}" in {path} must be a string (got {entry[name]!r}).')
        languages = entry.get("languages", ["en"])
        if not isinstance(languages, list) or not all(isinstance(item, str) and item.strip() for item in languages):
            raise ConfigError(
                f'languages for feed "{key}" in {path} must be a list of language codes, e.g. ["en"] (got '
                f"{languages!r})."
            )
        feeds.append(
            Feed(
                key=key,
                name=(entry.get("name") or "").strip() or key,
                url=url.strip(),
                enabled=entry.get("enabled", True),
                category=(entry.get("category") or "").strip() or "markets",
                dedup_titles=entry.get("dedup_titles", True),
                languages=tuple(dict.fromkeys(item.strip().lower().replace("_", "-") for item in languages)),
                exclude_titles=_title_patterns(entry.get("exclude_titles", []), key, path),
            )
        )
    return feeds


def _title_patterns(value: Any, key: str, path: Path) -> tuple[re.Pattern[str], ...]:
    """exclude_titles of a feed compiled: Python regular expressions, searched anywhere in the headline (anchor them
    with ^ and $), case-sensitive unless they start with (?i). A pattern that doesn't compile is a ConfigError."""
    where = f'exclude_titles for feed "{key}" in {path}'
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ConfigError(
            f"{where} must be a list of regular expressions, e.g. ['^About .+ - Reuters$'] (got {value!r})."
        )
    patterns = []
    for item in dict.fromkeys(value):
        try:
            patterns.append(re.compile(item))
        except re.error as exc:
            raise ConfigError(f"{where}: {item!r} is not a valid regular expression ({exc}).") from None
    return tuple(patterns)


# --- files ---------------------------------------------------------------------------------------------------------


def default_file(name: str, env_var: str, *, env: Mapping[str, str] | None = None) -> Path:
    """The file named by env_var if set, else DATA_DIR/name when the DATA_DIR variable is set and that file exists (on
    Fly.io a copy on the volume replaces the image's), else ./name if it exists, else the copy shipped with the
    project. Only the DATA_DIR variable counts here, not --data-dir or the ./data default, so a local command line
    finds its files as before."""
    env = os.environ if env is None else env
    value = (env.get(env_var) or "").strip()
    if value:
        return Path(value).expanduser()
    data_dir = (env.get("DATA_DIR") or "").strip()
    if data_dir and (Path(data_dir).expanduser() / name).is_file():
        return Path(data_dir).expanduser() / name
    local = Path.cwd() / name
    return local if local.exists() else PROJECT_ROOT / name


def _read_toml(path: Path, what: str) -> dict[str, Any]:
    """A TOML file's contents. A leading byte-order mark (Windows Notepad and PowerShell 5 write one) is skipped."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise ConfigError(f"{what} not found: {path}") from None
    except OSError as exc:  # a folder, no permission...
        raise ConfigError(f"{what} can't be read: {path} ({exc.strerror or exc})") from None
    try:
        return tomllib.loads(raw.removeprefix(b"\xef\xbb\xbf").decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ConfigError(
            f"{path} is not valid TOML: it isn't UTF-8 text (byte {exc.start}); save it as UTF-8."
        ) from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from None


def _plural(items: list) -> str:
    return "s" if len(items) > 1 else ""
