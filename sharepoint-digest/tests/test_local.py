from datetime import UTC, datetime

import pytest
from conftest import make_docx, make_pptx, write_file

from sharepoint_digest.config import ConfigError
from sharepoint_digest.local import LocalFolder

START = datetime(2026, 9, 1, tzinfo=UTC)
END = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.fixture
def synced(tmp_path):
    """A folder laid out like a OneDrive-synced SharePoint folder."""
    write_file(tmp_path, "Q3 plan.pptx", make_pptx(), "2026-09-20T10:00:00+00:00")
    write_file(tmp_path, "Minutes/Kickoff.docx", make_docx(), "2026-09-02T08:30:00+00:00")
    write_file(tmp_path, "Q1 plan.pptx", b"old", "2026-03-02T10:00:00+00:00")
    write_file(tmp_path, "Old deck.ppt", b"legacy", "2026-09-03T10:00:00+00:00")
    write_file(tmp_path, "Budget.xlsx", b"sheet", "2026-09-05T10:00:00+00:00")
    write_file(tmp_path, "~$Q3 plan.pptx", b"lock", "2026-09-21T10:00:00+00:00")
    return tmp_path


def test_files_are_selected_by_modified_date_and_type(synced):
    listing = LocalFolder(synced).list_files(START, END)

    assert [(f.path, f.kind) for f in listing.files] == [
        ("Minutes/Kickoff.docx", "Word document"),
        ("Q3 plan.pptx", "PowerPoint deck"),
    ]
    assert [s.file.name for s in listing.skipped] == ["Old deck.ppt"]
    kickoff = listing.files[0]
    assert kickoff.modified == datetime(2026, 9, 2, 8, 30, tzinfo=UTC)
    assert kickoff.web_url == (synced.resolve() / "Minutes" / "Kickoff.docx").as_uri()


def test_subfolders_can_be_skipped(synced):
    assert [f.name for f in LocalFolder(synced).list_files(START, END, recursive=False).files] == ["Q3 plan.pptx"]


def test_download_reads_the_file(synced):
    folder = LocalFolder(synced)
    deck = next(f for f in folder.list_files(START, END).files if f.name == "Q3 plan.pptx")
    assert folder.download(deck) == (synced / "Q3 plan.pptx").read_bytes()


def test_a_missing_folder_is_reported(tmp_path):
    with pytest.raises(ConfigError, match="Local folder not found"):
        LocalFolder(tmp_path / "nope")


def test_created_dates_are_only_available_from_sharepoint(synced):
    with pytest.raises(ConfigError, match="only works when reading SharePoint directly"):
        LocalFolder(synced).list_files(START, END, date_field="created")
