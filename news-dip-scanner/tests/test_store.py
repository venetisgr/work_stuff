import json
import sqlite3
import threading
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import NOW, make_article, make_impact, make_opportunity

from dip_scanner.feeds import FeedState, title_key
from dip_scanner.store import DEFAULT_RECIPIENT, SCANNER_PAUSED, SCHEMA_VERSION, Store


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "data" / "scanner.sqlite3") as db:
        yield db


def article(title: str, *, hours_ago: float = 1, source: str = "marketwatch", **overrides):
    """An article published hours_ago before NOW and fetched at NOW, with the real title_key."""
    values = {
        "source": source,
        "published": NOW - timedelta(hours=hours_ago),
        "fetched": NOW,
        "title_key": title_key(title),
        **overrides,
    }
    return make_article(title=title, **values)


def add(store: Store, *articles, max_age_hours: float = 24, now=NOW):
    return store.add_articles(list(articles), max_age_hours=max_age_hours, now=now)


# --- setup ---------------------------------------------------------------------------------------------------------


def test_creates_the_folder_and_schema_in_wal_mode_and_data_survives_reopening(tmp_path):
    path = tmp_path / "nested" / "dir" / "scanner.sqlite3"
    with Store(path) as db:
        assert db._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        tables = {row[0] for row in db._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"feeds", "articles", "impacts", "tickers", "opportunities"} <= tables
        add(db, article("AMD slides"))
    with Store(path) as db:  # CREATE TABLE IF NOT EXISTS: reopening keeps the data
        assert [a.title for a in db.get_articles([article("AMD slides").id])] == ["AMD slides"]


def test_in_memory_store_and_double_close():
    db = Store(":memory:")
    assert add(db, article("AMD slides")) == [article("AMD slides")]
    db.close()
    db.close()


def test_a_file_that_is_not_a_database_gives_a_clear_error(tmp_path):
    path = tmp_path / "scanner.sqlite3"
    path.write_text("this is not sqlite " * 100)
    with pytest.raises(sqlite3.DatabaseError, match="Couldn't open the scanner database"):
        Store(path)
    with pytest.raises(sqlite3.DatabaseError, match="Couldn't open the scanner database"):
        Store(tmp_path)  # a folder


# --- feeds ---------------------------------------------------------------------------------------------------------


def test_feed_state_round_trip_and_health(store):
    assert store.feed_state("mw") is None
    first = FeedState(etag='"v1"', last_modified="Thu, 24 Sep 2026 10:00:00 GMT")
    store.save_feed_state("mw", first, status=200, error=None, fetched=NOW - timedelta(minutes=5))
    store.save_feed_state("cnbc", FeedState(), status=403, error="HTTP 403 Forbidden.", fetched=NOW)
    assert store.feed_state("mw") == FeedState(etag='"v1"', last_modified="Thu, 24 Sep 2026 10:00:00 GMT")
    assert store.feed_state("cnbc") == FeedState()

    # A later fetch replaces the saved outcome.
    store.save_feed_state("mw", FeedState(etag='"v2"'), status=200, error=None, fetched=NOW)
    assert store.feed_state("mw") == FeedState(etag='"v2"')

    add(store, article("AMD slides", source="mw"), article("Boeing deliveries slow", source="mw"))
    assert store.feed_health() == [
        {"key": "cnbc", "last_fetch": NOW, "last_status": 403, "last_error": "HTTP 403 Forbidden.", "articles": 0},
        {"key": "mw", "last_fetch": NOW, "last_status": 200, "last_error": None, "articles": 2},
    ]


# --- articles ------------------------------------------------------------------------------------------------------


def test_add_articles_returns_only_new_ones_and_round_trips_them(store):
    amd, boeing = article("AMD slides on guidance"), article("Boeing deliveries slow", hours_ago=3)
    assert add(store, amd, boeing) == [amd, boeing]
    assert add(store, amd, boeing) == []  # seen by id
    assert store.get_articles([boeing.id, "unknown", amd.id]) == [boeing, amd]


def test_add_articles_skips_the_same_headline_from_another_source_within_72_hours(store):
    original = article("AMD shares slide after weak guidance", source="marketwatch")
    add(store, original)

    copy = article("AMD shares slide after weak guidance - Reuters", source="google", link="https://example.com/b")
    assert copy.id != original.id and copy.title_key == original.title_key
    assert add(store, copy, now=NOW + timedelta(hours=71)) == []
    assert store.get_articles([copy.id]) == []  # not stored at all

    # Three days later the same headline is a new story again.
    later = replace(copy, published=NOW + timedelta(hours=80), fetched=NOW + timedelta(hours=80))
    assert add(store, later, now=NOW + timedelta(hours=80)) == [later]


