"""Fetch and parse the news feeds (RSS/Atom), normalise articles, and find per-ticker news.

feedparser only ever gets the bytes we downloaded ourselves (never a URL or a file name), so every request goes
through the session we were given: one User-Agent, one timeout, one place to fake in tests.
"""

from __future__ import annotations

import hashlib
import html
import io
import logging
import math
import re
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, quote_plus, unquote_plus, urlsplit, urlunsplit

import feedparser
import requests

from .models import Article, Feed, utc

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; news-dip-scanner/0.1; +https://github.com/venetisgr/work_stuff)"
ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.8"

MAX_SUMMARY_CHARS = 1500
# Feeds sometimes date items in the future (bad time zones, scheduled posts); beyond this we use the fetch time.
MAX_CLOCK_SKEW = timedelta(hours=1)

# Hosts (and their subdomains) that refuse requests unless the User-Agent names a contact (the SEC's fair-access
# policy answers 403 otherwise). Without one configured, such feeds aren't requested at all.
CONTACT_USER_AGENT_HOSTS = ("sec.gov",)
CONTACT_USER_AGENT_MISSING = (
    "sec.gov only answers requests whose User-Agent names a contact: set SEC_USER_AGENT in .env, "
    'e.g. "Jane Doe jane@example.com".'
)

YAHOO_TICKER_RSS = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US"
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}&hl={hl}&gl={gl}&ceid={ceid}"
# Google News editions for ticker_news, as (hl, gl, ceid, the word for "share" in that language): every listing is
# searched in the US English edition, and listings on these exchanges (by Yahoo suffix) in the local one as well.
GOOGLE_NEWS_ENGLISH = ("en-US", "US", "US:en", "stock")
GOOGLE_NEWS_LOCAL = {
    ".AT": ("el", "GR", "GR:el", "μετοχή"),
    ".DE": ("de", "DE", "DE:de", "Aktie"),
    ".PA": ("fr", "FR", "FR:fr", "action"),
    ".MI": ("it", "IT", "IT:it", "azioni"),
    ".MC": ("es", "ES", "ES:es", "acciones"),
    ".AS": ("nl", "NL", "NL:nl", "aandeel"),
}
# ticker_news: context headlines older than this are left out (Google News goes back years for quiet tickers).
MAX_CONTEXT_AGE = timedelta(days=30)

