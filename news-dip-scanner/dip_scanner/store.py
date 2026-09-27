"""SQLite persistence for feed state, articles, impacts, ticker validity, symbol lookups and opportunities.

One connection per Store, shared between threads (check_same_thread=False) and serialised with a lock. WAL mode
lets a second process (e.g. `dip-scanner news` while `watch` runs) read while the scanner writes. Timestamps are
stored as ISO 8601 UTC text with microseconds (always the same width), so SQL can compare them as strings.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Collection, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .feeds import FeedState
from .models import Article, Impact, ModelUsage, Opportunity, from_iso, utc

log = logging.getLogger(__name__)

# A headline seen from any source within this window counts as the same story.
TITLE_DEDUP_WINDOW = timedelta(hours=72)
# Only headlines of at least this many words are matched by title: short ones ("Profit warning", "Trading update",
# "Transaction in own shares") are formulaic, and different companies publish them.
MIN_DEDUP_TITLE_WORDS = 5
# A ticker without prices is re-checked after this long (it may have been listed or renamed since).
INVALID_TICKER_TTL = timedelta(days=7)
# A search for the new symbol of a ticker without prices (symbols.py) is repeated after this long.
SYMBOL_LOOKUP_TTL = timedelta(days=7)
ARTICLE_STATUSES = ("pending", "done", "failed", "skipped")
_MAX_PARAMS = 500  # stay well under SQLite's bound-parameter limit in IN (...) lists

# 2: opportunities.alerted; 3: model_calls and system_notices; 4: symbol_lookups; 5: symbol_lookups.unconfirmed, and
# the lookups of version 4 forgotten (its looser name test took HESM for Hess and a bond fund for Credit Suisse).
SCHEMA_VERSION = 5
_SCHEMA = """
CREATE TABLE IF NOT EXISTS feeds (
    key TEXT PRIMARY KEY,
    etag TEXT,
    last_modified TEXT,
    last_fetch TEXT,
    last_status INTEGER,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS articles (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_name TEXT NOT NULL,
    title TEXT NOT NULL,
    link TEXT NOT NULL,
    summary TEXT NOT NULL,
    published TEXT NOT NULL,
    fetched TEXT NOT NULL,
    title_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'failed', 'skipped')),
    attempts INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS articles_published ON articles (published);
