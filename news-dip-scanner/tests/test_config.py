import re
from dataclasses import fields
from pathlib import Path

import pytest
from dotenv import dotenv_values

from dip_scanner.config import (
    DEFAULT_MODELS,
    PROJECT_ROOT,
    AlertConfig,
    ConfigError,
    DipConfig,
    LLMSettings,
    NotifySettings,
    ScanConfig,
    ScannerConfig,
    Settings,
    UniverseConfig,
    default_file,
    load_feeds,
    load_scanner_config,
    load_settings,
)
from dip_scanner.models import Feed

# --- environment ---


def test_an_empty_environment_gives_the_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = load_settings({})
    assert settings.llm == LLMSettings(provider="openai")
    assert settings.notify == NotifySettings()
    assert settings.notify.smtp_port == 587 and settings.notify.smtp_starttls is True
    assert settings.notify.email_to == [] and settings.notify.webhook_format == "generic"
    assert settings.sec_user_agent is None
    assert settings.data_dir == tmp_path / "data"
    assert Settings().llm.provider == "openai"


def test_blank_values_count_as_missing():
    settings = load_settings({"LLM_PROVIDER": "  ", "OPENAI_API_KEY": "", "SMTP_PORT": " ", "SEC_USER_AGENT": "  "})
    assert settings.llm.provider == "openai"
    assert settings.llm.openai_api_key is None
    assert settings.notify.smtp_port == 587
    assert settings.sec_user_agent is None


def test_every_setting_is_read():
    env = {
        "LLM_PROVIDER": "Azure",
        "LLM_TRIAGE_MODEL": "gpt-5-mini-dep",
        "LLM_ANALYSIS_MODEL": "gpt-5-dep",
        "OPENAI_API_KEY": "sk-1",
        "OPENAI_BASE_URL": "https://gateway.example.com/v1",
        "ANTHROPIC_API_KEY": "sk-ant-1",
        "FOUNDRY_ENDPOINT": "my-resource",
        "FOUNDRY_API_KEY": "fk",
        "FOUNDRY_DEPLOYMENT": "gpt-5",
        "LLM_REASONING_EFFORT": "Low",
        "LLM_TRIAGE_REASONING_EFFORT": "Minimal",
        "LLM_ANALYSIS_REASONING_EFFORT": "high",
        "LLM_MAX_OUTPUT_TOKENS": "16_000",
        "SMTP_HOST": "smtp.example.com",
        "SMTP_PORT": "465",
        "SMTP_USER": "me",
        "SMTP_PASSWORD": "pw",
        "SMTP_FROM": "Scanner <me@example.com>",
        "SMTP_STARTTLS": "no",
        "EMAIL_TO": "a@example.com, b@example.com;c@example.com,,",
        "WEBHOOK_URL": "https://hooks.slack.com/services/x",
        "WEBHOOK_FORMAT": "SLACK",
        "TELEGRAM_BOT_TOKEN": "123:abc",
        "TELEGRAM_CHAT_ID": "-10042",
        "SEC_USER_AGENT": "Jane Doe jane@example.com",
    }
    settings = load_settings(env)
    assert settings.llm == LLMSettings(
        provider="azure",
        triage_model="gpt-5-mini-dep",
        analysis_model="gpt-5-dep",
        openai_api_key="sk-1",
        openai_base_url="https://gateway.example.com/v1",
        anthropic_api_key="sk-ant-1",
        foundry_endpoint="my-resource",
        foundry_api_key="fk",
        foundry_deployment="gpt-5",
        reasoning_effort="low",
        triage_reasoning_effort="minimal",
        analysis_reasoning_effort="high",
        max_output_tokens=16000,
    )
    assert settings.notify == NotifySettings(
        smtp_host="smtp.example.com",
        smtp_port=465,
        smtp_user="me",
        smtp_password="pw",
        smtp_from="Scanner <me@example.com>",
        smtp_starttls=False,
        email_to=["a@example.com", "b@example.com", "c@example.com"],
        webhook_url="https://hooks.slack.com/services/x",
        webhook_format="slack",
        telegram_bot_token="123:abc",
        telegram_chat_id="-10042",
    )
    assert settings.sec_user_agent == "Jane Doe jane@example.com"


@pytest.mark.parametrize(
    ("value", "expected"), [("anthropic", "anthropic"), ("foundry", "azure"), ("OpenAI", "openai")]
)
def test_provider_names_are_forgiving(value, expected):
    assert load_settings({"LLM_PROVIDER": value}).llm.provider == expected


