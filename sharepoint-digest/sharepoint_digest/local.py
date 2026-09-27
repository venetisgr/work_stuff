"""Read the files from a folder on this computer, such as a SharePoint folder synced with OneDrive."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from .config import ConfigError
from .sharepoint import DriveFile, FolderListing, select_files


class LocalFolder:
    """A folder on disk. OneDrive gives synced files their SharePoint modified dates, so date ranges still work."""

    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise ConfigError(f"Local folder not found: {self.root}")

    def list_files(
        self, start: datetime, end: datetime, *, date_field: str = "modified", recursive: bool = True
    ) -> FolderListing:
        if date_field == "created":
            raise ConfigError(
                "--date-field created only works when reading SharePoint directly; "
                "synced or downloaded copies don't keep SharePoint's creation dates."
            )
        return select_files(self._walk(recursive), start, end, date_field=date_field)

    def download(self, file: DriveFile) -> bytes:
        # With OneDrive "files on demand", reading a cloud-only file downloads it first.
        return (self.root / file.path).read_bytes()

    def _walk(self, recursive: bool) -> Iterator[DriveFile]:
        for path in self.root.rglob("*") if recursive else self.root.iterdir():
            if path.is_file():
                yield _drive_file(path, self.root)


def _drive_file(path: Path, root: Path) -> DriveFile:
    info = path.stat()
    relative = path.relative_to(root).as_posix()
    return DriveFile(
        id=relative,
        name=path.name,
        path=relative,
        web_url=path.as_uri(),
        size=info.st_size,
        created=datetime.fromtimestamp(_creation_time(info), tz=UTC),
        modified=datetime.fromtimestamp(info.st_mtime, tz=UTC),
        created_by=None,
        modified_by=None,
    )


def _creation_time(info: os.stat_result) -> float:
    if hasattr(info, "st_birthtime"):  # macOS, and Windows on Python 3.12+
        return info.st_birthtime
    return info.st_ctime if sys.platform == "win32" else info.st_mtime  # st_ctime is creation time on Windows