# Query parameters that only track where a click came from; dropping them makes the same story's links match.
_TRACKING_PARAMS = {"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "ref", "ref_src", ".tsrc"}

# " - Reuters", " | Yahoo Finance", " — Bloomberg" at the end of a headline: aggregators append the publisher.
_SEPARATOR = re.compile(r"\s+[-|\u2013\u2014]\s+")
_MAX_SOURCE_WORDS = 5
_MAX_SOURCE_CHARS = 40
# A suffix with these characters is part of the headline ("- Dow up 500 points", "(0000320193) (Filer)").
_NOT_A_SOURCE = re.compile(r"[\d()?!:,%$\"]")
# title_key only drops a " - X" suffix when X is one of these publishers (or the item's own <source>): a suffix can
# as well be a company ("Profit warning - Puma SE") or part of the headline ("... - and Washington is worried").
_KNOWN_PUBLISHERS = frozenset(
    {
        "reuters", "bloomberg", "bloomberg.com", "bnn bloomberg", "cnbc", "cnbc tv18", "marketwatch",
        "the wall street journal", "wall street journal", "wsj", "financial times", "ft", "ft.com", "yahoo finance",
        "yahoo", "yahoo news", "investing.com", "seeking alpha", "barron's", "barrons", "ap", "ap news",
        "associated press", "the information", "business insider", "insider", "markets insider", "fortune", "forbes",
        "cnn", "cnn business", "bbc", "bbc news", "the guardian", "guardian", "benzinga", "the motley fool",
        "motley fool", "nasdaq", "tipranks", "zacks", "zacks investment research", "simply wall st",
        "simply wall st.", "techcrunch", "the verge", "naftemporiki", "stocktwits", "aol", "msn", "fox business",
        "the economist", "axios", "politico", "the new york times", "nyt", "the washington post", "morningstar",
        "thestreet", "investopedia", "24/7 wall st", "24/7 wall st.", "9to5mac", "9to5google", "kiplinger",
        "electrek", "tom's hardware", "gurufocus", "marketbeat", "finviz", "quartz", "wired", "ars technica",
        "endpoints news", "fierce biotech", "fiercebiotech", "stat", "stat news", "oilprice.com", "euronews",
        "investor's business daily", "ibd", "the globe and mail", "nikkei asia", "south china morning post", "scmp",
        "fxstreet", "proactive investors", "sharecast", "the times", "the telegraph", "evening standard", "sky news",
        "capital.gr", "business wire", "businesswire", "pr newswire", "globenewswire", "accesswire", "techmeme",
        "reuters.com", "cnbc.com", "the hill", "semafor", "dow jones newswires",
    }
)  # fmt: skip
# GlobeNewswire links name the language: /news-release/2026/09/27/3369451/0/fr/...
_GLOBENEWSWIRE_LANGUAGE = re.compile(r"/news-release/\d{4}/\d{2}/\d{2}/\d+/\d+/([a-z]{2}(?:-[a-z]+)?)/")

_DROP_BLOCKS = re.compile(r"<(script|style|head|title)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_CDATA = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.DOTALL)
# Only things that look like tags, so "revenue < $5bn" survives.
_TAG = re.compile(r"</?[a-zA-Z][a-zA-Z0-9:-]*(?:\s[^<>]*)?/?>|<![a-zA-Z][^<>]*>")
_WHITESPACE = re.compile(r"\s+")
_PUNCTUATION = re.compile(r"[^\w\s]")


class FeedParseError(ValueError):
    """The downloaded document isn't an RSS or Atom feed (an HTML error page, a login wall, garbage)."""


@dataclass(frozen=True)
class FeedState:
    """What the server told us last time, for conditional GETs (If-None-Match / If-Modified-Since)."""

    etag: str | None = None
    last_modified: str | None = None


@dataclass(frozen=True)
class FeedResult:
    """The outcome of fetching one feed. Errors are reported here instead of raised."""

    feed: Feed
    articles: list[Article]
    state: FeedState
    status: int | None = None
    error: str | None = None
    not_modified: bool = False


# --- normalisation -------------------------------------------------------------------------------------------------


def canonical_link(url: str) -> str:
    """Drop utm_*/fbclid/gclid/ref query parameters and the #fragment, and lowercase the host.

    Anything that isn't an absolute http(s)-style URL is returned stripped but otherwise unchanged.
    """
    url = (url or "").strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.scheme or not parts.netloc:
        return url
    # Filter the raw "name=value" pairs so the ones we keep stay exactly as the publisher encoded them.
    query = "&".join(pair for pair in parts.query.split("&") if pair and not _is_tracking(pair.split("=", 1)[0]))
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, query, ""))


def _is_tracking(name: str) -> bool:
    name = unquote_plus(name).strip().lower()
    return name.startswith("utm_") or name in _TRACKING_PARAMS


def title_key(title: str, publisher: str | None = None) -> str:
    """Normalise a headline for cross-source dedup (casefold, no trailing " - Publisher", no punctuation).

    A trailing " - X" (or " | X", " — X") is only dropped when X is a known publisher (_KNOWN_PUBLISHERS) or the
    publisher the feed names for the item (publisher, e.g. Google News' <source>), with at least two words of headline
    left in front; up to two such suffixes go ("... chips – The Information - Investing.com"). So "AMD slides -
    Reuters" and "AMD slides" match, while "Profit warning - Continental AG" and "Profit warning - Puma SE" don't,
    and "8-K - APPLE INC (0000320193) (Filer)" keeps the company name.
    """
    headline = _WHITESPACE.sub(" ", html.unescape(title or "")).strip()
    for _ in range(2):
        separators = list(_SEPARATOR.finditer(headline))
        if not separators:
            break
        last = separators[-1]
        head, suffix = headline[: last.start()].strip(), headline[last.end() :].strip()
        if len(head.split()) < 2 or not _is_publisher(suffix, publisher):
            break
        headline = head
    return " ".join(_PUNCTUATION.sub(" ", headline.casefold()).split())


def _is_publisher(name: str, publisher: str | None) -> bool:
    def norm(text: str) -> str:
        return " ".join(text.casefold().replace("’", "'").split()).rstrip(".")

    wanted = norm(name)
    return bool(wanted) and (wanted == norm(publisher or "") or wanted in _KNOWN_PUBLISHERS)


