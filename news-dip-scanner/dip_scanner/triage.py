"""News triage: ask the model which listed companies each article affects (batched).

The model sees a batch of articles under short ids (a1, a2, ...) instead of the 40-character article hashes: they
cost fewer tokens and the model can't mistype them. Its reply is checked for shape (validate_triage), then every
company entry is cleaned up: tickers are normalised to Yahoo Finance symbols, enum values are coerced, magnitudes
clamped, and entries that can't be repaired (no usable ticker, unknown direction, ETFs, indices, crypto) are dropped.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from . import prompts
from .config import ConfigError
from .llm import ChatModel, LLMError, LLMRequestError, LLMSetupError, LLMUnavailableError, complete_json
from .models import DIRECTIONS, EVENT_TYPES, RELATIONS, Article, Impact, utc

if TYPE_CHECKING:
    from .store import Store

log = logging.getLogger(__name__)

MAX_COMPANIES_PER_ARTICLE = 5
SUMMARY_CHARS = 700  # per article sent to the model; RSS summaries are usually shorter
RATIONALE_CHARS = 300

# --- ticker normalisation ------------------------------------------------------------------------------------------

# "NASDAQ:AMD" style prefixes (Google Finance, TradingView, news sites). US ones are dropped; the others become the
# Yahoo Finance suffix. TSE is Toronto on Google Finance but Tokyo elsewhere; numeric codes decide (see below).
_US_EXCHANGES = {
    "NASDAQ", "NASDAQGS", "NASDAQGM", "NASDAQCM", "NYSE", "NYSEARCA", "NYSEAMERICAN", "NYSEMKT", "AMEX", "ARCA",
    "BATS", "CBOE", "OTC", "OTCMKTS", "PINK", "US",
}  # fmt: skip
_EXCHANGE_SUFFIXES = {
    "LON": ".L", "LSE": ".L", "ETR": ".DE", "XETRA": ".DE", "FRA": ".F", "EPA": ".PA", "AMS": ".AS", "EBR": ".BR",
    "ELI": ".LS", "BIT": ".MI", "BME": ".MC", "SWX": ".SW", "VTX": ".SW", "ATH": ".AT", "ATHEX": ".AT", "TYO": ".T",
    "HKG": ".HK", "HKEX": ".HK", "TSX": ".TO", "ASX": ".AX", "STO": ".ST", "CPH": ".CO", "HEL": ".HE", "OSL": ".OL",
    "VIE": ".VI", "WSE": ".WA", "NSE": ".NS", "BOM": ".BO", "KRX": ".KS", "TPE": ".TW", "SHA": ".SS", "SHE": ".SZ",
    "BVMF": ".SA", "JSE": ".JO", "SGX": ".SI", "TLV": ".TA", "IST": ".IS", "BMV": ".MX",
}  # fmt: skip
# "VOD LN" / "AAPL US Equity" style (Bloomberg) exchange codes.
_BLOOMBERG_SUFFIXES = {
    "US": "", "UN": "", "UW": "", "UQ": "", "LN": ".L", "GR": ".DE", "GY": ".DE", "FP": ".PA", "NA": ".AS",
    "IM": ".MI", "SM": ".MC", "SW": ".SW", "SE": ".SW", "GA": ".AT", "JP": ".T", "JT": ".T", "HK": ".HK",
    "CN": ".TO", "CT": ".TO", "AU": ".AX", "SS": ".ST", "DC": ".CO", "FH": ".HE", "NO": ".OL", "AV": ".VI",
    "PW": ".WA", "BB": ".BR", "PL": ".LS",
}  # fmt: skip
# Yahoo suffixes that are a single letter, so "XYZ.L" is a London listing, not class L shares.
_ONE_LETTER_SUFFIXES = {"L", "T", "F", "V"}
# Symbols models reach for that are not single company shares: indices and the biggest ETFs.
_NOT_EQUITIES = {
    "SPX", "NDX", "DJI", "DJIA", "RUT", "VIX", "DAX", "FTSE", "CAC", "STOXX", "NIKKEI", "SPY", "QQQ", "DIA", "IWM",
    "VOO", "VTI", "IVV", "GLD", "SLV", "USO", "UNG", "TLT", "HYG", "LQD", "EEM", "EFA", "XLE", "XLF", "XLK", "XLV",
    "XLY", "XLP", "XLI", "XLU", "XLB", "XLRE", "XLC", "SMH", "SOXX", "ARKK", "KRE", "XBI", "IBB", "GDX", "UVXY",
    "SQQQ", "TQQQ", "IBIT", "FBTC", "ETHA", "BITO", "BTC", "ETH",
}  # fmt: skip
_CRYPTO_PAIR = re.compile(r"-(USD|USDT|USDC|EUR|GBP|BTC|ETH)$")
_CLASS_SHARE = re.compile(r"([A-Z]{1,5})[./ ]([A-Z])")
# Reuters instrument codes of US listings (AMZN.O Nasdaq, IBM.N NYSE): the plain symbol on Yahoo.
_US_RIC = re.compile(r"([A-Z]{1,5})\.(O|OQ|N)")
# What models write when they don't know the symbol.
_PLACEHOLDERS = {"N/A", "NONE", "NULL", "UNKNOWN", "PRIVATE", "TBD", "UNLISTED", "NOT LISTED"}
_YAHOO_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9&-]{0,11}(\.[A-Z]{1,3})?")


def normalise_ticker(raw: Any) -> str | None:
    """A Yahoo Finance symbol for what the model wrote, or None when it isn't a usable single-company ticker.

    Strips whitespace, "$" and "NASDAQ:"/"NYSE:" prefixes, uppercases, turns other exchange prefixes ("LON:VOD") and
    Bloomberg codes ("VOD LN") into Yahoo suffixes ("VOD.L"), turns Reuters codes of US listings into plain symbols
    ("AMZN.O" -> "AMZN"), writes US share classes the Yahoo way ("BRK.B" -> "BRK-B"), pads Hong Kong codes ("700.HK"
    -> "0700.HK"), and rejects placeholders ("N/A", "unknown"), indices, ETFs, currencies and crypto pairs.
    """
    if not isinstance(raw, str):
        return None
    text = " ".join(raw.upper().split())
    if text in _PLACEHOLDERS:
        return None
    text = re.sub(r"\s*\(.*\)$", "", text)  # "TSM (NYSE)"
    text = text.removesuffix(" EQUITY").lstrip("$").strip()
    if (ric := _US_RIC.fullmatch(text)) is not None:
        text = ric.group(1)
    suffix: str | None = None  # None: nothing said about the exchange
    if ":" in text:
        prefix, _, text = (part.strip() for part in text.partition(":"))
        text = text.lstrip("$")
        if prefix == "TSE":
            suffix = ".T" if text.isdigit() else ".TO"
        elif prefix in _US_EXCHANGES:
            suffix = ""
        else:
            suffix = _EXCHANGE_SUFFIXES.get(prefix)
    else:
        code, _, exchange = text.rpartition(" ")
        if code and exchange in _BLOOMBERG_SUFFIXES:
            text, suffix = code, _BLOOMBERG_SUFFIXES[exchange]
    # "BRK.B" is class B shares, but "VOD.L" is London, unless the exchange was already said to be a US one.
    match = _CLASS_SHARE.fullmatch(text) if suffix in (None, "") else None
    if match and (match.group(2) not in _ONE_LETTER_SUFFIXES or suffix == ""):
        text = f"{match.group(1)}-{match.group(2)}"
    if suffix and "." not in text:
        text += suffix
    if (match := re.fullmatch(r"(\d{1,4})\.HK", text)) is not None:
        text = f"{int(match.group(1)):04d}.HK"
    if not _YAHOO_SYMBOL.fullmatch(text) or _CRYPTO_PAIR.search(text) or text in _NOT_EQUITIES:
        return None
    return text


# --- coercing the other fields -------------------------------------------------------------------------------------

_RELATION_ALIASES = {"primary": "direct", "company": "direct", "self": "direct"}
_DIRECTION_ALIASES = {
    "bearish": "negative", "down": "negative", "neg": "negative", "bad": "negative",
    "bullish": "positive", "up": "positive", "pos": "positive", "good": "positive",
    "none": "neutral", "no_impact": "neutral", "both": "mixed",
}  # fmt: skip
_EVENT_ALIASES = {
    "m_a": "m&a", "m_and_a": "m&a", "merger": "m&a", "mergers": "m&a", "acquisition": "m&a", "takeover": "m&a",
    "mergers_and_acquisitions": "m&a", "deal": "m&a", "supply": "supply_chain", "supplychain": "supply_chain",
    "supplier": "supply_chain", "regulatory": "regulation", "policy": "regulation", "tariff": "regulation",
    "tariffs": "regulation", "lawsuit": "legal", "litigation": "legal", "results": "earnings",
    "earnings_report": "earnings", "outlook": "guidance", "forecast": "guidance", "rating": "analyst",
    "downgrade": "analyst", "upgrade": "analyst", "analyst_rating": "analyst", "competitive": "competition",
    "leadership": "management", "disaster": "accident", "incident": "accident", "economy": "macro",
    "economic": "macro",
}  # fmt: skip


def _word(value: Any) -> str:
    return re.sub(r"[\s-]+", "_", value.strip().lower()) if isinstance(value, str) else ""


def _relation(value: Any) -> str:
    word = _word(value)
    word = _RELATION_ALIASES.get(word, word)
    return word if word in RELATIONS else "indirect"  # when unsure how the company is linked, assume the weaker link


def _direction(value: Any) -> str | None:
    word = _word(value)
    word = _DIRECTION_ALIASES.get(word, word)
    return word if word in DIRECTIONS else None


def _event_type(value: Any) -> str:
    word = _word(value)
    word = _EVENT_ALIASES.get(word, word)
    return word if word in EVENT_TYPES else "other"


def _magnitude(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return None
    if not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return min(5, max(1, round(value)))


def _clean(value: Any, limit: int) -> str:
    text = " ".join(value.split()) if isinstance(value, str) else ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _to_impact(article_id: str, entry: dict) -> Impact | None:
    ticker = normalise_ticker(entry.get("ticker"))
    direction = _direction(entry.get("direction"))
    magnitude = _magnitude(entry.get("magnitude"))
    if ticker is None or direction is None or magnitude is None:
        log.debug("Dropped an unusable triage entry for article %s: %r", article_id, entry)
        return None
    return Impact(
        article_id=article_id,
        ticker=ticker,
        company=_clean(entry.get("company"), 120) or ticker,
        relation=_relation(entry.get("relation")),
        direction=direction,
        magnitude=magnitude,
        event_type=_event_type(entry.get("event_type")),
        rationale=_clean(entry.get("rationale"), RATIONALE_CHARS),
    )


def _strength(impact: Impact) -> tuple[bool, int]:
    return impact.relation == "direct", impact.magnitude


def _article_impacts(article_id: str, entries: list[dict]) -> list[Impact]:
    """Clean one article's company entries: one impact per ticker (the strongest), at most five."""
    by_ticker: dict[str, Impact] = {}
    for entry in entries:
        impact = _to_impact(article_id, entry)
        if impact is None:
            continue
        current = by_ticker.get(impact.ticker)
        if current is None or _strength(impact) > _strength(current):
            by_ticker[impact.ticker] = impact
    impacts = list(by_ticker.values())
    if len(impacts) > MAX_COMPANIES_PER_ARTICLE:
        keep = {id(impact) for impact in sorted(impacts, key=_strength, reverse=True)[:MAX_COMPANIES_PER_ARTICLE]}
        impacts = [impact for impact in impacts if id(impact) in keep]
    return impacts


