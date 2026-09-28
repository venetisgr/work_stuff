// The formatters give the same text as the Fly app's (report.py, web/app.py), so both halves read alike.
import { describe, expect, it } from "vitest";
import {
  formatApprox,
  formatClock,
  formatDay,
  formatPct,
  formatPercent,
  formatPrice,
  formatScore,
  formatTokens,
  formatWhen,
  hostOf,
  pctTone,
  plural,
  relativeTime,
  safeUrl,
  scoreBand,
  verdictLabel,
} from "@/lib/format";

describe("formatPrice (report.format_price)", () => {
  it.each([
    [142.5, "USD", "$142.50"],
    [12.3, "EUR", "€12.30"],
    [3.45, "GBP", "£3.45"],
    [245.6, "GBp", "245.60p"],
    [245.6, "GBX", "245.60p"],
    [1234, "HKD", "1,234.00 HKD"],
    [0.0123, "USD", "$0.0123"],
    [-5.5, "EUR", "-€5.50"],
    [-0.00001, "USD", "$0.0000"],
    [12, "", "12.00"],
    [12, null, "12.00"],
  ])("%d %s -> %s", (value, currency, text) => {
    expect(formatPrice(value, currency)).toBe(text);
  });

  it("shows a dash for missing values", () => {
    expect(formatPrice(null, "USD")).toBe("–");
    expect(formatPrice(Number.NaN, "USD")).toBe("–");
  });

  it("approx amounts", () => {
    expect(formatApprox(115.934, "EUR")).toBe("≈ €115.93");
    expect(formatApprox(null, "EUR")).toBe("");
    expect(formatApprox(1, null)).toBe("");
  });
});

describe("percentages and scores", () => {
  it.each([
    [17.94, "+17.9%"],
    [-5, "-5.0%"],
    [0, "+0.0%"],
    [-0.04, "+0.0%"],
    [-0.05, "-0.1%"],
    [null, "–"],
  ])("formatPct(%s) = %s", (value, text) => {
    expect(formatPct(value)).toBe(text);
  });

  it("colours by the value as shown", () => {
    expect(pctTone(0.3)).toBe("up");
    expect(pctTone(-0.3)).toBe("down");
    expect(pctTone(-0.04)).toBeNull();
    expect(pctTone(null)).toBeNull();
  });

  it("unsigned chances and scores", () => {
    expect(formatPercent(68)).toBe("68%");
    expect(formatPercent(12.345, 1)).toBe("12.3%");
    expect(formatScore(57.46)).toBe("57.5");
    expect(formatScore(null)).toBe("–");
  });

  it("score bands (report.SCORE_BANDS)", () => {
    expect(scoreBand(80)).toBe("strong");
    expect(scoreBand(79.99)).toBe("good");
    expect(scoreBand(65)).toBe("good");
    expect(scoreBand(64.96)).toBe("fair");
    expect(scoreBand(50)).toBe("fair");
    expect(scoreBand(12)).toBe("weak");
  });

  it("verdicts, counts and tokens", () => {
    expect(verdictLabel("temporary_fear")).toBe("Temporary fear");
    expect(verdictLabel("fundamental")).toBe("Fundamental damage");
    expect(verdictLabel("new_kind")).toBe("New kind");
    expect(plural(1, "idea")).toBe("1 idea");
    expect(plural(1234, "idea")).toBe("1,234 ideas");
    expect(plural(2, "analysis", "analyses")).toBe("2 analyses");
    expect(formatTokens(148230)).toBe("148.2k");
    expect(formatTokens(2_500_000)).toBe("2.5M");
    expect(formatTokens(950)).toBe("950");
  });
});

describe("times", () => {
  const now = Date.parse("2026-09-28T06:00:00Z");

  it.each([
    ["2026-09-28T05:59:30Z", "just now"],
    ["2026-09-28T05:55:00Z", "5 min ago"],
    ["2026-09-28T04:40:00Z", "80 min ago"],
    ["2026-09-28T03:00:00Z", "3 h ago"],
    ["2026-09-26T18:00:00Z", "36 h ago".replace("36 h", "2 days")],
    ["2026-09-27T06:00:00Z", "24 h ago"],
    ["2026-09-21T06:00:00Z", "7 days ago"],
    ["2026-09-28T06:03:00Z", "in 3 min"],
    ["nonsense", "–"],
  ])("relativeTime(%s) = %s", (iso, text) => {
    expect(relativeTime(iso, now)).toBe(text);
  });

  it("formatWhen in the reader's zone, with its abbreviation", () => {
    expect(formatWhen("2026-09-25T15:00:00+00:00", "Europe/Athens")).toBe("2026-09-25 18:00 EEST");
    expect(formatWhen("2026-01-25T15:00:00+00:00", "Europe/Athens")).toBe("2026-01-25 17:00 EET");
    expect(formatWhen("2026-09-25T15:00:00+00:00", "America/New_York")).toBe("2026-09-25 11:00 EDT");
    expect(formatWhen("2026-09-25T15:00:00+00:00", "UTC")).toBe("2026-09-25 15:00 UTC");
    expect(formatWhen("2026-09-25T15:00:00+00:00", "Asia/Hong_Kong")).toBe("2026-09-25 23:00 GMT+8");
    expect(formatWhen("2026-09-25T15:00:00+00:00", "Not/AZone")).toBe("2026-09-25 15:00 UTC");
    expect(formatClock("2026-09-25T15:00:00+00:00", "Europe/Berlin")).toBe("17:00 CEST");
    expect(formatWhen(null, "UTC")).toBe("–");
  });

  it("formatDay for calendar days", () => {
    expect(formatDay("2026-09-25")).toBe("Fri 25 Sep 2026");
    expect(formatDay("2026-9-25")).toBe("–");
    expect(formatDay(null)).toBe("–");
  });
});

describe("links", () => {
  it("allows only http and https", () => {
    expect(safeUrl("https://www.reuters.com/x")).toBe("https://www.reuters.com/x");
    expect(safeUrl("http://example.com")).toBe("http://example.com/");
    expect(safeUrl("javascript:alert(1)")).toBeNull();
    expect(safeUrl("data:text/html,x")).toBeNull();
    expect(safeUrl("https://a.com/ x")).toBeNull();
    expect(safeUrl("/relative")).toBeNull();
    expect(safeUrl(null)).toBeNull();
    expect(hostOf("https://www.reuters.com/x")).toBe("reuters.com");
    expect(hostOf("javascript:x")).toBe("");
  });
});