def split_source(title: str) -> tuple[str, str | None]:
    """(headline, publisher) when the title ends in something that looks like " - Publisher", else (title, None).

    "Looks like": at most 5 words, no digits, brackets or sentence punctuation, and at least two words of headline
    in front. Only used to credit aggregator items to their publisher; title_key is stricter.
    """
    separators = list(_SEPARATOR.finditer(title))
    if not separators:
        return title, None
    last = separators[-1]
    head, source = title[: last.start()].strip(), title[last.end() :].strip()
    if (
        source
        and len(source.split()) <= _MAX_SOURCE_WORDS
        and len(source) <= _MAX_SOURCE_CHARS
        and not _NOT_A_SOURCE.search(source)
        and len(head.split()) >= 2
    ):
        return head, source
    return title, None


def strip_html(text: str) -> str:
    """Plain text from an HTML snippet: tags removed, entities unescaped, whitespace collapsed.

    Runs a second pass when unescaping reveals more markup (feeds that escape their HTML twice).
    """
    text = text or ""
    for _ in range(2):
        text = _CDATA.sub(r"\1", text)
        text = _DROP_BLOCKS.sub(" ", text)
        text = _COMMENT.sub(" ", text)
        text = _TAG.sub(" ", text)
        text = html.unescape(text)
        if not _TAG.search(text):
            break
    return _WHITESPACE.sub(" ", text.replace("\xa0", " ")).strip()