def test_default_models_cover_openai_and_anthropic_only():
    assert set(DEFAULT_MODELS) == {"openai", "anthropic"}
    assert all(len(pair) == 2 and all(pair) for pair in DEFAULT_MODELS.values())


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"LLM_PROVIDER": "gemini"}, "LLM_PROVIDER must be one of openai, azure, anthropic (got 'gemini')"),
        ({"LLM_MAX_OUTPUT_TOKENS": "lots"}, "LLM_MAX_OUTPUT_TOKENS must be a whole number"),
        ({"LLM_MAX_OUTPUT_TOKENS": "0"}, "LLM_MAX_OUTPUT_TOKENS must be greater than zero"),
        ({"SMTP_PORT": "smtp"}, "SMTP_PORT must be a whole number"),
        ({"SMTP_PORT": "70000"}, "SMTP_PORT must be a port number"),
        ({"SMTP_STARTTLS": "maybe"}, "SMTP_STARTTLS must be true or false"),
        ({"WEBHOOK_FORMAT": "teams"}, "WEBHOOK_FORMAT must be one of slack, discord, generic"),
    ],
)
def test_wrong_values_are_reported(env, message):
    with pytest.raises(ConfigError, match=message.replace("(", r"\(").replace(")", r"\)")):
        load_settings(env)


@pytest.mark.parametrize(
    ("value", "expected"), [("1", True), ("TRUE", True), ("on", True), ("0", False), ("off", False)]
)
def test_starttls_accepts_common_spellings(value, expected):
    assert load_settings({"SMTP_STARTTLS": value}).notify.smtp_starttls is expected


