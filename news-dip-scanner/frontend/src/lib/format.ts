/**
 * How numbers, amounts and times read on the pages: the same as the Fly app's (dip_scanner/report.py and
 * web/app.py filters), so the React pages and Fly's pages look like one site. The API sends plain numbers and
 * ISO dates; these turn them into text with Intl.
 */
import type { Job, ScoreBand, Verdict } from "./types";

const PENCE = new Set(["GBp", "GBX"]);
const SYMBOLS: Record<string, string> = { USD: "$", EUR: "€", GBP: "£" };

const twoDecimals = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fourDecimals = new Intl.NumberFormat("en-US", { minimumFractionDigits: 4, maximumFractionDigits: 4 });
const wholeNumber = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });

/** report.format_price: $142.50, €12.30, £3.45, 245.60p (London pence), 1,234.00 HKD; four decimals under 1. */
export function formatPrice(value: number | null | undefined, currency: string | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "–";
  const magnitude = Math.abs(value);
  const number = magnitude >= 1 ? twoDecimals.format(magnitude) : fourDecimals.format(magnitude);
  const sign = value < 0 && /[1-9]/.test(number) ? "-" : "";
  const code = (currency ?? "").trim();
  if (PENCE.has(code)) return `${sign}${number}p`;
  const symbol = SYMBOLS[code.toUpperCase()];
  if (symbol) return `${sign}${symbol}${number}`;
  return code ? `${sign}${number} ${code}` : `${sign}${number}`;
}

/** "≈ €115.93", or "" when there is nothing converted. */
export function formatApprox(value: number | null | undefined, currency: string | null | undefined): string {
  if (value === null || value === undefined || !currency) return "";
  return `≈ ${formatPrice(value, currency)}`;
}

/** report.format_pct: a signed percentage with one decimal, +17.9%, -5.0%, +0.0% (never -0.0%). */
export function formatPct(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "–";
  const rounded = Number(value.toFixed(1)) + 0; // like Python's round(value, 1): -0.05 is -0.1, -0.04 is 0
  return `${rounded >= 0 ? "+" : "-"}${Math.abs(rounded).toFixed(1)}%`;
}

/** The sign of a percentage as shown: -0.04 reads "+0.0%" and counts as 0. */
export function pctTone(value: number | null | undefined): "up" | "down" | null {
  if (value === null || value === undefined || !Number.isFinite(value)) return null;
  const rounded = Number(value.toFixed(1));
  return rounded > 0 ? "up" : rounded < 0 ? "down" : null;
}

/** An unsigned chance: 68% ("–" when missing). */
export function formatPercent(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "–";
  return `${value.toFixed(digits)}%`;
}

/** A score with one decimal: 72.4. */
export function formatScore(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "–";
  return value.toFixed(1);
}

/** 1,234 */
export function formatCount(value: number): string {
  return wholeNumber.format(value);
}

/** 12.3k tokens, 1.2M (web/app.py format_tokens). */
export function formatTokens(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
  return String(value);
}

/** "1 idea", "3 ideas" */
export function plural(count: number, singular: string, pluralForm?: string): string {
  return `${formatCount(count)} ${count === 1 ? singular : (pluralForm ?? `${singular}s`)}`;
}

/** "45 s", "2 min 05 s": how long something has been going. */
export function formatElapsed(ms: number): string {
  const seconds = Math.max(0, Math.floor(ms / 1000));
  if (seconds < 60) return `${seconds} s`;
  return `${Math.floor(seconds / 60)} min ${String(seconds % 60).padStart(2, "0")} s`;
}

/**
 * What "Analyse again" says while its analysis waits or runs (job: as last polled, null before the first answer).
 * No promise of "under a minute": with LLM_ANALYSIS_MODE=debate (the Fly deployment's) two models make their case,
 * answer each other and a judge rules, three rounds of model calls that take minutes.
 */
export function analysisProgress(job: Pick<Job, "status" | "ahead"> | null, elapsedMs: number): string {
  if (job?.status === "done") return "Done. Opening the new analysis…";
  if (job?.status === "running") {
    return (
      `Analysing now (${formatElapsed(elapsedMs)} so far): prices, headlines, then the models. A debate between two ` +
      "models takes a few minutes; you can leave this page, and the new analysis will be in the ideas list."
    );
  }
  return `Waiting to start${job?.ahead ? ` (${job.ahead} ahead)` : ""}. Analyses run one at a time.`;
}

/** report.SCORE_BANDS: 80+ strong, 65-80 good, 50-65 fair, under 50 weak. */
export function scoreBand(score: number): ScoreBand {
  if (score >= 80) return "strong";
  if (score >= 65) return "good";
  if (score >= 50) return "fair";
  return "weak";
}

export const VERDICT_LABELS: Record<Verdict, string> = {
  temporary_fear: "Temporary fear",
  mixed: "Mixed",
  fundamental: "Fundamental damage",
  unclear: "Unclear",
};

export function verdictLabel(verdict: string): string {
  return VERDICT_LABELS[verdict as Verdict] ?? verdict.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}

/** web/app.relative_time: "just now", "5 min ago", "3 h ago", "2 days ago", "in 3 min". */
export function relativeTime(iso: string | null | undefined, now: number): string {
  if (!iso) return "–";
  const moment = Date.parse(iso);
  if (Number.isNaN(moment)) return "–";
  let seconds = (now - moment) / 1000;
  const ahead = seconds < 0;
  seconds = Math.abs(seconds);
  if (seconds < 45) return "just now";
  let text: string;
  if (seconds < 90 * 60) text = `${Math.max(1, Math.round(seconds / 60))} min`;
  else if (seconds < 36 * 3600) text = `${Math.round(seconds / 3600)} h`;
  else {
    const days = Math.round(seconds / 86400);
    text = `${days} day${days === 1 ? "" : "s"}`;
  }
  return ahead ? `in ${text}` : `${text} ago`;
}

