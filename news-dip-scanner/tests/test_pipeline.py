"""Whole scan cycles with fake feeds, prices, SEC data, models and notifiers (no network, no sleeping)."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import requests
from conftest import (
    FakeChatModel,
    FakeResponse,
    FakeSession,
    make_analysis,
    make_article,
    make_impact,
    make_opportunity,
)

from dip_scanner.config import AlertConfig, DipConfig, ScanConfig, ScannerConfig, Settings
from dip_scanner.fundamentals import SecFundamentals
from dip_scanner.llm import LLMError, LLMSetupError, LLMUnavailableError
from dip_scanner.models import Feed
from dip_scanner.notify import NotifyError, WebhookNotifier
from dip_scanner.pipeline import CycleResult, Scanner, alert_subject, seconds_until_next, usage_lines
from dip_scanner.prices import PriceError, YahooPrices
from dip_scanner.report import render_markdown
from dip_scanner.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
# The fixture chart's last session closed at 20:00 UTC on 2026-09-25 (AMD -10% on the day); its articles are from
# that day, so a cycle half an hour after the close sees all of them as fresh.
CYCLE = datetime(2026, 9, 25, 20, 30, tzinfo=UTC)

MARKETWATCH_URL = "https://feeds.example.com/marketwatch/topstories"
SEC_URL = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&output=atom"
FEEDS = [
    Feed("marketwatch", "MarketWatch", MARKETWATCH_URL),
    Feed("sec-8k", "SEC 8-K filings", SEC_URL, category="filings"),
    Feed("off", "Switched off", "https://off.example.com/rss", enabled=False),
]
SEC_AGENT = "Test Runner test@example.com"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture_json(name: str) -> dict:
    return json.loads(fixture(name))


def routes() -> dict:
    return {
        MARKETWATCH_URL: fixture("rss_marketwatch.xml"),
        "https://www.sec.gov/cgi-bin/": fixture("atom_sec.xml"),
        "https://www.sec.gov/files/company_tickers.json": fixture_json("sec_company_tickers.json"),
        "https://data.sec.gov/api/xbrl/companyfacts/CIK0000002488.json": fixture_json("sec_companyfacts_small.json"),
        "https://query1.finance.yahoo.com/v8/finance/chart/AMD": fixture_json("yahoo_chart_amd.json"),
        "https://feeds.finance.yahoo.com/rss/2.0/headline": fixture("rss_yahoo_amd.xml"),
        "https://news.google.com/rss/search": fixture("rss_google_amd.xml"),
    }  # anything else (e.g. Yahoo charts for BA, NVDA) is a 404


def rss(*items: tuple[str, str, datetime]) -> bytes:
    """A small RSS feed of (title, link, published) items."""
    body = "".join(
        f"<item><title>{title}</title><link>{link}</link>"
        f"<pubDate>{published:%a, %d %b %Y %H:%M:%S} GMT</pubDate><description>{title}, and more.</description></item>"
        for title, link, published in items
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>{body}</channel></rss>'.encode()


def company(ticker, name, relation, direction, magnitude, event_type="other"):
    return {
        "ticker": ticker,
        "company": name,
        "relation": relation,
        "direction": direction,
        "magnitude": magnitude,
        "event_type": event_type,
        "rationale": f"{name} is affected.",
    }


TRIAGE_RULES = {  # a phrase in the title -> the companies the fake triage model names
    "AMD shares slide": [company("AMD", "Advanced Micro Devices", "direct", "negative", 4, "guidance")],
    "Boeing": [company("BA", "Boeing", "direct", "negative", 3, "supply_chain")],
    "TSMC": [
        company("TSM", "Taiwan Semiconductor", "direct", "positive", 3, "product"),
        company("NVDA", "Nvidia", "indirect", "negative", 2, "supply_chain"),
    ],
    "ADVANCED MICRO DEVICES": [company("AMD", "Advanced Micro Devices", "direct", "mixed", 2, "earnings")],
}


def triage_reply(system: str, prompt: str, json_mode: bool) -> dict:
    """Answers a triage prompt like a model would: one entry per <article>, companies picked by TRIAGE_RULES."""
    entries = []
    for short_id, body in re.findall(r'<article id="(a\d+)"[^>]*>(.*?)</article>', prompt, re.DOTALL):
        title = body.split("\n", 1)[0]
        found = [entry for phrase, entries in TRIAGE_RULES.items() if phrase in title for entry in entries]
        entries.append({"id": short_id, "companies": found})
    return {"articles": entries}


ANALYSIS = {
    "verdict": "temporary_fear",
    "probability_up_6m": 75,
    "potential_low": 118.0,
    "entry_price": 132.0,
    "target_price": 170.0,
    "confidence": "high",
    "fear": "Investors fear AI data-center spending is slowing.",
    "fundamental_impact": "One quarter of softer guidance; the roadmap and balance sheet are intact.",
    "thesis": "The drop prices in a lasting slowdown the guidance doesn't support.",
    "risks": ["Hyperscalers cut capex further"],
    "catalysts": ["Next quarter's earnings"],
    "checks": ["Read the earnings call transcript"],
}
# 100 * (0.7 * 0.75 + 0.3 * reward/risk 0.5086) * 1.0 (temporary fear) * 1.0 (high confidence), at a price of 143.55
AMD_SCORE = 67.8


class FakeNotifier:
    name = "fake"

    def __init__(self, error: Exception | None = None):
        self.error = error
        self.sent: list[tuple[str, str, str]] = []

    def send(self, subject: str, markdown: str, html: str) -> None:
        self.sent.append((subject, markdown, html))
        if self.error is not None:
            raise self.error


@pytest.fixture
def build(tmp_path):
    """Builds Scanners on a fresh data folder; keyword arguments replace the fakes. Stores are closed afterwards."""
    stores: list[Store] = []

    def factory(
        *,
        session: FakeSession | None = None,
        triage_model=None,
        analysis_model=None,
        notifiers=None,
        config: ScannerConfig | None = None,
        sec_user_agent: str | None = SEC_AGENT,
        feeds: list[Feed] | None = None,
        notify: bool = True,
    ) -> Scanner:
        session = session if session is not None else FakeSession(routes())
        settings = Settings(data_dir=tmp_path / "data", sec_user_agent=sec_user_agent)
        store = Store(settings.data_dir / "scanner.sqlite3")
        stores.append(store)
        fundamentals = None
        if sec_user_agent:
            fundamentals = SecFundamentals(
                sec_user_agent, session=session, cache_dir=settings.data_dir / "cache", sleep=lambda _: None
            )
        return Scanner(
            settings=settings,
            config=config or ScannerConfig(),
            feeds=FEEDS if feeds is None else feeds,
            store=store,
            triage_model=triage_model or FakeChatModel(triage_reply, name="fake-triage"),
            analysis_model=analysis_model or FakeChatModel(ANALYSIS, name="fake-analysis"),
            prices=YahooPrices(session, sleep=lambda _: None),
            fundamentals=fundamentals,
            notifiers=[FakeNotifier()] if notifiers is None else notifiers,
            session=session,
            notify=notify,
        )

    yield factory
    for store in stores:
        store.close()


# --- a full cycle --------------------------------------------------------------------------------------------------


def test_a_full_cycle_finds_scores_stores_reports_and_alerts(build):
    session = FakeSession(routes())
    triage_model = FakeChatModel(triage_reply)
    analysis_model = FakeChatModel(ANALYSIS, name="fake-analysis")
    notifier = FakeNotifier()
    scanner = build(session=session, triage_model=triage_model, analysis_model=analysis_model, notifiers=[notifier])

    result = scanner.run_cycle(CYCLE)

    # Poll: two enabled feeds (the disabled one is never requested); 4 MarketWatch + 2 SEC articles, all triaged
    # in one batch. SEC got the contact User-Agent.
    assert (result.feeds_ok, result.feeds_failed) == (2, 0)
    assert "https://off.example.com/rss" not in session.urls
    assert result.new_articles == result.triaged == 6
    assert len(triage_model.calls) == 1
    sec_call = next(call for call in session.calls if call["url"] == SEC_URL)
    assert sec_call["headers"]["User-Agent"] == SEC_AGENT

    # Triage found AMD (twice), BA, TSM and NVDA; only AMD has prices and a dip.
    assert result.impacts == 5
    assert result.candidates == 1
    notes = "\n".join(result.notes)
    assert "BA" in notes and "NVDA" in notes and "marked invalid" in notes
    assert "TSM (positive news)" in notes
    assert scanner.store.ticker_valid("BA", now=CYCLE) is False
    assert scanner.store.ticker_valid("AMD", now=CYCLE) is True

    # The analysis saw the price picture, the SEC fundamentals, the flagged news and the per-ticker context news.
    (system, prompt, json_mode), *_ = analysis_model.calls
    assert json_mode
    assert "down 10.0% today" in prompt
    assert "ADVANCED MICRO DEVICES INC (SEC CIK 0000002488)" in prompt
    assert "AMD shares slide after weak data-center guidance" in prompt
    assert "Is AMD stock a buy after the guidance cut?" in prompt  # Yahoo per-ticker RSS
    assert "Analysts defend AMD after selloff" in prompt  # Google News

    # The opportunity is scored, stored and reported.
    [opp] = result.opportunities
    assert (opp.ticker, opp.company, opp.score, opp.model) == (
        "AMD",
        "Advanced Micro Devices, Inc.",
        AMD_SCORE,
        "fake-analysis",
    )
    assert opp.id is not None and opp.created == CYCLE and opp.price == 143.55
    assert len(opp.article_ids) == 2 and len(opp.headlines) == 2
    assert opp.dip_reasons == ["down 10.0% today", "down 8.9% over 5 days", "10.6% below its 20-day high"]
    assert scanner.store.opportunities() == [opp]
    assert [path.name for path in result.report_paths] == [
        "203000-opportunities.md",
        "203000-opportunities.html",
        "203000-opportunities.json",
        "latest.md",
        "latest.html",
    ]
    assert all(path.exists() for path in result.report_paths)
    latest = result.report_paths[3].read_text(encoding="utf-8")
    assert "AMD — Advanced Micro Devices, Inc. · score 67.8" in latest
    assert "marked invalid" in latest  # the cycle's notes are in the report

    # It passes [alerts], so it was sent and marked as sent.
    assert result.alerts == [opp]
    [(subject, markdown, html)] = notifier.sent
    assert subject == "Dip alert: AMD (score 68)"
    assert markdown.startswith("# Dip alerts") and "AMD" in markdown
    assert html.lstrip().lower().startswith("<!doctype html")
    assert scanner.store.unnotified() == []
    assert result.finished is not None and result.finished >= CYCLE
    assert result.summary().startswith("Cycle 2026-09-25 20:30 UTC: 2/2 feeds ok, 6 new articles, 6 triaged")


def test_the_next_cycle_respects_the_cooldown_until_new_news_arrives(build):
    session = FakeSession(routes())
    triage_model = FakeChatModel(triage_reply)
    analysis_model = FakeChatModel(ANALYSIS)
    notifier = FakeNotifier()
    config = ScannerConfig(scan=ScanConfig(reanalyse_same_session_hours=0))  # see the weekend test below
    scanner = build(
        session=session, triage_model=triage_model, analysis_model=analysis_model, notifiers=[notifier], config=config
    )
    first = scanner.run_cycle(CYCLE)

    # Five minutes later the feeds list the same articles: nothing new, AMD is in its cooldown.
    second = scanner.run_cycle(CYCLE + timedelta(minutes=5))
    assert (second.new_articles, second.triaged, second.candidates) == (0, 0, 0)
    assert second.opportunities == [] and second.report_paths == [] and second.alerts == []
    assert any("cooldown" in note and "AMD (analysed 0.1h ago)" in note for note in second.notes)
    assert len(triage_model.calls) == 1 and len(analysis_model.calls) == 1
    assert len(notifier.sent) == 1  # the first cycle's alert isn't sent again

    # New negative news about AMD ends the cooldown.
    fresh = ("AMD shares slide further as analysts cut targets", "https://www.marketwatch.com/story/amd-cut", CYCLE)
    session.routes[MARKETWATCH_URL] = rss(fresh)
    third = scanner.run_cycle(CYCLE + timedelta(minutes=10))
    assert third.new_articles == 1 and third.candidates == 1
    [again] = third.opportunities
    assert len(analysis_model.calls) == 2
    assert again.id != first.opportunities[0].id
    assert again.headlines[0]["title"] == fresh[0]  # newest first
    assert [opp.id for opp in scanner.store.opportunities()] == [again.id, first.opportunities[0].id]


def test_the_first_cycle_only_triages_recent_articles(build):
    """A fresh install sees days of backlog in the feeds; only the last [scan] max_article_age_hours are triaged."""
    items = [
        ("AMD shares slide on guidance", "https://example.com/new", CYCLE - timedelta(hours=2)),
        ("Boeing deliveries slow", "https://example.com/day-old", CYCLE - timedelta(hours=30)),
        ("TSMC raises prices", "https://example.com/week-old", CYCLE - timedelta(days=6)),
    ]
    session = FakeSession({**routes(), MARKETWATCH_URL: rss(*items)})
    triage_model = FakeChatModel(triage_reply)
    scanner = build(session=session, triage_model=triage_model, feeds=FEEDS[:1])

    result = scanner.run_cycle(CYCLE)

    assert result.new_articles == result.triaged == 1
    [(_, prompt, _)] = triage_model.calls
    assert "AMD shares slide on guidance" in prompt
    assert "Boeing" not in prompt and "TSMC" not in prompt
    stored = scanner.store.get_articles([make_article(link=link).id for _, link, _ in items])
    assert [scanner.store.article_status(article.id)[0] for article in stored] == ["done", "skipped", "skipped"]


def test_poll_saves_feed_state_counts_failures_and_sends_conditional_requests(build):
    def marketwatch(method, url, call):
        if call["headers"].get("If-None-Match") == '"v1"':
            return 304
        return FakeResponse(content=fixture("rss_marketwatch.xml"), headers={"ETag": '"v1"'})

    session = FakeSession({MARKETWATCH_URL: marketwatch, "https://www.sec.gov/": 500})
    scanner = build(session=session)

    assert scanner.poll(CYCLE) == (1, 1, 4)
    assert scanner.poll(CYCLE + timedelta(minutes=5)) == (1, 1, 0)  # a 304 counts as ok

    health = {row["key"]: row for row in scanner.store.feed_health()}
    assert health["marketwatch"]["last_status"] == 304 and health["marketwatch"]["last_error"] is None
    assert health["marketwatch"]["articles"] == 4
    assert health["sec-8k"]["last_status"] == 500 and "HTTP 500" in health["sec-8k"]["last_error"]
    assert "off" not in health


def test_a_failed_store_keeps_the_old_validators_so_the_next_poll_gets_the_articles(build, monkeypatch):
    """Regression: the new ETag was saved before the articles; when storing them failed, the next poll got a 304
    and those articles were lost."""

    def marketwatch(method, url, call):
        if call["headers"].get("If-None-Match") == '"v1"':
            return 304
        return FakeResponse(content=fixture("rss_marketwatch.xml"), headers={"ETag": '"v1"'})

    scanner = build(session=FakeSession({MARKETWATCH_URL: marketwatch}), feeds=FEEDS[:1])
    real_add = scanner.store.add_articles
    attempts = []

    def locked_once(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_add(*args, **kwargs)

    monkeypatch.setattr(scanner.store, "add_articles", locked_once)
    with pytest.raises(sqlite3.OperationalError):
        scanner.poll(CYCLE)
    assert scanner.store.feed_state("marketwatch") is None

    assert scanner.poll(CYCLE + timedelta(minutes=5)) == (1, 0, 4)
    assert scanner.store.feed_state("marketwatch").etag == '"v1"'


def test_the_same_story_again_in_one_feed_is_not_new_news(build):
    """Regression: Google News listed a second copy of the same headline (another outlet, another link); it was
    triaged again, lifted the cooldown and sent a second alert for the same news."""
    google = Feed("google-news-stock-drops", "Google News", "https://news.example.com/rss")
    first = ("AMD shares slide after weak guidance - Stocktwits", "https://news.example.com/a", CYCLE)
    copy = ("AMD shares slide after weak guidance - Yahoo Finance", "https://news.example.com/b", CYCLE)
    session = FakeSession({**routes(), google.url: rss(first)})
    triage_model, analysis_model, notifier = FakeChatModel(triage_reply), FakeChatModel(ANALYSIS), FakeNotifier()
    scanner = build(
        session=session, feeds=[google], triage_model=triage_model, analysis_model=analysis_model, notifiers=[notifier]
    )
    assert len(scanner.run_cycle(CYCLE).opportunities) == 1

    session.routes[google.url] = rss(first, copy)
    second = scanner.run_cycle(CYCLE + timedelta(minutes=30))

    assert (second.new_articles, second.triaged, second.candidates) == (0, 0, 0)
    assert len(triage_model.calls) == len(analysis_model.calls) == len(notifier.sent) == 1


def test_news_triaged_a_cycle_late_ends_the_cooldown(build):
    """Regression: an article fetched with the one that was analysed, but triaged a cycle later (the model was
    unavailable), was never "newer" than the analysis, so the ticker stayed locked for 24 hours."""
    items = (
        ("AMD shares slide after weak guidance", "https://example.com/a1", CYCLE - timedelta(minutes=30)),
        ("AMD CFO resigns amid accounting probe", "https://example.com/a2", CYCLE - timedelta(minutes=10)),
    )
    down = {"now": True}

    def triage_model_reply(system, prompt, json_mode):
        if "CFO resigns" in prompt:
            if down["now"]:
                raise LLMUnavailableError("The service is overloaded (529).")
            return {"articles": [{"id": "a1", "companies": [company("AMD", "AMD", "direct", "negative", 5)]}]}
        return triage_reply(system, prompt, json_mode)

    analysis_model = FakeChatModel(ANALYSIS)
    scanner = build(
        session=FakeSession({**routes(), MARKETWATCH_URL: rss(*items)}),
        feeds=FEEDS[:1],
        triage_model=FakeChatModel(triage_model_reply),
        analysis_model=analysis_model,
        config=ScannerConfig(scan=ScanConfig(triage_batch_size=1, context_news=False, reanalyse_same_session_hours=0)),
    )
    first = scanner.run_cycle(CYCLE)
    assert first.triaged == 1 and len(first.opportunities) == 1

    down["now"] = False
    second = scanner.run_cycle(CYCLE + timedelta(minutes=5))

    assert (second.new_articles, second.triaged, second.candidates) == (0, 1, 1)
    assert len(analysis_model.calls) == 2
    assert "CFO resigns" in analysis_model.prompts[1]


def test_articles_left_pending_by_an_outage_are_not_triaged_once_too_old(build):
    """Regression: after a long outage the whole backlog was triaged, oldest first, long after it mattered."""
    triage_model = FakeChatModel(LLMUnavailableError("The service is overloaded (529)."))
    session = FakeSession(routes())
    scanner = build(session=session, feeds=FEEDS[:1], triage_model=triage_model)
    assert scanner.run_cycle(CYCLE).triaged == 0  # 4 articles stay pending

    later = CYCLE + timedelta(hours=26)
    session.routes[MARKETWATCH_URL] = rss(("AMD shares slide again", "https://example.com/new", later))
    scanner.triage_model = recovered = FakeChatModel(triage_reply)
    result = scanner.run_cycle(later)

    assert result.triaged == 1
    [(_, prompt, _)] = recovered.calls
    assert "AMD shares slide again" in prompt and "Boeing" not in prompt and "TSMC" not in prompt


def test_sec_feeds_are_left_out_without_a_contact_user_agent(build, caplog):
    session = FakeSession(routes())
    analysis_model = FakeChatModel(ANALYSIS)
    with caplog.at_level(logging.WARNING, logger="dip_scanner"):
        scanner = build(session=session, analysis_model=analysis_model, sec_user_agent=None)
    assert [feed.key for feed in scanner.feeds] == ["marketwatch"]
    assert "Skipping feed sec-8k" in caplog.text and "SEC_USER_AGENT" in caplog.text

    result = scanner.run_cycle(CYCLE)

    assert (result.feeds_ok, result.feeds_failed) == (1, 0)
    assert not any("sec.gov" in url for url in session.urls)
    assert len(result.opportunities) == 1
    assert "Not available" in analysis_model.prompts[0]  # no fundamentals without SEC_USER_AGENT


# --- failures during the cycle -------------------------------------------------------------------------------------


def test_a_failed_analysis_is_noted_and_the_ticker_backs_off(build):
    replies = {"next": LLMError("The reply was blocked by the content filter.")}
    analysis_model = FakeChatModel(lambda system, prompt, json_mode: replies["next"])
    scanner = build(analysis_model=analysis_model)

    first = scanner.run_cycle(CYCLE)
    assert first.opportunities == [] and first.report_paths == [] and first.alerts == []
    assert any(
        note.startswith("Analysis of AMD failed (1 time in a row), retrying after 2026-09-25 21:00 UTC")
        for note in first.notes
    )
    assert scanner.store.analysis_failures("AMD") == (1, CYCLE)

    # Within the 30-minute backoff the model isn't asked again.
    second = scanner.run_cycle(CYCLE + timedelta(minutes=10))
    assert second.candidates == 0 and len(analysis_model.calls) == 1
    assert any("waiting before trying again: AMD (1 failure, next try 21:00 UTC)" in note for note in second.notes)

    # After it, it is; a second failure doubles the wait.
    third = scanner.run_cycle(CYCLE + timedelta(minutes=31))
    assert len(analysis_model.calls) == 2 and third.opportunities == []
    assert scanner.store.analysis_failures("AMD")[0] == 2

    # A success clears the record.
    replies["next"] = ANALYSIS
    fourth = scanner.run_cycle(CYCLE + timedelta(minutes=31 + 61))
    assert [opp.ticker for opp in fourth.opportunities] == ["AMD"]
    assert scanner.store.analysis_failures("AMD") is None


def test_an_unavailable_model_leaves_the_remaining_candidates_for_the_next_cycle(build):
    session = FakeSession(
        {
            **routes(),
            "https://query1.finance.yahoo.com/v8/finance/chart/BA": routes()[
                "https://query1.finance.yahoo.com/v8/finance/chart/AMD"
            ],
        }
    )
    analysis_model = FakeChatModel(LLMUnavailableError("The service is overloaded (529)."))
    scanner = build(session=session, analysis_model=analysis_model)

    result = scanner.run_cycle(CYCLE)

    assert result.candidates == 2 and result.opportunities == []
    assert len(analysis_model.calls) == 1  # the second candidate wasn't even tried
    assert any("unavailable, left for the next cycle: AMD, BA" in note for note in result.notes)
    assert scanner.store.analysis_failures("AMD") is None  # not the ticker's fault: no backoff


def test_setup_errors_stop_the_cycle(build):
    scanner = build(triage_model=FakeChatModel(LLMSetupError("Incorrect API key provided.")))
    with pytest.raises(LLMSetupError, match="Incorrect API key"):
        scanner.run_cycle(CYCLE)


def test_a_failed_notification_is_noted_and_retried_next_cycle(build):
    notifier = FakeNotifier(NotifyError("SMTP server smtp.example.com:587 refused the connection."))
    healthy = FakeNotifier()
    scanner = build(notifiers=[notifier])

    first = scanner.run_cycle(CYCLE)
    assert len(notifier.sent) == 1
    assert any(note.startswith("Couldn't send the alerts by fake: SMTP server") for note in first.notes)
    assert scanner.store.unnotified() == first.alerts  # not marked as sent

    scanner.notifiers = [healthy]
    scanner.run_cycle(CYCLE + timedelta(minutes=5))
    [(subject, _, _)] = healthy.sent  # the earlier alert, sent now
    assert subject == "Dip alert: AMD (score 68)"
    assert scanner.store.unnotified() == []


def test_no_notify_results_are_not_pushed_by_a_later_run(build):
    """Regression: a `run --no-notify` test run's alerts were sent by the next cron or watch cycle."""
    notifier = FakeNotifier()
    scanner = build(notifiers=[notifier], notify=False)
    result = scanner.run_cycle(CYCLE)
    assert len(result.alerts) == 1 and notifier.sent == []
    assert scanner.store.unnotified() == []

    scanner.notify = True
    scanner.run_cycle(CYCLE + timedelta(minutes=5))
    assert notifier.sent == []


