import json
from datetime import date, datetime, timedelta

import pytest
from conftest import (
    SITE_URL,
    FakeModel,
    children_url,
    content_url,
    fake_graph,
    graph_file,
    make_docx,
    make_pptx,
    make_settings,
    site_routes,
    write_file,
)

from sharepoint_digest.config import ConfigError, Folder
from sharepoint_digest.llm import LLMError, LLMSetupError
from sharepoint_digest.pipeline import DateRange, run_digest
from sharepoint_digest.report import write_report
from sharepoint_digest.sharepoint import SharePointFolder

FOLDER = Folder("temp-folder-1", "Temp Folder 1", "Temp Folder 1")
SEPTEMBER = DateRange(date(2026, 9, 1), date(2026, 9, 30))


def location(files: dict[str, tuple[str, str, bytes]]) -> SharePointFolder:
    """A resolved folder whose children are {item_id: (name, modified, content)}."""
    routes = site_routes()
    routes[children_url("folder-1")] = {
        "value": [graph_file(item_id, name, modified) for item_id, (name, modified, _) in files.items()]
    }
    for item_id, (_, _, content) in files.items():
        routes[content_url(item_id)] = content
    graph, _, _ = fake_graph(routes)
    return SharePointFolder.resolve(graph, SITE_URL, "Documents", "Temp Folder 1")


def sample_location() -> SharePointFolder:
    return location(
        {
            "deck": ("Q3 plan.pptx", "2026-09-20T10:00:00Z", make_pptx()),
            "doc": ("Alpha status.docx", "2026-09-05T10:00:00Z", make_docx()),
            "broken": ("Locked.docx", "2026-09-10T10:00:00Z", b"encrypted"),
            "legacy": ("Old notes.doc", "2026-09-11T10:00:00Z", b""),
            "august": ("August.docx", "2026-08-30T10:00:00Z", make_docx()),
        }
    )


def test_run_summarizes_each_file_then_writes_the_digest(tmp_path):
    model = FakeModel()

    run = run_digest(make_settings(), FOLDER, SEPTEMBER, location=sample_location(), model=model, workers=2)

    assert [s.file.name for s in run.summaries] == ["Alpha status.docx", "Q3 plan.pptx"]  # oldest first
    assert {s.file.name: s.reason.split(";")[0] for s in run.skipped} == {
        "Old notes.doc": ".doc files aren't supported",
        "Locked.docx": "Couldn't open the file",
    }
    assert run.digest.startswith("## Highlights")
    document_prompts, digest_prompt = model.prompts[:-1], model.prompts[-1]
    assert len(document_prompts) == 2
    assert any("## Slide 1: Q3 plan" in prompt for prompt in document_prompts)
    assert "modified between 1 Sep 2026 and 30 Sep 2026" in digest_prompt

    markdown_path, json_path = write_report(run, tmp_path)

    assert markdown_path.name == "temp-folder-1_2026-09-01_to_2026-09-30_digest.md"
    markdown = markdown_path.read_text()
    assert markdown.startswith("# Temp Folder 1 digest\n\n_2 documents modified between 1 Sep 2026 and 30 Sep 2026")
    assert "with gpt-test_" in markdown
    assert "## Highlights\n- Launch is on track" in markdown
    assert (
        f"| 1 | [Alpha status.docx](<{SITE_URL}/Shared%20Documents/Temp%20Folder%201/Alpha%20status.docx>) |"
        in markdown
    )
    assert "## Not included" in markdown
    assert "### 2. [Q3 plan.pptx]" in markdown

    data = json.loads(json_path.read_text())
    assert [d["name"] for d in data["documents"]] == ["Alpha status.docx", "Q3 plan.pptx"]
    assert data["documents"][0]["summary"].startswith("**Overview:**")
    assert {d["name"] for d in data["not_included"]} == {"Old notes.doc", "Locked.docx"}


def test_a_folder_with_a_local_path_is_read_from_disk(tmp_path):
    write_file(tmp_path, "Q3 plan.pptx", make_pptx(), "2026-09-20T10:00:00+00:00")
    write_file(tmp_path, "Minutes/Alpha status.docx", make_docx(), "2026-09-05T10:00:00+00:00")
    folder = Folder("synced", "Synced copy", "", local_path=str(tmp_path))
    model = FakeModel()

    run = run_digest(make_settings(), folder, SEPTEMBER, model=model)

    assert [s.file.path for s in run.summaries] == ["Minutes/Alpha status.docx", "Q3 plan.pptx"]
    assert run.digest.startswith("## Highlights")
    assert any("## Slide 1: Q3 plan" in prompt for prompt in model.prompts)


def test_nothing_in_range_means_no_model_calls():
    model = FakeModel()
    run = run_digest(
        make_settings(), FOLDER, DateRange(date(2025, 1, 1), date(2025, 1, 31)), location=sample_location(), model=model
    )
    assert run.files == [] and run.digest is None
    assert model.prompts == []


def test_dry_run_lists_files_without_summarizing():
    model = FakeModel()
    run = run_digest(make_settings(), FOLDER, SEPTEMBER, location=sample_location(), model=model, dry_run=True)
    assert [f.name for f in run.files] == ["Alpha status.docx", "Locked.docx", "Q3 plan.pptx"]
    assert model.prompts == []


class BrokenModel(FakeModel):
    def __init__(self, error: Exception, fail_on_digest_only: bool = False):
        super().__init__()
        self.error = error
        self.fail_on_digest_only = fail_on_digest_only

    def complete(self, system, prompt):
        if not self.fail_on_digest_only or "Write a digest" in prompt:
            raise self.error
        return super().complete(system, prompt)


def test_setup_errors_stop_the_run():
    with pytest.raises(LLMSetupError):
        run_digest(
            make_settings(), FOLDER, SEPTEMBER, location=sample_location(), model=BrokenModel(LLMSetupError("bad key"))
        )


def test_a_failed_digest_keeps_the_individual_summaries(tmp_path):
    model = BrokenModel(LLMError("content filter"), fail_on_digest_only=True)
    run = run_digest(make_settings(), FOLDER, SEPTEMBER, location=sample_location(), model=model)

    assert len(run.summaries) == 2
    assert run.digest is None and run.digest_error == "content filter"
    markdown_path, _ = write_report(run, tmp_path)
    assert "_The digest couldn't be written: content filter._" in markdown_path.read_text()


def test_date_range_covers_whole_local_days():
    start, end = DateRange(date(2026, 9, 1), date(2026, 9, 30)).bounds()
    assert start == datetime(2026, 9, 1).astimezone()
    assert end == datetime(2026, 10, 1).astimezone()
    assert end - start >= timedelta(days=29, hours=23)  # DST changes can shift an hour


def test_date_range_must_not_run_backwards():
    with pytest.raises(ConfigError, match="after the end date"):
        DateRange(date(2026, 9, 30), date(2026, 9, 1))