def test_add_articles_dedups_within_one_batch(store):
    first = article("Fed signals more rate cuts ahead", source="a")
    same_id = article("Fed signals more rate cuts ahead", source="b")  # same link -> same id
    same_title = article("Fed signals more rate cuts ahead", source="c", link="https://example.com/other")
    same_feed = article("Fed signals more rate cuts ahead", source="a", link="https://example.com/syndicated")
    assert add(store, first, same_id, same_title, same_feed) == [first]


def test_a_repeated_headline_in_the_same_feed_is_the_same_story(store):
    """Regression: Google News lists one story from several outlets, each with its own link; every copy was triaged
    again, added corroboration and lifted the cooldown (a second alert for the same news)."""
    first = article("HPE stock drops 11% after Evercore downgrade - Stocktwits", source="google-news-stock-drops")
    copy = article(
        "HPE stock drops 11% after Evercore downgrade - Yahoo Finance",
        source="google-news-stock-drops",
        link="https://news.google.com/rss/articles/other",
    )
    assert add(store, first) == [first]
    assert add(store, copy, now=NOW + timedelta(minutes=30)) == []


def test_the_same_title_from_a_formulaic_feed_with_a_new_link_is_a_new_article(store):
    # SEC 8-K titles repeat for every filing of a company; each filing has its own link (and id).
    title = "8-K - Advanced Micro Devices Inc (0000002488) (Filer)"
    first = article(title, source="sec-8k-filings", link="https://www.sec.gov/Archives/edgar/data/2488/1-index.htm")
    second = article(title, source="sec-8k-filings", link="https://www.sec.gov/Archives/edgar/data/2488/2-index.htm")
    exempt = {"sec-8k-filings"}
    assert store.add_articles([first], max_age_hours=24, now=NOW, same_source_titles=exempt) == [first]
    later = NOW + timedelta(hours=5)
    assert store.add_articles([second], max_age_hours=24, now=later, same_source_titles=exempt) == [second]
    # ...while the same headline from another source is still a duplicate.
    elsewhere = article(title, source="google", link="https://example.com/amd-8k")
    assert add(store, elsewhere, now=NOW + timedelta(hours=6)) == []


def test_short_formulaic_headlines_of_different_companies_are_all_kept(store):
    """Regression: "Profit warning - Continental AG" and "Profit warning - Puma SE" (and "Transaction in Own
    Shares" from any issuer) had the same key; the second company's news was never stored."""
    continental = article("Profit warning - Continental AG", source="pr-newswire")
    puma = article("Profit warning - Puma SE", source="globenewswire", link="https://example.com/puma")
    shares = [
        article("Transaction in Own Shares", source=source, link=f"https://example.com/{source}")
        for source in ("rns-a", "rns-b")
    ]
    assert continental.title_key != puma.title_key
    assert add(store, continental) == [continental]
    assert add(store, puma, *shares, now=NOW + timedelta(hours=1)) == [puma, *shares]


def test_an_old_skipped_copy_does_not_hide_a_fresh_story(store):
    """Regression: Google News listed a 200-day-old "Boeing halts 737 MAX deliveries"; stored as skipped (fetched
    now), it blocked the fresh CNBC story with the same headline for 72 hours."""
    old = article("Boeing halts 737 MAX deliveries again", hours_ago=200 * 24, source="reuters-business")
    fresh = article("Boeing halts 737 MAX deliveries again", source="cnbc", link="https://example.com/fresh")
    assert add(store, old) == [] and store.article_status(old.id) == ("skipped", 0)
    assert add(store, fresh, now=NOW + timedelta(hours=1)) == [fresh]


def test_an_article_another_process_stored_meanwhile_is_skipped_not_an_error(tmp_path):
    """Regression: `run` next to `watch` could insert the same id between the check and the insert; the
    IntegrityError aborted the whole poll."""
    path = tmp_path / "scanner.sqlite3"
    story = article("AMD slides")  # too short to be matched by title: only the id decides
    with Store(path) as mine, Store(path) as other:
        real = mine._conn

        class RacingConnection:
            def execute(self, sql, params=()):
                result = real.execute(sql, params)
                if sql.startswith("SELECT 1 FROM articles WHERE id"):  # the other process wins the race
                    other.add_articles([story], max_age_hours=24, now=NOW)
                return result

            def __enter__(self):
                return real.__enter__()

            def __exit__(self, *exc):
                return real.__exit__(*exc)

        mine._conn = RacingConnection()
        try:
            assert mine.add_articles([story], max_age_hours=24, now=NOW) == []
        finally:
            mine._conn = real
        assert [a.id for a in other.get_articles([story.id])] == [story.id]


