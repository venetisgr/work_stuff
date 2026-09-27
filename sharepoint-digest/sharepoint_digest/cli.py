"""The two command-line versions of the digest.

- local_digest.py (digest-local): reads a folder on this computer, e.g. a OneDrive-synced SharePoint folder.
- online_digest.py (digest-online): reads a SharePoint folder directly, through Microsoft Graph.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

import truststore
from dotenv import load_dotenv

from .config import PROJECT_ROOT, ConfigError, Folder, default_folders_file, find_folder, load_folders, load_settings
from .llm import LLMSetupError
from .local import LocalFolder
from .pipeline import DateRange, DigestRun, FileSource, open_sharepoint_folder, run_digest
from .report import write_report
from .sharepoint import GraphError
from .summarize import day

log = logging.getLogger("sharepoint_digest")

DEFAULT_DAYS = 7


def main_local(argv: list[str] | None = None) -> int:
    """Version 1: a folder on this computer."""
    return _main(argv, local=True)


def main_online(argv: list[str] | None = None) -> int:
    """Version 2: a SharePoint folder, read through Microsoft Graph."""
    return _main(argv, local=False)


def _main(argv: list[str] | None, *, local: bool) -> int:
    args = _parser(local).parse_args(argv)
    _setup_logging(args.verbose)
    # Trust the operating system's certificates, so corporate TLS inspection proxies work out of the box.
    truststore.inject_into_ssl()
    load_dotenv(Path.cwd() / ".env")
    load_dotenv(PROJECT_ROOT / ".env")  # doesn't override anything already set

    try:
        folders_file = args.folders_file or default_folders_file()
        if args.list_folders:
            _print_folders(load_folders(folders_file), local=local)
            return 0
        settings = load_settings()
        date_range = _date_range(args)
        if local:
            folder = _choose_local_folder(args.folder, folders_file)
            source: FileSource = LocalFolder(folder.local_path)
        else:
            folders = load_folders(folders_file)
            folder = find_folder(folders, args.folder) if args.folder else _ask_for_folder(folders)
            source = open_sharepoint_folder(settings, folder)
        run = run_digest(
            settings,
            folder,
            date_range,
            source,
            date_field="modified" if local else args.date_field,
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


def _parser(local: bool) -> argparse.ArgumentParser:
    if local:
        prog = "local_digest.py"
        where = "a folder on this computer (such as a SharePoint folder synced with OneDrive)"
        folder_help = "a folder path, or a folder from folders.toml that has a local_path (asks if omitted)"
    else:
        prog = "online_digest.py"
        where = "a SharePoint folder"
        folder_help = "folder key, label or number from folders.toml (asks if omitted)"
    parser = argparse.ArgumentParser(
        prog=prog,
        description=(
            f"Summarize the PowerPoint and Word files in {where} that changed in a date range, "
            "then combine the summaries into one digest, using a GPT model in Azure AI Foundry."
        ),
    )
    parser.add_argument("--folder", help=folder_help)
    when = parser.add_mutually_exclusive_group()
    when.add_argument("--start", type=_iso_date, metavar="YYYY-MM-DD", help="first day of the range")
    when.add_argument(
        "--days", type=_positive_int, metavar="N", help=f"the last N days up to --end (default: {DEFAULT_DAYS})"
    )
    parser.add_argument("--end", type=_iso_date, metavar="YYYY-MM-DD", help="last day of the range (default: today)")
    if not local:  # synced and downloaded copies don't keep SharePoint's creation dates
        parser.add_argument(
            "--date-field",
            choices=("modified", "created"),
            default="modified",
            help="which file date the range applies to (default: modified)",
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


def _choose_local_folder(choice: str | None, folders_file: Path) -> Folder:
    """A folder given as a path, or one from folders.toml that has a local_path."""
    if choice and _looks_like_path(choice):
        path = Path(choice).expanduser()
        name = path.resolve().name or "local"
        return Folder(key=name, label=name, path="", local_path=str(path))

    folders = load_folders(folders_file)
    if choice:
        folder = find_folder(folders, choice)
        if not folder.local_path:
            raise ConfigError(
                f'"{folder.label}" has no local_path in folders.toml. Add the path of its synced copy, '
                "or pass the folder's path with --folder."
            )
        return folder
    synced = [folder for folder in folders if folder.local_path]
    if not synced:
        raise ConfigError("Pass the folder's path with --folder, or add local_path to the folders in folders.toml.")
    return _ask_for_folder(synced)


def _looks_like_path(value: str) -> bool:
    return (
        Path(value).expanduser().exists()
        or any(mark in value for mark in ("/", "\\"))
        or value.startswith("~")
        or value[1:2] == ":"  # a Windows drive, e.g. C:
    )


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


def _print_folders(folders: list[Folder], *, local: bool) -> None:
    for number, folder in enumerate(folders, start=1):
        if local:
            where = f"local: {folder.local_path}" if folder.local_path else "no local_path set"
        else:
            where = f"path: /{folder.path}"
        print(f"{number}. {folder.label}  (--folder {folder.key}, {where})")


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
