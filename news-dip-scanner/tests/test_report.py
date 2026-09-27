from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser

import pytest
from conftest import NOW, make_analysis, make_article, make_impact, make_opportunity, make_stats

from dip_scanner.models import Opportunity
from dip_scanner.report import (
    DISCLAIMER,
    format_pct,
    format_price,
    md_escape,
    md_link,
    relative_to,
    render_html,
    render_markdown,
    render_news_digest,
    safe_url,
    score_band,
    score_color,
    verdict_label,
    write_reports,
)

AMD_LINK = "https://www.example.com/news/amd-shares-slide-after-weak-data-center-guidance"


# --- formatting helpers --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "currency", "expected"),
    [
        (142.5, "USD", "$142.50"),
        (1234.5, "EUR", "€1,234.50"),
        (3.456, "GBP", "£3.46"),
        (245.6, "GBp", "245.60p"),  # London prices in pence
        (1234, "JPY", "1,234.00 JPY"),
        (12.3, "", "12.30"),
        (0.5, "USD", "$0.5000"),
        (-3.5, "USD", "-$3.50"),
        (-0.00001, "USD", "$0.0000"),
    ],
)
def test_format_price(value, currency, expected):
    assert format_price(value, currency) == expected


def test_format_pct_is_signed_with_one_decimal():
    assert format_pct(17.94) == "+17.9%"
    assert format_pct(-5.0) == "-5.0%"
    assert format_pct(-0.04) == "+0.0%"
    assert format_pct(0) == "+0.0%"


def test_relative_to():
    assert relative_to(118, 142.5) == "17.2% below"
    assert relative_to(168, 142.5) == "17.9% above"
    assert relative_to(142.5, 142.5) == "at the price"


def test_score_bands():
    assert score_band(80) == ("#116329", "strong")
    assert score_band(79.9)[1] == "good"
    assert score_band(65)[1] == "good"
    assert score_band(64.9)[1] == "fair"
    assert score_band(50)[1] == "fair"
    assert score_band(49.9)[1] == "weak"
    assert score_color(0) == score_band(10)[0]
    assert len({score_color(score) for score in (90, 70, 55, 30)}) == 4


def test_verdict_label():
    assert verdict_label("temporary_fear") == "Temporary fear"
    assert verdict_label("fundamental") == "Fundamental damage"
    assert verdict_label("something_else") == "Something else"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com/a?b=1", "https://example.com/a?b=1"),
        ("  HTTP://Example.com/  ", "HTTP://Example.com/"),
        ("javascript:alert(1)", None),
        ("JavaScript:alert(1)", None),
        ("data:text/html,<b>x</b>", None),
        ("ftp://example.com/file", None),
        ("//example.com/no-scheme", None),
        ("https://exa mple.com/", None),
        ("https://example.com/\nX-Injected: 1", None),
        ("", None),
        (None, None),
        (42, None),
    ],
)
def test_safe_url(url, expected):
    assert safe_url(url) == expected


def test_markdown_escaping_and_links():
    assert md_escape("Foo | Bar [Class A]\n  next") == "Foo \\| Bar \\[Class A\\] next"
    assert md_link("A [b]", "https://e.com/x y") == "A \\[b\\]"  # whitespace in the URL: not a link
    assert md_link("A", "https://e.com/x>y") == "[A](<https://e.com/x%3Ey>)"
    assert md_link("A", "javascript:alert(1)") == "A"
    assert md_link("", "https://e.com") == "[link](<https://e.com>)"


# --- Markdown report -----------------------------------------------------------------------------------------------


