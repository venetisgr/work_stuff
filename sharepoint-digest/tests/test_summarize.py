from datetime import UTC, datetime

from conftest import FakeModel

from sharepoint_digest.sharepoint import DriveFile
from sharepoint_digest.summarize import (
    NO_TEXT_SUMMARY,
    DocumentSummary,
    build_digest,
    split_text,
    summarize_document,
)


def drive_file(name="Q3 plan.pptx", path="Q3 plan.pptx") -> DriveFile:
    moment = datetime(2026, 9, 20, 10, tzinfo=UTC)
    return DriveFile("id-" + name, name, path, "https://example/" + name, 1, moment, moment, "Ann", "Ann")


def test_split_text_prefers_blank_lines_then_line_breaks():
    slides = [f"## Slide {n}\n- point {n}" for n in range(1, 7)]
    text = "\n\n".join(slides)

    chunks = split_text(text, limit=45)  # room for two 20-character slides per chunk

    assert chunks == ["\n\n".join(slides[i : i + 2]) for i in (0, 2, 4)]


def test_split_text_splits_an_oversized_section_on_line_breaks():
    section = "\n".join(f"- bullet {n}" for n in range(1, 9))  # 8 lines of 10 characters
    chunks = split_text(f"## Intro\n\n{section}", limit=35)

    assert chunks[0] == "## Intro"
    assert all(len(chunk) <= 35 for chunk in chunks)
    assert "\n".join(chunks[1:]) == section


def test_split_text_hard_splits_a_single_huge_line():
    assert split_text("x" * 25, limit=10) == ["x" * 10, "x" * 10, "x" * 5]
    assert split_text("short", limit=10) == ["short"]


def test_short_documents_take_one_call_with_the_text_and_details():
    model = FakeModel()

    summary = summarize_document(
        model, drive_file(), "## Slide 1: Q3 plan", folder_label="Temp Folder 1", max_input_chars=1000
    )

    assert summary.text == "**Overview:** Summary number 1."
    assert (summary.model_calls, summary.source_chars) == (1, len("## Slide 1: Q3 plan"))
    prompt = model.prompts[0]
    assert 'Summarize this PowerPoint deck from the "Temp Folder 1" folder.' in prompt
    assert "<document>\n## Slide 1: Q3 plan\n</document>" in prompt
    assert "Last modified: 2026-09-20 by Ann" in prompt
    assert "**Action items:**" in prompt


def test_long_documents_are_summarized_in_parts_then_merged():
    model = FakeModel()
    text = "\n\n".join(f"## Slide {n}\n- detail {n}" for n in range(1, 11))

    summary = summarize_document(model, drive_file(), text, folder_label="Temp Folder 1", max_input_chars=60)

    parts = len(split_text(text, 60))
    assert parts > 1
    assert summary.model_calls == parts + 1
    assert all(f"This is part {n} of {parts}." in model.prompts[n - 1] for n in range(1, parts + 1))
    merge_prompt = model.prompts[-1]
    assert f"Below are notes on each of the {parts} parts" in merge_prompt
    assert '<notes part="1">\n**Overview:** Summary number 1.\n</notes>' in merge_prompt


def test_documents_without_text_skip_the_model():
    model = FakeModel()
    summary = summarize_document(model, drive_file(), "  \n", folder_label="F", max_input_chars=100)
    assert summary.text == NO_TEXT_SUMMARY
    assert model.prompts == []


def summaries(count: int) -> list[DocumentSummary]:
    return [
        DocumentSummary(drive_file(f"Doc {n}.docx", f"Doc {n}.docx"), f"Summary of doc {n}.", 100, 1)
        for n in range(1, count + 1)
    ]


def test_digest_from_all_summaries_in_one_call():
    model = FakeModel()

    digest = build_digest(
        model, summaries(2), folder_label="Temp Folder 1", period="modified on 20 Sep 2026", max_input_chars=10_000
    )

    assert digest.startswith("## Highlights")
    assert len(model.prompts) == 1
    prompt = model.prompts[0]
    assert (
        'Below are summaries of 2 documents from the "Temp Folder 1" SharePoint folder, modified on 20 Sep 2026.'
        in prompt
    )
    assert '<summary file="Doc 1.docx" path="Doc 1.docx"' in prompt
    assert "Summary of doc 2." in prompt


def test_digest_condenses_batches_first_when_summaries_do_not_fit():
    model = FakeModel()

    build_digest(model, summaries(5), folder_label="F", period="modified today", max_input_chars=400)

    *batch_prompts, final_prompt = model.prompts
    assert len(batch_prompts) > 1
    assert all(f"of {len(batch_prompts)})" in prompt for prompt in batch_prompts)
    assert f"condensed in {len(batch_prompts)} batches" in final_prompt
    assert '<notes batch="1">' in final_prompt
