"""One scan cycle (poll -> triage -> candidates -> analysis -> report -> notify), the watch loop and manual analysis.

Everything the scanner talks to (feeds, prices, the SEC, the models, notifiers and the database) is passed in, so
tests can run whole cycles with fakes.
"""

from __future__ import annotations

import logging
import math
import sqlite3
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
from .llm import ChatModel, LLMError, LLMSetupError, LLMUnavailableError, Usage
from .models import Candidate, Feed, ModelUsage, Opportunity, utc
from .notices import FEEDS_FAILING, MODEL_UNAVAILABLE, one_line, secrets_of, send_notice
from .notify import Notifier, NotifyError, TelegramNotifier, WebhookNotifier, short_alert
from .prices import YahooPrices
from .report import format_price, format_when, render_html, render_markdown, verdict_label, write_reports
from .store import Store
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

    def summary(self) -> str:
        """One line for the log, e.g. "Cycle 2026-09-25 15:00 UTC: 19/20 feeds ok, 37 new articles, ...", ending with
        the day's model use when there is any."""
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
        return f"Cycle {utc(self.started):%Y-%m-%d %H:%M} UTC: {', '.join(parts)}{took}{usage}"


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
        Without notifications (notify=False or no channel set up) the cycle's opportunities count as handled, so a
        later run doesn't send them.
        """
        now = utc(now) if now is not None else utc(self._clock())
        started = time.monotonic()
        scan = self.config.scan
        result = CycleResult(started=now)

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
            )
        finally:
            result.model_calls += triage_model.answered
        result.impacts = len(impacts)

        recent = self.store.recent_impacts(now - timedelta(hours=scan.lookback_hours))
        candidates, notes = select_candidates(
            recent, self.prices, self.store, self.config, now=now, waiting=lambda ticker: self._waiting(ticker, now)
        )
        result.notes.extend(notes)
        candidates = self._daily_limit(candidates, now, result)
        result.candidates = len(candidates)

        fatal: Exception | None = None
        for index, candidate in enumerate(candidates):
            try:
                opportunity = self._analyze(candidate, now, result=result)
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
                    f"retrying after {_retry_at(now, failures):%Y-%m-%d %H:%M} UTC: {exc}"
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
            result.alerts = [opp for opp in result.opportunities if self.is_alert(opp)]
            if self.notify and self.notifiers:
                self._send_alerts(now, result)
            else:  # shown in the summary and the report; a later notifying run mustn't push them (like `analyze`)
                self.store.mark_notified(
                    [opp.id for opp in result.opportunities if opp.id is not None], when=now, sent=False
                )
        finally:
            if fatal is not None:
                raise fatal  # after the report and the alerts, so the analyses already paid for aren't stranded
        self._prune(now)
        result.usage_today = self._usage_today(now)
        self._check_health(now, result)
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

    def _check_health(self, now: datetime, result: CycleResult) -> None:
        """Count the cycles in a row in which the model couldn't be used, or every feed failed, and send a system
        notice once a count reaches [alerts] notice_after_cycles (see notices.py). A cycle in which the model
        answered (or a feed did) resets its count; a cycle that didn't need the model leaves it. Never raises."""
        needed = self.config.alerts.notice_after_cycles
        try:
            if result.model_unavailable is not None:
                streak = self.store.bump_streak(MODEL_UNAVAILABLE)
                if streak >= needed:
                    self._notice(
                        MODEL_UNAVAILABLE,
                        now,
                        subject=f"dip-scanner: the language model has been unavailable for {streak} cycles",
                        lines=[
                            f"The last {streak} cycles in a row couldn't use the language model (the latest at "
                            f"{now:%Y-%m-%d %H:%M} UTC): {one_line(result.model_unavailable)}",
                            "Meanwhile new articles wait for triage and candidates for their analysis; articles "
                            "older than [scan] max_article_age_hours are skipped for good. Check the provider's "
                            "status page, the network, and your rate limits and quota.",
                        ],
                    )
            elif result.model_calls:
                self.store.reset_streak(MODEL_UNAVAILABLE)
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
                        subject=f"dip-scanner: every feed has failed for {streak} cycles",
                        lines=[
                            f"All {_count(result.feeds_failed, 'feed')} failed in each of the last {streak} cycles "
                            f"(the latest at {now:%Y-%m-%d %H:%M} UTC), so no news is coming in. Check the network "
                            "connection with `dip-scanner feeds --check`.",
                            "Latest errors: " + "; ".join(errors[:3]) + (" ..." if len(errors) > 3 else ""),
                        ],
                    )
            elif result.feeds_ok:
                self.store.reset_streak(FEEDS_FAILING)
        except Exception:  # bookkeeping and notices must never break a cycle
            log.warning("Couldn't check whether to send a system notice.", exc_info=True)

    def _notice(self, kind: str, now: datetime, *, subject: str, lines: list[str]) -> None:
        if not (self.notify and self.notifiers and self.config.alerts.system_notices):
            return
        send_notice(
            self.notifiers,
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
            return f"{ticker} ({_count(failed[0], 'failure')}, next try {retry:%H:%M} UTC)"
        return None

    def _analyze(
        self,
        candidate: Candidate,
        now: datetime,
        *,
        context_news: bool | None = None,
        result: CycleResult | None = None,
    ) -> Opportunity:
        """Gather per-ticker news and fundamentals for a candidate and ask the analysis model (its calls recorded)."""
        if context_news is None:
            context_news = self.config.scan.context_news
        extra = []
        if context_news:
            extra = ticker_news(
                self.session, candidate.ticker, company=candidate.company, now=now, limit=CONTEXT_NEWS_LIMIT
            )
        fundamentals = self.fundamentals.get(candidate.ticker) if self.fundamentals is not None else None
        model = MeteredModel(self.analysis_model, self.store, step="analysis", when=now, ticker=candidate.ticker)
        try:
            return analyze_candidate(model, candidate, fundamentals=fundamentals, extra_news=extra, now=now)
        finally:
            if result is not None:
                result.model_calls += model.answered

    def _send_alerts(self, now: datetime, result: CycleResult) -> None:
        """Send this cycle's alerts and thesis changes, plus any from the last day that couldn't be sent.

        Only the newest analysis of a ticker counts: an older unsent one is superseded (never sent, even when the
        newer analysis isn't an alert). An alert for a ticker already alerted within [alerts] repeat_hours is only
        sent when something material changed (see _material); otherwise it is noted and dropped. A new analysis of a
        ticker alerted within THESIS_WINDOW that no longer passes [alerts] (or whose chance of being higher fell by
        THESIS_DROP_POINTS) is sent as a "thesis change", so open orders on the earlier idea get reviewed.

        Email and generic webhooks get the full report; Slack, Discord and Telegram get the compact short_alert
        text. Messages count as sent (store.mark_notified) when at least one notifier took them; alerts from an
        earlier cycle are labelled as such.
        """
        alerts: list[Opportunity] = []
        handled: list[Opportunity] = []  # decided not to send: superseded or a repeat
        repeats: list[str] = []
        seen: set[str] = set()
        for opp in self.store.unnotified(since=now - ALERT_RETRY_WINDOW):  # newest first
            latest = self.store.last_opportunity(opp.ticker)
            if opp.ticker in seen or (latest is not None and latest.id != opp.id):
                handled.append(opp)  # a newer analysis of this ticker exists
                continue
            seen.add(opp.ticker)
            previous = self.store.last_alerted(opp.ticker, before=opp.created)
            if previous is not None and utc(previous.created) < now - THESIS_WINDOW:
                previous = None
            if previous is not None and self.is_alert(previous) and self._weakened(previous, opp):
                result.thesis_changes.append((previous, opp))
            elif self.is_alert(opp):
                recent = previous is not None and utc(previous.created) >= now - self._repeat_window()
                if recent and self.is_alert(previous) and not self._material(opp, previous):
                    handled.append(opp)
                    repeats.append(f"{opp.ticker} (score {opp.score:.1f}, alerted at {previous.score:.1f})")
                else:
                    alerts.append(opp)
        if repeats:
            result.notes.append(
                f"Alerted within the last {self.config.alerts.repeat_hours:g}h and nothing material changed, not "
                f"sent again: {', '.join(repeats)}"
            )
        if handled:
            self.store.mark_notified([opp.id for opp in handled if opp.id is not None], when=now, sent=False)

        if alerts:
            alerts.sort(key=lambda opp: opp.score, reverse=True)
            self._deliver(
                alerts,
                result,
                subject=alert_subject(alerts),
                markdown=render_markdown(alerts, title=ALERT_TITLE, generated=now),
                html=render_html(alerts, title=ALERT_TITLE, generated=now),
                short=short_alert(alerts, now=now),
                what="alerts",
                now=now,
            )
        if result.thesis_changes:
            lines = [thesis_change_line(previous, opp) for previous, opp in result.thesis_changes]
            changed = [opp for _, opp in result.thesis_changes]
            self._deliver(
                changed,
                result,
                subject=thesis_subject(result.thesis_changes),
                markdown=render_markdown(changed, title=THESIS_TITLE, generated=now, notes=lines),
                html=render_html(changed, title=THESIS_TITLE, generated=now, notes=lines),
                short="\n".join(["Thesis change: review open orders on these ideas.", *lines, _NOT_ADVICE]),
                what="thesis changes",
                now=now,
            )

    def _deliver(
        self,
        opps: list[Opportunity],
        result: CycleResult,
        *,
        subject: str,
        markdown: str,
        html: str,
        short: str,
        what: str,
        now: datetime,
    ) -> None:
        sent = False
        for notifier in self.notifiers:
            name = getattr(notifier, "name", type(notifier).__name__)
            try:
                notifier.send(subject, short if _is_chat(notifier) else markdown, html)
            except NotifyError as exc:
                result.notes.append(f"Couldn't send the {what} by {name}: {exc}")
                log.warning("%s", result.notes[-1])
            except Exception as exc:  # a notifier bug must not stop the scanner
                result.notes.append(f"Couldn't send the {what} by {name}: {type(exc).__name__}: {exc}")
                log.warning("%s", result.notes[-1], exc_info=True)
            else:
                sent = True
                log.info("Sent %s by %s.", _count(len(opps), what.rstrip("s")), name)
        if sent:
            self.store.mark_notified([opp.id for opp in opps if opp.id is not None], when=now)

    def _repeat_window(self) -> timedelta:
        return timedelta(hours=self.config.alerts.repeat_hours)

    def _material(self, opp: Opportunity, previous: Opportunity) -> bool:
        """Whether a new alert says something the last one didn't: the score rose by [alerts] min_score_change, the
        verdict changed, or the price fell by another [dip] min_drop_1d_pct since."""
        alerts = self.config.alerts
        further_drop = opp.stats.price <= previous.stats.price * (1 - self.config.dip.min_drop_1d_pct / 100)
        return (
            opp.score - previous.score >= alerts.min_score_change
            or opp.analysis.verdict != previous.analysis.verdict
            or (previous.stats.currency == opp.stats.currency and further_drop)
        )

    def _weakened(self, previous: Opportunity, opp: Opportunity) -> bool:
        """Whether a new analysis undercuts an earlier alert: it isn't an alert, or its chance of being higher in 6
        months is THESIS_DROP_POINTS or more lower."""
        drop = previous.analysis.probability_up_6m - opp.analysis.probability_up_6m
        return not self.is_alert(opp) or drop >= THESIS_DROP_POINTS

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
            dip_reasons=dip_reasons(stats, self.config.dip, now=now),
            severity=severity(stats, impacts),
        )
        return self._analyze(candidate, now, context_news=True)


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
        f"{was.probability_up_6m}%, entry {format_price(was.entry_price, previous.currency)}, target "
        f"{format_price(was.target_price, previous.currency)} (alerted {format_when(previous.created)}). "
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


def _is_chat(notifier: object) -> bool:
    """Chat channels get the compact alert text instead of the whole report."""
    if isinstance(notifier, TelegramNotifier):
        return True
    return isinstance(notifier, WebhookNotifier) and notifier.format != "generic"


def _unavailable(result: CycleResult, reason: str) -> None:
    """Remember the first reason the model couldn't be used in this cycle."""
    if result.model_unavailable is None:
        result.model_unavailable = reason


def _retry_at(last_failure: datetime, failures: int) -> datetime:
    wait = min(MAX_FAILURE_BACKOFF, FAILURE_BACKOFF * 2 ** min(max(failures, 1) - 1, 10))
    return utc(last_failure) + wait


def _count(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else plural or singular + 's'}"
