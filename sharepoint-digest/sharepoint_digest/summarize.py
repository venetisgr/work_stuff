"""Map-reduce summarization: each document on its own, then all the summaries together as a digest."""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime

from .llm import ChatModel
from .prompts import (
    BATCH_NOTES_PROMPT,
    DIGEST_INTRO,
    DIGEST_INTRO_FROM_NOTES,
    DIGEST_PROMPT,
    DOCUMENT_PART_PROMPT,
    DOCUMENT_PROMPT,
    MERGE_PARTS_PROMPT,
    SYSTEM_PROMPT,
)
from .sharepoint import DriveFile

NO_TEXT_SUMMARY = "_No text could be extracted from this file (it may contain only images)._"


@dataclass(frozen=True)
class DocumentSummary:
    file: DriveFile
    text: str  # Markdown, following prompts.SUMMARY_FORMAT
    source_chars: int  # length of the extracted text
    model_calls: int


def summarize_document(
    model: ChatModel, file: DriveFile, text: str, *, folder_label: str, max_input_chars: int
) -> DocumentSummary:
    """Summarize one document. Text longer than max_input_chars is summarized in parts, then merged."""
    if not text.strip():
        return DocumentSummary(file, NO_TEXT_SUMMARY, 0, 0)

    details = {
        "kind": file.kind or "document",
        "name": file.name,
        "path": file.path,
        "folder": folder_label,
        "modified": day(file.modified),
        "modified_by": file.modified_by or "unknown",
    }
    if len(text) <= max_input_chars:
        summary = model.complete(SYSTEM_PROMPT, DOCUMENT_PROMPT.format(text=text, **details))
        return DocumentSummary(file, summary, len(text), 1)

    parts = split_text(text, max_input_chars)
    notes = [
        model.complete(SYSTEM_PROMPT, DOCUMENT_PART_PROMPT.format(text=part, index=index, total=len(parts), **details))
        for index, part in enumerate(parts, start=1)
    ]
    merged = "\n\n".join(f'<notes part="{index}">\n{note}\n</notes>' for index, note in enumerate(notes, start=1))
    summary = model.complete(SYSTEM_PROMPT, MERGE_PARTS_PROMPT.format(notes=merged, total=len(parts), **details))
    return DocumentSummary(file, summary, len(text), len(parts) + 1)


def build_digest(
    model: ChatModel, summaries: list[DocumentSummary], *, folder_label: str, period: str, max_input_chars: int
) -> str:
    """Roll the per-document summaries up into one digest.

    If the summaries don't fit in one request, each batch is condensed into notes first and the
    digest is written from the notes.
    """
    blocks = [_summary_block(summary) for summary in summaries]
    count = len(summaries)
    intro = DIGEST_INTRO.format(count=count, folder=folder_label, period=period)
    batches = _batches(blocks, max_input_chars)
    if len(batches) > 1:
        notes = [
            model.complete(
                SYSTEM_PROMPT,
                BATCH_NOTES_PROMPT.format(
                    count=len(batch),
                    total=count,
                    folder=folder_label,
                    period=period,
                    index=index,
                    batches=len(batches),
                    material="\n\n".join(batch),
                ),
            )
            for index, batch in enumerate(batches, start=1)
        ]
        blocks = [f'<notes batch="{index}">\n{text}\n</notes>' for index, text in enumerate(notes, start=1)]
        intro = DIGEST_INTRO_FROM_NOTES.format(count=count, folder=folder_label, period=period, batches=len(batches))
    return model.complete(SYSTEM_PROMPT, DIGEST_PROMPT.format(intro=intro, material="\n\n".join(blocks)))


def _summary_block(summary: DocumentSummary) -> str:
    file = summary.file
    attributes = {
        "file": file.name,
        "path": file.path,
        "type": file.kind or "document",
        "modified": day(file.modified),
        "by": file.modified_by or "unknown",
    }
    rendered = " ".join(f'{key}="{html.escape(value)}"' for key, value in attributes.items())
    return f"<summary {rendered}>\n{summary.text}\n</summary>"


def _batches(blocks: list[str], limit: int) -> list[list[str]]:
    """Group blocks, in order, so that each group's combined length stays within limit."""
    batches: list[list[str]] = []
    current: list[str] = []
    size = 0
    for block in blocks:
        if current and size + len(block) > limit:
            batches.append(current)
            current, size = [], 0
        current.append(block)
        size += len(block) + 2  # the blank line that joins blocks
    if current:
        batches.append(current)
    return batches


def split_text(text: str, limit: int) -> list[str]:
    """Split text into chunks of at most limit characters, preferring blank lines, then line breaks."""
    if len(text) <= limit:
        return [text]
    for separator in ("\n\n", "\n"):
        pieces = text.split(separator)
        if len(pieces) > 1:
            break
    else:
        return [text[i : i + limit] for i in range(0, len(text), limit)]

    chunks: list[str] = []
    current = ""
    for piece in pieces:
        candidate = f"{current}{separator}{piece}" if current else piece
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        if len(piece) <= limit:
            current = piece
        else:  # a single oversized section: split it on the next-finer boundary
            chunks.extend(split_text(piece, limit))
            current = ""
    if current:
        chunks.append(current)
    return chunks


def day(moment: datetime) -> str:
    """A timestamp as a date in the local time zone, e.g. 2026-09-27."""
    return f"{moment.astimezone():%Y-%m-%d}"
