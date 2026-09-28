"""The JSON API under /api/v1 (web/api.py) with FastAPI's TestClient: every answer is validated against the front
end's contract, frontend/contract/api-v1.schema.json (JSON Schema draft 2020-12, formats checked), on databases
seeded like the member pages' tests; plus signing in, the CSRF header, the Origin check, the limits and the errors.

No network (fake prices and exchange rates) and no sleeping: the clock is fixed and analyses run inline.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from conftest import make_analysis, make_debate, make_opportunity, make_stats
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from test_web_pages import (  # noqa: F401 (fast_scrypt and no_network are autouse fixtures)
    BASE,
    NOW,
    TODAY,
    FakePrices,
    Site,
    bars_until,
    fast_scrypt,
    no_network,
    seed,
    standard_prices,
)

from dip_scanner.config import PROJECT_ROOT, ScannerConfig, UniverseConfig
from dip_scanner.models import Participant
from dip_scanner.web import jobs as jobs_module
from dip_scanner.web import pages

CONTRACT = PROJECT_ROOT / "frontend" / "contract" / "api-v1.schema.json"
SCHEMA = json.loads(CONTRACT.read_text(encoding="utf-8"))
ENDPOINTS = ["/api/v1/me", "/api/v1/status", "/api/v1/ideas", "/api/v1/ideas/1", "/api/v1/jobs/1"]
ENDPOINTS += ["/api/v1/thesis-changes"]


def problems(body: Any, definition: str) -> list[str]:
    """What is wrong with body as the contract's definition (formats such as date-time included)."""
    validator = Draft202012Validator(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$defs": SCHEMA["$defs"],
            "$ref": f"#/$defs/{definition}",
        },
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )
    return [f"{'/'.join(map(str, error.absolute_path))}: {error.message}" for error in validator.iter_errors(body)]


def valid(response, definition: str, status: int = 200) -> dict:
    """The response's JSON, once its status, its type and its shape are right."""
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/json"
    body = response.json()
    assert problems(body, definition) == []
    return body


def error_of(response, status: int, code: str) -> dict:
    body = valid(response, "Error", status)
    assert body["error"]["code"] == code, body
    return body["error"]


def csrf(client: TestClient) -> str:
    return client.get("/api/v1/me").json()["csrf"]


def post(client: TestClient, path: str, **headers: str):
    return client.post(path, headers={"x-csrf-token": csrf(client), "origin": BASE, **headers})


def eur_reader(site: Site, email: str = "eur@example.com", **settings) -> TestClient:
    return site.client(email, currency="EUR", **settings)


@pytest.fixture
def site(tmp_path) -> Site:
    return Site(tmp_path)


@pytest.fixture
def seeded(site) -> tuple[Site, dict[str, int]]:
    return site, seed(site)


def analysing_site(tmp_path, **web) -> Site:
    """A site whose "Analyse now" works: the analysis is a copy of an AMD idea made now."""

    def analyse(ticker, now):
        return make_opportunity(ticker=ticker, created=now, stats=make_stats(ticker=ticker, as_of=now))

    return Site(tmp_path, analyse=analyse, **web)


# --- the contract itself -------------------------------------------------------------------------------------------


def test_the_contract_is_a_valid_schema_and_checks_formats():
    Draft202012Validator.check_schema(SCHEMA)
    assert SCHEMA["$id"] == "urn:dip-scanner:api-v1"
    # date-time is really checked (it needs rfc3339-validator, from jsonschema[format]).
    assert problems("2026-09-25T15:00:00+00:00", "DateTime") == []
    assert problems("25 Sep 2026", "DateTime") and problems("2026-09-25", "DateTime")
    assert problems("2026-09-25", "Day") == [] and problems("2026-9-25", "Day")


def test_every_endpoint_of_the_contract_is_served(site):
    paths = site.app.openapi()["paths"]
    served = {(method.upper(), path) for path, item in paths.items() if path.startswith("/api/") for method in item}
    wanted = {tuple(key.split(" ", 1)) for key in SCHEMA["x-endpoints"] if key.split(" ", 1)[0] in ("GET", "POST")}

    def shape(pairs):
        return {(method, re.sub(r"\{[^}]+\}", "{}", path)) for method, path in pairs}

    assert len(wanted) == 7 and shape(wanted) == shape(served)


# --- signing in ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", [*ENDPOINTS, "/api/v1/thesis-changes?days=3"])
def test_every_endpoint_needs_signing_in(site, path):
    anonymous = TestClient(site.app, base_url=BASE, follow_redirects=False)
    error = error_of(anonymous.get(path), 401, "not_signed_in")
    assert error["message"] == "Sign in to continue." and error["retry_after"] is None
    stale = TestClient(site.app, base_url=BASE, follow_redirects=False, cookies={"dsid": "not-a-session"})
    response = stale.get(path)
    error_of(response, 401, "not_signed_in")
    assert 'dsid=""' in response.headers.get("set-cookie", "")  # the stale cookie is cleared, as on the pages


