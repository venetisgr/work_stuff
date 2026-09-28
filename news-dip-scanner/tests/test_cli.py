"""The command line, with the network, the models and the clock replaced by fakes."""

from __future__ import annotations

import functools
import io
import logging
import re
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from conftest import FakeChatModel, FakeSession, chart_json, make_article, make_impact, make_opportunity
from test_pipeline import ANALYSIS, CYCLE, MARKETWATCH_URL, SEC_URL, debate_panel, routes, triage_reply

from dip_scanner import cli
from dip_scanner.config import DATABASE_NAME
from dip_scanner.fundamentals import SecFundamentals
from dip_scanner.pipeline import Scanner
from dip_scanner.report import set_display_zone
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
    "DISPLAY_TZ",
    "SCANNER_CONFIG",
    "FEEDS_FILE",
    "SECRET_KEY",
    "BASE_URL",
    "COOKIE_SECURE",
    "ANALYZE_LIMIT_PER_USER",
    "SCANNER_ENABLED",
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


CHART = "https://query1.finance.yahoo.com/v8/finance/chart/"


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


# --- output on Windows, display time zone ----------------------------------------------------------------------------


class Console(io.TextIOWrapper):
    """A console (isatty) in a Windows code page."""

    def isatty(self) -> bool:
        return True


def greek_news(workdir: Path) -> str:
    title = "Η ΔΕΗ ανακοίνωσε αποτελέσματα — μετοχή −4%"  # Greek, an em dash and a minus: none are in cp1252
    article = make_article(title=title, published=CYCLE - timedelta(hours=2), fetched=CYCLE - timedelta(hours=2))
    with store_at(workdir) as store:
        store.add_articles([article], max_age_hours=24, now=CYCLE)
        store.record_triage([article.id], [make_impact(article_id=article.id, ticker="PPC.AT", company="ΔΕΗ")])
    return title


def test_redirected_output_is_utf8_and_never_crashes(workdir, monkeypatch):
    """Regression (Windows): `dip-scanner news >> data\\scanner.log` wrote in cp1252 and died with
    UnicodeEncodeError on the first Greek headline."""
    title = greek_news(workdir)
    redirected = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")  # a file or a pipe on Western Windows
    monkeypatch.setattr("sys.stdout", redirected)
    assert cli.main(["news"]) == 0
    redirected.flush()
    assert title in redirected.buffer.getvalue().decode("utf-8")
    assert redirected.encoding == "utf-8"


def test_a_console_that_lacks_a_character_shows_a_question_mark(workdir, monkeypatch):
    greek_news(workdir)
    console = Console(io.BytesIO(), encoding="cp1252")
    errors = Console(io.BytesIO(), encoding="cp1252")
    monkeypatch.setattr("sys.stdout", console)
    monkeypatch.setattr("sys.stderr", errors)
    assert cli.main(["news"]) == 0
    console.flush()
    assert (console.encoding, console.errors, errors.errors) == ("cp1252", "replace", "replace")
    shown = console.buffer.getvalue().decode("cp1252")
    assert "? ??? ?????????? ???????????? — ?????? ?4%" in shown  # cp1252 has the dash, not Greek or the minus


def test_display_tz_sets_the_time_zone_of_the_output(workdir, capsys, monkeypatch):
    from dip_scanner.feeds import FeedState

    with store_at(workdir) as store:
        store.save_feed_state("marketwatch", FeedState(), status=200, error=None, fetched=CYCLE)
    (workdir / ".env").write_text("DISPLAY_TZ=Europe/Athens\n", encoding="utf-8")
    assert cli.main(["feeds"]) == 0
    assert "last fetch 2026-09-25 23:30 EEST, HTTP 200" in capsys.readouterr().out

    monkeypatch.setenv("DISPLAY_TZ", "Athens")
    assert cli.main(["feeds"]) == 2
    assert "Configuration problem: DISPLAY_TZ must be an IANA time zone name" in capsys.readouterr().err


def test_log_times_use_the_display_time_zone():
    record = logging.LogRecord("dip_scanner", logging.INFO, __file__, 1, "Cycle done", None, None)
    record.created = datetime(2026, 9, 25, 20, 30, 5, tzinfo=UTC).timestamp()
    record.msecs = 250
    brief = cli._DisplayTimeFormatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    verbose = cli._DisplayTimeFormatter("%(asctime)s %(message)s")
    assert brief.format(record) == "2026-09-25 20:30:05 UTC Cycle done"  # UTC by default, not the machine's time
    set_display_zone(ZoneInfo("Europe/Athens"))
    # Regression: the zone wasn't named, so a log line couldn't be told from local time.
    assert brief.format(record) == "2026-09-25 23:30:05 EEST Cycle done"
    assert verbose.format(record) == "2026-09-25 23:30:05,250 EEST Cycle done"


