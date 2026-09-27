from datetime import date, timedelta

import pytest
from conftest import FakeModel, make_pptx, write_file
from test_pipeline import sample_location

from sharepoint_digest import cli, pipeline

SEPTEMBER = ["--start", "2026-09-01", "--end", "2026-09-30"]


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    """Keep the developer's .env and shell settings out of the tests."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setattr(cli.truststore, "inject_into_ssl", lambda: None)
    for name in ("FOLDERS_FILE", "FOUNDRY_ENDPOINT", "FOUNDRY_DEPLOYMENT", "FOUNDRY_API_KEY", "SHAREPOINT_SITE_URL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def model(monkeypatch):
    fake = FakeModel()
    monkeypatch.setattr(pipeline, "FoundryChatModel", lambda settings: fake)
    return fake


@pytest.fixture
def sharepoint(monkeypatch):
    """Stand in for the SharePoint folder the online version would open."""
    monkeypatch.setattr(cli, "open_sharepoint_folder", lambda settings, folder: sample_location())


def folders_file(tmp_path, content: str):
    path = tmp_path / "folders.toml"
    path.write_text(content)
    return str(path)


# --- Version 2: online ------------------------------------------------------------------


def test_online_lists_the_sharepoint_folders(capsys):
    assert cli.main_online(["--list-folders"]) == 0
    output = capsys.readouterr().out
    assert "1. Temp Folder 1  (--folder temp-folder-1, path: /Temp Folder 1)" in output
    assert "4. Temp Folder 4" in output


def test_online_rejects_an_unknown_folder(capsys):
    assert cli.main_online(["--folder", "finance"]) == 1
    assert "Unknown folder 'finance'" in capsys.readouterr().err


def test_reversed_dates_are_an_error(capsys):
    assert cli.main_online(["--folder", "1", "--start", "2026-09-10", "--end", "2026-09-01"]) == 1
    assert "after the end date" in capsys.readouterr().err


def test_start_and_days_cannot_be_combined():
    with pytest.raises(SystemExit):
        cli.main_online(["--folder", "1", "--start", "2026-09-01", "--days", "3"])


def test_days_counts_back_from_the_end_date():
    args = cli._parser(local=False).parse_args(["--days", "7", "--end", "2026-09-27"])
    assert cli._date_range(args) == pipeline.DateRange(date(2026, 9, 21), date(2026, 9, 27))
    default = cli._date_range(cli._parser(local=False).parse_args([]))
    assert default.end - default.start == timedelta(days=cli.DEFAULT_DAYS - 1)


def test_online_dry_run_lists_the_files(sharepoint, model, capsys):
    assert cli.main_online(["--folder", "temp-folder-1", *SEPTEMBER, "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "Temp Folder 1: 3 file(s) modified between 1 Sep 2026 and 30 Sep 2026 would be summarized." in output
    assert "Q3 plan.pptx" in output and "Old notes.doc" in output
    assert model.prompts == []


def test_online_run_writes_the_digest(sharepoint, model, tmp_path, capsys):
    code = cli.main_online(["--folder", "Temp Folder 1", *SEPTEMBER, "--output-dir", str(tmp_path / "out")])

    output = capsys.readouterr().out
    assert code == 0
    digest = tmp_path / "out" / "temp-folder-1_2026-09-01_to_2026-09-30_digest.md"
    assert digest.exists()
    assert f"Digest: {digest}" in output
    assert "2 of 3 file(s) summarized." in output


# --- Version 1: local -------------------------------------------------------------------


def test_local_takes_a_folder_path_and_needs_no_folders_toml(model, tmp_path, capsys):
    synced = tmp_path / "Board packs"
    write_file(synced, "Q3 plan.pptx", make_pptx(), "2026-09-20T10:00:00+00:00")

    code = cli.main_local(
        ["--folder", str(synced), *SEPTEMBER, "--output-dir", str(tmp_path / "out")]
        + ["--folders-file", str(tmp_path / "missing.toml")]
    )

    assert code == 0
    assert (tmp_path / "out" / "Board-packs_2026-09-01_to_2026-09-30_digest.md").exists()
    assert "1 of 1 file(s) summarized." in capsys.readouterr().out


def test_local_uses_the_local_path_of_a_configured_folder(tmp_path, capsys):
    synced = tmp_path / "synced"
    write_file(synced, "Q3 plan.pptx", b"deck", "2026-09-20T10:00:00+00:00")
    config = folders_file(tmp_path, f"[folders.board]\nlabel = 'Board'\nlocal_path = '{synced}'\n")

    assert cli.main_local(["--folder", "board", "--folders-file", config, *SEPTEMBER, "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "Board: 1 file(s) modified between 1 Sep 2026 and 30 Sep 2026 would be summarized." in output


def test_local_explains_a_folder_without_a_local_path(capsys):
    assert cli.main_local(["--folder", "temp-folder-1"]) == 1
    assert '"Temp Folder 1" has no local_path in folders.toml' in capsys.readouterr().err


def test_local_needs_a_folder_when_none_has_a_local_path(capsys):
    assert cli.main_local([]) == 1
    assert "Pass the folder's path with --folder" in capsys.readouterr().err


def test_local_reports_a_missing_folder_path(tmp_path, capsys):
    assert cli.main_local(["--folder", str(tmp_path / "nope")]) == 1
    assert "Local folder not found" in capsys.readouterr().err


def test_local_lists_folders_with_their_local_paths(tmp_path, capsys):
    config = folders_file(
        tmp_path,
        "[folders.board]\nlabel = 'Board'\nlocal_path = 'C:\\Users\\me\\Board'\n\n"
        "[folders.hr]\nlabel = 'HR'\npath = 'HR'\n",
    )
    assert cli.main_local(["--list-folders", "--folders-file", config]) == 0
    output = capsys.readouterr().out
    assert "1. Board  (--folder board, local: C:\\Users\\me\\Board)" in output
    assert "2. HR  (--folder hr, no local_path set)" in output


def test_local_has_no_created_date_option():
    with pytest.raises(SystemExit):
        cli.main_local(["--folder", ".", "--date-field", "created"])