def test_a_disabled_user_is_signed_out(site):
    client = site.client()
    site.accounts.set_disabled(site.user().id, True)
    error_of(client.get("/api/v1/me"), 401, "not_signed_in")


def test_answers_are_never_cached_and_carry_the_security_headers(seeded):
    site, _ = seeded
    response = site.client().get("/api/v1/ideas")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


# --- /me -----------------------------------------------------------------------------------------------------------


def test_me_describes_the_reader(site):
    client = site.client(
        "nikos@example.com",
        currency="EUR",
        timezone="Europe/Athens",
        watchlist=("META", "ALWN.AT"),
        min_score=55.0,
        webhook_url="https://hooks.slack.com/services/T0/B0/x",
    )
    site.accounts.set_name(site.user("nikos@example.com").id, "Nikos")
    me = valid(client.get("/api/v1/me"), "Me")
    assert me["user"] == {
        "id": site.user("nikos@example.com").id,
        "email": "nikos@example.com",
        "name": "Nikos",
        "label": "Nikos",
        "role": "member",
    }
    assert me["settings"] == {
        "currency": "EUR",
        "timezone": "Europe/Athens",
        "watchlist": ["META", "ALWN.AT"],
        "alert_rules": {
            "min_score": 55.0,
            "min_probability": 60,
            "verdicts": ["temporary_fear", "mixed"],
            "only_watchlist": False,
            "thesis_changes": True,
        },
        "has_alert_channel": True,
    }
    session = site.accounts.list_sessions(site.user("nikos@example.com").id)[0]
    assert me["csrf"] == session.csrf
    # No analyses on this site (no model): the button says why.
    assert me["capabilities"] == {
        "admin": False,
        "analyse": {
            "available": False,
            "limit": 5,
            "remaining": 5,
            "note": "Manual analyses aren't available on this server.",
        },
    }


def test_me_for_an_admin_and_for_a_member_with_analyses(tmp_path):
    site = analysing_site(tmp_path)
    admin = valid(site.client("admin@example.com", role="admin").get("/api/v1/me"), "Me")
    assert admin["user"]["role"] == "admin" and admin["user"]["label"] == "admin@example.com"
    assert admin["capabilities"] == {
        "admin": True,
        "analyse": {"available": True, "limit": None, "remaining": None, "note": None},
    }
    assert admin["settings"]["currency"] is None and admin["settings"]["has_alert_channel"] is False
    member = valid(site.client().get("/api/v1/me"), "Me")
    assert member["capabilities"]["analyse"] == {"available": True, "limit": 5, "remaining": 5, "note": None}


# --- /status -------------------------------------------------------------------------------------------------------


def test_status_before_any_cycle(site):
    status = valid(site.client().get("/api/v1/status"), "Status")
    assert status["state"] == "disabled" and status["label"] == "Scanner off"
    assert "doesn't run in this process" in status["reason"]
    assert status["last_cycle"] is None and status["feeds"] is None and status["next_cycle_at"] is None
    assert status["interval_minutes"] == 5
    assert status["model_today"] == {
        "calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "since": "2026-09-25T00:00:00+00:00",
    }


def test_status_shows_the_last_cycle_the_feeds_and_the_models_use_today(site):
    site.store.record_cycle(
        started=NOW - timedelta(minutes=4),
        finished=NOW - timedelta(minutes=3),
        summary="Cycle 14:56: 14 new articles, 1 opportunity",
        stats={"feeds_ok": 19, "feeds_failed": 1},
    )
    site.store.record_model_call(when=NOW - timedelta(hours=1), step="triage", model="gpt-5-mini", input_tokens=1500)
    site.store.record_model_call(
        when=NOW - timedelta(hours=2), step="analysis:opening", model="gpt-5", input_tokens=3500, output_tokens=900
    )
    site.store.record_model_call(when=NOW - timedelta(hours=20), step="triage", model="gpt-5-mini", input_tokens=9)
    status = valid(site.client().get("/api/v1/status"), "Status")
    assert status["last_cycle"] == {
        "started": "2026-09-25T14:56:00+00:00",
        "finished": "2026-09-25T14:57:00+00:00",
        "ok": True,
        "summary": "Cycle 14:56: 14 new articles, 1 opportunity",
    }
    assert status["feeds"] == {"ok": 19, "total": 20}
    assert status["model_today"]["calls"] == 2
    assert (status["model_today"]["input_tokens"], status["model_today"]["output_tokens"]) == (5000, 900)
    # A failed cycle has no feed counts.
    site.store.record_cycle(started=NOW - timedelta(minutes=1), finished=None, summary="Cycle failed: boom", ok=False)
    status = valid(site.client().get("/api/v1/status"), "Status")
    assert status["feeds"] is None and status["last_cycle"]["ok"] is False and status["last_cycle"]["finished"] is None


# --- /ideas --------------------------------------------------------------------------------------------------------


