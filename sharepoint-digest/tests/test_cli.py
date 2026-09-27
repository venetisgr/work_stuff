from datetime import date, timedelta

import pytest
from conftest import FakeModel, make_pptx, write_file
from test_pipeline import sample_location

from sharepoint_digest import cli, pipeline


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    """Keep the developer's .env and shell settings out of the tests."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setattr(cli.truststore, "inject_into_ssl", lambda: None)
    for name in ("FOLDERS_FILE", "FOUNDRY_ENDPOINT", "FOUNDRY_DEPLOYMENT", "FOUNDRY_API_KEY", "SHAREPOINT_SITE_URL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def fake_services(monkeypatch):
    model = FakeModel()
    monkeypatch.setattr(pipeline, "open_folder", lambda settings, folder: sample_location())
    monkeypatch.setattr(pipeline, "FoundryChatModel", lambda settings: model)
    return model


def test_list_folders(capsys):
    assert cli.main(["--list-folders"]) == 0
    output = capsys.readouterr().out
    assert "1. Temp Folder 1  (--folder temp-folder-1, path: /Temp Folder 1)" in output
    assert "4. Temp Folder 4" in output


def test_unknown_folder_is_an_error(capsys):
    assert cli.main(["--folder", "finance"]) == 1
    assert "Unknown folder 'finance'" in capsys.readouterr().err


def test_reversed_dates_are_an_error(capsys):
    assert cli.main(["--folder", "1", "--start", "2026-09-10", "--end", "2026-09-01"]) == 1
    assert "after the end date" in capsys.readouterr().err


def test_start_and_days_cannot_be_combined():
    with pytest.raises(SystemExit):
        cli.main(["--folder", "1", "--start", "2026-09-01", "--days", "3"])


def test_days_counts_back_from_the_end_date():
    args = cli._parser().parse_args(["--days", "7", "--end", "2026-09-27"])
    assert cli._date_range(args) == pipeline.DateRange(date(2026, 9, 21), date(2026, 9, 27))
    default = cli._date_range(cli._parser().parse_args([]))
    assert default.end - default.start == timedelta(days=cli.DEFAULT_DAYS - 1)


def test_dry_run_lists_the_files(fake_services, capsys):
    code = cli.main(["--folder", "temp-folder-1", "--start", "2026-09-01", "--end", "2026-09-30", "--dry-run"])
    output = capsys.readouterr().out
    assert code == 0
    assert "Temp Folder 1: 3 file(s) modified between 1 Sep 2026 and 30 Sep 2026 would be summarized." in output
    assert "Q3 plan.pptx" in output and "Old notes.doc" in output
    assert fake_services.prompts == []


def test_full_run_writes_the_digest(fake_services, tmp_path, capsys):
    code = cli.main(
        [
            "--folder",
            "Temp Folder 1",
            "--start",
            "2026-09-01",
            "--end",
            "2026-09-30",
            "--output-dir",
            str(tmp_path / "out"),
        ]
    )
    output = capsys.readouterr().out
    assert code == 0
    digest = tmp_path / "out" / "temp-folder-1_2026-09-01_to_2026-09-30_digest.md"
    assert digest.exists()
    assert f"Digest: {digest}" in output
    assert "2 of 3 file(s) summarized." in output


def test_local_dir_needs_neither_sharepoint_nor_folders_toml(tmp_path, monkeypatch, capsys):
    model = FakeModel()
    monkeypatch.setattr(pipeline, "FoundryChatModel", lambda settings: model)
    synced = tmp_path / "Board packs"
    write_file(synced, "Q3 plan.pptx", make_pptx(), "2026-09-20T10:00:00+00:00")

    code = cli.main(
        ["--local-dir", str(synced), "--start", "2026-09-01", "--end", "2026-09-30"]
        + ["--output-dir", str(tmp_path / "out"), "--folders-file", str(tmp_path / "missing.toml")]
    )

    assert code == 0
    assert (tmp_path / "out" / "Board-packs_2026-09-01_to_2026-09-30_digest.md").exists()
    assert "1 of 1 file(s) summarized." in capsys.readouterr().out


def test_local_dir_can_stand_in_for_a_configured_folder(tmp_path, capsys):
    write_file(tmp_path, "Q3 plan.pptx", b"deck", "2026-09-20T10:00:00+00:00")
    args = ["--folder", "temp-folder-1", "--local-dir", str(tmp_path), "--start", "2026-09-01", "--end", "2026-09-30"]

    assert cli.main([*args, "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "Temp Folder 1: 1 file(s) modified between 1 Sep 2026 and 30 Sep 2026 would be summarized." in output
    assert "Q3 plan.pptx" in output


def test_list_folders_shows_local_paths(tmp_path, capsys):
    folders = tmp_path / "folders.toml"
    folders.write_text("[folders.board]\nlabel = 'Board'\nlocal_path = 'C:\\Users\\me\\Board'\n")
    assert cli.main(["--list-folders", "--folders-file", str(folders)]) == 0
    assert "1. Board  (--folder board, local: C:\\Users\\me\\Board)" in capsys.readouterr().out
