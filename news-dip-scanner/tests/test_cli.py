"""The command line, with the network, the models and the clock replaced by fakes."""

from __future__ import annotations

import functools
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import FakeChatModel, FakeSession, make_article, make_impact, make_opportunity
from test_pipeline import ANALYSIS, CYCLE, MARKETWATCH_URL, SEC_URL, routes, triage_reply

from dip_scanner import cli
from dip_scanner.config import DATABASE_NAME
from dip_scanner.fundamentals import SecFundamentals
from dip_scanner.pipeline import Scanner
from dip_scanner.store import Store

ENV_VARS = (
    "LLM_PROVIDER",
    "LLM_TRIAGE_MODEL",
    "LLM_ANALYSIS_MODEL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY",
    "FOUNDRY_ENDPOINT",
    "FOUNDRY_API_KEY",
    "FOUNDRY_DEPLOYMENT",
    "LLM_REASONING_EFFORT",
    "LLM_MAX_OUTPUT_TOKENS",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "SMTP_FROM",
    "SMTP_STARTTLS",
    "EMAIL_TO",
    "WEBHOOK_URL",
    "WEBHOOK_FORMAT",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "SEC_USER_AGENT",
    "DATA_DIR",
    "SCANNER_CONFIG",
    "FEEDS_FILE",
)

FEEDS_TOML = f"""
[feeds.marketwatch]
name = "MarketWatch"
url = "{MARKETWATCH_URL}"

[feeds.sec-8k]
name = "SEC 8-K filings"
url = "{SEC_URL}"
category = "filings"

[feeds.off]
url = "https://off.example.com/rss"
enabled = false
"""


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """No real environment, .env, clock, TLS setup or network: each test starts in an empty folder at CYCLE."""
    for name in ENV_VARS:  # setenv first so monkeypatch restores the original, also after load_dotenv sets it
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.truststore, "inject_into_ssl", lambda: None)
    monkeypatch.setattr(cli, "_now", lambda: CYCLE)
    monkeypatch.setattr(cli, "make_session", lambda: FakeSession({}))
    monkeypatch.setattr(cli, "SecFundamentals", functools.partial(SecFundamentals, sleep=lambda _: None))


@pytest.fixture
def workdir(tmp_path) -> Path:
    (tmp_path / "feeds.toml").write_text(FEEDS_TOML, encoding="utf-8")
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-test\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def web(monkeypatch) -> FakeSession:
    """Every command's HTTP session serves the fixture feeds, charts and SEC files."""
    session = FakeSession(routes())
    monkeypatch.setattr(cli, "make_session", lambda: session)
    return session


@pytest.fixture
def models(monkeypatch) -> tuple[FakeChatModel, FakeChatModel]:
    triage_model, analysis_model = FakeChatModel(triage_reply), FakeChatModel(ANALYSIS, name="fake-analysis")
    monkeypatch.setattr(cli, "build_models", lambda settings: (triage_model, analysis_model))
    return triage_model, analysis_model


def store_at(folder: Path) -> Store:
    return Store(folder / "data" / DATABASE_NAME)


# --- basics --------------------------------------------------------------------------------------------------------


