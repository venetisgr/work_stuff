"""The end-to-end run: find the files in SharePoint, summarize each one, then write the digest."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from .config import ConfigError, Folder, Settings
from .extract import extract_text
from .llm import ChatModel, FoundryChatModel, LLMError, LLMSetupError
from .sharepoint import DriveFile, GraphClient, SharePointFolder, SkippedFile, graph_credential
from .summarize import DocumentSummary, build_digest, summarize_document

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DateRange:
    """Whole days in the local time zone, both ends included."""

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ConfigError(f"The start date ({self.start}) is after the end date ({self.end}).")

    def bounds(self) -> tuple[datetime, datetime]:
        """[start, end) as timezone-aware datetimes: local midnight to midnight after the last day."""
        start = datetime.combine(self.start, time.min).astimezone()
        end = datetime.combine(self.end + timedelta(days=1), time.min).astimezone()
        return start, end

    def describe(self) -> str:
        if self.start == self.end:
            return f"on {_long_date(self.start)}"
        return f"between {_long_date(self.start)} and {_long_date(self.end)}"


def _long_date(day: date) -> str:
    return f"{day.day} {day:%b %Y}"


@dataclass
class DigestRun:
    folder: Folder
    date_range: DateRange
    date_field: str
    files: list[DriveFile] = field(default_factory=list)  # files in the range that we can read
    summaries: list[DocumentSummary] = field(default_factory=list)
    skipped: list[SkippedFile] = field(default_factory=list)
    digest: str | None = None
    digest_error: str | None = None
    model_name: str | None = None

    @property
    def period(self) -> str:
        """E.g. "modified between 1 Sep 2026 and 27 Sep 2026"."""
        verb = "created" if self.date_field == "created" else "modified"
        return f"{verb} {self.date_range.describe()}"


def open_folder(settings: Settings, folder: Folder) -> SharePointFolder:
    site_url = folder.site_url or settings.sharepoint.site_url
    if not site_url:
        raise ConfigError("Set SHAREPOINT_SITE_URL in .env (or site_url for this folder in folders.toml).")
    graph = GraphClient(graph_credential(settings.sharepoint))
    return SharePointFolder.resolve(graph, site_url, folder.library or settings.sharepoint.library, folder.path)


def run_digest(
    settings: Settings,
    folder: Folder,
    date_range: DateRange,
    *,
    date_field: str = "modified",
    recursive: bool = True,
    workers: int = 4,
    dry_run: bool = False,
    location: SharePointFolder | None = None,
    model: ChatModel | None = None,
) -> DigestRun:
    """List the folder's PowerPoint and Word files in the date range, summarize them and write a digest.

    With dry_run, stops after listing the files. `location` and `model` can be injected for testing.
    """
    run = DigestRun(folder, date_range, date_field)
    location = location or open_folder(settings, folder)
    start, end = date_range.bounds()
    listing = location.list_files(start, end, date_field=date_field, recursive=recursive)
    run.files, run.skipped = listing.files, list(listing.skipped)
    log.info("Found %d PowerPoint/Word file(s) %s in %s.", len(run.files), run.period, folder.label)
    if dry_run or not run.files:
        return run

    model = model or FoundryChatModel(settings.foundry)
    run.model_name = model.deployment
    run.summaries, failures = _summarize_files(
        model, location, run.files, folder_label=folder.label, max_input_chars=settings.max_input_chars, workers=workers
    )
    run.skipped.extend(failures)
    if not run.summaries:
        run.digest_error = "none of the files could be summarized"
        return run

    log.info("Writing the digest from %d summaries...", len(run.summaries))
    try:
        run.digest = build_digest(
            model,
            run.summaries,
            folder_label=folder.label,
            period=run.period,
            max_input_chars=settings.max_input_chars,
        )
    except (LLMError, LLMSetupError) as exc:  # keep the summaries we already paid for
        log.error("Couldn't write the digest: %s", exc)
        run.digest_error = str(exc)
    return run


def _summarize_files(
    model: ChatModel,
    location: SharePointFolder,
    files: list[DriveFile],
    *,
    folder_label: str,
    max_input_chars: int,
    workers: int,
) -> tuple[list[DocumentSummary], list[SkippedFile]]:
    def summarize(file: DriveFile) -> DocumentSummary:
        text = extract_text(location.download(file), file.extension)
        return summarize_document(model, file, text, folder_label=folder_label, max_input_chars=max_input_chars)

    summaries: list[DocumentSummary] = []
    failures: list[SkippedFile] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(summarize, file): file for file in files}
        for done, future in enumerate(as_completed(futures), start=1):
            file = futures[future]
            try:
                summaries.append(future.result())
            except LLMSetupError:
                pool.shutdown(cancel_futures=True)
                raise
            except Exception as exc:  # one unreadable file shouldn't sink the whole digest
                log.warning("[%d/%d] Skipped %s: %s", done, len(files), file.path, exc)
                log.debug("Details for %s", file.path, exc_info=True)
                failures.append(SkippedFile(file, str(exc)))
            else:
                log.info("[%d/%d] Summarized %s", done, len(files), file.path)

    order = {file.id: index for index, file in enumerate(files)}
    summaries.sort(key=lambda summary: order[summary.file.id])
    failures.sort(key=lambda skipped: order[skipped.file.id])
    return summaries, failures
