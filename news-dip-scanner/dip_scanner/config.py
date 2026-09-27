"""Settings: environment variables (usually from .env), the feed list in feeds.toml and thresholds in scanner.toml."""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from .models import DIRECTIONS, VERDICTS, Feed

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATABASE_NAME = "scanner.sqlite3"  # inside DATA_DIR, next to reports/ and cache/

LLM_PROVIDERS = ("openai", "azure", "anthropic")
_PROVIDER_ALIASES = {"foundry": "azure", "azure_foundry": "azure", "azure-foundry": "azure", "claude": "anthropic"}
WEBHOOK_FORMATS = ("slack", "discord", "generic")

# (triage model, analysis model) per provider. Azure has no default: deployment names are yours to choose.
DEFAULT_MODELS = {"openai": ("gpt-5-mini", "gpt-5"), "anthropic": ("claude-haiku-4-5", "claude-sonnet-5")}

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


@dataclass(frozen=True)
class Settings:
    llm: LLMSettings = field(default_factory=LLMSettings)
    notify: NotifySettings = field(default_factory=NotifySettings)
    sec_user_agent: str | None = None  # SEC_USER_AGENT, e.g. "Jane Doe jane@example.com"; None -> fundamentals off
    data_dir: Path = field(default_factory=lambda: Path.cwd() / "data")  # DATA_DIR: database, reports/, cache/


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Read settings from the environment. Missing values are fine here; they're reported when they're needed.

    Values that are set but wrong (an unknown LLM_PROVIDER, a port that isn't a number...) raise ConfigError.
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

    smtp_port = _positive_int(get("SMTP_PORT"), "SMTP_PORT")
    if smtp_port is not None and smtp_port > 65535:
        raise ConfigError(f"SMTP_PORT must be a port number between 1 and 65535 (got {smtp_port}).")

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
    )


def _lower(value: str | None) -> str | None:
    return value.lower() if value else None


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
class ScannerConfig:
    scan: ScanConfig = field(default_factory=ScanConfig)
    dip: DipConfig = field(default_factory=DipConfig)
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    alerts: AlertConfig = field(default_factory=AlertConfig)


_SECTIONS: dict[str, type] = {"scan": ScanConfig, "dip": DipConfig, "universe": UniverseConfig, "alerts": AlertConfig}


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
    return _normalise(cls(**values))


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
    raise AssertionError(f"No converter for field type {kind}")  # a new field type in this module


def _normalise(section: Any) -> Any:
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
        return replace(
            section,
            watchlist=_tickers(section.watchlist),
            exclude=_tickers(section.exclude),
            allowed_suffixes=None if suffixes is None else tuple(dict.fromkeys(_suffix(item) for item in suffixes)),
        )
    if isinstance(section, AlertConfig):
        return replace(section, verdicts=tuple(item.lower() for item in section.verdicts))
    return section


def _tickers(values: tuple[str, ...]) -> tuple[str, ...]:
    """Symbols written the way triage writes them ("BRK.B" -> "BRK-B", "NASDAQ:TSLA" -> "TSLA"), so they match."""
    from .triage import normalise_ticker  # here: triage imports this module

    return tuple(dict.fromkeys(normalise_ticker(value) or value.strip().upper() for value in values if value.strip()))


def _suffix(value: str) -> str:
    value = value.strip().upper()
    return value if not value or value.startswith(".") else f".{value}"


def _check_scanner_config(config: ScannerConfig, path: Path) -> None:
    scan, dip, universe, alerts = config.scan, config.dip, config.universe, config.alerts
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
    ]
    for ok, message in checks:
        if not ok:
            raise ConfigError(f"{message} (in {path}).")
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
    """The file named by env_var if set, else ./name if it exists, else the copy shipped with the project."""
    env = os.environ if env is None else env
    value = (env.get(env_var) or "").strip()
    if value:
        return Path(value).expanduser()
    local = Path.cwd() / name
    return local if local.exists() else PROJECT_ROOT / name


def _read_toml(path: Path, what: str) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"{what} not found: {path}") from None
    except OSError as exc:  # a folder, no permission...
        raise ConfigError(f"{what} can't be read: {path} ({exc.strerror or exc})") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from None


def _plural(items: list) -> str:
    return "s" if len(items) > 1 else ""
