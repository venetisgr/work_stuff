import hashlib
from datetime import timedelta
from pathlib import Path

import feedparser
import pytest
import requests
from conftest import NOW, FakeResponse, FakeSession

from dip_scanner.feeds import (
    USER_AGENT,
    FeedParseError,
    FeedState,
    canonical_link,
    fetch_all,
    fetch_feed,
    needs_contact_user_agent,
    parse_feed,
    strip_html,
    ticker_news,
    title_key,
)
from dip_scanner.models import Feed

FIXTURES = Path(__file__).parent / "fixtures"
MARKETWATCH = Feed(key="marketwatch", name="MarketWatch", url="https://feeds.example.com/marketwatch/topstories")
SEC = Feed(key="sec-8k", name="SEC 8-K filings", url="https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent")


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def rss(*items: str) -> bytes:
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>{"".join(items)}</channel></rss>'.encode()


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()


@pytest.fixture(autouse=True)
def no_feedparser_downloads(monkeypatch):
    """feedparser must only ever see bytes we downloaded; it must never fetch anything itself."""

    def refuse(*args, **kwargs):
        raise AssertionError("feedparser tried to download something")

    monkeypatch.setattr(feedparser.api.http, "get", refuse)


# --- canonical_link ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://WWW.Example.COM/Story/AMD-Slides?utm_source=rss&id=42&utm_medium=feed#comments",
            "https://www.example.com/Story/AMD-Slides?id=42",
        ),
        ("https://example.com/a?fbclid=abc&gclid=def&ref=rss&page=2", "https://example.com/a?page=2"),
        ("https://example.com/a?UTM_Campaign=x", "https://example.com/a"),
        (
            "https://finance.yahoo.com/news/amd-133000123.html?.tsrc=rss",
            "https://finance.yahoo.com/news/amd-133000123.html",
        ),
        ("  https://example.com/a?q=a%20b&x  ", "https://example.com/a?q=a%20b&x"),  # kept params stay as encoded
        ("https://example.com/a?reference=1", "https://example.com/a?reference=1"),  # only exact "ref" goes
        ("HTTP://Example.com:8080/Path", "http://example.com:8080/Path"),
        ("not a url", "not a url"),
        ("", ""),
    ],
)
def test_canonical_link(url, expected):
    assert canonical_link(url) == expected


# --- title_key -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("AMD shares slide after weak data-center guidance", "amd shares slide after weak data center guidance"),
        (
            "AMD shares slide after weak data-center guidance - Reuters",
            "amd shares slide after weak data center guidance",
        ),
        ("Analysts defend AMD after selloff | Yahoo Finance", "analysts defend amd after selloff"),
        ("Oil jumps on OPEC+ cuts — The Wall Street Journal", "oil jumps on opec cuts"),
        ("Stocks fall - Dow Jones - MarketWatch", "stocks fall dow jones"),  # only the last suffix goes
        ("Coca-Cola beats estimates - Reuters", "coca cola beats estimates"),
        ("Wall Street rallies - Dow up 500 points", "wall street rallies dow up 500 points"),  # digits: headline
        ("Is AMD a buy - or a trap?", "is amd a buy or a trap"),
        ("8-K - ADVANCED MICRO DEVICES INC (0000002488) (Filer)", "8 k advanced micro devices inc 0000002488 filer"),
        ("Tesla - Reuters", "tesla reuters"),  # too little headline left to be sure
        (
            "Markets wait - for the central bank to finally make up its mind",  # 11 words is no publisher
            "markets wait for the central bank to finally make up its mind",
        ),
        ("  Boeing &amp; Airbus:   deliveries   SLOW  ", "boeing airbus deliveries slow"),
        ("Η ΔΕΗ ανεβαίνει - Naftemporiki", "η δεη ανεβαίνει"),
        ("", ""),
    ],
)
def test_title_key(title, expected):
    assert title_key(title) == expected