CREATE INDEX IF NOT EXISTS articles_title_key ON articles (title_key);
CREATE INDEX IF NOT EXISTS articles_status ON articles (status, published);
CREATE TABLE IF NOT EXISTS impacts (
    article_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    company TEXT NOT NULL,
    relation TEXT NOT NULL,
    direction TEXT NOT NULL,
    magnitude INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    rationale TEXT NOT NULL,
    PRIMARY KEY (article_id, ticker)
);
CREATE INDEX IF NOT EXISTS impacts_ticker ON impacts (ticker);
CREATE TABLE IF NOT EXISTS tickers (
    ticker TEXT PRIMARY KEY,
    valid INTEGER NOT NULL,
    checked TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS symbol_lookups (
    ticker TEXT NOT NULL,    -- a symbol without prices
    query TEXT NOT NULL,     -- the company name searched for
    resolved TEXT,           -- the symbol found, NULL when nothing matched
    name TEXT,               -- Yahoo's name for it
    checked TEXT NOT NULL,
    unconfirmed INTEGER NOT NULL DEFAULT 0,  -- 1: found by a longer name; taken once a story names it
    PRIMARY KEY (ticker, query)
);
CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    created TEXT NOT NULL,
    score REAL NOT NULL,
    data TEXT NOT NULL,
    notified TEXT,  -- handled: sent, or deliberately not sent (a repeat, superseded, --no-notify, read in `analyze`)
    alerted TEXT    -- actually delivered to the user (an alert, a thesis change, or shown by `analyze`)
);
CREATE INDEX IF NOT EXISTS opportunities_ticker_created ON opportunities (ticker, created);
CREATE INDEX IF NOT EXISTS opportunities_created ON opportunities (created);
CREATE TABLE IF NOT EXISTS analysis_failures (
    ticker TEXT PRIMARY KEY,
    failures INTEGER NOT NULL,
    last_failure TEXT NOT NULL,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS model_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created TEXT NOT NULL,   -- the time of the cycle (or manual analysis) that made the call
    step TEXT NOT NULL,      -- 'triage' or 'analysis'
    model TEXT NOT NULL,
    ticker TEXT,             -- the analysed ticker (analysis only)
    input_tokens INTEGER,    -- NULL when the service didn't report them
    output_tokens INTEGER
);
CREATE INDEX IF NOT EXISTS model_calls_created ON model_calls (created);
CREATE TABLE IF NOT EXISTS system_notices (
    kind TEXT PRIMARY KEY,
    streak INTEGER NOT NULL DEFAULT 0,  -- cycles in a row with this problem
    last_attempt TEXT,                  -- the last time a notice of this kind was tried
    last_sent TEXT                      -- the last time one reached at least one channel
);
"""

_ARTICLE_COLUMNS = "id, source, source_name, title, link, summary, published, fetched, title_key"
_IMPACT_COLUMNS = "article_id, ticker, company, relation, direction, magnitude, event_type, rationale"


def _ts(dt: datetime) -> str:
    """Fixed-width ISO 8601 UTC text, e.g. 2026-09-25T15:00:00.000000+00:00."""
    return utc(dt).isoformat(timespec="microseconds")


def _chunks(items: Sequence, size: int = _MAX_PARAMS) -> Iterator[Sequence]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _placeholders(count: int) -> str:
    return ", ".join("?" * count)


class Store:
    """The scanner's database (stdlib sqlite3, WAL mode). Timestamps are stored as ISO 8601 UTC strings."""

    def __init__(self, path: Path) -> None:
        """Open (and create if needed) the database at path, including its parent folder and schema.

        ":memory:" gives a throwaway in-memory database (handy in tests).
        """
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        conn: sqlite3.Connection | None = None
        try:
            # timeout: wait up to 30 s for another process (e.g. a second CLI command) holding the write lock.
            conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            with conn:
                if conn.execute("PRAGMA user_version").fetchone()[0] < 5:
                    conn.execute("DROP TABLE IF EXISTS symbol_lookups")  # see SCHEMA_VERSION
                conn.executescript(_SCHEMA)
                columns = {row[1] for row in conn.execute("PRAGMA table_info(opportunities)")}
                if "alerted" not in columns:  # a version 1 database: what was notified then was sent
                    conn.execute("ALTER TABLE opportunities ADD COLUMN alerted TEXT")
                    conn.execute("UPDATE opportunities SET alerted = notified")
                if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
                    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        except sqlite3.DatabaseError as exc:
            if conn is not None:
                conn.close()
            raise sqlite3.DatabaseError(f"Couldn't open the scanner database {path}: {exc}") from exc
        self._conn = conn

    def close(self) -> None:
        """Close the connection (safe to call twice)."""
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """One locked transaction: committed on success, rolled back on error."""
        with self._lock, self._conn:
            yield self._conn

    def _query(self, sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    # --- feeds ---

    def feed_state(self, key: str) -> FeedState | None:
        """The saved ETag/Last-Modified of a feed, or None if it was never fetched."""
        rows = self._query("SELECT etag, last_modified FROM feeds WHERE key = ?", (key,))
        if not rows:
            return None
        return FeedState(etag=rows[0]["etag"], last_modified=rows[0]["last_modified"])

    def save_feed_state(
        self, key: str, state: FeedState, *, status: int | None, error: str | None, fetched: datetime
    ) -> None:
        """Remember the outcome of fetching a feed."""
        with self._write() as conn:
            conn.execute(
                """
                INSERT INTO feeds (key, etag, last_modified, last_fetch, last_status, last_error)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (key) DO UPDATE SET
                    etag = excluded.etag, last_modified = excluded.last_modified, last_fetch = excluded.last_fetch,
                    last_status = excluded.last_status, last_error = excluded.last_error
                """,
                (key, state.etag, state.last_modified, _ts(fetched), status, error),
            )

    def feed_health(self) -> list[dict]:
        """One dict per fetched feed, by key: key, last_fetch (UTC datetime), last_status, last_error, articles.

        articles counts the stored articles from that feed (pruned ones are gone, so it covers the retention window).
        """
        rows = self._query(
            """
            SELECT f.key, f.last_fetch, f.last_status, f.last_error,
                   (SELECT COUNT(*) FROM articles a WHERE a.source = f.key) AS articles
            FROM feeds f ORDER BY f.key
            """
        )
        return [
            {
                "key": row["key"],
                "last_fetch": from_iso(row["last_fetch"]) if row["last_fetch"] else None,
                "last_status": row["last_status"],
                "last_error": row["last_error"],
                "articles": row["articles"],
            }
            for row in rows
        ]

    # --- articles ---

    def add_articles(
        self,
        articles: list[Article],
        *,
        max_age_hours: float,
        now: datetime,
        same_source_titles: Collection[str] = (),
    ) -> list[Article]:
        """Insert unseen articles; return the new ones young enough to triage (older ones are stored as skipped).

        An article is not new when its id is already stored, or when an article with the same title_key (at least
        MIN_DEDUP_TITLE_WORDS words) was published or fetched in the last 72 hours from any source, this one
        included (the same story again, e.g. Google News listing one headline from several outlets); those aren't
        stored. Stored copies too old to triage (status 'skipped') don't count. Sources in same_source_titles are
        feeds with formulaic titles, like the SEC's "8-K - APPLE INC (0000320193) (Filer)" for every new filing of a
        company: within such a source only the id (the canonical link) decides.
        New articles published more than max_age_hours ago are stored with status 'skipped' and not returned.
        An article another process stores at the same moment is skipped, not an error.
        """
        now = utc(now)
        too_old = now - timedelta(hours=max_age_hours)
        window = _ts(now - TITLE_DEDUP_WINDOW)
        exempt = set(same_source_titles)
        fresh: list[Article] = []
        with self._write() as conn:
            for article in articles:
                if conn.execute("SELECT 1 FROM articles WHERE id = ?", (article.id,)).fetchone():
                    continue
                if article.title_key and len(article.title_key.split()) >= MIN_DEDUP_TITLE_WORDS:
                    sql = (
                        "SELECT 1 FROM articles WHERE title_key = ? AND status != 'skipped' "
                        "AND (published >= ? OR fetched >= ?)"
                    )
                    params: tuple = (article.title_key, window, window)
                    if article.source in exempt:
                        sql, params = sql + " AND source != ?", (*params, article.source)
                    if conn.execute(sql + " LIMIT 1", params).fetchone():
                        continue
                status = "pending" if utc(article.published) >= too_old else "skipped"
                inserted = conn.execute(
                    f"INSERT OR IGNORE INTO articles ({_ARTICLE_COLUMNS}, status) VALUES ({_placeholders(10)})",
                    (*_article_values(article), status),
                )
                if inserted.rowcount == 1 and status == "pending":
                    fresh.append(article)
        return fresh

    def skip_stale_pending(self, before: datetime) -> int:
        """Mark pending articles published before `before` as 'skipped' (too old to triage); returns how many.

        Articles stay pending while the model is unavailable; after a long outage the backlog would otherwise be
        triaged oldest first, long after it matters.
        """
        with self._write() as conn:
            return conn.execute(
                "UPDATE articles SET status = 'skipped' WHERE status = 'pending' AND published < ?",
                (_ts(before),),
            ).rowcount

    def pending_triage(self, limit: int, *, max_attempts: int) -> list[Article]:
        """Articles waiting for triage (status 'pending', fewer than max_attempts tries), oldest first."""
        rows = self._query(
            f"""
            SELECT {_ARTICLE_COLUMNS} FROM articles
            WHERE status = 'pending' AND attempts < ?
            ORDER BY published, fetched, id LIMIT ?
            """,
            (max_attempts, max(0, limit)),
        )
        return [_article(row) for row in rows]

    def record_triage(self, article_ids: list[str], impacts: list[Impact]) -> None:
        """Mark articles as triaged ('done') and store their impacts (replacing any from an earlier triage)."""
        ids = list(dict.fromkeys(article_ids))
        with self._write() as conn:
            for chunk in _chunks(ids):
                marks = _placeholders(len(chunk))
                conn.execute(f"DELETE FROM impacts WHERE article_id IN ({marks})", tuple(chunk))
                conn.execute(f"UPDATE articles SET status = 'done' WHERE id IN ({marks})", tuple(chunk))
            conn.executemany(
                f"INSERT OR REPLACE INTO impacts ({_IMPACT_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [_impact_values(impact) for impact in impacts],
            )

    def record_triage_failure(self, article_ids: list[str], *, max_attempts: int) -> None:
        """Count a failed triage attempt on pending articles; they become 'failed' after max_attempts."""
        ids = list(dict.fromkeys(article_ids))
        with self._write() as conn:
            for chunk in _chunks(ids):
                conn.execute(
                    f"""
                    UPDATE articles SET
                        attempts = attempts + 1,
                        status = CASE WHEN attempts + 1 >= ? THEN 'failed' ELSE status END
                    WHERE status = 'pending' AND id IN ({_placeholders(len(chunk))})
                    """,
                    (max_attempts, *chunk),
                )

    def recent_impacts(self, since: datetime) -> list[tuple[Impact, Article]]:
        """Impacts of articles published since the given time, newest article first."""
        rows = self._query(
            f"""
            SELECT {_prefixed("a", _ARTICLE_COLUMNS)}, {_prefixed("i", _IMPACT_COLUMNS, skip="article_id")}
            FROM impacts i JOIN articles a ON a.id = i.article_id
            WHERE a.published >= ?
            ORDER BY a.published DESC, a.id, i.rowid
            """,
            (_ts(since),),
        )
        pairs: list[tuple[Impact, Article]] = []
        articles: dict[str, Article] = {}
        for row in rows:
            article = articles.get(row["id"])
            if article is None:
                article = articles[row["id"]] = _article(row)
            pairs.append((_impact(row, article_id=article.id), article))
        return pairs

    def news(
        self, since: datetime, ticker: str | None = None, *, also: Collection[str] = ()
    ) -> list[tuple[Article, list[Impact]]]:
        """Articles published since the given time with their impacts, newest first.

        With a ticker, only articles with an impact on that ticker, or on one of the symbols in also (whose news
        belongs to it: an old symbol, a preferred listing's other symbol), are returned, each with one such impact
        (its own first) relabelled as ticker.
        """
        params: list = [_ts(since)]
        where = "a.published >= ?"
        wanted: list[str] = []
        if ticker:
            symbol = ticker.strip().upper()
            wanted = list(dict.fromkeys([symbol, *(item.strip().upper() for item in also)]))
            where += f" AND a.id IN (SELECT article_id FROM impacts WHERE ticker IN ({_placeholders(len(wanted))}))"
            params += wanted
        article_rows = self._query(
            f"SELECT {_prefixed('a', _ARTICLE_COLUMNS)} FROM articles a WHERE {where} ORDER BY a.published DESC, a.id",
            params,
        )
        articles = [_article(row) for row in article_rows]
        impacts = self._impacts_for([article.id for article in articles])
        result = []
        for article in articles:
            found = impacts.get(article.id, [])
            if wanted:
                mine = sorted((i for i in found if i.ticker in wanted), key=lambda impact: impact.ticker != symbol)
                found = [replace(mine[0], ticker=symbol)] if mine else []
            result.append((article, found))
        return result

    def get_articles(self, ids: list[str]) -> list[Article]:
        """The stored articles with these ids, in the order asked (unknown or pruned ids are left out)."""
        found: dict[str, Article] = {}
        for chunk in _chunks(list(dict.fromkeys(ids))):
            rows = self._query(
                f"SELECT {_ARTICLE_COLUMNS} FROM articles WHERE id IN ({_placeholders(len(chunk))})", chunk
            )
            found.update((row["id"], _article(row)) for row in rows)
        return [found[article_id] for article_id in dict.fromkeys(ids) if article_id in found]

    def article_status(self, article_id: str) -> tuple[str, int] | None:
        """(status, attempts) of a stored article, or None. Mostly for diagnostics and tests."""
        rows = self._query("SELECT status, attempts FROM articles WHERE id = ?", (article_id,))
        return (rows[0]["status"], rows[0]["attempts"]) if rows else None

    def _impacts_for(self, article_ids: list[str]) -> dict[str, list[Impact]]:
        impacts: dict[str, list[Impact]] = {}
        for chunk in _chunks(article_ids):
            rows = self._query(
                f"SELECT {_IMPACT_COLUMNS} FROM impacts WHERE article_id IN ({_placeholders(len(chunk))}) "
                "ORDER BY rowid",
                chunk,
            )
            for row in rows:
                impacts.setdefault(row["article_id"], []).append(_impact(row))
        return impacts

    # --- tickers ---

    def ticker_valid(self, ticker: str, *, now: datetime | None = None) -> bool | None:
        """Whether the ticker is known to have prices (None = not checked, or an invalid mark that expired).

        now defaults to the current time; pass the cycle's time to keep results deterministic.
        """
        rows = self._query("SELECT valid, checked FROM tickers WHERE ticker = ?", (ticker.strip().upper(),))
        if not rows:
            return None
        if rows[0]["valid"]:
            return True
        now = utc(now) if now is not None else datetime.now(UTC)
        if from_iso(rows[0]["checked"]) <= now - INVALID_TICKER_TTL:
            return None
        return False

    def set_ticker_valid(self, ticker: str, valid: bool, *, checked: datetime) -> None:
        """Remember whether a ticker has prices. Invalid marks expire after 7 days."""
        with self._write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO tickers (ticker, valid, checked) VALUES (?, ?, ?)",
                (ticker.strip().upper(), int(bool(valid)), _ts(checked)),
            )

    # --- symbol lookups (symbols.py) ---

    def symbol_lookup(self, ticker: str, query: str, *, now: datetime) -> tuple[str | None, str | None, bool] | None:
        """(symbol found or None, its name, whether it is unconfirmed) of a search for query made for ticker in the
        last 7 days, else None. An unconfirmed symbol was found by a longer name (symbols.candidate) and only counts
        once a story names it."""
        rows = self._query(
            "SELECT resolved, name, unconfirmed FROM symbol_lookups WHERE ticker = ? AND query = ? AND checked > ?",
            (ticker.strip().upper(), query, _ts(utc(now) - SYMBOL_LOOKUP_TTL)),
        )
        return (rows[0]["resolved"], rows[0]["name"], bool(rows[0]["unconfirmed"])) if rows else None

    def save_symbol_lookup(
        self,
        ticker: str,
        query: str,
        resolved: str | None,
        name: str | None,
        *,
        checked: datetime,
        unconfirmed: bool = False,
    ) -> None:
        """Remember what a search for query found for ticker (resolved None: nothing matched)."""
        with self._write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO symbol_lookups (ticker, query, resolved, name, checked, unconfirmed) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    ticker.strip().upper(),
                    query,
                    resolved.strip().upper() if resolved else None,
                    name,
                    _ts(checked),
                    int(bool(resolved and unconfirmed)),
                ),
            )

    def confirm_symbol_lookup(self, ticker: str, query: str) -> None:
        """A story named the symbol found for ticker by query: from now on it counts (resolved_symbol)."""
        with self._write() as conn:
            conn.execute(
                "UPDATE symbol_lookups SET unconfirmed = 0 WHERE ticker = ? AND query = ?",
                (ticker.strip().upper(), query),
            )

    def resolved_symbol(self, ticker: str, *, now: datetime) -> tuple[str, str, str] | None:
        """(symbol, name, query) of the newest search in the last 7 days that found a replacement for ticker (an
        unconfirmed one doesn't count)."""
        rows = self._query(
            "SELECT resolved, name, query FROM symbol_lookups WHERE ticker = ? AND resolved IS NOT NULL "
            "AND unconfirmed = 0 AND checked > ? ORDER BY checked DESC LIMIT 1",
            (ticker.strip().upper(), _ts(utc(now) - SYMBOL_LOOKUP_TTL)),
        )
        if not rows:
            return None
        return rows[0]["resolved"], rows[0]["name"] or rows[0]["resolved"], rows[0]["query"]

    def renamed_to(self, symbol: str, *, now: datetime) -> list[str]:
        """The symbols whose news goes to symbol: those whose newest replacement of the last 7 days
        (resolved_symbol) is symbol, e.g. ["OPAP.AT"] for "ALWN.AT"."""
        symbol = symbol.strip().upper()
        rows = self._query(
            "SELECT DISTINCT ticker FROM symbol_lookups WHERE resolved = ? AND unconfirmed = 0 AND checked > ? "
            "ORDER BY ticker",
            (symbol, _ts(utc(now) - SYMBOL_LOOKUP_TTL)),
        )
        return [
            row["ticker"]
            for row in rows
            if (found := self.resolved_symbol(row["ticker"], now=now)) is not None and found[0] == symbol
        ]

    # --- opportunities ---

    def add_opportunity(self, opp: Opportunity) -> Opportunity:
        """Store an opportunity and return a copy with its id set."""
        data = opp.to_dict()
        data.pop("id", None)
        with self._write() as conn:
            cursor = conn.execute(
                "INSERT INTO opportunities (ticker, created, score, data) VALUES (?, ?, ?, ?)",
                (opp.ticker.strip().upper(), _ts(opp.created), float(opp.score), json.dumps(data)),
            )
            return replace(opp, id=cursor.lastrowid)

    def last_opportunity(self, ticker: str) -> Opportunity | None:
        """The newest opportunity stored for a ticker."""
        found = self.opportunities(ticker=ticker, limit=1)
        return found[0] if found else None

    def opportunities(
        self,
        *,
        since: datetime | None = None,
        ticker: str | None = None,
        min_score: float | None = None,
        limit: int | None = None,
    ) -> list[Opportunity]:
        """Stored opportunities, newest first, optionally filtered by creation time, ticker and minimum score."""
        where, params = ["1 = 1"], []
        if since is not None:
            where.append("created >= ?")
            params.append(_ts(since))
        if ticker:
            where.append("ticker = ?")
            params.append(ticker.strip().upper())
        if min_score is not None:
            where.append("score >= ?")
            params.append(float(min_score))
        sql = f"SELECT id, data FROM opportunities WHERE {' AND '.join(where)} ORDER BY created DESC, id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, limit))
        return [_opportunity(row) for row in self._query(sql, params)]

    def mark_notified(self, ids: list[int], *, when: datetime, sent: bool = True) -> None:
        """Record that these opportunities were handled: sent to the user (sent=True), or deliberately not sent
        (a repeat, superseded by a newer analysis, a --no-notify run). Either way they aren't retried."""
        with self._write() as conn:
            for chunk in _chunks(list(dict.fromkeys(ids))):
                conn.execute(
                    f"UPDATE opportunities SET notified = ?, alerted = COALESCE(?, alerted) "
                    f"WHERE id IN ({_placeholders(len(chunk))})",
                    (_ts(when), _ts(when) if sent else None, *chunk),
                )

    def last_alerted(self, ticker: str, *, before: datetime | None = None) -> Opportunity | None:
        """The newest opportunity of a ticker that was sent to the user (optionally only one created before)."""
        sql = "SELECT id, data FROM opportunities WHERE ticker = ? AND alerted IS NOT NULL"
        params: list = [ticker.strip().upper()]
        if before is not None:
            sql += " AND created < ?"
            params.append(_ts(before))
        rows = self._query(sql + " ORDER BY created DESC, id DESC LIMIT 1", params)
        return _opportunity(rows[0]) if rows else None

    def unnotified(self, *, since: datetime | None = None) -> list[Opportunity]:
        """Opportunities that were never sent as notifications (optionally only those created since), newest first."""
        sql, params = "SELECT id, data FROM opportunities WHERE notified IS NULL", []
        if since is not None:
            sql += " AND created >= ?"
            params.append(_ts(since))
        rows = self._query(sql + " ORDER BY created DESC, id DESC", params)
        return [_opportunity(row) for row in rows]

    # --- failed analyses (so one ticker the model keeps failing on doesn't cost a request every cycle) ---

    def record_analysis_failure(self, ticker: str, *, when: datetime, error: str) -> int:
        """Count a failed analysis of a ticker; returns how many times in a row it has failed."""
        with self._write() as conn:
            conn.execute(
                """
                INSERT INTO analysis_failures (ticker, failures, last_failure, last_error) VALUES (?, 1, ?, ?)
                ON CONFLICT (ticker) DO UPDATE SET
                    failures = failures + 1, last_failure = excluded.last_failure, last_error = excluded.last_error
                """,
                (ticker.strip().upper(), _ts(when), error[:500]),
            )
            row = conn.execute(
                "SELECT failures FROM analysis_failures WHERE ticker = ?", (ticker.strip().upper(),)
            ).fetchone()
        return int(row["failures"])

    def analysis_failures(self, ticker: str) -> tuple[int, datetime] | None:
        """(failures in a row, time of the last one) for a ticker, or None when its last analysis didn't fail."""
        rows = self._query(
            "SELECT failures, last_failure FROM analysis_failures WHERE ticker = ?", (ticker.strip().upper(),)
        )
        return (int(rows[0]["failures"]), from_iso(rows[0]["last_failure"])) if rows else None

    def clear_analysis_failures(self, ticker: str) -> None:
        """Forget a ticker's failed analyses (after one succeeds)."""
        with self._write() as conn:
            conn.execute("DELETE FROM analysis_failures WHERE ticker = ?", (ticker.strip().upper(),))

    # --- model use (token totals and the daily analysis limit) ---

    def record_model_call(
        self,
        *,
        when: datetime,
        step: str,
        model: str,
        ticker: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        """Remember one call the model service answered, with the tokens it reported (None when it didn't)."""
        with self._write() as conn:
            conn.execute(
                "INSERT INTO model_calls (created, step, model, ticker, input_tokens, output_tokens) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (_ts(when), step, model, ticker.strip().upper() if ticker else None, input_tokens, output_tokens),
            )

    def model_usage(self, *, since: datetime) -> list[ModelUsage]:
        """The model calls since the given time, totalled per step and model (triage first)."""
        rows = self._query(
            """
            SELECT step, model, COUNT(*) AS calls,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens, COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   SUM(input_tokens IS NULL OR output_tokens IS NULL) AS unmetered
            FROM model_calls WHERE created >= ?
            GROUP BY step, model ORDER BY step = 'analysis', step, model
            """,
            (_ts(since),),
        )
        return [
            ModelUsage(
                step=row["step"],
                model=row["model"],
                calls=row["calls"],
                input_tokens=row["input_tokens"],
                output_tokens=row["output_tokens"],
                unmetered=row["unmetered"],
            )
            for row in rows
        ]

    def analyses_since(self, since: datetime) -> int:
        """How many analyses the model answered since the given time (a corrective retry is part of its analysis,
        and a reply that turned out unusable counts too: it was paid for)."""
        rows = self._query(
            "SELECT COUNT(*) FROM (SELECT DISTINCT ticker, created FROM model_calls "
            "WHERE step = 'analysis' AND created >= ?)",
            (_ts(since),),
        )
        return int(rows[0][0])

    # --- system notices (see notices.py) ---

    def notice_times(self, kind: str) -> tuple[datetime | None, datetime | None]:
        """(last attempt, last successful send) of a kind of system notice."""
        rows = self._query("SELECT last_attempt, last_sent FROM system_notices WHERE kind = ?", (kind,))
        if not rows:
            return None, None
        return tuple(from_iso(value) if value else None for value in (rows[0]["last_attempt"], rows[0]["last_sent"]))

    def record_notice(self, kind: str, *, when: datetime, sent: bool) -> None:
        """Remember that a notice of this kind was tried at when, and whether any channel took it."""
        with self._write() as conn:
            conn.execute(
                """
                INSERT INTO system_notices (kind, last_attempt, last_sent) VALUES (?, ?, ?)
                ON CONFLICT (kind) DO UPDATE SET
                    last_attempt = excluded.last_attempt, last_sent = COALESCE(excluded.last_sent, last_sent)
                """,
                (kind, _ts(when), _ts(when) if sent else None),
            )

    def bump_streak(self, kind: str) -> int:
        """Count one more cycle in a row with this problem; returns the count."""
        with self._write() as conn:
            conn.execute(
                "INSERT INTO system_notices (kind, streak) VALUES (?, 1) "
                "ON CONFLICT (kind) DO UPDATE SET streak = streak + 1",
                (kind,),
            )
            return int(conn.execute("SELECT streak FROM system_notices WHERE kind = ?", (kind,)).fetchone()[0])

    def reset_streak(self, kind: str) -> None:
        """The problem is gone: the next occurrence starts counting from one again."""
        with self._write() as conn:
            conn.execute("UPDATE system_notices SET streak = 0 WHERE kind = ?", (kind,))

    def prune(self, *, older_than: datetime) -> int:
        """Delete articles (and their impacts) older than the given time; returns how many were deleted.

        An article goes once both its publication and our first sighting of it are older than older_than (so an
        old item still listed in a feed isn't re-inserted as new every day). Model call records and symbol lookups
        older than that go too. Opportunities are always kept.
        """
        cutoff = _ts(older_than)
        with self._write() as conn:
            old = "SELECT id FROM articles WHERE published < ? AND fetched < ?"
            conn.execute(f"DELETE FROM impacts WHERE article_id IN ({old})", (cutoff, cutoff))
            deleted = conn.execute("DELETE FROM articles WHERE published < ? AND fetched < ?", (cutoff, cutoff))
            count = deleted.rowcount
            conn.execute("DELETE FROM model_calls WHERE created < ?", (cutoff,))
            conn.execute("DELETE FROM symbol_lookups WHERE checked < ?", (cutoff,))
        if count:
            log.info("Pruned %d article(s) older than %s", count, cutoff)
        return count


# --- row conversion ------------------------------------------------------------------------------------------------


def _prefixed(alias: str, columns: str, *, skip: str | None = None) -> str:
    return ", ".join(f"{alias}.{name.strip()}" for name in columns.split(",") if name.strip() != skip)


def _article_values(article: Article) -> tuple:
    return (
        article.id,
        article.source,
        article.source_name,
        article.title,
        article.link,
        article.summary,
        _ts(article.published),
        _ts(article.fetched),
        article.title_key,
    )


def _article(row: sqlite3.Row) -> Article:
    return Article(
        id=row["id"],
        source=row["source"],
        source_name=row["source_name"],
        title=row["title"],
        link=row["link"],
        summary=row["summary"],
        published=from_iso(row["published"]),
        fetched=from_iso(row["fetched"]),
        title_key=row["title_key"],
    )


def _impact_values(impact: Impact) -> tuple:
    return (
        impact.article_id,
        impact.ticker,
        impact.company,
        impact.relation,
        impact.direction,
        int(impact.magnitude),
        impact.event_type,
        impact.rationale,
    )


def _impact(row: sqlite3.Row, *, article_id: str | None = None) -> Impact:
    return Impact(
        article_id=article_id if article_id is not None else row["article_id"],
        ticker=row["ticker"],
        company=row["company"],
        relation=row["relation"],
        direction=row["direction"],
        magnitude=row["magnitude"],
        event_type=row["event_type"],
        rationale=row["rationale"],
    )


def _opportunity(row: sqlite3.Row) -> Opportunity:
    data = json.loads(row["data"])
    data["id"] = row["id"]
    return Opportunity.from_dict(data)