function zoneName(moment: Date, timeZone: string): string {
  // en-GB names European zones (EEST, CEST), en-US American ones (EDT); otherwise an offset (GMT+8).
  for (const locale of ["en-GB", "en-US"]) {
    const part = new Intl.DateTimeFormat(locale, { timeZone, timeZoneName: "short" })
      .formatToParts(moment)
      .find((item) => item.type === "timeZoneName")?.value;
    if (part && !/^GMT[+-]/.test(part)) return part;
  }
  const fallback = new Intl.DateTimeFormat("en-GB", { timeZone, timeZoneName: "short" })
    .formatToParts(moment)
    .find((item) => item.type === "timeZoneName")?.value;
  return fallback ?? timeZone;
}

function safeZone(timeZone: string | null | undefined): string {
  try {
    new Intl.DateTimeFormat("en-GB", { timeZone: timeZone || "UTC" });
    return timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

/** web/app.format_when: "2026-09-25 18:00 EEST" in the reader's time zone. */
export function formatWhen(iso: string | null | undefined, timeZone: string | null | undefined): string {
  if (!iso) return "–";
  const moment = new Date(iso);
  if (Number.isNaN(moment.getTime())) return "–";
  const zone = safeZone(timeZone);
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-GB", {
      timeZone: zone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    })
      .formatToParts(moment)
      .map((part) => [part.type, part.value]),
  );
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute} ${zoneName(moment, zone)}`;
}

/** "18:00 EEST" */
export function formatClock(iso: string | null | undefined, timeZone: string | null | undefined): string {
  const text = formatWhen(iso, timeZone);
  return text === "–" ? text : text.slice(11);
}

function offsetMinutes(moment: Date, timeZone: string): number {
  const name = new Intl.DateTimeFormat("en-US", { timeZone, timeZoneName: "longOffset" })
    .formatToParts(moment)
    .find((part) => part.type === "timeZoneName")?.value;
  const match = /GMT([+-])(\d{2}):?(\d{2})?/.exec(name ?? "");
  return match ? (match[1] === "-" ? -1 : 1) * (Number(match[2]) * 60 + Number(match[3] ?? 0)) : 0;
}

/**
 * The US exchanges' regular session (09:30 to 16:00 New York time) on the day of `now`, in the reader's time zone:
 * "16:30–23:00 EEST" for Athens. When most of the scanner's dips happen.
 */
export function usSessionHours(now: number, timeZone: string | null | undefined): string {
  const moment = new Date(now);
  const day = Object.fromEntries(
    new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", year: "numeric", month: "numeric", day: "numeric" })
      .formatToParts(moment)
      .map((part) => [part.type, part.value]),
  );
  const midnight = Date.UTC(Number(day.year), Number(day.month) - 1, Number(day.day));
  const offset = offsetMinutes(moment, "America/New_York");
  const at = (minutes: number) => new Date(midnight + (minutes - offset) * 60_000).toISOString();
  const open = formatClock(at(9 * 60 + 30), timeZone);
  return `${open.split(" ")[0]}–${formatClock(at(16 * 60), timeZone)}`;
}

/** What a cycle found, from its summary ("Cycle 2026-09-25 18:00 EEST: 19/20 feeds ok, 37 new articles, ...; took
 * 41 s; ..."): the counts only. A failed cycle's summary stays whole. */
export function cycleFindings(summary: string): string {
  const match = /^Cycle .*?\d{1,2}:\d{2}(?: \S+)?: ([\s\S]*)$/.exec(summary);
  return (match ? match[1] : summary).split("; ")[0];
}

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
export const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function dayParts(day: string): Date | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return null;
  const moment = new Date(`${day}T00:00:00Z`);
  return Number.isNaN(moment.getTime()) ? null : moment;
}

/** A calendar day (YYYY-MM-DD, no time zone): "Fri 25 Sep 2026" (Python's "%a %d %b %Y", without the 0). */
export function formatDay(day: string | null | undefined): string {
  const moment = day ? dayParts(day) : null;
  if (!moment) return "–";
  return `${WEEKDAYS[moment.getUTCDay()]} ${moment.getUTCDate()} ${MONTHS[moment.getUTCMonth()]} ${moment.getUTCFullYear()}`;
}

/** "25 Sep" for the chart's axis and marker. */
export function formatShortDay(day: string): string {
  const moment = dayParts(day);
  return moment ? `${moment.getUTCDate()} ${MONTHS[moment.getUTCMonth()]}` : "–";
}

/** An http(s) link, or null: headlines come from feeds and are never trusted with other schemes. */
export function safeUrl(url: string | null | undefined): string | null {
  if (!url || /[\s\u0000-\u001f]/.test(url)) return null;
  try {
    const parsed = new URL(url);
    return parsed.protocol === "http:" || parsed.protocol === "https:" ? parsed.toString() : null;
  } catch {
    return null;
  }
}

/** "reuters.com" for a link. */
export function hostOf(url: string | null | undefined): string {
  const link = safeUrl(url);
  return link ? new URL(link).hostname.replace(/^www\./, "") : "";
}