def test_too_old_articles_are_stored_as_skipped_and_never_triaged(store):
    fresh, stale = article("Fresh news", hours_ago=2), article("Old news", hours_ago=30)
    assert add(store, fresh, stale, max_age_hours=24) == [fresh]
    assert store.article_status(stale.id) == ("skipped", 0)
    assert store.pending_triage(10, max_attempts=3) == [fresh]
    assert add(store, stale) == []  # already stored, so still not new


def test_stale_pending_articles_are_retired(store):
    """Regression: after a model outage, days-old pending articles were still triaged, oldest first."""
    story = article("AMD shares slide after weak guidance")
    add(store, story)
    assert store.skip_stale_pending(NOW - timedelta(hours=4)) == 0  # 20 hours later, with a 24-hour limit
    assert store.skip_stale_pending(NOW) == 1  # 25 hours later
    assert store.pending_triage(10, max_attempts=3) == []
    assert store.article_status(story.id) == ("skipped", 0)


def test_pending_triage_is_oldest_first_and_limited(store):
    newest, middle, oldest = (article(f"Story {n}", hours_ago=n) for n in (1, 2, 3))
    add(store, newest, middle, oldest)
    assert store.pending_triage(2, max_attempts=3) == [oldest, middle]
    assert store.pending_triage(10, max_attempts=3) == [oldest, middle, newest]


def test_triage_state_machine(store):
    ok, flaky, broken, skipped = (article(f"Story {n}", hours_ago=n) for n in (1, 2, 3, 40))
    add(store, ok, flaky, broken, skipped)

    store.record_triage([ok.id], [make_impact(article_id=ok.id, ticker="AMD")])
    assert store.article_status(ok.id) == ("done", 0)

    store.record_triage_failure([flaky.id, broken.id], max_attempts=2)
    assert store.article_status(flaky.id) == ("pending", 1)
    assert store.pending_triage(10, max_attempts=2) == [broken, flaky]

    store.record_triage_failure([broken.id, ok.id, skipped.id], max_attempts=2)
    assert store.article_status(broken.id) == ("failed", 2)
    assert store.article_status(ok.id) == ("done", 0)  # only pending articles count failures
    assert store.article_status(skipped.id) == ("skipped", 0)
    assert store.pending_triage(10, max_attempts=2) == [flaky]

    # A later success after a failure is still a success.
    store.record_triage([flaky.id], [])
    assert store.article_status(flaky.id) == ("done", 1)
    assert store.pending_triage(10, max_attempts=2) == []


def test_pending_triage_skips_articles_that_used_up_their_attempts_under_a_lower_limit(store):
    story = article("Story")
    add(store, story)
    store.record_triage_failure([story.id], max_attempts=5)
    store.record_triage_failure([story.id], max_attempts=5)
    assert store.pending_triage(10, max_attempts=2) == []
    assert store.pending_triage(10, max_attempts=3) == [story]


def test_record_triage_replaces_earlier_impacts(store):
    story = article("Chip export rules tightened")
    add(store, story)
    store.record_triage(
        [story.id], [make_impact(article_id=story.id, ticker="NVDA"), make_impact(article_id=story.id, ticker="AMD")]
    )
    store.record_triage([story.id], [make_impact(article_id=story.id, ticker="AMD", magnitude=2)])
    [(impact, found)] = store.recent_impacts(NOW - timedelta(days=1))
    assert (impact.ticker, impact.magnitude, found) == ("AMD", 2, story)


