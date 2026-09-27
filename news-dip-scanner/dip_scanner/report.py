"""Reports: the opportunity report (Markdown, HTML, JSON) and the news digest ("newsletter").

Markdown is the main format: it is what people read in a terminal or editor, the plain-text part of the email and what
chat notifiers get. The HTML page is self-contained and email-safe (layout tables, inline styles only, no scripts,
no external images or fonts). JSON is [Opportunity.to_dict(), ...] for other tools.

Every piece of text that came from a feed or a model is escaped for the format it goes into, and only http(s) links
are turned into links (anything else, e.g. javascript:, is shown as plain text).
"""

from __future__ import annotations

import html
import json
import logging
import math
import os
import re
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from .models import Article, Impact, Opportunity, from_iso, utc

log = logging.getLogger(__name__)

DEFAULT_TITLE = "Dip opportunities"
DISCLAIMER = (
    "Not investment advice. A language model wrote this from news headlines and price data, and it can be wrong or "
    "out of date. Its chances and scores are uncalibrated estimates until `dip-scanner track` shows otherwise, and "
    "the potential low is not a floor or a stop. Do your own checks before you buy or sell anything; the tool never "
    "places orders."
)
VERDICT_LABELS = {
    "temporary_fear": "Temporary fear",
    "mixed": "Mixed",
    "fundamental": "Fundamental damage",
    "unclear": "Unclear",
}
# (lowest score, badge colour, label), best first. The same buckets as the track record's score buckets.
SCORE_BANDS = (
    (80.0, "#116329", "strong"),
    (65.0, "#3f7f22", "good"),
    (50.0, "#9a6700", "fair"),
    (-math.inf, "#6e7781", "weak"),
)
DIGEST_OTHER_HEADLINES = 30  # render_news_digest: headlines without a company shown at most

_CURRENCY_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£"}
_PENCE = ("GBp", "GBX")  # London prices quoted in pence
_DIRECTION_SECTIONS = (
    ("negative", "Negative news"),
    ("mixed", "Mixed news"),
    ("positive", "Positive news"),
    ("neutral", "Neutral mentions"),
)
_DIRECTION_RANK = {direction: rank for rank, (direction, _) in enumerate(_DIRECTION_SECTIONS)}
# Backslash-escaped in Markdown text: link brackets, table pipes, emphasis and code markers, and < > so raw HTML
# (from a feed or a prompt-injected model reply) shows as text in any viewer that renders HTML. The backslash itself
# too, so "\<" can't undo the escape. notify.py undoes exactly these for the chat services.
_MD_SPECIAL = re.compile(r"([\\`*_\[\]|<>])")

# HTML palette (light, email-safe).
_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
_INK = "#1f2328"
_MUTED = "#59636e"
_LINE = "#d1d9e0"
_PAGE = "#f6f8fa"
_CARD = "#ffffff"
_LINK = "#0969da"
_WARN_BG = "#fff8c5"
_WARN_LINE = "#d4a72c"


# --- formatting helpers (also used by notify.py and track.py) ---------------------------------------------------------


def format_price(value: float, currency: str | None) -> str:
    """A price with its currency: $142.50, €12.30, £3.45, 245.60p (London pence), 1,234.00 JPY.

    Two decimals with thousands separators; prices below 1 get four decimals so penny stocks stay readable.
    """
    magnitude = abs(value)
    number = f"{magnitude:,.2f}" if magnitude >= 1 else f"{magnitude:.4f}"
    sign = "-" if value < 0 and number.strip("0.,") else ""
    code = (currency or "").strip()
    if code in _PENCE:
        return f"{sign}{number}p"
    symbol = _CURRENCY_SYMBOLS.get(code.upper())
    if symbol:
        return f"{sign}{symbol}{number}"
    return f"{sign}{number} {code}" if code else f"{sign}{number}"


def format_pct(value: float) -> str:
    """A signed percentage with one decimal: +17.9%, -5.0%, +0.0% (never -0.0%)."""
    return f"{round(value, 1) + 0.0:+.1f}%"


def format_when(dt: datetime) -> str:
    """A timestamp as 2026-09-25 15:00 UTC."""
    return f"{utc(dt):%Y-%m-%d %H:%M} UTC"