def test_version_and_help(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.startswith("dip-scanner 0.1.0")
    with pytest.raises(SystemExit) as exit_info:
        cli.main([])  # a command is required
    assert exit_info.value.code == 2


def test_a_missing_env_file_is_a_config_error(capsys, tmp_path):
    assert cli.main(["--env-file", str(tmp_path / "nope.env"), "news"]) == 2
    assert "Configuration problem: The settings file" in capsys.readouterr().err


def test_dotenv_never_overrides_the_real_environment(workdir, monkeypatch):
    (workdir / ".env").write_text("DATA_DIR=from-dotenv\n", encoding="utf-8")
    assert cli.main(["news"]) == 0
    assert (workdir / "from-dotenv" / DATABASE_NAME).exists()

    monkeypatch.setenv("DATA_DIR", str(workdir / "real"))
    assert cli.main(["news"]) == 0
    assert (workdir / "real" / DATABASE_NAME).exists()


def test_a_bad_scanner_config_is_a_config_error(workdir, models, capsys):
    (workdir / "scanner.toml").write_text("[scan]\ninterval_minute = 5\n", encoding="utf-8")
    assert cli.main(["run"]) == 2
    assert "Unknown setting 'interval_minute' in [scan]" in capsys.readouterr().err


def test_a_closed_pipe_stops_quietly(workdir, monkeypatch, capsys):
    """Regression (live run): `dip-scanner feeds | head` printed "Error: [Errno 32] Broken pipe"."""

    class ClosedPipe:
        def write(self, text):
            raise BrokenPipeError(32, "Broken pipe")

        def flush(self):
            pass

    monkeypatch.setattr("sys.stdout", ClosedPipe())
    assert cli.main(["feeds"]) == 1
    assert capsys.readouterr().err == ""


def test_ctrl_c_exits_with_130(workdir, monkeypatch, capsys):
    def interrupted(args, settings):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_news", interrupted)
    assert cli.main(["news"]) == 130
    assert "Interrupted." in capsys.readouterr().err


# --- prices --------------------------------------------------------------------------------------------------------


def test_prices_prints_the_statistics_and_whether_it_is_a_dip(workdir, web, capsys):
    assert cli.main(["prices", "$amd", "-v"]) == 0  # global options also work after the command
    out = capsys.readouterr().out
    assert out.startswith("AMD (Advanced Micro Devices, Inc.) on NasdaqGS, prices in USD")
    assert "Dip by the [dip] thresholds: down 10.0% today; down 8.9% over 5 days" in out


def test_prices_of_an_unknown_ticker_is_an_error(workdir, web, capsys):
    assert cli.main(["prices", "NOSUCH"]) == 1
    assert capsys.readouterr().err.startswith("Error: Yahoo Finance has no prices for NOSUCH")


# --- feeds ---------------------------------------------------------------------------------------------------------


def test_feeds_lists_every_feed_with_its_last_fetch(workdir, capsys):
    from dip_scanner.feeds import FeedState

    with store_at(workdir) as store:
        store.save_feed_state("marketwatch", FeedState(), status=200, error=None, fetched=CYCLE)
        store.save_feed_state("sec-8k", FeedState(), status=403, error="HTTP 403 Forbidden.", fetched=CYCLE)

    assert cli.main(["feeds"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("3 feeds, 2 enabled:")
    assert "on  marketwatch" in out and "last fetch 2026-09-25 20:30 UTC, HTTP 200, 0 stored" in out
    assert "error: HTTP 403 Forbidden." in out
    assert "off off" in out and "never fetched" in out


def test_feeds_check_fetches_each_feed_once(workdir, web, capsys):
    web.routes["https://off.example.com/rss"] = 500

    assert cli.main(["feeds", "--check"]) == 1  # the enabled SEC feed can't be fetched without SEC_USER_AGENT
    captured = capsys.readouterr()
    assert "marketwatch" in captured.out and "4 items, newest" in captured.out
    assert "FAILED: sec.gov only answers requests whose User-Agent names a contact" in captured.out
    assert "FAILED: HTTP 500" in captured.out  # disabled feeds are checked too, but don't fail the check
    assert "1 of 2 enabled feeds answered." in captured.out
    assert "Failed: sec-8k" in captured.err
    assert not any("sec.gov" in url for url in web.urls)
    assert not (workdir / "data").exists()  # a check doesn't touch the database

    (workdir / ".env").write_text("SEC_USER_AGENT=Jane Doe jane@example.com\n", encoding="utf-8")
    assert cli.main(["feeds", "--check"]) == 0
    sec_call = next(call for call in web.calls if call["url"] == SEC_URL)
    assert sec_call["headers"]["User-Agent"] == "Jane Doe jane@example.com"


# --- run, watch, analyze -------------------------------------------------------------------------------------------


def test_run_does_one_cycle_and_prints_the_summary(workdir, web, models, capsys):
    (workdir / ".env").write_text("OPENAI_API_KEY=sk-test\nSEC_USER_AGENT=Jane Doe jane@example.com\n")

    assert cli.main(["run", "--no-notify"]) == 0

    out = capsys.readouterr().out
    assert out.startswith("Cycle 2026-09-25 20:30 UTC: 2/2 feeds ok, 6 new articles, 6 triaged")
    assert "  AMD (Advanced Micro Devices, Inc.): score 67.8, temporary_fear (alert)" in out
    assert "Notes:" in out and "marked invalid" in out
    report = workdir / "data" / "reports" / "2026-09-25" / "203000-opportunities.md"
    assert f"Report: {report}" in out and report.exists()
    assert any("companyfacts" in url for url in web.urls)  # SEC fundamentals were used
    with store_at(workdir) as store:
        [opp] = store.opportunities()
        assert opp.ticker == "AMD" and store.unnotified() == [opp]  # --no-notify


def test_run_without_an_api_key_is_a_config_error(workdir, capsys):
    (workdir / ".env").write_text("LLM_PROVIDER=openai\n", encoding="utf-8")
    assert cli.main(["run"]) == 2
    assert "Configuration problem: Set OPENAI_API_KEY in .env" in capsys.readouterr().err


def test_run_fails_when_every_feed_fails(workdir, models, capsys):
    assert cli.main(["run"]) == 1  # the default session has no routes: every feed is a 404
    assert "Every feed failed" in capsys.readouterr().err


def test_watch_runs_the_scanner_loop_with_the_interval(workdir, models, monkeypatch):
    seen = {}

    def watch(self, *, interval_minutes=None, **kwargs):
        seen.update(interval=interval_minutes, notify=self.notify, feeds=[feed.key for feed in self.feeds])

    monkeypatch.setattr(Scanner, "watch", watch)
    assert cli.main(["watch", "--interval", "2.5", "--no-notify"]) == 0
    assert seen == {"interval": 2.5, "notify": False, "feeds": ["marketwatch"]}  # no SEC_USER_AGENT: SEC skipped
    with pytest.raises(SystemExit):
        cli.main(["watch", "--interval", "0"])


def test_analyze_prints_the_report_and_saves_it(workdir, web, models, capsys):
    assert cli.main(["analyze", "amd"]) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("# Analysis of AMD")
    assert "AMD — Advanced Micro Devices, Inc. · score 67.8" in captured.out
    assert "Saved as opportunity #1" in captured.err

    assert cli.main(["analyze", "AMD", "--no-save"]) == 0
    assert "Saved" not in capsys.readouterr().err
    with store_at(workdir) as store:
        assert len(store.opportunities()) == 1
        assert store.unnotified() == []  # already seen: a running watch won't send it as an alert


def test_analyze_of_an_unknown_ticker_is_an_error(workdir, web, models, capsys):
    assert cli.main(["analyze", "NOSUCH"]) == 1
    assert "no prices for NOSUCH" in capsys.readouterr().err


# --- news, report, track -------------------------------------------------------------------------------------------


def test_news_prints_the_digest_from_the_database(workdir, capsys):
    article = make_article(published=CYCLE - timedelta(hours=2), fetched=CYCLE - timedelta(hours=2))
    other = make_article(title="Fed holds rates", published=CYCLE - timedelta(hours=3))
    with store_at(workdir) as store:
        store.add_articles([article, other], max_age_hours=24, now=CYCLE)
        store.record_triage([article.id, other.id], [make_impact(article_id=article.id)])

    assert cli.main(["news", "--ticker", "amd"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# News digest: last 24 hours")
    assert "AMD — Advanced Micro Devices" in out and article.title in out
    assert "Fed holds rates" not in out  # not about AMD

    assert cli.main(["news", "--hours", "1"]) == 0
    captured = capsys.readouterr()
    assert "No articles in the last hour." in captured.out and "dip-scanner run" in captured.err


def test_report_prints_markdown_and_writes_html(workdir, capsys):
    with store_at(workdir) as store:
        store.add_opportunity(make_opportunity(created=CYCLE - timedelta(days=2)))
        store.add_opportunity(make_opportunity(ticker="OLD", created=CYCLE - timedelta(days=9)))

    assert cli.main(["report", "--html", "out/report.html"]) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("# Dip opportunities of the last 7 days")
    assert "AMD — Advanced Micro Devices · score 72.4" in captured.out and "OLD" not in captured.out
    html = (workdir / "out" / "report.html").read_text(encoding="utf-8")
    assert "Advanced Micro Devices" in html and "Wrote out/report.html" in captured.err

    assert cli.main(["report", "--min-score", "80"]) == 0
    assert "No opportunities this time." in capsys.readouterr().out


def test_track_evaluates_every_stored_opportunity(workdir, web, capsys):
    with store_at(workdir) as store:
        store.add_opportunity(make_opportunity(created=CYCLE - timedelta(days=20)))
        store.add_opportunity(make_opportunity(ticker="NOSUCH", created=CYCLE - timedelta(days=3)))

    assert cli.main(["track"]) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("# Track record")
    assert "AMD" in captured.out and "NOSUCH" not in captured.out
    assert "Left out NOSUCH (1 opportunities): Yahoo Finance has no prices for NOSUCH" in captured.err
    chart_calls = [call for call in web.calls if "/v8/finance/chart/AMD" in call["url"]]
    assert [call["params"]["range"] for call in chart_calls] == ["1mo"]  # one download covering the signal day


def test_track_with_nothing_stored(workdir, capsys):
    assert cli.main(["track", "--days", "30"]) == 0
    assert "No opportunities stored in the last 30 days yet." in capsys.readouterr().out
