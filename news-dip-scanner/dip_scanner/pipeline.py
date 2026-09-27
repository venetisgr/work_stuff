"""One scan cycle (poll -> triage -> candidates -> analysis -> report -> notify), the watch loop and manual analysis.

Everything the scanner talks to (feeds, prices, the SEC, the models, notifiers and the database) is passed in, so
tests can run whole cycles with fakes.
"""

from __future__ import annotations

import logging
import math
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .analyze import analyze_candidate
from .config import ConfigError, ScannerConfig, Settings
from .detect import dip_reasons, select_candidates, severity
from .feeds import CONTACT_USER_AGENT_MISSING, fetch_all, needs_contact_user_agent, ticker_news, user_agent_for
from .fundamentals import SecFundamentals
from .llm import ChatModel, LLMError, LLMSetupError, LLMUnavailableError
from .models import Candidate, Feed, Opportunity, utc
from .notify import Notifier, NotifyError, TelegramNotifier, WebhookNotifier, short_alert
from .prices import YahooPrices
from .report import render_html, render_markdown, write_reports
from .store import Store
from .triage import normalise_ticker, triage

log = logging.getLogger(__name__)

CONTEXT_NEWS_LIMIT = 15  # per-ticker headlines (Yahoo + Google News) added to each analysis
PRUNE_EVERY = timedelta(days=1)
ALERT_RETRY_WINDOW = timedelta(hours=24)  # alerts that couldn't be sent are retried for this long
# After a failed analysis a ticker waits this long before the next try, doubling with every failure in a row (up to
# MAX_FAILURE_BACKOFF), so one article the model keeps choking on doesn't cost a request every five minutes.
FAILURE_BACKOFF = timedelta(minutes=30)
MAX_FAILURE_BACKOFF = timedelta(hours=24)
ALERT_TITLE = "Dip alerts"
MANUAL_TITLE = "Manual analysis"


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
    notes: list[str] = field(default_factory=list)
    report_paths: list[Path] = field(default_factory=list)

    def summary(self) -> str:
        """One line for the log, e.g. "Cycle 2026-09-25 15:00 UTC: 19/20 feeds ok, 37 new articles, ..."."""
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
        return f"Cycle {utc(self.started):%Y-%m-%d %H:%M} UTC: {', '.join(parts)}{took}"


