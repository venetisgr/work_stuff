"""The member pages (web/pages.py) with FastAPI's TestClient: the ideas, an idea, a ticker (watchlist, "Analyse
now"), the news and the track record, on a database seeded with ideas, articles and model calls.

No network (fake prices and exchange rates, fake DNS) and no sleeping: prices are daily bars built in the test, the
clock is fixed, analyses run inline.
"""

from __future__ import annotations

import re
import socket
import threading
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from conftest import make_analysis, make_article, make_bars, make_debate, make_impact, make_opportunity, make_stats
from fastapi.testclient import TestClient

from dip_scanner import accounts as accounts_module
from dip_scanner.config import DATABASE_NAME, ScannerConfig, Settings, UniverseConfig, WebSettings
from dip_scanner.models import PriceBar, Split
from dip_scanner.prices import PriceError, PriceFetchError
from dip_scanner.track import BENCHMARKS, DEFAULT_BENCHMARK, EUROPE_BENCHMARK, HORIZON_DAYS, signal_day
from dip_scanner.web import pages
from dip_scanner.web.app import create_app
from dip_scanner.web.jobs import InlineExecutor

BASE = "https://dips.example.com"
NOW = datetime(2026, 9, 25, 15, 0, tzinfo=UTC)  # a Friday, during the US session
TODAY = NOW.date()
PASSWORD = "a long enough password"
STATIC = Path(pages.__file__).resolve().parent / "static"


@pytest.fixture(autouse=True)
def fast_scrypt(monkeypatch):
    monkeypatch.setattr(accounts_module, "SCRYPT_N", 2**4)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any DNS lookup or connection is a test bug: prices come from FakePrices."""

    def refuse(*args, **kwargs):
        raise AssertionError(f"a test tried to use the network: {args[:2]}")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


# --- fakes ---------------------------------------------------------------------------------------------------------


def closes_until(end: date, last: float, *, days: int = 400, drift: float = 0.0004) -> list[float]:
    """Weekday closes over about days calendar days that end at last (a gentle rise with a wobble)."""
    count = sum(1 for n in range(days) if (end - timedelta(days=n)).weekday() < 5)
    return [round(last * (1 - drift * (count - 1 - i)) * (1 + 0.01 * ((i % 7) - 3) / 3), 4) for i in range(count)]


def bars_until(end: date, last: float, **kwargs) -> list[PriceBar]:
    days = kwargs.pop("days", 400)
    closes = closes_until(end, last, days=days, **kwargs)
    start = end - timedelta(days=days - 1)
    while start.weekday() >= 5:
        start += timedelta(days=1)
    bars = make_bars(closes, start)
    assert bars[-1].day == end or end.weekday() >= 5, (bars[-1].day, end)
    return bars


class FakePrices:
    """YahooPrices without the network: daily bars and splits per symbol, stats built from make_stats. Symbols in
    missing have no prices (PriceError); symbols in down can't be reached (PriceFetchError). Records every call."""

    def __init__(self, bars: dict[str, list[PriceBar]] | None = None, *, splits=None, currencies=None) -> None:
        self.bars = dict(bars or {})
        self.splits: dict[str, list[Split]] = dict(splits or {})
        self.currencies: dict[str, str] = {"EURUSD=X": "USD", **(currencies or {})}
        self.missing: set[str] = set()
        self.down: set[str] = set()
        self.calls: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def _check(self, kind: str, symbol: str) -> list[PriceBar]:
        with self._lock:
            self.calls.append((kind, symbol))
        if symbol in self.down:
            raise PriceFetchError(f"Couldn't get prices for {symbol} from Yahoo Finance (test).")
        if symbol in self.missing or symbol not in self.bars:
            raise PriceError(f"Yahoo Finance has no prices for {symbol}.")
        return self.bars[symbol]

    def history_since(self, ticker, start, *, now=None):
        bars = self._check("history", ticker)
        return [bar for bar in bars if bar.day >= start], list(self.splits.get(ticker, []))

    def bars_since(self, ticker, start, *, now=None):
        return [bar for bar in self._check("bars", ticker) if bar.day >= start]

    def chart(self, symbol, *, range_="2y", interval="1d"):
        bars = self._check("chart", symbol)
        meta = {"currency": self.currencies.get(symbol, "USD"), "regularMarketPrice": bars[-1].close}
        return meta, bars

    def stats(self, ticker, *, now=None):
        bars = self._check("stats", ticker)
        return make_stats(ticker=ticker, price=bars[-1].close, currency=self.currencies.get(ticker, "USD"))


def standard_prices() -> FakePrices:
    """AMD in USD around the seeded ideas' prices, SAP.DE in EUR, the S&P 500 and DAX, and EUR/USD at 1.25."""
    return FakePrices(
        {
            "AMD": bars_until(TODAY, 150.0),
            "SAP.DE": bars_until(TODAY, 200.0),
            "^GSPC": bars_until(TODAY, 6000.0),
            "^GDAXI": bars_until(TODAY, 19000.0),
            "EURUSD=X": [replace(bar, close=1.25) for bar in bars_until(TODAY, 1.25)],
        },
        currencies={"SAP.DE": "EUR", "^GDAXI": "EUR"},
    )


class Site:
    """An app on a fresh database with fake prices, its context and a client that doesn't follow redirects."""

    def __init__(self, tmp_path: Path, *, prices: FakePrices | None = None, config: ScannerConfig | None = None, **web):
        values = {"secret_key": "s" * 40, "base_url": BASE, "cookie_secure": True}
        values.update(web.pop("web", {}))
        self.settings = Settings(data_dir=tmp_path / "data", web=WebSettings(**values))
        self.prices = prices if prices is not None else standard_prices()
        self.app = create_app(
            settings=self.settings,
            config=config or ScannerConfig(),
            feeds=[],
            store_path=self.settings.data_dir / DATABASE_NAME,
            prices=self.prices,
            clock=lambda: NOW,
            analyse=web.pop("analyse", None),
            job_executor=InlineExecutor(),
            resolver=lambda host, port: ["34.120.1.2"],
        )
        self.ctx = self.app.state.ctx
        self.store = self.ctx.store
        self.accounts = self.ctx.accounts

    def client(self, email: str = "member@example.com", *, role: str = "member", **settings) -> TestClient:
        """A signed-in client (the user is created, with these settings, when needed)."""
        user = self.accounts.get_user_by_email(email) or self.accounts.create_user(email, role=role, password=PASSWORD)
        if settings:
            self.accounts.update_settings(user.id, replace(user.settings, **settings))
        client = TestClient(self.app, base_url=BASE, follow_redirects=False)
        page = client.get("/login")
        data = {"email": email, "password": PASSWORD, "csrf_token": token_on(page.text), "next": "/"}
        assert client.post("/login", data=data).status_code == 303
        return client

    def user(self, email: str = "member@example.com"):
        return self.accounts.get_user_by_email(email)

    def add(self, **overrides):
        return self.store.add_opportunity(make_opportunity(**overrides))


