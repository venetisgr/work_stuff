"""Read-only access to SharePoint document libraries through Microsoft Graph."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, unquote, urlparse

import requests
from azure.core.credentials import AccessToken, TokenCredential
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import ConfigError, SharePointSettings

GRAPH_URL = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"

#: File types that get summarized, and how prompts and reports describe them.
SUPPORTED_TYPES = {".pptx": "PowerPoint deck", ".pptm": "PowerPoint deck", ".docx": "Word document"}

#: Office formats we can't read yet. They're listed in the report instead of silently ignored.
UNSUPPORTED_OFFICE_TYPES = {".ppt", ".pps", ".ppsx", ".doc", ".docm"}

_ITEM_FIELDS = "id,name,size,webUrl,file,folder,createdDateTime,lastModifiedDateTime,createdBy,lastModifiedBy"


class GraphError(Exception):
    def __init__(self, status: int, message: str, url: str):
        self.status = status
        self.url = url
        hint = ""
        if status in (401, 403):
            hint = (
                " Check that the app registration has the Graph permission Sites.Read.All"
                " (or Sites.Selected with access to this site) and that an admin has granted consent."
            )
        super().__init__(f"Microsoft Graph returned {status} for {url}: {message}{hint}")

    @classmethod
    def from_response(cls, response: requests.Response) -> GraphError:
        try:
            message = response.json()["error"]["message"]
        except (ValueError, KeyError, TypeError):
            message = response.text[:300] or response.reason
        return cls(response.status_code, message, response.url)


@dataclass(frozen=True)
class DriveFile:
    """A file in the selected SharePoint folder."""

    id: str
    name: str
    path: str  # relative to the selected folder, e.g. "Q3/Plan.pptx"
    web_url: str
    size: int
    created: datetime
    modified: datetime
    created_by: str | None
    modified_by: str | None

    @property
    def extension(self) -> str:
        return PurePosixPath(self.name).suffix.lower()

    @property
    def kind(self) -> str | None:
        """Label used in prompts and reports ("PowerPoint deck", "Word document"), or None if not summarized."""
        return SUPPORTED_TYPES.get(self.extension)

    def timestamp(self, date_field: str) -> datetime:
        return self.created if date_field == "created" else self.modified

    @classmethod
    def from_graph(cls, item: dict[str, Any], path: str) -> DriveFile:
        return cls(
            id=item["id"],
            name=item["name"],
            path=path,
            web_url=item.get("webUrl", ""),
            size=int(item.get("size") or 0),
            created=datetime.fromisoformat(item["createdDateTime"]),
            modified=datetime.fromisoformat(item["lastModifiedDateTime"]),
            created_by=_display_name(item.get("createdBy")),
            modified_by=_display_name(item.get("lastModifiedBy")),
        )


def _display_name(identity_set: dict[str, Any] | None) -> str | None:
    for key in ("user", "application", "device"):
        name = ((identity_set or {}).get(key) or {}).get("displayName")
        if name:
            return name
    return None


@dataclass(frozen=True)
class SkippedFile:
    file: DriveFile
    reason: str


@dataclass(frozen=True)
class FolderListing:
    files: list[DriveFile]  # files to summarize, oldest first
    skipped: list[SkippedFile]  # Office files in the date range that can't be read


def graph_credential(settings: SharePointSettings) -> TokenCredential:
    """Build the Entra ID credential used to call Microsoft Graph, per GRAPH_AUTH_MODE."""
    from azure.identity import ClientSecretCredential, DefaultAzureCredential, DeviceCodeCredential

    if settings.auth_mode == "default":
        return DefaultAzureCredential()

    required = {"AZURE_TENANT_ID": settings.tenant_id, "AZURE_CLIENT_ID": settings.client_id}
    if settings.auth_mode == "app":
        required["AZURE_CLIENT_SECRET"] = settings.client_secret
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ConfigError(f"GRAPH_AUTH_MODE={settings.auth_mode} needs {', '.join(missing)} in .env.")

    if settings.auth_mode == "device_code":
        return DeviceCodeCredential(client_id=settings.client_id, tenant_id=settings.tenant_id)
    return ClientSecretCredential(settings.tenant_id, settings.client_id, settings.client_secret)


def _new_session() -> requests.Session:
    # Graph throttles with 429/503 plus a Retry-After header, which urllib3 honours.
    retry = Retry(
        total=6,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


class GraphClient:
    """GET-only Microsoft Graph client: authentication, retries, and paging. Safe to share across threads."""

    def __init__(
        self,
        credential: TokenCredential,
        session_factory: Callable[[], requests.Session] = _new_session,
        timeout: float = 120,
    ):
        self._credential = credential
        self._session_factory = session_factory
        self._timeout = timeout
        self._token: AccessToken | None = None
        self._token_lock = threading.Lock()
        self._local = threading.local()  # requests sessions aren't thread-safe, so one per thread

    def _headers(self) -> dict[str, str]:
        with self._token_lock:
            if self._token is None or self._token.expires_on - 120 < time.time():
                self._token = self._credential.get_token(GRAPH_SCOPE)
            return {"Authorization": f"Bearer {self._token.token}"}

    def _get(self, url: str, params: dict[str, str] | None = None) -> requests.Response:
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._local.session = self._session_factory()
        if url.startswith("/"):
            url = GRAPH_URL + url
        # File downloads redirect to a pre-authenticated SharePoint URL; requests drops the
        # Authorization header when the redirect changes host, which is what we want.
        response = session.get(url, headers=self._headers(), params=params, timeout=self._timeout)
        if response.status_code >= 400:
            raise GraphError.from_response(response)
        return response

    def get_json(self, url: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        return self._get(url, params).json()

    def get_bytes(self, url: str) -> bytes:
        return self._get(url).content

    def iter_items(self, url: str, params: dict[str, str] | None = None) -> Iterator[dict[str, Any]]:
        """Yield every entry of a Graph collection, following @odata.nextLink pages."""
        next_url: str | None = url
        while next_url:
            page = self.get_json(next_url, params)
            yield from page.get("value", [])
            next_url, params = page.get("@odata.nextLink"), None  # nextLink already carries the query


class SharePointFolder:
    """A folder in a SharePoint document library, resolved to Graph drive and item IDs."""

    def __init__(self, graph: GraphClient, drive_id: str, item_id: str, web_url: str = ""):
        self.graph = graph
        self.drive_id = drive_id
        self.item_id = item_id
        self.web_url = web_url

    @classmethod
    def resolve(cls, graph: GraphClient, site_url: str, library: str, folder_path: str) -> SharePointFolder:
        site_id = _site_id(graph, site_url)
        drive_id = _drive_id(graph, site_id, site_url, library)
        folder = _folder_item(graph, drive_id, library, folder_path)
        return cls(graph, drive_id, folder["id"], folder.get("webUrl", ""))

    def list_files(
        self, start: datetime, end: datetime, *, date_field: str = "modified", recursive: bool = True
    ) -> FolderListing:
        """Files whose modified (or created) time falls in [start, end)."""
        files: list[DriveFile] = []
        skipped: list[SkippedFile] = []
        for item, path in self._walk(recursive):
            if item["name"].startswith("~$"):  # Office lock files
                continue
            file = DriveFile.from_graph(item, path)
            if not start <= file.timestamp(date_field) < end:
                continue
            if file.kind:
                files.append(file)
            elif file.extension in UNSUPPORTED_OFFICE_TYPES:
                reason = f"{file.extension} files aren't supported; save it as .pptx or .docx to include it."
                skipped.append(SkippedFile(file, reason))
        files.sort(key=lambda f: f.timestamp(date_field))
        return FolderListing(files, skipped)

    def download(self, file: DriveFile) -> bytes:
        return self.graph.get_bytes(f"/drives/{self.drive_id}/items/{file.id}/content")

    def _walk(self, recursive: bool) -> Iterator[tuple[dict[str, Any], str]]:
        pending = [(self.item_id, "")]
        while pending:
            item_id, prefix = pending.pop()
            children = f"/drives/{self.drive_id}/items/{item_id}/children"
            for item in self.graph.iter_items(children, {"$select": _ITEM_FIELDS}):
                path = f"{prefix}/{item['name']}" if prefix else item["name"]
                if "folder" in item:
                    if recursive:
                        pending.append((item["id"], path))
                elif "file" in item:
                    yield item, path


def _site_id(graph: GraphClient, site_url: str) -> str:
    parsed = urlparse(site_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ConfigError(
            "The SharePoint site URL should look like https://contoso.sharepoint.com/sites/TeamSite "
            f"(got {site_url!r})."
        )
    site_path = unquote(parsed.path).rstrip("/")
    resource = f"/sites/{parsed.hostname}" + (f":{quote(site_path)}" if site_path else "")
    try:
        return graph.get_json(resource, {"$select": "id"})["id"]
    except GraphError as exc:
        if exc.status == 404:
            raise ConfigError(
                f"SharePoint site not found: {site_url}. Use the site's address only, e.g. "
                "https://contoso.sharepoint.com/sites/TeamSite, without library or page paths."
            ) from exc
        raise


def _drive_id(graph: GraphClient, site_id: str, site_url: str, library: str) -> str:
    drives = list(graph.iter_items(f"/sites/{site_id}/drives", {"$select": "id,name,webUrl"}))
    wanted = library.strip().strip("/").casefold()
    for drive in drives:
        # Match the display name ("Documents") or the name in the URL ("Shared Documents").
        url_name = unquote(drive.get("webUrl", "").rstrip("/").rsplit("/", 1)[-1])
        if wanted in (drive.get("name", "").casefold(), url_name.casefold()):
            return drive["id"]
    available = ", ".join(sorted(drive.get("name", "?") for drive in drives)) or "none visible"
    raise ConfigError(f'Document library "{library}" not found on {site_url}. Libraries on that site: {available}.')


def _folder_item(graph: GraphClient, drive_id: str, library: str, folder_path: str) -> dict[str, Any]:
    path = folder_path.strip("/")
    resource = f"/drives/{drive_id}/root" + (f":/{quote(path)}" if path else "")
    try:
        item = graph.get_json(resource, {"$select": "id,name,webUrl,folder"})
    except GraphError as exc:
        if exc.status == 404:
            raise ConfigError(
                f'Folder "{folder_path}" not found in the "{library}" library. Check its path in folders.toml.'
            ) from exc
        raise
    if "folder" not in item:
        raise ConfigError(f'"{folder_path}" in the "{library}" library is a file, not a folder.')
    return item