def test_render_markdown_reads_like_a_newsletter():
    opp = make_opportunity()
    text = render_markdown([opp], title="Dip opportunities", generated=NOW)
    stat_low = relative_to(opp.stats.stat_low_6m, opp.price)

    assert text.startswith("# Dip opportunities\n\n_1 opportunity · generated 2026-09-25 15:00 UTC_\n")
    assert "| 1 | **AMD** | Advanced Micro Devices | 72.4 | 68% | $142.50 | $132.00 | $168.00 (+17.9%) |" in text
    assert "## AMD — Advanced Micro Devices · score 72.4" in text
    assert "_Temporary fear · medium confidence · down 5.0% today · 13.6% below its 20-day high_" in text
    for row in [
        "| Price | $142.50 (-5.0% 1 day, -8.0% 5 days) |",
        "| From 52-week high | -25.0% |",
        "| Chance of being higher in 6 months | 68% |",
        "| Potential low | $118.00 (17.2% below) |",
        f"| Statistical 6-month low | {format_price(opp.stats.stat_low_6m, 'USD')} ({stat_low}) |",
        "| Entry (limit buy) | $132.00 (7.4% below) |",
        "| Target (limit sell idea) | $168.00 (17.9% above) |",
        "| Upside / downside | +17.9% / -17.2% |",
        "| Verdict | Temporary fear |",
        "| Confidence | Medium |",
    ]:
        assert row in text
    assert "**What the market fears:** Investors fear a slowdown in AI data-center spending." in text
    assert "**Fundamental impact:** One quarter of softer guidance" in text
    assert "**Thesis:** The drop prices in a lasting slowdown" in text
    assert "**Risks**\n\n- Hyperscalers cut capex further" in text
    assert "**Catalysts**\n\n- Next quarter's earnings" in text
    assert "**Check before buying**\n\n- Read the earnings call transcript" in text
    assert (
        f"- [AMD shares slide after weak data-center guidance](<{AMD_LINK}>) · MarketWatch, Sep 25, 14:00 UTC · "
        "negative 4/5"
    ) in text
    assert "_Analysis by fake-model; prices as of 2026-09-25 14:45 UTC._" in text
    assert "Numbers fixed" not in text
    assert text.rstrip().endswith(f"_{DISCLAIMER}_")
    assert "Not investment advice" in DISCLAIMER and "the tool never places orders" in DISCLAIMER


def test_render_markdown_ranks_by_score_and_shows_warnings_and_notes():
    low = make_opportunity(ticker="LOW", score=55.0)
    high = make_opportunity(
        ticker="HIGH",
        score=88.0,
        currency="EUR",
        analysis=make_analysis(warnings=["entry_price 150.00 was above the price; used 142.50."]),
    )
    text = render_markdown([low, high], title="Report", generated=NOW, notes=["NVDA: cooldown", "2 more [capped]"])
    assert text.index("## HIGH") < text.index("## LOW")
    assert text.index("| 1 | **HIGH**") < text.index("| 2 | **LOW**")
    assert "| 1 | **HIGH** | Advanced Micro Devices | 88.0 | 68% | €142.50 | €132.00 | €168.00 (+17.9%) |" in text
    assert "**Numbers fixed after the analysis**\n\n- entry_price 150.00 was above the price; used 142.50." in text
    assert "## Notes\n\n- NVDA: cooldown\n- 2 more \\[capped\\]" in text


def test_render_markdown_without_opportunities():
    text = render_markdown([], title="Report", generated=NOW, notes=["3 feeds failed"])
    assert "_0 opportunities · generated 2026-09-25 15:00 UTC_" in text
    assert "No opportunities this time." in text
    assert "- 3 feeds failed" in text
    assert DISCLAIMER in text


def test_render_markdown_escapes_model_and_feed_text():
    opp = make_opportunity(
        company="Foo | Bar [Class A]",
        analysis=make_analysis(thesis="Line one\n\n# not a heading | nor a cell"),
        headlines=[
            {"title": "Click [me]", "link": "javascript:alert(1)", "source": "Evil", "published": "not a date"},
            {"title": "No link at all"},
        ],
    )
    text = render_markdown([opp], title="Report", generated=NOW)
    assert "## AMD — Foo \\| Bar \\[Class A\\] · score 72.4" in text
    assert "**Thesis:** Line one # not a heading \\| nor a cell" in text
    assert "javascript:" not in text
    assert "- Click \\[me\\] · Evil" in text
    assert "- No link at all\n" in text


# --- HTML report ---------------------------------------------------------------------------------------------------


class _Collector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.opened: dict[str, int] = {}
        self.closed: dict[str, int] = {}
        self.hrefs: list[str] = []
        self.attributes: set[str] = set()
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.opened[tag] = self.opened.get(tag, 0) + 1
        self.hrefs += [value for name, value in attrs if name in ("href", "src")]
        self.attributes.update(name for name, _ in attrs)

    def handle_endtag(self, tag):
        self.closed[tag] = self.closed.get(tag, 0) + 1

    def handle_data(self, data):
        self.text.append(data)


def _parse(page: str) -> _Collector:
    collector = _Collector()
    collector.feed(page)
    collector.close()
    return collector