# --- the model's reply ---------------------------------------------------------------------------------------------


# Keys some models use instead of "companies"; taken as the same thing.
_COMPANY_KEY_ALIASES = ("affected_companies", "impacts")


def validate_triage(data: Any, ids: list[str]) -> dict[str, list[dict]]:
    """Check the model's triage reply has the expected shape; ValueError says what's wrong.

    Returns the company entries (dicts, not yet cleaned) of the ids the reply covers; ids it left out are not in
    the result (the caller keeps those articles for another try) and ids it made up are ignored. A reply that covers
    fewer than half of the ids (none of one or two) is rejected so complete_json asks again. An entry without
    "companies" counts as "none affected", unless it holds another list: "affected_companies" and "impacts" are
    read as companies, anything else is rejected, since the companies would be lost.
    """
    wanted = set(ids)
    if isinstance(data, dict) and "articles" not in data and wanted & set(data):
        data = [{"id": key, "companies": value} for key, value in data.items()]  # {"a1": [...], "a2": []}
    entries = data.get("articles") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise ValueError('Expected a JSON object like {"articles": [{"id": "a1", "companies": [...]}, ...]}.')
    result: dict[str, list[dict]] = {article_id: [] for article_id in ids}
    covered: set[str] = set()
    unknown: list[str] = []
    for position, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise ValueError(f'Entry {position} of "articles" is not an object with "id" and "companies".')
        entry_id = entry.get("id")
        if isinstance(entry_id, int) and not isinstance(entry_id, bool):
            entry_id = str(entry_id)
        if not isinstance(entry_id, str) or not entry_id.strip():
            raise ValueError(f'Entry {position} of "articles" has no "id".')
        entry_id = entry_id.strip()
        if entry_id not in wanted and f"a{entry_id}" in wanted:  # "1" for "a1"
            entry_id = f"a{entry_id}"
        companies = entry.get("companies")
        if companies is None and "companies" not in entry:
            companies = next((entry[key] for key in _COMPANY_KEY_ALIASES if isinstance(entry.get(key), list)), None)
        if companies is None and "companies" not in entry:
            other = next((key for key, value in entry.items() if isinstance(value, list) and value), None)
            if other is not None:
                raise ValueError(
                    f'Article {entry_id} has no "companies" list (found "{other}"); put the affected companies '
                    'under "companies".'
                )
        if companies is None:
            companies = []
        if not isinstance(companies, list):
            raise ValueError(f'"companies" of article {entry_id} must be a list (use [] for none).')
        if entry_id not in wanted:
            unknown.append(entry_id)
            continue
        covered.add(entry_id)
        result[entry_id].extend(company for company in companies if isinstance(company, dict))
    if unknown:
        log.debug("Ignored triage entries for unknown article ids: %s", ", ".join(unknown))
    if entries and ids and not covered:
        raise ValueError(f"None of the ids in the reply match the articles; use the ids given ({', '.join(ids)}).")
    if ids and len(covered) * 2 < len(ids):
        missing = ", ".join(article_id for article_id in ids if article_id not in covered)
        raise ValueError(
            f"The reply covers only {len(covered)} of the {len(ids)} articles (missing: {missing}). "
            'Include every id, with "companies": [] when no listed company is affected.'
        )
    return {article_id: companies for article_id, companies in result.items() if article_id in covered}