def tickers(body: dict) -> list[str]:
    return [idea["ticker"] for idea in body["ideas"]]


def test_the_ideas_list_follows_the_dashboard(seeded):
    site, ids = seeded
    client = site.client()
    body = valid(client.get("/api/v1/ideas"), "IdeasList")
    assert tickers(body) == ["SAP.DE", "AMD"]  # best score first; NVDA is 20 days old
    assert body["filters"] == {
        "days": 7,
        "min_score": None,
        "verdict": None,
        "watchlist": False,
        "matching": False,
        "sort": "score",
    }
    assert (body["total"], body["count"], body["page"]) == (2, 2, {"number": 1, "pages": 1, "size": 25})
    assert body["generated_at"] == "2026-09-25T15:00:00+00:00"
    amd = body["ideas"][1]
    assert amd["id"] == ids["amd"] and amd["analyses_count"] == 2 and amd["superseded"] is False
    assert amd["superseded_by"] is None and amd["age_seconds"] == 7200
    assert (amd["verdict"], amd["verdict_label"], amd["score"], amd["score_band"]) == (
        "fundamental",
        "Fundamental damage",
        20.3,
        "weak",
    )
    assert amd["fx"] is None and amd["price"]["approx"] is None and amd["debate"] is None
    assert tickers(valid(client.get("/api/v1/ideas?days=30"), "IdeasList")) == ["NVDA", "SAP.DE", "AMD"]
    assert tickers(valid(client.get("/api/v1/ideas?sort=new"), "IdeasList")) == ["AMD", "SAP.DE"]


def test_ideas_filters(seeded):
    site, _ = seeded
    client = site.client(watchlist=("SAP.DE",))
    assert tickers(client.get("/api/v1/ideas?min_score=65").json()) == ["SAP.DE"]
    assert tickers(client.get("/api/v1/ideas?verdict=fundamental").json()) == ["AMD"]
    assert tickers(client.get("/api/v1/ideas?watchlist=1").json()) == ["SAP.DE"]
    matching = valid(client.get("/api/v1/ideas?matching=1&days=30"), "IdeasList")
    assert tickers(matching) == ["NVDA", "SAP.DE"]  # default rules: score 65+, chance 60%+, fear or mixed
    assert (matching["total"], matching["count"]) == (3, 2)
    assert all(idea["matches_my_rules"] for idea in matching["ideas"])
    sap = matching["ideas"][1]
    assert sap["on_my_watchlist"] is True and sap["exchange"] == "XETRA" and sap["currency"] == "EUR"
    empty = valid(client.get("/api/v1/ideas?min_score=80"), "IdeasList")
    assert empty["ideas"] == [] and empty["count"] == 0 and empty["page"] == {"number": 1, "pages": 1, "size": 25}


def test_invalid_ideas_filters_fall_back_to_the_defaults(seeded):
    site, _ = seeded
    body = valid(
        site.client().get("/api/v1/ideas?days=abc&min_score=12&verdict=<x>&sort=up&watchlist=maybe&page=-3"),
        "IdeasList",
    )
    assert tickers(body) == ["SAP.DE", "AMD"]
    assert body["filters"] == {
        "days": 7,
        "min_score": None,
        "verdict": None,
        "watchlist": False,
        "matching": False,
        "sort": "score",
    }
    assert body["page"]["number"] == 1


def test_ideas_come_a_page_at_a_time(site):
    for n in range(30):
        site.add(ticker=f"T{n:02d}", company=f"Company {n}", created=NOW - timedelta(hours=n + 1), score=40.0 + n)
    client = site.client()
    first = valid(client.get("/api/v1/ideas"), "IdeasList")
    assert first["page"] == {"number": 1, "pages": 2, "size": 25} and len(first["ideas"]) == 25
    assert first["ideas"][0]["ticker"] == "T29" and first["ideas"][0]["score_band"] == "good"
    second = valid(client.get("/api/v1/ideas?page=2"), "IdeasList")
    assert tickers(second) == ["T04", "T03", "T02", "T01", "T00"]
    assert valid(client.get("/api/v1/ideas?page=99"), "IdeasList")["page"]["number"] == 2


def test_ideas_show_amounts_in_the_readers_currency_at_the_stored_rate(site):
    stored = site.add(created=NOW - timedelta(hours=1), fx_rates={"EUR": 0.8})
    site.add(
        ticker="SAP.DE",
        company="SAP SE",
        currency="EUR",
        created=NOW - timedelta(hours=2),
        stats=make_stats(ticker="SAP.DE", currency="EUR", price=190.0),
        price=190.0,
    )
    body = valid(eur_reader(site).get("/api/v1/ideas"), "IdeasList")
    amd = next(idea for idea in body["ideas"] if idea["id"] == stored.id)
    analysis = stored.analysis
    assert amd["price"] == {"amount": stored.price, "approx": round(stored.price * 0.8, 4)}
    assert amd["entry"] == {"amount": analysis.entry_price, "approx": round(analysis.entry_price * 0.8, 4)}
    assert amd["fx"] == {
        "currency": "EUR",
        "rate": 0.8,
        "main_currency": "USD",
        "rate_main_unit": 0.8,
        "source": "analysis",
        "as_of": "2026-09-25T14:00:00+00:00",
        "note": "1 USD = 0.8 EUR at the analysis (Yahoo Finance); your broker's rate and conversion fee differ",
    }
    sap = next(idea for idea in body["ideas"] if idea["ticker"] == "SAP.DE")
    assert sap["fx"] is None and sap["price"] == {"amount": 190.0, "approx": None}  # already in euros


