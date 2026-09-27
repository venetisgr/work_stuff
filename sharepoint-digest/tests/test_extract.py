import pytest
from conftest import make_docx, make_pptx

from sharepoint_digest.extract import ExtractionError, docx_text, extract_text, pptx_text


def test_pptx_text_keeps_slide_structure_tables_charts_and_notes():
    text = pptx_text(make_pptx())

    slide_1, slide_2 = text.split("\n\n")
    assert slide_1.splitlines() == [
        "## Slide 1: Q3 plan",
        "- Revenue up 12%",
        "  - EMEA drove most of the growth",
        "Speaker notes: Mention the hiring freeze",
    ]
    lines = slide_2.splitlines()
    assert lines[0] == "## Slide 2: Numbers"
    assert "Region | Sales" in lines
    assert "EMEA | 4.2M" in lines
    assert "[Chart: Revenue by quarter]" in lines
    assert "  2026: Q1 = 1.5; Q2 = 2" in lines
    assert "- Grouped callout" in lines
    assert "Slide 3" not in text  # the blank slide is left out


def test_docx_text_keeps_document_order_headings_lists_and_tables():
    assert docx_text(make_docx()).splitlines() == [
        "# Project Alpha status",
        "## Summary",
        "We are on track for the October launch.",
        "- Finalize vendor contract",
        "Milestone | Date",
        "Beta | Internal | 2026-10-01",
        "Next review in two weeks.",
    ]


def test_extract_text_picks_the_parser_by_extension():
    assert extract_text(make_docx(), ".docx").startswith("# Project Alpha status")
    assert extract_text(make_pptx(), ".pptx").startswith("## Slide 1: Q3 plan")


def test_unreadable_files_raise_extraction_error():
    with pytest.raises(ExtractionError, match="password-protected"):
        extract_text(b"not a zip file", ".pptx")


def test_parser_failures_inside_a_readable_file_are_reported_as_such(monkeypatch):
    from sharepoint_digest import extract

    def broken_lines(shape):
        raise KeyError("rId7")

    monkeypatch.setattr(extract, "_shape_lines", broken_lines)
    with pytest.raises(ExtractionError, match=r"Couldn't read the file's content \(KeyError: 'rId7'\)"):
        extract_text(make_pptx(), ".pptx")


def test_unsupported_extension_is_a_programming_error():
    with pytest.raises(ValueError):
        extract_text(b"", ".pdf")