# --- strip_html ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("html_text", "expected"),
    [
        ("<p>AMD <b>cut</b> its&nbsp;outlook &amp; fell.</p><p>More&#8230;</p>", "AMD cut its outlook & fell. More…"),
        ("<script>var x = '<b>';</script><style>p {}</style>Text <!-- note --> here", "Text here"),
        ("&lt;b&gt;Boeing&lt;/b&gt; delivered 38 jets", "Boeing delivered 38 jets"),  # escaped twice
        ("Revenue &lt; $5bn and margin &gt; 40%", "Revenue < $5bn and margin > 40%"),
        ("Line one<br/>line\n\t two", "Line one line two"),
        ("<![CDATA[Inside <i>cdata</i>]]>", "Inside cdata"),
        ("", ""),
    ],
)
def test_strip_html(html_text, expected):
    assert strip_html(html_text) == expected


# --- parse_feed ----------------------------------------------------------------------------------------------------


def test_parse_rss_with_html_descriptions_and_tracking_links():
    articles = parse_feed(MARKETWATCH, fixture("rss_marketwatch.xml"), now=NOW)

    assert [a.title for a in articles] == [
        "AMD shares slide after weak data-center guidance",
        "Boeing & Airbus deliveries slow as supply-chain snags persist",
        "Fed's Waller says more rate cuts are likely if the job market weakens",
        "TSMC to raise prices on advanced chips in 2027, Nvidia and Apple among customers hit",
    ]
    amd, boeing, fed, tsmc = articles
    assert amd.link == (
        "https://www.marketwatch.com/story/amd-shares-slide-after-weak-data-center-guidance-2026-09-25"
        "?mod=mw_rss_topstories"
    )
    assert amd.id == sha1(amd.link)
    assert amd.summary == (
        "Advanced Micro Devices Inc. cut its data-center revenue outlook for the fourth quarter, citing slower cloud "
        "spending & inventory digestion. Shares fell 7% in premarket trading."
    )
    assert amd.published == NOW.replace(hour=13, minute=5)
    assert (amd.source, amd.source_name, amd.fetched) == ("marketwatch", "MarketWatch", NOW)
    assert amd.title_key == "amd shares slide after weak data center guidance"

    assert (
        boeing.link
        == "https://www.marketwatch.com/story/boeing-airbus-deliveries-slow-2026-09-25?mod=mw_rss_topstories"
    )
    assert (
        boeing.summary == "Boeing delivered 38 jets in August — down from 44 a year earlier & well short of its target."
    )
    assert boeing.published == NOW.replace(hour=13, minute=30)  # 09:30 -0400
    assert boeing.published.utcoffset() == timedelta(0)

    assert fed.link == "https://www.marketwatch.com/story/feds-waller-says-more-rate-cuts-likely-2026-09-25"
    assert fed.published == NOW  # no date in the feed: the fetch time
    assert tsmc.published == NOW  # dated tomorrow: not believable, so the fetch time


def test_parse_sec_atom_feed_in_latin1():
    first, second = parse_feed(SEC, fixture("atom_sec.xml"), now=NOW)

    assert first.title == "8-K - ADVANCED MICRO DEVICES INC (0000002488) (Filer)"
    assert first.link == (
        "https://www.sec.gov/Archives/edgar/data/2488/000000248826000071/0000002488-26-000071-index.htm"
    )
    assert first.id == sha1(first.link)
    assert first.published == NOW.replace(hour=14, minute=32, second=15)  # <updated> 10:32:15 -04:00
    assert first.summary == (
        "Filed: 2026-09-25 AccNo: 0000002488-26-000071 Size: 312 KB Item 2.02: Results of Operations and Financial "
        "Condition Item 9.01: Financial Statements and Exhibits"
    )
    assert second.title == "8-K - SOCIÉTÉ GÉNÉRALE AMERICAS HOLDINGS INC (0001234567) (Filer)"
    assert first.title_key != second.title_key  # filings of different companies never look like duplicates


def test_parse_uses_guid_when_there_is_no_link_and_caps_the_summary():
    long_text = "word " * 600
    articles = parse_feed(
        MARKETWATCH,
        rss(
            f"<item><title>No link here</title><guid isPermaLink='false'>tag:mw,2026:1</guid>"
            f"<description>{long_text}</description></item>"
        ),
        now=NOW,
    )
    [article] = articles
    assert article.link == ""
    assert article.id == sha1("tag:mw,2026:1")
    assert len(article.summary) <= 1500
    assert article.summary.endswith("…")
    assert article.summary.startswith("word word")