def test_pence_convert_per_penny(site):
    stats = make_stats(ticker="BARC.L", currency="GBp", price=250.0, exchange="LSE")
    site.add(
        ticker="BARC.L",
        company="Barclays PLC",
        currency="GBp",
        price=250.0,
        stats=stats,
        analysis=make_analysis(potential_low=200.0, entry_price=230.0, target_price=300.0),
        fx_rates={"EUR": 0.0115},
    )
    idea = valid(eur_reader(site).get("/api/v1/ideas"), "IdeasList")["ideas"][0]
    assert idea["currency"] == "GBp" and idea["price"] == {"amount": 250.0, "approx": 2.875}
    assert (idea["fx"]["rate"], idea["fx"]["main_currency"], idea["fx"]["rate_main_unit"]) == (0.0115, "GBP", 1.15)


def test_a_debated_idea_carries_its_debate_line(site):
    site.add(debate=make_debate(), analysis=make_analysis(verdict="mixed", probability_up_6m=64))
    idea = valid(site.client().get("/api/v1/ideas"), "IdeasList")["ideas"][0]
    assert idea["debate"] == {
        "mode": "debate",
        "agreement": "medium",
        "line": "GPT-5 66% · Claude Sonnet 5 58% → 64% (medium agreement)",
        "participants": [
            {
                "label": "B",
                "model": "openai:gpt-5",
                "model_label": "GPT-5",
                "opening_probability": 72,
                "final_probability": 66,
                "final_verdict": "mixed",
                "changed_mind": True,
            },
            {
                "label": "A",
                "model": "anthropic:claude-sonnet-5",
                "model_label": "Claude Sonnet 5",
                "opening_probability": 58,
                "final_probability": 58,
                "final_verdict": "mixed",
                "changed_mind": False,
            },
        ],
        "final_probability": 64,
        "judge": "anthropic:claude-sonnet-5",
        "judge_label": "Claude Sonnet 5",
    }


# --- /ideas/{id} ---------------------------------------------------------------------------------------------------


def test_an_idea_in_full(seeded):
    site, ids = seeded
    client = site.client()
    body = valid(client.get(f"/api/v1/ideas/{ids['sap']}"), "IdeaDetail")
    assert body["idea"]["id"] == ids["sap"] and body["idea"]["analyses_count"] == 1
    assert body["opportunity"] == site.store.get_opportunity(ids["sap"]).to_dict()
    assert {level["key"] for level in body["levels"]} == {"target", "price", "entry", "stat_low", "potential_low"}
    values = [level["value"]["amount"] for level in body["levels"]]
    assert values == sorted(values, reverse=True)  # highest first
    target = next(level for level in body["levels"] if level["key"] == "target")
    assert target == {
        "key": "target",
        "label": "Target (limit sell idea)",
        "short_label": "Target",
        "value": {"amount": 225.0, "approx": None},
        "change_pct": round((225 / 190 - 1) * 100, 4),
        "from_entry_pct": 25.0,
    }
    price = next(level for level in body["levels"] if level["key"] == "price")
    assert price["change_pct"] is None and price["from_entry_pct"] is None and price["short_label"] == "Reported"
    low = next(level for level in body["levels"] if level["key"] == "potential_low")
    assert low["from_entry_pct"] == round((160 / 180 - 1) * 100, 4) and low["short_label"] == "Low"
    assert body["fx_note"] is None  # the reader has no currency of their own
    chart = body["chart"]
    assert chart["currency"] == "EUR" and chart["signal_day"] == "2026-09-24" and chart["split_factor"] == 1.0
    assert chart["levels"]["reported"] == 190.0 and chart["levels"]["target"] == 225.0
    assert chart["closes"][-1] == [TODAY.isoformat(), site.prices.bars["SAP.DE"][-1].close]
    assert chart["split_note"] is None
    days = [day for day, _ in chart["closes"]]
    assert days == sorted(days) and days[0] <= (TODAY - timedelta(days=183)).isoformat()
    outcome = body["outcome"]
    assert outcome["priced"] is True and outcome["benchmark"] == "^GDAXI" and outcome["benchmark_name"] == "DAX"
    assert outcome["status_label"] and outcome["last_day"] == TODAY.isoformat()
    assert body["prices_problem"] is None and body["outcome_notes"] == [] and body["debate"] is None
    assert body["history"] == [
        {
            "id": ids["sap"],
            "created": "2026-09-24T15:00:00+00:00",
            "verdict": "mixed",
            "verdict_label": "Mixed",
            "score": 66.0,
            "score_band": "good",
            "probability_up_6m": 74,
            "superseded": False,
            "current": True,
        }
    ]
    assert body["rule_misses"] == []


