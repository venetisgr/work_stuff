// The price chart's geometry: ticks, scales, gaps, the nearest close, label spreading and the text summary.
import { describe, expect, it } from "vitest";
import {
  areaPath,
  chartSummary,
  layoutFor,
  linePath,
  linearScale,
  monthTicks,
  nearestIndex,
  niceTicks,
  spreadLabels,
  tickGutter,
  tickText,
  toPoints,
  yDomain,
} from "@/lib/chart";
import type { ChartData } from "@/lib/types";

const data: ChartData = {
  currency: "USD",
  closes: [
    ["2026-09-01", 100],
    ["2026-09-02", 102],
    ["2026-09-03", 98],
    ["2026-09-25", 110],
  ],
  levels: { reported: 110, entry: 104, target: 125, potential_low: 95, stat_low: 80 },
  signal_day: "2026-09-27",
  split_factor: 1,
  split_note: null,
};

describe("chart geometry", () => {
  it("draws phones and desktops at their own size", () => {
    expect(layoutFor(358)).toMatchObject({ width: 358, wide: false });
    expect(layoutFor(640)).toMatchObject({ width: 640, wide: true });
    expect(layoutFor(100).width).toBe(280);
  });

  it("round ticks inside the range", () => {
    expect(niceTicks(402, 870, 6)).toEqual([500, 600, 700, 800]);
    expect(niceTicks(9.2, 14.8, 6)).toEqual([10, 12, 14]);
    expect(niceTicks(9.2, 14.8, 7)).toEqual([10, 11, 12, 13, 14]);
    expect(niceTicks(5, 5)).toEqual([5]);
    expect(tickText(1250, 250)).toBe("1,250");
    expect(tickText(12.5, 0.5)).toBe("12.5");
    // with the currency, like the Fly chart's axis (charts.tick_text)
    expect(tickText(150, 10, "USD")).toBe("$150");
    expect(tickText(12.5, 0.5, "EUR")).toBe("€12.5");
    expect(tickText(245, 5, "GBp")).toBe("245p");
    expect(tickText(1200, 100, "HKD")).toBe("1,200");
    expect(tickText(80, 10, "ZAc")).toBe("80");
    expect(tickGutter(["$1,250", "$1,000"], 46)).toBe(52);
    expect(tickGutter(["$16", "$14"], 46)).toBe(46);
  });

  it("a price range with room above and below", () => {
    const [lo, hi] = yDomain([100, 200]);
    expect(lo).toBeLessThan(100);
    expect(hi).toBeGreaterThan(200);
    expect(yDomain([])).toEqual([0, 1]);
    expect(yDomain([50, 50])[0]).toBeLessThan(50);
  });

  it("month ticks, thinned on a phone", () => {
    const start = Date.parse("2026-03-30T00:00:00Z");
    const end = Date.parse("2026-09-27T00:00:00Z");
    expect(monthTicks(start, end, 12).map((t) => t.label)).toEqual(["Apr", "May", "Jun", "Jul", "Aug", "Sep"]);
    expect(monthTicks(start, end, 4)).toHaveLength(3);
    expect(monthTicks(Date.parse("2025-11-20T00:00:00Z"), Date.parse("2026-02-01T00:00:00Z"), 12).map((t) => t.label))
      .toEqual(["Dec", "2026", "Feb"]);
  });

  it("breaks the line over a gap of more than 10 days", () => {
    const points = toPoints(data.closes);
    const x = linearScale([points[0].t, points[3].t], [0, 100]);
    const y = linearScale([90, 120], [100, 0]);
    const path = linePath(points, x, y);
    expect(path.match(/M/g)).toHaveLength(2);
    expect(areaPath(points, x, y, 100).match(/Z/g)).toHaveLength(1); // the lone last close has no area
  });

  it("finds the nearest close", () => {
    const points = toPoints(data.closes);
    expect(nearestIndex(points, Date.parse("2026-09-02T10:00:00Z"))).toBe(1);
    expect(nearestIndex(points, Date.parse("2026-09-20T00:00:00Z"))).toBe(3);
    expect(nearestIndex(points, 0)).toBe(0);
    expect(nearestIndex([], 0)).toBe(-1);
  });

  it("spreads close labels apart inside the plot, keeping their order", () => {
    const slots = spreadLabels(
      [
        { key: "a", y: 50 },
        { key: "b", y: 52 },
        { key: "c", y: 54 },
        { key: "d", y: 199 },
      ],
      14,
      10,
      200,
    );
    const ys = slots.map((s) => s.labelY);
    expect(slots.map((s) => s.key)).toEqual(["a", "b", "c", "d"]);
    for (let i = 1; i < ys.length; i++) expect(ys[i] - ys[i - 1]).toBeGreaterThanOrEqual(14);
    expect(Math.max(...ys)).toBeLessThanOrEqual(200);
  });

  it("summarises the chart in words", () => {
    const text = chartSummary(data, "AMD");
    expect(text).toContain("Daily closes of AMD from Tue 1 Sep 2026 to Fri 25 Sep 2026: from $100.00 to $110.00 (+10.0%).");
    expect(text).toContain("Highest close $110.00 on Fri 25 Sep 2026, lowest $98.00 on Thu 3 Sep 2026.");
    expect(text).toContain("the target (limit sell idea) $125.00");
    expect(text).toContain("reported on Sun 27 Sep 2026");
    expect(chartSummary({ ...data, closes: [] }, "X")).toBe("No daily closes of X.");
  });
});