class Scanner:
    """Runs scan cycles with everything it needs passed in (so tests can pass fakes).

    Feeds that are disabled, or that need a contact User-Agent nobody configured (sec.gov without SEC_USER_AGENT),
    are left out once, here, with a warning, instead of failing every cycle.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        config: ScannerConfig,
        feeds: list[Feed],
        store: Store,
        triage_model: ChatModel,
        analysis_model: ChatModel,
        prices: YahooPrices,
        fundamentals: SecFundamentals | None,
        notifiers: list[Notifier],
        session,
        notify: bool = True,
        clock: Callable[[], datetime] = _now,
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
        self.user_agents = {"sec.gov": settings.sec_user_agent}
        self.feeds = [feed for feed in feeds if feed.enabled and self._can_fetch(feed)]
        self._last_prune: datetime | None = None

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
        ok = failed = 0
        for result in results:
            self.store.save_feed_state(
                result.feed.key, result.state, status=result.status, error=result.error, fetched=now
            )
            if result.error is None:
                ok += 1
            else:
                failed += 1
        listed = [article for result in results for article in result.articles]
        new = self.store.add_articles(listed, max_age_hours=self.config.scan.max_article_age_hours, now=now)
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
        and ConfigError propagate: no later call could succeed. Notification failures are noted, never raised.
        """
        now = utc(now) if now is not None else utc(self._clock())
        started = time.monotonic()
        scan = self.config.scan
        result = CycleResult(started=now)

        result.feeds_ok, result.feeds_failed, result.new_articles = self.poll(now)
        result.triaged, impacts = triage(
            self.triage_model,
            self.store,
            batch_size=scan.triage_batch_size,
            max_attempts=scan.max_triage_attempts,
            now=now,
        )
        result.impacts = len(impacts)

        recent = self.store.recent_impacts(now - timedelta(hours=scan.lookback_hours))
        candidates, notes = select_candidates(recent, self.prices, self.store, self.config, now=now)
        result.notes.extend(notes)
        candidates = self._ready(candidates, now, result.notes)
        result.candidates = len(candidates)

        for index, candidate in enumerate(candidates):
            try:
                opportunity = self._analyze(candidate, now)
            except LLMUnavailableError as exc:
                left = ", ".join(c.ticker for c in candidates[index:])
                result.notes.append(f"The analysis model is unavailable, left for the next cycle: {left} ({exc})")
                log.warning("%s", result.notes[-1])
                break
            except (LLMSetupError, ConfigError):
                raise
            except Exception as exc:  # LLMError, or a bug: note it, back off this ticker, go on with the next
                failures = self.store.record_analysis_failure(candidate.ticker, when=now, error=str(exc))
                result.notes.append(
                    f"Analysis of {candidate.ticker} failed ({_count(failures, 'time')} in a row), "
                    f"retrying after {_retry_at(now, failures):%Y-%m-%d %H:%M} UTC: {exc}"
                )
                log.warning("%s", result.notes[-1], exc_info=not isinstance(exc, LLMError))
                continue
            self.store.clear_analysis_failures(candidate.ticker)
            result.opportunities.append(self.store.add_opportunity(opportunity))

        if result.opportunities:
            result.report_paths = write_reports(
                result.opportunities, self.settings.data_dir, generated=now, notes=result.notes
            )
        result.alerts = [opp for opp in result.opportunities if self.is_alert(opp)]
        if self.notify:
            self._send_alerts(now, result)
        self._prune(now)
        result.finished = now + timedelta(seconds=time.monotonic() - started)
        return result

    def is_alert(self, opp: Opportunity) -> bool:
        """Whether an opportunity passes [alerts]: min_score, min_probability and verdicts."""
        alerts = self.config.alerts
        return (
            opp.score >= alerts.min_score
            and opp.analysis.probability_up_6m >= alerts.min_probability
            and opp.analysis.verdict in alerts.verdicts
        )

    def _ready(self, candidates: list[Candidate], now: datetime, notes: list[str]) -> list[Candidate]:
        """The candidates whose last analysis didn't fail recently (see FAILURE_BACKOFF)."""
        ready, waiting = [], []
        for candidate in candidates:
            failed = self.store.analysis_failures(candidate.ticker)
            if failed is not None and now < (retry := _retry_at(failed[1], failed[0])):
                waiting.append(f"{candidate.ticker} ({_count(failed[0], 'failure')}, next try {retry:%H:%M} UTC)")
            else:
                ready.append(candidate)
        if waiting:
            notes.append(f"Analysis failed recently, waiting before trying again: {', '.join(waiting)}")
        return ready

    def _analyze(self, candidate: Candidate, now: datetime, *, context_news: bool | None = None) -> Opportunity:
        """Gather per-ticker news and fundamentals for a candidate and ask the analysis model."""
        if context_news is None:
            context_news = self.config.scan.context_news
        extra = []
        if context_news:
            extra = ticker_news(
                self.session, candidate.ticker, company=candidate.company, now=now, limit=CONTEXT_NEWS_LIMIT
            )
        fundamentals = self.fundamentals.get(candidate.ticker) if self.fundamentals is not None else None
        return analyze_candidate(self.analysis_model, candidate, fundamentals=fundamentals, extra_news=extra, now=now)

    def _send_alerts(self, now: datetime, result: CycleResult) -> None:
        """Send this cycle's alerts, plus any from the last day that couldn't be sent, to every notifier.

        Email and generic webhooks get the full report; Slack, Discord and Telegram get the compact short_alert
        text. The alerts count as sent (store.mark_notified) when at least one notifier took them.
        """
        if not self.notifiers:
            return
        pending = [opp for opp in self.store.unnotified(since=now - ALERT_RETRY_WINDOW) if self.is_alert(opp)]
        if not pending:
            return
        pending.sort(key=lambda opp: opp.score, reverse=True)
        subject = alert_subject(pending)
        markdown = render_markdown(pending, title=ALERT_TITLE, generated=now)
        html = render_html(pending, title=ALERT_TITLE, generated=now)
        short = short_alert(pending)
        if len(result.report_paths) > 1:
            short += f"\n\nFull report: {result.report_paths[1]}"
        sent = False
        for notifier in self.notifiers:
            name = getattr(notifier, "name", type(notifier).__name__)
            try:
                notifier.send(subject, short if _is_chat(notifier) else markdown, html)
            except NotifyError as exc:
                result.notes.append(f"Couldn't send the alerts by {name}: {exc}")
                log.warning("%s", result.notes[-1])
            except Exception as exc:  # a notifier bug must not stop the scanner
                result.notes.append(f"Couldn't send the alerts by {name}: {type(exc).__name__}: {exc}")
                log.warning("%s", result.notes[-1], exc_info=True)
            else:
                sent = True
                log.info("Sent %s by %s.", _count(len(pending), "alert"), name)
        if sent:
            self.store.mark_notified([opp.id for opp in pending if opp.id is not None], when=now)

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
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        """Run cycles on the interval until interrupted (or max_cycles).

        The first cycle starts at once; later ones start on the interval's boundaries (with 5 minutes: :00, :05,
        :10, ...), and a cycle that overruns skips to the next boundary. Each cycle logs a one-line summary. A cycle
        that fails is logged and the loop goes on, except for LLMSetupError and ConfigError, which propagate.
        Ctrl+C (KeyboardInterrupt) stops the loop cleanly.
        """
        minutes = interval_minutes if interval_minutes is not None else self.config.scan.interval_minutes
        if minutes <= 0:
            raise ConfigError(f"The interval must be greater than zero (got {minutes:g} minutes).")
        seconds = minutes * 60
        cycles = 0
        log.info("Watching %s every %g minute(s); press Ctrl+C to stop.", _count(len(self.feeds), "feed"), minutes)
        try:
            while True:
                try:
                    log.info("%s", self.run_cycle(clock()).summary())
                except (LLMSetupError, ConfigError):
                    raise
                except Exception:
                    log.exception("The cycle failed; trying again at the next interval.")
                cycles += 1
                if max_cycles is not None and cycles >= max_cycles:
                    return
                sleep(seconds_until_next(clock(), seconds))
        except KeyboardInterrupt:
            log.info("Stopped after %s.", _count(cycles, "cycle"))

    # --- manual analysis ---

    def analyze_ticker(self, ticker: str, now: datetime | None = None) -> Opportunity:
        """Analyse one ticker on demand, ignoring dip thresholds and the cooldown. The result isn't stored.

        Uses the ticker's stored impacts from the [scan] lookback window plus fresh per-ticker headlines (always
        fetched here, whatever [scan] context_news says). Raises PriceError / PriceFetchError when there are no
        prices, LLMError when the model's reply is unusable.
        """
        now = utc(now) if now is not None else utc(self._clock())
        symbol = normalise_ticker(ticker) or ticker.strip().upper()
        stats = self.prices.stats(symbol, now=now)
        since = now - timedelta(hours=self.config.scan.lookback_hours)
        impacts = [(impact, article) for impact, article in self.store.recent_impacts(since) if impact.ticker == symbol]
        names = Counter(impact.company for impact, _ in impacts if impact.company)
        company = (stats.name or "").strip() or (names.most_common(1)[0][0] if names else symbol)
        candidate = Candidate(
            ticker=symbol,
            company=company,
            stats=stats,
            impacts=impacts,
            dip_reasons=dip_reasons(stats, self.config.dip),
            severity=severity(stats, impacts),
        )
        return self._analyze(candidate, now, context_news=True)