def test_render_html_is_self_contained_and_email_safe():
    opps = [make_opportunity(score=85.0), make_opportunity(ticker="SAP.DE", company="SAP SE", score=55.0)]
    page = render_html(opps, title="Dip opportunities", generated=NOW, notes=["One note"])
    parsed = _parse(page)

    assert page.startswith("<!DOCTYPE html>")
    assert "script" not in parsed.opened and "style" not in parsed.opened and "link" not in parsed.opened
    assert "img" not in parsed.opened
    for tag in ("html", "body", "table", "tr", "td", "p", "ul", "li", "span", "a", "strong", "h1", "th", "div"):
        assert parsed.opened.get(tag, 0) == parsed.closed.get(tag, 0), tag
    assert parsed.hrefs == [AMD_LINK, AMD_LINK]
    text = " ".join(parsed.text)
    for expected in ("Dip opportunities", "AMD", "SAP SE", "$142.50", "Potential low", "One note", "Temporary fear"):
        assert expected in text
    assert DISCLAIMER in text
    assert score_color(85.0) in page and score_color(55.0) in page
    assert score_color(70.0) not in page and score_color(30.0) not in page
    assert page.index("SAP SE") > page.index("Advanced Micro Devices")  # ranked by score


def test_render_html_escapes_everything_and_drops_unsafe_links():
    opp = make_opportunity(
        company='Evil "Corp" <b>',
        analysis=make_analysis(thesis="<script>alert(1)</script>", risks=["<img src=x onerror=alert(1)>"]),
        headlines=[
            {"title": "<i>Bad</i>", "link": "javascript:alert(1)", "source": "S"},
            {"title": 'Quote" onmouseover="x', "link": 'https://e.com/?q="x"&a=<b>', "source": "S"},
        ],
    )
    page = render_html([opp], title="<Title & more>", generated=NOW, notes=["<u>note</u>"])
    parsed = _parse(page)
    assert "script" not in parsed.opened and "img" not in parsed.opened and "i" not in parsed.opened
    assert "u" not in parsed.opened and "b" not in parsed.opened
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<title>&lt;Title &amp; more&gt;</title>" in page
    assert "Evil &quot;Corp&quot; &lt;b&gt;" in page
    assert "javascript:" not in page
    assert parsed.hrefs == ['https://e.com/?q="x"&a=<b>']  # attribute-escaped in the source, intact once parsed
    assert not {name for name in parsed.attributes if name.startswith("on")}
    assert "Quote&quot; onmouseover=&quot;x" in page


def test_render_html_without_opportunities():
    page = render_html([], title="Report", generated=NOW)
    assert "No opportunities this time." in page
    assert DISCLAIMER in unescape(page)


# --- news digest ---------------------------------------------------------------------------------------------------


def _news():
    negative = make_article(title="AMD guidance disappoints", published=NOW - timedelta(hours=2))
    later_positive = make_article(title="AMD wins a Microsoft deal", published=NOW - timedelta(minutes=30))
    mixed = make_article(title="Intel restructures", source_name="Reuters", published=NOW - timedelta(hours=3))
    positive = make_article(title="Nvidia beats estimates", source_name="CNBC", published=NOW - timedelta(hours=1))
    unrelated = [
        make_article(title=f"Macro headline {n}", source_name="FT", published=NOW - timedelta(hours=4 + n))
        for n in range(5)
    ]
    return [
        (later_positive, [make_impact(article_id=later_positive.id, direction="positive", magnitude=3)]),
        (positive, [make_impact(article_id=positive.id, ticker="NVDA", company="Nvidia", direction="positive")]),
        (
            negative,
            [
                make_impact(article_id=negative.id),
                make_impact(article_id=negative.id, ticker="TSM", company="TSMC", relation="indirect", magnitude=2),
            ],
        ),
        (mixed, [make_impact(article_id=mixed.id, ticker="INTC", company="Intel", direction="mixed", magnitude=5)]),
        *[(article, []) for article in unrelated],
    ]


def test_news_digest_puts_negative_news_first_and_groups_by_company():
    text = render_news_digest(_news(), hours=24, generated=NOW, max_other=3)
    assert text.startswith("# News digest: last 24 hours\n")
    assert "_Generated 2026-09-25 15:00 UTC · 9 articles · 4 sources · 4 companies flagged_" in text
    sections = [line for line in text.splitlines() if line.startswith("## ")]
    assert sections == ["## Negative news", "## Mixed news", "## Positive news", "## Other headlines"]
    companies = [line for line in text.splitlines() if line.startswith("### ")]
    # AMD has negative (magnitude 4) and positive news, so it leads; TSM (magnitude 2) next.
    assert companies == ["### AMD — Advanced Micro Devices", "### TSM — TSMC", "### INTC — Intel", "### NVDA — Nvidia"]

    amd = text[text.index("### AMD") : text.index("### TSM")]
    assert "_1 positive, 1 negative_" in amd
    assert amd.index("AMD wins a Microsoft deal") < amd.index("AMD guidance disappoints")  # newest first
    assert "- **negative 4/5** · direct · guidance · [AMD guidance disappoints](<https://" in amd
    assert "· MarketWatch, Sep 25, 13:00 UTC" in amd
    assert "  _Lower data-center guidance cuts expected revenue growth._" in amd
    assert "- **negative 2/5** · indirect · guidance" in text