def test_a_superseded_idea_points_at_the_newer_one(seeded):
    site, ids = seeded
    body = valid(site.client().get(f"/api/v1/ideas/{ids['amd_old']}"), "IdeaDetail")
    assert body["idea"]["superseded"] is True and body["idea"]["superseded_by"] == ids["amd"]
    assert body["idea"]["analyses_count"] == 2
    assert [(item["id"], item["superseded"], item["current"]) for item in body["history"]] == [
        (ids["amd"], False, False),
        (ids["amd_old"], True, True),
    ]
    newest = valid(site.client().get(f"/api/v1/ideas/{ids['amd']}"), "IdeaDetail")
    assert newest["rule_misses"] == [
        "its score 20.3 is under your 65",
        "its chance up of 30% is under your 60%",
        "you don't alert on “Fundamental damage”",
    ]


def test_an_idea_without_a_stored_rate_converts_at_todays_rate(site):
    opp = site.add(created=NOW - timedelta(hours=1))
    body = valid(eur_reader(site).get(f"/api/v1/ideas/{opp.id}"), "IdeaDetail")
    fx = body["idea"]["fx"]
    assert fx["source"] == "today" and fx["rate"] == 0.8 and fx["as_of"] == "2026-09-25T15:00:00+00:00"
    assert "at today's rate" in fx["note"] and body["fx_note"] == fx["note"]
    assert all(level["value"]["approx"] is not None for level in body["levels"])
    assert body["outcome"]["account_currency"] == "EUR"


def test_an_idea_whose_rate_is_unknown_says_so(site):
    prices = standard_prices()
    prices.missing.add("EURUSD=X")
    prices.missing.add("USDEUR=X")
    site = Site(site.settings.data_dir.parent / "other", prices=prices)
    opp = site.add(created=NOW - timedelta(hours=1))
    body = valid(eur_reader(site).get(f"/api/v1/ideas/{opp.id}"), "IdeaDetail")
    assert body["idea"]["fx"] is None
    assert body["fx_note"] == "No USD/EUR rate is known, so the amounts are in USD only"
    assert "exchange rates" in " ".join(body["outcome_notes"])


def test_an_idea_whose_prices_cant_be_loaded(site):
    site.prices.down.add("AMD")
    opp = site.add(created=NOW - timedelta(hours=1))
    body = valid(site.client().get(f"/api/v1/ideas/{opp.id}"), "IdeaDetail")
    assert body["chart"] is None and body["outcome"] is None
    assert body["prices_problem"] == "Couldn't get prices for AMD from Yahoo Finance (test)."
    unknown = site.add(ticker="GONE", company="Gone Inc.", stats=make_stats(ticker="GONE"))
    body = valid(site.client().get(f"/api/v1/ideas/{unknown.id}"), "IdeaDetail")
    assert body["chart"] is None and body["prices_problem"] == "Yahoo Finance has no prices for GONE."


def test_an_idea_before_its_first_session_has_no_returns_yet(tmp_path):
    prices = FakePrices({"AMD": bars_until(TODAY - timedelta(days=1), 150.0), "^GSPC": bars_until(TODAY, 6000.0)})
    site = Site(tmp_path, prices=prices)
    opp = site.add(created=NOW - timedelta(hours=1), stats=make_stats(as_of=NOW - timedelta(days=1), price=150.0))
    outcome = valid(site.client().get(f"/api/v1/ideas/{opp.id}"), "IdeaDetail")["outcome"]
    assert outcome["priced"] is False and outcome["status"] == "waiting_entry"
    assert outcome["status_label"] == "waiting for entry"
    for key in ("last_price", "last_day", "return_pct", "benchmark_return_pct", "max_gain_pct", "entry_filled"):
        assert outcome[key] is None, key


def test_after_a_split_the_chart_levels_follow_the_prices(tmp_path):
    from dip_scanner.models import Split

    prices = FakePrices(
        {"AMD": bars_until(TODAY, 75.0), "^GSPC": bars_until(TODAY, 6000.0)}, splits={"AMD": [Split(TODAY, 2.0)]}
    )
    site = Site(tmp_path, prices=prices)
    opp = site.add(
        created=NOW - timedelta(days=3), price=150.0, stats=make_stats(price=150.0, as_of=NOW - timedelta(days=3))
    )
    chart = valid(site.client().get(f"/api/v1/ideas/{opp.id}"), "IdeaDetail")["chart"]
    assert chart["split_factor"] == 2.0 and chart["levels"]["reported"] == 75.0
    assert "2:1 split" in chart["split_note"]


