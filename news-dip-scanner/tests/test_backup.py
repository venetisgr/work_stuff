"""Backups of the database: consistent copies with SQLite's backup API, and rotation."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest
from conftest import NOW, make_opportunity

from dip_scanner.backup import backup_database, backups, rotate
from dip_scanner.store import Store


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "data" / "scanner.sqlite3"
    with Store(path) as store:
        store.add_opportunity(make_opportunity())
    return path


def test_a_backup_is_a_complete_database_named_after_the_time(database, tmp_path):
    folder = tmp_path / "data" / "backups"
    path = backup_database(database, folder, now=NOW)
    assert path == folder / "scanner-20260925-150000.sqlite3"
    with Store(path) as copy:
        assert [opp.ticker for opp in copy.opportunities()] == ["AMD"]
    assert [p.name for p in folder.iterdir()] == [path.name]  # no temporary file left behind


def test_a_backup_taken_while_the_scanner_writes_has_everything_committed(database, tmp_path):
    """The scanner keeps its connection open (WAL mode, uncheckpointed writes): the copy still has them."""
    with Store(database) as live:
        for hours in range(1, 4):
            live.add_opportunity(make_opportunity(created=NOW + timedelta(hours=hours)))
        path = backup_database(database, tmp_path / "backups", now=NOW)
        live.add_opportunity(make_opportunity(created=NOW + timedelta(hours=9)))  # after the backup: not in it
    with Store(path) as copy:
        assert len(copy.opportunities()) == 4


def test_only_the_newest_backups_are_kept(database, tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    (folder / "notes.txt").write_text("mine")  # other files are never touched
    made = [backup_database(database, folder, keep=3, now=NOW + timedelta(days=day)) for day in range(5)]
    assert backups(folder) == made[:1:-1]
    assert (folder / "notes.txt").exists()
    assert rotate(folder, keep=1) == made[3:1:-1] and backups(folder) == [made[-1]]
    assert backups(tmp_path / "missing") == []


def test_two_backups_in_the_same_second_both_survive(database, tmp_path):
    first = backup_database(database, tmp_path / "b", now=NOW)
    second = backup_database(database, tmp_path / "b", now=NOW)
    third = backup_database(database, tmp_path / "b", now=NOW)
    assert (first.name, second.name, third.name) == (
        "scanner-20260925-150000.sqlite3",
        "scanner-20260925-150000-1.sqlite3",
        "scanner-20260925-150000-2.sqlite3",
    )
    assert backups(tmp_path / "b") == [third, second, first]


def test_nothing_to_back_up_and_wrong_arguments(tmp_path, database):
    with pytest.raises(FileNotFoundError, match="nothing to back up"):
        backup_database(tmp_path / "nope.sqlite3", tmp_path / "b")
    with pytest.raises(ValueError, match="at least one"):
        backup_database(database, tmp_path / "b", keep=0)


def test_a_damaged_database_leaves_no_backup(tmp_path):
    broken = tmp_path / "scanner.sqlite3"
    broken.write_bytes(b"this is not a database" * 200)
    with pytest.raises(sqlite3.DatabaseError):
        backup_database(broken, tmp_path / "b", now=NOW)
    assert list((tmp_path / "b").iterdir()) == []