def test_cycles_without_a_channel_are_not_pushed_once_one_is_set_up(build):
    scanner = build(notifiers=[])
    assert len(scanner.run_cycle(CYCLE).alerts) == 1
    scanner.notifiers = [notifier := FakeNotifier()]
    scanner.run_cycle(CYCLE + timedelta(hours=10))
    assert notifier.sent == []


def test_only_opportunities_passing_the_alert_rules_are_sent(build):
    notifier = FakeNotifier()
    scanner = build(notifiers=[notifier], analysis_model=FakeChatModel({**ANALYSIS, "verdict": "fundamental"}))
    result = scanner.run_cycle(CYCLE)
    assert len(result.opportunities) == 1 and result.alerts == [] and notifier.sent == []

    good = make_opportunity(score=80.0)
    assert scanner.is_alert(good)
    assert not scanner.is_alert(make_opportunity(score=64.9))
    assert not scanner.is_alert(make_opportunity(score=80.0, analysis=make_analysis(probability_up_6m=59)))


def test_chat_channels_get_the_short_alert_and_email_the_full_report(build):
    hook = FakeSession({"https://hooks.slack.com/": "ok"})
    slack = WebhookNotifier("https://hooks.slack.com/services/T000/B000/XXXXXXXXXXXXXXXX", "slack", session=hook)
    email_like = FakeNotifier()
    scanner = build(notifiers=[slack, email_like])

    result = scanner.run_cycle(CYCLE)

    [call] = hook.calls
    text = call["json"]["text"]
    assert "1 new dip opportunity" in text and "score 67.8" in text
    # Regression: the chat text ended with this host's absolute report path (useless on a phone, and it leaks the
    # folder and user name to the chat service).
    assert "Full report" not in text and str(result.report_paths[1].parent.parent) not in text
    assert "| # | Ticker |" not in text  # not the report table
    assert email_like.sent[0][1].startswith("# Dip alerts")