@pytest.mark.parametrize("mode", ["debate", "agreed", "single"])
def test_an_idea_shows_its_debate(site, mode):
    debate = make_debate()
    if mode == "agreed":
        debate = replace(debate, mode="agreed", rounds=0, judge=None, favoured=None, agreement="high")
    elif mode == "single":
        only = debate.participants[0]
        debate = replace(
            debate,
            mode="single",
            reason="anthropic:claude-sonnet-5 failed, so openai:gpt-5 analysed it alone: no credit",
            rounds=0,
            participants=[only],
            judge=None,
            summary=None,
            agreement=None,
            favoured=None,
        )
    opp = site.add(debate=debate, model="debate: gpt-5 vs claude-sonnet-5, judged by claude-sonnet-5")
    body = valid(site.client().get(f"/api/v1/ideas/{opp.id}"), "IdeaDetail")
    view = body["debate"]
    assert view["mode"] == mode and view["line"] == body["idea"]["debate"]["line"]
    assert body["opportunity"]["debate"]["mode"] == mode
    first = view["participants"][0]
    assert (first["model_label"], first["provider"]) == ("GPT-5", "openai")
    assert first["provider_label"] == "OpenAI"
    # the Fly idea page's card, word for word (pages.debate_view)
    card = pages.debate_view(site.store.get_opportunity(opp.id))
    assert (view["title"], view["how"], view["reason_label"]) == (card.title, card.how, card.reason)
    assert (view["ruling_title"], view["judge_note"]) == (card.ruling_title, card.judge_note)
    if mode == "debate":
        assert view["judge_label"] == "Claude Sonnet 5" and view["favoured_label"] == "GPT-5"
        assert first["critique"] == debate.participants[0].critique[:5] and first["concessions"]
        assert view["participants"][1]["provider"] == "anthropic"
        assert first["other_label"] == "Claude Sonnet 5" and view["participants"][1]["other_label"] == "GPT-5"
        assert first["favoured"] and not view["participants"][1]["favoured"]
        assert first["compare"] and view["ruling_title"] == "The ruling by Claude Sonnet 5"
        assert view["how"].endswith("and Claude Sonnet 5 ruled.")
    elif mode == "single":
        assert view["reason"].startswith("anthropic:claude-sonnet-5 failed") and len(view["participants"]) == 1
        assert view["reason_label"] == "Claude Sonnet 5 failed, so GPT-5 analysed it alone: no credit."
        assert view["line"] == "GPT-5 alone: 68% (the other model failed)"
        assert view["title"] == "The models" and view["how"] is None and view["ruling_title"] is None
        assert first["other_label"] is None and not first["compare"]
    else:
        assert view["judge"] is None and view["judge_label"] is None and view["favoured_label"] is None
        assert view["ruling_title"] == "The merged analysis" and view["judge_note"] is None
        assert not first["compare"] and not first["favoured"]


def test_debaters_whose_models_read_the_same_are_told_apart(site):
    """Like the idea page: two debaters whose models have one name get their service in brackets."""
    debate = make_debate()
    azure = replace(debate.participants[1], model="azure:gpt-5")
    debate = replace(debate, participants=[debate.participants[0], azure], judge="azure:gpt-5", favoured=None)
    opp = site.add(debate=debate)
    view = valid(site.client().get(f"/api/v1/ideas/{opp.id}"), "IdeaDetail")["debate"]
    labels = [side["model_label"] for side in view["participants"]]
    assert labels == ["GPT-5 (OpenAI)", "GPT-5 (Azure AI Foundry)"]
    assert view["judge_label"] == "GPT-5 (Azure AI Foundry)"
    assert view["participants"][0]["other_label"] == "GPT-5 (Azure AI Foundry)"


def test_a_damaged_debate_is_left_out(site):
    broken = make_debate(
        participants=[Participant(label="", model="mystery", opening=make_analysis(), final=make_analysis())]
    )
    opp = site.add(debate=broken)
    body = valid(site.client().get(f"/api/v1/ideas/{opp.id}"), "IdeaDetail")
    assert body["debate"] is None and body["idea"]["debate"] is None and body["opportunity"]["debate"] is None


def test_the_watchlist_goes_through_preferred_listings(tmp_path):
    config = ScannerConfig(universe=UniverseConfig(preferred_listings={"SAP": "SAP.DE"}))
    site = Site(tmp_path, config=config)
    opp = site.add(ticker="SAP.DE", company="SAP SE", currency="EUR", stats=make_stats(ticker="SAP.DE", currency="EUR"))
    idea = valid(site.client(watchlist=("SAP",)).get(f"/api/v1/ideas/{opp.id}"), "IdeaDetail")["idea"]
    assert idea["on_my_watchlist"] is True


