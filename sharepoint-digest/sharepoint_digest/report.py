"""Write a run out as Markdown (to read and share) and JSON (for other tools)."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .pipeline import DigestRun
from .sharepoint import DriveFile
from .summarize import day


def write_report(run: DigestRun, output_dir: Path) -> tuple[Path, Path]:
    """Write <folder>_<start>_to_<end>_digest.md and ..._summaries.json; return both paths."""
    output_dir.mkdir(parents=True, exist_ok=True)
    folder_slug = re.sub(r"[^A-Za-z0-9._-]+", "-", run.folder.key).strip("-") or "folder"
    stem = f"{folder_slug}_{run.date_range.start:%Y-%m-%d}_to_{run.date_range.end:%Y-%m-%d}"
    generated = datetime.now().astimezone()

    markdown_path = output_dir / f"{stem}_digest.md"
    markdown_path.write_text(render_markdown(run, generated), encoding="utf-8")
    json_path = output_dir / f"{stem}_summaries.json"
    json_path.write_text(json.dumps(to_json(run, generated), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return markdown_path, json_path


def render_markdown(run: DigestRun, generated: datetime) -> str:
    count = len(run.summaries)
    lines = [
        f"# {run.folder.label} digest",
        "",
        f"_{count} document{'' if count == 1 else 's'} {run.period} · "
        f"generated {generated:%Y-%m-%d %H:%M} with {run.model_name}_",
        "",
        run.digest.strip() if run.digest else f"_The digest couldn't be written: {run.digest_error}._",
        "",
        "---",
        "",
        "## Documents",
        "",
        "| # | File | Type | Modified | Modified by |",
        "|---|---|---|---|---|",
    ]
    for number, summary in enumerate(run.summaries, start=1):
        file = summary.file
        lines.append(
            f"| {number} | {_link(file)} | {file.kind} | {day(file.modified)} | {_escape(file.modified_by or '')} |"
        )

    if run.skipped:
        lines += ["", "## Not included", ""]
        lines += [f"- {_link(item.file)}: {item.reason}" for item in run.skipped]

    lines += ["", "## Individual summaries"]
    for number, summary in enumerate(run.summaries, start=1):
        file = summary.file
        by = f" by {file.modified_by}" if file.modified_by else ""
        lines += [
            "",
            f"### {number}. {_link(file)}",
            "",
            f"_{_escape(file.path)} · modified {day(file.modified)}{by}_",
            "",
            summary.text.strip(),
        ]
    return "\n".join(lines) + "\n"


def to_json(run: DigestRun, generated: datetime) -> dict[str, Any]:
    return {
        "folder": {"key": run.folder.key, "label": run.folder.label, "path": run.folder.path},
        "date_field": run.date_field,
        "start": run.date_range.start.isoformat(),
        "end": run.date_range.end.isoformat(),
        "generated_at": generated.isoformat(timespec="seconds"),
        "model": run.model_name,
        "digest": run.digest,
        "digest_error": run.digest_error,
        "documents": [
            {
                **_file_json(summary.file),
                "summary": summary.text,
                "extracted_chars": summary.source_chars,
                "model_calls": summary.model_calls,
            }
            for summary in run.summaries
        ],
        "not_included": [{**_file_json(item.file), "reason": item.reason} for item in run.skipped],
    }


def _file_json(file: DriveFile) -> dict[str, Any]:
    return {
        "name": file.name,
        "path": file.path,
        "type": file.kind,
        "url": file.web_url,
        "size": file.size,
        "created": file.created.isoformat(),
        "created_by": file.created_by,
        "modified": file.modified.isoformat(),
        "modified_by": file.modified_by,
    }


def _link(file: DriveFile) -> str:
    name = _escape(file.name)
    return f"[{name}](<{file.web_url}>)" if file.web_url else name


def _escape(text: str) -> str:
    """Escape characters that would break Markdown links or table cells."""
    return re.sub(r"([\[\]|])", r"\\\1", text)