def relative_to(value: float, base: float) -> str:
    """Where value sits relative to base in words: "17.2% below", "3.0% above" or "at the price"."""
    change = round((value / base - 1) * 100, 1) if base else 0.0
    if change < 0:
        return f"{-change:.1f}% below"
    if change > 0:
        return f"{change:.1f}% above"
    return "at the price"


def score_band(score: float) -> tuple[str, str]:
    """(colour, label) of a score: 80+ strong, 65-80 good, 50-65 fair, below 50 weak."""
    for lowest, colour, label in SCORE_BANDS:
        if score >= lowest:
            return colour, label
    return SCORE_BANDS[-1][1], SCORE_BANDS[-1][2]  # NaN


def score_color(score: float) -> str:
    """The badge colour for a score (see SCORE_BANDS)."""
    return score_band(score)[0]


def verdict_label(verdict: str) -> str:
    """A verdict for people: temporary_fear -> "Temporary fear"."""
    return VERDICT_LABELS.get(verdict, verdict.replace("_", " ").capitalize())


def safe_url(url: object) -> str | None:
    """The URL if it is an absolute http(s) link without whitespace or control characters, else None."""
    if not isinstance(url, str):
        return None
    url = url.strip()
    if not url or any(ord(char) <= 32 or ord(char) == 127 for char in url):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return None
    return url


def md_escape(text: object) -> str:
    """Text for a Markdown line or table cell: whitespace collapsed; \\ ` * _ [ ] | < > backslash-escaped."""
    return _MD_SPECIAL.sub(r"\\\1", " ".join(str(text).split()))


def md_link(text: object, url: object) -> str:
    """A Markdown link, or just the escaped text when the URL isn't a safe http(s) link."""
    label = md_escape(text) or "link"
    link = safe_url(url)
    if not link:
        return label
    return f"[{label}](<{link.replace('<', '%3C').replace('>', '%3E')}>)"


# --- opportunity report: Markdown -------------------------------------------------------------------------------------


def render_markdown(opps: list[Opportunity], *, title: str, generated: datetime, notes: Sequence[str] = ()) -> str:
    """The opportunity report as Markdown, best score first, ending with the not-investment-advice disclaimer.

    An overview table comes first, then one section per opportunity: the key figures (price and recent moves, the
    6-month probability, potential low, statistical low, the limit-buy entry and limit-sell target, upside and
    downside, verdict and confidence), the fear / fundamental impact / thesis, risks, catalysts, what to check
    before buying, the headlines that flagged it and any numbers the sanitizer had to fix. An opportunity with a newer
    analysis of the same ticker in the list is marked superseded, with what the newer one says.
    """
    ranked = _ranked(opps)
    newer = superseded_by(ranked)
    lines = [f"# {md_escape(title)}", "", f"_{_count_text(len(ranked))} · generated {format_when(generated)}_"]
    if ranked:
        lines += [
            "",
            "| # | Ticker | Company | Score | Chance up in 6m | Price | Entry (limit buy) | Target | Verdict |",
            "|---:|---|---|---:|---:|---:|---:|---:|---|",
        ]
        for number, opp in enumerate(ranked, start=1):
            analysis = opp.analysis
            cells = [
                str(number),
                f"**{md_escape(opp.ticker)}**",
                md_escape(opp.company),
                f"{opp.score:.1f}",
                f"{analysis.probability_up_6m}%",
                format_price(opp.price, opp.currency),
                format_price(analysis.entry_price, opp.currency),
                f"{format_price(analysis.target_price, opp.currency)} ({format_pct(opp.upside_pct())})",
                verdict_label(analysis.verdict) + (" (superseded)" if number - 1 in newer else ""),
            ]
            lines.append("| " + " | ".join(cells) + " |")
        for index, opp in enumerate(ranked):
            lines += ["", *_opportunity_markdown(opp, newer.get(index))]
    else:
        lines += ["", "No opportunities this time."]
    if notes:
        lines += ["", "## Notes", "", *(f"- {md_escape(note)}" for note in notes)]
    lines += ["", "---", "", f"_{DISCLAIMER}_"]
    return "\n".join(lines) + "\n"