def token_on(page: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', page)
    assert match, "no csrf_token on the page"
    return match.group(1)


def post_form(client: TestClient, path: str, data: dict, *, page: str = "/"):
    return client.post(path, data={"csrf_token": token_on(client.get(page).text), **data})


def seed(site: Site) -> dict[str, int]:
    """AMD analysed twice (a good idea, then a much worse one: a thesis change), SAP.DE once, an older NVDA idea, and
    news about them."""
    ids = {}
    ids["amd_old"] = site.add(created=NOW - timedelta(days=3), stats=make_stats(as_of=NOW - timedelta(days=3))).id
    ids["amd"] = site.add(
        created=NOW - timedelta(hours=2),
        score=20.3,
        analysis=make_analysis(
            verdict="fundamental",
            probability_up_6m=30,
            confidence="high",
            fear="The guidance cut is the start of lost share to Nvidia.",
            fundamental_impact="Data-center revenue falls for several quarters.",
            thesis="The drop reflects real damage; waiting is better.",
            risks=["Share loss accelerates"],
            catalysts=["Product launch in December"],
            checks=["Read the 10-Q", "Compare with Nvidia's guidance"],
        ),
    ).id
    sap_stats = make_stats(ticker="SAP.DE", price=190.0, currency="EUR", name="SAP SE", exchange="XETRA")
    ids["sap"] = site.add(
        ticker="SAP.DE",
        company="SAP SE",
        created=NOW - timedelta(days=1),
        price=190.0,
        currency="EUR",
        stats=sap_stats,
        score=66.0,
        analysis=make_analysis(
            verdict="mixed", probability_up_6m=74, potential_low=160.0, entry_price=180.0, target_price=225.0
        ),
    ).id
    ids["nvda"] = site.add(ticker="NVDA", company="NVIDIA Corporation", created=NOW - timedelta(days=20), score=81.0).id

    fresh = make_article(title="AMD shares slide after weak data-center guidance")
    sap = make_article(
        title="SAP cuts cloud outlook as customers delay deals",
        source="reuters",
        source_name="Reuters",
        published=NOW - timedelta(hours=5),
    )
    other = make_article(
        title="Oil prices steady ahead of OPEC meeting", source_name="CNBC", published=NOW - timedelta(hours=10)
    )
    old = make_article(title="Chip stocks rally on AI hopes", published=NOW - timedelta(hours=30))
    site.store.add_articles([fresh, sap, other, old], max_age_hours=72, now=NOW)
    site.store.record_triage(
        [fresh.id, sap.id, other.id, old.id],
        [
            make_impact(article_id=fresh.id),
            make_impact(article_id=fresh.id, ticker="NVDA", company="NVIDIA", direction="positive", magnitude=2),
            make_impact(
                article_id=sap.id,
                ticker="SAP.DE",
                company="SAP",
                magnitude=3,
                rationale="A lower cloud outlook trims growth.",
            ),
            make_impact(article_id=old.id, ticker="NVDA", company="NVIDIA", direction="positive", magnitude=3),
        ],
    )
    return ids


@pytest.fixture
def site(tmp_path) -> Site:
    return Site(tmp_path)


@pytest.fixture
def seeded(site) -> tuple[Site, dict[str, int]]:
    return site, seed(site)


# --- access --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/ideas/1", "/tickers/AMD", "/tickers?symbol=AMD", "/news", "/track"])
def test_member_pages_need_signing_in(site, path):
    response = TestClient(site.app, base_url=BASE, follow_redirects=False).get(path)
    assert response.status_code == 303 and response.headers["location"].startswith("/login")


def test_pages_include_their_stylesheet(site):
    page = site.client().get("/").text
    assert re.search(r'href="/static/pages\.css\?v=[0-9a-f]{10}"', page)


def test_the_chart_colours_have_dark_mode_steps():
    css = (STATIC / "pages.css").read_text(encoding="utf-8")
    dark = css[css.index("@media (prefers-color-scheme: dark)") :]
    for token in ("--chart-price", "--chart-up", "--chart-down"):
        assert token in css.split("@media")[0] and token in dark


# --- the dashboard -------------------------------------------------------------------------------------------------


def ideas_on(page: str) -> list[str]:
    """The tickers of the idea rows, in order."""
    row = r'class="idea-row[^"]*" href="/ideas/\d+"[^>]*>.*?<span class="ticker">([^<]+)</span>'
    return re.findall(row, page, re.S)


def test_the_dashboard_lists_the_newest_idea_of_each_stock(seeded):
    site, ids = seeded
    page = site.client().get("/").text
    assert ideas_on(page) == ["SAP.DE", "AMD"]  # best score first; NVDA is 20 days old
    assert f'href="/ideas/{ids["amd"]}"' in page and "2 analyses" in page
    assert "Scanner off" in page and "No cycle yet" in page  # the status strip
    month = site.client().get("/?days=30").text
    assert ideas_on(month) == ["NVDA", "SAP.DE", "AMD"]
    assert ideas_on(site.client().get("/?sort=new").text) == ["AMD", "SAP.DE"]


def test_dashboard_filters(seeded):
    site, _ = seeded
    client = site.client(watchlist=("SAP.DE",))
    assert ideas_on(client.get("/?score=65").text) == ["SAP.DE"]
    assert ideas_on(client.get("/?verdict=fundamental").text) == ["AMD"]
    assert ideas_on(client.get("/?watchlist=1").text) == ["SAP.DE"]
    rules = client.get("/?rules=1&days=30").text  # default rules: score 65+, chance 60%+, fear or mixed
    assert ideas_on(rules) == ["NVDA", "SAP.DE"]
    assert "1 of 2 ideas match the filters." in client.get("/?score=65").text
    nothing = client.get("/?score=80").text
    assert "No idea matches these filters" in nothing and "Clear the filters" in nothing
    # nonsense parameters fall back to the defaults
    assert ideas_on(client.get("/?days=abc&score=12&verdict=<x>&sort=up").text) == ["SAP.DE", "AMD"]


def alerted(site: Site, email: str, *opportunity_ids: int) -> None:
    """The scanner sent these ideas to the user as alerts."""
    user = site.user(email)
    site.store.record_deliveries(user.recipient_key, opportunity_ids, "alert", when=NOW - timedelta(days=3), sent=True)


def test_the_dashboard_shows_thesis_changes(seeded):
    site, ids = seeded
    client = site.client()
    alerted(site, "member@example.com", ids["amd_old"])
    page = client.get("/").text
    assert "Thesis changes" in page and "Review open orders" in page
    assert "no longer passes your alert rules" in page
    assert f'href="/ideas/{ids["amd_old"]}">the earlier idea' in page
    # Rules the earlier idea didn't pass either, and never sent to them: nothing to review.
    strict = site.client("strict@example.com", min_score=90.0).get("/").text
    assert "Thesis changes" not in strict


def test_a_drop_in_the_chance_up_is_a_thesis_change(site):
    client = site.client()
    old = site.add(created=NOW - timedelta(days=2), score=80.0, analysis=make_analysis(probability_up_6m=85))
    site.add(created=NOW - timedelta(hours=1), score=70.0, analysis=make_analysis(probability_up_6m=62))
    alerted(site, "member@example.com", old.id)
    page = client.get("/").text
    assert "its chance of being higher fell by 23 points" in page


def test_a_new_member_is_not_told_to_review_orders_on_ideas_from_before_they_joined(seeded):
    """Their first dashboard: no "Review open orders" about ideas nobody sent them, from before they had an account."""
    site, ids = seeded
    assert "Thesis changes" not in site.client("newcomer@example.com").get("/").text
    newcomer = site.user("newcomer@example.com")
    assert pages.thesis_changes(site.ctx, newcomer, now=NOW) == []


def test_ideas_a_member_saw_after_joining_count_without_an_alert(site):
    """Members without an alert channel follow the ideas on the dashboard: an idea from after they joined counts."""
    site.client()
    member = site.user()
    with site.store.transaction() as conn:
        conn.execute("UPDATE users SET created = ? WHERE id = ?", ((NOW - timedelta(days=5)).isoformat(), member.id))
    site.add(created=NOW - timedelta(days=2), score=80.0, analysis=make_analysis(probability_up_6m=85))
    site.add(created=NOW - timedelta(hours=1), score=30.0, analysis=make_analysis(verdict="fundamental"))
    [change] = pages.thesis_changes(site.ctx, site.user(), now=NOW)
    assert change.reason == "no longer passes your alert rules"


def test_an_alert_still_counts_after_the_member_tightened_their_rules(site):
    """They got the alert at score 72.4 and may have orders on it; raising min_score to 80 since doesn't make the
    later "fundamental damage" analysis any less of a thesis change. The same analysis again is none, though."""
    client = site.client(min_score=80.0)
    old = site.add(created=NOW - timedelta(days=2))
    alerted(site, "member@example.com", old.id)
    same = site.add(created=NOW - timedelta(hours=2))
    assert pages.thesis_changes(site.ctx, site.user(), now=NOW) == []  # nothing new failed
    site.add(created=NOW - timedelta(hours=1), score=20.0, analysis=make_analysis(verdict="fundamental"))
    [change] = pages.thesis_changes(site.ctx, site.user(), now=NOW)
    assert (change.previous.id, change.reason) == (old.id, "no longer passes your alert rules")
    assert same.id != change.current.id and "Thesis changes" in client.get("/").text


def test_thesis_changes_can_be_turned_off(seeded):
    site, ids = seeded
    client = site.client(thesis_changes=False)
    alerted(site, "member@example.com", ids["amd_old"])
    assert "Thesis changes" not in client.get("/").text
    assert pages.thesis_changes(site.ctx, site.user(), now=NOW) == []


def test_the_thesis_card_comes_after_the_filters_shows_two_and_converts_amounts(site):
    client = site.client(currency="EUR")
    ids = []
    for number, ticker in enumerate(("AMD", "NVDA", "INTC")):
        old = site.add(ticker=ticker, created=NOW - timedelta(days=2), fx_rates={"EUR": 0.8}, account_currency=None)
        site.add(ticker=ticker, created=NOW - timedelta(hours=1 + number), score=20.0,
                 analysis=make_analysis(verdict="fundamental"))  # fmt: skip
        ids.append(old.id)
    alerted(site, "member@example.com", *ids)
    page = client.get("/").text
    assert page.index('aria-label="Period"') < page.index("Thesis changes")  # the ideas' controls come first
    card = page[page.index("Thesis changes") :]
    card = card[: card.index("</section>")]
    assert card.count("<li>") == 3 and "Show all 3" in card
    assert card.index("Show all 3") > card.index("the earlier idea")  # two shown, the third behind "Show all"
    assert "≈ €105.60" in card  # entry $132.00 at 0.8


def test_the_dashboard_counts_the_models_calls_of_the_day(site):
    for hours in (1, 2, 20):
        site.store.record_model_call(when=NOW - timedelta(hours=hours), step="triage", model="mini")
    assert "2 model calls today" in site.client().get("/").text


def test_the_dashboard_pages_through_many_ideas(site):
    for number in range(30):
        site.add(ticker=f"T{number:02d}", created=NOW - timedelta(minutes=number), score=50.0 + number)
    client = site.client()
    first = client.get("/").text
    assert len(ideas_on(first)) == pages.PAGE_SIZE and 'href="/?page=2"' in first
    assert len(ideas_on(client.get("/?page=2").text)) == 5


def test_an_empty_dashboard_says_what_to_do(site):
    page = site.client().get("/").text
    assert "No ideas in the last 7 days" in page and "Look up a stock" in page


def test_the_dashboard_shows_the_readers_watchlist_and_rules(site):
    page = site.client(watchlist=("AMD", "SAP.DE"), min_score=70.0, only_watchlist=True).get("/").text
    assert 'href="/tickers/SAP.DE"' in page
    assert "Score 70 or more" in page and "on your watchlist only" in page


# --- an idea -------------------------------------------------------------------------------------------------------


def test_the_idea_page_shows_everything(seeded):
    site, ids = seeded
    page = site.client().get(f"/ideas/{ids['amd']}").text
    for text in (
        "Fundamental damage",
        "High confidence",
        "30%",
        "chance of being higher in 6 months",
        "20.3",
        "$142.50",
        "Target (limit sell idea)",
        "$168.00",
        "Entry (limit buy)",
        "$132.00",
        "Potential low",
        "$118.00",
        "Statistical 6-month low",
        "The guidance cut is the start of lost share to Nvidia.",
        "Data-center revenue falls for several quarters.",
        "The drop reflects real damage; waiting is better.",
        "Share loss accelerates",
        "Product launch in December",
        "Compare with Nvidia&#39;s guidance",
        "down 5.0% today",
        "Analyses of AMD",
        "Outcome so far",
    ):
        assert text in page, text
    assert (
        '<a href="https://www.example.com/news/amd-shares-slide-after-weak-data-center-guidance" target="_blank" '
        'rel="noopener noreferrer">AMD shares slide after weak data-center guidance</a>'
    ) in page
    assert 'class="chart-svg chart-wide"' in page and 'class="chart-svg chart-narrow"' in page
    assert "The closes as a table" in page
    assert "No alert under your rules" in page and "you don&#39;t alert on “Fundamental damage”" in page
    assert "There is a newer analysis" not in page  # it is the newest
    older = site.client().get(f"/ideas/{ids['amd_old']}").text
    assert "There is a newer analysis of AMD" in older and f'href="/ideas/{ids["amd"]}"' in older
    assert "Passes your alert rules" in older


def test_the_idea_page_has_analyse_again_and_watchlist_buttons(tmp_path):
    site = Site(tmp_path, analyse=lambda ticker, now: make_opportunity(ticker=ticker, created=now))
    opp = site.add()
    page = site.client().get(f"/ideas/{opp.id}").text
    assert 'action="/analyze"' in page and 'name="ticker" value="AMD"' in page
    assert f'name="next" value="/ideas/{opp.id}"' in page and "Analyse again now" in page
    assert 'action="/tickers/AMD/watchlist"' in page and "Add to watchlist" in page
    # Without analyses on the server, the page says so instead of offering a button that can't work.
    plain = Site(tmp_path / "plain")
    other = plain.add()
    assert "Manual analyses aren&#39;t available right now." in plain.client().get(f"/ideas/{other.id}").text
    admin = plain.client("admin@example.com", role="admin")
    assert "Manual analyses aren&#39;t available on this server." in admin.get(f"/ideas/{other.id}").text


def test_an_analysis_asked_for_by_hand_says_so(site):
    manual = site.add(dip_reasons=[], headlines=[])
    assert "this analysis was asked for by hand" in site.client().get(f"/ideas/{manual.id}").text
    flagged = site.add(headlines=[])  # a dip with no stored headlines (an old record)
    page = site.client().get(f"/ideas/{flagged.id}").text
    assert "asked for by hand" not in page and "No headlines were stored with this analysis." in page


def test_levels_show_the_readers_currency_at_the_stored_rate(site):
    opp = site.add(fx_rates={"EUR": 0.9})
    page = site.client(currency="EUR").get(f"/ideas/{opp.id}").text
    assert "≈ €151.20" in page and "≈ €118.80" in page  # target 168, entry 132
    assert "1 USD = 0.9 EUR at the analysis" in page


def test_levels_use_todays_rate_when_none_was_stored(site):
    opp = site.add()
    page = site.client(currency="EUR").get(f"/ideas/{opp.id}").text
    assert "≈ €134.40" in page  # 168 / 1.25
    assert "1 USD = 0.8 EUR at today&#39;s rate" in page


def test_the_outcome_so_far(tmp_path):
    report = NOW - timedelta(days=30)
    before = bars_until(report.date() - timedelta(days=1), 142.5)
    after = make_bars([140.0, 131.0, 150.0, 170.0, 165.0] + [160.0] * 15, report.date())
    prices = standard_prices()
    prices.bars["AMD"] = before + after
    site = Site(tmp_path, prices=prices)
    stats = make_stats(as_of=report - timedelta(hours=1), timezone="America/New_York", session_elapsed=0.5)
    opp = site.add(created=report, stats=stats, fx_rates={"EUR": 0.8})
    page = site.client(currency="EUR").get(f"/ideas/{opp.id}").text
    assert "Target hit" in page
    assert "<dt>Entry filled</dt><dd>Thu 27 Aug 2026</dd>" in page  # the report's day doesn't reach 132
    assert "<dt>Target hit</dt><dd>Mon 31 Aug 2026</dd>" in page
    last = after[-1]
    assert f"{last.day:%a} {last.day.day} {last.day:%b %Y}" in page
    assert "+12.3%" in page  # 160 / 142.5
    assert "In EUR" in page and "S&amp;P 500" in page and "Versus the index" in page
    assert "The limit orders" in page and "+27.3%" in page  # 132 -> 168


def test_an_idea_without_trading_since_waits(site):
    opp = site.add(created=NOW, stats=make_stats(as_of=NOW, session_elapsed=None, timezone="America/New_York"))
    site.prices.bars["AMD"] = [bar for bar in site.prices.bars["AMD"] if bar.day < TODAY]
    page = site.client().get(f"/ideas/{opp.id}").text
    assert "Nothing has traded since the report yet" in page


def test_an_idea_without_prices_still_shows(site):
    opp = site.add()
    site.prices.missing.add("AMD")
    page = site.client().get(f"/ideas/{opp.id}").text
    assert "No chart" in page and "Yahoo Finance has no prices for AMD." in page
    assert "Target (limit sell idea)" in page and "The analysis" in page
    site.prices.missing.clear()
    site.prices.down.add("AMD")
    site.ctx.cache.clear()
    assert "Couldn&#39;t get prices for AMD from Yahoo Finance (test)." in site.client().get(f"/ideas/{opp.id}").text


def test_the_chart_is_adjusted_after_a_split(site):
    opp = site.add(created=NOW - timedelta(days=10), stats=make_stats(as_of=NOW - timedelta(days=10)))
    site.prices.bars["AMD"] = bars_until(TODAY, 75.0)  # Yahoo divides the history before a split too
    site.prices.splits["AMD"] = [Split(day=TODAY - timedelta(days=5), ratio=2.0)]
    page = site.client().get(f"/ideas/{opp.id}").text
    assert "After a 2:1 split since the report" in page
    assert '<tspan class="chart-label-value">$84.00</tspan>' in page  # the target of 168 on the chart
    assert "Outcome so far" in page and "doesn't match Yahoo Finance's price history" not in page


def test_unknown_ideas_are_not_found(site):
    client = site.client()
    missing = client.get("/ideas/99")
    assert missing.status_code == 404 and "There is no idea with that number." in missing.text
    assert client.get("/ideas/abc").status_code == 404


def test_untrusted_text_on_the_idea_page_is_escaped(site):
    evil = "<script>alert(1)</script>"
    headlines = [
        {"title": f"Bad {evil}", "link": "javascript:alert(1)", "source": evil, "direction": "negative"},
        {"title": "Fine", "link": "https://example.com/a", "published": "not a date"},
    ]
    opp = site.add(company=f"Evil {evil}", headlines=headlines, analysis=make_analysis(fear=evil, risks=[evil]))
    page = site.client().get(f"/ideas/{opp.id}").text
    assert evil not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "javascript:" not in page


# --- the debate behind an idea -------------------------------------------------------------------------------------

JUDGED_MODEL = "debate: gpt-5 vs claude-sonnet-5, judged by claude-sonnet-5"


def debate_card(page: str) -> str:
    """The debate card of an idea page."""
    start = page.index('<section class="card order-3 debate-card" id="debate"')
    return page[start : page.index("</section>", start)]


def debater(card: str, number: int) -> str:
    """The number-th debater's column of a debate card (1 or 2)."""
    start = card.index(f'aria-labelledby="debater-{number}"')
    return card[start : card.index("</article>", start)]


def agreed_debate(**overrides):
    """Two openings that agreed: merged without a rebuttal or a judge (each final is its opening)."""
    sides = [
        replace(side, final=side.opening, critique=[], concessions=[], changed_mind=False)
        for side in make_debate().participants
    ]
    values = {
        "mode": "agreed",
        "rounds": 0,
        "participants": sides,
        "judge": None,
        "favoured": None,
        "agreement": "high",
        "summary": "Both analysts called it Temporary fear with close numbers, so their analyses were merged.",
    }
    return make_debate(**{**values, **overrides})


def lone_debate(**overrides):
    """GPT-5 alone: Claude Sonnet 5 failed."""
    gpt = make_debate().participants[0]
    values = {
        "mode": "single",
        "rounds": 0,
        "participants": [replace(gpt, final=gpt.opening, critique=[], concessions=[], changed_mind=False)],
        "judge": None,
        "favoured": None,
        "agreement": None,
        "summary": None,
        "reason": "anthropic:claude-sonnet-5 failed, so openai:gpt-5 analysed it alone: no credit left",
    }
    return make_debate(**{**values, **overrides})


def test_the_idea_page_shows_a_judged_debate(site):
    opp = site.add(
        debate=make_debate(),
        analysis=make_analysis(verdict="mixed", probability_up_6m=64, target_price=165.0),
        model=JUDGED_MODEL,
    )
    page = site.client().get(f"/ideas/{opp.id}").text
    card = debate_card(page)
    assert '<h2 id="debate-title">The debate</h2>' in card
    assert '<span class="badge badge-warn">Medium agreement</span>' in card
    how = "GPT-5 and Claude Sonnet 5 analysed it separately, answered each other once, and Claude Sonnet 5 ruled."
    assert how in card
    # in LLM_DEBATERS order, each with the label the judge saw
    gpt, claude = debater(card, 1), debater(card, 2)
    assert '<h3 id="debater-1">GPT-5</h3>' in gpt and "OpenAI · Analyst B" in gpt
    assert '<h3 id="debater-2">Claude Sonnet 5</h3>' in claude and "Anthropic · Analyst A" in claude
    assert '<article class="debate-side is-favoured"' in card and "Favoured by the judge" in gpt
    assert "Favoured by the judge" not in claude
    assert "Changed its mind" in gpt and "Changed its mind" not in claude
    # opening -> final: what changed is struck through and marked
    assert '<s class="was">Temporary fear</s>' in gpt and "Mixed</span>" in gpt
    assert "<s>72%</s>" in gpt and "<mark>66%</mark>" in gpt
    assert "<s>$118.00</s>" in gpt and "<mark>$115.00</mark>" in gpt
    assert "<s>" not in claude and "<mark>" not in claude and "58%" in claude  # it kept its position
    assert '<th scope="col">Opening</th><th scope="col">Final</th>' in gpt
    # each side's critique of the other and what it accepts
    assert "Its critique of Claude Sonnet 5" in gpt and "Uses a revenue figure the input doesn&#39;t give" in gpt
    assert "<li>4th</li>" in gpt  # up to 5 points, all of them here
    assert "What it accepts from Claude Sonnet 5" in gpt and "The guidance cut is real" in gpt
    assert "Its critique of GPT-5" in claude and "Too optimistic about the recovery" in claude
    assert "What it accepts" not in claude
    # then the ruling: the idea's own analysis
    ruling = card[card.index('<div class="debate-ruling">') :]
    assert "<h3>The ruling by Claude Sonnet 5</h3>" in ruling
    assert "64%</strong> chance up</span>" in ruling and 'target <span class="num">$165.00</span>' in ruling
    assert "They agree the drop is partly sentiment; the crux is the guidance cut" in ruling
    assert "It found GPT-5&#39;s case stronger." in ruling
    assert "in an order that doesn&#39;t say which model wrote which." in ruling
    # the headline figures carry the debate in one line, and the analysis says whose it is
    start = page.index('<header class="card idea-hero">')
    hero = page[start : page.index("</header>", start)]
    assert "GPT-5 66% · Claude Sonnet 5 58% → 64% (medium agreement)" in hero
    assert f"The outcome of the debate above ({JUDGED_MODEL})" in page
    # on a phone the card comes after the chart, before the outcome and the analysis
    assert page.index('id="chart-title"') < page.index('id="debate-title"') < page.index('id="outcome-title"')
    assert 'class="card order-4" aria-labelledby="outcome-title"' in page


def test_the_debate_card_shows_at_most_five_points_a_side(site):
    debate = make_debate()
    gpt = replace(debate.participants[0], critique=[f"point {n}" for n in range(1, 8)])
    opp = site.add(debate=replace(debate, participants=[gpt, debate.participants[1]]))
    card = debate_card(site.client().get(f"/ideas/{opp.id}").text)
    assert "<li>point 5</li>" in card and "point 6" not in card


def test_an_agreed_debate_shows_the_merged_analysis(site):
    opp = site.add(debate=agreed_debate(), model="debate: gpt-5 vs claude-sonnet-5, agreed")
    page = site.client().get(f"/ideas/{opp.id}").text
    card = debate_card(page)
    assert '<span class="badge badge-ok">High agreement</span>' in card
    assert "GPT-5 and Claude Sonnet 5 analysed it separately and agreed, so no rebuttal or judge was needed." in card
    assert "Opening</th>" not in card and "<s>" not in card and "<mark>" not in card  # no rebuttal: nothing changed
    assert "Analyst A" not in card and "analyst-mark" not in card  # no judge saw them
    assert "Changed its mind" not in card and "Favoured by the judge" not in card
    assert "<h3>The merged analysis</h3>" in card and "so their analyses were merged.</p>" in card
    assert "The ruling" not in card and "It found" not in card
    assert "GPT-5 72% · Claude Sonnet 5 58% → 68% (agreed)" in page


def test_a_debate_without_rebuttals_goes_straight_to_the_judge(site):
    sides = agreed_debate().participants
    opp = site.add(debate=make_debate(rounds=0, participants=sides))
    card = debate_card(site.client().get(f"/ideas/{opp.id}").text)
    assert "GPT-5 and Claude Sonnet 5 analysed it separately, and Claude Sonnet 5 ruled on their analyses" in card
    assert "Opening</th>" not in card and "<h3>The ruling by Claude Sonnet 5</h3>" in card
    assert "Analyst B" in card  # the judge still saw them as A and B


def test_a_failed_judge_leaves_a_merge_by_rule(site):
    debate = make_debate(
        judge=None,
        favoured=None,
        rounds=2,
        summary="The judge wasn't available, so the two final positions were merged by rule: Mixed, 62% chance up.",
        reason="The judge anthropic:claude-sonnet-5 failed: the request timed out",
    )
    opp = site.add(debate=debate, model="debate: gpt-5 vs claude-sonnet-5, merged without a judge")
    page = site.client().get(f"/ideas/{opp.id}").text
    card = debate_card(page)
    assert "analysed it separately and answered each other twice, but the judge failed." in card
    assert "<h3>Merged by rule, without a judge</h3>" in card
    # Members: what happened, by the models' names; admins: the provider's error too.
    assert "The judge Claude Sonnet 5 was unavailable, so the two final positions were merged by rule.</p>" in card
    admin_card = debate_card(site.client("admin@example.com", role="admin").get(f"/ideas/{opp.id}").text)
    assert "The judge Claude Sonnet 5 failed: the request timed out.</p>" in admin_card  # names, not provider labels
    assert "<mark>66%</mark>" in card and "Changed its mind" in card  # the rebuttal still ran
    assert "Favoured by the judge" not in card and "It found" not in card
    assert "(medium agreement, no judge)" in page


def test_when_one_model_failed_the_other_stands_alone(site):
    opp = site.add(debate=lone_debate(), model="gpt-5 alone (claude-sonnet-5 unavailable)")
    page = site.client().get(f"/ideas/{opp.id}").text
    card = debate_card(page)
    assert '<h2 id="debate-title">The models</h2>' in card and "agreement</span>" not in card
    assert "<strong>Only GPT-5 answered.</strong>" in card
    assert "Claude Sonnet 5 was unavailable, so GPT-5 analysed it alone." in card and "no credit" not in card
    admin_card = debate_card(site.client("admin@example.com", role="admin").get(f"/ideas/{opp.id}").text)
    assert "Claude Sonnet 5 failed, so GPT-5 analysed it alone: no credit left." in admin_card
    assert "No second model checked this analysis, so read it with more care." in card
    assert card.count("<article") == 1 and "debate-sides is-pair" not in card
    assert "Opening</th>" not in card and "72%" in card
    assert "debate-ruling" not in card and "Analyst A" not in card
    assert "GPT-5 alone: 68% (the other model failed)" in page
    assert "By gpt-5 alone (claude-sonnet-5 unavailable)" in page


def test_single_model_ideas_have_no_debate_card(site):
    plain = site.add()  # LLM_ANALYSIS_MODE=single, and every record from before debates
    page = site.client().get(f"/ideas/{plain.id}").text
    assert 'id="debate"' not in page and "debate-line" not in page and "By fake-model" in page
    empty = site.add(debate=make_debate(participants=[]))  # a stored debate that lost its participants
    assert 'id="debate"' not in site.client().get(f"/ideas/{empty.id}").text


def test_model_text_in_the_debate_is_escaped(site):
    evil = "<script>alert(1)</script>"
    image = '<img src=x onerror="alert(2)">'
    debate = make_debate(
        summary=f"Summary {evil}",
        reason=f"Reason {image}",
        judge=f"anthropic:{image}",
        favoured=f"anthropic:{image}",
    )
    gpt, claude = debate.participants
    gpt = replace(gpt, critique=[f"Critique {evil}"], concessions=[f"Concession {image}"])
    claude = replace(claude, model=f"anthropic:{image}", critique=["[x](javascript:alert(3))"])
    opp = site.add(
        debate=replace(debate, participants=[gpt, claude]),
        analysis=make_analysis(probability_up_6m=64),
        model=f"debate: gpt-5 vs {image}",
    )
    client = site.client()
    for page in (client.get(f"/ideas/{opp.id}").text, client.get("/").text):
        assert evil not in page and image not in page and "<img" not in page
    page = client.get(f"/ideas/{opp.id}").text
    assert "Summary &lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "Critique &lt;script&gt;" in page and "Concession &lt;img src=x onerror=&#34;alert(2)&#34;&gt;" in page
    assert '<h3 id="debater-2">&lt;img src=x onerror=&#34;alert(2)&#34;&gt;</h3>' in page
    assert "The ruling by &lt;img" in page and "Its critique of &lt;img" in page
    assert "<li>[x](javascript:alert(3))</li>" in page  # text, never a link
    assert 'href="javascript' not in page


def test_idea_lists_show_the_debate_in_one_line(seeded):
    site, ids = seeded
    debated = site.add(
        ticker="SAP.DE",
        company="SAP SE",
        created=NOW - timedelta(minutes=30),
        price=190.0,
        currency="EUR",
        stats=make_stats(ticker="SAP.DE", price=190.0, currency="EUR", name="SAP SE", exchange="XETRA"),
        analysis=make_analysis(probability_up_6m=64, potential_low=160.0, entry_price=180.0, target_price=225.0),
        debate=make_debate(),
        model=JUDGED_MODEL,
    )
    line = "GPT-5 66% · Claude Sonnet 5 58% → 64% (medium agreement)"
    client = site.client()
    dashboard = client.get("/").text
    assert dashboard.count('<span class="debate-line">') == 1  # the other ideas had one model
    row = dashboard[dashboard.index(f'href="/ideas/{debated.id}"') :]
    row = row[: row.index("</a>")]
    assert f'<span class="visually-hidden">Debate: </span>{line}</span>' in row
    assert 'class="debate-mark"' in row and 'aria-hidden="true"' in row
    ticker = client.get("/tickers/SAP.DE").text
    assert ticker.count(line) == 1
    history = client.get(f"/ideas/{ids['sap']}").text  # the older idea lists the newer, debated one
    assert history.count(line) == 1 and 'id="debate"' not in history


# --- a ticker ------------------------------------------------------------------------------------------------------


def test_the_ticker_page(seeded):
    site, ids = seeded
    page = site.client().get("/tickers/AMD").text
    for text in ("A dip by the scanner's rules", "down 5.0% today", "52-week range", "Statistical 6-month low"):
        assert text in page, text
    assert 'class="chart-svg chart-wide"' in page and "The lines are the levels of the newest idea" in page
    assert f'href="/ideas/{ids["amd"]}"' in page and f'href="/ideas/{ids["amd_old"]}"' in page
    assert "AMD shares slide after weak data-center guidance" in page
    assert "Lower data-center guidance cuts expected revenue growth." in page
    assert "Add to watchlist" in page
    assert "Manual analyses aren&#39;t available right now." in page


def test_a_ticker_that_is_no_dip_says_so(site):
    site.prices.bars["KO"] = bars_until(TODAY, 60.0)
    original = site.prices.stats
    site.prices.stats = lambda ticker, now=None: replace(
        original(ticker, now=now), change_1d_pct=0.5, change_5d_pct=1.0, drawdown_20d_pct=-1.0
    )
    page = site.client().get("/tickers/KO").text
    assert "Not a dip by the scanner's rules" in page and "a fall of 3.0% on the day" in page
    assert "hasn't analysed KO yet" in page and "No news about KO" in page


def test_a_ticker_without_prices(site):
    page = site.client().get("/tickers/ZZZZ").text
    assert "Yahoo Finance has no prices for ZZZZ." in page and "No chart" in page


def test_a_ticker_without_prices_offers_no_analysis_and_names_its_new_symbol(tmp_path):
    """An analysis of a symbol Yahoo has no prices for fails at once: the page says so instead of offering "Analyse
    now" (each click cost a member one of their analyses), and points to the symbol the scanner found instead."""
    site = Site(tmp_path, analyse=lambda ticker, now: make_opportunity(ticker=ticker, created=now))
    client = site.client()
    page = client.get("/tickers/ZZZZQ").text
    assert "Yahoo Finance has no prices for ZZZZQ." in page and "ZZZZQ can&#39;t be analysed without prices." in page
    assert 'value="ZZZZQ"' not in page  # no "Analyse now" form, and no "Add to watchlist" either
    assert "Add to watchlist" not in page
    site.store.save_symbol_lookup("OPAP.AT", "OPAP", "ALWN.AT", "Allwyn International AG", checked=NOW)
    renamed = client.get("/tickers/OPAP.AT").text
    assert 'href="/tickers/ALWN.AT"' in renamed and "open that one to analyse it" in renamed
    # Yahoo merely unreachable: the button stays (it may work in a minute).
    site.prices.down.add("AMD")
    assert 'value="AMD"' in client.get("/tickers/AMD").text


def test_stock_pages_are_limited_for_members(site):
    """Each made-up symbol costs two Yahoo requests from the scanner's own address: a script must not get Yahoo to
    throttle the scanner for everybody."""
    client = site.client()
    for number in range(pages.TICKER_LIMIT):
        assert client.get(f"/tickers/ZZ{number}").status_code == 200
    blocked = client.get("/tickers/AMD")
    assert blocked.status_code == 429 and "stock pages in the last 15 minutes" in blocked.text
    stats_calls = len([call for call in site.prices.calls if call[0] == "stats"])
    assert stats_calls == pages.TICKER_LIMIT  # the blocked one asked Yahoo nothing
    admin = site.client("admin@example.com", role="admin")
    for number in range(pages.TICKER_LIMIT + 1):
        assert admin.get(f"/tickers/ZZ{number}").status_code == 200


def test_ticker_addresses_are_normalised(site):
    client = site.client()
    response = client.get("/tickers/brk.b")
    assert response.status_code == 303 and response.headers["location"] == "/tickers/BRK-B"
    bad = client.get("/tickers/not a symbol!")
    assert bad.status_code == 404 and "isn&#39;t a Yahoo Finance symbol" in bad.text


def test_looking_up_a_ticker(site):
    client = site.client()
    found = client.get("/tickers?symbol=%20sap.de%20")
    assert found.status_code == 303 and found.headers["location"] == "/tickers/SAP.DE"
    empty = client.get("/tickers?symbol=")
    assert empty.status_code == 303 and empty.headers["location"] == "/"
    assert "Type a Yahoo Finance symbol" in client.get("/").text
    client.get("/tickers?symbol=%24%24%24")
    assert "isn&#39;t a Yahoo Finance symbol" in client.get("/").text


def test_adding_and_removing_a_watchlist_ticker(site):
    client = site.client()
    added = post_form(client, "/tickers/AMD/watchlist", {"action": "add", "next": "/tickers/AMD"}, page="/tickers/AMD")
    assert added.status_code == 303 and added.headers["location"] == "/tickers/AMD"
    assert site.user().settings.watchlist == ("AMD",)
    page = client.get("/tickers/AMD").text
    assert "is on your watchlist now" in page and "On your watchlist · remove" in page
    removed = post_form(client, "/tickers/AMD/watchlist", {"action": "remove", "next": "https://evil.com/"})
    assert removed.headers["location"] == "/tickers/AMD"  # never another site
    assert site.user().settings.watchlist == ()
    odd = post_form(client, "/tickers/AMD/watchlist", {"action": "toggle"})
    assert odd.status_code == 303 and site.user().settings.watchlist == ()


def test_watchlist_changes_need_the_form_token(site):
    client = site.client()
    response = client.post("/tickers/AMD/watchlist", data={"action": "add"})
    assert response.status_code == 403 and site.user().settings.watchlist == ()


def test_a_full_watchlist_says_so(site):
    client = site.client(watchlist=tuple(f"T{n}" for n in range(accounts_module.MAX_WATCHLIST)))
    post_form(client, "/tickers/AMD/watchlist", {"action": "add"})
    assert "at most 100 symbols" in client.get("/tickers/AMD").text


def test_analyse_now_from_the_ticker_page(tmp_path):
    site = Site(tmp_path, analyse=lambda ticker, now: make_opportunity(ticker=ticker, created=now))
    client = site.client()
    page = client.get("/tickers/AMD").text
    assert 'name="next" value="/tickers/AMD"' in page and "You have 5 of 5 left in the last 24 hours." in page
    response = post_form(client, "/analyze", {"ticker": "AMD", "next": "/tickers/AMD"}, page="/tickers/AMD")
    assert response.status_code == 303 and response.headers["location"] == "/jobs/1"
    assert "You have 4 of 5 left" in client.get("/tickers/AMD").text
    limited = Site(tmp_path / "limited", analyse=lambda t, n: make_opportunity(), web={"analyze_limit_per_user": 0})
    assert "switched off for members" in limited.client().get("/tickers/AMD").text


def test_the_ticker_page_names_the_preferred_listing(tmp_path):
    config = ScannerConfig(universe=UniverseConfig(preferred_listings={"ASML": "ASML.AS"}))
    site = Site(tmp_path, config=config)
    page = site.client().get("/tickers/ASML").text
    assert 'href="/tickers/ASML.AS"' in page and "preferred_listings" in page


def test_the_watchlist_buttons_read_symbols_through_preferred_listings(tmp_path):
    """A watchlist with ASML is read as ASML.AS by the scanner, its alerts and the API: the pages must agree, and
    removing ASML.AS must really take it off (not leave ASML, which alerts on ASML.AS all the same)."""
    config = ScannerConfig(universe=UniverseConfig(preferred_listings={"ASML": "ASML.AS"}))
    prices = standard_prices()
    prices.bars["ASML.AS"] = bars_until(TODAY, 700.0)
    prices.currencies["ASML.AS"] = "EUR"
    site = Site(tmp_path, config=config, prices=prices)
    client = site.client(watchlist=("ASML",))
    idea = site.add(ticker="ASML.AS", company="ASML Holding", stats=make_stats(ticker="ASML.AS", price=700.0))
    remove_form = 'name="action" value="remove"'
    for path in ("/tickers/ASML.AS", "/tickers/ASML", f"/ideas/{idea.id}"):
        page = client.get(path).text
        assert remove_form in page and "On your watchlist" in page, path
    assert client.get(f"/api/v1/ideas/{idea.id}").json()["idea"]["on_my_watchlist"] is True

    post_form(client, "/tickers/ASML.AS/watchlist", {"action": "remove"}, page="/tickers/ASML.AS")
    assert site.user().settings.watchlist == ()
    assert client.get(f"/api/v1/ideas/{idea.id}").json()["idea"]["on_my_watchlist"] is False

    site.accounts.update_settings(site.user().id, replace(site.user().settings, watchlist=("ASML",)))
    post_form(client, "/tickers/ASML.AS/watchlist", {"action": "add"}, page="/tickers/ASML.AS")
    assert site.user().settings.watchlist == ("ASML",)  # no duplicate of the same listing


# --- the news ------------------------------------------------------------------------------------------------------


def articles_on(page: str) -> list[str]:
    return re.findall(r'<li class="article">\s*<span class="headline-title"><a [^>]*>([^<]+)</a>', page)


def test_the_news_digest(seeded):
    site, _ = seeded
    page = site.client().get("/news").text
    assert articles_on(page) == [
        "AMD shares slide after weak data-center guidance",
        "SAP cuts cloud outlook as customers delay deals",
        "Oil prices steady ahead of OPEC meeting",
    ]
    assert "3 articles from 3 sources in the last 24 hours, 3 companies flagged" in page
    companies = page[page.index("Companies in the news") : page.index("</section>", page.index("Companies in"))]
    assert companies.index(">AMD<") < companies.index(">SAP.DE<") < companies.index(">NVDA<")  # most worrying first
    assert "A lower cloud outlook trims growth." in page
    assert 'class="chip chip-negative" href="/tickers/AMD"' in page


def test_news_filters(seeded):
    site, _ = seeded
    client = site.client()
    assert len(articles_on(client.get("/news?hours=72").text)) == 4
    assert len(articles_on(client.get("/news?hours=6").text)) == 2
    assert len(articles_on(client.get("/news?hours=5").text)) == 3  # not a choice: 24 hours
    assert articles_on(client.get("/news?companies=1").text) == [
        "AMD shares slide after weak data-center guidance",
        "SAP cuts cloud outlook as customers delay deals",
    ]
    nvda = client.get("/news?ticker=nvda&hours=72").text
    assert articles_on(nvda) == ["AMD shares slide after weak data-center guidance", "Chip stocks rally on AI hopes"]
    assert 'News about <span class="ticker">NVDA</span>' in nvda
    bad = client.get("/news?ticker=%3Cb%3E!").text
    assert "isn&#39;t a Yahoo Finance symbol" in bad and len(articles_on(bad)) == 3
    assert "No news about TSLA" in client.get("/news?ticker=TSLA").text


def test_news_under_the_preferred_listing(tmp_path):
    config = ScannerConfig(universe=UniverseConfig(preferred_listings={"ASML": "ASML.AS"}))
    site = Site(tmp_path, config=config)
    article = make_article(title="ASML orders disappoint")
    site.store.add_articles([article], max_age_hours=72, now=NOW)
    site.store.record_triage([article.id], [make_impact(article_id=article.id, ticker="ASML", company="ASML")])
    client = site.client()
    assert 'href="/tickers/ASML.AS"' in client.get("/news").text
    assert articles_on(client.get("/news?ticker=ASML").text) == ["ASML orders disappoint"]


def test_news_pages_and_escapes(site):
    evil = "<script>alert(1)</script>"
    articles = [
        make_article(
            title=f"Story number {n} {evil}", link=f"https://example.com/{n}", published=NOW - timedelta(minutes=n)
        )
        for n in range(35)
    ]
    articles.append(make_article(title="A bad link", link="javascript:alert(1)", published=NOW - timedelta(hours=2)))
    site.store.add_articles(articles, max_age_hours=72, now=NOW)
    client = site.client()
    first = client.get("/news").text
    assert evil not in first and "javascript:" not in first
    assert len(articles_on(first)) == pages.NEWS_PAGE_SIZE and 'href="/news?page=2"' in first
    second = client.get("/news?page=2").text
    assert "A bad link" in second and len(articles_on(second)) == 5  # the unlinked one isn't counted by articles_on


def test_an_empty_news_page(site):
    assert "No articles in the last 24 hours" in site.client().get("/news").text


# --- the track record ----------------------------------------------------------------------------------------------


def test_the_track_record(tmp_path):
    report = NOW - timedelta(days=30)
    prices = standard_prices()
    prices.bars["AMD"] = bars_until(report.date() - timedelta(days=1), 142.5) + make_bars(
        [140.0, 131.0, 150.0, 170.0, 165.0] + [160.0] * 15, report.date()
    )
    prices.bars["SAP.DE"] = bars_until(TODAY, 180.0)
    site = Site(tmp_path, prices=prices)
    stats = make_stats(as_of=report - timedelta(hours=1), timezone="America/New_York", session_elapsed=0.5)
    site.add(created=report, stats=stats, fx_rates={"EUR": 0.8})
    sap_stats = make_stats(ticker="SAP.DE", price=190.0, currency="EUR", as_of=report, timezone="Europe/Berlin")
    site.add(
        ticker="SAP.DE",
        created=report,
        price=190.0,
        currency="EUR",
        stats=sap_stats,
        score=55.0,
        analysis=make_analysis(verdict="mixed", entry_price=185.0, target_price=220.0, potential_low=170.0),
    )
    site.add(ticker="GONE", created=report)
    page = site.client(currency="EUR").get("/track").text
    for text in (
        "Higher now than reported",
        "Average return since the report",
        "Versus the index",
        "Average return in EUR",
        "By verdict",
        "By score",
        "Every idea",
        "Temporary fear",
        "Mixed",
        "target hit",
        "In EUR",
        "Left out:",
        ">GONE</a> (1)",
    ):
        assert text in page, text
    assert "+12.3%" in page  # AMD: 160 / 142.5
    assert re.search(r'data-label="In EUR">\s*<span class="num (up|down)">[+-]\d', page)


def scoreboard_on(page: str) -> str:
    start = page.index('aria-labelledby="scoreboard-title"')
    return page[start : page.index("</section>", start)]


def scoreboard_row(board: str, label: str) -> list[str]:
    """The cells of a scoreboard row after its model cell, as text."""
    row = board[board.index(f"<strong>{label}</strong>") :]
    row = row[: row.index("</tr>")]
    return [re.sub(r"<[^>]+>", "", cell).strip() for cell in re.findall(r'data-label="[^"]+">(.*?)</td>', row)]


def test_the_track_record_scores_the_debating_models(site):
    old = NOW - timedelta(days=200)  # 6 months of results: AMD rose from about 141 to 149, so it was higher
    site.add(
        created=old, stats=make_stats(as_of=old), debate=make_debate(), analysis=make_analysis(probability_up_6m=64)
    )
    recent = NOW - timedelta(days=10)  # waiting for its result
    waiting = site.add(created=recent, stats=make_stats(as_of=recent), debate=make_debate())
    site.add(created=NOW - timedelta(days=20), stats=make_stats(as_of=NOW - timedelta(days=20)))  # one model
    page = site.client().get("/track").text
    board = scoreboard_on(page)
    assert "Model scoreboard" in board and "2 debated ideas" in board
    assert board.index("Claude Sonnet 5") < board.index("GPT-5") < board.index("After the debate")
    assert '<strong>GPT-5</strong> <span class="small muted">OpenAI</span>' in board
    # GPT-5: final 66% and opening 72% on a stock that was higher: (0.66 - 1)² = 0.1156, (0.72 - 1)² = 0.0784
    assert scoreboard_row(board, "GPT-5") == ["2", "1", "0.116", "0.078", "66%", "100%"]
    # Claude Sonnet 5 kept its 58%: (0.58 - 1)² = 0.1764 for both
    assert scoreboard_row(board, "Claude Sonnet 5") == ["2", "1", "0.176", "0.176", "58%", "100%"]
    # the debate's outcome, 64%: (0.64 - 1)² = 0.1296; it has no opening
    assert scoreboard_row(board, "After the debate") == ["2", "1", "0.130", "–", "64%", "100%"]
    due = signal_day(waiting) + timedelta(days=HORIZON_DAYS)
    wait = f"1 more debated idea waits for 6 months of results; the next is due about {due:%a} {due.day} {due:%b %Y}."
    assert wait in board


def test_the_scoreboard_shows_dashes_until_six_months_have_passed(site):
    recent = NOW - timedelta(days=10)
    site.add(created=recent, stats=make_stats(as_of=recent), debate=make_debate())
    site.add(created=recent, stats=make_stats(as_of=recent), debate=lone_debate())  # one model alone: not compared
    board = scoreboard_on(site.client().get("/track").text)
    assert scoreboard_row(board, "GPT-5") == ["1", "0", "–", "–", "–", "–"]
    assert scoreboard_row(board, "After the debate") == ["1", "0", "–", "–", "–", "–"]
    assert "No debated idea has 6 months of results yet, so the scores show – until then; the next is due" in board


def test_the_track_record_has_no_scoreboard_without_debates(site):
    site.add(created=NOW - timedelta(days=10), stats=make_stats(as_of=NOW - timedelta(days=10)))
    page = site.client().get("/track").text
    assert "Every idea" in page and "Model scoreboard" not in page


def test_track_prices_are_downloaded_once_an_hour(site):
    site.add(created=NOW - timedelta(days=10), stats=make_stats(as_of=NOW - timedelta(days=10)))
    client = site.client()
    client.get("/track")
    downloads = len(site.prices.calls)
    assert ("history", "AMD") in site.prices.calls and ("bars", "^GSPC") in site.prices.calls
    client.get("/track")
    assert len(site.prices.calls) == downloads  # from the cache
    site.ctx.cache.clear()
    client.get("/track")
    assert len(site.prices.calls) == 2 * downloads


def test_a_failed_download_is_retried_on_the_next_visit(site):
    site.add(created=NOW - timedelta(days=10), stats=make_stats(as_of=NOW - timedelta(days=10)))
    site.prices.down.add("AMD")
    client = site.client()
    assert "No prices just now for AMD (1 idea)" in client.get("/track").text
    site.prices.down.clear()
    assert "No prices just now" not in client.get("/track").text


def test_an_index_without_prices_is_a_note(site):
    site.add(created=NOW - timedelta(days=10), stats=make_stats(as_of=NOW - timedelta(days=10)))
    site.prices.missing.add("^GSPC")
    page = site.client().get("/track").text
    assert "No prices for the index S&amp;P 500 (^GSPC), so 1 idea show – next to it" in page


def test_an_empty_track_record(site):
    page = site.client().get("/track").text
    assert "No ideas since" in page and "Look at 2 years" in page
    assert site.prices.calls == []


def test_the_track_record_period(site):
    site.add(created=NOW - timedelta(days=100), stats=make_stats(as_of=NOW - timedelta(days=100)))
    client = site.client()
    assert "No ideas since" in client.get("/track?days=90").text
    assert "Every idea" in client.get("/track?days=180").text
    assert "Every idea" in client.get("/track?days=12345").text  # not a choice: a year


# --- helpers -------------------------------------------------------------------------------------------------------


def test_rule_misses_explain_why_an_idea_would_not_alert(site):
    user = site.accounts.create_user("rules@example.com", password=PASSWORD)
    config = ScannerConfig()
    good = make_opportunity()
    assert pages.rule_misses(good, user, config) == [] and pages.matches_rules(good, user, config)
    bad = make_opportunity(score=40.0, analysis=make_analysis(verdict="unclear", probability_up_6m=50))
    assert pages.rule_misses(bad, user, config) == [
        "its score 40.0 is under your 65",
        "its chance up of 50% is under your 60%",
        "you don't alert on “Unclear”",
    ]
    watcher = site.accounts.update_settings(user.id, replace(user.settings, only_watchlist=True, watchlist=("SAP",)))
    assert pages.rule_misses(good, watcher, config) == ["it isn't on your watchlist, and you alert only on those"]


def test_debater_names_tell_two_services_apart():
    assert pages.debater_names(make_debate()) == {
        "openai:gpt-5": "GPT-5",
        "anthropic:claude-sonnet-5": "Claude Sonnet 5",
    }
    gpt, claude = make_debate().participants
    twins = make_debate(participants=[gpt, replace(claude, model="azure:gpt-5")])
    assert pages.debater_names(twins) == {"openai:gpt-5": "GPT-5 (OpenAI)", "azure:gpt-5": "GPT-5 (Azure AI Foundry)"}
    view = pages.debate_view(make_opportunity(debate=replace(twins, judge="azure:gpt-5", favoured="azure:gpt-5")))
    assert view.judge_note.startswith("It found GPT-5 (Azure AI Foundry)'s case stronger.")
    assert [side.other for side in view.sides] == ["GPT-5 (Azure AI Foundry)", "GPT-5 (OpenAI)"]


def test_a_judge_from_outside_the_debate_is_named_so():
    view = pages.debate_view(make_opportunity(debate=make_debate(judge="openai:o3", favoured=None)))
    assert view.ruling_title == "The ruling by o3"
    assert view.judge_note == (
        "It favoured neither side. It saw the two labelled Analyst A and Analyst B, in an order that doesn't say which "
        "model wrote which."
    )
    assert pages.debate_view(make_opportunity()) is None


def test_readable_reasons_name_the_models():
    assert (
        pages.readable_reason("anthropic:claude-sonnet-5 failed, so openai:gpt-5 analysed it alone: no credit")
        == "Claude Sonnet 5 failed, so GPT-5 analysed it alone: no credit."
    )
    assert pages.readable_reason("The judge azure:my-deploy failed: 429.") == "The judge my-deploy failed: 429."
    assert pages.readable_reason("see https://x.test/openai:gpt-5 ") == "see https://x.test/openai:gpt-5."
    assert pages.readable_reason(None) is None and pages.readable_reason("  ") is None


def test_position_figures_mark_what_changed():
    figures = pages.position_figures(
        make_analysis(), make_analysis(probability_up_6m=60, confidence="low", target_price=168.004), "USD"
    )
    assert [(f.label, f.opening, f.final, f.changed) for f in figures] == [
        ("Chance up", "68%", "60%", True),
        ("Target", "$168.00", "$168.00", False),  # the same as shown
        ("Entry", "$132.00", "$132.00", False),
        ("Potential low", "$118.00", "$118.00", False),
        ("Confidence", "Medium", "Low", True),
    ]


def test_newest_per_ticker():
    older = make_opportunity(id=1, created=NOW - timedelta(days=1))
    newer = make_opportunity(id=2, created=NOW)
    other = make_opportunity(id=3, ticker="SAP.DE", created=NOW - timedelta(hours=1))
    ideas, counts = pages.newest_per_ticker([older, other, newer])
    assert [opp.id for opp in ideas] == [2, 3] and counts == {"AMD": 2, "SAP.DE": 1}


def test_cached_keeps_answers_and_missing_symbols_but_not_failures(site):
    calls = []

    def missing():
        calls.append("missing")
        raise PriceError("no such symbol")

    def down():
        calls.append("down")
        raise PriceFetchError("unreachable")

    for _ in range(2):
        with pytest.raises(PriceError, match="no such symbol"):
            pages.cached(site.ctx, "a", missing, seconds=60)
        with pytest.raises(PriceFetchError):
            pages.cached(site.ctx, "b", down, seconds=60)
        assert pages.cached(site.ctx, "c", lambda: calls.append("ok") or 7, seconds=60) == 7
    assert calls == ["missing", "down", "ok", "down"]


def test_chart_start_reaches_back_to_old_reports():
    assert pages.chart_start(TODAY) == TODAY - timedelta(days=pages.CHART_DAYS)
    old = TODAY - timedelta(days=300)
    assert pages.chart_start(TODAY, old) == old - timedelta(days=pages.CHART_CONTEXT_DAYS)


def test_every_benchmark_index_has_a_name():
    for symbol in {*BENCHMARKS.values(), EUROPE_BENCHMARK, DEFAULT_BENCHMARK}:
        assert pages.index_name(symbol) != symbol, symbol
    assert pages.index_name("^XYZ") == "^XYZ" and pages.index_name(None) == "the index"