def test_parse_drops_duplicate_and_empty_items_and_uses_summary_when_title_is_missing():
    items = (
        "<item><title>Same story</title><link>https://example.com/a?utm_source=x</link></item>",
        "<item><title>Same story again</title><link>https://example.com/a#top</link></item>",
        "<item><title></title><description></description></item>",
        "<item><description>Only a description, no headline.</description><link>https://example.com/b</link></item>",
    )
    articles = parse_feed(MARKETWATCH, rss(*items), now=NOW)
    assert [(a.title, a.link) for a in articles] == [
        ("Same story", "https://example.com/a"),
        ("Only a description, no headline.", "https://example.com/b"),
    ]


def test_parse_keeps_a_long_description_used_as_the_headline():
    text = "Shares of Example Corp jumped after the company raised its outlook for the year " * 3
    [article] = parse_feed(MARKETWATCH, rss(f"<item><description>{text}</description></item>"), now=NOW)
    assert len(article.title) <= 150 and article.title.endswith("…")
    assert article.summary == text.strip()


def test_parse_drops_a_summary_that_only_repeats_the_headline():
    [article] = parse_feed(MARKETWATCH, fixture("rss_google_amd.xml"), now=NOW)[:1]
    assert article.title == "AMD shares slide after weak data-center guidance - Reuters"
    assert article.summary == ""
    assert article.source_name == "MarketWatch"  # plain parse_feed always credits the feed


def test_parse_a_naive_now_is_taken_as_utc():
    [article] = parse_feed(MARKETWATCH, rss("<item><title>Undated news</title></item>"), now=NOW.replace(tzinfo=None))
    assert article.published == NOW and article.fetched == NOW


def test_parse_an_empty_but_valid_feed_gives_no_articles():
    assert parse_feed(MARKETWATCH, rss(), now=NOW) == []


@pytest.mark.parametrize(
    "content",
    [b"<html><body><h1>Access denied</h1></body></html>", b"", b"https://example.com/feed.xml", b"/etc/hostname"],
)
def test_parse_rejects_documents_that_are_not_feeds_without_fetching_them(content):
    with pytest.raises(FeedParseError, match="Not an RSS or Atom feed"):
        parse_feed(MARKETWATCH, content, now=NOW)


# --- fetch_feed ----------------------------------------------------------------------------------------------------


def test_fetch_sends_user_agent_and_conditional_headers_and_returns_the_new_state():
    response = FakeResponse(
        content=fixture("rss_marketwatch.xml"),
        headers={"ETag": '"v2"', "Last-Modified": "Fri, 25 Sep 2026 14:58:00 GMT", "Content-Type": "application/xml"},
    )
    session = FakeSession({MARKETWATCH.url: response})

    result = fetch_feed(
        session, MARKETWATCH, FeedState(etag='"v1"', last_modified="Thu, 24 Sep 2026 10:00:00 GMT"), now=NOW
    )

    [call] = session.calls
    assert call["headers"]["User-Agent"] == USER_AGENT
    assert call["headers"]["If-None-Match"] == '"v1"'
    assert call["headers"]["If-Modified-Since"] == "Thu, 24 Sep 2026 10:00:00 GMT"
    assert call["timeout"] == 20
    assert (result.status, result.error, result.not_modified) == (200, None, False)
    assert len(result.articles) == 4
    assert result.state == FeedState(etag='"v2"', last_modified="Fri, 25 Sep 2026 14:58:00 GMT")


def test_fetch_without_state_sends_no_conditional_headers():
    session = FakeSession({MARKETWATCH.url: fixture("rss_marketwatch.xml")})
    result = fetch_feed(session, MARKETWATCH, None, now=NOW)
    assert "If-None-Match" not in session.calls[0]["headers"]
    assert "If-Modified-Since" not in session.calls[0]["headers"]
    assert result.state == FeedState()  # the server sent no validators


