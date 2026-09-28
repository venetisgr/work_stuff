"""The SVG price chart (web/charts.py): well-formed, accessible, escaped, styled only by classes (so dark mode works
under the Content-Security-Policy), and sensible with missing data, gaps and a single close."""

from __future__ import annotations

import itertools
import math
import re
import xml.etree.ElementTree as ET
from datetime import date, timedelta

import pytest

from dip_scanner.web.charts import (
    MAX_GAP_DAYS,
    NARROW,
    WIDE,
    Level,
    PriceChart,
    clean_points,
    nice_ticks,
    price_chart,
    render_svg,
    spread,
    summarize,
    tick_text,
)

SVG = "{http://www.w3.org/2000/svg}"
REPORTED = date(2026, 9, 25)
LEVELS = [
    Level("target", "Target", 168.0, "the target"),
    Level("price", "Reported", 142.5, "the price in the report"),
    Level("entry", "Entry", 132.0, "the entry"),
    Level("stat-low", "Stat. low", 125.6, "the statistical 6-month low"),
    Level("low", "Low", 118.0, "the potential low"),
]


def closes(count: int = 120, start: date = date(2026, 4, 1), first: float = 150.0) -> list[tuple[date, float]]:
    """Weekday closes drifting down then up, deterministic."""
    points, day, index = [], start, 0
    while len(points) < count:
        if day.weekday() < 5:
            points.append((day, round(first * (1 + 0.12 * math.sin(index / 9)) - index * 0.1, 2)))
            index += 1
        day += timedelta(days=1)
    return points


def chart(**overrides) -> PriceChart:
    values = {"currency": "USD", "levels": LEVELS, "marker": REPORTED, "marker_value": 142.5, "name": "AMD"}
    values.update(overrides)
    points = values.pop("points", closes())
    result = price_chart(points, **values)
    assert result is not None
    return result


def parse(svg) -> ET.Element:
    return ET.fromstring(str(svg))


def labels_of(root: ET.Element) -> list[str]:
    """The texts of the direct labels in the gutter, whitespace collapsed."""
    return [
        " ".join("".join(text.itertext()).split())
        for text in root.iter(f"{SVG}text")
        if "chart-label" in (text.get("class") or "")
    ]


def classes(root: ET.Element) -> list[str]:
    return [name for element in root.iter() for name in (element.get("class") or "").split()]


def test_both_drawings_are_well_formed_accessible_svgs():
    result = chart()
    for svg, layout in ((result.wide, WIDE), (result.narrow, NARROW)):
        root = parse(svg)
        assert root.tag == f"{SVG}svg"
        assert root.get("viewBox") == f"0 0 {layout.width} {layout.height}"
        assert root.get("role") == "img"
        assert f"chart-{layout.name}" in root.get("class")
        title_id, desc_id = root.get("aria-labelledby").split()
        found = {element.get("id"): element for element in root.iter() if element.get("id")}
        assert found[title_id].tag == f"{SVG}title" and found[desc_id].tag == f"{SVG}desc"
        assert found[title_id].text.startswith("AMD daily closes, Wed 1 Apr 2026 to ")
        assert found[desc_id].text == result.summary
    # ids are unique per drawing, so both can sit on one page
    assert parse(result.wide).get("aria-labelledby") != parse(result.narrow).get("aria-labelledby")


def test_colours_come_from_classes_only():
    """No inline style (the CSP blocks it), no event handlers and no hard-coded colour that would break dark mode."""
    for svg in (chart().wide, chart().narrow):
        text = str(svg)
        assert not re.search(r"\sstyle\s*=", text)
        assert not re.search(r"\son[a-z]+\s*=", text)
        assert "<script" not in text
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b", text)
        assert not re.search(r'\s(fill|stroke)="(?!none")', text)
        found = classes(parse(svg))
        for name in ("chart-line", "chart-end", "chart-grid", "chart-tick", "chart-marker-line", "chart-marker-dot"):
            assert name in found, name
        for key in ("target", "price", "entry", "stat-low", "low"):
            assert f"chart-level-{key}" in found, key