# --- manual analysis -----------------------------------------------------------------------------------------------


def test_analyze_ticker_ignores_the_thresholds_and_uses_stored_and_fresh_news(build):
    analysis_model = FakeChatModel(ANALYSIS)
    config = ScannerConfig(
        scan=ScanConfig(context_news=False),
        dip=DipConfig(min_drop_1d_pct=50, min_drop_5d_pct=50, min_drawdown_20d_pct=50),
    )
    scanner = build(analysis_model=analysis_model, config=config)
    stored = make_article(title="AMD cuts guidance", published=CYCLE - timedelta(hours=3), fetched=CYCLE)
    scanner.store.add_articles([stored], max_age_hours=24, now=CYCLE)
    scanner.store.record_triage([stored.id], [make_impact(article_id=stored.id)])

    opp = scanner.analyze_ticker("$amd", now=CYCLE)

    assert opp.ticker == "AMD" and opp.id is None and opp.dip_reasons == []
    assert scanner.store.opportunities() == []  # not stored: the CLI decides
    assert [headline["title"] for headline in opp.headlines] == ["AMD cuts guidance"]
    prompt = analysis_model.prompts[0]
    assert "manual analysis (no dip thresholds applied)" in prompt
    assert "Is AMD stock a buy after the guidance cut?" in prompt  # context news even with context_news = false


