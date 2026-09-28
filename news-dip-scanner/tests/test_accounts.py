"""The website's accounts: passwords, invites, sessions, password links, rate limits, settings and jobs."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import timedelta

import pytest
from conftest import NOW

from dip_scanner import accounts as accounts_module
from dip_scanner.accounts import (
    CURRENCIES,
    LOGIN_LIMIT,
    AccountError,
    Accounts,
    SettingsError,
    UserSettings,
    check_new_password,
    check_password,
    clean_name,
    hash_password,
    normalise_email,
    same_token,
    token_hash,
    validate_settings,
)
from dip_scanner.config import AccountConfig, AlertConfig, NotifySettings, ScannerConfig, Settings
from dip_scanner.store import Store

PASSWORD = "correct horse battery"


class Clock:
    """A clock the test moves forward by hand."""

    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)


@pytest.fixture(autouse=True)
def fast_scrypt(monkeypatch):
    """Cheap scrypt parameters for speed; the stored hash carries its own, so checking still works."""
    monkeypatch.setattr(accounts_module, "SCRYPT_N", 2**4)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "scanner.sqlite3") as db:
        yield db


@pytest.fixture
def accounts(store, clock):
    return Accounts(store, clock=clock)


def admin(accounts, email="owner@example.com"):
    return accounts.create_user(email, name="Owner", role="admin", password=PASSWORD)


# --- passwords and tokens ------------------------------------------------------------------------------------------


def test_passwords_are_stored_as_salted_scrypt_hashes(monkeypatch):
    monkeypatch.setattr(accounts_module, "SCRYPT_N", 2**14)
    stored = hash_password(PASSWORD)
    scheme, n, r, p, salt, key = stored.split("$")
    assert (scheme, n, r, p) == ("scrypt", "16384", "8", "1")
    assert len(accounts_module._unb64(salt)) == 16 and len(accounts_module._unb64(key)) == 32
    assert hash_password(PASSWORD) != stored  # a new salt every time
    assert check_password(stored, PASSWORD)
    assert not check_password(stored, PASSWORD + "!")


def test_the_same_password_typed_with_decomposed_accents_matches():
    composed = "κωδικός πρόσβασης"  # Greek with composed accents
    decomposed = "κωδικός πρόσβασης"
    assert composed != decomposed
    assert check_password(hash_password(composed), decomposed)


@pytest.mark.parametrize(
    "stored",
    [None, "", "plain-text", "scrypt$16384$8$1$salt", "bcrypt$1$2$3$c2FsdA$aGFzaA", "scrypt$99999999$8$1$c2FsdA$aGFzaA",
     "scrypt$16$8$1$!!!$aGFzaA", "scrypt$16$8$1$c2FsdA$"],
)  # fmt: skip
def test_malformed_hashes_never_match(stored):
    assert not check_password(stored, PASSWORD)


def test_tokens_are_compared_in_constant_time_and_hashed_with_sha256():
    assert token_hash("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert same_token("abc", "abc") and not same_token("abc", "abd")
    assert not same_token(None, "abc") and not same_token("abc", None) and not same_token("", "")


@pytest.mark.parametrize(
    ("password", "message"),
    [
        ("short", "at least 10 characters"),
        (None, "at least 10 characters"),
        ("          ", "only spaces"),
        ("x" * 1025, "too long"),
        ("owner@example.com", "can't be your email address"),
    ],
)
def test_weak_passwords_are_refused(password, message):
    with pytest.raises(AccountError, match=message):
        check_new_password(password, email="Owner@Example.com")


def test_emails_and_names_are_normalised():
    assert normalise_email("  Jane.Doe+dips@Example.COM ") == "jane.doe+dips@example.com"
    for wrong in ("", "jane", "jane@", "@example.com", "jane@example", "ja ne@example.com", "<a>@example.com"):
        with pytest.raises(AccountError):
            normalise_email(wrong)
    assert clean_name("  Jane \n\t Doe\x00‮ ") == "Jane Doe"
    assert len(clean_name("x" * 500)) == 80


# --- users ---------------------------------------------------------------------------------------------------------


def test_users_are_created_found_and_listed(accounts, clock):
    owner = admin(accounts)
    member = accounts.create_user("Friend@Example.com", name=" Friend ")
    assert (owner.id, owner.email, owner.name, owner.role) == (1, "owner@example.com", "Owner", "admin")
    assert owner.is_admin
    assert owner.has_password and not member.has_password and not member.is_admin
    assert owner.created == NOW == owner.alerts_since and owner.last_login is None
    assert owner.recipient_key == "user:1" and member.label == "Friend"
    assert accounts.get_user_by_email("FRIEND@example.com") == member
    assert accounts.get_user_by_email("not an email") is None and accounts.get_user(99) is None
    assert [user.email for user in accounts.list_users()] == ["owner@example.com", "friend@example.com"]
    assert accounts.active_users() == [owner]  # the member has no password yet
    with pytest.raises(AccountError, match="already an account"):
        accounts.create_user("OWNER@example.com")
    with pytest.raises(AccountError, match="role must be"):
        accounts.create_user("x@example.com", role="root")


def test_authenticate_checks_the_password_and_records_the_login(accounts, clock):
    owner = admin(accounts)
    clock.advance(hours=1)
    user = accounts.authenticate("Owner@Example.com", PASSWORD)
    assert user is not None and user.id == owner.id and user.last_login == NOW + timedelta(hours=1)
    assert accounts.authenticate("owner@example.com", "wrong password!") is None
    assert accounts.authenticate("nobody@example.com", PASSWORD) is None
    assert accounts.authenticate("not an email", PASSWORD) is None
    assert accounts.verify_password(owner.id, PASSWORD) and not accounts.verify_password(owner.id, "nope nope nope")
    accounts.create_user("second@example.com", role="admin", password=PASSWORD)
    accounts.set_disabled(owner.id, True)
    assert accounts.authenticate("owner@example.com", PASSWORD) is None


def test_changing_the_password_ends_every_session_and_unused_link(accounts):
    owner = admin(accounts)
    token, _ = accounts.create_session(owner.id)
    link = accounts.create_password_token(owner.id)
    accounts.set_password(owner.id, "a brand new password")
    assert accounts.get_session(token) is None
    assert accounts.get_password_token(link) is None
    assert accounts.authenticate("owner@example.com", "a brand new password") is not None
    with pytest.raises(AccountError, match="at least 10"):
        accounts.set_password(owner.id, "short")


def test_the_last_admin_who_can_sign_in_can_not_be_demoted_or_disabled(accounts):
    owner = admin(accounts)
    member = accounts.create_user("friend@example.com", password=PASSWORD)
    with pytest.raises(AccountError, match="only admin"):
        accounts.set_role(owner.id, "member")
    with pytest.raises(AccountError, match="only admin"):
        accounts.set_disabled(owner.id, True)
    pending = accounts.create_user("pending@example.com", role="admin")  # no password yet: can't sign in
    with pytest.raises(AccountError, match="only admin"):
        accounts.set_role(owner.id, "member")
    assert accounts.set_role(pending.id, "member").role == "member"  # demoting one who can't sign in is fine
    accounts.set_role(member.id, "admin")
    assert accounts.set_role(owner.id, "member").role == "member"
    with pytest.raises(AccountError, match="only admin"):
        accounts.set_disabled(member.id, True)


def test_disabling_a_user_ends_their_sessions_and_blocks_new_ones(accounts):
    admin(accounts)
    member = accounts.create_user("friend@example.com", password=PASSWORD)
    token, _ = accounts.create_session(member.id)
    assert accounts.set_disabled(member.id, True).disabled
    assert accounts.get_session(token) is None
    with pytest.raises(AccountError, match="disabled"):
        accounts.create_session(member.id)
    assert not accounts.set_disabled(member.id, False).disabled


def test_a_user_disabled_elsewhere_is_signed_out_on_the_next_request(accounts, store):
    admin(accounts)
    member = accounts.create_user("friend@example.com", password=PASSWORD)
    first, _ = accounts.create_session(member.id)
    second, _ = accounts.create_session(member.id)
    with store.transaction() as conn:  # e.g. by the command line, which doesn't touch the sessions
        conn.execute("UPDATE users SET disabled = 1 WHERE id = ?", (member.id,))
    assert accounts.get_session(first) is None
    assert store.query("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (member.id,))[0][0] == 0
    assert accounts.get_session(second) is None


# --- sessions ------------------------------------------------------------------------------------------------------


def test_sessions_store_only_the_token_hash_and_slide_for_30_days(accounts, store, clock):
    owner = admin(accounts)
    token, session = accounts.create_session(owner.id, ip="203.0.113.9", user_agent="Mozilla/5.0 " + "x" * 400)
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", token)
    [row] = store.query("SELECT * FROM sessions")
    assert row["token_hash"] == token_hash(token) != token and token not in json.dumps(dict(row))
    assert session.user == owner and session.ip == "203.0.113.9" and len(session.user_agent) == 300
    assert len(session.csrf) >= 43 and session.csrf != token
    assert session.expires == NOW + timedelta(days=30)

    clock.advance(days=29)
    again = accounts.get_session(token)
    assert again is not None and again.expires == clock.now + timedelta(days=30)  # used: extended
    assert again.csrf == session.csrf
    clock.advance(minutes=1)
    assert accounts.get_session(token).expires == again.expires  # written at most every few minutes
    clock.advance(days=30, minutes=1)
    assert accounts.get_session(token) is None  # unused for 30 days: gone
    assert store.query("SELECT COUNT(*) FROM sessions")[0][0] == 0


def test_unknown_and_revoked_sessions(accounts):
    owner = admin(accounts)
    assert accounts.get_session(None) is None and accounts.get_session("") is None
    assert accounts.get_session("made-up-token") is None
    token, _ = accounts.create_session(owner.id)
    other, _ = accounts.create_session(owner.id)
    third, _ = accounts.create_session(owner.id)
    assert accounts.revoke_session(token) and not accounts.revoke_session(token)
    assert accounts.get_session(token) is None
    assert {s.token_hash for s in accounts.list_sessions(owner.id)} == {token_hash(other), token_hash(third)}
    assert accounts.revoke_sessions(owner.id, keep=other) == 1
    assert accounts.get_session(other) is not None and accounts.get_session(third) is None
    assert accounts.revoke_sessions(owner.id) == 1


# --- invites -------------------------------------------------------------------------------------------------------


def test_an_invite_is_single_use_and_creates_the_account(accounts, store, clock):
    owner = admin(accounts)
    token = accounts.create_invite(created_by=owner.id, role="member")
    assert store.query("SELECT COUNT(*) FROM invites WHERE token_hash = ?", (token,))[0][0] == 0  # only the hash
    invite = accounts.get_invite(token)
    assert invite is not None and invite.email is None and invite.expires == NOW + timedelta(days=7)
    assert [i.token_hash for i in accounts.list_invites()] == [invite.token_hash]

    clock.advance(hours=1)
    user = accounts.accept_invite(token, name="Friend", password=PASSWORD, email="Friend@Example.com")
    assert (user.email, user.name, user.role, user.has_password) == ("friend@example.com", "Friend", "member", True)
    assert user.alerts_since == NOW + timedelta(hours=1)
    assert accounts.get_invite(token) is None and accounts.list_invites() == []
    assert accounts.list_invites(pending_only=False)[0].status(clock.now) == "used"
    with pytest.raises(AccountError, match="expired or was already used"):
        accounts.accept_invite(token, name="Again", password=PASSWORD, email="again@example.com")


def test_an_invite_for_an_address_only_creates_that_account(accounts):
    owner = admin(accounts)
    token = accounts.create_invite(created_by=owner.id, email="Boss@Example.com", role="admin")
    user = accounts.accept_invite(token, name="Boss", password=PASSWORD, email="someone.else@example.com")
    assert (user.email, user.role) == ("boss@example.com", "admin")
    with pytest.raises(AccountError, match="already an account"):
        accounts.create_invite(created_by=owner.id, email="boss@example.com")


def test_invites_expire_and_can_be_revoked(accounts, clock):
    owner = admin(accounts)
    expired = accounts.create_invite(created_by=owner.id)
    clock.advance(days=7)
    assert accounts.get_invite(expired) is None
    with pytest.raises(AccountError, match="expired"):
        accounts.accept_invite(expired, name="x", password=PASSWORD, email="x@example.com")
    revoked = accounts.create_invite(created_by=None)
    invite = accounts.get_invite(revoked)
    assert accounts.revoke_invite(invite.token_hash) and not accounts.revoke_invite(invite.token_hash)
    assert accounts.get_invite(revoked) is None
    assert {i.status(clock.now) for i in accounts.list_invites(pending_only=False)} == {"expired", "revoked"}


def test_accepting_an_invite_checks_the_password_and_the_email(accounts):
    owner = admin(accounts)
    token = accounts.create_invite(created_by=owner.id)
    with pytest.raises(AccountError, match="at least 10"):
        accounts.accept_invite(token, name="x", password="short", email="x@example.com")
    with pytest.raises(AccountError, match="isn't an email address"):
        accounts.accept_invite(token, name="x", password=PASSWORD, email="nope")
    with pytest.raises(AccountError, match="sign in instead"):
        accounts.accept_invite(token, name="x", password=PASSWORD, email="owner@example.com")
    assert accounts.get_invite(token) is not None  # nothing was used up by the failures


# --- password links ------------------------------------------------------------------------------------------------


def test_a_setup_link_sets_the_first_password_once(accounts, clock):
    user = accounts.create_user("owner@example.com", role="admin")
    token = accounts.create_password_token(user.id)
    found = accounts.get_password_token(token)
    assert found is not None and found.purpose == "setup" and found.expires == NOW + timedelta(hours=48)
    assert accounts.use_password_token(token, PASSWORD).has_password
    assert accounts.get_password_token(token) is None
    with pytest.raises(AccountError, match="expired or was already used"):
        accounts.use_password_token(token, "another password!")
    assert accounts.create_password_token(user.id) != token
    assert accounts.get_password_token(accounts.create_password_token(user.id)).purpose == "reset"


def test_password_links_expire_after_48_hours_and_need_an_enabled_user(accounts, clock):
    owner = admin(accounts)
    member = accounts.create_user("friend@example.com", password=PASSWORD)
    old = accounts.create_password_token(member.id)
    clock.advance(hours=48)
    assert accounts.get_password_token(old) is None
    fresh = accounts.create_password_token(member.id)
    accounts.set_disabled(member.id, True)
    assert accounts.get_password_token(fresh) is None
    with pytest.raises(AccountError, match="expired"):
        accounts.use_password_token(fresh, "a new password here")
    with pytest.raises(ValueError, match="Unknown purpose"):
        accounts.create_password_token(owner.id, "hack")


def test_a_reset_link_ends_the_sessions(accounts):
    owner = admin(accounts)
    session, _ = accounts.create_session(owner.id)
    accounts.use_password_token(accounts.create_password_token(owner.id), "a fresh password")
    assert accounts.get_session(session) is None


# --- rate limits ---------------------------------------------------------------------------------------------------


def test_ten_failed_logins_in_15_minutes_block_the_ip_or_the_email(accounts, clock):
    keys = Accounts.login_keys("203.0.113.9", " Owner@Example.com ")
    assert keys == ["ip:203.0.113.9", "email:owner@example.com"]
    for _ in range(LOGIN_LIMIT - 1):
        accounts.record_login_failure(*keys)
    assert not accounts.too_many_attempts(*keys)
    accounts.record_login_failure(*keys)
    assert accounts.too_many_attempts(*keys)
    assert accounts.too_many_attempts("email:owner@example.com")  # from any other address too
    assert not accounts.too_many_attempts("ip:198.51.100.1", "email:other@example.com")
    clock.advance(minutes=15, seconds=1)
    assert not accounts.too_many_attempts(*keys)
    accounts.record_login_failure(*keys)
    accounts.clear_attempts("email:owner@example.com")
    assert accounts.login_keys(None, "") == []


def test_rate_limited_counts_and_refuses_over_the_limit(accounts, clock):
    for _ in range(3):
        assert not accounts.rate_limited("test-alert:1", limit=3, window=timedelta(hours=1))
    assert accounts.rate_limited("test-alert:1", limit=3, window=timedelta(hours=1))
    assert not accounts.rate_limited("test-alert:2", limit=3, window=timedelta(hours=1))
    clock.advance(hours=1, seconds=1)
    assert not accounts.rate_limited("test-alert:1", limit=3, window=timedelta(hours=1))


# --- settings ------------------------------------------------------------------------------------------------------

SERVER = Settings(
    notify=NotifySettings(smtp_host="smtp.example.com", smtp_from="dips@example.com", telegram_bot_token="123:abc")
)


def check(data, *, current=None, settings=SERVER, resolver=lambda host, port: ["34.120.1.2"]):
    return validate_settings(data, current=current or UserSettings(), settings=settings, resolver=resolver)


def test_defaults_follow_scanner_toml_and_display_tz():
    from zoneinfo import ZoneInfo

    config = ScannerConfig(
        alerts=AlertConfig(min_score=55, min_probability=70, verdicts=("temporary_fear",)),
        account=AccountConfig(currency="EUR"),
    )
    defaults = UserSettings.defaults(config, Settings(display_tz=ZoneInfo("Europe/Athens")))
    assert (defaults.min_score, defaults.min_probability, defaults.verdicts) == (55.0, 70, ("temporary_fear",))
    assert defaults.timezone == "Europe/Athens" and defaults.currency is None and not defaults.has_channel


def test_a_whole_settings_form_is_checked_and_normalised():
    form = {
        "watchlist": "brk.b, $amd\nNASDAQ:tsla  ΕΤΕ.ΑΤ;amd",
        "min_score": "57,5",
        "min_probability": "70",
        "verdicts": ["temporary_fear", "Mixed"],
        "only_watchlist": "on",
        "thesis_changes": "",
        "email_alerts": "true",
        "telegram_chat_id": " -1001234567 ",
        "webhook_url": " https://hooks.slack.com/services/T0/B0/XYZ ",
        "webhook_format": "Discord",
        "currency": "eur",
        "timezone": "europe/athens",
        "unknown_key": "ignored",
    }
    result = check(form)
    assert result == UserSettings(
        watchlist=("BRK-B", "AMD", "TSLA", "ETE.AT"),
        min_score=57.5,
        min_probability=70,
        verdicts=("temporary_fear", "mixed"),
        only_watchlist=True,
        thesis_changes=False,
        email_alerts=True,
        telegram_chat_id="-1001234567",
        webhook_url="https://hooks.slack.com/services/T0/B0/XYZ",
        webhook_format="discord",
        currency="EUR",
        timezone="Europe/Athens",
    )
    assert result.has_channel
    assert check({"telegram_chat_id": "", "webhook_url": "", "currency": ""}, current=result).webhook_url is None


def test_keys_left_out_keep_their_current_value():
    current = UserSettings(watchlist=("AMD",), min_score=70, email_alerts=True)
    assert check({"min_probability": 65}, current=current) == replace(current, min_probability=65)


def test_every_wrong_field_is_named():
    with pytest.raises(SettingsError) as error:
        check(
            {
                "watchlist": "AMD, ^GSPC, SPY, N/A",
                "min_score": "lots",
                "min_probability": "60.5",
                "verdicts": ["temporary_fear", "moonshot"],
                "only_watchlist": "maybe",
                "telegram_chat_id": "my chat",
                "webhook_url": "http://hooks.slack.com/x",
                "webhook_format": "teams",
                "currency": "GBX",
                "timezone": "Athens",
            }
        )
    errors = error.value.errors
    assert set(errors) == {
        "watchlist",
        "min_score",
        "min_probability",
        "verdicts",
        "only_watchlist",
        "telegram_chat_id",
        "webhook_url",
        "webhook_format",
        "currency",
        "timezone",
    }
    assert errors["watchlist"].startswith("Not a company's Yahoo Finance symbol: ^GSPC, SPY, N/A.")
    assert errors["min_score"] == "The minimum score must be a number from 0 to 100."
    assert errors["min_probability"] == "The minimum chance must be a whole number from 0 to 100."
    assert "moonshot" in errors["verdicts"]
    assert "https://" in errors["webhook_url"]
    assert "choose GBP" in errors["currency"]
    assert "Europe/Athens" in errors["timezone"]
    with pytest.raises(SettingsError, match="at least one verdict"):
        check({"verdicts": []})
    with pytest.raises(SettingsError) as odd:  # values that no form would send
        check({"watchlist": 5, "min_score": None, "verdicts": 3})
    assert odd.value.errors == {
        "watchlist": "The watchlist setting isn't valid.",
        "min_score": "The minimum score must be a number from 0 to 100.",
        "verdicts": "The verdicts setting isn't valid.",
    }
    with pytest.raises(SettingsError, match="isn't one of them"):
        check({"currency": "XYZ"})
    with pytest.raises(SettingsError, match="at most 100"):
        check({"watchlist": [f"T{n}" for n in range(101)]})
    assert set(CURRENCIES) >= {"EUR", "USD", "GBP", "CHF"}


def test_a_private_webhook_address_is_refused_when_saved():
    with pytest.raises(SettingsError) as error:
        check({"webhook_url": "https://rebind.example.com/hook"}, resolver=lambda host, port: ["10.1.2.3"])
    assert "public internet" in error.value.errors["webhook_url"]


def test_channels_the_server_can_not_serve_are_refused():
    bare = Settings()
    with pytest.raises(SettingsError) as error:
        check({"email_alerts": "on", "telegram_chat_id": "12345"}, settings=bare)
    assert error.value.errors == {
        "email_alerts": "Email alerts aren't available: the server has no email (SMTP) settings.",
        "telegram_chat_id": "Telegram alerts aren't available: the server has no Telegram bot.",
    }
    # Switching them off, or saving other fields, is always possible.
    current = UserSettings(email_alerts=True, telegram_chat_id="12345")
    assert check({"min_score": 70}, current=current, settings=bare).min_score == 70
    assert not check({"email_alerts": "off"}, current=current, settings=bare).email_alerts


def test_stored_settings_load_leniently():
    defaults = UserSettings(min_score=60, timezone="Europe/Athens")
    stored = {
        "watchlist": ["AMD", "AMD", "SAP.DE"],
        "min_score": 72,
        "min_probability": "high",  # damaged: the default
        "verdicts": ["temporary_fear", "nonsense"],  # damaged: the default
        "email_alerts": True,
        "timezone": "Mars/Olympus",  # damaged: the default
        "webhook_format": "teams",
        "future_setting": 1,
    }
    loaded = UserSettings.from_dict(stored, defaults)
    assert loaded == replace(defaults, watchlist=("AMD", "SAP.DE"), min_score=72.0, email_alerts=True)
    assert UserSettings.from_dict("not a dict", defaults) == defaults
    assert UserSettings.from_dict(loaded.to_dict()) == loaded
    assert json.loads(json.dumps(loaded.to_dict()))["watchlist"] == ["AMD", "SAP.DE"]


def test_saved_settings_round_trip_and_alerts_start_with_the_first_channel(accounts, store, clock):
    accounts.defaults = UserSettings(min_score=60)
    owner = admin(accounts)
    assert owner.settings == UserSettings(min_score=60)  # nothing saved: the defaults
    clock.advance(hours=2)
    saved = accounts.update_settings(owner.id, replace(owner.settings, watchlist=("AMD",)))
    assert saved.settings.watchlist == ("AMD",) and saved.alerts_since == NOW  # no channel yet
    clock.advance(hours=1)
    saved = accounts.update_settings(owner.id, replace(saved.settings, telegram_chat_id="42"))
    assert saved.alerts_since == NOW + timedelta(hours=3)
    clock.advance(hours=1)
    saved = accounts.update_settings(owner.id, replace(saved.settings, email_alerts=True))
    assert saved.alerts_since == NOW + timedelta(hours=3)  # already had a channel
    with store.transaction() as conn:
        conn.execute("UPDATE users SET settings = 'not json' WHERE id = ?", (owner.id,))
    assert accounts.get_user(owner.id).settings == UserSettings(min_score=60)


# --- jobs ----------------------------------------------------------------------------------------------------------


def test_jobs_are_queued_run_and_finished(accounts, clock):
    owner = admin(accounts)
    job = accounts.create_job(owner.id, " amd ")
    assert (job.status, job.ticker, job.kind, job.done, job.finished) == ("queued", "AMD", "analyze", False, None)
    assert accounts.start_job(job.id).status == "running"
    clock.advance(minutes=2)
    done = accounts.finish_job(job.id, opportunity_id=7)
    assert (done.status, done.opportunity_id, done.finished, done.done) == ("done", 7, NOW + timedelta(minutes=2), True)
    failed = accounts.finish_job(accounts.create_job(owner.id, "NOSUCH").id, error="No prices for NOSUCH.")
    assert (failed.status, failed.error) == ("failed", "No prices for NOSUCH.")
    assert [j.id for j in accounts.list_jobs(user_id=owner.id)] == [failed.id, job.id]
    assert accounts.get_job(999) is None
    with pytest.raises(ValueError, match="Unknown job kind"):
        accounts.create_job(owner.id, "AMD", kind="mine")


def test_jobs_count_per_user_over_24_hours_and_restarts_fail_unfinished_ones(accounts, clock):
    owner = admin(accounts)
    member = accounts.create_user("friend@example.com", password=PASSWORD)
    accounts.create_job(member.id, "AMD")
    clock.advance(hours=20)
    accounts.create_job(member.id, "NVDA")
    accounts.create_job(owner.id, "SAP.DE")
    assert accounts.count_jobs(member.id) == 2 and accounts.count_jobs(owner.id) == 1
    clock.advance(hours=5)
    assert accounts.count_jobs(member.id) == 1
    assert accounts.count_jobs(member.id, since=NOW - timedelta(days=1)) == 2
    assert accounts.fail_unfinished_jobs() == 3
    assert {job.status for job in accounts.list_jobs()} == {"failed"}
    assert "restart" in accounts.list_jobs()[0].error
