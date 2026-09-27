import sqlite3
import threading
from dataclasses import replace
from datetime import timedelta

import pytest
from conftest import NOW, make_article, make_impact, make_opportunity

from dip_scanner.feeds import FeedState, title_key
from dip_scanner.store import Store


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
    first = article("Fed signals more cuts", source="a")
    same_id = article("Fed signals more cuts", source="b")  # same link -> same id
    same_title = article("Fed signals more cuts", source="c", link="https://example.com/other")
    assert add(store, first, same_id, same_title) == [first]


def test_the_same_title_from_the_same_source_with_a_new_link_is_a_new_article(store):
    # SEC 8-K titles repeat for every filing of a company; each filing has its own link (and id).
    title = "8-K - Advanced Micro Devices Inc (0000002488) (Filer)"
    first = article(title, source="sec-8k-filings", link="https://www.sec.gov/Archives/edgar/data/2488/1-index.htm")
    second = article(title, source="sec-8k-filings", link="https://www.sec.gov/Archives/edgar/data/2488/2-index.htm")
    assert add(store, first) == [first]
    assert add(store, second, now=NOW + timedelta(hours=5)) == [second]
    # ...while the same headline from another source is still a duplicate.
    elsewhere = article(title, source="google", link="https://example.com/amd-8k")
    assert add(store, elsewhere, now=NOW + timedelta(hours=6)) == []


def test_too_old_articles_are_stored_as_skipped_and_never_triaged(store):
    fresh, stale = article("Fresh news", hours_ago=2), article("Old news", hours_ago=30)
    assert add(store, fresh, stale, max_age_hours=24) == [fresh]
    assert store.article_status(stale.id) == ("skipped", 0)
    assert store.pending_triage(10, max_attempts=3) == [fresh]
    assert add(store, stale) == []  # already stored, so still not new


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