def test_untrusted_names_are_escaped():
    evil = '<script>alert(1)</script>"&'
    result = chart(name=evil, levels=[Level("target", "<b>Target</b>", 168.0)])
    for svg in (result.wide, result.narrow):
        text = str(svg)
        assert "<script>" not in text and "<b>" not in text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text
        root = parse(svg)  # still well-formed
        assert evil in root.find(f"{SVG}title").text
    assert evil in result.summary  # plain text; the template escapes it


def test_levels_are_labelled_with_their_values_in_the_wide_drawing_only():
    result = chart()
    wide, narrow = (labels_of(parse(svg)) for svg in (result.wide, result.narrow))
    for text in ("Target $168.00", "Entry $132.00", "Stat. low $125.60", "Low $118.00", "Reported $142.50"):
        assert text in wide
    assert "Last $149.08" in wide
    assert sorted(narrow) == ["Entry", "Last", "Low", "Reported", "Stat. low", "Target"]


def test_level_lines_sit_at_their_prices():
    root = parse(chart().wide)
    lines = {
        element.get("class").split()[-1]: float(element.get("y1"))
        for element in root.iter(f"{SVG}line")
        if "chart-level" in (element.get("class") or "")
    }
    assert lines["chart-level-target"] < lines["chart-level-price"] < lines["chart-level-entry"]
    assert lines["chart-level-entry"] < lines["chart-level-stat-low"] < lines["chart-level-low"]


def test_close_labels_are_spread_apart():
    near = [Level("target", "Target", 150.2), Level("price", "Reported", 150.0), Level("entry", "Entry", 149.9)]
    root = parse(chart(levels=near).wide)
    spots = sorted(float(text.get("y")) for text in root.iter(f"{SVG}text") if "chart-label" in text.get("class"))
    assert len(spots) == 4  # three levels and the last close
    assert all(later - earlier >= WIDE.min_label_gap - 0.01 for earlier, later in itertools.pairwise(spots))


def test_the_report_day_is_marked():
    result = chart()
    root = parse(result.wide)
    [label] = [text for text in root.iter(f"{SVG}text") if "chart-marker-label" in text.get("class")]
    assert label.text == "Reported 25 Sep"
    assert "The idea was reported on Fri 25 Sep 2026." in result.summary
    # A report before the first close widens the chart to include it.
    early = chart(marker=date(2026, 3, 20))
    assert "Reported 20 Mar" in str(early.wide)


def test_hover_columns_show_each_close_on_wide_screens():
    points = closes(30)
    result = chart(points=points)
    hits = [group for group in parse(result.wide).iter(f"{SVG}g") if group.get("class") == "chart-hit"]
    assert len(hits) == 30
    assert hits[0].find(f"{SVG}title").text == "Wed 1 Apr 2026: $" + f"{points[0][1]:,.2f}"
    assert "chart-hit" not in str(result.narrow)  # phones: the table under the chart has the closes


def test_missing_data_is_left_out_and_long_gaps_break_the_line():
    points = closes(40)
    gap_start = points[20][0] + timedelta(days=MAX_GAP_DAYS + 5)
    holed = points[:20] + [(day + timedelta(days=MAX_GAP_DAYS + 5), close) for day, close in points[20:]]
    bad = [(date(2026, 4, 4), float("nan")), (date(2026, 4, 5), None), (date(2026, 4, 11), -1.0), (points[3][0], 0)]
    result = chart(points=[*holed, *bad])
    assert len(result.points) == 40  # the bad closes are left out, and never replace a good one
    [path] = [p for p in parse(result.wide).iter(f"{SVG}path") if p.get("class") == "chart-line"]
    assert path.get("d").count("M") == 2
    assert gap_start in dict(result.points)


def test_no_usable_close_means_no_chart():
    assert price_chart([], currency="USD") is None
    assert price_chart([(date(2026, 9, 1), 0.0), (date(2026, 9, 2), float("inf"))], currency="USD") is None