def test_data_dir_is_made_absolute_and_expands_home(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert load_settings({"DATA_DIR": "scanner-data"}).data_dir == tmp_path / "scanner-data"
    assert load_settings({"DATA_DIR": "~/dips"}).data_dir == tmp_path / "home" / "dips"
    assert load_settings({"DATA_DIR": str(tmp_path / "abs")}).data_dir == tmp_path / "abs"


def test_data_dir_must_not_be_a_file(tmp_path):
    path = tmp_path / "data"
    path.write_text("oops")
    with pytest.raises(ConfigError, match="DATA_DIR must be a folder"):
        load_settings({"DATA_DIR": str(path)})


def test_without_an_env_mapping_the_process_environment_is_used(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    settings = load_settings()
    assert (settings.llm.provider, settings.llm.anthropic_api_key) == ("anthropic", "sk-ant-env")


# --- scanner.toml ---


def _write(tmp_path: Path, text: str, name: str = "scanner.toml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_no_scanner_file_gives_the_defaults(tmp_path):
    assert load_scanner_config(None) == ScannerConfig()
    assert load_scanner_config(tmp_path / "missing.toml") == ScannerConfig()
    assert load_scanner_config(_write(tmp_path, "")) == ScannerConfig()


def test_the_defaults_match_the_spec():
    config = ScannerConfig()
    assert config.scan == ScanConfig(
        interval_minutes=5,
        max_article_age_hours=24,
        lookback_hours=48,
        triage_batch_size=20,
        max_triage_attempts=3,
        max_candidates_per_cycle=8,
        cooldown_hours=24,
        reanalyse_same_session_hours=12,
        max_analyses_per_day=40,
        context_news=True,
        workers=8,
        retention_days=30,
    )
    assert config.dip == DipConfig(3.0, 6.0, 10.0, 2, ("negative", "mixed"), True)
    assert config.universe == UniverseConfig((), False, (), 1.0, None)
    assert config.alerts == AlertConfig(
        min_score=65,
        min_probability=60,
        verdicts=("temporary_fear", "mixed"),
        repeat_hours=24,
        min_score_change=10,
        system_notices=True,
        notice_after_cycles=6,
    )


def test_a_full_scanner_file_is_read(tmp_path):
    path = _write(
        tmp_path,
        """
[scan]
interval_minutes = 10
max_article_age_hours = 12.5
lookback_hours = 72
triage_batch_size = 10
max_triage_attempts = 2
max_candidates_per_cycle = 0
cooldown_hours = 0
reanalyse_same_session_hours = 6.5
max_analyses_per_day = 0
context_news = false
workers = 4
retention_days = 14

[dip]
min_drop_1d_pct = -4        # the sign doesn't matter
min_drop_5d_pct = 7.5
min_drawdown_20d_pct = 12
min_magnitude = 3
directions = ["Negative"]
include_indirect = false

[universe]
watchlist = [" amd", "sap.de", "AMD"]
only_watchlist = true
exclude = ["tsla"]
min_price = 5
allowed_suffixes = ["", "de", ".at", ".DE"]

[alerts]
min_score = 70.5
min_probability = 65
verdicts = ["temporary_fear"]
system_notices = false
notice_after_cycles = 12
""",
    )
    config = load_scanner_config(path)
    assert config.scan == ScanConfig(
        interval_minutes=10,
        max_article_age_hours=12.5,
        lookback_hours=72,
        triage_batch_size=10,
        max_triage_attempts=2,
        max_candidates_per_cycle=0,
        cooldown_hours=0,
        reanalyse_same_session_hours=6.5,
        max_analyses_per_day=0,
        context_news=False,
        workers=4,
        retention_days=14,
    )
    assert isinstance(config.scan.interval_minutes, float)
    assert config.dip == DipConfig(4.0, 7.5, 12.0, 3, ("negative",), False)
    assert config.universe == UniverseConfig(("AMD", "SAP.DE"), True, ("TSLA",), 5.0, ("", ".DE", ".AT"))
    assert config.alerts == AlertConfig(70.5, 65, ("temporary_fear",), system_notices=False, notice_after_cycles=12)


def test_watchlist_and_exclude_are_written_like_triage_tickers(tmp_path):
    """Regression: exclude = ["NASDAQ:TSLA"] or ["BRK.B"] never matched the triage's TSLA / BRK-B."""
    text = '[universe]\nwatchlist = ["$amd", "BRK.B"]\nexclude = ["NASDAQ:TSLA", "700.HK", "SPY"]\n'
    universe = load_scanner_config(_write(tmp_path, text)).universe
    assert universe.watchlist == ("AMD", "BRK-B")
    assert universe.exclude == ("TSLA", "0700.HK", "SPY")  # an ETF isn't a triage ticker, but stays excluded


def test_the_repeat_alert_settings_are_read_and_checked(tmp_path):
    alerts = load_scanner_config(_write(tmp_path, "[alerts]\nrepeat_hours = 6\nmin_score_change = 5\n")).alerts
    assert (alerts.repeat_hours, alerts.min_score_change) == (6.0, 5.0)
    with pytest.raises(ConfigError, match="alerts.repeat_hours can't be negative"):
        load_scanner_config(_write(tmp_path, "[alerts]\nrepeat_hours = -1\n"))


def test_a_partial_scanner_file_keeps_the_other_defaults(tmp_path):
    config = load_scanner_config(_write(tmp_path, "[dip]\nmin_drop_1d_pct = 5\n"))
    assert config.dip.min_drop_1d_pct == 5.0
    assert config.dip.min_drop_5d_pct == 6.0
    assert config.scan == ScanConfig()


def test_unknown_keys_are_named_with_their_section(tmp_path):
    path = _write(tmp_path, "[scan]\ninterval_minute = 5\nworkers = 2\n")
    with pytest.raises(ConfigError) as info:
        load_scanner_config(path)
    message = str(info.value)
    assert "'interval_minute'" in message and "[scan]" in message and str(path) in message
    assert "interval_minutes" in message  # lists the known settings


def test_several_unknown_keys_are_all_named(tmp_path):
    with pytest.raises(ConfigError, match=r"Unknown settings 'a', 'b' in \[alerts\]"):
        load_scanner_config(_write(tmp_path, "[alerts]\na = 1\nb = 2\n"))


def test_unknown_sections_are_named(tmp_path):
    with pytest.raises(ConfigError, match=r"Unknown section \[alert\]"):
        load_scanner_config(_write(tmp_path, "[alert]\nmin_score = 1\n"))


def test_top_level_keys_are_unknown_sections(tmp_path):
    with pytest.raises(ConfigError, match=r"Unknown section \[workers\]"):
        load_scanner_config(_write(tmp_path, "workers = 3\n"))


def test_a_section_must_be_a_table(tmp_path):
    with pytest.raises(ConfigError, match=r"\[scan\] in .* should be a table"):
        load_scanner_config(_write(tmp_path, 'scan = "fast"\n'))


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('[scan]\nworkers = "8"\n', "scan.workers in .* must be a whole number"),
        ("[scan]\nworkers = 2.5\n", "scan.workers in .* must be a whole number"),
        ("[scan]\ninterval_minutes = true\n", "scan.interval_minutes in .* must be a number"),
        ('[scan]\ncontext_news = "yes"\n', "scan.context_news in .* must be true or false"),
        ('[universe]\nwatchlist = "AMD"\n', "universe.watchlist in .* must be a list of strings"),
        ("[universe]\nexclude = [1, 2]\n", "universe.exclude in .* must be a list of strings"),
        ("[scan]\ninterval_minutes = 0\n", "scan.interval_minutes must be greater than zero"),
        ("[scan]\nworkers = 0\n", "scan.workers must be at least 1"),
        ("[scan]\nmax_candidates_per_cycle = -1\n", "scan.max_candidates_per_cycle can't be negative"),
        ("[scan]\nreanalyse_same_session_hours = -1\n", "scan.reanalyse_same_session_hours can't be negative"),
        ("[scan]\nmax_analyses_per_day = -5\n", "scan.max_analyses_per_day can't be negative"),
        ("[scan]\nmax_analyses_per_day = 2.5\n", "scan.max_analyses_per_day in .* must be a whole number"),
        ("[alerts]\nnotice_after_cycles = 0\n", "alerts.notice_after_cycles must be at least 1"),
        ('[alerts]\nsystem_notices = "yes"\n', "alerts.system_notices in .* must be true or false"),
        ("[dip]\nmin_magnitude = 6\n", "dip.min_magnitude must be between 1 and 5"),
        ("[alerts]\nmin_score = 101\n", "alerts.min_score must be between 0 and 100"),
        ("[alerts]\nmin_probability = -1\n", "alerts.min_probability must be between 0 and 100"),
        ('[dip]\ndirections = ["down"]\n', "dip.directions in .* has unknown value 'down'"),
        ('[alerts]\nverdicts = ["buy", "sell"]\n', "alerts.verdicts in .* has unknown values 'buy', 'sell'"),
        ("[scan\n", "is not valid TOML"),
    ],
)
def test_wrong_scanner_values_are_reported(tmp_path, text, message):
    with pytest.raises(ConfigError, match=message):
        load_scanner_config(_write(tmp_path, text))


def test_a_whole_float_is_accepted_for_a_whole_number(tmp_path):
    assert load_scanner_config(_write(tmp_path, "[scan]\nworkers = 4.0\n")).scan.workers == 4


# --- feeds.toml ---


def test_feeds_are_read_with_defaults(tmp_path):
    path = _write(
        tmp_path,
        """
[feeds.marketwatch]
name = "MarketWatch Top Stories"
url = " https://feeds.marketwatch.com/marketwatch/topstories/ "
category = "markets"

[feeds.sec]
url = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&output=atom"
enabled = false
category = "filings"

[feeds.plain]
url = "http://example.com/rss"
name = "  "
""",
        "feeds.toml",
    )
    assert load_feeds(path) == [
        Feed("marketwatch", "MarketWatch Top Stories", "https://feeds.marketwatch.com/marketwatch/topstories/"),
        Feed(
            "sec",
            "sec",
            "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&output=atom",
            enabled=False,
            category="filings",
        ),
        Feed("plain", "plain", "http://example.com/rss", enabled=True, category="markets"),
    ]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "doesn't define any feeds"),
        ("[feeds]\n", "doesn't define any feeds"),
        ('feeds = "x"\n', "doesn't define any feeds"),
        ('[feeds]\nreuters = "https://x"\n', 'Feed "reuters" .* should be a table'),
        ('[feeds.reuters]\nname = "Reuters"\n', 'Feed "reuters" .* needs a url'),
        ('[feeds.reuters]\nurl = "ftp://x"\n', 'Feed "reuters" .* needs a url starting with http'),
        ('[feeds.reuters]\nurl = "https://x"\nlink = "y"\n', "Unknown setting 'link' for feed \"reuters\""),
        ('[feeds.reuters]\nurl = "https://x"\nenabled = "no"\n', 'enabled for feed "reuters" .* must be true or false'),
        ('[feeds.reuters]\nurl = "https://x"\nname = 3\n', 'name for feed "reuters" .* must be a string'),
        ('[feeds.sec]\nurl = "https://x"\ndedup_titles = 0\n', 'dedup_titles for feed "sec" .* must be true or false'),
        ('[feeds.pr]\nurl = "https://x"\nlanguages = "en"\n', 'languages for feed "pr" .* must be a list'),
        ('[feeds.pr]\nurl = "https://x"\nlanguages = [""]\n', 'languages for feed "pr" .* must be a list'),
        ('[feeds."ticker:AMD"]\nurl = "https://x"\n', "can't be empty or contain"),
        ("[feeds\n", "is not valid TOML"),
    ],
)
def test_wrong_feed_lists_are_reported(tmp_path, text, message):
    with pytest.raises(ConfigError, match=message):
        load_feeds(_write(tmp_path, text, "feeds.toml"))


