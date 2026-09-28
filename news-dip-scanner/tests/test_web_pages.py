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
from conftest import make_analysis, make_article, make_bars, make_impact, make_opportunity, make_stats
from fastapi.testclient import TestClient

from dip_scanner import accounts as accounts_module
from dip_scanner.config import DATABASE_NAME, ScannerConfig, Settings, UniverseConfig, WebSettings
from dip_scanner.models import PriceBar, Split
from dip_scanner.prices import PriceError, PriceFetchError
from dip_scanner.track import BENCHMARKS, DEFAULT_BENCHMARK, EUROPE_BENCHMARK
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


def test_the_dashboard_shows_thesis_changes(seeded):
    site, ids = seeded
    page = site.client().get("/").text
    assert "Thesis changes" in page and "Review open orders" in page
    assert "no longer passes your alert rules" in page
    assert f'href="/ideas/{ids["amd_old"]}">the earlier idea' in page
    # Rules the earlier idea didn't pass either: nothing to review.
    strict = site.client("strict@example.com", min_score=90.0).get("/").text
    assert "Thesis changes" not in strict


def test_a_drop_in_the_chance_up_is_a_thesis_change(site):
    site.add(created=NOW - timedelta(days=2), score=80.0, analysis=make_analysis(probability_up_6m=85))
    site.add(created=NOW - timedelta(hours=1), score=70.0, analysis=make_analysis(probability_up_6m=62))
    page = site.client().get("/").text
    assert "its chance of being higher fell by 23 points" in page


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
    assert "Manual analyses aren&#39;t available on this server." in plain.client().get(f"/ideas/{other.id}").text


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
    assert "Manual analyses aren&#39;t available on this server." in page


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