def test_news_digest_caps_other_headlines_and_says_how_many_were_left_out():
    text = render_news_digest(_news(), hours=24, generated=NOW, max_other=3)
    other = text[text.index("## Other headlines") :]
    assert other.count("- [Macro headline") == 3
    assert "Macro headline 0" in other and "Macro headline 3" not in other  # newest first
    assert "_…and 2 more headlines without a listed company._" in other

    everything = render_news_digest(_news(), hours=24, generated=NOW, max_other=10)
    assert "more headline" not in everything
    assert "## Other headlines" not in render_news_digest(_news(), hours=24, generated=NOW, max_other=0)


def test_news_digest_empty_and_hours_wording():
    assert "No articles in the last 24 hours." in render_news_digest([], hours=24, generated=NOW)
    assert render_news_digest([], hours=1, generated=NOW).startswith("# News digest: last hour\n")
    assert render_news_digest([], hours=1.5, generated=NOW).startswith("# News digest: last 1.5 hours\n")


def test_news_digest_escapes_titles_and_drops_unsafe_links():
    article = make_article(title="Hack [x] | y", link="javascript:alert(1)")
    text = render_news_digest([(article, [make_impact(article_id=article.id)])], hours=24, generated=NOW)
    assert "Hack \\[x\\] \\| y · MarketWatch" in text
    assert "javascript:" not in text


# --- files ---------------------------------------------------------------------------------------------------------


def test_write_reports_writes_dated_files_and_latest(tmp_path):
    opps = [make_opportunity(score=60.0), make_opportunity(ticker="SAP.DE", score=90.0, id=7)]
    paths = write_reports(opps, tmp_path, generated=NOW, notes=["a note"])

    reports = tmp_path / "reports"
    assert paths == [
        reports / "2026-09-25" / "150000-opportunities.md",
        reports / "2026-09-25" / "150000-opportunities.html",
        reports / "2026-09-25" / "150000-opportunities.json",
        reports / "latest.md",
        reports / "latest.html",
    ]
    assert all(path.is_file() for path in paths)
    assert paths[0].read_text(encoding="utf-8") == paths[3].read_text(encoding="utf-8")
    assert paths[1].read_text(encoding="utf-8") == paths[4].read_text(encoding="utf-8")
    assert "a note" in paths[0].read_text(encoding="utf-8")
    assert paths[1].read_text(encoding="utf-8").startswith("<!DOCTYPE html>")

    data = json.loads(paths[2].read_text(encoding="utf-8"))
    assert [Opportunity.from_dict(item) for item in data] == [opps[1], opps[0]]  # ranked, exact round trip
    assert not list(tmp_path.rglob("*.tmp"))


def test_write_reports_overwrites_latest_and_uses_utc(tmp_path):
    write_reports([make_opportunity(ticker="OLD")], tmp_path, generated=NOW)
    athens = timezone(timedelta(hours=3))
    generated = datetime(2026, 9, 26, 1, 30, 5, tzinfo=athens)  # 2026-09-25 22:30:05 UTC
    paths = write_reports([make_opportunity(ticker="NEW")], tmp_path, generated=generated)

    assert paths[0] == tmp_path / "reports" / "2026-09-25" / "223005-opportunities.md"
    latest = (tmp_path / "reports" / "latest.md").read_text(encoding="utf-8")
    assert "## NEW" in latest and "## OLD" not in latest
    assert (tmp_path / "reports" / "2026-09-25" / "150000-opportunities.md").is_file()  # the older report stays


def test_write_reports_creates_missing_folders(tmp_path):
    data_dir = tmp_path / "does" / "not" / "exist"
    paths = write_reports([], data_dir, generated=NOW.replace(tzinfo=None))  # naive -> UTC
    assert paths[2].read_text(encoding="utf-8") == "[]\n"
    assert paths[3] == data_dir / "reports" / "latest.md"


def test_report_uses_the_opportunitys_currency():
    stats = make_stats(currency="GBp", price=245.6)
    opp = make_opportunity(
        stats=stats,
        analysis=make_analysis(potential_low=200.0, entry_price=230.0, target_price=290.0),
    )
    text = render_markdown([opp], title="Report", generated=NOW)
    assert "| Price | 245.60p" in text
    assert "| Entry (limit buy) | 230.00p (6.4% below) |" in text
