"""Command line entry point: python -m sharepoint_digest --help"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import truststore
from dotenv import load_dotenv

from .config import PROJECT_ROOT, ConfigError, Folder, default_folders_file, find_folder, load_folders, load_settings
from .llm import LLMSetupError
from .pipeline import DateRange, DigestRun, run_digest
from .report import write_report
from .sharepoint import GraphError
from .summarize import day

log = logging.getLogger("sharepoint_digest")

DEFAULT_DAYS = 7


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    _setup_logging(args.verbose)
    # Trust the operating system's certificates, so corporate TLS inspection proxies work out of the box.
    truststore.inject_into_ssl()
    load_dotenv(Path.cwd() / ".env")
    load_dotenv(PROJECT_ROOT / ".env")  # doesn't override anything already set

    try:
        if args.list_folders:
            for number, folder in enumerate(load_folders(args.folders_file or default_folders_file()), start=1):
                where = f"local: {folder.local_path}" if folder.local_path else f"path: /{folder.path}"
                print(f"{number}. {folder.label}  (--folder {folder.key}, {where})")
            return 0
        folder = _choose_folder(args)
        date_range = _date_range(args)
        run = run_digest(
            load_settings(),
            folder,
            date_range,
            date_field=args.date_field,
            recursive=not args.no_subfolders,
            workers=args.workers,
            dry_run=args.dry_run,
        )
    except (ConfigError, GraphError, LLMSetupError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130

    if args.dry_run:
        _print_listing(run)
        return 0
    if not run.files:
        print(f"No PowerPoint or Word files were {run.period} in {folder.label}.")
        _print_skipped(run)
        return 0

    markdown_path, json_path = write_report(run, args.output_dir)
    print(f"\nDigest: {markdown_path}\nSummaries (JSON): {json_path}")
    print(f"{len(run.summaries)} of {len(run.files)} file(s) summarized.")
    _print_skipped(run)
    return 0 if run.digest else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sharepoint-digest",
        description=(
            "Summarize the PowerPoint and Word files in a SharePoint folder that changed in a date range, "
            "then combine the summaries into one digest, using a GPT model in Azure AI Foundry."
        ),
    )
    parser.add_argument("--folder", help="folder key, label or number from folders.toml (asks if omitted)")
    when = parser.add_mutually_exclusive_group()
    when.add_argument("--start", type=_iso_date, metavar="YYYY-MM-DD", help="first day of the range")
    when.add_argument(
        "--days", type=_positive_int, metavar="N", help=f"the last N days up to --end (default: {DEFAULT_DAYS})"
    )
    parser.add_argument("--end", type=_iso_date, metavar="YYYY-MM-DD", help="last day of the range (default: today)")
    parser.add_argument(
        "--date-field",
        choices=("modified", "created"),
        default="modified",
        help="which file date the range applies to (default: modified)",
    )
    parser.add_argument(
        "--local-dir",
        type=Path,
        metavar="PATH",
        help="read the files from this folder on your computer (e.g. a OneDrive-synced copy) instead of SharePoint",
    )
    parser.add_argument("--no-subfolders", action="store_true", help="don't look inside subfolders")
    parser.add_argument("--workers", type=_positive_int, default=4, help="files summarized in parallel (default: 4)")
    parser.add_argument(
        "--output-dir", type=Path, default=Path("output"), help="where to write the digest (default: ./output)"
    )
    parser.add_argument("--folders-file", type=Path, help="folder list to use (default: folders.toml)")
    parser.add_argument("--list-folders", action="store_true", help="show the configured folders and exit")
    parser.add_argument(
        "--dry-run", action="store_true", help="list the matching files without downloading or summarizing them"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="show debug logging")
    return parser


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a date like 2026-09-01, got {value!r}") from None


def _positive_int(value: str) -> int:
    if not value.isdigit() or int(value) < 1:
        raise argparse.ArgumentTypeError(f"expected a whole number of at least 1, got {value!r}")
    return int(value)


def _date_range(args: argparse.Namespace) -> DateRange:
    end = args.end or date.today()
    if args.start:
        return DateRange(args.start, end)
    return DateRange(end - timedelta(days=(args.days or DEFAULT_DAYS) - 1), end)


def _choose_folder(args: argparse.Namespace) -> Folder:
    if args.local_dir and not args.folder:  # an ad-hoc local folder; folders.toml isn't needed
        name = args.local_dir.expanduser().resolve().name or "local"
        return Folder(key=name, label=name, path="", local_path=str(args.local_dir))
    folders = load_folders(args.folders_file or default_folders_file())
    folder = find_folder(folders, args.folder) if args.folder else _ask_for_folder(folders)
    return replace(folder, local_path=str(args.local_dir)) if args.local_dir else folder


def _ask_for_folder(folders: list[Folder]) -> Folder:
    if not sys.stdin.isatty():
        raise ConfigError(f"Pass --folder (one of: {', '.join(f.key for f in folders)}).")
    print("Which folder should the digest cover?")
    for number, folder in enumerate(folders, start=1):
        print(f"  {number}. {folder.label}")
    while True:
        answer = input(f"Folder [1-{len(folders)}]: ")
        try:
            return find_folder(folders, answer)
        except ConfigError:
            print("Please enter one of the numbers above.")


def _print_listing(run: DigestRun) -> None:
    print(f"{run.folder.label}: {len(run.files)} file(s) {run.period} would be summarized.")
    for file in run.files:
        print(f"  {day(file.timestamp(run.date_field))}  {file.path}")
    _print_skipped(run)


def _print_skipped(run: DigestRun) -> None:
    if run.skipped:
        print(f"{len(run.skipped)} file(s) not included:")
        for item in run.skipped:
            print(f"  {item.file.path}: {item.reason}")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(format="%(message)s", level=logging.WARNING)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