def test_analyze_ticker_without_prices_raises_price_error(build):
    scanner = build()
    with pytest.raises(PriceError):
        scanner.analyze_ticker("NOSUCH", now=CYCLE)


# --- housekeeping --------------------------------------------------------------------------------------------------


def test_prune_runs_once_a_day(build, monkeypatch):
    scanner = build(feeds=[])
    calls = []
    monkeypatch.setattr(scanner.store, "prune", lambda *, older_than: calls.append(older_than) or 0)
    for minutes in (0, 5, 60 * 23):
        scanner.run_cycle(CYCLE + timedelta(minutes=minutes))
    scanner.run_cycle(CYCLE + timedelta(hours=25))
    assert calls == [CYCLE - timedelta(days=30), CYCLE + timedelta(hours=25) - timedelta(days=30)]


# --- the watch loop ------------------------------------------------------------------------------------------------


def _clock(*times: datetime):
    queue = list(times)
    return lambda: queue.pop(0)


def test_watch_aligns_cycles_to_the_interval_and_keeps_going_after_errors(build, caplog):
    scanner = build()
    started: list[datetime] = []

    def cycle(now=None):
        started.append(now)
        if len(started) == 2:
            raise RuntimeError("database is locked")
        return CycleResult(started=now, finished=now + timedelta(seconds=40))

    scanner.run_cycle = cycle
    sleeps: list[float] = []
    t = datetime(2026, 9, 25, 15, 2, 10, tzinfo=UTC)
    clock = _clock(
        t,  # first cycle starts at once
        t + timedelta(seconds=50),  # it ended at 15:03:00 -> sleep until 15:05
        datetime(2026, 9, 25, 15, 5, 0, 500_000, tzinfo=UTC),
        datetime(2026, 9, 25, 15, 6, tzinfo=UTC),  # the failed cycle ended -> sleep until 15:10
        datetime(2026, 9, 25, 15, 10, tzinfo=UTC),
    )
    with caplog.at_level(logging.INFO, logger="dip_scanner"):
        scanner.watch(interval_minutes=5, max_cycles=3, sleep=sleeps.append, clock=clock)

    assert sleeps == [120.0, 240.0]
    assert started[0] == t and len(started) == 3
    assert "The cycle failed; trying again at the next interval." in caplog.text
    assert "database is locked" in caplog.text
    assert caplog.text.count("Cycle 2026-09-25") == 2  # one summary line per successful cycle


