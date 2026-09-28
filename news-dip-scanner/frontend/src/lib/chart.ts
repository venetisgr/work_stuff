/**
 * The geometry of the idea page's price chart, apart from React so the tests can check it: one series (the daily
 * closes, in the accent colour) on a time axis, the idea's levels as labelled horizontal lines, a marker at the
 * report's day, and a text summary for screen readers. Like the Fly app's chart (dip_scanner/web/charts.py): the
 * level labels sit in a gutter on the right, spread apart with short leader lines when levels are close, and a gap
 * of more than MAX_GAP_DAYS between two closes breaks the line.
 */
import { formatDay, formatPct, formatPrice, MONTHS } from "./format";
import type { ChartData, LevelKey } from "./types";

export const MAX_GAP_DAYS = 10;
const DAY_MS = 86_400_000;

export interface ChartLayout {
  width: number;
  height: number;
  left: number;
  right: number;
  top: number;
  bottom: number;
  /** Room for the level amounts in the labels (else names only; the levels card has the amounts). */
  wide: boolean;
}

/** The drawing for a width in CSS pixels: text stays 11.5px whatever the screen. */
export function layoutFor(width: number): ChartLayout {
  const w = Math.max(280, Math.round(width));
  const wide = w >= 540;
  return { width: w, height: wide ? 300 : 248, left: 46, right: wide ? 138 : 70, top: 28, bottom: 26, wide };
}

export interface Point {
  day: string;
  t: number;
  close: number;
}

export function dayTime(day: string): number {
  return Date.parse(`${day}T00:00:00Z`);
}

export function toPoints(closes: ChartData["closes"]): Point[] {
  return closes
    .filter(([day, close]) => Number.isFinite(close) && close > 0 && !Number.isNaN(dayTime(day)))
    .map(([day, close]) => ({ day, t: dayTime(day), close }));
}

/** Round tick values covering [min, max]: 1, 2, 2.5 or 5 times a power of ten. */
export function niceTicks(min: number, max: number, count = 5): number[] {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return [];
  if (min === max) return [min];
  const raw = (max - min) / Math.max(1, count - 1);
  const power = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * power).find((s) => s >= raw) ?? 10 * power;
  const first = Math.ceil(min / step - 1e-9) * step;
  const ticks: number[] = [];
  for (let value = first; value <= max + step * 1e-9; value += step) ticks.push(Number(value.toPrecision(12)));
  return ticks;
}

/** The price range to show: every close and level, with a little room above and below. */
export function yDomain(values: number[]): [number, number] {
  const usable = values.filter((v) => Number.isFinite(v) && v > 0);
  if (!usable.length) return [0, 1];
  let lo = Math.min(...usable);
  let hi = Math.max(...usable);
  if (lo === hi) {
    lo *= 0.95;
    hi *= 1.05;
  }
  const pad = (hi - lo) * 0.06;
  return [Math.max(0, lo - pad), hi + pad];
}

const TICK_SYMBOLS: Record<string, string> = { USD: "$", EUR: "€", GBP: "£" };
const TICK_PENCE = new Set(["GBp", "GBX"]);
const TICK_MINOR = new Set(["GBp", "GBX", "ZAc", "ILA"]); // quoted in a minor unit: no symbol of the main one

/** A price-axis label like the Fly chart's (charts.tick_text): "$150", "€12.5", "245p", "1,200" (other currencies:
 * the number only; the card's subtitle names the currency). */
export function tickText(value: number, step: number, currency: string | null = null): string {
  const decimals = step >= 1 ? 0 : step >= 0.1 ? 1 : step >= 0.01 ? 2 : 3;
  const number = value.toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
  const code = (currency ?? "").trim();
  if (TICK_PENCE.has(code)) return `${number}p`;
  if (TICK_MINOR.has(code)) return number;
  const symbol = TICK_SYMBOLS[code.toUpperCase()];
  return symbol ? `${symbol}${number}` : number;
}

/** The left gutter that fits the longest axis label (11.5px tabular figures, about 7px a character). */
export function tickGutter(labels: string[], minimum: number): number {
  const longest = Math.max(0, ...labels.map((label) => label.length));
  return Math.max(minimum, Math.ceil(longest * 7 + 10));
}

/** The first day of each month in [start, end], thinned to at most maxTicks: {t, label} ("Apr", "2026" at a year). */
export function monthTicks(start: number, end: number, maxTicks: number): { t: number; label: string }[] {
  const ticks: { t: number; label: string }[] = [];
  const cursor = new Date(start);
  cursor.setUTCDate(1);
  cursor.setUTCHours(0, 0, 0, 0);
  if (cursor.getTime() < start) cursor.setUTCMonth(cursor.getUTCMonth() + 1);
  while (cursor.getTime() <= end) {
    const month = cursor.getUTCMonth();
    ticks.push({ t: cursor.getTime(), label: month === 0 ? String(cursor.getUTCFullYear()) : MONTHS[month] });
    cursor.setUTCMonth(month + 1);
  }
  if (ticks.length <= maxTicks) return ticks;
  const every = Math.ceil(ticks.length / maxTicks);
  return ticks.filter((_tick, index) => index % every === 0);
}