def test_recent_impacts_newest_article_first_with_round_tripped_values(store):
    old, new, older_than_window = (
        article("Old", hours_ago=10),
        article("New", hours_ago=1),
        article("Ancient", hours_ago=60),
    )
    add(store, old, new, older_than_window, max_age_hours=100)
    boeing = make_impact(
        article_id=old.id,
        ticker="BA",
        company="Boeing",
        relation="indirect",
        direction="mixed",
        magnitude=2,
        event_type="supply_chain",
        rationale="Supplier delays.",
    )
    impacts = [
        boeing,
        make_impact(article_id=new.id, ticker="AMD"),
        make_impact(article_id=new.id, ticker="NVDA", direction="positive"),
        make_impact(article_id=older_than_window.id, ticker="INTC"),
    ]
    store.record_triage([old.id, new.id, older_than_window.id], impacts)

    result = store.recent_impacts(NOW - timedelta(hours=48))

    assert [(impact.ticker, found.title) for impact, found in result] == [
        ("AMD", "New"),
        ("NVDA", "New"),
        ("BA", "Old"),
    ]
    assert result[2] == (boeing, old)
    assert result[0][1] is result[1][1]  # one Article object per article


def test_news_lists_articles_with_their_impacts_and_filters_by_ticker(store):
    quiet, amd, both = (
        article("Quiet day", hours_ago=1),
        article("AMD slides", hours_ago=2),
        article("Chip rules", hours_ago=3),
    )
    add(store, quiet, amd, both, article("Last week", hours_ago=100), max_age_hours=200)
    impacts = [
        make_impact(article_id=amd.id, ticker="AMD"),
        make_impact(article_id=both.id, ticker="AMD"),
        make_impact(article_id=both.id, ticker="NVDA"),
    ]
    store.record_triage([quiet.id, amd.id, both.id], impacts)

    news = store.news(NOW - timedelta(hours=24))
    assert [(a.title, [i.ticker for i in impacts]) for a, impacts in news] == [
        ("Quiet day", []),
        ("AMD slides", ["AMD"]),
        ("Chip rules", ["AMD", "NVDA"]),
    ]
    nvda = store.news(NOW - timedelta(hours=24), ticker="nvda")
    assert [(a.title, [i.ticker for i in impacts]) for a, impacts in nvda] == [("Chip rules", ["NVDA"])]

    # With the symbols whose news belongs to it (an old symbol, a preferred listing's other one): relabelled, once.
    both_ways = store.news(NOW - timedelta(hours=24), ticker="NVDA", also={"amd"})
    assert [(a.title, [i.ticker for i in impacts]) for a, impacts in both_ways] == [
        ("AMD slides", ["NVDA"]),
        ("Chip rules", ["NVDA"]),
    ]
    assert both_ways[0][1][0] == replace(impacts[0], ticker="NVDA")
    assert both_ways[1][1][0] == impacts[2]  # filed under both: its own impact


def test_get_articles_handles_many_ids(store):
    stories = [article(f"Story number {n}", hours_ago=n / 100) for n in range(1200)]
    add(store, *stories)
    ids = [story.id for story in reversed(stories)]
    assert [a.id for a in store.get_articles(ids)] == ids
    assert store.get_articles([]) == []


# --- tickers -------------------------------------------------------------------------------------------------------


def test_ticker_validity_and_expiry_of_invalid_marks(store):
    assert store.ticker_valid("AMD", now=NOW) is None
    store.set_ticker_valid("amd", True, checked=NOW - timedelta(days=30))
    store.set_ticker_valid("XYZQ", False, checked=NOW - timedelta(days=6))
    store.set_ticker_valid("OLDQ", False, checked=NOW - timedelta(days=7))
    assert store.ticker_valid("AMD", now=NOW) is True  # valid marks don't expire
    assert store.ticker_valid("xyzq", now=NOW) is False
    assert store.ticker_valid("OLDQ", now=NOW) is None  # re-check after 7 days
    assert store.ticker_valid("XYZQ", now=NOW + timedelta(days=1)) is None

    store.set_ticker_valid("XYZQ", True, checked=NOW)  # listed after all
    assert store.ticker_valid("XYZQ", now=NOW) is True


def test_ticker_validity_defaults_to_the_real_clock(store):
    store.set_ticker_valid("NEWQ", False, checked=NOW + timedelta(days=36500))  # far in the future
    assert store.ticker_valid("NEWQ") is False


# --- opportunities -------------------------------------------------------------------------------------------------


def test_add_opportunity_sets_the_id_and_round_trips_exactly(store):
    opp = make_opportunity()
    saved = store.add_opportunity(opp)
    assert saved.id is not None and opp.id is None
    assert saved == replace(opp, id=saved.id)
    assert store.last_opportunity("amd") == saved
    assert store.last_opportunity("NVDA") is None