# --- prompting -----------------------------------------------------------------------------------------------------


def _safe(text: str) -> str:
    """Article text can't open or close <article> elements (a prompt-injection guard)."""
    return text.replace("<", "‹").replace(">", "›")


def _summary(article: Article) -> str:
    summary = " ".join(article.summary.split())
    title = " ".join(article.title.split())
    if summary.casefold().startswith(title.casefold()):  # Google News: "<title>  <source>"
        summary = summary[len(title) :].strip(" -–—|:")
        if len(summary) < 40:
            return ""
    if len(summary) > SUMMARY_CHARS:
        summary = summary[:SUMMARY_CHARS].rsplit(" ", 1)[0].rstrip(",;:") + " …"
    return summary


def article_block(short_id: str, article: Article) -> str:
    """One article as the model sees it: <article id=".." source=".." published="..">title\\nsummary</article>."""
    source = _safe(article.source_name or article.source).replace('"', "'")
    published = utc(article.published).strftime("%Y-%m-%d %H:%M UTC")
    title = _safe(" ".join(article.title.split()))
    summary = _safe(_summary(article))
    body = f"{title}\n{summary}" if summary else title
    return f'<article id="{short_id}" source="{source}" published="{published}">{body}</article>'


def triage_batch(model: ChatModel, articles: list[Article], *, now: datetime) -> list[Impact]:
    """The impacts the model finds in one batch of articles (invalid entries dropped, tickers normalised).

    Raises LLMError when the model's reply is unusable even after a corrective retry. Articles the reply left out
    simply give no impacts here; triage() keeps them for another try (see _triage_batch).
    """
    return _triage_batch(model, articles, now=now)[0]