def alert_subject(opps: list[Opportunity]) -> str:
    """The notification subject, e.g. "Dip alert: AMD (score 72), NVDA (score 66)"."""
    ranked = sorted(opps, key=lambda opp: opp.score, reverse=True)
    names = ", ".join(f"{opp.ticker} (score {opp.score:.0f})" for opp in ranked[:4])
    more = f" and {len(ranked) - 4} more" if len(ranked) > 4 else ""
    return f"Dip alert{'s' if len(ranked) > 1 else ''}: {names}{more}"


def seconds_until_next(now: datetime, interval_seconds: float) -> float:
    """Seconds from now to the next multiple of the interval since the epoch (always > 0)."""
    timestamp = utc(now).timestamp()
    following = (math.floor(timestamp / interval_seconds) + 1) * interval_seconds
    return max(following - timestamp, 0.001)


def _is_chat(notifier: object) -> bool:
    """Chat channels get the compact alert text instead of the whole report."""
    if isinstance(notifier, TelegramNotifier):
        return True
    return isinstance(notifier, WebhookNotifier) and notifier.format != "generic"


def _retry_at(last_failure: datetime, failures: int) -> datetime:
    wait = min(MAX_FAILURE_BACKOFF, FAILURE_BACKOFF * 2 ** min(max(failures, 1) - 1, 10))
    return utc(last_failure) + wait


def _count(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else plural or singular + 's'}"