def test_opportunities_filters_and_newest_first(store):
    a = store.add_opportunity(make_opportunity(created=NOW - timedelta(days=3), score=80.0))
    b = store.add_opportunity(make_opportunity(ticker="NVDA", created=NOW - timedelta(days=1), score=55.0))
    c = store.add_opportunity(make_opportunity(created=NOW, score=70.5))
    d = store.add_opportunity(make_opportunity(created=NOW, score=60.0))  # same time: the later insert first

    assert [o.id for o in store.opportunities()] == [d.id, c.id, b.id, a.id]
    assert [o.id for o in store.opportunities(since=NOW - timedelta(days=2))] == [d.id, c.id, b.id]
    assert [o.id for o in store.opportunities(ticker="amd")] == [d.id, c.id, a.id]
    assert [o.id for o in store.opportunities(min_score=65)] == [c.id, a.id]
    assert [o.id for o in store.opportunities(ticker="AMD", min_score=65, limit=1)] == [c.id]
    assert store.last_opportunity("AMD") == d


def test_mark_notified_and_unnotified(store):
    first = store.add_opportunity(make_opportunity(created=NOW - timedelta(hours=2)))
    second = store.add_opportunity(make_opportunity(created=NOW - timedelta(hours=1)))
    third = store.add_opportunity(make_opportunity(created=NOW))
    assert store.unnotified() == [third, second, first]
    store.mark_notified([first.id, third.id], when=NOW)
    assert store.unnotified() == [second]
    store.mark_notified([], when=NOW)
    assert store.unnotified() == [second]
    assert store.unnotified(since=NOW - timedelta(minutes=30)) == []
    assert store.unnotified(since=NOW - timedelta(hours=1)) == [second]


def test_analysis_failures_count_up_and_clear(store):
    assert store.analysis_failures("amd") is None
    assert store.record_analysis_failure("amd", when=NOW, error="bad JSON") == 1
    assert store.record_analysis_failure("AMD", when=NOW + timedelta(hours=1), error="x" * 2000) == 2
    assert store.analysis_failures("AMD") == (2, NOW + timedelta(hours=1))
    assert store.analysis_failures("NVDA") is None
    store.clear_analysis_failures("amd")
    assert store.analysis_failures("AMD") is None


# --- prune ---------------------------------------------------------------------------------------------------------


def test_prune_deletes_old_articles_and_their_impacts_but_keeps_opportunities(store):
    old = article("Old story", hours_ago=24 * 40, fetched=NOW - timedelta(days=40))
    stale_but_new_to_us = article("Old item still in a feed", hours_ago=24 * 40)  # fetched just now
    recent = article("Recent story", hours_ago=1)
    add(store, old, stale_but_new_to_us, recent, now=NOW)
    store.record_triage([old.id, recent.id], [make_impact(article_id=old.id), make_impact(article_id=recent.id)])
    opp = store.add_opportunity(make_opportunity(created=NOW - timedelta(days=60), article_ids=[old.id]))

    assert store.prune(older_than=NOW - timedelta(days=30)) == 1

    assert store.get_articles([old.id, stale_but_new_to_us.id, recent.id]) == [stale_but_new_to_us, recent]
    remaining = store._conn.execute("SELECT article_id FROM impacts").fetchall()
    assert [row[0] for row in remaining] == [recent.id]
    assert store.opportunities() == [opp]
    assert store.prune(older_than=NOW - timedelta(days=30)) == 0


# --- threads -------------------------------------------------------------------------------------------------------


def test_concurrent_writers_from_threads(store):
    def worker(n: int) -> None:
        stories = [article(f"Thread {n} story {i}", hours_ago=1) for i in range(20)]
        add(store, *stories)
        store.record_triage([s.id for s in stories], [make_impact(article_id=s.id) for s in stories])
        store.add_opportunity(make_opportunity(score=float(n)))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(store.news(NOW - timedelta(days=1))) == 160
    assert len(store.recent_impacts(NOW - timedelta(days=1))) == 160
    assert len(store.opportunities()) == 8


def test_a_version_1_database_gets_the_alerted_column(tmp_path):
    """Opportunities notified before `alerted` existed count as sent (thesis changes and repeats compare with them)."""
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE opportunities (id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, created TEXT NOT NULL,"
        " score REAL NOT NULL, data TEXT NOT NULL, notified TEXT); PRAGMA user_version = 1;"
    )
    data = make_opportunity().to_dict()
    data.pop("id")
    conn.execute(
        "INSERT INTO opportunities (ticker, created, score, data, notified) VALUES (?, ?, ?, ?, ?)",
        ("AMD", NOW.isoformat(timespec="microseconds"), 72.4, json.dumps(data), NOW.isoformat(timespec="microseconds")),
    )
    conn.commit()
    conn.close()

    with Store(path) as store:
        assert store.last_alerted("AMD") is not None
        assert store._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 6


