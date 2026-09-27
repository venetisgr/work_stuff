"""Settings: environment variables (usually from .env) plus the folder list in folders.toml."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

GRAPH_AUTH_MODES = ("app", "device_code", "default")


class ConfigError(Exception):
    """A setting is missing or wrong. The message says what to fix."""


@dataclass(frozen=True)
class Folder:
    """A folder the digest can run against (one entry in folders.toml)."""

    key: str
    label: str
    path: str  # inside the SharePoint library
    site_url: str | None = None
    library: str | None = None
    local_path: str | None = None  # read the folder on this computer instead, e.g. a OneDrive-synced copy


@dataclass(frozen=True)
class SharePointSettings:
    site_url: str | None
    library: str
    auth_mode: str
    tenant_id: str | None
    client_id: str | None
    client_secret: str | None


@dataclass(frozen=True)
class FoundrySettings:
    endpoint: str | None
    deployment: str | None
    api_key: str | None
    reasoning_effort: str | None
    max_output_tokens: int | None


@dataclass(frozen=True)
class Settings:
    sharepoint: SharePointSettings
    foundry: FoundrySettings
    max_input_chars: int


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Read settings from the environment. Missing values are only reported when they're needed."""
    env = os.environ if env is None else env

    def get(name: str, default: str | None = None) -> str | None:
        value = env.get(name, "").strip()
        return value or default

    auth_mode = (get("GRAPH_AUTH_MODE") or "app").lower()
    if auth_mode not in GRAPH_AUTH_MODES:
        raise ConfigError(f"GRAPH_AUTH_MODE must be one of {', '.join(GRAPH_AUTH_MODES)} (got {auth_mode!r}).")

    effort = get("FOUNDRY_REASONING_EFFORT")
    return Settings(
        sharepoint=SharePointSettings(
            site_url=get("SHAREPOINT_SITE_URL"),
            library=get("SHAREPOINT_LIBRARY", "Documents"),
            auth_mode=auth_mode,
            tenant_id=get("AZURE_TENANT_ID"),
            client_id=get("AZURE_CLIENT_ID"),
            client_secret=get("AZURE_CLIENT_SECRET"),
        ),
        foundry=FoundrySettings(
            endpoint=get("FOUNDRY_ENDPOINT"),
            deployment=get("FOUNDRY_DEPLOYMENT"),
            api_key=get("FOUNDRY_API_KEY"),
            reasoning_effort=effort.lower() if effort else None,
            max_output_tokens=_positive_int(get("LLM_MAX_OUTPUT_TOKENS"), "LLM_MAX_OUTPUT_TOKENS"),
        ),
        max_input_chars=_positive_int(get("LLM_MAX_INPUT_CHARS"), "LLM_MAX_INPUT_CHARS") or 200_000,
    )


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


def default_folders_file() -> Path:
    """FOLDERS_FILE if set, else folders.toml in the current directory, else the one in the project."""
    if os.environ.get("FOLDERS_FILE"):
        return Path(os.environ["FOLDERS_FILE"])
    local = Path.cwd() / "folders.toml"
    return local if local.exists() else PROJECT_ROOT / "folders.toml"


def load_folders(path: Path) -> list[Folder]:
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"Folder list not found: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from None

    entries = data.get("folders")
    if not isinstance(entries, dict) or not entries:
        raise ConfigError(f"{path} doesn't define any folders; add a [folders.<key>] table for each one.")

    folders = []
    for key, entry in entries.items():
        if not isinstance(entry, dict):
            raise ConfigError(f'Folder "{key}" in {path} should be a table of settings.')
        local_path = _optional_str(entry.get("local_path"))
        folder_path = entry.get("path", "" if local_path else None)
        if not isinstance(folder_path, str):
            raise ConfigError(
                f'Folder "{key}" in {path} needs a path inside the SharePoint library '
                '(use path = "" for the library root) or a local_path.'
            )
        folders.append(
            Folder(
                key=key,
                label=str(entry.get("label") or key),
                path=folder_path.strip().strip("/"),
                site_url=_optional_str(entry.get("site_url")),
                library=_optional_str(entry.get("library")),
                local_path=local_path,
            )
        )
    return folders


def _optional_str(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def find_folder(folders: Sequence[Folder], choice: str) -> Folder:
    """Look a folder up by key, label or 1-based position in the list."""
    wanted = choice.strip().casefold()
    for folder in folders:
        if wanted in (folder.key.casefold(), folder.label.casefold()):
            return folder
    if wanted.isdigit() and 1 <= int(wanted) <= len(folders):
        return folders[int(wanted) - 1]
    options = ", ".join(folder.key for folder in folders)
    raise ConfigError(f"Unknown folder {choice!r}. Choose one of: {options}")
