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
    "LLM_TRIAGE_REASONING_EFFORT",
    "LLM_ANALYSIS_REASONING_EFFORT",
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


@pytest.mark.parametrize("how", ["option", "environment"])
def test_a_named_scanner_config_that_does_not_exist_is_a_config_error(workdir, web, capsys, monkeypatch, how):
    """Regression: a mistyped --config or SCANNER_CONFIG silently ran with the defaults (no watchlist, default
    thresholds and alert rules)."""
    if how == "option":
        argv = ["--config", "scaner.toml", "prices", "AMD"]
    else:
        monkeypatch.setenv("SCANNER_CONFIG", str(workdir / "nope" / "x.toml"))
        argv = ["prices", "AMD"]
    assert cli.main(argv) == 2
    err = capsys.readouterr().err
    assert "Configuration problem: Scanner config not found" in err
    assert ("from --config" if how == "option" else "from SCANNER_CONFIG") in err


def test_without_a_named_scanner_config_the_defaults_still_apply(workdir, web, capsys):
    assert cli.main(["prices", "AMD"]) == 0  # no ./scanner.toml here: the project's copy (the defaults)
    assert "Dip by the [dip] thresholds" in capsys.readouterr().out


def test_a_folder_given_as_the_scanner_config_is_a_config_error(workdir, capsys):
    (workdir / "configs").mkdir()
    (workdir / "configs" / "scanner.toml").mkdir()
    assert cli.main(["--config", "configs/scanner.toml", "prices", "AMD"]) == 2
    assert "Configuration problem: Scanner config not found" in capsys.readouterr().err
    assert cli.main(["--feeds", "configs", "feeds"]) == 2  # was "Error: [Errno 21] Is a directory", exit 1
    assert "Configuration problem: Feed list can't be read" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["news", "--hours", "inf"], ["report", "--days", "nan"], ["track", "--days", "-1"]])
def test_non_finite_or_negative_periods_are_usage_errors(workdir, argv):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(argv)
    assert exit_info.value.code == 2


def test_a_huge_period_means_everything(workdir, capsys):
    """Regression: `report --days 1000000` ended in an OverflowError traceback."""
    with store_at(workdir) as store:
        store.add_opportunity(make_opportunity(created=CYCLE - timedelta(days=3000)))
    assert cli.main(["report", "--days", "1000000"]) == 0
    assert "AMD — Advanced Micro Devices" in capsys.readouterr().out
    assert cli.main(["news", "--hours", "1e12"]) == 0


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


def test_a_pipe_closed_before_the_final_flush_stops_quietly(workdir, monkeypatch, capsys):
    """Regression: with buffered stdout the output was only flushed at exit, after main() returned: Python printed
    "Exception ignored ... BrokenPipeError" and exited with 120."""

    class BufferedClosedPipe:
        def write(self, text):
            return len(text)  # buffered: nothing goes out yet

        def flush(self):
            raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr("sys.stdout", BufferedClosedPipe())
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

    # Without SEC_USER_AGENT the SEC feed is skipped, as the scanner skips it: not a failure (regression: exit 1).
    assert cli.main(["feeds", "--check"]) == 0
    captured = capsys.readouterr()
    assert "marketwatch" in captured.out and "4 items, newest" in captured.out
    assert "skipped: sec.gov only answers requests whose User-Agent names a contact" in captured.out
    assert "FAILED: HTTP 500" in captured.out  # disabled feeds are checked too, but don't fail the check
    assert "1 of 1 enabled feeds answered." in captured.out
    assert "Skipped until SEC_USER_AGENT is set: sec-8k" in captured.out and "Failed" not in captured.err
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
    assert "Model use today (since 00:00 UTC):\n  triage with fake-model: 1 call" in out
    assert "  analysis with fake-analysis: 1 call, 0 tokens in, 0 out (1 without token counts)" in out
    report = workdir / "data" / "reports" / "2026-09-25" / "203000-opportunities.md"
    assert f"Report: {report}" in out and report.exists()
    assert any("companyfacts" in url for url in web.urls)  # SEC fundamentals were used
    with store_at(workdir) as store:
        [opp] = store.opportunities()
        # --no-notify: shown here, and not pushed by a later notifying run either.
        assert opp.ticker == "AMD" and store.unnotified() == []