def test_watch_stops_cleanly_on_ctrl_c(build, caplog):
    scanner = build()
    scanner.run_cycle = lambda now=None: CycleResult(started=now)

    def interrupt(seconds: float) -> None:
        raise KeyboardInterrupt

    with caplog.at_level(logging.INFO, logger="dip_scanner"):
        scanner.watch(sleep=interrupt, clock=lambda: CYCLE)  # returns instead of raising
    assert "Stopped after 1 cycle." in caplog.text


def test_watch_stops_on_setup_errors(build):
    scanner = build(triage_model=FakeChatModel(LLMSetupError("No deployment named gpt-x.")))
    with pytest.raises(LLMSetupError):
        scanner.watch(sleep=lambda _: None, clock=lambda: CYCLE, max_cycles=5)


def test_seconds_until_next_boundary():
    assert seconds_until_next(datetime(2026, 9, 25, 15, 3, tzinfo=UTC), 300) == 120
    assert seconds_until_next(datetime(2026, 9, 25, 15, 5, tzinfo=UTC), 300) == 300  # on a boundary: the next one
    assert seconds_until_next(datetime(2026, 9, 25, 15, 59, 30, tzinfo=UTC), 3600) == 30


# --- small helpers -------------------------------------------------------------------------------------------------


def test_alert_subject_names_the_best_first():
    opps = [
        make_opportunity(ticker=ticker, score=score)
        for ticker, score in zip("ABCDE", (50, 90, 70, 60, 80), strict=True)
    ]
    assert alert_subject(opps[:1]) == "Dip alert: A (score 50)"
    assert alert_subject(opps) == "Dip alerts: B (score 90), E (score 80), C (score 70), D (score 60) and 1 more"


