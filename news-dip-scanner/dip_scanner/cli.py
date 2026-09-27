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
from .llm import LLMError, LLMSetupError, build_models
from .models import Feed, Opportunity, utc
from .notify import build_notifiers
from .pipeline import Scanner, thesis_change_line
from .prices import PriceError, PriceFetchError, YahooPrices
from .report import render_html, render_markdown, render_news_digest
from .store import Store
from .track import Outcome, evaluate, quote_day, render_track_record, summarize
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
    args = _parser().parse_args(argv)
    _setup_logging(args.verbose)
    # Trust the operating system's certificates, so corporate TLS inspection proxies work out of the box.
    truststore.inject_into_ssl()
    try:
        _load_env(args.env_file)
        settings = load_settings()
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


# --- setup ---------------------------------------------------------------------------------------------------------


def _setup_logging(verbose: bool) -> None:
    if verbose:
        logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.WARNING)
    else:
        logging.basicConfig(format="%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S", level=logging.WARNING)
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
        )
    finally:
        store.close()
        session.close()


# --- commands ------------------------------------------------------------------------------------------------------


def _run(args: argparse.Namespace, settings: Settings) -> int:
    with _scanner(args, settings, notify=not args.no_notify) as scanner:
        result = scanner.run_cycle()
    print(result.summary())
    for opp in sorted(result.opportunities, key=lambda opp: opp.score, reverse=True):
        alert = " (alert)" if opp in result.alerts else ""
        print(f"  {opp.ticker} ({opp.company}): score {opp.score:.1f}, {opp.analysis.verdict}{alert}")
    for previous, opp in result.thesis_changes:
        print(f"Thesis change: {thesis_change_line(previous, opp)}")
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
    with _scanner(args, settings, notify=not args.no_notify) as scanner:
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
            last = f"last fetch {row['last_fetch']:%Y-%m-%d %H:%M} UTC, {outcome}, {row['articles']} stored"
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
    now = _now()
    ticker = _symbol(args.ticker) if args.ticker else None
    with open_store(settings) as store:
        news = store.news(now - timedelta(hours=args.hours), ticker)
    print(render_news_digest(news, hours=args.hours, generated=now), end="")
    if not news:
        print("(Nothing stored yet? `dip-scanner run` polls the feeds.)", file=sys.stderr)
    return EXIT_OK


def _analyze(args: argparse.Namespace, settings: Settings) -> int:
    with _scanner(args, settings, notify=False, feeds=False) as scanner:
        opp = scanner.analyze_ticker(args.ticker)
        if not args.no_save:
            opp = scanner.store.add_opportunity(opp)
            # You're reading it right now, so a running `watch` shouldn't send it to you as an alert as well.
            scanner.store.mark_notified([opp.id], when=opp.created)
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
    print(render_track_record(outcomes, summarize(outcomes), missing=missing), end="")
    for problem in problems:
        print(f"Left out {problem}", file=sys.stderr)
    return EXIT_ERROR if problems and not outcomes else EXIT_OK


def _prices(args: argparse.Namespace, settings: Settings) -> int:
    symbol = _symbol(args.ticker)
    config = _scanner_config(args)
    with make_session() as session:
        stats = YahooPrices(session).stats(symbol, now=_now())
    print(stats.as_text())
    reasons = dip_reasons(stats, config.dip, now=_now())
    print(f"Dip by the [dip] thresholds: {'; '.join(reasons)}" if reasons else "No dip by the [dip] thresholds.")
    return EXIT_OK


def _symbol(text: str) -> str:
    """The Yahoo symbol for what was typed ("$amd", "BRK.B", "NASDAQ:AMD" all work)."""
    symbol = normalise_ticker(text) or text.strip().upper()
    if not symbol:
        raise ConfigError("No ticker given.")
    return symbol
