"""Server-side SVG price charts for the idea and ticker pages.

A chart is one line of daily closes (the price is the one series, in the accent colour) with the idea's levels as
labelled horizontal lines: the target (limit sell idea), the price in the report, the entry (limit buy), the
statistical 6-month low and the potential low, and a marker at the day the idea was reported. Target and potential
low wear the site's up and down colours, the rest are greys; each line also has its own dash pattern and a direct
label in a gutter on the right (spread apart with short leader lines when levels sit close together), so no line is
told apart by its colour alone.

price_chart() draws every chart twice: a wide one for tablets and desktops (level values in the labels, and a hover
layer: every day is a column with a tooltip and a dot) and a narrow one for phones (short labels; the levels card
next to it has the amounts). pages.css shows one of them by the width of the screen, because an SVG's text scales
with its viewBox and one drawing can't be legible at both 340 and 900 pixels.

There is no JavaScript and no inline style (the Content-Security-Policy forbids both): every colour comes from the
classes in static/pages.css, which use the site's custom properties, so light and dark mode follow the system. The
geometry is presentation attributes only. Each SVG is role="img" with a <title> and a <desc> (the text summary),
and the page adds a table of the closes as the chart's text twin.

Everything written into the SVG is escaped here: ticker and company names come from feeds and a language model. The
result is a markupsafe.Markup, which templates print as it is (without |safe).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from html import escape

from markupsafe import Markup

from ..fx import MINOR_UNITS
from ..report import format_pct, format_price, relative_to

LEVEL_KEYS = ("target", "price", "entry", "stat-low", "low")  # CSS classes chart-level-<key>, top to bottom usually
MAX_GAP_DAYS = 10  # a longer gap between two closes (a trading halt, missing data) breaks the line
_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£"}
_CHAR_WIDTH = 0.6  # the average advance of a character, in font sizes (system sans; digits are about 0.55)


@dataclass(frozen=True)
class Level:
    """A horizontal line: key picks its style (LEVEL_KEYS), label is the name next to it ("Target", "Stat. low"),
    words what the text summary calls it ("the statistical 6-month low")."""

    key: str
    label: str
    value: float
    words: str = ""  # how the summary names it ("the potential low"); the label when empty


@dataclass(frozen=True)
class Layout:
    """The size and look of one drawing, in viewBox units (about CSS pixels at the size it is shown)."""

    name: str  # "wide" or "narrow": CSS class chart-<name>
    width: int
    height: int
    font: float
    left: int  # room for the price axis
    top: int
    bottom: int  # room for the date axis
    values: bool  # level amounts in the labels (the narrow drawing has only the names)
    hover: bool  # a column per day with a tooltip (<title>) and a dot, shown on hover (mouse devices)
    min_label_gap: float  # vertical room per label in the right-hand gutter


WIDE = Layout("wide", 580, 270, 12.5, left=52, top=26, bottom=26, values=True, hover=True, min_label_gap=15.5)
NARROW = Layout("narrow", 340, 232, 11.5, left=44, top=24, bottom=24, values=False, hover=False, min_label_gap=14)


@dataclass(frozen=True)
class PriceChart:
    """The two drawings of a chart, its text summary and the closes it shows (oldest first), for the table view."""

    wide: Markup
    narrow: Markup
    summary: str
    points: list[tuple[date, float]] = field(default_factory=list)

    @property
    def rows(self) -> list[tuple[date, float]]:
        """The closes newest first (the table under the chart)."""
        return list(reversed(self.points))


def price_chart(
    points: Iterable[tuple[date, float]],
    *,
    currency: str | None,
    levels: Sequence[Level] = (),
    marker: date | None = None,
    marker_value: float | None = None,
    name: str = "",
    chart_id: str = "price-chart",
) -> PriceChart | None:
    """The chart of daily closes (day, close), or None when there isn't a single usable close.

    levels are drawn as horizontal lines (ones that aren't positive numbers are left out); marker is the day the
    idea was reported, with a dot at marker_value (the reported price) on that day. name ("AMD") goes into the
    title and summary. chart_id prefixes the ids inside the SVGs (unique per page).
    """
    clean = clean_points(points)
    if not clean:
        return None
    shown = [level for level in levels if _usable(level.value)]
    value = marker_value if marker_value is not None and _usable(marker_value) else None
    summary = summarize(clean, currency=currency, levels=shown, marker=marker, name=name)
    drawings = {
        layout.name: render_svg(
            clean,
            currency=currency,
            levels=shown,
            marker=marker,
            marker_value=value,
            name=name,
            summary=summary,
            layout=layout,
            chart_id=f"{chart_id}-{layout.name}",
        )
        for layout in (WIDE, NARROW)
    }
    return PriceChart(wide=drawings["wide"], narrow=drawings["narrow"], summary=summary, points=clean)


def clean_points(points: Iterable[tuple[date, float]]) -> list[tuple[date, float]]:
    """The closes oldest first, one per day (the last one given wins), without missing, zero or negative ones."""
    by_day: dict[date, float] = {}
    for day, close in points:
        if isinstance(day, date) and _usable(close):
            by_day[day] = float(close)
    return sorted(by_day.items())


def _usable(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value) and value > 0


# --- text ----------------------------------------------------------------------------------------------------------


def day_text(day: date) -> str:
    """ "Fri 25 Sep 2026"."""
    return f"{day:%a} {day.day} {day:%b %Y}"


def _short_day(day: date) -> str:
    return f"{day.day} {day:%b}"


def summarize(
    points: Sequence[tuple[date, float]],
    *,
    currency: str | None,
    levels: Sequence[Level] = (),
    marker: date | None = None,
    name: str = "",
) -> str:
    """The chart in words, for screen readers and as the SVG's <desc>: the first and last close and the change, the
    highest and lowest close, where the last close sits against the entry and the target, the levels and the
    report's day."""
    if not points:
        return f"No prices to show{' for ' + name if name else ''}."
    who = f"{name} " if name else ""
    first_day, first = points[0]
    last_day, last = points[-1]
    if len(points) == 1:
        sentences = [f"{who}has one close so far: {format_price(last, currency)} on {day_text(last_day)}."]
    else:
        high_day, high = max(points, key=lambda point: (point[1], point[0]))
        low_day, low = min(points, key=lambda point: (point[1], point[0]))
        sentences = [
            f"{who}closed at {format_price(last, currency)} on {day_text(last_day)}, "
            f"{format_pct((last / first - 1) * 100)} since {format_price(first, currency)} on {day_text(first_day)}.",
            f"Highest close {format_price(high, currency)} on {day_text(high_day)}, lowest "
            f"{format_price(low, currency)} on {day_text(low_day)}.",
        ]
    named = {level.key: level for level in levels}
    places = [
        f"{relative_to(last, named[key].value).replace('at the price', 'at')} the {words}"
        for key, words in (("entry", "entry"), ("target", "target"))
        if key in named
    ]
    if places:
        sentences.append(f"The last close is {' and '.join(places)}.")
    if levels:
        lines = [
            f"{level.words or 'the ' + level.label.lower()} ({format_price(level.value, currency)})" for level in levels
        ]
        joined = lines[0] if len(lines) == 1 else ", ".join(lines[:-1]) + " and " + lines[-1]
        sentences.append(f"Lines mark {joined}.")
    if marker is not None:
        sentences.append(f"The idea was reported on {day_text(marker)}.")
    return " ".join(sentences)


def tick_text(value: float, currency: str | None, decimals: int) -> str:
    """A price-axis label: "$150", "€12.5", "245p", "1,200" (other currencies: the number only; the chart's title
    names the currency)."""
    code = (currency or "").strip()
    number = f"{value:,.{decimals}f}"
    if code in MINOR_UNITS:
        return f"{number}p" if code in ("GBp", "GBX") else number
    symbol = _SYMBOLS.get(code.upper())
    return f"{symbol}{number}" if symbol else number


def _text_width(text: str, font: float) -> float:
    return len(text) * font * _CHAR_WIDTH


# --- scales --------------------------------------------------------------------------------------------------------


def nice_ticks(low: float, high: float, count: int = 5) -> tuple[list[float], int]:
    """(tick values covering low..high on a 1-2-2.5-5 step, the decimals the step needs): about count ticks."""
    if not (math.isfinite(low) and math.isfinite(high)) or high <= low:
        return [low], 2
    raw = (high - low) / max(1, count - 1)
    power = 10 ** math.floor(math.log10(raw))
    step = next(power * factor for factor in (1, 2, 2.5, 5, 10) if power * factor >= raw * (1 - 1e-9))
    ticks = []
    index = math.ceil(low / step - 1e-9)
    while index * step <= high + step * 1e-9:
        ticks.append(round(index * step, 10))
        index += 1
    decimals = next((digits for digits in range(7) if abs(round(step, digits) - step) < step * 1e-6), 6)
    return ticks, decimals


def _y_domain(values: Sequence[float]) -> tuple[float, float]:
    low, high = min(values), max(values)
    if high - low < max(abs(high), 1e-9) * 0.01:  # flat: give it a band of about ±2%
        pad = max(abs(high) * 0.02, 0.01)
        return max(0.0, low - pad), high + pad
    pad = (high - low) * 0.06
    return max(0.0, low - pad), high + pad


def _date_ticks(start: date, end: date, room: int) -> list[tuple[date, str]]:
    """(day, label) for the date axis, at most room of them: month starts ("Apr"; January with its year, "Jan 2027")
    over long spans, weekly Mondays ("7 Sep") over short ones."""
    span = (end - start).days
    if span > 62:
        months: list[date] = []
        month = date(start.year, start.month, 1)
        while month <= end:
            if month >= start:
                months.append(month)
            month = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
        every = max(1, math.ceil(len(months) / max(1, room)))
        return [(day, f"{day:%b %Y}" if day.month == 1 else f"{day:%b}") for day in months[::every]]
    first = start + timedelta(days=(7 - start.weekday()) % 7)
    days = [first + timedelta(days=7 * n) for n in range(span // 7 + 2) if first + timedelta(days=7 * n) <= end]
    every = max(1, math.ceil(len(days) / max(1, room)))
    return [(day, _short_day(day)) for day in days[::every]]


def spread(positions: Sequence[float], gap: float, low: float, high: float) -> list[float]:
    """Label positions (sorted top to bottom) moved apart so neighbours are at least gap apart, staying between low
    and high where they fit (when they can't, the gap shrinks to fit). Positions that are far enough apart stay."""
    if not positions:
        return []
    if len(positions) > 1 and (high - low) / (len(positions) - 1) < gap:
        gap = max(1.0, (high - low) / (len(positions) - 1))
    out = [min(max(position, low), high) for position in positions]
    for index in range(1, len(out)):
        out[index] = max(out[index], out[index - 1] + gap)
    if out[-1] > high:
        out[-1] = high
        for index in range(len(out) - 2, -1, -1):
            out[index] = min(out[index], out[index + 1] - gap)
    return out


# --- drawing -------------------------------------------------------------------------------------------------------


def _n(value: float) -> str:
    """A coordinate with at most one decimal ("12.3", "40")."""
    text = f"{value:.1f}"
    return text[:-2] if text.endswith(".0") else text


def _attr(value: object) -> str:
    return escape(str(value), quote=True)


def render_svg(
    points: Sequence[tuple[date, float]],
    *,
    currency: str | None,
    levels: Sequence[Level] = (),
    marker: date | None = None,
    marker_value: float | None = None,
    name: str = "",
    summary: str = "",
    layout: Layout = WIDE,
    chart_id: str = "price-chart",
) -> Markup:
    """One drawing of the chart (see price_chart); points must be clean (clean_points) and not empty."""
    font = layout.font
    first_day, last_day = points[0][0], points[-1][0]
    start, end = first_day, max(last_day, marker) if marker is not None else last_day
    if marker is not None and marker < start:
        start = marker
    if start == end:  # a single day: at the right-hand end, next to its label
        start = start - timedelta(days=1)
    last_close = points[-1][1]

    # Labels in the right-hand gutter: the levels and the last close, each at its line: (key, name, amount, value).
    labels = [(level.key, level.label, level.value) for level in levels] + [("last", "Last", last_close)]
    labels = [
        (key, label, format_price(value, currency) if layout.values else "", value) for key, label, value in labels
    ]
    widest = max(_text_width(f"{label} {amount}".strip(), font) for _, label, amount, _ in labels)
    right = int(min(layout.width * 0.42, widest + 16))
    plot_right = layout.width - right
    plot_top, plot_bottom = layout.top, layout.height - layout.bottom

    values = [close for _, close in points] + [level.value for level in levels]
    if marker_value is not None:
        values.append(marker_value)
    y_low, y_high = _y_domain(values)
    room = max(3, int((plot_bottom - plot_top) / (font * 3)))
    ticks, decimals = nice_ticks(y_low, y_high, count=room + 1)
    ticks = [tick for tick in ticks if y_low <= tick <= y_high]
    # The price axis's labels end 6 units left of the plot: a long one ("300,000" in won) widens the gutter instead
    # of losing its first digits past the drawing's edge.
    widest_tick = max((_text_width(tick_text(tick, currency, decimals), font) for tick in ticks), default=0.0)
    plot_left = max(layout.left, math.ceil(widest_tick + 10))

    span = (end - start).days

    def x(day: date) -> float:
        return plot_left + (day - start).days / span * (plot_right - plot_left)

    def y(value: float) -> float:
        return plot_bottom - (value - y_low) / (y_high - y_low) * (plot_bottom - plot_top)

    title = f"{name + ' ' if name else ''}daily closes, {day_text(first_day)} to {day_text(last_day)}"
    if currency:
        title += f", in {currency}"
    parts = [
        f'<svg class="chart-svg chart-{layout.name}" xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {layout.width} {layout.height}" width="{layout.width}" height="{layout.height}" '
        f'font-size="{_n(font)}" role="img" aria-labelledby="{_attr(chart_id)}-title {_attr(chart_id)}-desc">',
        f'<title id="{_attr(chart_id)}-title">{escape(title, quote=False)}</title>',
        f'<desc id="{_attr(chart_id)}-desc">{escape(summary, quote=False)}</desc>',
    ]

    # Grid and axes: solid hairlines, recessive.
    grid = [
        f'<line class="chart-grid" x1="{_n(plot_left)}" x2="{_n(plot_right)}" y1="{_n(y(tick))}" y2="{_n(y(tick))}"/>'
        for tick in ticks
    ]
    grid.append(
        f'<line class="chart-baseline" x1="{_n(plot_left)}" x2="{_n(plot_right)}" y1="{_n(plot_bottom)}" '
        f'y2="{_n(plot_bottom)}"/>'
    )
    parts.append('<g aria-hidden="true">' + "".join(grid) + "</g>")
    axis = [
        f'<text class="chart-tick" x="{_n(plot_left - 6)}" y="{_n(y(tick))}" dy="0.35em" text-anchor="end">'
        f"{escape(tick_text(tick, currency, decimals), quote=False)}</text>"
        for tick in ticks
    ]
    date_room = max(2, int((plot_right - plot_left) / (font * 4.6)))
    for day, text in _date_ticks(start, end, date_room):
        position = x(day)
        anchor = "start" if position - plot_left < font * 1.5 else "middle"
        if plot_right - position < font * 2:
            anchor = "end"
        axis.append(
            f'<line class="chart-grid-tick" x1="{_n(position)}" x2="{_n(position)}" y1="{_n(plot_bottom)}" '
            f'y2="{_n(plot_bottom + 4)}"/>'
            f'<text class="chart-tick" x="{_n(position)}" y="{_n(plot_bottom + font + 5)}" '
            f'text-anchor="{anchor}">{escape(text, quote=False)}</text>'
        )
    parts.append('<g class="chart-axis" aria-hidden="true">' + "".join(axis) + "</g>")

    # The report's day: a hairline across the plot and its name above it.
    if marker is not None:
        position = x(marker)
        text = f"Reported {_short_day(marker)}"
        anchor = "middle"
        if position - plot_left < _text_width(text, font) / 2:
            anchor = "start"
        elif plot_right - position < _text_width(text, font) / 2:
            anchor = "end"
        parts.append(
            f'<g class="chart-marker" aria-hidden="true"><line class="chart-marker-line" x1="{_n(position)}" '
            f'x2="{_n(position)}" y1="{_n(plot_top)}" y2="{_n(plot_bottom)}"/>'
            f'<text class="chart-marker-label" x="{_n(position)}" y="{_n(plot_top - 8)}" text-anchor="{anchor}">'
            f"{escape(text, quote=False)}</text></g>"
        )

    # The levels: horizontal lines across the plot.
    parts.append(
        '<g class="chart-levels" aria-hidden="true">'
        + "".join(
            f'<line class="chart-level chart-level-{_attr(level.key)}" x1="{_n(plot_left)}" x2="{_n(plot_right)}" '
            f'y1="{_n(y(level.value))}" y2="{_n(y(level.value))}"/>'
            for level in levels
        )
        + "</g>"
    )

    # The closes: one line, broken where data is missing for more than MAX_GAP_DAYS.
    if len(points) > 1:
        commands = []
        previous: date | None = None
        for day, close in points:
            move = previous is None or (day - previous).days > MAX_GAP_DAYS
            commands.append(f"{'M' if move else 'L'}{_n(x(day))} {_n(y(close))}")
            previous = day
        parts.append(f'<path class="chart-line" fill="none" d="{" ".join(commands)}"/>')
    parts.append(
        f'<circle class="chart-end" cx="{_n(x(last_day))}" cy="{_n(y(last_close))}" r="{4 if len(points) > 1 else 5}"/>'
    )
    if marker is not None and marker_value is not None:
        parts.append(f'<circle class="chart-marker-dot" cx="{_n(x(marker))}" cy="{_n(y(marker_value))}" r="4.5"/>')

    # Direct labels in the gutter, spread apart; a short leader joins each to its line.
    order = sorted(labels, key=lambda label: y(label[3]))
    wanted = [y(value) for *_, value in order]
    placed = spread(wanted, layout.min_label_gap, plot_top + font / 2, plot_bottom - font / 2)
    gutter = []
    for (key, label, amount, _), target, spot in zip(order, wanted, placed, strict=True):
        start_x = x(last_day) + 5 if key == "last" else plot_right
        css = "chart-leader chart-leader-last" if key == "last" else f"chart-leader chart-level-{_attr(key)}"
        gutter.append(
            f'<path class="{css}" fill="none" d="M{_n(start_x)} {_n(target)} L{_n(plot_right + 6)} {_n(spot)} '
            f'L{_n(plot_right + 9)} {_n(spot)}"/>'
        )
        text = f'<tspan class="chart-label-name">{escape(label, quote=False)}</tspan>'
        if amount:
            text += f' <tspan class="chart-label-value">{escape(amount, quote=False)}</tspan>'
        gutter.append(
            f'<text class="chart-label chart-label-{_attr(key)}" x="{_n(plot_right + 12)}" y="{_n(spot)}" '
            f'dy="0.35em">{text}</text>'
        )
    parts.append('<g class="chart-labels" aria-hidden="true">' + "".join(gutter) + "</g>")

    # Hover: a column per day with the day's close as a tooltip and a dot on the line.
    if layout.hover and len(points) > 1:
        columns = []
        xs = [x(day) for day, _ in points]
        for index, (day, close) in enumerate(points):
            left = plot_left if index == 0 else (xs[index - 1] + xs[index]) / 2
            right_edge = plot_right if index == len(points) - 1 else (xs[index] + xs[index + 1]) / 2
            tip = f"{day_text(day)}: {format_price(close, currency)}"
            columns.append(
                f'<g class="chart-hit"><title>{escape(tip, quote=False)}</title>'
                f'<rect x="{_n(left)}" y="{_n(plot_top)}" width="{_n(max(0.5, right_edge - left))}" '
                f'height="{_n(plot_bottom - plot_top)}"/>'
                f'<circle cx="{_n(xs[index])}" cy="{_n(y(close))}" r="4"/></g>'
            )
        parts.append('<g class="chart-hits">' + "".join(columns) + "</g>")
    parts.append("</svg>")
    return Markup("".join(parts))
