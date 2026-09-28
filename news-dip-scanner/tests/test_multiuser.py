"""Scan cycles with several recipients (the website's users): each gets alerts by their own rules, channels, currency
and time zone, with their own alert state; plus cycle records, the pause flag and the watch loop's controls."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import UTC, timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import FakeChatModel, FakeResponse, FakeSession, chart_json, make_article, make_impact
from test_pipeline import (
    AMD_SCORE,
    ANALYSIS,
    CYCLE,
    FEEDS,
    MARKETWATCH_URL,
    FakeNotifier,
    routes,
    rss,
    seed,
    triage_reply,
)

from dip_scanner.accounts import Accounts
from dip_scanner.config import AlertConfig, LLMSettings, ScanConfig, ScannerConfig, Settings
from dip_scanner.llm import LLMSetupError
from dip_scanner.notify import NotifyError, WebhookNotifier
from dip_scanner.pipeline import CycleResult, Scanner, cycle_stats
from dip_scanner.prices import YahooPrices
from dip_scanner.recipients import Recipient
from dip_scanner.report import format_when
from dip_scanner.store import DEFAULT_RECIPIENT, Store
from dip_scanner.web.jobs import InlineExecutor, JobRunner

QUIET = ScannerConfig(scan=ScanConfig(context_news=False, reanalyse_same_session_hours=0))
CHART = "https://query1.finance.yahoo.com/v8/finance/chart/"
EURUSD = CHART + "EURUSD%3DX"
GBPUSD = CHART + "GBPUSD%3DX"


def person(n: int, notifiers: list, **overrides) -> Recipient:
    """A website user as a recipient: user:<n>, userN@example.com, the default [alerts] rules, UTC."""
    values = {
        "key": f"user:{n}",
        "label": f"user{n}@example.com",
        "alerts": AlertConfig(),
        "watchlist": (),
        "only_watchlist": False,
        "notifiers": notifiers,
        "currency": None,
        "tz": UTC,
    }
    values.update(overrides)
    return Recipient(**values)


@pytest.fixture
def make(tmp_path):
    """Builds Scanners on one data folder; recipients may be a list (the same every cycle) or a callable."""
    stores: list[Store] = []

    def factory(
        *,
        recipients=None,
        watchlist=None,
        currencies=None,
        session: FakeSession | None = None,
        analysis_model=None,
        triage_model=None,
        config: ScannerConfig = QUIET,
        notifiers=(),
        feeds=None,
        settings: Settings | None = None,
    ) -> Scanner:
        session = session if session is not None else FakeSession(routes())
        settings = settings or Settings(data_dir=tmp_path / "data")
        store = Store(settings.data_dir / "scanner.sqlite3")
        stores.append(store)
        if isinstance(recipients, list):
            fixed = recipients
            recipients = lambda now: list(fixed)  # noqa: E731
        return Scanner(
            settings=settings,
            config=config,
            feeds=FEEDS[:1] if feeds is None else feeds,
            store=store,
            triage_model=triage_model or FakeChatModel(triage_reply),
            analysis_model=analysis_model or FakeChatModel(ANALYSIS),
            prices=YahooPrices(session, sleep=lambda _: None),
            fundamentals=None,
            notifiers=list(notifiers),
            session=session,
            recipients=recipients,
            watchlist=watchlist,
            currencies=currencies,
        )

    yield factory
    for store in stores:
        store.close()


def story(n: int, minutes: int = 0) -> tuple:
    return (
        f"AMD shares slide as analysts react, update {n}",
        f"https://example.com/{n}",
        CYCLE + timedelta(minutes=minutes),
    )


# --- per-recipient alerts ------------------------------------------------------------------------------------------


def test_two_recipients_with_different_thresholds_get_different_alerts(make):
    low, high, mixed_only = FakeNotifier(), FakeNotifier(), FakeNotifier()
    people = [
        person(1, [low], alerts=AlertConfig(min_score=60)),
        person(2, [high], alerts=AlertConfig(min_score=70)),
        person(3, [mixed_only], alerts=AlertConfig(min_score=0, verdicts=("mixed",))),
    ]
    scanner = make(recipients=people)

    result = scanner.run_cycle(CYCLE)

    [opp] = result.opportunities
    assert opp.score == AMD_SCORE == 67.8
    assert result.alerts == [opp] and (result.recipients, result.sent) == (3, 1)
    [(subject, markdown, _)] = low.sent
    assert subject == "Dip alert: AMD (score 68)" and markdown.startswith("# Dip alerts")
    assert high.sent == [] and mixed_only.sent == []
    assert [(row["recipient"], row["kind"], row["sent"]) for row in scanner.store.deliveries()] == [
        ("user:1", "alert", True)
    ]
    # Not an alert for the others: nothing is recorded, and nothing is handled for everybody either.
    assert scanner.store.unnotified(recipient="user:2") == [opp]
    assert scanner.is_alert(opp, people[0]) and not scanner.is_alert(opp, people[1])
    assert scanner.is_alert(opp)  # the command line's rules: scanner.toml's [alerts]


def test_the_repeat_window_and_material_changes_are_judged_per_recipient(make):
    strict, loose, eager = FakeNotifier(), FakeNotifier(), FakeNotifier()
    people = [
        person(1, [strict], alerts=AlertConfig(repeat_hours=24, min_score_change=20)),
        person(2, [loose], alerts=AlertConfig(repeat_hours=24, min_score_change=10)),
        person(3, [eager], alerts=AlertConfig(repeat_hours=0)),
    ]
    replies = {"probability": 75}
    session = FakeSession(routes())
    scanner = make(
        recipients=people,
        session=session,
        analysis_model=FakeChatModel(lambda *_: {**ANALYSIS, "probability_up_6m": replies["probability"]}),
    )
    items = [story(1)]
    session.routes[MARKETWATCH_URL] = rss(*items)
    scanner.run_cycle(CYCLE)
    assert [len(n.sent) for n in (strict, loose, eager)] == [1, 1, 1]

    # The same story again: a repeat for the first two, not for the one without a repeat window.
    items.append(story(2, 5))
    session.routes[MARKETWATCH_URL] = rss(*items)
    second = scanner.run_cycle(CYCLE + timedelta(minutes=5))
    assert [len(n.sent) for n in (strict, loose, eager)] == [1, 1, 2]
    notes = "\n".join(second.notes)
    for n in (1, 2):
        assert f"not sent to user{n}@example.com again: AMD (score 67.8, alerted at 67.8)" in notes
    assert "user3@example.com" not in notes

    # The score rises by 10.5: material for the one who asked for 10 points, not for the one who asked for 20.
    replies["probability"] = 90
    items.append(story(3, 10))
    session.routes[MARKETWATCH_URL] = rss(*items)
    scanner.run_cycle(CYCLE + timedelta(minutes=10))
    assert [len(n.sent) for n in (strict, loose, eager)] == [1, 2, 3]
    assert loose.sent[1][0] == "Dip alert: AMD (score 78)"


def test_thesis_changes_go_only_to_recipients_who_were_alerted_and_want_them(make):
    alerted, never, muted = FakeNotifier(), FakeNotifier(), FakeNotifier()
    people = [
        person(1, [alerted], alerts=AlertConfig(min_score=60)),
        person(2, [never], alerts=AlertConfig(min_score=70)),
        person(3, [muted], alerts=AlertConfig(min_score=60), thesis_changes=False),
    ]
    verdict = {"now": ANALYSIS}
    session = FakeSession(routes())
    scanner = make(recipients=people, session=session, analysis_model=FakeChatModel(lambda *_: verdict["now"]))
    scanner.run_cycle(CYCLE)
    assert [len(n.sent) for n in (alerted, never, muted)] == [1, 0, 1]

    later = CYCLE + timedelta(hours=44)
    session.routes[MARKETWATCH_URL] = rss(("AMD shares slide as the SEC opens a probe", "https://e.com/p", later))
    verdict["now"] = {**ANALYSIS, "verdict": "fundamental", "probability_up_6m": 25}
    result = scanner.run_cycle(later)

    assert len(result.thesis_changes) == 1 and result.alerts == []
    assert [len(n.sent) for n in (alerted, never, muted)] == [2, 0, 1]
    subject, markdown, _ = alerted.sent[1]
    assert subject == (
        "Thesis change: AMD now Fundamental damage (was Temporary fear, entry $132.00) - review open orders"
    )
    assert "If you placed orders on the earlier idea, review them." in markdown
    kinds = {(row["recipient"], row["kind"]) for row in scanner.store.deliveries() if row["sent"]}
    assert kinds == {("user:1", "alert"), ("user:1", "thesis"), ("user:3", "alert")}


def thesis_run(make, second: dict, *, raise_to: float | None = 70):
    """AMD alerted to user:1 (min_score 65) in one cycle; then min_score raised to raise_to and AMD analysed again as
    second 44 hours later. Returns (the notifier, the second cycle's result)."""
    inbox = FakeNotifier()
    rules = {"alerts": AlertConfig()}
    verdict = {"now": ANALYSIS}
    session = FakeSession(routes())
    scanner = make(
        recipients=lambda now: [person(1, [inbox], alerts=rules["alerts"])],
        session=session,
        analysis_model=FakeChatModel(lambda *_: verdict["now"]),
    )
    scanner.run_cycle(CYCLE)
    assert [subject for subject, _, _ in inbox.sent] == ["Dip alert: AMD (score 68)"]
    if raise_to is not None:
        rules["alerts"] = AlertConfig(min_score=raise_to)
    later = CYCLE + timedelta(hours=44)
    session.routes[MARKETWATCH_URL] = rss(("AMD shares slide as the SEC opens a probe", "https://e.com/p", later))
    verdict["now"] = second
    return inbox, scanner.run_cycle(later)


def test_raising_the_minimum_score_does_not_cancel_a_thesis_change(make):
    """They may have orders on the alert they got; stricter rules since don't make "fundamental damage" news."""
    inbox, result = thesis_run(make, {**ANALYSIS, "verdict": "fundamental", "probability_up_6m": 30})
    assert len(result.thesis_changes) == 1
    assert inbox.sent[-1][0] == (
        "Thesis change: AMD now Fundamental damage (was Temporary fear, entry $132.00) - review open orders"
    )


def test_stricter_rules_alone_are_no_thesis_change(make):
    """The same idea analysed again fails the new rules only because they are stricter: nothing to review."""
    inbox, result = thesis_run(make, ANALYSIS)
    assert result.thesis_changes == [] and len(inbox.sent) == 1


def test_a_channel_failing_for_one_recipient_does_not_mark_another_as_sent(make):
    broken = FakeNotifier(NotifyError("SMTP server smtp.example.com:587 refused the connection."))
    working = FakeNotifier()
    scanner = make(recipients=[person(1, [broken]), person(2, [working])])

    first = scanner.run_cycle(CYCLE)

    [opp] = first.opportunities
    assert len(broken.sent) == 1 and len(working.sent) == 1 and first.sent == 1
    assert (
        "Couldn't send the alerts to user1@example.com by fake: SMTP server smtp.example.com:587 refused the "
        "connection." in first.notes
    )
    [failed] = scanner.store.deliveries(recipient="user:1")
    assert (failed["sent"], failed["detail"]) == (
        False,
        "fake: SMTP server smtp.example.com:587 refused the connection.",
    )
    assert scanner.store.unnotified(recipient="user:1") == [opp]
    assert scanner.store.unnotified(recipient="user:2") == []

    broken.error = None  # the server is back: the alert goes out, to the one who didn't get it only
    scanner.run_cycle(CYCLE + timedelta(minutes=5))
    assert len(broken.sent) == 2 and len(working.sent) == 1
    assert broken.sent[1][0] == "Dip alert: AMD (score 68)"
    assert scanner.store.deliveries(recipient="user:1")[0]["sent"] is True


def test_a_members_webhook_that_never_answers_does_not_hold_the_cycle(make):
    """A tarpit (a public https server that accepts the POST and trickles its answer) costs the cycle the deadline,
    not hours: the next member still gets the alert and the stuck delivery is recorded as not sent."""
    release = threading.Event()

    class Tarpit:
        def post(self, url, **kwargs):
            release.wait(10)
            return FakeResponse(url, 200)

    tarpit = WebhookNotifier("https://tarpit.example.com/hook", "slack", session=Tarpit(), deadline=0.2)
    working = FakeNotifier()
    scanner = make(recipients=[person(1, [tarpit]), person(2, [working])])
    try:
        started = time.monotonic()
        result = scanner.run_cycle(CYCLE)
        assert time.monotonic() - started < 2
    finally:
        release.set()
    assert [subject for subject, _, _ in working.sent] == ["Dip alert: AMD (score 68)"]
    [stuck] = scanner.store.deliveries(recipient="user:1")
    assert stuck["sent"] is False and "didn't answer within 0.2 s" in stuck["detail"]
    assert (
        "Couldn't send the alerts to user1@example.com by slack webhook: The slack webhook at tarpit.example.com "
        "didn't answer within 0.2 s." in result.notes
    )


def test_a_recipient_gets_ideas_only_from_when_their_alerts_started(make):
    first, late, newcomer = FakeNotifier(), FakeNotifier(), FakeNotifier()
    everyone = {"now": [person(1, [first])]}
    scanner = make(recipients=lambda now: everyone["now"])
    scanner.run_cycle(CYCLE)
    assert len(first.sent) == 1

    everyone["now"] = [person(1, [first]), person(2, [late], since=CYCLE + timedelta(minutes=1))]
    scanner.run_cycle(CYCLE + timedelta(minutes=5))
    assert late.sent == [] and len(first.sent) == 1

    # Without a start, a new recipient gets the unsent ideas of the retry window, labelled as late.
    everyone["now"] = [person(1, [first]), person(3, [newcomer])]
    scanner.run_cycle(CYCLE + timedelta(minutes=10))
    assert len(newcomer.sent) == 1 and len(first.sent) == 1


def test_only_watchlist_limits_a_recipients_alerts_to_their_tickers(make):
    fans, others = FakeNotifier(), FakeNotifier()
    people = [
        person(1, [fans], watchlist=("AMD",), only_watchlist=True),
        person(2, [others], watchlist=("NVDA",), only_watchlist=True),
    ]
    scanner = make(recipients=people)
    [opp] = scanner.run_cycle(CYCLE).opportunities
    assert len(fans.sent) == 1 and others.sent == []
    assert scanner.is_alert(opp, people[0]) and not scanner.is_alert(opp, people[1])


def test_messages_use_each_recipients_currency_and_time_zone(make):
    athens, york, plain = FakeNotifier(), FakeNotifier(), FakeNotifier()
    people = [
        person(1, [athens], currency="EUR", tz=ZoneInfo("Europe/Athens")),
        person(2, [york], currency="GBP", tz=ZoneInfo("America/New_York")),
        person(3, [plain]),
    ]
    session = FakeSession(
        {
            **routes(),
            EURUSD: chart_json("EURUSD=X", [1.13, 1.14], currency="USD"),
            GBPUSD: chart_json("GBPUSD=X", [1.25, 1.26], currency="USD"),
        }
    )
    scanner = make(recipients=people, session=session)

    result = scanner.run_cycle(CYCLE)

    [opp] = result.opportunities
    assert opp.fx_rates == {"EUR": pytest.approx(1 / 1.14), "GBP": pytest.approx(1 / 1.26)}
    assert (opp.account_currency, opp.fx_rate) == (None, None)  # no [account] currency
    assert scanner.store.opportunities()[0].fx_rates == opp.fx_rates
    athens_text = athens.sent[0][1]
    assert "| Price | $143.55 ≈ €125.92 (-10.0% 1 day" in athens_text
    assert "| Reported | 2026-09-25 23:30 EEST |" in athens_text and "1 USD = 0.8772 EUR" in athens_text
    york_text = york.sent[0][1]
    assert (
        "| Price | $143.55 ≈ £113.93 (-10.0% 1 day" in york_text and "| Reported | 2026-09-25 16:30 EDT |" in york_text
    )
    plain_text = plain.sent[0][1]
    assert "≈" not in plain_text and "| Reported | 2026-09-25 20:30 UTC |" in plain_text
    assert "≈" not in result.report_paths[3].read_text(encoding="utf-8")  # the report: trading currency only
    assert format_when(CYCLE) == "2026-09-25 20:30 UTC"  # the process-wide zone is untouched


def test_system_notices_go_to_admins_and_the_default_recipient_only(make):
    config = ScannerConfig(scan=ScanConfig(context_news=False), alerts=AlertConfig(notice_after_cycles=1))
    own, admin, member = FakeNotifier(), FakeNotifier(), FakeNotifier()
    people = [
        person(0, [own], key=DEFAULT_RECIPIENT, label="default"),
        person(1, [admin], admin=True),
        person(2, [member]),
    ]
    scanner = make(recipients=people, config=config, session=FakeSession({MARKETWATCH_URL: 500}))
    scanner.run_cycle(CYCLE)
    assert [subject for subject, _, _ in own.sent] == ["dip-scanner: every feed has failed for 1 cycles"]
    assert [subject for subject, _, _ in admin.sent] == ["dip-scanner: every feed has failed for 1 cycles"]
    assert member.sent == []


def test_recipients_that_can_not_be_loaded_leave_the_alerts_for_the_next_cycle(make):
    notifier = FakeNotifier()
    state = {"fail": True}

    def recipients(now):
        if state["fail"]:
            raise sqlite3.OperationalError("database is locked")
        return [person(1, [notifier])]

    scanner = make(recipients=recipients)
    first = scanner.run_cycle(CYCLE)
    [opp] = first.opportunities
    assert "Couldn't load the alert recipients, so alerts wait for the next cycle: database is locked" in first.notes
    assert first.alerts == [opp] and notifier.sent == []
    assert scanner.store.unnotified(recipient="user:1") == [opp]  # not handled for everybody

    state["fail"] = False
    scanner.run_cycle(CYCLE + timedelta(minutes=5))
    assert [subject for subject, _, _ in notifier.sent] == ["Dip alert: AMD (score 68)"]


def test_without_any_recipient_with_a_channel_the_ideas_are_handled_for_everybody(make):
    scanner = make(recipients=[person(1, [])])
    [opp] = scanner.run_cycle(CYCLE).opportunities
    assert scanner.store.unnotified(recipient="user:1") == [] and scanner.store.unnotified() == []
    assert opp.id is not None


def test_the_command_line_keeps_its_alert_state_under_default(make):
    notifier = FakeNotifier()
    scanner = make(notifiers=[notifier])  # no recipients callable: the default recipient
    [opp] = scanner.run_cycle(CYCLE).opportunities
    assert len(notifier.sent) == 1
    assert [(row["recipient"], row["kind"], row["sent"]) for row in scanner.store.deliveries()] == [
        (DEFAULT_RECIPIENT, "alert", True)
    ]
    assert scanner.store.last_alerted("AMD") == opp
    recipient = scanner.default_recipient()
    assert recipient.notifiers == [notifier] and recipient.alerts == QUIET.alerts


# --- what the website adds to a cycle ------------------------------------------------------------------------------


def test_every_users_watchlist_joins_the_candidates(make):
    """A magnitude-1 story is below [dip] min_magnitude, except for a watchlist ticker."""
    session = FakeSession({CHART: routes()[CHART + "AMD"]})
    plain = make(feeds=[], recipients=[], session=session)
    article = make_article(title="AMD mentioned in passing", published=CYCLE - timedelta(minutes=30))
    plain.store.add_articles([article], max_age_hours=24, now=CYCLE)
    plain.store.record_triage([article.id], [make_impact(article_id=article.id, magnitude=1)])
    assert plain.run_cycle(CYCLE).candidates == 0

    watched = make(feeds=[], recipients=[], session=session, watchlist=lambda now: ["$amd", "not a symbol!"])
    result = watched.run_cycle(CYCLE + timedelta(minutes=5))
    assert result.candidates == 1 and [opp.ticker for opp in result.opportunities] == ["AMD"]
    assert watched.config.universe.watchlist == ()  # the config itself is left as it is


def test_rates_are_stored_for_every_users_currency_also_in_manual_analyses(make):
    session = FakeSession({**routes(), EURUSD: chart_json("EURUSD=X", [1.14], currency="USD")})
    scanner = make(recipients=[], currencies=lambda now: ["eur", "CHF"], session=session)
    result = scanner.run_cycle(CYCLE)
    [opp] = result.opportunities
    assert opp.fx_rates == {"EUR": pytest.approx(1 / 1.14)} and opp.fx_rate is None
    assert any(note.startswith("No USD/CHF exchange rate for AMD, amounts in USD only") for note in result.notes)
    manual = scanner.analyze_ticker("AMD", now=CYCLE)
    assert manual.fx_rates == {"EUR": pytest.approx(1 / 1.14)}


def test_hooks_that_fail_never_stop_the_cycle(make, caplog):
    def broken(now):
        raise RuntimeError("settings table is gone")

    scanner = make(recipients=[], watchlist=broken, currencies=broken)
    with caplog.at_level(logging.WARNING, logger="dip_scanner"):
        result = scanner.run_cycle(CYCLE)
    assert len(result.opportunities) == 1
    assert "Couldn't load the users' watchlists, scanning with [universe] watchlist only: settings table is gone" in (
        result.notes
    )
    assert "Couldn't load the users' currencies" in caplog.text


# --- cycle records -------------------------------------------------------------------------------------------------


def test_every_cycle_is_recorded_for_the_website(make):
    scanner = make(recipients=[person(1, [FakeNotifier()])])
    result = scanner.run_cycle(CYCLE)
    [record] = scanner.store.cycles()
    assert record.ok and record.summary == result.summary() and record.notes == result.notes
    assert (record.started, record.finished) == (CYCLE, result.finished)
    assert record.stats == cycle_stats(result)
    assert {key: record.stats[key] for key in ("feeds_ok", "opportunities", "alerts", "recipients", "sent")} == {
        "feeds_ok": 1,
        "opportunities": 1,
        "alerts": 1,
        "recipients": 1,
        "sent": 1,
    }
    assert scanner.last_result is result and scanner.cycle_started is None


def test_a_failed_cycle_is_recorded_without_secrets(make, tmp_path):
    key = "sk-proj-verysecretkey123"
    settings = Settings(data_dir=tmp_path / "data", llm=LLMSettings(openai_api_key=key))
    scanner = make(settings=settings, triage_model=FakeChatModel(LLMSetupError(f"Incorrect API key provided: {key}")))
    with pytest.raises(LLMSetupError):
        scanner.run_cycle(CYCLE)
    [record] = scanner.store.cycles()
    assert not record.ok
    assert record.summary == "Cycle 2026-09-25 20:30 UTC failed: LLMSetupError: Incorrect API key provided: ***"
    assert record.finished is not None and scanner.cycle_started is None


def test_a_cycle_that_can_not_be_recorded_still_counts(make, monkeypatch, caplog):
    scanner = make(recipients=[])

    def locked(**kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(scanner.store, "record_cycle", locked)
    with caplog.at_level(logging.WARNING, logger="dip_scanner"):
        assert len(scanner.run_cycle(CYCLE).opportunities) == 1
    assert "Couldn't record the cycle: database is locked" in caplog.text


# --- the watch loop's controls -------------------------------------------------------------------------------------


def _counting(scanner: Scanner) -> list:
    ran: list = []

    def cycle(now=None):
        ran.append(now)
        return CycleResult(started=now, finished=now)

    scanner.run_cycle = cycle
    return ran


def test_a_paused_scanner_skips_its_cycles_until_resumed_and_says_so_once(make, caplog):
    scanner = make()
    ran = _counting(scanner)
    scanner.store.set_scanner_paused(True)
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 3:
            scanner.store.set_scanner_paused(False)

    with caplog.at_level(logging.INFO, logger="dip_scanner"):
        scanner.watch(interval_minutes=5, max_cycles=5, sleep=sleep, clock=lambda: CYCLE)
    assert len(ran) == 2 and len(sleeps) == 4  # three intervals skipped, then two cycles
    assert caplog.text.count("The scanner is paused") == 1
    assert "The scanner was resumed." in caplog.text and "Stopped after 2 cycles." in caplog.text
    assert not scanner.paused()


def test_a_requested_cycle_runs_even_while_paused(make):
    scanner = make()
    ran = _counting(scanner)
    scanner.store.set_scanner_paused(True)
    scanner.request_cycle_now()
    scanner.watch(max_cycles=3, sleep=lambda seconds: None, clock=lambda: CYCLE)
    assert len(ran) == 1  # the one asked for; the other intervals stay paused


def test_the_next_cycle_time_is_known_while_waiting(make):
    scanner = make()
    _counting(scanner)
    seen = []
    scanner.watch(
        interval_minutes=5,
        max_cycles=2,
        sleep=lambda seconds: seen.append((seconds, scanner.next_cycle_at, scanner.watching)),
        clock=lambda: CYCLE + timedelta(minutes=2),
    )
    assert seen == [(180.0, CYCLE + timedelta(minutes=5), True)]
    assert scanner.next_cycle_at is None and not scanner.watching


def test_request_cycle_now_wakes_the_waiting_loop_and_stop_ends_it(make):
    scanner = make()
    started: list = []
    events = [threading.Event(), threading.Event()]

    def cycle(now=None):
        started.append(now)
        events[min(len(started), 2) - 1].set()
        return CycleResult(started=now, finished=now)

    scanner.run_cycle = cycle
    thread = threading.Thread(target=scanner.watch, kwargs={"interval_minutes": 1440, "clock": lambda: CYCLE})
    thread.start()
    try:
        assert events[0].wait(5)  # the first cycle starts at once
        scanner.request_cycle_now()  # the next one would be a day away
        assert events[1].wait(5)
    finally:
        scanner.stop()
        thread.join(5)
    assert not thread.is_alive() and len(started) == 2
    assert not scanner.watching and scanner.next_cycle_at is None


def test_stop_before_watch_starts_means_no_cycle_at_all(make):
    scanner = make()
    ran = _counting(scanner)
    scanner.stop()
    scanner.watch(sleep=lambda seconds: pytest.fail("no wait expected"), clock=lambda: CYCLE)
    assert ran == []


def test_an_unreadable_pause_flag_counts_as_running(make, monkeypatch):
    scanner = make()

    def broken():
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(scanner.store, "scanner_paused", broken)
    assert scanner.paused() is False


# --- a member's "Analyse now" and everybody else's alerts ---------------------------------------------------------

PASSWORD = "a long enough password"


def members(scanner: Scanner):
    """Two website users on the scanner's database (ids 1 and 2) with a notifier each, as recipients."""
    accounts = Accounts(scanner.store, clock=lambda: CYCLE)
    alice = accounts.create_user("user1@example.com", password=PASSWORD)
    bob = accounts.create_user("user2@example.com", password=PASSWORD)
    inboxes = FakeNotifier(), FakeNotifier()
    return accounts, (alice, bob), inboxes, [person(1, [inboxes[0]]), person(2, [inboxes[1]])]


def analyse_now(scanner: Scanner, accounts: Accounts, user, *, at):
    """What the website's "Analyse now" does: a job running the scanner's analyze_ticker (inline here)."""
    jobs = JobRunner(
        accounts,
        analyse=scanner.analyze_ticker,
        settings=Settings(data_dir=scanner.settings.data_dir),
        clock=lambda: at,
        executor=InlineExecutor(),
    )
    job = accounts.get_job(jobs.submit(user, "AMD").id)
    assert job.status == "done"
    return scanner.store.get_opportunity(job.opportunity_id)


def test_a_members_analyse_now_does_not_hold_the_dip_back_from_everybody(make):
    """The story is in (/news shows it) when member 2 clicks "Analyse now". The cycle must still analyse the dip and
    alert member 1: a manual analysis isn't the scanner's cooldown."""
    recipients: list = []
    scanner = make(recipients=recipients, feeds=[])
    accounts, (_, bob), (alice_inbox, bob_inbox), people = members(scanner)
    recipients += people
    seed(scanner, "AMD", magnitude=4, at=CYCLE - timedelta(minutes=10))
    manual = analyse_now(scanner, accounts, bob, at=CYCLE - timedelta(minutes=10))
    assert scanner.store.is_manual(manual.id)

    result = scanner.run_cycle(CYCLE)

    assert [opp.ticker for opp in result.opportunities] == ["AMD"]
    assert not any("cooldown" in note for note in result.notes)
    assert [subject for subject, _, _ in alice_inbox.sent] == ["Dip alert: AMD (score 68)"]
    assert bob_inbox.sent == []  # he read the same idea a moment ago: a repeat
    assert any(note.startswith("Alerted within the last") and "user2@example.com" in note for note in result.notes)


def test_a_manual_analysis_before_the_news_does_not_make_the_news_wait_for_the_next_session(make):
    config = ScannerConfig(scan=ScanConfig(context_news=False))  # the same-session wait on (12 hours)
    recipients: list = []
    scanner = make(recipients=recipients, feeds=[], config=config)
    accounts, (_, bob), (alice_inbox, _), people = members(scanner)
    recipients += people
    analyse_now(scanner, accounts, bob, at=CYCLE - timedelta(hours=5))
    seed(scanner, "AMD", magnitude=4, at=CYCLE - timedelta(minutes=30))  # the news comes after the click

    result = scanner.run_cycle(CYCLE)

    assert [opp.ticker for opp in result.opportunities] == ["AMD"]
    assert not any(note.startswith("Already analysed on the latest session's prices") for note in result.notes)
    assert [subject for subject, _, _ in alice_inbox.sent] == ["Dip alert: AMD (score 68)"]


def test_a_manual_analysis_stored_during_the_cycle_is_sent_in_the_pending_alerts_place(make):
    """A member's analysis finishes while the cycle is still busy with later candidates: the cycle's alert is
    superseded, and the others get the newer analysis instead of nothing."""
    recipients: list = []
    scanner = make(recipients=recipients)
    accounts, (_, bob), (alice_inbox, bob_inbox), people = members(scanner)
    recipients += people
    store = scanner.store
    real_add = store.add_opportunity
    manual: list = []

    def add_then_click(opp, **kwargs):
        stored = real_add(opp, **kwargs)
        if not manual and not kwargs.get("manual"):  # the website's job, in its own thread, a minute later
            manual.append(analyse_now(scanner, accounts, bob, at=CYCLE + timedelta(minutes=1)))
        return stored

    store.add_opportunity = add_then_click
    result = scanner.run_cycle(CYCLE)
    store.add_opportunity = real_add

    [cycle_opp] = result.opportunities
    [(subject, markdown, _)] = alice_inbox.sent
    assert subject == "Dip alert: AMD (score 68)" and "analysed 1" not in markdown
    assert bob_inbox.sent == []  # the viewer read it on the website
    rows = {(row["recipient"], row["opportunity_id"], row["kind"], row["sent"]) for row in store.deliveries()}
    assert ("user:1", manual[0].id, "alert", True) in rows
    assert ("user:2", cycle_opp.id, "handled", False) in rows
    scanner.run_cycle(CYCLE + timedelta(minutes=5))  # nothing again, and nothing left waiting
    assert len(alice_inbox.sent) == 1 and store.unnotified(recipient="user:1") == []


def test_an_alert_retried_after_a_manual_analysis_is_sent_once(make):
    recipients: list = []
    scanner = make(recipients=recipients)
    accounts, (_, bob), (alice_inbox, _), people = members(scanner)
    recipients += people
    alice_inbox.error = NotifyError("Slack answered 503")
    scanner.run_cycle(CYCLE)
    analyse_now(scanner, accounts, bob, at=CYCLE + timedelta(minutes=2))  # "Analyse again" while Alice waits
    scanner.run_cycle(CYCLE + timedelta(minutes=5))  # still down: tried again
    alice_inbox.error = None
    scanner.run_cycle(CYCLE + timedelta(minutes=10))
    scanner.run_cycle(CYCLE + timedelta(minutes=15))
    assert [subject for subject, _, _ in alice_inbox.sent] == ["Dip alert: AMD (score 68)"] * 3  # 2 failed, 1 sent
    assert scanner.store.unnotified(recipient="user:1") == []


def test_a_bearish_manual_analysis_stops_the_stale_alert(make):
    replies = {"now": ANALYSIS}
    recipients: list = []
    scanner = make(recipients=recipients, analysis_model=FakeChatModel(lambda *_: replies["now"]))
    accounts, (_, bob), (alice_inbox, _), people = members(scanner)
    recipients += people
    alice_inbox.error = NotifyError("Slack answered 503")
    scanner.run_cycle(CYCLE)
    replies["now"] = {**ANALYSIS, "verdict": "fundamental", "probability_up_6m": 25}
    analyse_now(scanner, accounts, bob, at=CYCLE + timedelta(minutes=2))
    alice_inbox.error = None
    scanner.run_cycle(CYCLE + timedelta(minutes=5))
    assert len(alice_inbox.sent) == 1  # only the failed attempt of cycle 1: the bullish alert is stale now
    assert scanner.store.unnotified(recipient="user:1") == []


def test_a_manual_analysis_that_fails_the_rules_is_no_idea_to_review(make):
    """Member 1 read a "fundamental damage" analysis of their own; the scanner's own, as gloomy, is no thesis change
    for them: they never had an idea to place orders on."""
    gloomy = {**ANALYSIS, "verdict": "fundamental", "probability_up_6m": 30}
    recipients: list = []
    scanner = make(recipients=recipients, feeds=[], analysis_model=FakeChatModel(gloomy))
    accounts, (alice, _), (alice_inbox, bob_inbox), people = members(scanner)
    recipients += people
    seed(scanner, "AMD", magnitude=4, at=CYCLE - timedelta(minutes=10))
    analyse_now(scanner, accounts, alice, at=CYCLE - timedelta(minutes=10))

    result = scanner.run_cycle(CYCLE)

    assert [opp.ticker for opp in result.opportunities] == ["AMD"] and result.thesis_changes == []
    assert alice_inbox.sent == [] and bob_inbox.sent == []