def test_fetch_304_is_not_modified_and_keeps_the_state():
    state = FeedState(etag='"v1"', last_modified=None)
    result = fetch_feed(FakeSession({MARKETWATCH.url: 304}), MARKETWATCH, state, now=NOW)
    assert result.not_modified
    assert (result.status, result.error, result.articles, result.state) == (304, None, [], state)


@pytest.mark.parametrize(
    ("route", "status", "error"),
    [
        (503, 503, "HTTP 503"),
        (403, 403, "HTTP 403"),
        (requests.ConnectionError("connection refused"), None, "Couldn't connect: connection refused"),
        (requests.ReadTimeout("slow"), None, "No answer within 20 seconds"),
        (RuntimeError("boom"), None, "Unexpected error while fetching: RuntimeError: boom"),
        (b"<html><body>Please enable JavaScript</body></html>", 200, "Not an RSS or Atom feed"),
    ],
)
def test_fetch_errors_are_captured_and_the_old_state_kept(route, status, error):
    state = FeedState(etag='"v1"', last_modified="Thu, 24 Sep 2026 10:00:00 GMT")
    result = fetch_feed(FakeSession({MARKETWATCH.url: route}), MARKETWATCH, state, now=NOW)
    assert result.status == status
    assert error in result.error
    assert result.articles == [] and not result.not_modified
    assert result.state == state


# --- fetch_all -----------------------------------------------------------------------------------------------------


def test_fetch_all_keeps_feed_order_skips_disabled_feeds_and_captures_errors():
    feeds = [
        Feed(key="sec", name="SEC", url="https://sec.example.com/8k"),
        Feed(key="down", name="Down", url="https://down.example.com/rss"),
        Feed(key="off", name="Off", url="https://off.example.com/rss", enabled=False),
        Feed(key="mw", name="MarketWatch", url="https://mw.example.com/rss"),
        Feed(key="same", name="Unchanged", url="https://same.example.com/rss"),
    ]
    session = FakeSession(
        {
            "https://sec.example.com/": fixture("atom_sec.xml"),
            "https://down.example.com/": requests.ConnectionError("down"),
            "https://mw.example.com/": fixture("rss_marketwatch.xml"),
            "https://same.example.com/": 304,
        }
    )
    states = {"same": FeedState(etag='"abc"'), "mw": FeedState(last_modified="Thu, 24 Sep 2026 10:00:00 GMT")}

    results = fetch_all(session, feeds, states, workers=3, now=NOW)

    assert [r.feed.key for r in results] == ["sec", "down", "mw", "same"]
    assert [len(r.articles) for r in results] == [2, 0, 4, 0]
    assert [r.error is None for r in results] == [True, False, True, True]
    assert results[3].not_modified and results[3].state == states["same"]
    assert "https://off.example.com/rss" not in session.urls
    headers = {call["url"]: call["headers"] for call in session.calls}
    assert headers["https://same.example.com/rss"]["If-None-Match"] == '"abc"'
    assert headers["https://mw.example.com/rss"]["If-Modified-Since"] == "Thu, 24 Sep 2026 10:00:00 GMT"
    assert {a.fetched for r in results for a in r.articles} == {NOW}


def test_fetch_all_sends_a_configured_user_agent_to_matching_hosts_only():
    feeds = [
        Feed(key="sec", name="SEC", url="https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&output=atom"),
        Feed(key="notsec", name="Look-alike", url="https://notsec.gov.example.com/rss"),
        Feed(key="mw", name="MarketWatch", url="https://mw.example.com/rss"),
    ]
    session = FakeSession({"https://": fixture("rss_marketwatch.xml")})
    agents = {"sec.gov": "Jane Doe jane@example.com", "example.com": None}

    fetch_all(session, feeds, {}, now=NOW, user_agents=agents)

    sent = {call["url"]: call["headers"]["User-Agent"] for call in session.calls}
    assert sent == {
        feeds[0].url: "Jane Doe jane@example.com",
        feeds[1].url: USER_AGENT,
        feeds[2].url: USER_AGENT,  # None means "no override"
    }
    single = FakeSession({"https://": fixture("rss_marketwatch.xml")})
    fetch_feed(single, feeds[0], None, now=NOW, user_agent="Jane Doe jane@example.com")
    assert single.calls[0]["headers"]["User-Agent"] == "Jane Doe jane@example.com"