@pytest.mark.parametrize("path", ["/api/v1/ideas/999", "/api/v1/ideas/abc", "/api/v1/ideas/0", "/api/v1/nothing"])
def test_what_doesnt_exist_is_a_json_404(seeded, path):
    site, _ = seeded
    error = error_of(site.client().get(path), 404, "not_found")
    assert error["retry_after"] is None and error["message"]
    if path.endswith("999"):
        assert error["message"] == "There is no idea with that number. It may have been removed."


def test_a_wrong_method_is_a_json_405(seeded):
    site, ids = seeded
    client = site.client()
    error_of(client.post("/api/v1/me", headers={"origin": BASE}), 405, "method_not_allowed")
    error_of(client.get(f"/api/v1/ideas/{ids['amd']}/reanalyse"), 405, "method_not_allowed")


def test_a_server_error_is_json_without_details(seeded, monkeypatch):
    site, ids = seeded

    def broken(*args, **kwargs):
        raise RuntimeError("secret internals")

    client = site.client()
    client_safe = TestClient(site.app, base_url=BASE, raise_server_exceptions=False, cookies=client.cookies)
    monkeypatch.setattr(site.store, "get_opportunity", broken)
    response = client_safe.get(f"/api/v1/ideas/{ids['amd']}")
    error = error_of(response, 500, "server_error")
    assert "secret" not in response.text and error["message"]
    assert response.headers["content-security-policy"]


# --- "Analyse again" -----------------------------------------------------------------------------------------------


def test_reanalyse_queues_an_analysis_and_the_job_says_how_it_went(tmp_path):
    site = analysing_site(tmp_path)
    opp = site.add(created=NOW - timedelta(days=2))
    client = site.client()
    accepted = valid(post(client, f"/api/v1/ideas/{opp.id}/reanalyse"), "ReanalyseAccepted", 202)
    assert accepted["status"] == "queued" and accepted["ticker"] == "AMD"
    job = valid(client.get(f"/api/v1/jobs/{accepted['job_id']}"), "Job")  # it ran inline
    assert job["status"] == "done" and job["opportunity_id"] and job["error"] is None and job["ahead"] is None
    assert job["remaining"] == 4 and job["finished"] == "2026-09-25T15:00:00+00:00"
    newest = valid(client.get(f"/api/v1/ideas/{job['opportunity_id']}"), "IdeaDetail")
    assert newest["idea"]["analyses_count"] == 2
    old = client.get(f"/api/v1/ideas/{opp.id}").json()
    assert old["idea"]["superseded_by"] == job["opportunity_id"]
    assert valid(client.get("/api/v1/me"), "Me")["capabilities"]["analyse"]["remaining"] == 4


def test_a_queued_job_says_how_many_are_ahead(tmp_path):
    site = analysing_site(tmp_path)
    client = site.client()
    user = site.user()
    first = site.accounts.create_job(user.id, "NVDA")
    queued = site.accounts.create_job(user.id, "AMD")
    job = valid(client.get(f"/api/v1/jobs/{queued.id}"), "Job")
    assert (job["status"], job["ahead"], job["remaining"]) == ("queued", 1, 3)
    site.accounts.start_job(first.id)
    site.accounts.finish_job(first.id, error="The analysis of NVDA failed: the model refused")
    failed = valid(client.get(f"/api/v1/jobs/{first.id}"), "Job")
    assert failed["status"] == "failed" and failed["error"].startswith("The analysis of NVDA failed")


def test_only_the_owner_or_an_admin_sees_a_job(tmp_path):
    site = analysing_site(tmp_path)
    job = site.accounts.create_job(site.accounts.create_user("owner@example.com", password="x" * 12).id, "AMD")
    error_of(site.client().get(f"/api/v1/jobs/{job.id}"), 404, "not_found")
    error_of(site.client().get("/api/v1/jobs/12345"), 404, "not_found")
    valid(site.client("boss@example.com", role="admin").get(f"/api/v1/jobs/{job.id}"), "Job")


def test_reanalyse_needs_the_csrf_header(tmp_path):
    site = analysing_site(tmp_path)
    opp = site.add()
    client = site.client()
    path = f"/api/v1/ideas/{opp.id}/reanalyse"
    error = error_of(client.post(path, headers={"origin": BASE}), 403, "csrf")
    assert error["message"] == "This page has expired. Reload it and try again."
    error_of(client.post(path, headers={"origin": BASE, "x-csrf-token": "wrong"}), 403, "csrf")
    # The form field doesn't count: an API call sends the header.
    error_of(client.post(path, headers={"origin": BASE}, data={"csrf_token": csrf(client)}), 403, "csrf")
    assert site.accounts.list_jobs() == []


def test_reanalyse_refuses_other_sites(tmp_path):
    site = analysing_site(tmp_path)
    opp = site.add()
    client = site.client()
    path = f"/api/v1/ideas/{opp.id}/reanalyse"
    error_of(post(client, path, origin="https://evil.example"), 403, "origin")
    error_of(post(client, path, origin="null"), 403, "origin")
    token = csrf(client)
    response = client.post(path, headers={"x-csrf-token": token, "referer": "https://evil.example/page"})
    error_of(response, 403, "origin")
    assert site.accounts.list_jobs() == []
    # Neither header (a server-side call) is fine: the token still has to match.
    valid(client.post(path, headers={"x-csrf-token": token}), "ReanalyseAccepted", 202)