def test_feed_title_dedup_and_languages_can_be_set(tmp_path):
    text = (
        '[feeds.sec]\nurl = "https://sec.example.com"\ndedup_titles = false\n\n'
        '[feeds.athens]\nurl = "https://gr.example.com"\nlanguages = ["EL", "en_US", "el"]\n\n'
        '[feeds.any]\nurl = "https://any.example.com"\nlanguages = []\n'
    )
    sec, athens, anything = load_feeds(_write(tmp_path, text, "feeds.toml"))
    assert (sec.dedup_titles, sec.languages) == (False, ("en",))
    assert (athens.dedup_titles, athens.languages) == (True, ("el", "en-us"))
    assert anything.languages == ()


def test_a_missing_feed_list_is_reported(tmp_path):
    with pytest.raises(ConfigError, match="Feed list not found"):
        load_feeds(tmp_path / "nope.toml")


@pytest.mark.skipif(not (PROJECT_ROOT / "feeds.toml").exists(), reason="feeds.toml not written yet")
def test_the_shipped_feed_list_loads():
    feeds = load_feeds(PROJECT_ROOT / "feeds.toml")
    assert len(feeds) >= 10
    assert len({feed.key for feed in feeds}) == len(feeds)
    # Only the SEC feed repeats formulaic titles for different items.
    assert [feed.key for feed in feeds if not feed.dedup_titles] == ["sec-8k-filings"]