def _truncate(text: str, limit: int) -> str:
    """Cut text to at most limit characters at a word boundary, marking the cut with an ellipsis."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    if space > limit * 0.8:
        cut = cut[:space]
    return cut.rstrip(" ,;:-") + "…"


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


# --- parsing -------------------------------------------------------------------------------------------------------


def parse_feed(feed: Feed, content: bytes, *, now: datetime, content_type: str | None = None) -> list[Article]:
    """Articles from the raw bytes of an RSS or Atom document, in feed order, without duplicate ids.

    Dates are converted to UTC; an item without one gets now, and one dated more than an hour in the future gets
    now too. content_type (the HTTP header) helps feedparser pick the character encoding. Raises FeedParseError
    when the bytes aren't a feed at all; an empty but valid feed gives an empty list.

    Items tagged with a language the feed isn't set up for (Feed.languages; PR Newswire and GlobeNewswire publish
    machine translations of every release, each with its own link and title) are dropped. Items without a language
    tag are kept. Items whose headline matches one of Feed.exclude_titles (pages that aren't news) are dropped too.
    """
    return _parse(feed, content, now=utc(now), content_type=content_type, publisher_names=False)


def _parse(
    feed: Feed, content: bytes, *, now: datetime, content_type: str | None, publisher_names: bool
) -> list[Article]:
    headers = {"content-type": content_type} if content_type else None
    # A stream, not bytes: given bytes, feedparser first tries to open them as a file name.
    parsed = feedparser.parse(io.BytesIO(content or b""), response_headers=headers)
    if not parsed.entries and (parsed.get("bozo") or not parsed.get("version")):
        reason = parsed.get("bozo_exception") or "no RSS or Atom elements found"
        raise FeedParseError(f"Not an RSS or Atom feed ({reason}).")

    articles: list[Article] = []
    seen: set[str] = set()
    for entry in parsed.entries:
        article = _article(feed, entry, now=now, publisher_names=publisher_names)
        if article is not None and article.id not in seen:
            seen.add(article.id)
            articles.append(article)
    return articles


def _article(feed: Feed, entry: dict, *, now: datetime, publisher_names: bool) -> Article | None:
    title = strip_html(entry.get("title") or "")
    summary = strip_html(_entry_text(entry))
    link = canonical_link(_entry_link(entry))
    guid = (entry.get("id") or "").strip()
    if not _wanted_language(feed, _entry_language(entry, link)):
        return None
    headline_from_summary = not title
    if headline_from_summary:
        if not summary:
            return None
        title = _truncate(summary, 150)
    if any(pattern.search(title) for pattern in feed.exclude_titles):
        return None
    if link:
        article_id = _sha1(link)
    elif guid:
        article_id = _sha1(guid)
    else:  # nothing stable to go on: the same headline in the same feed is the same item
        article_id = _sha1(f"{feed.key}\n{title}")

    entry_source = strip_html((entry.get("source") or {}).get("title") or "")  # Google News names the publisher
    key = title_key(title, entry_source or None)
    # Google News descriptions just repeat the headline and publisher; that's no summary.
    if headline_from_summary:
        summary = "" if summary == title else summary
    elif key and title_key(summary, entry_source or None).startswith(key) and len(summary) <= len(title) + 80:
        summary = ""

    source_name = feed.name
    if publisher_names:  # aggregators: credit the publisher from <source>, else from a " - Publisher" suffix
        publisher = entry_source or split_source(title)[1]
        source_name = publisher or feed.name

    return Article(
        id=article_id,
        source=feed.key,
        source_name=source_name,
        title=title,
        link=link,
        summary=_truncate(summary, MAX_SUMMARY_CHARS),
        published=_entry_published(entry, now),
        fetched=now,
        title_key=key,
    )


def _entry_language(entry: dict, link: str) -> str:
    """The item's own language tag (dc:language), else the one in a GlobeNewswire link, lowercased; "" if none."""
    language = entry.get("language")
    if isinstance(language, str) and language.strip():
        return language.strip().lower().replace("_", "-")
    match = _GLOBENEWSWIRE_LANGUAGE.search(link) if "globenewswire.com" in link else None
    return match.group(1) if match else ""


def _wanted_language(feed: Feed, language: str) -> bool:
    if not language or not feed.languages:
        return True
    return any(language == wanted or language.startswith(f"{wanted}-") for wanted in feed.languages)


def _entry_link(entry: dict) -> str:
    link = entry.get("link") or ""
    if link:
        return link
    for candidate in entry.get("links") or []:
        if candidate.get("href") and candidate.get("rel", "alternate") == "alternate":
            return candidate["href"]
    guid = entry.get("id") or ""
    return guid if guid.startswith(("http://", "https://")) else ""


def _entry_text(entry: dict) -> str:
    summary = entry.get("summary") or ""
    if summary:
        return summary
    for content in entry.get("content") or []:
        if content.get("value"):
            return content["value"]
    return ""


def _entry_published(entry: dict, now: datetime) -> datetime:
    """The item's date in UTC (feedparser already converts to UTC), or now when missing or in the future."""
    for name in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(name)
        if not parsed:
            continue
        try:
            published = datetime(*parsed[:6], tzinfo=UTC)
        except (TypeError, ValueError):
            continue
        return now if published > now + MAX_CLOCK_SKEW else published
    return now


# --- fetching ------------------------------------------------------------------------------------------------------


def fetch_feed(
    session,
    feed: Feed,
    state: FeedState | None,
    *,
    now: datetime | None = None,
    timeout: float = 20,
    user_agent: str | None = None,
) -> FeedResult:
    """Fetch one feed with a conditional GET. Never raises: failures go in FeedResult.error.

    A 304 gives not_modified=True and no articles. On any failure the old state is kept, so the next attempt is
    still conditional; on success the state holds the ETag/Last-Modified the server just sent. user_agent replaces
    USER_AGENT for this request (SEC feeds need one with contact details, e.g. Settings.sec_user_agent); a feed on a
    CONTACT_USER_AGENT_HOSTS host without one isn't requested and gets CONTACT_USER_AGENT_MISSING as its error.
    """
    now = utc(now) if now is not None else datetime.now(UTC)
    if not user_agent and needs_contact_user_agent(feed.url):
        return FeedResult(feed=feed, articles=[], state=state or FeedState(), error=CONTACT_USER_AGENT_MISSING)
    return _fetch(session, feed, state, now=now, timeout=timeout, publisher_names=False, user_agent=user_agent)


def needs_contact_user_agent(url: str) -> bool:
    """Whether url is on a host that only answers a User-Agent with contact details (see CONTACT_USER_AGENT_HOSTS)."""
    return _host_matches(url, CONTACT_USER_AGENT_HOSTS)


def user_agent_for(url: str, user_agents: Mapping[str, str | None] | None) -> str | None:
    """The User-Agent configured for url's host in user_agents ({"sec.gov": "..."} also covers www.sec.gov)."""
    for suffix, agent in (user_agents or {}).items():
        if agent and _host_matches(url, (suffix,)):
            return agent
    return None


def _host_matches(url: str, suffixes: Sequence[str]) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    for suffix in suffixes:
        suffix = suffix.lower().lstrip(".")
        if host == suffix or host.endswith(f".{suffix}"):
            return True
    return False


def _fetch(
    session,
    feed: Feed,
    state: FeedState | None,
    *,
    now: datetime,
    timeout: float,
    publisher_names: bool,
    user_agent: str | None = None,
) -> FeedResult:
    old_state = state or FeedState()
    headers = {"User-Agent": user_agent or USER_AGENT, "Accept": ACCEPT}
    if old_state.etag:
        headers["If-None-Match"] = old_state.etag
    if old_state.last_modified:
        headers["If-Modified-Since"] = old_state.last_modified

    def failed(error: str, status: int | None = None) -> FeedResult:
        log.warning("Feed %s (%s): %s", feed.key, feed.url, error)
        return FeedResult(feed=feed, articles=[], state=old_state, status=status, error=error)

    http = session if session is not None else requests
    try:
        response = http.get(feed.url, headers=headers, timeout=timeout)
    except requests.Timeout:
        return failed(f"No answer within {timeout:g} seconds.")
    except requests.RequestException as exc:
        return failed(f"Couldn't connect: {exc}")
    except Exception as exc:  # never let one feed break the whole poll
        return failed(f"Unexpected error while fetching: {type(exc).__name__}: {exc}")

    status = response.status_code
    if status == 304:
        return FeedResult(feed=feed, articles=[], state=old_state, status=status, not_modified=True)
    if status >= 400 or status < 200:
        reason = getattr(response, "reason", "") or ""
        return failed(f"HTTP {status} {reason}".strip() + ".", status)

    try:
        articles = _parse(
            feed,
            response.content,
            now=now,
            content_type=response.headers.get("Content-Type"),
            publisher_names=publisher_names,
        )
    except FeedParseError as exc:
        return failed(str(exc), status)
    except Exception as exc:
        return failed(f"Couldn't parse the feed: {type(exc).__name__}: {exc}", status)

    new_state = FeedState(
        etag=response.headers.get("ETag") or None,
        last_modified=response.headers.get("Last-Modified") or None,
    )
    return FeedResult(feed=feed, articles=articles, state=new_state, status=status)


def fetch_all(
    session,
    feeds: Sequence[Feed],
    states: dict[str, FeedState],
    *,
    workers: int = 8,
    now: datetime | None = None,
    user_agents: Mapping[str, str | None] | None = None,
) -> list[FeedResult]:
    """Fetch the enabled feeds in parallel threads: one FeedResult per enabled feed, in the order given.

    Disabled feeds are skipped. All articles of one poll share the same fetch time. user_agents maps a host (and
    its subdomains) to the User-Agent to send there instead of USER_AGENT, e.g. {"sec.gov": settings.sec_user_agent};
    None values are ignored. Never raises.
    """
    now = utc(now) if now is not None else datetime.now(UTC)
    enabled = [feed for feed in feeds if feed.enabled]
    if not enabled:
        return []

    def fetch(feed: Feed) -> FeedResult:
        try:
            agent = user_agent_for(feed.url, user_agents)
            return fetch_feed(session, feed, states.get(feed.key), now=now, user_agent=agent)
        except Exception as exc:  # fetch_feed shouldn't raise, but one feed must never stop the others
            log.exception("Feed %s failed unexpectedly", feed.key)
            return FeedResult(feed=feed, articles=[], state=states.get(feed.key) or FeedState(), error=str(exc))

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(enabled))), thread_name_prefix="feed") as pool:
        return list(pool.map(fetch, enabled))