def _opportunity_markdown(opp: Opportunity, newer: Opportunity | None = None) -> list[str]:
    analysis = opp.analysis
    tagline = [verdict_label(analysis.verdict), f"{analysis.confidence} confidence", *opp.dip_reasons]
    lines = [
        f"## {md_escape(opp.ticker)} — {md_escape(opp.company)} · score {opp.score:.1f}",
        "",
        "_" + " · ".join(md_escape(part) for part in tagline) + "_",
        "",
    ]
    if newer is not None:
        lines += [f"**{md_escape(superseded_text(newer))}**", ""]
    lines += [
        "| Key figures | |",
        "|---|---|",
        *(f"| {label} | {md_escape(value)} |" for label, value in key_figures(opp)),
    ]
    for label, text in _paragraphs(opp):
        lines += ["", f"**{label}:** {md_escape(text)}"]
    for label, items in _lists(opp):
        lines += ["", f"**{label}**", "", *(f"- {md_escape(item)}" for item in items)]
    if opp.headlines:
        lines += ["", "**Headlines**", "", *(f"- {_headline_markdown(headline)}" for headline in opp.headlines)]
    if analysis.warnings:
        lines += ["", "**Numbers fixed after the analysis**", "", *(f"- {md_escape(w)}" for w in analysis.warnings)]
    lines += [
        "",
        f"_Analysis by {md_escape(opp.model or 'unknown model')}; prices as of {format_when(opp.stats.as_of)}._",
    ]
    return lines


def key_figures(opp: Opportunity) -> list[tuple[str, str]]:
    """The (label, value) rows of an opportunity's key-figures table, shared by the Markdown and HTML reports."""
    analysis, stats, currency, price = opp.analysis, opp.stats, opp.currency, opp.price

    def level(value: float) -> str:
        return f"{format_price(value, currency)} ({relative_to(value, price)})"

    moves = f"{format_pct(stats.change_1d_pct)} 1 day, {format_pct(stats.change_5d_pct)} 5 days"
    return [
        ("Reported", format_when(opp.created)),
        ("Price", f"{format_price(price, currency)} ({moves})"),
        ("From 52-week high", format_pct(stats.drawdown_52w_pct)),
        ("Chance of being higher in 6 months", f"{analysis.probability_up_6m}%"),
        ("Potential low", level(analysis.potential_low)),
        ("Statistical 6-month low", level(stats.stat_low_6m)),
        ("Entry (limit buy)", level(analysis.entry_price)),
        ("Target (limit sell idea)", level(analysis.target_price)),
        ("Upside / downside", f"{format_pct(opp.upside_pct())} / {format_pct(opp.downside_pct())}"),
        ("Verdict", verdict_label(analysis.verdict)),
        ("Confidence", analysis.confidence.capitalize()),
    ]


def _paragraphs(opp: Opportunity) -> list[tuple[str, str]]:
    analysis = opp.analysis
    pairs = [
        ("What the market fears", analysis.fear),
        ("Fundamental impact", analysis.fundamental_impact),
        ("Thesis", analysis.thesis),
    ]
    return [(label, text) for label, text in pairs if text and text.strip()]


def _lists(opp: Opportunity) -> list[tuple[str, list[str]]]:
    analysis = opp.analysis
    pairs = [("Risks", analysis.risks), ("Catalysts", analysis.catalysts), ("Check before buying", analysis.checks)]
    return [(label, items) for label, items in pairs if items]


def _headline_markdown(headline: dict) -> str:
    text = md_link(headline.get("title") or "Untitled", headline.get("link"))
    details = _headline_details(headline)
    return f"{text} · {md_escape(details)}" if details else text


def _headline_details(headline: dict) -> str:
    """ "MarketWatch, Sep 25, 14:00 UTC · negative 4/5" from a headline dict (missing parts are left out)."""
    where = [str(headline.get("source") or "").strip()]
    published = _parse_time(headline.get("published"))
    if published is not None:
        where.append(_short_time(published))
    parts = [", ".join(part for part in where if part)]
    direction = str(headline.get("direction") or "").strip()
    magnitude = headline.get("magnitude")
    if direction:
        parts.append(f"{direction} {magnitude}/5" if isinstance(magnitude, int) else direction)
    return " · ".join(part for part in parts if part)