def test_cycle_summary_line():
    result = CycleResult(
        started=CYCLE,
        finished=CYCLE + timedelta(seconds=42),
        feeds_ok=19,
        feeds_failed=1,
        new_articles=1,
        triaged=1,
        impacts=2,
        candidates=1,
        opportunities=[make_opportunity(score=72.4)],
    )
    assert result.summary() == (
        "Cycle 2026-09-25 20:30 UTC: 19/20 feeds ok, 1 new article, 1 triaged, 2 company impacts, 1 candidate, "
        "1 opportunity (AMD 72.4), 0 alerts; took 42 s"
    )


# --- candidates in backoff, repeated and retried alerts, thesis changes -------------------------------------------

CHART_PREFIX = "https://query1.finance.yahoo.com/v8/finance/chart/"
QUIET = ScannerConfig(scan=ScanConfig(context_news=False))


def seed(scanner: Scanner, ticker: str, *, magnitude: int = 3, at: datetime = CYCLE, title: str | None = None):
    """A stored, triaged article with one negative direct impact on ticker, published half an hour before at."""
    article = make_article(
        title=title or f"{ticker} shares fall after a surprise warning from management",
        published=at - timedelta(minutes=30),
        fetched=at - timedelta(minutes=30),
    )
    scanner.store.add_articles([article], max_age_hours=24, now=at)
    scanner.store.record_triage([article.id], [make_impact(article_id=article.id, ticker=ticker, magnitude=magnitude)])
    return article


def test_tickers_in_backoff_do_not_take_the_places_of_ready_ones(build):
    """Regression: two failing tickers with bigger drops filled max_candidates_per_cycle = 2 and were then dropped
    by the backoff, so the two ready dips weren't analysed for up to a day."""
    config = ScannerConfig(scan=ScanConfig(context_news=False, max_candidates_per_cycle=2))
    analysis_model = FakeChatModel(ANALYSIS)
    session = FakeSession({CHART_PREFIX: fixture_json("yahoo_chart_amd.json")})
    scanner = build(session=session, feeds=[], analysis_model=analysis_model, config=config, sec_user_agent=None)
    for ticker, magnitude in (("FA", 5), ("FB", 5), ("OKA", 2), ("OKB", 2)):
        seed(scanner, ticker, magnitude=magnitude)
    for ticker in ("FA", "FB"):
        scanner.store.record_analysis_failure(ticker, when=CYCLE - timedelta(minutes=5), error="content filter")

    result = scanner.run_cycle(CYCLE)

    assert result.candidates == 2 and len(analysis_model.calls) == 2
    assert sorted(opp.ticker for opp in result.opportunities) == ["OKA", "OKB"]
    notes = "\n".join(result.notes)
    assert "waiting before trying again: FA (1 failure" in notes and "FB (1 failure" in notes
    assert "Over the limit" not in notes


def test_a_developing_story_is_analysed_again_but_alerted_once(build):
    """Regression: one new article per cycle re-analysed and re-alerted the same ticker every five minutes."""
    notifier = FakeNotifier()
    replies = {"probability": 75}
    analysis_model = FakeChatModel(lambda *_: {**ANALYSIS, "probability_up_6m": replies["probability"]})
    session = FakeSession(routes())
    config = ScannerConfig(scan=ScanConfig(context_news=False, reanalyse_same_session_hours=0))
    scanner = build(
        session=session, feeds=FEEDS[:1], analysis_model=analysis_model, notifiers=[notifier], config=config
    )
    items = []
    for n in range(6):
        when = CYCLE + timedelta(minutes=5 * n)
        items.append((f"AMD shares slide as analysts react, update {n}", f"https://example.com/{n}", when))
        session.routes[MARKETWATCH_URL] = rss(*items)
        result = scanner.run_cycle(when)
    assert len(analysis_model.calls) == 6 and len(notifier.sent) == 1
    assert any("nothing material changed, not sent again: AMD (score 67.8, alerted at 67.8)" in n for n in result.notes)

    # A materially better score is news: alerted again.
    replies["probability"] = 90
    items.append(("AMD shares slide as analysts react, update 6", "https://example.com/6", CYCLE + timedelta(hours=1)))
    session.routes[MARKETWATCH_URL] = rss(*items)
    scanner.run_cycle(CYCLE + timedelta(hours=1))
    assert len(notifier.sent) == 2 and notifier.sent[1][0] == "Dip alert: AMD (score 78)"


