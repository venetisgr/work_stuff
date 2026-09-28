"""One scan cycle (poll -> triage -> candidates -> analysis -> report -> notify), the watch loop and manual analysis.

Everything the scanner talks to (feeds, prices, the SEC, the models, notifiers and the database) is passed in, so
tests can run whole cycles with fakes.

Alerts go to recipients (recipients.py), each with its own rules, channels, currency and time zone and its own alert
state in the database. The command line has one, "default", built from scanner.toml and .env; the website passes
recipients, watchlist and currencies callables (recipients.service_hooks) that the Scanner asks at every cycle.
Every cycle is recorded in the cycles table. The website controls a running watch loop from another thread with
request_cycle_now() and stop(), and pauses it through the database (Store.set_scanner_paused).
"""

from __future__ import annotations

import logging
import math
import sqlite3
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .analyze import analyze_candidate
from .config import AlertConfig, ConfigError, ScannerConfig, Settings
from .debate import DebaterFailure, debate_candidate
from .detect import dip_reasons, news_after_session, select_candidates, session_day, severity
from .feeds import CONTACT_USER_AGENT_MISSING, fetch_all, needs_contact_user_agent, ticker_news, user_agent_for
from .fundamentals import SecFundamentals
from .fx import FxRates
from .llm import ChatModel, DebatePanel, LLMError, LLMSetupError, LLMUnavailableError, Usage
from .models import Article, Candidate, Feed, Fundamentals, Impact, ModelUsage, Opportunity, utc
from .notices import (
    FEEDS_FAILING,
    MODEL_UNAVAILABLE,
    debater_notice_kind,
    debater_notice_lines,
    debater_notice_subject,
    one_line,
    scrub,
    secrets_of,
    send_notice,
)
from .notify import Notifier, NotifyError, TelegramNotifier, WebhookNotifier, short_alert
from .prices import PriceError, PriceFetchError, YahooPrices
from .recipients import Recipient
from .recipients import default_recipient as _default_recipient
from .report import (
    display_zone_as,
    format_clock,
    format_money,
    format_price,
    format_when,
    render_html,
    render_markdown,
    verdict_label,
    write_reports,
)
from .store import DEFAULT_RECIPIENT, Store
from .symbols import Resolution, SymbolResolver, symbol_aliases
from .track import benchmark_for, quote_day
from .triage import normalise_ticker, triage

log = logging.getLogger(__name__)