def test_the_shipped_scanner_config_spells_out_the_defaults():
    assert load_scanner_config(PROJECT_ROOT / "scanner.toml") == ScannerConfig()


def test_the_shipped_scanner_config_and_the_readme_name_every_setting():
    text = (PROJECT_ROOT / "scanner.toml").read_text(encoding="utf-8")
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    written = set(re.findall(r"^#? ?([a-z_0-9]+) = ", text, re.MULTILINE))
    sections = {"scan": ScanConfig, "dip": DipConfig, "universe": UniverseConfig, "alerts": AlertConfig}
    for section, cls in sections.items():
        assert f"[{section}]" in text
        for name in (f.name for f in fields(cls)):
            assert name in written, f"{section}.{name} is missing from scanner.toml"
    # The settings that bound the model bill and keep an unattended scanner honest are in the README too.
    for name in ("max_analyses_per_day", "reanalyse_same_session_hours", "system_notices", "notice_after_cycles"):
        assert f"`[{'scan' if 'analys' in name else 'alerts'}] {name}`" in readme, name


def test_the_env_example_loads_and_lists_every_setting():
    text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    values = {k: v or "" for k, v in dotenv_values(PROJECT_ROOT / ".env.example").items()}
    assert load_settings(values).llm.provider == "openai"
    read = set(re.findall(r'get\("([A-Z_]+)"', (PROJECT_ROOT / "dip_scanner" / "config.py").read_text()))
    assert len(read) > 20
    assert [name for name in sorted(read | {"SCANNER_CONFIG", "FEEDS_FILE"}) if name not in text] == []


# --- default_file ---


def test_default_file_prefers_the_env_var_then_the_current_folder_then_the_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert default_file("feeds.toml", "FEEDS_FILE", env={}) == PROJECT_ROOT / "feeds.toml"
    (tmp_path / "feeds.toml").write_text("")
    assert default_file("feeds.toml", "FEEDS_FILE", env={}) == tmp_path / "feeds.toml"
    assert default_file("feeds.toml", "FEEDS_FILE", env={"FEEDS_FILE": "/etc/my-feeds.toml"}) == Path(
        "/etc/my-feeds.toml"
    )
    monkeypatch.setenv("FEEDS_FILE", str(tmp_path / "other.toml"))
    assert default_file("feeds.toml", "FEEDS_FILE") == tmp_path / "other.toml"