def test_a_retried_alert_is_replaced_by_the_newer_analysis_of_the_ticker(build):
    """Regression: a failed alert was sent 20 hours later next to the newer analysis of the same stock, both as
    "new", one with a stale price."""
    broken, working = FakeNotifier(NotifyError("down")), FakeNotifier()
    session = FakeSession(routes())
    scanner = build(session=session, feeds=FEEDS[:1], notifiers=[broken], config=QUIET)
    [first] = scanner.run_cycle(CYCLE).alerts

    later = CYCLE + timedelta(hours=20)
    session.routes[MARKETWATCH_URL] = rss(
        ("AMD shares slide again on a second downgrade", "https://example.com/x", later)
    )
    scanner.notifiers = [working]
    [second] = scanner.run_cycle(later).alerts

    [(subject, markdown, _)] = working.sent
    assert subject == "Dip alert: AMD (score 68)" and "_1 opportunity ·" in markdown
    assert scanner.store.unnotified() == []
    assert scanner.store.last_alerted("AMD").id == second.id != first.id


def test_a_stale_alert_contradicted_by_a_newer_analysis_is_never_sent(build):
    broken, working = FakeNotifier(NotifyError("down")), FakeNotifier()
    verdict = {"now": ANALYSIS}
    session = FakeSession(routes())
    scanner = build(
        session=session,
        feeds=FEEDS[:1],
        notifiers=[broken],
        config=QUIET,
        analysis_model=FakeChatModel(lambda *_: verdict["now"]),
    )
    scanner.run_cycle(CYCLE)

    later = CYCLE + timedelta(hours=20)
    session.routes[MARKETWATCH_URL] = rss(("AMD shares slide on an accounting probe", "https://example.com/p", later))
    verdict["now"] = {**ANALYSIS, "verdict": "fundamental", "probability_up_6m": 30}
    scanner.notifiers = [working]
    scanner.run_cycle(later)

    assert working.sent == []  # the old bullish alert was superseded; nothing had been sent, so no thesis change


def test_a_thesis_change_after_an_alert_is_sent_once(build):
    """Regression: an alerted idea re-analysed as "fundamental damage" sent nothing, and `report` still listed the
    old bullish idea first."""
    notifier = FakeNotifier()
    verdict = {"now": ANALYSIS}
    session = FakeSession(routes())
    scanner = build(
        session=session,
        feeds=FEEDS[:1],
        notifiers=[notifier],
        config=QUIET,
        analysis_model=FakeChatModel(lambda *_: verdict["now"]),
    )
    scanner.run_cycle(CYCLE)
    assert len(notifier.sent) == 1

    later = CYCLE + timedelta(hours=44)
    session.routes[MARKETWATCH_URL] = rss(
        ("AMD shares slide as the SEC opens an accounting probe", "https://e.com/p", later)
    )
    verdict["now"] = {**ANALYSIS, "verdict": "fundamental", "probability_up_6m": 25, "confidence": "high"}
    result = scanner.run_cycle(later)
    scanner.run_cycle(later + timedelta(minutes=5))

    assert result.alerts == [] and len(result.thesis_changes) == 1
    assert len(notifier.sent) == 2
    subject, markdown, html = notifier.sent[1]
    assert (
        subject == "Thesis change: AMD now Fundamental damage (was Temporary fear, entry $132.00) - review open orders"
    )
    assert "If you placed orders on the earlier idea, review them." in markdown
    assert scanner.store.unnotified() == []

    report = render_markdown(scanner.store.opportunities(), title="Report", generated=later)
    assert "Temporary fear (superseded)" in report
    assert "**Superseded: analysed again on 2026-09-27 16:30 UTC: Fundamental damage, 25% chance up in 6m" in report


def test_a_setup_error_mid_analysis_still_reports_and_alerts_what_was_found(build):
    """Regression: insufficient_quota on the second candidate left the first one's paid-for analysis without a
    report or an alert, and in its cooldown."""
    session = FakeSession({**routes(), CHART_PREFIX + "BA": fixture_json("yahoo_chart_amd.json")})
    notifier = FakeNotifier()
    analysis_model = FakeChatModel([ANALYSIS, LLMSetupError("insufficient_quota")])
    scanner = build(session=session, analysis_model=analysis_model, notifiers=[notifier], config=QUIET)

    with pytest.raises(LLMSetupError, match="insufficient_quota"):
        scanner.run_cycle(CYCLE)

    latest = (scanner.settings.data_dir / "reports" / "latest.md").read_text(encoding="utf-8")
    assert (
        "## AMD" in latest
        and "Analysis stopped, the model can't be used: insufficient\\_quota. Not analysed: BA" in latest
    )
    [(subject, _, _)] = notifier.sent
    assert subject == "Dip alert: AMD (score 68)"


# --- model use, the daily analysis limit and system notices --------------------------------------------------------


def test_every_model_call_is_stored_with_its_tokens_and_totalled_for_the_day(build, caplog):
    triage_model = FakeChatModel(triage_reply, name="fake-triage", usage=(1_200, 150))
    analysis_model = FakeChatModel(ANALYSIS, name="fake-analysis", usage=(3_500, 900))
    scanner = build(triage_model=triage_model, analysis_model=analysis_model, config=QUIET)

    with caplog.at_level(logging.INFO, logger="dip_scanner"):
        result = scanner.run_cycle(CYCLE)

    assert result.model_calls == 2
    assert [(row.step, row.model, row.calls, row.input_tokens, row.output_tokens) for row in result.usage_today] == [
        ("triage", "fake-triage", 1, 1_200, 150),
        ("analysis", "fake-analysis", 1, 3_500, 900),
    ]
    assert result.summary().endswith("; model today: 2 calls, 4.7k tokens in, 1.1k out")
    assert usage_lines(result.usage_today) == [
        "triage with fake-triage: 1 call, 1.2k tokens in, 150 out",
        "analysis with fake-analysis: 1 call, 3.5k tokens in, 900 out",
    ]

    # The next day starts from zero; yesterday's calls still count towards the last 24 hours.
    tomorrow = CYCLE.replace(hour=0, minute=5) + timedelta(days=1)
    assert scanner.run_cycle(tomorrow).usage_today == []
    assert scanner.store.analyses_since(tomorrow - timedelta(hours=24)) == 1


