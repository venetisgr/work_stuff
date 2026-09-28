-- A version 5 database made by news-dip-scanner's Store before the web service (schema 6), dumped with
-- sqlite3's iterdump: two opportunities handled (one sent, one not), one waiting. See test_store.py.
BEGIN TRANSACTION;
CREATE TABLE analysis_failures (
    ticker TEXT PRIMARY KEY,
    failures INTEGER NOT NULL,
    last_failure TEXT NOT NULL,
    last_error TEXT
);
CREATE TABLE articles (
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
INSERT INTO "articles" VALUES('e1653422f6257a368be881eda233b798e563d01d','marketwatch','MarketWatch','AMD shares slide after weak data-center guidance','https://www.example.com/news/amd-shares-slide-after-weak-data-center-guidance','Advanced Micro Devices cut its data-center revenue outlook, citing slower cloud spending.','2026-09-25T14:00:00.000000+00:00','2026-09-25T14:05:00.000000+00:00','amd shares slide after weak data center guidance','done',0);
CREATE TABLE feeds (
    key TEXT PRIMARY KEY,
    etag TEXT,
    last_modified TEXT,
    last_fetch TEXT,
    last_status INTEGER,
    last_error TEXT
);
INSERT INTO "feeds" VALUES('marketwatch','"v1"',NULL,'2026-09-25T15:00:00.000000+00:00',200,NULL);
CREATE TABLE impacts (
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
INSERT INTO "impacts" VALUES('e1653422f6257a368be881eda233b798e563d01d','AMD','Advanced Micro Devices','direct','negative',4,'guidance','Lower data-center guidance cuts expected revenue growth.');
CREATE TABLE model_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created TEXT NOT NULL,   -- the time of the cycle (or manual analysis) that made the call
    step TEXT NOT NULL,      -- 'triage' or 'analysis'
    model TEXT NOT NULL,
    ticker TEXT,             -- the analysed ticker (analysis only)
    input_tokens INTEGER,    -- NULL when the service didn't report them
    output_tokens INTEGER
);
INSERT INTO "model_calls" VALUES(1,'2026-09-25T15:00:00.000000+00:00','triage','gpt-5-mini',NULL,1200,150);
CREATE TABLE opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    created TEXT NOT NULL,
    score REAL NOT NULL,
    data TEXT NOT NULL,
    notified TEXT,  -- handled: sent, or deliberately not sent (a repeat, superseded, --no-notify, read in `analyze`)
    alerted TEXT    -- actually delivered to the user (an alert, a thesis change, or shown by `analyze`)
);
INSERT INTO "opportunities" VALUES(1,'AMD','2026-09-25T12:00:00.000000+00:00',72.4,'{"ticker": "AMD", "company": "Advanced Micro Devices", "created": "2026-09-25T12:00:00+00:00", "price": 142.5, "currency": "USD", "score": 72.4, "analysis": {"verdict": "temporary_fear", "probability_up_6m": 68, "potential_low": 118.0, "entry_price": 132.0, "target_price": 168.0, "confidence": "medium", "fear": "Investors fear a slowdown in AI data-center spending.", "fundamental_impact": "One quarter of softer guidance; the product roadmap and balance sheet are intact.", "thesis": "The drop prices in a lasting slowdown the guidance doesn''t support.", "risks": ["Hyperscalers cut capex further"], "catalysts": ["Next quarter''s earnings"], "checks": ["Read the earnings call transcript"], "warnings": []}, "stats": {"ticker": "AMD", "name": "Advanced Micro Devices, Inc.", "currency": "USD", "exchange": "NasdaqGS", "as_of": "2026-09-25T14:45:00+00:00", "price": 142.5, "previous_close": 150.0, "change_1d_pct": -5.000000000000004, "change_5d_pct": -8.0, "change_20d_pct": -12.0, "high_20d": 165.0, "high_52w": 190.0, "low_52w": 95.0, "drawdown_20d_pct": -13.636363636363635, "drawdown_52w_pct": -25.0, "above_low_52w_pct": 50.0, "sma_50": 155.2, "sma_200": 140.1, "volatility_pct": 48.0, "volume_ratio": 2.3, "stat_low_6m": 81.53321516706478, "worst_6m_drawdown_pct": -38.5, "session_elapsed": null, "timezone": null, "instrument_type": null}, "article_ids": ["e1653422f6257a368be881eda233b798e563d01d"], "headlines": [{"title": "AMD shares slide after weak data-center guidance", "link": "https://www.example.com/news/amd-shares-slide-after-weak-data-center-guidance", "source": "MarketWatch", "published": "2026-09-25T14:00:00+00:00", "direction": "negative", "magnitude": 4}], "dip_reasons": ["down 5.0% today", "13.6% below its 20-day high"], "model": "fake-model", "news_after_session": false, "account_currency": null, "fx_rate": null, "benchmark": null, "benchmark_level": null}','2026-09-25T12:00:00.000000+00:00','2026-09-25T12:00:00.000000+00:00');
INSERT INTO "opportunities" VALUES(2,'NVDA','2026-09-25T13:00:00.000000+00:00',50.0,'{"ticker": "NVDA", "company": "Advanced Micro Devices", "created": "2026-09-25T13:00:00+00:00", "price": 142.5, "currency": "USD", "score": 50.0, "analysis": {"verdict": "temporary_fear", "probability_up_6m": 68, "potential_low": 118.0, "entry_price": 132.0, "target_price": 168.0, "confidence": "medium", "fear": "Investors fear a slowdown in AI data-center spending.", "fundamental_impact": "One quarter of softer guidance; the product roadmap and balance sheet are intact.", "thesis": "The drop prices in a lasting slowdown the guidance doesn''t support.", "risks": ["Hyperscalers cut capex further"], "catalysts": ["Next quarter''s earnings"], "checks": ["Read the earnings call transcript"], "warnings": []}, "stats": {"ticker": "NVDA", "name": "Advanced Micro Devices, Inc.", "currency": "USD", "exchange": "NasdaqGS", "as_of": "2026-09-25T14:45:00+00:00", "price": 142.5, "previous_close": 150.0, "change_1d_pct": -5.000000000000004, "change_5d_pct": -8.0, "change_20d_pct": -12.0, "high_20d": 165.0, "high_52w": 190.0, "low_52w": 95.0, "drawdown_20d_pct": -13.636363636363635, "drawdown_52w_pct": -25.0, "above_low_52w_pct": 50.0, "sma_50": 155.2, "sma_200": 140.1, "volatility_pct": 48.0, "volume_ratio": 2.3, "stat_low_6m": 81.53321516706478, "worst_6m_drawdown_pct": -38.5, "session_elapsed": null, "timezone": null, "instrument_type": null}, "article_ids": ["e1653422f6257a368be881eda233b798e563d01d"], "headlines": [{"title": "AMD shares slide after weak data-center guidance", "link": "https://www.example.com/news/amd-shares-slide-after-weak-data-center-guidance", "source": "MarketWatch", "published": "2026-09-25T14:00:00+00:00", "direction": "negative", "magnitude": 4}], "dip_reasons": ["down 5.0% today", "13.6% below its 20-day high"], "model": "fake-model", "news_after_session": false, "account_currency": null, "fx_rate": null, "benchmark": null, "benchmark_level": null}','2026-09-25T13:00:00.000000+00:00',NULL);
INSERT INTO "opportunities" VALUES(3,'BA','2026-09-25T14:00:00.000000+00:00',70.0,'{"ticker": "BA", "company": "Advanced Micro Devices", "created": "2026-09-25T14:00:00+00:00", "price": 142.5, "currency": "USD", "score": 70.0, "analysis": {"verdict": "temporary_fear", "probability_up_6m": 68, "potential_low": 118.0, "entry_price": 132.0, "target_price": 168.0, "confidence": "medium", "fear": "Investors fear a slowdown in AI data-center spending.", "fundamental_impact": "One quarter of softer guidance; the product roadmap and balance sheet are intact.", "thesis": "The drop prices in a lasting slowdown the guidance doesn''t support.", "risks": ["Hyperscalers cut capex further"], "catalysts": ["Next quarter''s earnings"], "checks": ["Read the earnings call transcript"], "warnings": []}, "stats": {"ticker": "BA", "name": "Advanced Micro Devices, Inc.", "currency": "USD", "exchange": "NasdaqGS", "as_of": "2026-09-25T14:45:00+00:00", "price": 142.5, "previous_close": 150.0, "change_1d_pct": -5.000000000000004, "change_5d_pct": -8.0, "change_20d_pct": -12.0, "high_20d": 165.0, "high_52w": 190.0, "low_52w": 95.0, "drawdown_20d_pct": -13.636363636363635, "drawdown_52w_pct": -25.0, "above_low_52w_pct": 50.0, "sma_50": 155.2, "sma_200": 140.1, "volatility_pct": 48.0, "volume_ratio": 2.3, "stat_low_6m": 81.53321516706478, "worst_6m_drawdown_pct": -38.5, "session_elapsed": null, "timezone": null, "instrument_type": null}, "article_ids": ["e1653422f6257a368be881eda233b798e563d01d"], "headlines": [{"title": "AMD shares slide after weak data-center guidance", "link": "https://www.example.com/news/amd-shares-slide-after-weak-data-center-guidance", "source": "MarketWatch", "published": "2026-09-25T14:00:00+00:00", "direction": "negative", "magnitude": 4}], "dip_reasons": ["down 5.0% today", "13.6% below its 20-day high"], "model": "fake-model", "news_after_session": false, "account_currency": null, "fx_rate": null, "benchmark": null, "benchmark_level": null}',NULL,NULL);
CREATE TABLE symbol_lookups (
    ticker TEXT NOT NULL,    -- a symbol without prices
    query TEXT NOT NULL,     -- the company name searched for
    resolved TEXT,           -- the symbol found, NULL when nothing matched
    name TEXT,               -- Yahoo's name for it
    checked TEXT NOT NULL,
    unconfirmed INTEGER NOT NULL DEFAULT 0,  -- 1: found by a longer name; taken once a story names it
    PRIMARY KEY (ticker, query)
);
CREATE TABLE system_notices (
    kind TEXT PRIMARY KEY,
    streak INTEGER NOT NULL DEFAULT 0,  -- cycles in a row with this problem
    last_attempt TEXT,                  -- the last time a notice of this kind was tried
    last_sent TEXT                      -- the last time one reached at least one channel
);
INSERT INTO "system_notices" VALUES('stopped',0,'2026-09-24T15:00:00.000000+00:00','2026-09-24T15:00:00.000000+00:00');
CREATE TABLE tickers (
    ticker TEXT PRIMARY KEY,
    valid INTEGER NOT NULL,
    checked TEXT NOT NULL
);
CREATE INDEX articles_published ON articles (published);
CREATE INDEX articles_title_key ON articles (title_key);
CREATE INDEX articles_status ON articles (status, published);
CREATE INDEX impacts_ticker ON impacts (ticker);
CREATE INDEX opportunities_ticker_created ON opportunities (ticker, created);
CREATE INDEX opportunities_created ON opportunities (created);
CREATE INDEX model_calls_created ON model_calls (created);
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('opportunities',3);
INSERT INTO "sqlite_sequence" VALUES('model_calls',1);
COMMIT;
PRAGMA user_version = 5;