def _triage_batch(model: ChatModel, articles: list[Article], *, now: datetime) -> tuple[list[Impact], set[str]]:
    """(impacts, ids of the articles the reply covered) for one batch."""
    if not articles:
        return [], set()
    ids = [f"a{number}" for number in range(1, len(articles) + 1)]
    today = utc(now)
    prompt = prompts.TRIAGE_PROMPT.format(
        articles="\n\n".join(article_block(short_id, article) for short_id, article in zip(ids, articles, strict=True)),
        count=len(articles),
        today=f"{today:%Y-%m-%d} ({today:%A})",
    )
    companies = complete_json(model, prompts.TRIAGE_SYSTEM, prompt, validate=lambda data: validate_triage(data, ids))
    impacts: list[Impact] = []
    covered: set[str] = set()
    for short_id, article in zip(ids, articles, strict=True):
        if short_id in companies:
            covered.add(article.id)
            impacts.extend(_article_impacts(article.id, companies[short_id]))
    return impacts, covered


# --- the triage step of a scan cycle -------------------------------------------------------------------------------


def triage(
    model: ChatModel,
    store: Store,
    *,
    batch_size: int,
    max_attempts: int,
    now: datetime,
    on_stop: Callable[[str], None] | None = None,
) -> tuple[int, list[Impact]]:
    """Triage every pending article in the store; returns (articles triaged, impacts found).

    Articles go to the model in batches of batch_size, oldest first. When a batch fails (LLMError), it is split in
    halves to find the article(s) that break it, so one bad article can't sink the other nineteen; the ones that
    still fail get a failed attempt recorded (the store gives up on them after max_attempts, and each article is
    tried at most once per cycle). Articles a reply leaves out are not "done": they get a failed attempt too, so
    they are sent again next cycle. When the service is unreachable or keeps throttling (LLMUnavailableError), or
    refuses every request of the cycle the same way (an LLMRequestError for a whole batch before anything worked:
    a spend limit, a setting the model rejects), triage stops for this cycle and the remaining articles stay pending
    without using up attempts. LLMSetupError propagates: every call would fail. Any other exception (a bug, an odd
    reply) counts as a failed batch. on_stop, when given, is called with the reason when triage stops for the cycle
    because the model can't be used (unreachable, throttling, or refusing every request).
    """
    batch_size = max(1, batch_size)
    triaged = 0
    impacts: list[Impact] = []
    tried: set[str] = set()  # every article sent to the model this cycle
    left_pending = 0  # articles that failed this cycle but may still be pending in the store

    def attempt(batch: list[Article]) -> LLMError | None:
        nonlocal triaged, left_pending
        try:
            found, covered = _triage_batch(model, batch, now=now)
        except (LLMUnavailableError, LLMSetupError, ConfigError):
            raise
        except LLMError as exc:
            return exc
        except Exception as exc:  # not the model's usual failures: count it, don't retry the batch forever
            log.warning("Triage of %d article(s) failed unexpectedly", len(batch), exc_info=True)
            return LLMError(f"{type(exc).__name__}: {exc}")
        done = [article.id for article in batch if article.id in covered]
        store.record_triage(done, found)
        triaged += len(done)
        impacts.extend(found)
        missing = [article for article in batch if article.id not in covered]
        if missing:
            left_pending += len(missing)
            store.record_triage_failure([article.id for article in missing], max_attempts=max_attempts)
            log.warning(
                "The triage reply left out %d of %d article(s) (%s); they stay pending for another try.",
                len(missing),
                len(batch),
                "; ".join(article.title for article in missing[:3]) + (" ..." if len(missing) > 3 else ""),
            )
        return None

    try:
        while True:
            pending = store.pending_triage(batch_size + left_pending, max_attempts=max_attempts)
            batch = [article for article in pending if article.id not in tried][:batch_size]
            if not batch:
                break
            tried.update(article.id for article in batch)
            error = attempt(batch)
            if error is None:
                continue
            failed, error = _isolate_failures(attempt, batch, error)
            if isinstance(error, LLMRequestError) and len(failed) == len(batch) and not triaged:
                log.warning(
                    "Stopped triage for this cycle: the model refused every request (%s); the articles stay pending.",
                    error,
                )
                if on_stop is not None:
                    on_stop(f"the triage model refused every request: {error}")
                break
            if failed:
                left_pending += len(failed)
                store.record_triage_failure([article.id for article in failed], max_attempts=max_attempts)
                titles = "; ".join(article.title for article in failed[:3]) + (" ..." if len(failed) > 3 else "")
                log.warning("Triage failed for %d article(s) (%s): %s", len(failed), titles, error)
    except LLMUnavailableError as exc:
        log.warning("Stopped triage for this cycle, the remaining articles stay pending: %s", exc)
        if on_stop is not None:
            on_stop(f"the triage model is unavailable: {exc}")
    if triaged:
        log.info("Triaged %d article(s): %d company impact(s).", triaged, len(impacts))
    return triaged, impacts


def _isolate_failures(
    attempt: Callable[[list[Article]], LLMError | None], batch: list[Article], error: LLMError
) -> tuple[list[Article], LLMError]:
    """Split a failed batch in halves until the failing part is found; returns (articles that failed, last error).

    When both halves fail the problem isn't one article (e.g. the model keeps writing broken JSON), so splitting
    stops there and the whole remainder counts as failed.
    """
    failing = batch
    while len(failing) > 1:
        middle = len(failing) // 2
        failures = [
            (half, half_error)
            for half in (failing[:middle], failing[middle:])
            if (half_error := attempt(half)) is not None
        ]
        if len(failures) != 1:
            return [article for half, _ in failures for article in half], (failures[-1][1] if failures else error)
        failing, error = failures[0]
    return failing, error
