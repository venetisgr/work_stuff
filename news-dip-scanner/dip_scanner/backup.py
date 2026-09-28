"""Backups of the scanner's database: a consistent copy made with SQLite's online backup API, with rotation.

Copying the database file while the scanner writes can catch a transaction half done (and misses what still sits in
the -wal file). The backup API copies the database as one consistent snapshot while the scanner and the website keep
running, into DATA_DIR/backups/scanner-YYYYmmdd-HHMMSS.sqlite3 (UTC). The copy is checked (PRAGMA quick_check)
before it takes its final name, and only the newest `keep` backups stay. A backup is a complete database: to restore
one, stop the scanner and put it in place of scanner.sqlite3 (see the README).
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .models import utc

log = logging.getLogger(__name__)

BACKUP_FOLDER = "backups"  # inside DATA_DIR
DEFAULT_KEEP = 7
_NAME = re.compile(r"scanner-(\d{8}-\d{6})(?:-(\d+))?\.sqlite3")


def backup_database(database: Path, folder: Path, *, keep: int = DEFAULT_KEEP, now: datetime | None = None) -> Path:
    """Copy database into folder as scanner-YYYYmmdd-HHMMSS.sqlite3 and delete all but the newest keep backups;
    returns the new backup's path.

    Raises FileNotFoundError when there is no database yet, sqlite3.DatabaseError when it can't be read or the copy
    fails its check (nothing is left behind then), ValueError when keep is below 1.
    """
    if keep < 1:
        raise ValueError(f"Keep at least one backup (got {keep}).")
    database = Path(database)
    if not database.is_file():
        raise FileNotFoundError(f"No database at {database} yet, so there is nothing to back up.")
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = f"{utc(now or datetime.now(UTC)):%Y%m%d-%H%M%S}"
    target = folder / f"scanner-{stamp}.sqlite3"
    number = 1
    while target.exists():  # two backups within the same second
        target = folder / f"scanner-{stamp}-{number}.sqlite3"
        number += 1
    temporary = folder / f".{target.name}.tmp"
    try:
        source = sqlite3.connect(str(database), timeout=30)
        try:
            copy = sqlite3.connect(str(temporary))
            try:
                source.backup(copy)
                check = copy.execute("PRAGMA quick_check").fetchone()[0]
            finally:
                copy.close()
        finally:
            source.close()
        if check != "ok":
            raise sqlite3.DatabaseError(f"The backup of {database} failed its check: {check}")
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    removed = rotate(folder, keep=keep)
    log.info("Backed up %s to %s%s.", database, target, f"; removed {len(removed)} older backup(s)" if removed else "")
    return target


def backups(folder: Path) -> list[Path]:
    """The backups in folder, newest first (by name, which starts with the UTC time)."""
    folder = Path(folder)
    if not folder.is_dir():
        return []
    return sorted((path for path in folder.iterdir() if _NAME.fullmatch(path.name)), key=_order, reverse=True)


def rotate(folder: Path, *, keep: int) -> list[Path]:
    """Delete all but the newest keep backups in folder; returns the deleted paths. Other files are left alone."""
    old = backups(folder)[max(1, keep) :]
    for path in old:
        path.unlink(missing_ok=True)
    return old


def _order(path: Path) -> tuple[str, int]:
    """(time stamp, number within the second) of a backup's name, for sorting."""
    match = _NAME.fullmatch(path.name)
    assert match is not None  # backups() only lists names that match
    return match.group(1), int(match.group(2) or 0)