def test_reanalyse_needs_signing_in_and_an_idea(tmp_path):
    site = analysing_site(tmp_path)
    anonymous = TestClient(site.app, base_url=BASE, follow_redirects=False)
    error_of(
        anonymous.post("/api/v1/ideas/1/reanalyse", headers={"origin": BASE, "x-csrf-token": "x"}), 401, "not_signed_in"
    )
    error_of(post(site.client(), "/api/v1/ideas/42/reanalyse"), 404, "not_found")


def test_reanalyse_without_a_model_is_unavailable(site):
    opp = site.add()
    error = error_of(post(site.client(), f"/api/v1/ideas/{opp.id}/reanalyse"), 503, "unavailable")
    assert error["message"] == "Manual analyses aren't available on this server."


def test_reanalyse_keeps_the_daily_limit(tmp_path):
    site = analysing_site(tmp_path, web={"analyze_limit_per_user": 2})
    opp = site.add()
    client = site.client()
    user = site.user()
    earlier = site.accounts.create_job(user.id, "NVDA")  # made at NOW by the site's clock
    site.accounts.finish_job(earlier.id, error="failed")
    path = f"/api/v1/ideas/{opp.id}/reanalyse"
    valid(post(client, path), "ReanalyseAccepted", 202)
    response = post(client, path)
    error = error_of(response, 429, "limit_reached")
    assert error["message"].startswith("You have used your 2 manual analyses of the last 24 hours")
    assert error["retry_after"] == 86400 and response.headers["retry-after"] == "86400"
    me = valid(client.get("/api/v1/me"), "Me")
    assert me["capabilities"]["analyse"]["available"] is False and me["capabilities"]["analyse"]["remaining"] == 0
    # An admin has no daily limit.
    valid(post(site.client("admin@example.com", role="admin"), path), "ReanalyseAccepted", 202)


def test_reanalyse_keeps_the_burst_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "BURST_LIMIT", 1)
    site = analysing_site(tmp_path)
    first = site.add()
    second = site.add(ticker="NVDA", company="NVIDIA", stats=make_stats(ticker="NVDA"))
    client = site.client("admin@example.com", role="admin")
    valid(post(client, f"/api/v1/ideas/{first.id}/reanalyse"), "ReanalyseAccepted", 202)
    response = post(client, f"/api/v1/ideas/{second.id}/reanalyse")
    error = error_of(response, 429, "rate_limited")
    assert error["retry_after"] == 600 and response.headers["retry-after"] == "600"


def test_reanalyse_when_members_may_not(tmp_path):
    site = analysing_site(tmp_path, web={"analyze_limit_per_user": 0})
    opp = site.add()
    error = error_of(post(site.client(), f"/api/v1/ideas/{opp.id}/reanalyse"), 403, "forbidden")
    assert error["message"] == "Manual analyses are switched off for members on this server."


def test_reanalyse_returns_the_analysis_already_waiting(tmp_path):
    site = analysing_site(tmp_path)
    opp = site.add()
    client = site.client()
    waiting = site.accounts.create_job(site.user().id, "AMD")
    accepted = valid(post(client, f"/api/v1/ideas/{opp.id}/reanalyse"), "ReanalyseAccepted", 202)
    assert accepted == {"job_id": waiting.id, "status": "queued", "ticker": "AMD"}


# --- /thesis-changes -----------------------------------------------------------------------------------------------


def test_thesis_changes(seeded):
    site, ids = seeded
    body = valid(eur_reader(site).get("/api/v1/thesis-changes"), "ThesisChanges")
    assert body["days"] == 7 and len(body["changes"]) == 1
    change = body["changes"][0]
    assert (change["ticker"], change["company"], change["reason"]) == (
        "AMD",
        "Advanced Micro Devices",
        "no longer passes your alert rules",
    )
    assert (change["current"]["id"], change["previous"]["id"]) == (ids["amd"], ids["amd_old"])
    assert change["current"]["verdict_label"] == "Fundamental damage" and change["current"]["score_band"] == "weak"
    assert change["previous"]["entry"]["approx"] is None  # no rate stored with it: the list converts at stored rates
    for days, echoed in (("3", 3), ("30", 30), ("0", 7), ("31", 7), ("x", 7)):
        assert valid(site.client().get(f"/api/v1/thesis-changes?days={days}"), "ThesisChanges")["days"] == echoed


def test_no_thesis_changes_for_rules_the_earlier_idea_didnt_pass(seeded):
    site, _ = seeded
    body = valid(site.client("strict@example.com", min_score=90.0).get("/api/v1/thesis-changes"), "ThesisChanges")
    assert body == {"days": 7, "changes": []}