# --- opportunity report: HTML -----------------------------------------------------------------------------------------


def render_html(opps: list[Opportunity], *, title: str, generated: datetime, notes: Sequence[str] = ()) -> str:
    """The opportunity report as a self-contained, email-safe HTML page (same content as the Markdown).

    Layout tables and inline styles only (email clients drop <style> blocks and scripts), all text escaped, links
    only for http(s) URLs. Scores get a coloured badge: 80+ strong, 65-80 good, 50-65 fair, below 50 weak.
    """
    ranked = _ranked(opps)
    newer = superseded_by(ranked)
    rows = [_html_header(title, generated, len(ranked))]
    if ranked:
        rows.append(_html_overview(ranked, newer))
        rows += [_html_card(opp, newer.get(index)) for index, opp in enumerate(ranked)]
    else:
        rows.append(_row(f'<p style="margin:0;padding:16px 0;">{_e("No opportunities this time.")}</p>'))
    if notes:
        items = "".join(f'<li style="margin:0 0 4px 0;">{_e(note)}</li>' for note in notes)
        rows.append(_row(_section_title("Notes") + f'<ul style="margin:0 0 16px 0;padding-left:20px;">{items}</ul>'))
    rows.append(
        _row(
            f'<p style="margin:8px 0 0 0;padding-top:12px;border-top:1px solid {_LINE};font-size:12px;'
            f'color:{_MUTED};">{_e(DISCLAIMER)}</p>'
        )
    )
    return "\n".join(
        [
            "<!DOCTYPE html>",
            '<html lang="en">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            '<meta name="color-scheme" content="light">',
            f"<title>{_e(title)}</title>",
            "</head>",
            f'<body style="margin:0;padding:0;background:{_PAGE};">',
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'style="background:{_PAGE};">',
            '<tr><td align="center" style="padding:24px 12px;">',
            f'<table role="presentation" width="680" cellpadding="0" cellspacing="0" border="0" '
            f'style="width:100%;max-width:680px;font-family:{_FONT};font-size:15px;line-height:1.5;color:{_INK};">',
            *rows,
            "</table>",
            "</td></tr>",
            "</table>",
            "</body>",
            "</html>",
        ]
    )


def _e(text: object) -> str:
    return html.escape(str(text), quote=True)


def _row(content: str) -> str:
    return f"<tr><td>{content}</td></tr>"


def _section_title(text: str) -> str:
    return f'<p style="margin:16px 0 6px 0;font-size:13px;font-weight:700;color:{_INK};">{_e(text)}</p>'


def _html_link(text: object, url: object) -> str:
    link = safe_url(url)
    if not link:
        return _e(text)
    return f'<a href="{_e(link)}" style="color:{_LINK};text-decoration:none;">{_e(text)}</a>'


def _badge(score: float) -> str:
    colour, _ = score_band(score)
    return (
        f'<span style="display:inline-block;background:{colour};color:#ffffff;font-weight:700;'
        f'padding:2px 8px;border-radius:4px;">{score:.1f}</span>'
    )


def _html_header(title: str, generated: datetime, count: int) -> str:
    return _row(
        f'<h1 style="margin:0 0 4px 0;font-size:24px;line-height:1.3;color:{_INK};">{_e(title)}</h1>'
        f'<p style="margin:0 0 16px 0;font-size:13px;color:{_MUTED};">'
        f"{_e(_count_text(count))} · generated {_e(format_when(generated))}</p>"
    )