export type Scale = (value: number) => number;

export function linearScale([d0, d1]: [number, number], [r0, r1]: [number, number]): Scale {
  const span = d1 - d0 || 1;
  return (value) => r0 + ((value - d0) / span) * (r1 - r0);
}

/** An SVG path through the closes, broken where two closes are more than MAX_GAP_DAYS apart. */
export function linePath(points: Point[], x: Scale, y: Scale): string {
  let path = "";
  points.forEach((point, index) => {
    const gap = index > 0 && point.t - points[index - 1].t > MAX_GAP_DAYS * DAY_MS;
    path += `${index === 0 || gap ? "M" : "L"}${x(point.t).toFixed(1)},${y(point.close).toFixed(1)}`;
  });
  return path;
}

/** The wash under the line down to the plot's bottom, per unbroken run. */
export function areaPath(points: Point[], x: Scale, y: Scale, bottom: number): string {
  const runs: Point[][] = [];
  points.forEach((point, index) => {
    const gap = index > 0 && point.t - points[index - 1].t > MAX_GAP_DAYS * DAY_MS;
    if (index === 0 || gap) runs.push([]);
    runs[runs.length - 1].push(point);
  });
  return runs
    .filter((run) => run.length > 1)
    .map((run) => {
      const line = run.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.close).toFixed(1)}`).join("");
      return `${line}L${x(run[run.length - 1].t).toFixed(1)},${bottom}L${x(run[0].t).toFixed(1)},${bottom}Z`;
    })
    .join("");
}

/** The index of the close nearest to time t (points sorted by t). */
export function nearestIndex(points: Point[], t: number): number {
  if (!points.length) return -1;
  let lo = 0;
  let hi = points.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (points[mid].t <= t) lo = mid;
    else hi = mid;
  }
  return Math.abs(points[lo].t - t) <= Math.abs(points[hi].t - t) ? lo : hi;
}

export interface LabelSlot {
  key: string;
  y: number;
  labelY: number;
}

/** Vertical places for labels that want to sit at y: at least gap apart, inside [top, bottom], order kept. */
export function spreadLabels(items: { key: string; y: number }[], gap: number, top: number, bottom: number): LabelSlot[] {
  const sorted = [...items].sort((a, b) => a.y - b.y);
  const slots = sorted.map((item) => ({ ...item, labelY: item.y }));
  for (let i = 0; i < slots.length; i++) {
    const min = i === 0 ? top : slots[i - 1].labelY + gap;
    slots[i].labelY = Math.max(slots[i].labelY, min);
  }
  for (let i = slots.length - 1; i >= 0; i--) {
    const max = i === slots.length - 1 ? bottom : slots[i + 1].labelY - gap;
    slots[i].labelY = Math.min(slots[i].labelY, max);
  }
  return slots;
}

export const LEVELS: { key: LevelKey; field: keyof ChartData["levels"]; name: string; words: string }[] = [
  { key: "target", field: "target", name: "Target", words: "the target (limit sell idea)" },
  { key: "price", field: "reported", name: "Reported", words: "the price in the report" },
  { key: "entry", field: "entry", name: "Entry", words: "the entry (limit buy)" },
  { key: "stat_low", field: "stat_low", name: "Stat. low", words: "the statistical 6-month low" },
  { key: "potential_low", field: "potential_low", name: "Low", words: "the potential low" },
];

/** What the chart shows, in sentences (the SVG's description; the table of closes is the full text twin). */
export function chartSummary(data: ChartData, name: string): string {
  const points = toPoints(data.closes);
  if (!points.length) return `No daily closes of ${name}.`;
  const first = points[0];
  const last = points[points.length - 1];
  const high = points.reduce((a, b) => (b.close > a.close ? b : a));
  const low = points.reduce((a, b) => (b.close < a.close ? b : a));
  const money = (value: number) => formatPrice(value, data.currency);
  const sentences = [
    `Daily closes of ${name} from ${formatDay(first.day)} to ${formatDay(last.day)}: from ${money(first.close)} to ` +
      `${money(last.close)} (${formatPct((last.close / first.close - 1) * 100)}).`,
    `Highest close ${money(high.close)} on ${formatDay(high.day)}, lowest ${money(low.close)} on ${formatDay(low.day)}.`,
    `Lines at ${LEVELS.map((level) => `${level.words} ${money(data.levels[level.field])}`).join(", ")}.`,
    `The idea was reported on ${formatDay(data.signal_day)}.`,
  ];
  return sentences.join(" ");
}