def test_a_version_4_database_forgets_its_symbol_lookups(tmp_path):
    """Regression (live): version 4's looser name test stored HES -> HESM (Hess Midstream LP) and CS -> DHY (a bond
    fund); those kept moving Hess and Credit Suisse news to them for 7 days after an upgrade."""
    path = tmp_path / "v4.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE symbol_lookups (ticker TEXT NOT NULL, query TEXT NOT NULL, resolved TEXT, name TEXT,"
        " checked TEXT NOT NULL, PRIMARY KEY (ticker, query)); PRAGMA user_version = 4;"
    )
    conn.execute(
        "INSERT INTO symbol_lookups VALUES (?, ?, ?, ?, ?)",
        ("HES", "Hess", "HESM", "Hess Midstream LP", NOW.isoformat(timespec="microseconds")),
    )
    conn.commit()
    conn.close()

    with Store(path) as store:
        assert store.resolved_symbol("HES", now=NOW) is None
        assert store.symbol_lookup("HES", "Hess", now=NOW) is None
        store.save_symbol_lookup("MYTIL.AT", "Metlen", "MTLN.AT", "Metlen Energy & Metals PLC", checked=NOW)
    with Store(path) as store:  # opening a version 5 database again keeps its lookups
        assert store.resolved_symbol("MYTIL.AT", now=NOW) == ("MTLN.AT", "Metlen Energy & Metals PLC", "Metlen")


# --- model use and system notices ----------------------------------------------------------------------------------


def test_model_calls_are_totalled_per_step_and_model(store):
    def call(hours_ago, step, model, ticker=None, tokens=(100, 20)):
        store.record_model_call(
            when=NOW - timedelta(hours=hours_ago),
            step=step,
            model=model,
            ticker=ticker,
            input_tokens=tokens[0],
            output_tokens=tokens[1],
        )

    call(30, "triage", "gpt-5-mini")  # yesterday: left out below
    call(2, "triage", "gpt-5-mini")
    call(1, "triage", "gpt-5-mini", tokens=(1_200, 80))
    call(1, "analysis", "gpt-5", "amd", tokens=(3_500, 900))
    call(1, "analysis", "gpt-5", "AMD", tokens=(4_000, None))  # the corrective retry of the same analysis
    call(0.5, "analysis", "gpt-5", "NVDA")

    usage = store.model_usage(since=NOW - timedelta(hours=24))
    assert [(row.step, row.model, row.calls, row.input_tokens, row.output_tokens, row.unmetered) for row in usage] == [
        ("triage", "gpt-5-mini", 2, 1_300, 100, 0),
        ("analysis", "gpt-5", 3, 7_600, 920, 1),
    ]
    # Analyses count once per ticker and time, however many calls they took.
    assert store.analyses_since(NOW - timedelta(hours=24)) == 2
    assert store.analyses_since(NOW - timedelta(minutes=45)) == 1
    assert store.model_usage(since=NOW + timedelta(hours=1)) == []

    store.prune(older_than=NOW - timedelta(hours=24))
    assert sum(row.calls for row in store.model_usage(since=NOW - timedelta(days=10))) == 5


def test_notice_times_and_failure_streaks_are_kept(store):
    assert store.notice_times("stopped") == (None, None)
    store.record_notice("stopped", when=NOW, sent=True)
    store.record_notice("stopped", when=NOW + timedelta(hours=13), sent=False)  # no channel took the next one
    assert store.notice_times("stopped") == (NOW + timedelta(hours=13), NOW)

    assert [store.bump_streak("feeds_failing") for _ in range(3)] == [1, 2, 3]
    store.reset_streak("feeds_failing")
    assert store.bump_streak("feeds_failing") == 1
    store.reset_streak("never_seen")  # nothing to reset: fine


# --- version 6: per-recipient alert state, app state, cycles, the website's tables -------------------------------

FIXTURES = Path(__file__).parent / "fixtures"