def _html_overview(ranked: list[Opportunity], newer: dict[int, Opportunity]) -> str:
    cell = f"padding:6px 8px;border-bottom:1px solid {_LINE};"
    head = f"{cell}font-size:12px;color:{_MUTED};font-weight:600;"
    headers = [
        ("Ticker", "left"),
        ("Score", "right"),
        ("Up in 6m", "right"),
        ("Price", "right"),
        ("Entry", "right"),
        ("Target", "right"),
        ("Verdict", "left"),
    ]
    lines = [
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="background:{_CARD};border:1px solid {_LINE};border-radius:8px;margin:0 0 16px 0;font-size:14px;">',
        "<tr>" + "".join(f'<th align="{align}" style="{head}">{name}</th>' for name, align in headers) + "</tr>",
    ]
    for index, opp in enumerate(ranked):
        analysis = opp.analysis
        company = f'<span style="font-size:12px;color:{_MUTED};">{_e(opp.company)}</span>'
        name = f"<strong>{_e(opp.ticker)}</strong><br>{company}"
        values = [
            (name, "left"),
            (_badge(opp.score), "right"),
            (f"{analysis.probability_up_6m}%", "right"),
            (_e(format_price(opp.price, opp.currency)), "right"),
            (_e(format_price(analysis.entry_price, opp.currency)), "right"),
            (_e(format_price(analysis.target_price, opp.currency)), "right"),
            (_e(verdict_label(analysis.verdict) + (" (superseded)" if index in newer else "")), "left"),
        ]
        lines.append(
            "<tr>"
            + "".join(f'<td align="{align}" style="{cell}vertical-align:top;">{value}</td>' for value, align in values)
            + "</tr>"
        )
    lines.append("</table>")
    return _row("".join(lines))


def _html_card(opp: Opportunity, newer: Opportunity | None = None) -> str:
    analysis = opp.analysis
    colour, band = score_band(opp.score)
    tagline = " · ".join([verdict_label(analysis.verdict), f"{analysis.confidence} confidence", *opp.dip_reasons])
    header = (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        '<td style="vertical-align:top;">'
        f'<p style="margin:0;font-size:20px;font-weight:700;line-height:1.3;">{_e(opp.ticker)} '
        f'<span style="font-weight:400;color:{_MUTED};">— {_e(opp.company)}</span></p>'
        f'<p style="margin:2px 0 0 0;font-size:13px;color:{_MUTED};">{_e(tagline)}</p>'
        "</td>"
        '<td align="right" style="vertical-align:top;white-space:nowrap;padding-left:12px;">'
        f'<span style="display:inline-block;background:{colour};color:#ffffff;font-size:18px;font-weight:700;'
        f'padding:4px 10px;border-radius:6px;">{opp.score:.1f}</span>'
        f'<p style="margin:2px 0 0 0;font-size:11px;color:{_MUTED};">score · {_e(band)}</p>'
        "</td></tr></table>"
    )
    figure_rows = "".join(
        f'<tr><td style="padding:5px 12px 5px 0;border-bottom:1px solid {_LINE};color:{_MUTED};font-size:13px;'
        f'white-space:nowrap;vertical-align:top;">{_e(label)}</td>'
        f'<td style="padding:5px 0;border-bottom:1px solid {_LINE};font-size:14px;">{_e(value)}</td></tr>'
        for label, value in key_figures(opp)
    )
    figures = (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="margin:12px 0 4px 0;">{figure_rows}</table>'
    )
    body = [header]
    if newer is not None:
        body.append(
            f'<p style="margin:12px 0 0 0;padding:8px 12px;background:{_WARN_BG};border:1px solid {_WARN_LINE};'
            f'border-radius:6px;font-size:13px;"><strong>{_e(superseded_text(newer))}</strong></p>'
        )
    body.append(figures)
    for label, text in _paragraphs(opp):
        body.append(f'<p style="margin:12px 0 0 0;"><strong>{_e(label)}:</strong> {_e(text)}</p>')
    for label, items in _lists(opp):
        body.append(_section_title(label) + _html_list(_e(item) for item in items))
    if opp.headlines:
        body.append(_section_title("Headlines") + _html_list(_html_headline(headline) for headline in opp.headlines))
    if analysis.warnings:
        body.append(
            f'<div style="margin:12px 0 0 0;padding:8px 12px;background:{_WARN_BG};border:1px solid {_WARN_LINE};'
            f'border-radius:6px;font-size:13px;"><strong>Numbers fixed after the analysis</strong>'
            + _html_list(_e(warning) for warning in analysis.warnings)
            + "</div>"
        )
    body.append(
        f'<p style="margin:12px 0 0 0;font-size:12px;color:{_MUTED};">Analysis by {_e(opp.model or "unknown model")}; '
        f"prices as of {_e(format_when(opp.stats.as_of))}.</p>"
    )
    return _row(
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="background:{_CARD};border:1px solid {_LINE};border-top:4px solid {colour};border-radius:8px;'
        f'margin:0 0 16px 0;"><tr><td style="padding:16px 20px;">' + "".join(body) + "</td></tr></table>"
    )