# --- per-ticker news -----------------------------------------------------------------------------------------------


def ticker_news(
    session,
    ticker: str,
    *,
    company: str | None = None,
    now: datetime | None = None,
    limit: int = 15,
    timeout: float = 15,
) -> list[Article]:
    """Recent headlines about one ticker (Yahoo per-ticker RSS + Google News), newest first. Never raises.

    Google News is searched for "<company or ticker> stock when:30d" in its US English edition, and for listings on
    the exchanges in GOOGLE_NEWS_LOCAL (Athens, Xetra, Paris, Milan, Madrid, Amsterdam) also in the local edition,
    with the local word for "share" ("Jumbo μετοχή when:30d" in Greek for BELA.AT). When the company's name is one
    word without its legal form, the English edition is searched for the name as given, quoted, or the symbol
    ('"Titan S.A." OR TITC stock when:30d'): "Titan stock" finds Titan Company, Titan Mining and Titan Machinery.
    Only headlines that are about the company are kept (the title or summary names it, or its symbol, see
    _mentions_matcher; Yahoo's per-ticker feed is full of unrelated roundups) and only those at most MAX_CONTEXT_AGE
    (30 days) old. They are deduplicated by title_key (so "AMD
    slides - Reuters" on Google and "AMD slides" on Yahoo count once), and no source gets more than its share of the
    places (half, or a third with a local edition) unless the others have too few. Every article's source is
    "ticker:<SYM>"; Google News items are credited to their publisher when the feed names one.
    """
    now = utc(now) if now is not None else datetime.now(UTC)
    symbol = ticker.strip().upper()
    source = f"ticker:{symbol}"
    name = company_core(company or "") or symbol
    sources = [(Feed(key=source, name="Yahoo Finance", url=YAHOO_TICKER_RSS.format(symbol=quote(symbol))), False)]
    bare, dot, suffix = symbol.rpartition(".")
    bare = bare if dot else symbol
    editions = [GOOGLE_NEWS_ENGLISH]
    if dot and (local := GOOGLE_NEWS_LOCAL.get(f".{suffix}")) is not None:
        editions.append(local)
    # A one-word name is often a common word or shared by other companies ("Titan": Titan Company, Titan Machinery,
    # "tech titan"): the English edition is searched for the full name as Yahoo writes it, or the symbol.
    english = name
    if company and len(name.split()) == 1:
        english = f'"{" ".join(company.replace(chr(34), "").split())}" OR {bare}'
    for hl, gl, ceid, word in editions:
        what = english if (hl, gl, ceid, word) == GOOGLE_NEWS_ENGLISH else name
        query = quote_plus(f"{what} {word} when:{MAX_CONTEXT_AGE.days}d")
        url = GOOGLE_NEWS_RSS.format(query=query, hl=hl, gl=gl, ceid=ceid)
        sources.append((Feed(key=source, name="Google News", url=url, languages=()), True))
    about = _mentions_matcher(symbol, company)
    oldest = now - MAX_CONTEXT_AGE

    tagged: list[tuple[int, Article]] = []
    for index, (feed, publisher_names) in enumerate(sources):
        result = _fetch(session, feed, None, now=now, timeout=timeout, publisher_names=publisher_names)
        # A failed source is logged and gives nothing; use what the other one gave.
        tagged += [(index, a) for a in result.articles if utc(a.published) >= oldest and about(a)]

    tagged.sort(key=lambda item: item[1].published, reverse=True)
    per_source: list[list[Article]] = [[] for _ in sources]
    seen_ids: set[str] = set()
    seen_titles: set[str] = set()
    for index, article in tagged:
        if article.id in seen_ids or article.title_key in seen_titles:
            continue
        seen_ids.add(article.id)
        if article.title_key:
            seen_titles.add(article.title_key)
        per_source[index].append(article)

    limit = max(0, limit)
    share = math.ceil(limit / len(sources))
    chosen = [article for items in per_source for article in items[:share]]
    rest = sorted((a for items in per_source for a in items[share:]), key=lambda a: a.published, reverse=True)
    chosen += rest[: max(0, limit - len(chosen))]
    chosen.sort(key=lambda article: article.published, reverse=True)
    return chosen[:limit]