CONTEXT_NEWS_LIMIT = 15  # per-ticker headlines (Yahoo + Google News) added to each analysis
PRUNE_EVERY = timedelta(days=1)
ALERT_RETRY_WINDOW = timedelta(hours=24)  # alerts that couldn't be sent are retried for this long
# A ticker alerted within this long (the 6-month horizon of the idea) whose new analysis no longer passes [alerts],
# or whose chance of being higher fell by THESIS_DROP_POINTS or more, gets a "thesis change" notice.
THESIS_WINDOW = timedelta(days=183)
THESIS_DROP_POINTS = 20
THESIS_TITLE = "Thesis changes: review open orders"
# After a failed analysis a ticker waits this long before the next try, doubling with every failure in a row (up to
# MAX_FAILURE_BACKOFF), so one article the model keeps choking on doesn't cost a request every five minutes.
FAILURE_BACKOFF = timedelta(minutes=30)
MAX_FAILURE_BACKOFF = timedelta(hours=24)
ALERT_TITLE = "Dip alerts"
MANUAL_TITLE = "Manual analysis"
DAY = timedelta(hours=24)  # the window of [scan] max_analyses_per_day


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class CycleResult:
    """What one scan cycle did. impacts and candidates are counts; the notes explain what was left out and why."""

    started: datetime
    finished: datetime | None = None
    feeds_ok: int = 0
    feeds_failed: int = 0
    new_articles: int = 0
    triaged: int = 0
    impacts: int = 0
    candidates: int = 0
    opportunities: list[Opportunity] = field(default_factory=list)
    alerts: list[Opportunity] = field(default_factory=list)
    # (the earlier alert, the new analysis that no longer supports it), sent as "thesis change" notices
    thesis_changes: list[tuple[Opportunity, Opportunity]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    report_paths: list[Path] = field(default_factory=list)
    # Calls the model service answered this cycle, and why the model couldn't be used (None when nothing failed).
    model_calls: int = 0
    model_unavailable: str | None = None
    usage_today: list[ModelUsage] = field(default_factory=list)  # model use since 00:00 UTC, this cycle included
    recipients: int = 0  # the recipients of this cycle's alerts (with or without a channel)
    sent: int = 0  # alerts and thesis changes that reached a recipient, counted per recipient

    def summary(self) -> str:
        """One line for the log, e.g. "Cycle 2026-09-25 15:00 UTC: 19/20 feeds ok, 37 new articles, ...", ending with
        the day's model use when there is any. The time is in the display time zone (DISPLAY_TZ)."""
        ranked = sorted(self.opportunities, key=lambda opp: opp.score, reverse=True)
        found = _count(len(ranked), "opportunity", "opportunities")
        if ranked:
            found += " (" + ", ".join(f"{opp.ticker} {opp.score:.1f}" for opp in ranked) + ")"
        parts = [
            f"{self.feeds_ok}/{self.feeds_ok + self.feeds_failed} feeds ok",
            _count(self.new_articles, "new article"),
            f"{self.triaged} triaged",
            _count(self.impacts, "company impact"),
            _count(self.candidates, "candidate"),
            found,
            _count(len(self.alerts), "alert"),
        ]
        took = f"; took {(self.finished - self.started).total_seconds():.0f} s" if self.finished else ""
        usage = f"; {usage_summary(self.usage_today)}" if self.usage_today else ""
        return f"Cycle {format_when(self.started)}: {', '.join(parts)}{took}{usage}"


class MeteredModel:
    """A ChatModel that stores every call the service answered in the store's model_calls, with its tokens.

    A call counts as answered when the wrapped model set last_usage (the models in llm.py do as soon as a reply
    arrives, also one that turns out unusable, which is billed all the same) or when it returned a reply (models
    without last_usage, like the tests' fakes). Calls that never reached the service (connection errors, throttling,
    bad credentials) aren't stored, so an outage doesn't use up [scan] max_analyses_per_day. answered counts the
    calls stored; when is the cycle's time, so all calls of one analysis share it.
    """

    def __init__(self, model: ChatModel, store: Store, *, step: str, when: datetime, ticker: str | None = None) -> None:
        self.model = model
        self.name = model.name
        self.answered = 0
        self._store = store
        self._step = step
        self._when = when
        self._ticker = ticker

    def complete(self, system: str, prompt: str, *, json_mode: bool = False) -> str:
        """The wrapped model's reply; the call is recorded whether or not the reply is usable."""
        try:
            reply = self.model.complete(system, prompt, json_mode=json_mode)
        except Exception:
            self._record(getattr(self.model, "last_usage", None))
            raise
        self._record(getattr(self.model, "last_usage", None) or Usage())
        return reply

    def _record(self, usage: object) -> None:
        if not isinstance(usage, Usage):
            return  # the service never answered
        self.answered += 1
        try:
            self._store.record_model_call(
                when=self._when,
                step=self._step,
                model=self.name,
                ticker=self._ticker,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
            )
        except sqlite3.Error as exc:  # bookkeeping must not cost the reply
            log.warning("Couldn't record a call to %s: %s", self.name, exc)


class Scanner:
    """Runs scan cycles with everything it needs passed in (so tests can pass fakes).

    analysis_model is a ChatModel, or a llm.DebatePanel with LLM_ANALYSIS_MODE=debate: then every analysis (in a cycle,
    `analyze` and the website's "Analyse now") is a two-model debate (debate.py) under [debate].

    Feeds that are disabled, or that need a contact User-Agent nobody configured (sec.gov without SEC_USER_AGENT),
    are left out once, here, with a warning, instead of failing every cycle. fx gives exchange rates for [account]
    currency (by default from the same Yahoo client as the prices).

    Alerts go to the recipients the recipients callable returns for the cycle's time; without it, to the single
    "default" recipient built from scanner.toml's [alerts], the notifiers passed here, [account] currency and
    DISPLAY_TZ (default_recipient), which is what the command line does. watchlist returns more symbols to treat like
    [universe] watchlist when looking for candidates (the website's users' watchlists), and currencies more currencies
    to store exchange rates into with each analysis (the users' display currencies). All three are asked at every
    cycle; one that fails is noted and the cycle goes on without it.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        config: ScannerConfig,
        feeds: list[Feed],
        store: Store,
        triage_model: ChatModel,
        analysis_model: ChatModel | DebatePanel,
        prices: YahooPrices,
        fundamentals: SecFundamentals | None,
        notifiers: list[Notifier],
        session,
        notify: bool = True,
        clock: Callable[[], datetime] = _now,
        symbols: SymbolResolver | None = None,
        fx: FxRates | None = None,
        recipients: Callable[[datetime], Iterable[Recipient]] | None = None,
        watchlist: Callable[[datetime], Iterable[str]] | None = None,
        currencies: Callable[[datetime], Iterable[str]] | None = None,
    ) -> None:
        self.settings = settings
        self.config = config
        self.store = store
        self.triage_model = triage_model
        self.analysis_model = analysis_model
        self.prices = prices
        self.fundamentals = fundamentals
        self.notifiers = list(notifiers)
        self.session = session
        self.notify = notify
        self._clock = clock
        self.symbols = symbols  # finds the new symbol of a renamed company (None: tickers without prices are skipped)
        self.fx = fx if fx is not None else FxRates(prices, clock=clock)
        self.user_agents = {"sec.gov": settings.sec_user_agent}
        self.feeds = [feed for feed in feeds if feed.enabled and self._can_fetch(feed)]
        self._last_prune: datetime | None = None
        self._recipients_of = recipients
        self._watchlist_of = watchlist
        self._currencies_of = currencies
        # The watch loop, as seen from other threads (the website): what it is doing and when it runs next.
        self.watching = False
        self.cycle_started: datetime | None = None  # while a cycle runs
        self.next_cycle_at: datetime | None = None  # while the watch loop waits
        self.last_result: CycleResult | None = None
        self._wake = threading.Event()
        self._requested = threading.Event()
        self._stopping = threading.Event()

    def default_recipient(self) -> Recipient:
        """The command line's recipient: [alerts], the notifiers passed in, [account] currency and DISPLAY_TZ."""
        return _default_recipient(self.settings, self.config, self.notifiers)

    def _can_fetch(self, feed: Feed) -> bool:
        if needs_contact_user_agent(feed.url) and not user_agent_for(feed.url, self.user_agents):
            log.warning("Skipping feed %s: %s", feed.key, CONTACT_USER_AGENT_MISSING)
            return False
        return True

    # --- the cycle ---

    def poll(self, now: datetime) -> tuple[int, int, int]:
        """Fetch every enabled feed and store new articles; returns (feeds ok, feeds failed, new articles).

        "New" counts the articles young enough to triage ([scan] max_article_age_hours): older new ones are stored
        as skipped, which is what keeps the first cycle after a fresh install from triaging days of backlog. A 304
        (not modified) counts as ok.

        The articles are stored before the feeds' new ETag/Last-Modified are saved: if storing fails, the next poll
        asks again without them and gets the items again, instead of a 304 that would lose them.
        """
        now = utc(now)
        states = {feed.key: state for feed in self.feeds if (state := self.store.feed_state(feed.key)) is not None}
        results = fetch_all(
            self.session,
            self.feeds,
            states,
            workers=self.config.scan.workers,
            now=now,
            user_agents=self.user_agents,
        )
        listed = [article for result in results for article in result.articles]
        new = self.store.add_articles(
            listed,
            max_age_hours=self.config.scan.max_article_age_hours,
            now=now,
            same_source_titles={feed.key for feed in self.feeds if not feed.dedup_titles},
        )
        ok = failed = 0
        for result in results:
            self.store.save_feed_state(
                result.feed.key, result.state, status=result.status, error=result.error, fetched=now
            )
            if result.error is None:
                ok += 1
            else:
                failed += 1
        log.debug(
            "Polled %d feed(s): %d ok, %d failed; %d article(s) listed, %d new.",
            len(results),
            ok,
            failed,
            len(listed),
            len(new),
        )
        return ok, failed, len(new)

    def run_cycle(self, now: datetime | None = None) -> CycleResult:
        """One full cycle: poll, triage, select candidates, analyse, report and notify.

        A failed analysis is noted and the next candidate is tried (the ticker then waits, see FAILURE_BACKOFF);
        when the analysis model is unreachable the remaining candidates are left for the next cycle. LLMSetupError
        and ConfigError stop the analyses (no later call could succeed): the opportunities already found are still
        reported and alerted, then the error propagates. Notification failures are noted, never raised.
        Without notifications (notify=False or no recipient with a channel) the cycle's opportunities count as handled
        for everybody, so a later run doesn't send them. Every cycle is stored in the cycles table (a cycle that
        raises with ok=False and the error, scrubbed of secrets, in its summary).
        """
        now = utc(now) if now is not None else utc(self._clock())
        started = time.monotonic()
        result = CycleResult(started=now)
        self.cycle_started = now
        try:
            self._cycle(now, started, result)
        except Exception as exc:
            self._record(result, started, error=exc)
            raise
        finally:
            self.cycle_started = None
        self._record(result, started)
        self.last_result = result
        return result

    def _cycle(self, now: datetime, started: float, result: CycleResult) -> None:
        scan = self.config.scan
        result.feeds_ok, result.feeds_failed, result.new_articles = self.poll(now)
        # Articles left pending by an unavailable model are only worth triaging while they're young enough.
        stale = self.store.skip_stale_pending(now - timedelta(hours=scan.max_article_age_hours))
        if stale:
            log.info(
                "Skipped %s older than [scan] max_article_age_hours that the model couldn't triage in time.",
                _count(stale, "pending article"),
            )
        triage_model = MeteredModel(self.triage_model, self.store, step="triage", when=now)
        try:
            result.triaged, impacts = triage(
                triage_model,
                self.store,
                batch_size=scan.triage_batch_size,
                max_attempts=scan.max_triage_attempts,
                now=now,
                on_stop=lambda reason: _unavailable(result, reason),
                preferred=self.config.universe.preferred_listings,
            )
        finally:
            result.model_calls += triage_model.answered
        result.impacts = len(impacts)

        recipients = self._recipients(now, result)
        recent = self.store.recent_impacts(now - timedelta(hours=scan.lookback_hours))
        candidates, notes = select_candidates(
            recent,
            self.prices,
            self.store,
            self._candidate_config(now, result),
            now=now,
            waiting=lambda ticker: self._waiting(ticker, now),
            symbols=self.symbols,
        )
        result.notes.extend(notes)
        candidates = self._daily_limit(candidates, now, result)
        result.candidates = len(candidates)
        currencies = self._currencies(now, recipients)

        fatal: Exception | None = None
        for index, candidate in enumerate(candidates):
            try:
                opportunity = self._analyze(candidate, now, result=result, currencies=currencies, recipients=recipients)
            except LLMUnavailableError as exc:
                left = ", ".join(c.ticker for c in candidates[index:])
                result.notes.append(f"The analysis model is unavailable, left for the next cycle: {left} ({exc})")
                log.warning("%s", result.notes[-1])
                _unavailable(result, f"the analysis model is unavailable: {exc}")
                break
            except (LLMSetupError, ConfigError) as exc:  # no later call can work; report what was found first
                left = ", ".join(c.ticker for c in candidates[index:])
                result.notes.append(f"Analysis stopped, the model can't be used: {exc}. Not analysed: {left}")
                log.error("%s", result.notes[-1])
                fatal = exc
                break
            except Exception as exc:  # LLMError, or a bug: note it, back off this ticker, go on with the next
                failures = self.store.record_analysis_failure(candidate.ticker, when=now, error=str(exc))
                result.notes.append(
                    f"Analysis of {candidate.ticker} failed ({_count(failures, 'time')} in a row), "
                    f"retrying after {format_when(_retry_at(now, failures))}: {exc}"
                )
                log.warning("%s", result.notes[-1], exc_info=not isinstance(exc, LLMError))
                continue
            self.store.clear_analysis_failures(candidate.ticker)
            result.opportunities.append(self.store.add_opportunity(opportunity))

        try:
            if result.opportunities:
                result.report_paths = write_reports(
                    result.opportunities, self.settings.data_dir, generated=now, notes=result.notes
                )
            judges = recipients if recipients else [self.default_recipient()]
            result.alerts = [opp for opp in result.opportunities if any(self.is_alert(opp, r) for r in judges)]
            result.recipients = len(recipients or [])
            sending = [recipient for recipient in recipients or [] if recipient.notifiers]
            if self.notify and sending:
                self._send_alerts(now, result, sending)
            elif self.notify and recipients is None:
                pass  # the recipients couldn't be loaded (noted): this cycle's alerts wait for the next cycle
            else:  # shown in the summary and the report; a later notifying run mustn't push them (like `analyze`)
                self.store.mark_notified(
                    [opp.id for opp in result.opportunities if opp.id is not None], when=now, sent=False
                )
        finally:
            if fatal is not None:
                raise fatal  # after the report and the alerts, so the analyses already paid for aren't stranded
        self._prune(now)
        result.usage_today = self._usage_today(now)
        self._check_health(now, result, recipients)
        result.finished = now + timedelta(seconds=time.monotonic() - started)

    def _record(self, result: CycleResult, started: float, *, error: Exception | None = None) -> None:
        """Store a finished cycle in the cycles table; bookkeeping never breaks a cycle."""
        finished = result.finished or result.started + timedelta(seconds=time.monotonic() - started)
        if error is None:
            summary = result.summary()
        else:
            reason = one_line(scrub(f"{type(error).__name__}: {error}", secrets_of(self.settings)))
            summary = f"Cycle {format_when(result.started)} failed: {reason}"
        try:
            self.store.record_cycle(
                started=result.started,
                finished=finished,
                summary=summary,
                notes=result.notes,
                stats=cycle_stats(result),
                ok=error is None,
            )
        except Exception as exc:  # sqlite3.Error, or a bug: the cycle's own outcome matters more
            log.warning("Couldn't record the cycle: %s", exc)

    def _recipients(self, now: datetime, result: CycleResult) -> list[Recipient] | None:
        """The recipients of this cycle's alerts; None when they couldn't be loaded (noted)."""
        if self._recipients_of is None:
            return [self.default_recipient()]
        try:
            return list(self._recipients_of(now))
        except Exception as exc:  # the database, or a bug: the alerts wait for the next cycle instead
            result.notes.append(f"Couldn't load the alert recipients, so alerts wait for the next cycle: {exc}")
            log.warning("%s", result.notes[-1], exc_info=True)
            return None

    def _candidate_config(self, now: datetime, result: CycleResult) -> ScannerConfig:
        """The config for candidate selection: [universe] watchlist together with the watchlist callable's symbols
        (read through preferred_listings)."""
        if self._watchlist_of is None:
            return self.config
        try:
            extra = list(self._watchlist_of(now))
        except Exception as exc:
            result.notes.append(f"Couldn't load the users' watchlists, scanning with [universe] watchlist only: {exc}")
            log.warning("%s", result.notes[-1], exc_info=True)
            return self.config
        universe = self.config.universe
        symbols = (normalise_ticker(item) for item in extra if isinstance(item, str))
        union = tuple(
            dict.fromkeys([*universe.watchlist, *(universe.preferred_listings.get(s, s) for s in symbols if s)])
        )
        if union == universe.watchlist:
            return self.config
        return replace(self.config, universe=replace(universe, watchlist=union))

    def _currencies(self, now: datetime, recipients: list[Recipient] | None = None) -> tuple[str, ...]:
        """The currencies to store exchange rates into with an analysis: [account] currency, the recipients' and the
        currencies callable's."""
        wanted = [self.config.account.currency, *(recipient.currency for recipient in recipients or [])]
        if self._currencies_of is not None:
            try:
                wanted += list(self._currencies_of(now))
            except Exception as exc:  # only the ≈ amounts of some users are missing
                log.warning("Couldn't load the users' currencies: %s", exc, exc_info=True)
        return tuple(dict.fromkeys(code.strip().upper() for code in wanted if isinstance(code, str) and code.strip()))

    def is_alert(self, opp: Opportunity, recipient: Recipient | None = None) -> bool:
        """Whether an opportunity alerts a recipient (by default the command line's, i.e. [alerts]): it passes their
        min_score, min_probability and verdicts and, with only_watchlist, its ticker is on their watchlist."""
        recipient = recipient or self.default_recipient()
        return self._passes(opp, recipient) and (not recipient.only_watchlist or opp.ticker in recipient.watchlist)

    @staticmethod
    def _passes(opp: Opportunity, recipient: Recipient) -> bool:
        """Whether an opportunity passes a recipient's rules (min_score, min_probability, verdicts), watchlist aside."""
        alerts = recipient.alerts
        return (
            opp.score >= alerts.min_score
            and opp.analysis.probability_up_6m >= alerts.min_probability
            and opp.analysis.verdict in alerts.verdicts
        )

    def _daily_limit(self, candidates: list[Candidate], now: datetime, result: CycleResult) -> list[Candidate]:
        """The candidates that fit under [scan] max_analyses_per_day (analyses the model answered in the last 24
        hours, `dip-scanner analyze` included); the rest are noted and stay candidates for a later cycle."""
        limit = self.config.scan.max_analyses_per_day
        if limit <= 0 or not candidates:
            return candidates
        done = self.store.analyses_since(now - DAY)
        room = max(0, limit - done)
        if len(candidates) <= room:
            return candidates
        left = ", ".join(f"{c.ticker} (severity {c.severity:.1f})" for c in candidates[room:])
        result.notes.append(
            f"Daily limit of {_count(limit, 'analysis', 'analyses')} reached ([scan] max_analyses_per_day; {done} in "
            f"the last 24h), left for later: {left}"
        )
        log.info("%s", result.notes[-1])
        return candidates[:room]

    def _usage_today(self, now: datetime) -> list[ModelUsage]:
        try:
            return self.store.model_usage(since=now.replace(hour=0, minute=0, second=0, microsecond=0))
        except sqlite3.Error as exc:
            log.warning("Couldn't total today's model use: %s", exc)
            return []

    # --- system notices ---

    def _check_health(self, now: datetime, result: CycleResult, recipients: list[Recipient] | None = None) -> None:
        """Count the cycles in a row in which the model couldn't be used, or every feed failed, and send a system
        notice once a count reaches [alerts] notice_after_cycles (see notices.py) to the recipients that get notices
        (admins and "default"; the notifiers passed in when the recipients couldn't be loaded). A cycle in which the
        model answered (or a feed did) resets its count, also when a later request of it was throttled; a cycle that
        didn't need the model leaves it. Never raises."""
        needed = self.config.alerts.notice_after_cycles
        if recipients is None:
            notifiers = self.notifiers
        else:
            notifiers = [notifier for r in recipients if r.gets_notices for notifier in r.notifiers]
        try:
            if result.model_calls:
                self.store.reset_streak(MODEL_UNAVAILABLE)
            elif result.model_unavailable is not None:
                streak = self.store.bump_streak(MODEL_UNAVAILABLE)
                if streak >= needed:
                    self._notice(
                        MODEL_UNAVAILABLE,
                        now,
                        notifiers,
                        subject=f"dip-scanner: the language model has been unavailable for {streak} cycles",
                        lines=[
                            f"The last {streak} cycles in a row couldn't use the language model (the latest at "
                            f"{format_when(now)}): {one_line(result.model_unavailable)}",
                            "Meanwhile new articles wait for triage and candidates for their analysis; articles "
                            "older than [scan] max_article_age_hours are skipped for good. Check the provider's "
                            "status page, the network, and your rate limits and quota.",
                        ],
                    )
            if result.feeds_failed and not result.feeds_ok:
                streak = self.store.bump_streak(FEEDS_FAILING)
                if streak >= needed:
                    keys = {feed.key for feed in self.feeds}
                    errors = [
                        f"{row['key']}: {one_line(row['last_error'], 150)}"
                        for row in self.store.feed_health()
                        if row["key"] in keys and row["last_error"]
                    ]
                    self._notice(
                        FEEDS_FAILING,
                        now,
                        notifiers,
                        subject=f"dip-scanner: every feed has failed for {streak} cycles",
                        lines=[
                            f"All {_count(result.feeds_failed, 'feed')} failed in each of the last {streak} cycles "
                            f"(the latest at {format_when(now)}), so no news is coming in. Check the network "
                            "connection with `dip-scanner feeds --check`.",
                            "Latest errors: " + "; ".join(errors[:3]) + (" ..." if len(errors) > 3 else ""),
                        ],
                    )
            elif result.feeds_ok:
                self.store.reset_streak(FEEDS_FAILING)
        except Exception:  # bookkeeping and notices must never break a cycle
            log.warning("Couldn't check whether to send a system notice.", exc_info=True)

    def _notice(self, kind: str, now: datetime, notifiers: list[Notifier], *, subject: str, lines: list[str]) -> None:
        if not (self.notify and notifiers and self.config.alerts.system_notices):
            return
        send_notice(
            notifiers,
            self.store,
            kind=kind,
            subject=subject,
            lines=lines,
            now=now,
            secrets=secrets_of(self.settings),
        )

    def _waiting(self, ticker: str, now: datetime) -> str | None:
        """Why a ticker waits (its last analysis failed recently, see FAILURE_BACKOFF), or None when it may go."""
        failed = self.store.analysis_failures(ticker)
        if failed is not None and now < (retry := _retry_at(failed[1], failed[0])):
            return f"{ticker} ({_count(failed[0], 'failure')}, next try {format_clock(retry)})"
        return None

    def _analyze(
        self,
        candidate: Candidate,
        now: datetime,
        *,
        context_news: bool | None = None,
        result: CycleResult | None = None,
        currencies: tuple[str, ...] | None = None,
        recipients: list[Recipient] | None = None,
    ) -> Opportunity:
        """Gather per-ticker news and fundamentals for a candidate and ask the analysis model, or the debate's models
        (their calls recorded). The opportunity carries the exchange rates into currencies (by default [account]
        currency and the currencies callable's), and [account] currency's as its fx_rate. recipients: the cycle's
        (None: asked for here when a debate needs their alert rules or a notice)."""
        if context_news is None:
            context_news = self.config.scan.context_news
        extra = []
        if context_news:
            extra = ticker_news(
                self.session, candidate.ticker, company=candidate.company, now=now, limit=CONTEXT_NEWS_LIMIT
            )
        # A preferred listing (ASML.AS) gets the SEC figures of the company's US listing (ASML).
        sec_ticker = self.config.universe.sec_symbol(candidate.ticker)
        fundamentals = self.fundamentals.get(sec_ticker) if self.fundamentals is not None else None
        if isinstance(self.analysis_model, DebatePanel):
            opportunity = self._debate(
                self.analysis_model,
                candidate,
                now,
                fundamentals=fundamentals,
                extra=extra,
                sec_ticker=sec_ticker,
                result=result,
                recipients=recipients,
            )
        else:
            model = MeteredModel(self.analysis_model, self.store, step="analysis", when=now, ticker=candidate.ticker)
            try:
                opportunity = analyze_candidate(
                    model, candidate, fundamentals=fundamentals, extra_news=extra, now=now, sec_ticker=sec_ticker
                )
            finally:
                if result is not None:
                    result.model_calls += model.answered
        if currencies is None:
            currencies = self._currencies(now)
        return self._with_benchmark_level(self._with_fx(opportunity, now, result, currencies), now)

    def _debate(
        self,
        panel: DebatePanel,
        candidate: Candidate,
        now: datetime,
        *,
        fundamentals: Fundamentals | None,
        extra: list[Article],
        sec_ticker: str,
        result: CycleResult | None,
        recipients: list[Recipient] | None,
    ) -> Opportunity:
        """A candidate's analysis as a debate (debate.py): every call metered with its step, the recipients' alert
        rules for the agreement check, and a failed debater noted and told to those who get system notices. Without
        recipients (they couldn't be loaded) [alerts] and the notifiers passed in stand in for them."""
        if recipients is None and result is None:  # a manual analysis; in a cycle they couldn't be loaded (noted)
            recipients = self._recipients(now, CycleResult(started=now))
        meters: list[MeteredModel] = []

        def meter(model: ChatModel, step: str) -> ChatModel:
            metered = MeteredModel(model, self.store, step=step, when=now, ticker=candidate.ticker)
            meters.append(metered)
            return metered

        try:
            outcome = debate_candidate(
                panel,
                candidate,
                fundamentals=fundamentals,
                extra_news=extra,
                now=now,
                sec_ticker=sec_ticker,
                config=self.config.debate,
                alert_rules=self._alert_rules(candidate.ticker, recipients),
                meter=meter,
            )
        finally:
            if result is not None:
                result.model_calls += sum(metered.answered for metered in meters)
        for failure in outcome.failures:
            if result is not None:
                result.notes.append(_failure_note(candidate.ticker, failure))
            if failure.stage == "opening":
                self._debater_notice(failure, candidate.ticker, now, recipients)
        return outcome.opportunity

    def _alert_rules(self, ticker: str, recipients: list[Recipient] | None) -> list[AlertConfig]:
        """The alert rules the debate's agreement check compares the openings with: those of every recipient the
        ticker could alert ([alerts] when there are none)."""
        rules = [
            recipient.alerts
            for recipient in recipients or []
            if not recipient.only_watchlist or ticker in recipient.watchlist
        ]
        return rules or [self.config.alerts]

    def _debater_notice(
        self, failure: DebaterFailure, ticker: str, now: datetime, recipients: list[Recipient] | None
    ) -> None:
        """Tell the recipients that get system notices that a debater failed and the other analyses alone (at most once
        per provider every 12 hours, notices.send_notice). Never raises."""
        if recipients is None:
            notifiers = self.notifiers
        else:
            notifiers = [notifier for r in recipients if r.gets_notices for notifier in r.notifiers]
        try:
            self._notice(
                debater_notice_kind(failure.provider),
                now,
                notifiers,
                subject=debater_notice_subject(failure.label, failure.other),
                lines=debater_notice_lines(failure.label, failure.other, ticker, str(failure.error), now),
            )
        except Exception:  # a notice must never cost the analysis
            log.warning("Couldn't send the notice about the failed debater %s.", failure.label, exc_info=True)

    def _with_benchmark_level(self, opp: Opportunity, now: datetime) -> Opportunity:
        """The opportunity with its exchange's benchmark index and that index's level now, when the index's quote is
        from the same session as the opportunity's price (so `track` compares both from the same moment, also for
        a report made while the market is open). Without one the level stays None; the analysis is never lost."""
        symbol = benchmark_for(opp.ticker)
        try:
            index = self.prices.stats(symbol, now=now)  # cached: one request per index and cycle
        except Exception as exc:  # PriceError, PriceFetchError, or a bug: only the track record's start point
            log.warning(
                "No quote for the benchmark index %s of %s: %s",
                symbol,
                opp.ticker,
                exc,
                exc_info=not isinstance(exc, PriceError | PriceFetchError),
            )
            return replace(opp, benchmark=symbol)
        same_session = session_day(index) == quote_day(opp)
        return replace(opp, benchmark=symbol, benchmark_level=index.price if same_session else None)

    def _with_fx(
        self, opp: Opportunity, now: datetime, result: CycleResult | None, currencies: tuple[str, ...]
    ) -> Opportunity:
        """The opportunity with today's exchange rates (Yahoo) into each of currencies, and [account] currency with
        its rate as account_currency and fx_rate. Without a rate the amounts stay in the trading currency only, with a
        note; the analysis is never lost over it."""
        if not currencies:
            return opp
        rates, problems = self.fx.rates(opp.currency, currencies, now=now)
        for code, exc in problems.items():
            message = f"No {opp.currency}/{code} exchange rate for {opp.ticker}, amounts in {opp.currency} only: {exc}"
            log.warning("%s", message, exc_info=not isinstance(exc, PriceError | PriceFetchError))
            if result is not None:
                result.notes.append(message)
        account = self.config.account.currency
        if not account:
            return replace(opp, fx_rates=rates)
        return replace(opp, account_currency=account, fx_rate=rates.get(account), fx_rates=rates)

    def _send_alerts(self, now: datetime, result: CycleResult, recipients: list[Recipient]) -> None:
        """Send each recipient its alerts and thesis changes (see _send_alerts_to), in its own time zone. A problem
        with one recipient is noted and the others still get theirs; a database error stops the cycle."""
        for recipient in recipients:
            try:
                with display_zone_as(recipient.tz):
                    self._send_alerts_to(recipient, now, result)
            except sqlite3.Error:
                raise
            except Exception as exc:  # a bug in one recipient's settings or channels must not cost the others
                result.notes.append(f"Couldn't work out the alerts{_to(recipient)}: {type(exc).__name__}: {exc}")
                log.warning("%s", result.notes[-1], exc_info=True)

    def _send_alerts_to(self, recipient: Recipient, now: datetime, result: CycleResult) -> None:
        """Send one recipient this cycle's alerts and thesis changes, plus any from the last day that couldn't be sent
        (only those from recipient.since on), with its own alert state in the database.

        Only the newest analysis of a ticker counts: an older unsent one is superseded (never sent, even when the
        newer analysis isn't an alert). An alert for a ticker already alerted to the recipient within its
        repeat_hours is only sent when something material changed (see _material); otherwise it is noted and
        dropped. A new analysis of a ticker alerted to it within THESIS_WINDOW that no longer passes its rules (or
        whose chance of being higher fell by THESIS_DROP_POINTS) is sent as a "thesis change", so open orders on the
        earlier idea get reviewed (unless the recipient turned those off).

        Email and generic webhooks get the full report; Slack, Discord and Telegram get the compact short_alert text,
        with amounts in the recipient's currency. Messages count as sent to the recipient when at least one of its
        notifiers took them; alerts from an earlier cycle are labelled as such.
        """
        since = now - ALERT_RETRY_WINDOW
        if recipient.since is not None and utc(recipient.since) > since:
            since = utc(recipient.since)
        alerts: list[Opportunity] = []
        changes: list[tuple[Opportunity, Opportunity]] = []
        handled: list[Opportunity] = []  # decided not to send: superseded or a repeat
        repeats: list[str] = []
        seen: set[str] = set()
        for opp in self.store.unnotified(since=since, recipient=recipient.key):  # newest first
            latest = self.store.last_opportunity(opp.ticker)
            if opp.ticker in seen or (latest is not None and latest.id != opp.id):
                handled.append(opp)  # a newer analysis of this ticker exists
                continue
            seen.add(opp.ticker)
            previous = self.store.last_alerted(opp.ticker, before=opp.created, recipient=recipient.key)
            if previous is not None and utc(previous.created) < now - THESIS_WINDOW:
                previous = None
            if (
                recipient.thesis_changes
                and previous is not None
                and self._passes(previous, recipient)
                and self._weakened(previous, opp, recipient)
            ):
                changes.append((previous, opp))
            elif self.is_alert(opp, recipient):
                recent = previous is not None and utc(previous.created) >= now - _repeat_window(recipient)
                if recent and self._passes(previous, recipient) and not self._material(opp, previous, recipient):
                    handled.append(opp)
                    repeats.append(f"{opp.ticker} (score {opp.score:.1f}, alerted at {previous.score:.1f})")
                else:
                    alerts.append(opp)
        if repeats:
            result.notes.append(
                f"Alerted within the last {recipient.alerts.repeat_hours:g}h and nothing material changed, not "
                f"sent{_to(recipient)} again: {', '.join(repeats)}"
            )
        if handled:
            ids = [opp.id for opp in handled if opp.id is not None]
            self.store.record_deliveries(recipient.key, ids, "handled", when=now, sent=False)

        def shown(opp: Opportunity) -> Opportunity:  # amounts in the recipient's currency
            return opp.in_currency(recipient.currency)

        if alerts:
            alerts.sort(key=lambda opp: opp.score, reverse=True)
            views = [shown(opp) for opp in alerts]
            self._deliver(
                recipient,
                alerts,
                result,
                kind="alert",
                subject=alert_subject(views),
                markdown=render_markdown(views, title=ALERT_TITLE, generated=now),
                html=render_html(views, title=ALERT_TITLE, generated=now),
                short=short_alert(views, now=now),
                what="alerts",
                now=now,
            )
        if changes:
            lines = [thesis_change_line(shown(previous), shown(opp)) for previous, opp in changes]
            views = [shown(opp) for _, opp in changes]
            self._deliver(
                recipient,
                [opp for _, opp in changes],
                result,
                kind="thesis",
                subject=thesis_subject([(shown(previous), shown(opp)) for previous, opp in changes]),
                markdown=render_markdown(views, title=THESIS_TITLE, generated=now, notes=lines),
                html=render_html(views, title=THESIS_TITLE, generated=now, notes=lines),
                short="\n".join(["Thesis change: review open orders on these ideas.", *lines, _NOT_ADVICE]),
                what="thesis changes",
                now=now,
            )
            known = {(previous.id, opp.id) for previous, opp in result.thesis_changes}
            result.thesis_changes += [pair for pair in changes if (pair[0].id, pair[1].id) not in known]

    def _deliver(
        self,
        recipient: Recipient,
        opps: list[Opportunity],
        result: CycleResult,
        *,
        kind: str,
        subject: str,
        markdown: str,
        html: str,
        short: str,
        what: str,
        now: datetime,
    ) -> None:
        """Send one message to each of a recipient's notifiers and record the outcome for it: sent once any notifier
        took it, else tried (with the reasons), so the next cycle tries again."""
        sent = False
        problems: list[str] = []
        for notifier in recipient.notifiers:
            name = getattr(notifier, "name", type(notifier).__name__)
            try:
                notifier.send(subject, short if _is_chat(notifier) else markdown, html)
            except NotifyError as exc:
                problems.append(f"{name}: {exc}")
                result.notes.append(f"Couldn't send the {what}{_to(recipient)} by {name}: {exc}")
                log.warning("%s", result.notes[-1])
            except Exception as exc:  # a notifier bug must not stop the scanner
                problems.append(f"{name}: {type(exc).__name__}: {exc}")
                result.notes.append(f"Couldn't send the {what}{_to(recipient)} by {name}: {type(exc).__name__}: {exc}")
                log.warning("%s", result.notes[-1], exc_info=True)
            else:
                sent = True
                log.info("Sent %s%s by %s.", _count(len(opps), what.rstrip("s")), _to(recipient), name)
        ids = [opp.id for opp in opps if opp.id is not None]
        detail = None if sent else "; ".join(problems)
        self.store.record_deliveries(recipient.key, ids, kind, when=now, sent=sent, detail=detail)
        if sent:
            result.sent += len(ids)

    def _material(self, opp: Opportunity, previous: Opportunity, recipient: Recipient) -> bool:
        """Whether a new alert says something the last one didn't: the score rose by the recipient's
        min_score_change, the verdict changed, or the price fell by another [dip] min_drop_1d_pct since."""
        further_drop = opp.stats.price <= previous.stats.price * (1 - self.config.dip.min_drop_1d_pct / 100)
        return (
            opp.score - previous.score >= recipient.alerts.min_score_change
            or opp.analysis.verdict != previous.analysis.verdict
            or (previous.stats.currency == opp.stats.currency and further_drop)
        )

    def _weakened(self, previous: Opportunity, opp: Opportunity, recipient: Recipient) -> bool:
        """Whether a new analysis undercuts an earlier alert: it doesn't pass the recipient's rules any more, or its
        chance of being higher in 6 months is THESIS_DROP_POINTS or more lower."""
        drop = previous.analysis.probability_up_6m - opp.analysis.probability_up_6m
        return not self._passes(opp, recipient) or drop >= THESIS_DROP_POINTS

    def _prune(self, now: datetime) -> None:
        if self._last_prune is not None and now - self._last_prune < PRUNE_EVERY:
            return
        self._last_prune = now
        self.store.prune(older_than=now - timedelta(days=self.config.scan.retention_days))

    # --- the watch loop ---

    def watch(
        self,
        *,
        interval_minutes: float | None = None,
        max_cycles: int | None = None,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        """Run cycles on the interval until interrupted, stopped (stop()) or max_cycles intervals have passed.

        The first cycle starts at once; later ones start on the interval's boundaries (with 5 minutes: :00, :05,
        :10, ...), and a cycle that overruns skips to the next boundary. Each cycle logs a one-line summary. A cycle
        that fails is logged and the loop goes on, except for LLMSetupError and ConfigError, which propagate.
        Ctrl+C (KeyboardInterrupt) stops the loop cleanly.

        While the scanner is paused (Store.set_scanner_paused, from the website) the loop skips its cycles, saying so
        once in the log; request_cycle_now() runs one at once, paused or not. The wait between cycles is sleep when
        given (tests), else an event that request_cycle_now() and stop() interrupt.
        """
        minutes = interval_minutes if interval_minutes is not None else self.config.scan.interval_minutes
        if minutes <= 0:
            raise ConfigError(f"The interval must be greater than zero (got {minutes:g} minutes).")
        seconds = minutes * 60
        wait = sleep if sleep is not None else self._wait
        cycles = slots = 0
        paused_logged = False
        log.info("Watching %s every %g minute(s); press Ctrl+C to stop.", _count(len(self.feeds), "feed"), minutes)
        self.watching = True
        try:
            while not self._stopping.is_set():
                requested = self._requested.is_set()
                self._requested.clear()
                if not requested and self.paused():
                    if not paused_logged:
                        log.info("The scanner is paused: no cycles run until it is resumed on the admin page.")
                        paused_logged = True
                else:
                    if paused_logged and not requested:
                        log.info("The scanner was resumed.")
                        paused_logged = False
                    try:
                        log.info("%s", self.run_cycle(clock()).summary())
                    except (LLMSetupError, ConfigError):
                        raise
                    except Exception:
                        log.exception("The cycle failed; trying again at the next interval.")
                    cycles += 1
                slots += 1
                if (max_cycles is not None and slots >= max_cycles) or self._stopping.is_set():
                    break
                current = utc(clock())
                delay = seconds_until_next(current, seconds)
                self.next_cycle_at = current + timedelta(seconds=delay)
                wait(delay)
        except KeyboardInterrupt:
            pass
        finally:
            self.watching = False
            self.next_cycle_at = None
        log.info("Stopped after %s.", _count(cycles, "cycle"))

    def paused(self) -> bool:
        """Whether the scanner is paused (app_state SCANNER_PAUSED); False when the database can't say."""
        try:
            return self.store.scanner_paused()
        except sqlite3.Error as exc:
            log.warning("Couldn't read whether the scanner is paused: %s", exc)
            return False

    def request_cycle_now(self) -> None:
        """Ask the watch loop (in another thread) for a cycle now, even while paused: it starts at once when the loop
        is waiting, or right after the cycle that is running. Safe to call from any thread."""
        self._requested.set()
        self._wake.set()

    def stop(self) -> None:
        """Ask the watch loop (in another thread) to stop: it returns once the cycle that is running has finished, or
        at once when it is waiting. Safe to call from any thread, also before watch() starts."""
        self._stopping.set()
        self._wake.set()

    def _wait(self, seconds: float) -> None:
        """Wait until the next cycle is due, or until request_cycle_now() or stop()."""
        self._wake.wait(seconds)
        self._wake.clear()

    # --- manual analysis ---

    def analyze_ticker(self, ticker: str, now: datetime | None = None) -> Opportunity:
        """Analyse one ticker on demand, ignoring dip thresholds and the cooldown. The result isn't stored.

        A symbol with a [universe] preferred_listings entry is analysed as that listing ("ASML" as ASML.AS), like a
        scan cycle reads it. Uses the ticker's stored impacts from the [scan] lookback window, those filed under the
        symbols whose news belongs to it included (the preferred listing's other symbols, an old symbol replaced by
        it: OPAP.AT's for ALWN.AT), plus fresh per-ticker headlines (always fetched here, whatever [scan]
        context_news says). Raises PriceError / PriceFetchError when there are no prices, LLMError when the model's
        reply is unusable. A PriceError names the symbol that replaces this one when the scanner found one (see
        symbols.py), e.g. "try `dip-scanner analyze ALWN.AT`" for OPAP.AT.
        """
        now = utc(now) if now is not None else utc(self._clock())
        symbol = normalise_ticker(ticker) or ticker.strip().upper()
        preferred = self.config.universe.preferred_listings
        if symbol in preferred:
            log.info("%s is read as %s ([universe] preferred_listings).", symbol, preferred[symbol])
            symbol = preferred[symbol]
        aliases = symbol_aliases(self.store, symbol, preferred, now=now)
        since = now - timedelta(hours=self.config.scan.lookback_hours)
        impacts: list[tuple[Impact, Article]] = []
        seen: set[str] = set()
        for impact, article in self.store.recent_impacts(since):
            if impact.ticker in aliases and article.id not in seen:
                seen.add(article.id)
                impacts.append((replace(impact, ticker=symbol), article))
        names = Counter(impact.company for impact, _ in impacts if impact.company)
        try:
            stats = self.prices.stats(symbol, now=now)
        except PriceError as exc:
            texts = [f"{article.title} {article.summary}" for _, article in impacts]
            found = self._replacement(symbol, [name for name, _ in names.most_common()], texts, now)
            if found is None:
                raise
            raise PriceError(
                f"{exc} Yahoo's search finds {found.symbol} ({found.name}) for {found.query}: try "
                f"`dip-scanner analyze {found.symbol}`."
            ) from exc
        company = (stats.name or "").strip() or (names.most_common(1)[0][0] if names else symbol)
        reasons = dip_reasons(stats, self.config.dip, now=now)
        unpriced = news_after_session(stats, impacts)  # as in a cycle, so its cooldown ends when the market moves
        if unpriced:
            reasons.append(f"all of this news came out after the last session ({session_day(stats):%a %d %b})")
        candidate = Candidate(
            ticker=symbol,
            company=company,
            stats=stats,
            impacts=impacts,
            dip_reasons=reasons,
            severity=severity(stats, impacts),
            news_after_session=unpriced,
        )
        return self._analyze(candidate, now, context_news=True)

    def _replacement(self, symbol: str, companies: list[str], texts: list[str], now: datetime) -> Resolution | None:
        """The symbol found for one without prices: remembered from a scan cycle, or searched for by the company
        names the triage gave it (texts: its stories). None when there is no resolver, nothing matches or the search
        fails."""
        if self.symbols is None:
            return None
        try:
            return self.symbols.known(symbol, now=now) or self.symbols.resolve(symbol, companies, now=now, texts=texts)
        except Exception as exc:  # only a hint: the price error is what counts
            log.debug("Couldn't look up a new symbol for %s: %s", symbol, exc)
            return None


_NOT_ADVICE = "Not investment advice; check before placing or cancelling any order."


def thesis_subject(changes: list[tuple[Opportunity, Opportunity]]) -> str:
    """The subject of a thesis-change notice, e.g. "Thesis change: AMD now Fundamental damage (was Temporary fear,
    entry $132.00) - review open orders"."""
    if len(changes) == 1:
        previous, opp = changes[0]
        return (
            f"Thesis change: {opp.ticker} now {verdict_label(opp.analysis.verdict)} "
            f"(was {verdict_label(previous.analysis.verdict)}, entry "
            f"{format_price(previous.analysis.entry_price, previous.currency)}) - review open orders"
        )
    names = ", ".join(opp.ticker for _, opp in changes[:4])
    more = f" and {len(changes) - 4} more" if len(changes) > 4 else ""
    return f"Thesis changes: {names}{more} - review open orders"


def thesis_change_line(previous: Opportunity, opp: Opportunity) -> str:
    """One line saying what changed between an earlier alert and the new analysis."""
    was, now = previous.analysis, opp.analysis
    return (
        f"{opp.ticker}: now {verdict_label(now.verdict)}, {now.probability_up_6m}% chance up in 6m, score "
        f"{opp.score:.1f} (analysed {format_when(opp.created)}). Was {verdict_label(was.verdict)}, "
        f"{was.probability_up_6m}%, entry {format_money(was.entry_price, previous)}, target "
        f"{format_money(was.target_price, previous)} (alerted {format_when(previous.created)}). "
        "If you placed orders on the earlier idea, review them."
    )


def alert_subject(opps: list[Opportunity]) -> str:
    """The notification subject, e.g. "Dip alert: AMD (score 72), NVDA (score 66)"."""
    ranked = sorted(opps, key=lambda opp: opp.score, reverse=True)
    names = ", ".join(f"{opp.ticker} (score {opp.score:.0f})" for opp in ranked[:4])
    more = f" and {len(ranked) - 4} more" if len(ranked) > 4 else ""
    return f"Dip alert{'s' if len(ranked) > 1 else ''}: {names}{more}"


def usage_summary(usage: list[ModelUsage]) -> str:
    """The day's model use in one phrase, e.g. "model today: 175 calls, 312.4k tokens in, 41.0k out"."""
    unmetered = sum(row.unmetered for row in usage)
    return (
        f"model today: {_count(sum(row.calls for row in usage), 'call')}, "
        f"{format_tokens(sum(row.input_tokens for row in usage))} tokens in, "
        f"{format_tokens(sum(row.output_tokens for row in usage))} out"
        + (f" ({unmetered} without token counts)" if unmetered else "")
    )


def usage_lines(usage: list[ModelUsage]) -> list[str]:
    """One line per step and model, e.g. "triage with gpt-5-mini: 162 calls, 230.1k tokens in, 30.2k out"."""
    return [
        f"{row.step} with {row.model}: {_count(row.calls, 'call')}, {format_tokens(row.input_tokens)} tokens in, "
        f"{format_tokens(row.output_tokens)} out"
        + (f" ({row.unmetered} without token counts)" if row.unmetered else "")
        for row in usage
    ]


def format_tokens(count: int) -> str:
    """A token count for people: 950, 312.4k, 1.25M."""
    if count < 1000:
        return str(count)
    if count < 1_000_000:
        return f"{count / 1000:.1f}k"
    return f"{count / 1_000_000:.2f}M"


def seconds_until_next(now: datetime, interval_seconds: float) -> float:
    """Seconds from now to the next multiple of the interval since the epoch (always > 0)."""
    timestamp = utc(now).timestamp()
    following = (math.floor(timestamp / interval_seconds) + 1) * interval_seconds
    return max(following - timestamp, 0.001)


def cycle_stats(result: CycleResult) -> dict[str, int]:
    """The counts of a cycle as stored with it (CycleRecord.stats)."""
    return {
        "feeds_ok": result.feeds_ok,
        "feeds_failed": result.feeds_failed,
        "new_articles": result.new_articles,
        "triaged": result.triaged,
        "impacts": result.impacts,
        "candidates": result.candidates,
        "opportunities": len(result.opportunities),
        "alerts": len(result.alerts),
        "thesis_changes": len(result.thesis_changes),
        "model_calls": result.model_calls,
        "recipients": result.recipients,
        "sent": result.sent,
    }


def _failure_note(ticker: str, failure: DebaterFailure) -> str:
    """A cycle note about a failed call of a debate."""
    reason = one_line(failure.error, 200)
    if failure.stage == "opening":
        return f"Debate of {ticker}: {failure.label} failed, so {failure.other} analysed it alone: {reason}"
    if failure.stage == "judge":
        return f"Debate of {ticker}: the judge {failure.label} failed, so the positions were merged by rule: {reason}"
    return f"Debate of {ticker}: {failure.label}'s rebuttal failed, so its earlier position stands: {reason}"


def _is_chat(notifier: object) -> bool:
    """Chat channels get the compact alert text instead of the whole report."""
    if isinstance(notifier, TelegramNotifier):
        return True
    return isinstance(notifier, WebhookNotifier) and notifier.format != "generic"


def _to(recipient: Recipient) -> str:
    """ " to jane@example.com" in notes about a website user's alerts; "" for the command line's own."""
    return "" if recipient.key == DEFAULT_RECIPIENT else f" to {recipient.label}"


def _repeat_window(recipient: Recipient) -> timedelta:
    return timedelta(hours=recipient.alerts.repeat_hours)


def _unavailable(result: CycleResult, reason: str) -> None:
    """Remember the first reason the model couldn't be used in this cycle."""
    if result.model_unavailable is None:
        result.model_unavailable = reason


def _retry_at(last_failure: datetime, failures: int) -> datetime:
    wait = min(MAX_FAILURE_BACKOFF, FAILURE_BACKOFF * 2 ** min(max(failures, 1) - 1, 10))
    return utc(last_failure) + wait


def _count(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else plural or singular + 's'}"