def test_a_single_close_is_a_dot():
    result = price_chart([(REPORTED, 142.5)], currency="EUR", levels=LEVELS[:3], name="SAP.DE")
    assert result is not None
    root = parse(result.wide)
    assert not [p for p in root.iter(f"{SVG}path") if p.get("class") == "chart-line"]
    assert [c for c in root.iter(f"{SVG}circle") if c.get("class") == "chart-end"]
    assert "SAP.DE has one close so far: €142.50 on Fri 25 Sep 2026." in result.summary
    parse(result.narrow)


def test_a_flat_series_still_has_a_scale():
    result = price_chart([(REPORTED - timedelta(days=n), 100.0) for n in range(10)], currency="USD")
    root = parse(result.wide)
    ticks = [text.text for text in root.iter(f"{SVG}text") if text.get("class") == "chart-tick" and "$" in text.text]
    assert ticks and all(value.startswith("$") for value in ticks)
    assert all(math.isfinite(float(c.get("cy"))) for c in root.iter(f"{SVG}circle"))


def test_invalid_levels_are_skipped():
    result = chart(levels=[Level("target", "Target", float("nan")), Level("entry", "Entry", 0), *LEVELS[4:]])
    found = classes(parse(result.wide))
    assert "chart-level-target" not in found and "chart-level-entry" not in found
    assert "chart-level-low" in found


def test_summary_says_where_the_last_close_is():
    points = [(date(2026, 9, 21), 100.0), (date(2026, 9, 22), 90.0), (date(2026, 9, 24), 120.0)]
    text = summarize(points, currency="USD", levels=LEVELS, marker=REPORTED, name="AMD")
    assert text.startswith("AMD closed at $120.00 on Thu 24 Sep 2026, +20.0% since $100.00 on Mon 21 Sep 2026.")
    assert "Highest close $120.00 on Thu 24 Sep 2026, lowest $90.00 on Tue 22 Sep 2026." in text
    assert "The last close is 9.1% below the entry and 28.6% below the target." in text
    assert "Lines mark the target ($168.00), the price in the report ($142.50)" in text
    assert "and the potential low ($118.00)." in text
    assert summarize([], currency="USD", name="AMD") == "No prices to show for AMD."


def test_clean_points_sorts_and_keeps_the_last_close_of_a_day():
    day = date(2026, 9, 1)
    assert clean_points([(day + timedelta(days=1), 2.0), (day, 1.0), (day, 1.5), ("x", 3.0), (day, True)]) == [
        (day, 1.5),
        (day + timedelta(days=1), 2.0),
    ]


@pytest.mark.parametrize(
    ("low", "high", "expected", "decimals"),
    [
        (95.3, 190.2, [100.0, 125.0, 150.0, 175.0], 0),
        (0.012, 0.019, [0.012, 0.014, 0.016, 0.018], 3),
        (1000, 1300, [1000, 1100, 1200, 1300], 0),
        (9.2, 10.9, [9.5, 10.0, 10.5], 1),
    ],
)
def test_nice_ticks(low, high, expected, decimals):
    ticks, digits = nice_ticks(low, high, 5)
    assert ticks == pytest.approx(expected) and digits == decimals


def test_tick_labels_by_currency():
    assert tick_text(150, "USD", 0) == "$150"
    assert tick_text(12.5, "EUR", 1) == "€12.5"
    assert tick_text(245, "GBp", 0) == "245p"
    assert tick_text(1200, "JPY", 0) == "1,200"
    assert tick_text(3.5, None, 1) == "3.5"


def test_spread_keeps_order_gaps_and_bounds():
    assert spread([10, 12, 13, 100], 14, 5, 200) == [10, 24, 38, 100]
    assert spread([195, 196, 199], 10, 0, 200) == [180, 190, 200]
    squeezed = spread([0, 0, 0, 0, 0], 20, 0, 40)  # can't fit: the gap shrinks
    assert squeezed == [0, 10, 20, 30, 40]
    assert spread([], 10, 0, 100) == []


def test_render_svg_on_its_own():
    svg = render_svg(closes(5), currency="USD", layout=NARROW, chart_id="x")
    root = parse(svg)
    assert root.get("aria-labelledby") == "x-title x-desc"