def _html_list(items) -> str:
    rendered = "".join(f'<li style="margin:0 0 4px 0;">{item}</li>' for item in items)
    return f'<ul style="margin:4px 0 0 0;padding-left:20px;">{rendered}</ul>'


def _html_headline(headline: dict) -> str:
    link = _html_link(headline.get("title") or "Untitled", headline.get("link"))
    details = _headline_details(headline)
    return link + (f' <span style="font-size:12px;color:{_MUTED};">· {_e(details)}</span>' if details else "")


# --- news digest ------------------------------------------------------------------------------------------------------


def render_news_digest(
    news: list[tuple[Article, list[Impact]]],
    *,
    hours: float,
    generated: datetime,
    max_other: int = DIGEST_OTHER_HEADLINES,
) -> str:
    """The news digest ("newsletter") as Markdown.

    Companies are grouped by their most worrying news: first those with negative news, then mixed, positive and
    neutral. Within a section, the biggest expected impact (magnitude) comes first. Each company lists its articles
    newest first with direction, magnitude, relation, event type, a link and the triage's one-line rationale. After
    that come up to max_other headlines that didn't map to any listed company (0 leaves them out), saying how many
    more there were.
    """
    groups: dict[str, list[tuple[Impact, Article]]] = {}
    unmatched: list[Article] = []
    for article, impacts in news:
        if not impacts:
            unmatched.append(article)
        for impact in impacts:
            groups.setdefault(impact.ticker, []).append((impact, article))

    sources = {article.source_name or article.source for article, _ in news}
    summary = [
        f"Generated {format_when(generated)}",
        _plural(len(news), "article"),
        _plural(len(sources), "source"),
        f"{_plural(len(groups), 'company', 'companies')} flagged",
    ]
    lines = [f"# News digest: last {_hours_text(hours)}", "", "_" + " · ".join(summary) + "_"]
    if not news:
        lines += ["", f"No articles in the last {_hours_text(hours)}."]
        return "\n".join(lines) + "\n"

    sections = [*_DIRECTION_SECTIONS, ("other", "Other news")]  # "other": a direction the triage shouldn't give
    by_section: list[list[str]] = [[] for _ in sections]
    for ticker in sorted(groups, key=lambda ticker: _ticker_rank(ticker, groups[ticker])):
        by_section[_ticker_rank(ticker, groups[ticker])[0]].append(ticker)
    for (_, section_title), tickers in zip(sections, by_section, strict=True):
        if tickers:
            lines += ["", f"## {section_title}"]
            for ticker in tickers:
                lines += ["", *_digest_company(ticker, groups[ticker])]

    if unmatched and max_other > 0:
        unmatched.sort(key=lambda article: utc(article.published), reverse=True)
        shown = unmatched[:max_other]
        lines += ["", "## Other headlines", ""]
        lines += [
            f"- {md_link(article.title, article.link)} · {md_escape(_article_where(article))}" for article in shown
        ]
        omitted = len(unmatched) - len(shown)
        if omitted:
            lines += ["", f"_…and {_plural(omitted, 'more headline')} without a listed company._"]
    lines += ["", "---", "", "_Companies were matched to the news by a language model and can be wrong._"]
    return "\n".join(lines) + "\n"


def _ticker_rank(ticker: str, items: list[tuple[Impact, Article]]) -> tuple:
    """Sort key: most worrying direction, then biggest magnitude in it, then most articles, newest, ticker."""
    lead = min(_DIRECTION_RANK.get(impact.direction, len(_DIRECTION_RANK)) for impact, _ in items)
    in_lead = [impact.magnitude for impact, _ in items if _DIRECTION_RANK.get(impact.direction) == lead] or [0]
    newest = max(utc(article.published) for _, article in items)
    return (lead, -max(in_lead), -len(items), -newest.timestamp(), ticker)