# A legal form (or share class, or "Holding") at the end of a company name, dotted or not, after a space or comma:
# "Inc.", "S.A.", "N.V.", "S.p.A.", "AG", "SE", "PLC", "A/S", "Holding N.V.", "Class A". company_core removes these
# one at a time from the end, so the letters of "S.A." never end up as stray words.
_LEGAL_SUFFIX = re.compile(
    r"(?:\s*,\s*|\s+)(?:"
    r"inc|incorporated|corp|corporation|co|company|ltd|limited|llc|l\.?\s?p|plc|p\.l\.c|pty|gmbh|kgaa|"
    r"s\.?\s?a|s\.?\s?a\.?\s?s|s\.?\s?a\.?\s?b|s\.?\s?p\.?\s?a|s\.?\s?r\.?\s?l|n\.?\s?v|b\.?\s?v|a\.?\s?g|s\.?\s?e|"
    r"a/s|asa|ab|oyj|k\.?\s?k|holdings?|group|class\s+[a-c]|adrs?|de\s+c\.?\s?v"
    r")\.?(?:\s*\(publ\))?\s*$",
    re.IGNORECASE,
)
# What a legal form can leave behind at the end: "Henkel AG & Co. KGaA" -> "Henkel AG &" -> "Henkel AG".
_TRAILING_JOINER = re.compile(r"(?:\s*[,&-]|\s+and)\s*$", re.IGNORECASE)