def test_run_without_an_api_key_is_a_config_error(workdir, capsys):
    (workdir / ".env").write_text("LLM_PROVIDER=openai\n", encoding="utf-8")
    assert cli.main(["run"]) == 2
    assert "Configuration problem: Set OPENAI_API_KEY in .env" in capsys.readouterr().err


HOOK = "https://hooks.example.com/T123/very-secret-webhook-token"


def hook_posts(web: FakeSession) -> list[dict]:
    return [call["json"] for call in web.calls if call["url"] == HOOK]


def test_a_setup_problem_sends_one_stop_notice_through_the_channels(workdir, web, capsys, monkeypatch):
    """Regression: under cron a revoked key or an empty balance made every run exit 2 in a log nobody reads."""
    web.routes[HOOK] = {"ok": True}
    (workdir / ".env").write_text(f"LLM_PROVIDER=openai\nWEBHOOK_URL={HOOK}\n", encoding="utf-8")

    assert cli.main(["run"]) == 2
    assert "Configuration problem: Set OPENAI_API_KEY in .env" in capsys.readouterr().err
    [payload] = hook_posts(web)
    reason = "Configuration problem: Set OPENAI_API_KEY in .env to use LLM_PROVIDER=openai."
    assert payload["subject"] == f"dip-scanner stopped: {reason}"
    assert f"dip-scanner run stopped at 2026-09-25 20:30 UTC: {reason}" in payload["markdown"]
    assert "very-secret-webhook-token" not in str(payload)

    # The next cron run five minutes later fails the same way, but doesn't repeat the notice.
    monkeypatch.setattr(cli, "_now", lambda: CYCLE + timedelta(minutes=5))
    assert cli.main(["run"]) == 2
    assert len(hook_posts(web)) == 1
    # Twelve hours later it is sent again.
    monkeypatch.setattr(cli, "_now", lambda: CYCLE + timedelta(hours=12))
    assert cli.main(["run"]) == 2
    assert len(hook_posts(web)) == 2


def test_no_stop_notice_with_no_notify_or_when_switched_off(workdir, web):
    web.routes[HOOK] = {"ok": True}
    (workdir / ".env").write_text(f"LLM_PROVIDER=openai\nWEBHOOK_URL={HOOK}\n", encoding="utf-8")
    assert cli.main(["run", "--no-notify"]) == 2
    (workdir / "scanner.toml").write_text("[alerts]\nsystem_notices = false\n", encoding="utf-8")
    assert cli.main(["run"]) == 2
    assert hook_posts(web) == []


def test_watch_sends_a_stop_notice_when_the_model_refuses_the_key(workdir, web, monkeypatch, capsys):
    from dip_scanner.llm import LLMSetupError

    web.routes[HOOK] = {"ok": True}
    (workdir / ".env").write_text(f"OPENAI_API_KEY=sk-test\nWEBHOOK_URL={HOOK}\n", encoding="utf-8")
    refused = FakeChatModel(LLMSetupError("OpenAI rejected the credentials (401). Check OPENAI_API_KEY."))
    monkeypatch.setattr(cli, "build_models", lambda settings: (refused, refused))

    assert cli.main(["watch"]) == 2
    assert "The language model can't be used: OpenAI rejected the credentials" in capsys.readouterr().err
    [payload] = hook_posts(web)
    assert payload["subject"] == (
        "dip-scanner stopped: The language model can't be used: OpenAI rejected the credentials (401). "
        "Check OPENAI_API_KEY."
    )
    assert "`dip-scanner watch` has exited" in payload["markdown"]


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
    assert "**AMD**" in captured.out and "**NOSUCH**" not in captured.out
    # Named in the record itself, so a delisting can't silently flatter the figures.
    assert "Left out: 1 opportunity without prices from Yahoo Finance" in captured.out
    assert "Left out NOSUCH (1 opportunities): Yahoo Finance has no prices for NOSUCH" in captured.err
    chart_calls = [call for call in web.calls if "/v8/finance/chart/AMD" in call["url"]]
    assert [call["params"]["range"] for call in chart_calls] == ["1mo"]  # one download covering the signal day
    assert chart_calls[0]["params"]["events"] == "split"  # with the splits, to compare old prices correctly


def test_track_with_nothing_stored(workdir, capsys):
    assert cli.main(["track", "--days", "30"]) == 0
    assert "No opportunities stored in the last 30 days yet." in capsys.readouterr().out