def _digest_company(ticker: str, items: list[tuple[Impact, Article]]) -> list[str]:
    items = sorted(items, key=lambda item: utc(item[1].published), reverse=True)
    company = next((impact.company for impact, _ in items if impact.company), "")
    counts: dict[str, int] = {}
    for impact, _ in items:
        counts[impact.direction] = counts.get(impact.direction, 0) + 1
    tally = ", ".join(f"{count} {direction}" for direction, count in counts.items())
    heading = f"### {md_escape(ticker)} — {md_escape(company)}" if company else f"### {md_escape(ticker)}"
    lines = [heading, "", f"_{md_escape(tally)}_", ""]
    for impact, article in items:
        tags = " · ".join(
            part for part in (impact.relation, impact.event_type.replace("_", " ") if impact.event_type else "") if part
        )
        lines.append(
            f"- **{md_escape(impact.direction)} {impact.magnitude}/5** · {md_escape(tags)} · "
            f"{md_link(article.title, article.link)} · {md_escape(_article_where(article))}"
        )
        if impact.rationale:
            lines.append(f"  _{md_escape(impact.rationale)}_")
    return lines


def _article_where(article: Article) -> str:
    return f"{article.source_name or article.source}, {_short_time(article.published)}"


# --- files ------------------------------------------------------------------------------------------------------------


def write_reports(
    opps: list[Opportunity],
    data_dir: Path,
    *,
    generated: datetime,
    notes: Sequence[str] = (),
    title: str = DEFAULT_TITLE,
) -> list[Path]:
    """Write the report and return the paths written, in this order:

    data_dir/reports/YYYY-MM-DD/HHMMSS-opportunities.md, .html and .json (UTC date and time of generated), then
    data_dir/reports/latest.md and latest.html (overwritten every time). Folders are created as needed; each file is
    written to a temporary name first and then renamed, so a reader never sees half a file.
    """
    generated = utc(generated)
    reports = Path(data_dir) / "reports"
    folder = reports / f"{generated:%Y-%m-%d}"
    folder.mkdir(parents=True, exist_ok=True)
    markdown = render_markdown(opps, title=title, generated=generated, notes=notes)
    page = render_html(opps, title=title, generated=generated, notes=notes)
    data = json.dumps([opp.to_dict() for opp in _ranked(opps)], indent=2, ensure_ascii=False) + "\n"
    stem = f"{generated:%H%M%S}-opportunities"
    outputs = [
        (folder / f"{stem}.md", markdown),
        (folder / f"{stem}.html", page),
        (folder / f"{stem}.json", data),
        (reports / "latest.md", markdown),
        (reports / "latest.html", page),
    ]
    for path, text in outputs:
        _write_text(path, text)
    log.info("Wrote the report for %s to %s", _count_text(len(opps)), folder)
    return [path for path, _ in outputs]


def _write_text(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


# --- small helpers ----------------------------------------------------------------------------------------------------


def _ranked(opps: Sequence[Opportunity]) -> list[Opportunity]:
    return sorted(opps, key=lambda opp: opp.score, reverse=True)


def superseded_by(opps: Sequence[Opportunity]) -> dict[int, Opportunity]:
    """{position in opps: the newest analysis of the same ticker in opps} for every opportunity that has a newer one."""
    newest: dict[str, Opportunity] = {}
    for opp in opps:
        current = newest.get(opp.ticker)
        if current is None or utc(opp.created) > utc(current.created):
            newest[opp.ticker] = opp
    return {
        index: newest[opp.ticker]
        for index, opp in enumerate(opps)
        if utc(newest[opp.ticker].created) > utc(opp.created)
    }


def superseded_text(newer: Opportunity) -> str:
    """ "Superseded: analysed again on 2026-09-27 16:30 UTC: Fundamental damage, 25% chance up in 6m, score 8.8." """
    return (
        f"Superseded: analysed again on {format_when(newer.created)}: {verdict_label(newer.analysis.verdict)}, "
        f"{newer.analysis.probability_up_6m}% chance up in 6m, score {newer.score:.1f}."
    )


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else plural or singular + 's'}"


def _count_text(count: int) -> str:
    return _plural(count, "opportunity", "opportunities")


def _hours_text(hours: float) -> str:
    return "hour" if hours == 1 else f"{hours:g} hours"


def _short_time(dt: datetime) -> str:
    return f"{utc(dt):%b %d, %H:%M} UTC"


def _parse_time(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return utc(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return from_iso(value.strip())
    except ValueError:
        return None