def strip_legal_forms(company: str) -> str:
    """A company name without the legal forms, share classes and "Holding(s)"/"Group" at its end, dotted or not,
    otherwise as written: "Metlen Energy & Metals PLC" -> "Metlen Energy & Metals", "ASML Holding N.V." -> "ASML",
    "Eni S.p.A." -> "Eni". Only whole words at the end go, and the first word always stays, so "Group 1 Automotive"
    and "Inc Research" keep their names."""
    name = " ".join((company or "").split())
    while True:
        first = re.split(r"[\s,]+", name, maxsplit=1)[0]  # "Tesla, Inc." -> "Tesla", not "Tesla,"
        stripped = _TRAILING_JOINER.sub("", _LEGAL_SUFFIX.sub("", name))
        if stripped == name or len(stripped) < len(first):
            return name
        name = stripped


def company_core(company: str) -> str:
    """The distinctive part of a company name, for searching and for spotting it in headlines.

    Legal forms, share classes and "Holding(s)"/"Group" are removed from the end of the raw name first (see
    strip_legal_forms: "Advanced Micro Devices, Inc." -> "Advanced Micro Devices", "National Bank of Greece S.A." ->
    "National Bank of Greece"), then punctuation and a leading "The"; "&" becomes "and".
    """
    words = _PUNCTUATION.sub(" ", strip_legal_forms(company).replace("&", " and ")).split()
    while len(words) > 1 and words[0].casefold() == "the":
        words.pop(0)
    return " ".join(words)


def _mentions_matcher(symbol: str, company: str | None):
    """A test for "is this article about the company": its name (without legal-form words) as whole words, or its
    symbol without the exchange suffix, case-sensitive; symbols of one or two letters only as $T, (T) or :T.

    The headline is read without the " - Publisher" of the publisher the item is credited to (every item from "Stock
    Titan" ends in "Titan"). A one-word name only counts as a name: capitalised ("Titan", "TITAN", "Nike" for "NIKE")
    or exactly as written ("eBay"), not the common word ("tech titan", "a jumbo rate cut"). Longer names match in any
    case."""
    bare = symbol.split(".")[0]  # BELA.AT -> BELA
    if len(bare) <= 2:
        symbol_pattern = re.compile(rf"(?:\$|\(|:){re.escape(bare)}\b")
    else:
        symbol_pattern = re.compile(rf"(?<![\w$-]){re.escape(bare)}(?![\w-])|\${re.escape(bare)}\b")
    core = company_core(company or "")
    name_pattern = re.compile(rf"\b{re.escape(core)}\b", re.IGNORECASE) if core else None
    one_word = len(core.split()) == 1

    def named(match: re.Match[str]) -> bool:
        return not one_word or match.group() == core or match.group()[:1].isupper()

    def about(article: Article) -> bool:
        text = f"{_without_publisher(article)} {article.summary}"
        if symbol_pattern.search(text):
            return True
        plain = " ".join(_PUNCTUATION.sub(" ", text.replace("&", " and ")).split())
        return bool(name_pattern and any(named(match) for match in name_pattern.finditer(plain)))

    return about


def _without_publisher(article: Article) -> str:
    """The headline without a trailing " - Publisher" naming the publisher the item is credited to."""
    if not article.source_name:
        return article.title
    return re.sub(rf"{_SEPARATOR.pattern}{re.escape(article.source_name)}\s*$", "", article.title)
