"""Fetch and parse the news feeds (RSS/Atom), normalise articles, and find per-ticker news.

feedparser only ever gets the bytes we downloaded ourselves (never a URL or a file name), so every request goes
through the session we were given: one User-Agent, one timeout, one place to fake in tests.
"""

from __future__ import annotations

import hashlib
import html
import io
import logging
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
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"

# Query parameters that only track where a click came from; dropping them makes the same story's links match.
_TRACKING_PARAMS = {"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "ref", "ref_src", ".tsrc"}

# " - Reuters", " | Yahoo Finance", " — Bloomberg" at the end of a headline: aggregators append the publisher.
_SEPARATOR = re.compile(r"\s+[-|\u2013\u2014]\s+")
_MAX_SOURCE_WORDS = 5
_MAX_SOURCE_CHARS = 40
# A suffix with these characters is part of the headline ("- Dow up 500 points", "(0000320193) (Filer)").
_NOT_A_SOURCE = re.compile(r"[\d()?!:,%$\"]")

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


def title_key(title: str) -> str:
    """Normalise a headline for cross-source dedup (casefold, no trailing " - Source", no punctuation).

    The publisher suffix is only dropped when it looks like one: at most 5 words, no digits, brackets or
    sentence punctuation, and at least two words of headline left in front of it. So "AMD slides - Reuters"
    and "AMD slides" match, while "8-K - APPLE INC (0000320193) (Filer)" keeps the company name.
    """
    headline, _ = split_source(_WHITESPACE.sub(" ", html.unescape(title or "")).strip())
    return " ".join(_PUNCTUATION.sub(" ", headline.casefold()).split())


def split_source(title: str) -> tuple[str, str | None]:
    """(headline, publisher) when the title ends in something that looks like " - Publisher", else (title, None)."""
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
    headline_from_summary = not title
    if headline_from_summary:
        if not summary:
            return None
        title = _truncate(summary, 150)
    if link:
        article_id = _sha1(link)
    elif guid:
        article_id = _sha1(guid)
    else:  # nothing stable to go on: the same headline in the same feed is the same item
        article_id = _sha1(f"{feed.key}\n{title}")

    key = title_key(title)
    # Google News descriptions just repeat the headline and publisher; that's no summary.
    if headline_from_summary:
        summary = "" if summary == title else summary
    elif key and title_key(summary).startswith(key) and len(summary) <= len(title) + 80:
        summary = ""

    source_name = feed.name
    if publisher_names:  # aggregators: credit the publisher from <source>, else from a " - Publisher" suffix
        publisher = strip_html((entry.get("source") or {}).get("title") or "") or split_source(title)[1]
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

    Google News is searched for "<company or ticker> stock". Headlines are merged and deduplicated by title_key
    (so "AMD slides - Reuters" on Google and "AMD slides" on Yahoo count once). Every article's source is
    "ticker:<SYM>"; Google News items are credited to their publisher when the feed names one.
    """
    now = utc(now) if now is not None else datetime.now(UTC)
    symbol = ticker.strip().upper()
    source = f"ticker:{symbol}"
    query = f"{(company or '').strip() or symbol} stock"
    sources = [
        (Feed(key=source, name="Yahoo Finance", url=YAHOO_TICKER_RSS.format(symbol=quote(symbol))), False),
        (Feed(key=source, name="Google News", url=GOOGLE_NEWS_RSS.format(query=quote_plus(query))), True),
    ]

    articles: list[Article] = []
    for feed, publisher_names in sources:
        result = _fetch(session, feed, None, now=now, timeout=timeout, publisher_names=publisher_names)
        articles += result.articles  # a failed source is logged and gives nothing; use what the other one gave

    articles.sort(key=lambda article: article.published, reverse=True)
    merged: list[Article] = []
    seen_ids: set[str] = set()
    seen_titles: set[str] = set()
    for article in articles:
        if article.id in seen_ids or article.title_key in seen_titles:
            continue
        seen_ids.add(article.id)
        if article.title_key:
            seen_titles.add(article.title_key)
        merged.append(article)
    return merged[: max(0, limit)]