def test_sec_feeds_are_not_requested_without_a_contact_user_agent():
    sec = Feed(key="sec", name="SEC", url="https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&output=atom")
    session = FakeSession({"https://": fixture("atom_sec.xml")})
    state = FeedState(etag='"old"')

    result = fetch_feed(session, sec, state, now=NOW)
    results = fetch_all(session, [sec, MARKETWATCH], {}, now=NOW, user_agents={"sec.gov": None})

    assert session.urls == [MARKETWATCH.url]  # sec.gov was never asked
    assert result.articles == [] and result.state == state and "SEC_USER_AGENT" in result.error
    assert "SEC_USER_AGENT" in results[0].error and results[1].error is None
    assert needs_contact_user_agent("https://efts.sec.gov/x") and not needs_contact_user_agent(MARKETWATCH.url)


def test_fetch_all_with_nothing_to_fetch():
    assert fetch_all(FakeSession(), [], {}, now=NOW) == []


# --- ticker_news ---------------------------------------------------------------------------------------------------

YAHOO = "https://feeds.finance.yahoo.com/rss/2.0/headline"
GOOGLE = "https://news.google.com/rss/search"


def test_ticker_news_merges_yahoo_and_google_newest_first_without_duplicates():
    session = FakeSession({YAHOO: fixture("rss_yahoo_amd.xml"), GOOGLE: fixture("rss_google_amd.xml")})

    articles = ticker_news(session, "amd", company="Advanced Micro Devices", now=NOW)

    assert session.urls == [
        f"{YAHOO}?s=AMD&region=US&lang=en-US",
        f"{GOOGLE}?q=Advanced+Micro+Devices+stock&hl=en-US&gl=US&ceid=US:en",
    ]
    assert [(a.source_name, a.title) for a in articles] == [
        ("Barron's", "Analysts defend AMD after selloff, see AI demand intact - Barron's"),
        ("Yahoo Finance", "Is AMD stock a buy after the guidance cut?"),
        # Reuters' copy of this story on Google News (13:10) is a duplicate of Yahoo's (13:30).
        ("Yahoo Finance", "AMD shares slide after weak data-center guidance"),
        ("CNBC", "AMD's MI400 ramp is on track, says CEO Lisa Su - CNBC"),
        ("Yahoo Finance", "AMD to present at Nasdaq investor conference"),
        ("The Motley Fool", "AMD stock hits a record high on AI optimism - The Motley Fool"),
    ]
    assert {a.source for a in articles} == {"ticker:AMD"}
    assert articles[2].link == "https://finance.yahoo.com/news/amd-shares-slide-weak-data-133000123.html"


def test_ticker_news_searches_google_for_the_ticker_without_a_company_and_respects_the_limit():
    session = FakeSession({YAHOO: fixture("rss_yahoo_amd.xml"), GOOGLE: fixture("rss_google_amd.xml")})
    articles = ticker_news(session, "SAP.DE", now=NOW, limit=2)
    assert session.urls[0] == f"{YAHOO}?s=SAP.DE&region=US&lang=en-US"
    assert session.urls[1].startswith(f"{GOOGLE}?q=SAP.DE+stock&")
    assert len(articles) == 2
    assert {a.source for a in articles} == {"ticker:SAP.DE"}


def test_ticker_news_uses_whatever_source_answered():
    session = FakeSession({YAHOO: 404, GOOGLE: fixture("rss_google_amd.xml")})
    articles = ticker_news(session, "AMD", now=NOW)
    assert len(articles) == 4
    assert all(a.source_name != "Google News" for a in articles)  # every item names its publisher

    session = FakeSession({YAHOO: requests.ConnectionError("down"), GOOGLE: requests.ReadTimeout("slow")})
    assert ticker_news(session, "AMD", now=NOW) == []
