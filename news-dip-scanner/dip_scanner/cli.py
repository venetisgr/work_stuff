"""Command line entry point: dip-scanner --help (or python -m dip_scanner --help)."""

from __future__ import annotations

import argparse
import logging
import math
import os
import sqlite3
import sys
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import requests
import truststore
from dotenv import load_dotenv

from . import __version__
from .accounts import ROLES, AccountError, Accounts, User
from .backup import BACKUP_FOLDER, DEFAULT_KEEP, backup_database
from .config import (
    DATABASE_NAME,
    PROJECT_ROOT,
    ConfigError,
    ScannerConfig,
    Settings,
    default_file,
    load_feeds,
    load_scanner_config,
    load_settings,
)
from .detect import dip_reasons
from .feeds import USER_AGENT, FeedResult, fetch_feed, needs_contact_user_agent, user_agent_for
from .fundamentals import SecFundamentals
from .fx import FxRates, main_currency, same_money
from .llm import LLMError, LLMSetupError, build_models
from .models import Feed, Impact, Opportunity, PriceBar, utc
from .notices import STOPPED, one_line, scrub, secrets_of, send_notice, stopped_lines
from .notify import build_notifiers
from .pipeline import Scanner, thesis_change_line, usage_lines
from .prices import PriceError, PriceFetchError, YahooPrices
from .report import (
    display_zone,
    format_when,
    render_html,
    render_markdown,
    render_news_digest,
    set_display_zone,
    zone_label,
)
from .store import Store
from .symbols import Resolution, SymbolResolver, current_symbol, symbol_aliases
from .track import (
    Outcome,
    benchmark_for,
    evaluate,
    quote_day,
    render_track_record,
    summarize,
    with_account_return,
    with_benchmark,
)
from .triage import normalise_ticker

log = logging.getLogger("dip_scanner")

EXIT_OK, EXIT_ERROR, EXIT_CONFIG, EXIT_INTERRUPTED = 0, 1, 2, 130
CHECK_WORKERS = 8

_DESCRIPTION = (
    "Polls ~20 news feeds, has a language model find the listed companies each story affects, checks whether their "
    "share price dipped, and rates each dip as temporary fear or real damage, with a 6-month probability, a "
    "potential low and limit-order ideas. It never places orders."
)
_EPILOG = 'Run "dip-scanner COMMAND --help" for a command\'s options. Not investment advice.'