def test_a_version_5_database_is_upgraded_and_keeps_its_alert_state(tmp_path):
    """scanner_v5.sql was dumped from a database made by the Store before version 6: AMD sent, NVDA handled without
    sending (a repeat), BA still waiting. The command line's marks become the "default" recipient's."""
    path = tmp_path / "v5.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript((FIXTURES / "scanner_v5.sql").read_text(encoding="utf-8"))
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
    conn.close()

    with Store(path) as store:
        assert store._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 6
        tables = {row[0] for row in store._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {
            "alert_deliveries", "users", "invites", "sessions", "password_tokens", "login_attempts", "jobs",
            "app_state", "cycles",
        } <= tables  # fmt: skip
        amd, nvda, ba = sorted(store.opportunities(), key=lambda opp: opp.id)
        assert (amd.ticker, nvda.ticker, ba.ticker) == ("AMD", "NVDA", "BA")
        assert store.unnotified() == [ba]
        assert store.last_alerted("AMD") == amd and store.last_alerted("NVDA") is None
        kinds = {(row["ticker"], row["kind"], row["sent"]) for row in store.deliveries(recipient=DEFAULT_RECIPIENT)}
        assert kinds == {("AMD", "alert", True), ("NVDA", "handled", False)}
        # What the command line already handled is handled for everybody; the waiting one waits for users too.
        assert store.unnotified(recipient="user:1") == [ba]
        assert store.last_alerted("AMD", recipient="user:1") is None
        # The rest of the data is untouched.
        assert store.feed_state("marketwatch").etag == '"v1"'
        assert len(store.news(NOW - timedelta(days=1))) == 1
        assert store.notice_times("stopped")[1] == NOW - timedelta(days=1)
    with Store(path) as store:  # opening it again changes nothing
        assert len(store.deliveries()) == 2 and store.unnotified() == [ba]


def test_deliveries_are_kept_per_recipient(store):
    first = store.add_opportunity(make_opportunity(created=NOW - timedelta(hours=2)))
    second = store.add_opportunity(make_opportunity(created=NOW - timedelta(hours=1)))
    other = store.add_opportunity(make_opportunity(ticker="NVDA", created=NOW))
    store.record_deliveries("user:1", [first.id], "alert", when=NOW, sent=True)
    store.record_deliveries("user:2", [first.id], "alert", when=NOW, sent=False, detail="email: refused")
    store.record_deliveries("user:2", [second.id], "handled", when=NOW, sent=False)

    assert store.unnotified(recipient="user:1") == [other, second]
    assert store.unnotified(recipient="user:2") == [other, first]  # a failed attempt stays waiting
    assert store.unnotified() == [other, second, first]  # "default" has decided nothing yet
    assert store.last_alerted("AMD", recipient="user:1") == first
    assert store.last_alerted("AMD", recipient="user:2") is None
    assert store.last_alerted("AMD", recipient="user:1", before=first.created) is None
    [failed] = store.deliveries(recipient="user:2", opportunity_id=first.id)
    assert failed == {
        "recipient": "user:2",
        "opportunity_id": first.id,
        "ticker": "AMD",
        "kind": "alert",
        "created": NOW,
        "sent": False,
        "detail": "email: refused",
    }
    store.record_deliveries("user:2", [first.id], "alert", when=NOW + timedelta(minutes=5), sent=True)  # retried
    assert store.deliveries(recipient="user:2", opportunity_id=first.id)[0]["sent"] is True
    assert store.unnotified(recipient="user:2") == [other]
    store.record_deliveries("user:3", [other.id], "thesis", when=NOW, sent=True)
    assert store.last_alerted("NVDA", recipient="user:3") == other  # a thesis change counts as alerted
    assert len(store.deliveries(limit=2)) == 2
    with pytest.raises(ValueError, match="Unknown delivery kind"):
        store.record_deliveries("user:1", [first.id], "email", when=NOW, sent=True)


def test_mark_notified_handles_an_opportunity_for_everybody(store):
    opp = store.add_opportunity(make_opportunity())
    store.mark_notified([opp.id], when=NOW, sent=False)
    assert store.unnotified() == [] and store.unnotified(recipient="user:1") == []
    assert store.last_alerted("AMD") is None
    shown = store.add_opportunity(make_opportunity(created=NOW + timedelta(hours=1)))
    store.mark_notified([shown.id], when=NOW, sent=True)  # read in `analyze`: alerted to the command line's user
    assert store.last_alerted("AMD") == shown and store.last_alerted("AMD", recipient="user:1") is None
    assert store.get_opportunity(shown.id) == shown and store.get_opportunity(999) is None


def test_app_state_and_the_pause_flag(store):
    assert store.get_state("x") is None and store.get_state("x", "fallback") == "fallback"
    store.set_state("x", "1")
    assert store.get_state("x") == "1"
    store.set_state("x", None)
    assert store.get_state("x") is None
    assert not store.scanner_paused()
    store.set_scanner_paused(True)
    assert store.scanner_paused() and store.get_state(SCANNER_PAUSED) == "1"
    store.set_scanner_paused(False)
    assert not store.scanner_paused()


def test_cycles_are_recorded_newest_first_and_only_the_newest_kept(store):
    for n in range(5):
        store.record_cycle(
            started=NOW + timedelta(minutes=5 * n),
            finished=NOW + timedelta(minutes=5 * n, seconds=30),
            summary=f"Cycle {n}",
            notes=[f"note {n}", "Ελληνικά"],
            stats={"feeds_ok": n, "alerts": 1},
            ok=n != 3,
            keep=3,
        )
    cycles = store.cycles()
    assert [cycle.summary for cycle in cycles] == ["Cycle 4", "Cycle 3", "Cycle 2"]
    newest = cycles[0]
    assert newest.started == NOW + timedelta(minutes=20) and newest.finished == newest.started + timedelta(seconds=30)
    assert newest.notes == ["note 4", "Ελληνικά"] and newest.stats == {"feeds_ok": 4, "alerts": 1} and newest.ok
    assert store.last_cycle().summary == "Cycle 4"
    assert store.last_cycle(ok=False).summary == "Cycle 3"
    assert [c.summary for c in store.cycles(limit=1)] == ["Cycle 4"]
    assert [c.summary for c in store.cycles(since=NOW + timedelta(minutes=15))] == ["Cycle 4", "Cycle 3"]
    with store.transaction() as conn:  # a damaged record still loads
        conn.execute("UPDATE cycles SET notes = 'oops', stats = '[1]', finished = NULL WHERE summary = 'Cycle 2'")
    damaged = store.cycles()[-1]
    assert (damaged.notes, damaged.stats, damaged.finished) == ([], {}, None)


def test_prune_clears_old_website_records_but_keeps_sent_alerts(store):
    old, recent = NOW - timedelta(days=40), NOW - timedelta(hours=1)
    opp = store.add_opportunity(make_opportunity(created=old))
    store.record_deliveries("user:1", [opp.id], "alert", when=old, sent=True)
    store.record_deliveries("user:2", [opp.id], "handled", when=old, sent=False)
    with store.transaction() as conn:
        conn.execute("INSERT INTO sessions VALUES ('old', 1, 'c', ?, ?, ?, NULL, NULL)", (_t(old), _t(old), _t(old)))
        conn.execute(
            "INSERT INTO sessions VALUES ('live', 1, 'c', ?, ?, ?, NULL, NULL)", (_t(recent), _t(NOW), _t(NOW))
        )
        conn.execute("INSERT INTO login_attempts VALUES ('ip:1', ?), ('ip:1', ?)", (_t(old), _t(recent)))
        conn.execute(
            "INSERT INTO jobs (user_id, kind, ticker, status, created) VALUES (1, 'analyze', 'A', 'done', ?)",
            (_t(old),),
        )
        conn.execute(
            "INSERT INTO jobs (user_id, kind, ticker, status, created) VALUES (1, 'analyze', 'B', 'done', ?)",
            (_t(recent),),
        )
        conn.execute(
            "INSERT INTO invites (token_hash, role, created, expires) VALUES ('i', 'member', ?, ?)", (_t(old), _t(old))
        )
        conn.execute("INSERT INTO password_tokens VALUES ('p', 1, 'reset', ?, ?, NULL)", (_t(old), _t(old)))

    store.prune(older_than=NOW - timedelta(days=30))

    assert [row["kind"] for row in store.deliveries()] == ["alert"]
    assert [row[0] for row in store.query("SELECT token_hash FROM sessions")] == ["live"]
    assert store.query("SELECT COUNT(*) FROM login_attempts")[0][0] == 1
    assert [row[0] for row in store.query("SELECT ticker FROM jobs")] == ["B"]
    assert store.query("SELECT COUNT(*) FROM invites")[0][0] == 0
    assert store.query("SELECT COUNT(*) FROM password_tokens")[0][0] == 0


def _t(dt):
    return dt.isoformat(timespec="microseconds")