# --- prices --------------------------------------------------------------------------------------------------------


def test_prices_prints_the_statistics_and_whether_it_is_a_dip(workdir, web, capsys):
    assert cli.main(["prices", "$amd", "-v"]) == 0  # global options also work after the command
    out = capsys.readouterr().out
    assert out.startswith("AMD (Advanced Micro Devices, Inc.) on NasdaqGS, prices in USD")
    assert "Dip by the [dip] thresholds: down 10.0% today; down 8.9% over 5 days" in out


def test_prices_of_an_unknown_ticker_is_an_error(workdir, web, capsys):
    assert cli.main(["prices", "NOSUCH"]) == 1
    assert capsys.readouterr().err.startswith("Error: Yahoo Finance has no prices for NOSUCH")


def test_prices_of_an_old_symbol_names_the_one_the_scanner_found(workdir, web, capsys):
    with store_at(workdir) as store:
        store.save_symbol_lookup("OPAP.AT", "Allwyn", "ALWN.AT", "Allwyn AG", checked=CYCLE - timedelta(days=1))
    assert cli.main(["prices", "OPAP.AT"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("Error: Yahoo Finance has no prices for OPAP.AT")
    assert "The scanner found ALWN.AT (Allwyn AG) for Allwyn: try `dip-scanner prices ALWN.AT`." in err


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
    # BA and NVDA have no prices in the fixtures: the scanner looked for a new symbol by company name.
    assert any(url.endswith("/v1/finance/search") for url in web.urls)
    with store_at(workdir) as store:
        [opp] = store.opportunities()
        # --no-notify: shown here, and not pushed by a later notifying run either.
        assert opp.ticker == "AMD" and store.unnotified() == []


def test_run_in_debate_mode_prints_the_debate_and_each_steps_use(workdir, web, capsys, monkeypatch):
    panel = debate_panel()
    monkeypatch.setattr(cli, "build_models", lambda settings: (FakeChatModel(triage_reply), panel))

    assert cli.main(["run", "--no-notify"]) == 0

    out = capsys.readouterr().out
    assert "    debate: GPT-5 75% · Claude Sonnet 5 55% → 70% (medium agreement)" in out
    assert "  analysis:opening with gpt-5: 1 call, 3.5k tokens in, 900 out" in out
    assert "  analysis:rebuttal with claude-sonnet-5: 1 call" in out and "  analysis:judge with " in out


def test_debate_mode_without_the_second_key_is_a_config_error(workdir, capsys, monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE", "ANTHROPIC_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(workdir))  # no ~/.config/anthropic profile either
    (workdir / ".env").write_text("OPENAI_API_KEY=sk-test\nLLM_ANALYSIS_MODE=debate\n", encoding="utf-8")
    assert cli.main(["run", "--no-notify"]) == 2
    assert (
        "Configuration problem: LLM_ANALYSIS_MODE=debate uses anthropic:claude-sonnet-5 (LLM_DEBATERS), but "
        "ANTHROPIC_API_KEY isn't set." in capsys.readouterr().err
    )


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


@pytest.mark.parametrize("command", ["run", "watch"])
def test_a_wrong_display_tz_stops_run_and_watch_with_a_notice(workdir, web, models, capsys, command):
    """Regression: an unknown DISPLAY_TZ (Athens, UTC+3, or any name on Windows without tzdata) stopped every cron
    run before the stop notice was set up, so the scanner went quiet without telling anyone."""
    web.routes[HOOK] = {"ok": True}
    (workdir / ".env").write_text(f"OPENAI_API_KEY=sk-test\nWEBHOOK_URL={HOOK}\nDISPLAY_TZ=Athens\n", encoding="utf-8")

    assert cli.main([command]) == 2

    assert "Configuration problem: DISPLAY_TZ must be an IANA time zone name" in capsys.readouterr().err
    [payload] = hook_posts(web)
    assert payload["subject"].startswith(
        "dip-scanner stopped: Configuration problem: DISPLAY_TZ must be an IANA time zone name"
    )
    assert f"dip-scanner {command} stopped at 2026-09-25 20:30 UTC" in payload["markdown"]
    assert models[0].calls == [] and not any(url == MARKETWATCH_URL for url in web.urls)  # nothing ran
    assert cli.main(["feeds"]) == 2  # other commands report it at once, without a notice
    assert len(hook_posts(web)) == 1


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


def test_analyze_says_when_it_reads_a_symbol_as_its_preferred_listing(workdir, web, models, capsys):
    (workdir / "scanner.toml").write_text('[universe]\npreferred_listings = { "XMD" = "AMD" }\n', encoding="utf-8")
    assert cli.main(["analyze", "XMD", "--no-save"]) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("# Analysis of AMD")
    assert "XMD is read as AMD ([universe] preferred_listings)." in captured.err


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


def test_news_shows_stories_under_the_symbol_a_scan_reads_them_as(workdir, capsys):
    """Regression (live): `news --ticker ALWN.AT` was empty while the Allwyn story sat under the dead OPAP.AT, and
    `news --ticker ASML.AS` left out what was filed under ASML before preferred_listings was set."""
    (workdir / "scanner.toml").write_text('[universe]\npreferred_listings = { "XMD" = "AMD" }\n', encoding="utf-8")
    allwyn = make_article(title="Allwyn shares slide on Italian costs", published=CYCLE - timedelta(hours=2))
    amd = make_article(title="AMD filed under XMD", published=CYCLE - timedelta(hours=3))
    with store_at(workdir) as store:
        store.add_articles([allwyn, amd], max_age_hours=24, now=CYCLE)
        store.record_triage(
            [allwyn.id, amd.id],
            [
                make_impact(ticker="OPAP.AT", company="Allwyn", article_id=allwyn.id),
                make_impact(ticker="XMD", article_id=amd.id),
            ],
        )
        store.save_symbol_lookup("OPAP.AT", "Allwyn", "ALWN.AT", "Allwyn AG", checked=CYCLE - timedelta(days=1))

    assert cli.main(["news", "--ticker", "ALWN.AT"]) == 0
    out = capsys.readouterr().out
    assert "ALWN.AT — Allwyn" in out and allwyn.title in out and "OPAP.AT" not in out

    assert cli.main(["news", "--ticker", "XMD"]) == 0
    captured = capsys.readouterr()
    assert "AMD — Advanced Micro Devices" in captured.out and amd.title in captured.out
    assert "XMD is read as AMD ([universe] preferred_listings)." in captured.err

    assert cli.main(["news"]) == 0
    out = capsys.readouterr().out
    assert "ALWN.AT — Allwyn" in out and "AMD — Advanced Micro Devices" in out
    assert "OPAP.AT —" not in out and "XMD —" not in out


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


def test_track_compares_with_the_index_and_shows_the_account_currency(workdir, web, capsys):
    web.routes[CHART + "%5EGSPC"] = chart_json(
        "^GSPC",
        [7000 + 10 * n for n in range(20)],
        currency="USD",
        start=date(2026, 9, 1),
        instrument="INDEX",
        zone="America/New_York",
    )
    web.routes[CHART + "EURUSD%3DX"] = chart_json("EURUSD=X", [1.15] * 20, currency="USD", start=date(2026, 9, 1))
    (workdir / "scanner.toml").write_text('[account]\ncurrency = "EUR"\n', encoding="utf-8")
    with store_at(workdir) as store:
        store.add_opportunity(make_opportunity(created=CYCLE - timedelta(days=20)))

    assert cli.main(["track"]) == 0
    captured = capsys.readouterr()
    assert "| Return | In EUR | Index | vs index |" in captured.out
    row = next(line for line in captured.out.splitlines() if "**AMD**" in line)
    assert "(^GSPC) |" in row and row.count("–") <= 4
    assert "| Average return in EUR (exchange-rate moves included) | +" in captured.out
    assert "Note:" not in captured.err
    assert [url.rsplit("/", 1)[1] for url in web.urls if "%5E" in url or "%3D" in url] == ["%5EGSPC", "EURUSD%3DX"]


def test_track_without_index_prices_says_so_and_carries_on(workdir, web, capsys):
    with store_at(workdir) as store:
        store.add_opportunity(make_opportunity(created=CYCLE - timedelta(days=20)))
    assert cli.main(["track"]) == 0
    captured = capsys.readouterr()
    assert "Note: No prices for the benchmark index ^GSPC, so 1 opportunities show – for it" in captured.err
    assert "| Average return of the benchmark index over the same days | – |" in captured.out


def test_track_with_nothing_stored(workdir, capsys):
    assert cli.main(["track", "--days", "30"]) == 0
    assert "No opportunities stored in the last 30 days yet." in capsys.readouterr().out


# --- the website's accounts and backups ----------------------------------------------------------------------------


@pytest.fixture
def fast_scrypt(monkeypatch):
    from dip_scanner import accounts as accounts_module

    monkeypatch.setattr(accounts_module, "SCRYPT_N", 2**4)


def _link(out: str, kind: str) -> str:
    [token] = re.findall(rf"https://dips\.example\.com/{kind}/([A-Za-z0-9_-]{{43}})", out)
    return token


def test_users_add_admin_prints_a_setup_link_that_works_once(workdir, capsys, monkeypatch, fast_scrypt):
    from dip_scanner.accounts import Accounts

    assert cli.main(["users", "add-admin", "owner@example.com"]) == 2  # no BASE_URL: nothing is created
    assert "Set BASE_URL" in capsys.readouterr().err
    assert cli.main(["users", "list"]) == 0
    assert "No accounts yet" in capsys.readouterr().out

    monkeypatch.setenv("BASE_URL", "https://dips.example.com/")
    assert cli.main(["users", "add-admin", "Owner@Example.com", "--name", "Owner"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Created the admin account owner@example.com.\nOpen this link within 48 hours to set the")
    token = _link(out, "password")
    with store_at(workdir) as store:
        accounts = Accounts(store, clock=lambda: CYCLE)
        user = accounts.use_password_token(token, "a long enough password")
        assert (user.email, user.name, user.role, user.has_password) == ("owner@example.com", "Owner", "admin", True)
        assert token not in str(store.query("SELECT * FROM password_tokens")[0])

    # Again for an existing user: it stays (or becomes) an admin, and gets a reset link.
    assert cli.main(["users", "add-admin", "owner@example.com"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("owner@example.com already had an account.\nOpen this link within 48 hours to choose a new")
    _link(out, "password")


def test_users_add_admin_promotes_and_enables_a_member(workdir, capsys, monkeypatch, fast_scrypt):
    from dip_scanner.accounts import Accounts

    monkeypatch.setenv("BASE_URL", "https://dips.example.com")
    with store_at(workdir) as store:
        accounts = Accounts(store, clock=lambda: CYCLE)
        accounts.create_user("owner@example.com", role="admin", password="a long enough password")
        member = accounts.create_user("friend@example.com", password="a long enough password")
        accounts.set_disabled(member.id, True)
    assert cli.main(["users", "add-admin", "friend@example.com"]) == 0
    assert "friend@example.com already had an account; made it an admin and enabled it." in capsys.readouterr().out
    with store_at(workdir) as store:
        friend = Accounts(store).get_user_by_email("friend@example.com")
        assert friend.is_admin and not friend.disabled


def test_users_invite_list_disable_enable_and_reset_link(workdir, capsys, monkeypatch, fast_scrypt):
    from dip_scanner.accounts import Accounts

    monkeypatch.setenv("BASE_URL", "https://dips.example.com")
    assert cli.main(["users", "invite", "Friend@Example.com"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Invite for friend@example.com as a member, valid once within 7 days:")
    member_invite = _link(out, "invite")
    assert cli.main(["users", "invite", "--role", "admin"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Invite for anyone with the link as an admin")
    with store_at(workdir) as store:
        accounts = Accounts(store, clock=lambda: CYCLE)
        accounts.create_user("owner@example.com", role="admin", password="a long enough password")
        accounts.accept_invite(member_invite, name="Friend", password="another long password")
        accounts.update_settings(
            accounts.get_user_by_email("friend@example.com").id,
            replace(accounts.defaults, telegram_chat_id="42", email_alerts=True),
        )

    assert cli.main(["users", "list"]) == 0
    out = capsys.readouterr().out
    assert "2 accounts:" in out and "1 unused invite:" in out
    assert re.search(r"#1 +owner@example.com +admin +active +never signed in; alerts: none", out)
    assert re.search(r"#2 +friend@example.com +member +active .*alerts: email, telegram \(Friend\)", out)
    assert "anyone with the link" in out and "expires 2026-10-02 20:30 UTC" in out

    assert cli.main(["users", "disable", "friend@example.com"]) == 0
    assert capsys.readouterr().out == "friend@example.com is disabled and signed out everywhere.\n"
    assert cli.main(["users", "reset-link", "friend@example.com"]) == 1
    assert "is disabled; `dip-scanner users enable` it first" in capsys.readouterr().err
    assert cli.main(["users", "enable", "FRIEND@example.com"]) == 0
    assert capsys.readouterr().out == "friend@example.com is enabled.\n"
    assert cli.main(["users", "reset-link", "friend@example.com"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Open this link within 48 hours to choose a new password for friend@example.com")
    _link(out, "password")

    assert cli.main(["users", "disable", "nobody@example.com"]) == 1
    assert "there is no account for nobody@example.com" in capsys.readouterr().err
    assert cli.main(["users", "disable", "owner@example.com"]) == 1  # the only admin
    assert "is the only admin" in capsys.readouterr().err
    assert cli.main(["users", "invite", "owner@example.com"]) == 1
    assert "There is already an account for owner@example.com." in capsys.readouterr().err


def test_users_unlock_forgets_an_emails_failed_sign_ins(workdir, capsys, fast_scrypt):
    from dip_scanner.accounts import EMAIL_LIMIT, Accounts

    with store_at(workdir) as store:
        accounts = Accounts(store)
        accounts.create_user("owner@example.com", role="admin", password="a long enough password")
        for number in range(EMAIL_LIMIT):
            accounts.record_failed_login(f"203.0.{number // 250}.{number % 250 + 1}", "Owner@example.com")
        assert accounts.login_locked("198.51.100.1", "owner@example.com")
    assert cli.main(["users", "unlock", "OWNER@example.com"]) == 0
    assert capsys.readouterr().out == (
        f"Forgot {2 * EMAIL_LIMIT} failed sign-ins for owner@example.com; it can sign in again.\n"
    )
    with store_at(workdir) as store:
        accounts = Accounts(store)
        assert not accounts.login_locked("198.51.100.1", "owner@example.com")
        keys = [row[0] for row in store.query("SELECT key FROM login_attempts")]
        assert len(keys) == EMAIL_LIMIT and all(key.startswith("ip:") for key in keys)  # the addresses' own counts


def test_backup_copies_the_database_and_keeps_the_newest(workdir, capsys, monkeypatch):
    assert cli.main(["backup"]) == 1  # nothing yet
    assert "nothing to back up" in capsys.readouterr().err
    with store_at(workdir) as store:
        store.add_opportunity(make_opportunity())
    times = iter(CYCLE + timedelta(hours=hours) for hours in range(4))
    monkeypatch.setattr(cli, "_now", lambda: next(times))
    for _ in range(4):
        assert cli.main(["backup", "--keep", "2"]) == 0
    out = capsys.readouterr().out
    assert "Backed up the database to " in out and "the newest 2 backups are kept." in out
    folder = workdir / "data" / "backups"
    assert sorted(path.name for path in folder.iterdir()) == [
        "scanner-20260925-223000.sqlite3",
        "scanner-20260925-233000.sqlite3",
    ]
    with Store(folder / "scanner-20260925-233000.sqlite3") as copy:
        assert len(copy.opportunities()) == 1
    with pytest.raises(SystemExit):
        cli.main(["backup", "--keep", "0"])


def test_serve_needs_a_secret_key_and_runs_the_website(workdir, capsys, monkeypatch):
    from dip_scanner.web import server

    seen: dict = {}

    def serve(settings, config, feeds, **kwargs):
        seen.update(kwargs, feeds=[feed.key for feed in feeds], secret=settings.web.secret_key)

    monkeypatch.setattr(server, "serve", serve)
    assert cli.main(["serve"]) == 2
    err = capsys.readouterr().err
    assert "Configuration problem: SECRET_KEY isn't set" in err and 'python -c "import secrets' in err
    assert seen == {}

    monkeypatch.setenv("SECRET_KEY", "k" * 40)
    assert cli.main(["serve", "--host", "0.0.0.0", "--port", "9000"]) == 0
    assert (seen["host"], seen["port"], seen["scanner_enabled"]) == ("0.0.0.0", 9000, True)
    assert seen["feeds"] == ["marketwatch", "sec-8k", "off"] and seen["secret"] == "k" * 40
    assert cli.main(["serve", "--no-scanner"]) == 0
    assert (seen["host"], seen["port"], seen["scanner_enabled"]) == ("127.0.0.1", 8080, False)
    monkeypatch.setenv("SCANNER_ENABLED", "false")
    assert cli.main(["serve"]) == 0 and seen["scanner_enabled"] is False
    with pytest.raises(SystemExit):
        cli.main(["serve", "--port", "70000"])


def test_serve_without_the_web_extra_says_what_to_install(workdir, capsys, monkeypatch):
    import sys

    monkeypatch.setenv("SECRET_KEY", "k" * 40)
    monkeypatch.setitem(sys.modules, "dip_scanner.web.server", None)
    assert cli.main(["serve"]) == 2
    assert 'The website needs the web extra: pip install -e ".[web]"' in capsys.readouterr().err


def test_the_readme_names_every_command():
    readme = (Path(cli.__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
    commands = cli._parser()._subparsers._group_actions[0].choices
    for name in commands:
        assert f"`dip-scanner {name}" in readme, name
    for action in commands["users"]._subparsers._group_actions[0].choices:
        assert f"`dip-scanner users {action}" in readme or f"`{action} EMAIL`" in readme, action