def main(argv: list[str] | None = None) -> int:
    """Run the command line; returns the exit code (0 ok, 1 runtime error, 2 config error)."""
    _safe_console()
    args = _parser().parse_args(argv)
    _setup_logging(args.verbose)
    # Trust the operating system's certificates, so corporate TLS inspection proxies work out of the box.
    truststore.inject_into_ssl()
    try:
        _load_env(args.env_file)
        # `run` and `watch` raise a wrong DISPLAY_TZ inside _stop_notice, so an unattended scanner still says it
        # stopped; every other command reports it right away.
        problems: list[ConfigError] = []
        settings = load_settings(problems=problems)
        if problems and args.handler not in (_run, _watch):
            raise problems[0]
        args.setting_problems = problems
        set_display_zone(settings.display_tz)
        if args.data_dir is not None:
            settings = replace(settings, data_dir=_folder(args.data_dir))
        code = args.handler(args, settings)
        sys.stdout.flush()  # here, so a reader that went away is handled below and not at interpreter exit
        return code
    except ConfigError as exc:
        print(f"Configuration problem: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except LLMSetupError as exc:
        print(f"The language model can't be used: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except BrokenPipeError:
        # Whoever read the output went away (e.g. `dip-scanner news | head`): stop quietly, like other tools.
        _silence_stdout()
        return EXIT_ERROR
    except (LLMError, PriceError, PriceFetchError, sqlite3.DatabaseError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return EXIT_INTERRUPTED


# --- arguments -----------------------------------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dip-scanner", description=_DESCRIPTION, epilog=_EPILOG)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    _global_options(parser, suppress=False)
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def command(name: str, handler: Callable[[argparse.Namespace, Settings], int], help_text: str):
        sub = commands.add_parser(name, help=help_text, description=help_text)
        _global_options(sub, suppress=True)  # so "dip-scanner run -v" works as well as "dip-scanner -v run"
        sub.set_defaults(handler=handler)
        return sub

    run = command("run", _run, "Run one scan cycle and print a summary and the report's path.")
    run.add_argument("--no-notify", action="store_true", help="don't send notifications")

    watch = command("watch", _watch, "Run scan cycles on an interval until Ctrl+C.")
    watch.add_argument(
        "--interval",
        type=_positive_float(1440),
        metavar="MIN",
        help="minutes between cycles (default: [scan] in the config)",
    )
    watch.add_argument("--no-notify", action="store_true", help="don't send notifications")

    feeds = command("feeds", _feeds, "List the configured feeds and how their last fetch went.")
    feeds.add_argument("--check", action="store_true", help="fetch every feed once now and show what came back")

    news = command("news", _news, "Print the news digest (Markdown) from the database.")
    news.add_argument("--hours", type=_positive_float(24 * 36500), default=24, help="how far back to go (default: 24)")
    news.add_argument("--ticker", help="only news about this ticker")

    analyze = command("analyze", _analyze, "Analyse one ticker now, ignoring the dip thresholds and the cooldown.")
    analyze.add_argument("ticker", help="Yahoo Finance symbol, e.g. AMD, ASML, SAP.DE, 7203.T")
    analyze.add_argument("--no-save", action="store_true", help="don't store the result as an opportunity")

    report = command("report", _report, "Print the stored opportunities of the last days (Markdown).")
    report.add_argument("--days", type=_positive_float(36500), default=7, help="how far back to go (default: 7)")
    report.add_argument("--min-score", type=float, metavar="N", help="only opportunities scoring at least N")
    report.add_argument("--html", type=Path, metavar="PATH", help="also write the report as an HTML file")

    track = command("track", _track, "Show how past opportunities played out (fetches prices).")
    track.add_argument(
        "--days", type=_positive_float(36500), default=365, help="opportunities from the last N days (365)"
    )

    prices = command("prices", _prices, "Print a ticker's price statistics (no language model needed).")
    prices.add_argument("ticker", help="Yahoo Finance symbol, e.g. AMD, SAP.DE")

    users = command("users", _users, "Manage the website's accounts; the links use BASE_URL.")
    actions = users.add_subparsers(dest="users_action", required=True, metavar="ACTION")

    def action(name: str, help_text: str) -> argparse.ArgumentParser:
        sub = actions.add_parser(name, help=help_text, description=help_text)
        _global_options(sub, suppress=True)
        return sub

    add_admin = action("add-admin", "Create an admin (or make a user one) and print a link to set the password.")
    add_admin.add_argument("email", help="the admin's email address")
    add_admin.add_argument("--name", default="", help="the name shown on the website")
    invite = action("invite", "Print a single-use invite link, valid for 7 days.")
    invite.add_argument("email", nargs="?", help="only this address can use it (default: anyone with the link)")
    invite.add_argument("--role", choices=ROLES, default="member", help="the new account's role (default: member)")
    action("list", "List the accounts and the unused invites.")
    action("disable", "Disable an account: it is signed out at once and can't sign in.").add_argument("email")
    action("enable", "Enable a disabled account again.").add_argument("email")
    action("reset-link", "Print a link to set a new password, valid for 48 hours.").add_argument("email")

    serve = command("serve", _serve, "Run the website, with the scanner in the same process (needs the web extra).")
    serve.add_argument(
        "--host", default="127.0.0.1", help="address to listen on (default: 127.0.0.1; 0.0.0.0 in a container)"
    )
    serve.add_argument(
        "--port", type=_positive_int(65535), default=8080, metavar="PORT", help="port to listen on (default: 8080)"
    )
    serve.add_argument(
        "--no-scanner", action="store_true", help="serve the pages only, without scanning (like SCANNER_ENABLED=false)"
    )

    backup = command("backup", _backup, "Copy the database to DATA_DIR/backups and keep the newest copies.")
    backup.add_argument(
        "--keep", type=_positive_int(10_000), default=DEFAULT_KEEP, metavar="N", help="backups to keep (default: 7)"
    )
    return parser


def _global_options(parser: argparse.ArgumentParser, *, suppress: bool) -> None:
    def default(value: object) -> object:
        return argparse.SUPPRESS if suppress else value

    parser.add_argument("-v", "--verbose", action="store_true", default=default(False), help="show debug logging")
    parser.add_argument(
        "--env-file", type=Path, default=default(None), metavar="PATH", help="settings file (default: ./.env)"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=default(None),
        metavar="PATH",
        help="thresholds and watchlist (default: $SCANNER_CONFIG, ./scanner.toml or the project's)",
    )
    parser.add_argument(
        "--feeds",
        type=Path,
        default=default(None),
        metavar="PATH",
        help="feed list (default: $FEEDS_FILE, ./feeds.toml or the project's)",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=default(None),
        metavar="DIR",
        help="database, reports and cache (default: $DATA_DIR or ./data)",
    )


def _positive_float(maximum: float) -> Callable[[str], float]:
    """An argparse type: a finite number above zero, at most maximum (a huge --days means "everything", and is
    capped so the date arithmetic can't overflow)."""

    def parse(value: str) -> float:
        try:
            number = float(value)
        except ValueError:
            number = 0.0
        if not number > 0 or not math.isfinite(number):
            raise argparse.ArgumentTypeError(f"expected a number greater than zero, got {value!r}")
        return min(number, maximum)

    return parse


def _positive_int(maximum: int) -> Callable[[str], int]:
    """An argparse type: a whole number from 1 to maximum."""

    def parse(value: str) -> int:
        try:
            number = int(value)
        except ValueError:
            number = 0
        if not 1 <= number <= maximum:
            raise argparse.ArgumentTypeError(f"expected a whole number from 1 to {maximum}, got {value!r}")
        return number

    return parse


# --- setup ---------------------------------------------------------------------------------------------------------


def _safe_console() -> None:
    """Make sure output that can't be encoded never crashes a command.

    On Windows, output redirected to a file or a pipe (Task Scheduler's `>> data\\scanner.log`, `dip-scanner report >
    report.md`) is written in the ANSI code page (cp1252, cp1253...), which lacks Greek or Cyrillic letters, "—" or
    emoji, and a headline with one ended the command with UnicodeEncodeError. So redirected output is written as
    UTF-8, and on a console a character it can't show becomes "?" instead of an error.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # replaced by something that isn't a text stream (an IDE, a test)
            continue
        try:
            if stream.isatty():
                reconfigure(errors="replace")
            else:
                reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):  # closed, or can't be changed: leave it as it is
            pass


class _DisplayTimeFormatter(logging.Formatter):
    """Log times in the display time zone (DISPLAY_TZ, UTC by default) with its abbreviation, like every other time
    the scanner shows ("2026-09-25 23:30:05 EEST"). The zone is looked up for every record, so the log follows
    DISPLAY_TZ from .env, which is read after logging starts."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        moment = datetime.fromtimestamp(record.created, display_zone())
        if datefmt:
            return f"{moment.strftime(datefmt)} {zone_label(moment)}"
        return f"{moment:%Y-%m-%d %H:%M:%S},{int(record.msecs):03d} {zone_label(moment)}"


def _setup_logging(verbose: bool) -> None:
    if verbose:
        formatter = _DisplayTimeFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    else:
        formatter = _DisplayTimeFormatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    logging.basicConfig(handlers=[handler], level=logging.WARNING)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)


def _load_env(env_file: Path | None) -> None:
    """Load .env without overriding variables that are already set in the real environment."""
    if env_file is not None:
        path = env_file.expanduser()
        if not path.is_file():
            raise ConfigError(f"The settings file {path} doesn't exist.")
        load_dotenv(path, override=False)
        return
    load_dotenv(Path.cwd() / ".env", override=False)
    load_dotenv(PROJECT_ROOT / ".env", override=False)  # when run from another folder


def _folder(path: Path) -> Path:
    path = (Path.cwd() / path.expanduser()).resolve()
    if path.exists() and not path.is_dir():
        raise ConfigError(f"--data-dir must be a folder, but {path} is a file.")
    return path


def _silence_stdout() -> None:
    """Point stdout at devnull so flushing it at exit doesn't raise BrokenPipeError again (see the Python docs)."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):  # stdout without a file descriptor (e.g. captured in tests)
        pass


def _now() -> datetime:
    return datetime.now(UTC)


def make_session() -> requests.Session:
    """The one HTTP session shared by feeds, prices, the SEC and notifications (each sets its own User-Agent)."""
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    return session


def open_store(settings: Settings) -> Store:
    """The scanner's database in DATA_DIR (created on first use)."""
    return Store(settings.data_dir / DATABASE_NAME)


def _scanner_config(args: argparse.Namespace) -> ScannerConfig:
    """The scanner config: a file named with --config or SCANNER_CONFIG must exist (a typo must not silently mean
    the defaults); without either, ./scanner.toml or the project's copy, and the defaults when there is none."""
    named = args.config if args.config is not None else ((os.environ.get("SCANNER_CONFIG") or "").strip() or None)
    if named is not None:
        path = Path(named).expanduser()
        if not path.is_file():
            source = "--config" if args.config is not None else "SCANNER_CONFIG"
            raise ConfigError(f"Scanner config not found: {path} (from {source}).")
        return load_scanner_config(path)
    return load_scanner_config(default_file("scanner.toml", "SCANNER_CONFIG"))


def _feed_list(args: argparse.Namespace) -> list[Feed]:
    return load_feeds(args.feeds.expanduser() if args.feeds is not None else default_file("feeds.toml", "FEEDS_FILE"))


@contextmanager
def _scanner(args: argparse.Namespace, settings: Settings, *, notify: bool, feeds: bool = True) -> Iterator[Scanner]:
    """A Scanner wired to the real services; the database and HTTP session are closed afterwards."""
    config = _scanner_config(args)
    feed_list = _feed_list(args) if feeds else []
    triage_model, analysis_model = build_models(settings.llm)
    session = make_session()
    fundamentals = None
    if settings.sec_user_agent:
        fundamentals = SecFundamentals(settings.sec_user_agent, session=session, cache_dir=settings.data_dir / "cache")
    else:
        log.debug("SEC_USER_AGENT isn't set, so the analysis runs without SEC fundamentals.")
    notifiers = build_notifiers(settings.notify, session=session) if notify else []
    if notify and not notifiers:
        log.info("No notification channel is set up (see .env.example); results only go to the reports folder.")
    store = open_store(settings)
    try:
        yield Scanner(
            settings=settings,
            config=config,
            feeds=feed_list,
            store=store,
            triage_model=triage_model,
            analysis_model=analysis_model,
            prices=YahooPrices(session, clock=_now),
            fundamentals=fundamentals,
            notifiers=notifiers,
            session=session,
            notify=notify,
            clock=_now,
            symbols=SymbolResolver(session, store),
        )
    finally:
        store.close()
        session.close()


@contextmanager
def _stop_notice(command: str, args: argparse.Namespace, settings: Settings, *, notify: bool) -> Iterator[None]:
    """Sends a "dip-scanner stopped" notice to the alert channels when `run` or `watch` stops on a setup problem
    (LLMSetupError or ConfigError), then lets the error through. Unattended runs otherwise fail where nobody looks.
    At most one such notice every 12 hours (notices.py); none with --no-notify or [alerts] system_notices = false.
    A setting main() collected instead of raising (a wrong DISPLAY_TZ) is raised here, so it is noticed too."""
    problems: list[ConfigError] = getattr(args, "setting_problems", [])
    try:
        if problems:
            raise problems[0]
        yield
    except (LLMSetupError, ConfigError) as exc:
        if notify:
            what = "Configuration problem" if isinstance(exc, ConfigError) else "The language model can't be used"
            _send_stop_notice(command, args, settings, f"{what}: {exc}")
        raise


def _send_stop_notice(command: str, args: argparse.Namespace, settings: Settings, reason: str) -> None:
    secrets = secrets_of(settings)
    try:
        try:
            enabled = _scanner_config(args).alerts.system_notices
        except ConfigError:  # the broken config may be the very problem: tell the user anyway
            enabled = True
        if not enabled:
            return
        with make_session() as session:
            notifiers = build_notifiers(settings.notify, session=session)
            if not notifiers:
                return
            now = _now()
            with open_store(settings) as store:
                send_notice(
                    notifiers,
                    store,
                    kind=STOPPED,
                    subject=f"dip-scanner stopped: {one_line(reason, 150)}",
                    lines=stopped_lines(command, one_line(reason), now),
                    now=now,
                    secrets=secrets,
                )
    except Exception as exc:  # the original error matters more; it is reported by main()
        log.warning("Couldn't send a notice that dip-scanner stopped: %s", scrub(str(exc), secrets))


# --- commands ------------------------------------------------------------------------------------------------------


def _run(args: argparse.Namespace, settings: Settings) -> int:
    notify = not args.no_notify
    with _stop_notice("run", args, settings, notify=notify), _scanner(args, settings, notify=notify) as scanner:
        result = scanner.run_cycle()
    print(result.summary())
    for opp in sorted(result.opportunities, key=lambda opp: opp.score, reverse=True):
        alert = " (alert)" if opp in result.alerts else ""
        print(f"  {opp.ticker} ({opp.company}): score {opp.score:.1f}, {opp.analysis.verdict}{alert}")
    for previous, opp in result.thesis_changes:
        print(f"Thesis change: {thesis_change_line(previous, opp)}")
    if result.usage_today:
        print("Model use today (since 00:00 UTC):")
        for line in usage_lines(result.usage_today):
            print(f"  {line}")
    if result.notes:
        print("Notes:")
        for note in result.notes:
            print(f"  - {note}")
    if result.report_paths:
        print(f"Report: {result.report_paths[0]}\n        {result.report_paths[1]}")
    if result.feeds_failed and not result.feeds_ok:
        print("Every feed failed; check the network connection (dip-scanner feeds --check).", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


def _watch(args: argparse.Namespace, settings: Settings) -> int:
    notify = not args.no_notify
    with _stop_notice("watch", args, settings, notify=notify), _scanner(args, settings, notify=notify) as scanner:
        scanner.watch(interval_minutes=args.interval)
    return EXIT_OK


def _feeds(args: argparse.Namespace, settings: Settings) -> int:
    feeds = _feed_list(args)
    if args.check:
        return _check_feeds(feeds, settings)
    health: dict[str, dict] = {}
    if (settings.data_dir / DATABASE_NAME).exists():
        with open_store(settings) as store:
            health = {row["key"]: row for row in store.feed_health()}
    enabled = sum(feed.enabled for feed in feeds)
    print(f"{len(feeds)} feeds, {enabled} enabled:")
    for feed in feeds:
        row = health.get(feed.key)
        if row is None or row["last_fetch"] is None:
            last = "never fetched"
        else:
            outcome = f"error: {row['last_error']}" if row["last_error"] else f"HTTP {row['last_status']}"
            last = f"last fetch {format_when(row['last_fetch'])}, {outcome}, {row['articles']} stored"
        state = "on " if feed.enabled else "off"
        print(f"  {state} {feed.key:<34} {feed.category:<14} {feed.name}  ({last})")
    return EXIT_OK


def _check_feeds(feeds: list[Feed], settings: Settings) -> int:
    agents = {"sec.gov": settings.sec_user_agent}
    now = _now()
    feeds_logger = logging.getLogger("dip_scanner.feeds")
    level = feeds_logger.level
    if not log.isEnabledFor(logging.DEBUG):
        feeds_logger.setLevel(logging.ERROR)  # the table below shows every error once
    try:
        with make_session() as session, ThreadPoolExecutor(max_workers=CHECK_WORKERS) as pool:
            results = list(
                pool.map(
                    lambda feed: fetch_feed(session, feed, None, now=now, user_agent=user_agent_for(feed.url, agents)),
                    feeds,
                )
            )
    finally:
        feeds_logger.setLevel(level)
    # A feed that needs a contact User-Agent nobody set is skipped by the scanner too (with a warning): not a failure.
    skipped = {
        feed.key for feed in feeds if needs_contact_user_agent(feed.url) and not user_agent_for(feed.url, agents)
    }
    for result in results:
        print(_check_line(result, now, skipped=result.feed.key in skipped))
    enabled = [result for result in results if result.feed.enabled and result.feed.key not in skipped]
    failed = [result.feed.key for result in enabled if result.error is not None]
    print(f"{len(enabled) - len(failed)} of {len(enabled)} enabled feeds answered.")
    left_out = [result.feed.key for result in results if result.feed.enabled and result.feed.key in skipped]
    if left_out:
        print(f"Skipped until SEC_USER_AGENT is set: {', '.join(left_out)}")
    if failed:
        print(f"Failed: {', '.join(failed)}", file=sys.stderr)
    return EXIT_ERROR if failed else EXIT_OK


def _check_line(result: FeedResult, now: datetime, *, skipped: bool = False) -> str:
    state = "on " if result.feed.enabled else "off"
    status = str(result.status) if result.status is not None else "---"
    if skipped:
        detail = f"skipped: {result.error}"
    elif result.error is not None:
        detail = f"FAILED: {result.error}"
    else:
        newest = max((utc(article.published) for article in result.articles), default=None)
        age = f", newest {_age(now - newest)} old" if newest is not None else ""
        detail = f"{len(result.articles)} items{age}"
    return f"  {state} {result.feed.key:<34} {status:>3}  {detail}"


def _age(delta: timedelta) -> str:
    minutes = max(0.0, delta.total_seconds() / 60)
    if minutes < 90:
        return f"{minutes:.0f} min"
    if minutes < 48 * 60:
        return f"{minutes / 60:.1f} h"
    return f"{minutes / 1440:.1f} days"


def _news(args: argparse.Namespace, settings: Settings) -> int:
    """The digest with every story under the symbol a scan cycle reads it as: its [universe] preferred_listings
    listing, or the replacement found for an old symbol (OPAP.AT's news under ALWN.AT); --ticker takes those too."""
    now = _now()
    preferred = _scanner_config(args).universe.preferred_listings
    ticker = _symbol(args.ticker) if args.ticker else None
    if ticker in preferred:
        print(f"{ticker} is read as {preferred[ticker]} ([universe] preferred_listings).", file=sys.stderr)
        ticker = preferred[ticker]
    since = now - timedelta(hours=args.hours)
    with open_store(settings) as store:
        if ticker:
            news = store.news(since, ticker, also=symbol_aliases(store, ticker, preferred, now=now))
        else:
            symbols: dict[str, str] = {}

            def current(symbol: str) -> str:
                if symbol not in symbols:
                    symbols[symbol] = current_symbol(store, symbol, preferred, now=now)
                return symbols[symbol]

            news = [(article, _relabelled(impacts, current)) for article, impacts in store.news(since)]
    print(render_news_digest(news, hours=args.hours, generated=now), end="")
    if not news:
        print("(Nothing stored yet? `dip-scanner run` polls the feeds.)", file=sys.stderr)
    return EXIT_OK


def _relabelled(impacts: list[Impact], current: Callable[[str], str]) -> list[Impact]:
    """The impacts under their current symbols, one per symbol (the first)."""
    result: dict[str, Impact] = {}
    for impact in impacts:
        symbol = current(impact.ticker)
        result.setdefault(symbol, replace(impact, ticker=symbol))
    return list(result.values())


def _analyze(args: argparse.Namespace, settings: Settings) -> int:
    with _scanner(args, settings, notify=False, feeds=False) as scanner:
        opp = scanner.analyze_ticker(args.ticker)
        if not args.no_save:
            opp = scanner.store.add_opportunity(opp)
            # You're reading it right now, so a running `watch` shouldn't send it to you as an alert as well.
            scanner.store.mark_notified([opp.id], when=opp.created)
    if opp.ticker != _symbol(args.ticker):
        print(f"{_symbol(args.ticker)} is read as {opp.ticker} ([universe] preferred_listings).", file=sys.stderr)
    print(render_markdown([opp], title=f"Analysis of {opp.ticker}", generated=opp.created), end="")
    if opp.id is not None:
        print(f"Saved as opportunity #{opp.id} (see `dip-scanner report` and `dip-scanner track`).", file=sys.stderr)
    return EXIT_OK


def _report(args: argparse.Namespace, settings: Settings) -> int:
    now = _now()
    with open_store(settings) as store:
        opps = store.opportunities(since=now - timedelta(days=args.days), min_score=args.min_score)
    title = f"Dip opportunities of the last {args.days:g} days"
    print(render_markdown(opps, title=title, generated=now), end="")
    if args.html is not None:
        path = args.html.expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_html(opps, title=title, generated=now), encoding="utf-8")
        print(f"Wrote {path}", file=sys.stderr)
    return EXIT_OK


def _track(args: argparse.Namespace, settings: Settings) -> int:
    now = _now()
    account = _scanner_config(args).account.currency
    with open_store(settings) as store:
        opps = store.opportunities(since=now - timedelta(days=args.days))
    if not opps:
        print(f"No opportunities stored in the last {args.days:g} days yet.")
        return EXIT_OK
    by_ticker: dict[str, list[Opportunity]] = {}
    for opp in opps:
        by_ticker.setdefault(opp.ticker, []).append(opp)
    outcomes: list[Outcome] = []
    problems: list[str] = []
    missing: list[tuple[str, int]] = []
    with make_session() as session:
        prices = YahooPrices(session)
        for ticker, group in by_ticker.items():  # one download per ticker, from its oldest quote day
            try:
                bars, splits = prices.history_since(ticker, min(quote_day(opp) for opp in group), now=now)
            except PriceError as exc:  # delisted, renamed...: named in the track record, not silently dropped
                problems.append(f"{ticker} ({len(group)} opportunities): {exc}")
                missing.append((ticker, len(group)))
                continue
            except PriceFetchError as exc:
                problems.append(f"{ticker} ({len(group)} opportunities): {exc}")
                continue
            outcomes.extend(evaluate(opp, bars, now=now, splits=splits) for opp in group)
        notes: list[str] = []
        outcomes = _with_benchmarks(outcomes, prices, now=now, notes=notes)
        if account:
            outcomes = _in_account(outcomes, FxRates(prices, clock=_now), account, now=now, notes=notes)
    print(render_track_record(outcomes, summarize(outcomes), missing=missing), end="")
    for problem in problems:
        print(f"Left out {problem}", file=sys.stderr)
    for note in notes:
        print(f"Note: {note}", file=sys.stderr)
    return EXIT_ERROR if problems and not outcomes else EXIT_OK


def _with_benchmarks(outcomes: list[Outcome], prices: YahooPrices, *, now: datetime, notes: list[str]) -> list[Outcome]:
    """The outcomes with their exchange's benchmark index return (one download per index); an index without prices
    leaves "–" and a note."""
    groups: dict[str, list[int]] = {}
    for index, outcome in enumerate(outcomes):
        groups.setdefault(benchmark_for(outcome.opportunity.ticker), []).append(index)
    result = list(outcomes)
    for symbol, indices in groups.items():
        bars: list[PriceBar] = []
        if any(outcomes[index].priced for index in indices):
            start = min(quote_day(outcomes[index].opportunity) for index in indices) - timedelta(days=10)
            try:
                bars = prices.bars_since(symbol, start, now=now)
            except (PriceError, PriceFetchError) as exc:
                notes.append(
                    f"No prices for the benchmark index {symbol}, so {len(indices)} opportunities show – for it: {exc}"
                )
        for index in indices:
            result[index] = with_benchmark(outcomes[index], symbol, bars)
    return result


def _in_account(
    outcomes: list[Outcome], fx: FxRates, account: str, *, now: datetime, notes: list[str]
) -> list[Outcome]:
    """The outcomes with their return in the [account] currency (one rate history per currency); a currency without
    rates leaves "–" and a note."""
    groups: dict[str, list[int]] = {}
    for index, outcome in enumerate(outcomes):
        groups.setdefault(outcome.opportunity.currency, []).append(index)
    result = list(outcomes)
    for currency, indices in groups.items():
        rates = None
        wanted = not same_money(currency, account) and any(outcomes[index].priced for index in indices)
        if wanted:
            start = min(quote_day(outcomes[index].opportunity) for index in indices)
            try:
                rates = fx.history(currency, account, start, now=now)
            except (PriceError, PriceFetchError) as exc:
                notes.append(
                    f"No {main_currency(currency)[0]}/{account} exchange rates, so {len(indices)} opportunities show – "
                    f"in {account}: {exc}"
                )
        for index in indices:
            result[index] = with_account_return(outcomes[index], account, rates)
    return result


def _prices(args: argparse.Namespace, settings: Settings) -> int:
    symbol = _symbol(args.ticker)
    config = _scanner_config(args)
    with make_session() as session:
        try:
            stats = YahooPrices(session).stats(symbol, now=_now())
        except PriceError as exc:
            found = _known_replacement(settings, symbol)
            if found is None:
                raise
            raise PriceError(
                f"{exc} The scanner found {found.symbol} ({found.name}) for {found.query}: try "
                f"`dip-scanner prices {found.symbol}`."
            ) from exc
    print(stats.as_text(display_zone()))
    reasons = dip_reasons(stats, config.dip, now=_now())
    print(f"Dip by the [dip] thresholds: {'; '.join(reasons)}" if reasons else "No dip by the [dip] thresholds.")
    return EXIT_OK


def _known_replacement(settings: Settings, symbol: str) -> Resolution | None:
    """The replacement a scan cycle found for a symbol without prices in the last 7 days, if there is a database."""
    if not (settings.data_dir / DATABASE_NAME).exists():
        return None
    try:
        with open_store(settings) as store:
            return SymbolResolver(None, store).known(symbol, now=_now())
    except sqlite3.Error as exc:  # only a hint
        log.debug("Couldn't read the symbol lookups: %s", exc)
        return None


@contextmanager
def _quiet(args: argparse.Namespace, *names: str) -> Iterator[None]:
    """Keep these loggers' info lines (which the command prints in its own words) out of the output, unless -v."""
    loggers = [logging.getLogger(name) for name in names]
    levels = [logger.level for logger in loggers]
    if not args.verbose:
        for logger in loggers:
            logger.setLevel(logging.WARNING)
    try:
        yield
    finally:
        for logger, level in zip(loggers, levels, strict=True):
            logger.setLevel(level)


def _users(args: argparse.Namespace, settings: Settings) -> int:
    """`dip-scanner users ACTION`: the website's accounts, for the server's owner (e.g. over `fly ssh console`).

    The links carry one-time tokens: they are printed for the person running the command and never logged.
    """
    action = args.users_action
    if action in ("add-admin", "invite", "reset-link"):
        settings.web.link("/")  # BASE_URL is needed for the link: say so before anything is changed
    with _quiet(args, "dip_scanner.accounts"), open_store(settings) as store:
        accounts = Accounts(store, clock=lambda: _now())
        try:
            if action == "list":
                _list_users(accounts)
                return EXIT_OK
            if action == "invite":
                token = accounts.create_invite(created_by=None, email=args.email, role=args.role)
                who = args.email.strip().lower() if args.email else "anyone with the link"
                role = "an admin" if args.role == "admin" else "a member"
                print(f"Invite for {who} as {role}, valid once within 7 days:")
                print(f"  {settings.web.link(f'/invite/{token}')}")
                return EXIT_OK
            if action == "add-admin":
                user, said = _make_admin(accounts, args.email, args.name)
                print(said)
            else:
                user = accounts.get_user_by_email(args.email)
                if user is None:
                    print(f"Error: there is no account for {args.email.strip()}.", file=sys.stderr)
                    return EXIT_ERROR
                if action in ("disable", "enable"):
                    user = accounts.set_disabled(user.id, action == "disable")
                    print(f"{user.email} is {action}d{' and signed out everywhere' if action == 'disable' else ''}.")
                    return EXIT_OK
                if user.disabled:
                    print(f"Error: {user.email} is disabled; `dip-scanner users enable` it first.", file=sys.stderr)
                    return EXIT_ERROR
        except AccountError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return EXIT_ERROR
        purpose = "reset" if user.has_password else "setup"
        token = accounts.create_password_token(user.id, purpose)
    what = "choose a new password" if purpose == "reset" else "set the password"
    print(f"Open this link within 48 hours to {what} for {user.email} (it works once):")
    print(f"  {settings.web.link(f'/password/{token}')}")
    return EXIT_OK


def _make_admin(accounts: Accounts, email: str, name: str) -> tuple[User, str]:
    """The admin account for email, created or made an admin (and enabled) when it already exists."""
    user = accounts.get_user_by_email(email)
    if user is None:
        user = accounts.create_user(email, name=name, role="admin")
        return user, f"Created the admin account {user.email}."
    changes = []
    if not user.is_admin:
        user = accounts.set_role(user.id, "admin")
        changes.append("made it an admin")
    if user.disabled:
        user = accounts.set_disabled(user.id, False)
        changes.append("enabled it")
    if name:
        user = accounts.set_name(user.id, name)
    done = f"; {' and '.join(changes)}" if changes else ""
    return user, f"{user.email} already had an account{done}."


def _list_users(accounts: Accounts) -> None:
    users = accounts.list_users()
    if not users:
        print("No accounts yet. Create the first admin with `dip-scanner users add-admin EMAIL`.")
    else:
        print(f"{len(users)} account{'s' if len(users) != 1 else ''}:")
    for user in users:
        chosen = user.settings
        status = "disabled" if user.disabled else "active" if user.has_password else "no password yet"
        seen = f"last login {format_when(user.last_login)}" if user.last_login else "never signed in"
        channels = [
            name
            for name, on in (
                ("email", chosen.email_alerts),
                ("telegram", bool(chosen.telegram_chat_id)),
                ("webhook", bool(chosen.webhook_url)),
            )
            if on
        ]
        name = f" ({user.name})" if user.name else ""
        print(
            f"  #{user.id:<3} {user.email:<34} {user.role:<6} {status:<15} {seen}; "
            f"alerts: {', '.join(channels) or 'none'}{name}"
        )
    invites = accounts.list_invites()
    if invites:
        print(f"{len(invites)} unused invite{'s' if len(invites) != 1 else ''}:")
        for invite in invites:
            print(
                f"  {invite.email or 'anyone with the link':<38} {invite.role:<6} expires {format_when(invite.expires)}"
            )


def _serve(args: argparse.Namespace, settings: Settings) -> int:
    """`dip-scanner serve`: the website and (unless --no-scanner or SCANNER_ENABLED=false) the scanner's watch loop in
    one process, until Ctrl+C or SIGTERM. A setup problem of the scanner doesn't stop the website (web/server.py)."""
    try:
        from .web.server import serve
    except ImportError as exc:  # installed without the web extra
        raise ConfigError(
            f'The website needs the web extra: pip install -e ".[web]" ({exc.name or exc} is missing).'
        ) from None
    settings.web.require_secret_key()
    config = _scanner_config(args)
    feeds = _feed_list(args)
    enabled = settings.web.scanner_enabled and not args.no_scanner
    serve(settings, config, feeds, host=args.host, port=args.port, scanner_enabled=enabled, session=make_session())
    return EXIT_OK


def _backup(args: argparse.Namespace, settings: Settings) -> int:
    with _quiet(args, "dip_scanner.backup"):
        path = backup_database(
            settings.data_dir / DATABASE_NAME, settings.data_dir / BACKUP_FOLDER, keep=args.keep, now=_now()
        )
    size = path.stat().st_size
    shown = f"{size / 1_048_576:.1f} MB" if size >= 1_048_576 else f"{max(1, round(size / 1024))} kB"
    print(f"Backed up the database to {path} ({shown}); the newest {args.keep} backups are kept.")
    return EXIT_OK


def _symbol(text: str) -> str:
    """The Yahoo symbol for what was typed ("$amd", "BRK.B", "NASDAQ:AMD" all work)."""
    symbol = normalise_ticker(text) or text.strip().upper()
    if not symbol:
        raise ConfigError("No ticker given.")
    return symbol