def test_calls_that_never_reached_the_model_are_not_counted(build):
    scanner = build(analysis_model=FakeChatModel(LLMUnavailableError("overloaded (529)")), config=QUIET)
    result = scanner.run_cycle(CYCLE)
    assert [row.step for row in result.usage_today] == ["triage"]  # the fake triage answered, without token counts
    assert result.summary().endswith("; model today: 1 call, 0 tokens in, 0 out (1 without token counts)")
    assert scanner.store.analyses_since(CYCLE - timedelta(hours=1)) == 0  # an outage uses up no daily analyses


def test_the_daily_analysis_limit_leaves_the_rest_for_later(build):
    config = ScannerConfig(scan=ScanConfig(context_news=False, max_analyses_per_day=3))
    analysis_model = FakeChatModel(ANALYSIS)
    session = FakeSession({CHART_PREFIX: fixture_json("yahoo_chart_amd.json")})
    scanner = build(session=session, feeds=[], analysis_model=analysis_model, config=config, sec_user_agent=None)
    for hours_ago, ticker in ((30, "OLD"), (20, "X1"), (2, "X2")):  # two analyses in the last 24 hours
        scanner.store.record_model_call(
            when=CYCLE - timedelta(hours=hours_ago), step="analysis", model="gpt-5", ticker=ticker
        )
    for ticker, magnitude in (("BIG", 5), ("MID", 3), ("LOW", 2)):
        seed(scanner, ticker, magnitude=magnitude)

    result = scanner.run_cycle(CYCLE)

    assert [opp.ticker for opp in result.opportunities] == ["BIG"] and result.candidates == 1
    note = next(note for note in result.notes if note.startswith("Daily limit"))
    assert note.startswith(
        "Daily limit of 3 analyses reached ([scan] max_analyses_per_day; 2 in the last 24h), left for later: MID "
    )
    assert "LOW (severity" in note

    # Once the analysis from 20 hours ago is more than a day old, the next one goes.
    later = scanner.run_cycle(CYCLE + timedelta(hours=4, minutes=5))
    assert [opp.ticker for opp in later.opportunities] == ["MID"]

    # 0 means no limit.
    scanner.config = ScannerConfig(scan=ScanConfig(context_news=False, max_analyses_per_day=0))
    assert [opp.ticker for opp in scanner.run_cycle(CYCLE + timedelta(hours=4, minutes=10)).opportunities] == ["LOW"]


def test_a_model_unavailable_for_six_cycles_sends_one_notice_every_12_hours(build):
    notifier = FakeNotifier()
    down = FakeChatModel(LLMUnavailableError("Couldn't connect to https://api.openai.com/v1/."))
    session = FakeSession(routes())
    scanner = build(session=session, feeds=FEEDS[:1], triage_model=down, notifiers=[notifier], config=QUIET)

    for n in range(6):
        when = CYCLE + timedelta(minutes=5 * n)
        session.routes[MARKETWATCH_URL] = rss((f"AMD shares slide, update {n}", f"https://e.com/{n}", when))
        result = scanner.run_cycle(when)
        assert (
            result.model_unavailable
            == "the triage model is unavailable: Couldn't connect to https://api.openai.com/v1/."
        )
        assert len(notifier.sent) == (1 if n == 5 else 0)
    subject, markdown, html = notifier.sent[0]
    assert subject == "dip-scanner: the language model has been unavailable for 6 cycles"
    assert "The last 6 cycles in a row couldn't use the language model (the latest at 2026-09-25 20:55 UTC)" in markdown
    assert "at most once every 12 hours" in markdown and "<h1>dip-scanner: the language model" in html

    # Still down: not repeated for 12 hours, even across separate runs (the time is in the database).
    for hours in (1, 6, 11.9):
        scanner.run_cycle(CYCLE + timedelta(hours=hours))
    assert len(notifier.sent) == 1
    scanner.run_cycle(CYCLE + timedelta(hours=12, minutes=30))
    assert len(notifier.sent) == 2 and "unavailable for 10 cycles" in notifier.sent[1][0]

    # A cycle in which the model answers resets the count.
    scanner.triage_model = FakeChatModel(triage_reply)
    session.routes[MARKETWATCH_URL] = rss(("AMD shares slide again", "https://e.com/x", CYCLE + timedelta(hours=13)))
    scanner.run_cycle(CYCLE + timedelta(hours=13))
    assert scanner.store.bump_streak("model_unavailable") == 1


def test_every_feed_failing_sends_a_notice_without_secrets(build, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-verysecretkey123")
    notifier = FakeNotifier()
    session = FakeSession(
        {
            MARKETWATCH_URL: requests.ConnectionError(
                "proxy https://me:hunter2@proxy.example.com refused, key sk-proj-verysecretkey123"
            )
        }
    )
    scanner = build(session=session, feeds=FEEDS[:1], notifiers=[notifier])

    for n in range(6):
        result = scanner.run_cycle(CYCLE + timedelta(minutes=5 * n))
    assert (result.feeds_ok, result.feeds_failed) == (0, 1)
    [(subject, markdown, _)] = notifier.sent
    assert subject == "dip-scanner: every feed has failed for 6 cycles"
    assert "All 1 feed failed in each of the last 6 cycles" in markdown and "marketwatch: " in markdown
    assert "verysecretkey" not in markdown and "hunter2" not in markdown
    assert "proxy https://***@proxy.example.com refused, key ***" in markdown

    session.routes[MARKETWATCH_URL] = fixture("rss_marketwatch.xml")
    scanner.run_cycle(CYCLE + timedelta(minutes=30))
    assert scanner.store.bump_streak("feeds_failing") == 1  # a feed answered: counting starts again


@pytest.mark.parametrize("how", ["no-notify", "switched off"])
def test_no_system_notices_without_notifications(build, how):
    notifier = FakeNotifier()
    config = ScannerConfig(alerts=AlertConfig(system_notices=how != "switched off", notice_after_cycles=1))
    scanner = build(
        session=FakeSession({MARKETWATCH_URL: 500}),
        feeds=FEEDS[:1],
        notifiers=[notifier],
        notify=how != "no-notify",
        config=config,
    )
    scanner.run_cycle(CYCLE)
    assert notifier.sent == []
    assert scanner.store.bump_streak("feeds_failing") == 2  # counted all the same
