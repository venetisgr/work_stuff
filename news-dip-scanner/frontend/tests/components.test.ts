// The React pieces whose words matter, rendered to HTML on the server as Next.js does: the idea list's figures and
// their labels, the status strip, the empty dashboard, the 404's header and the home-screen icon.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import IdeaList from "@/components/IdeaList";
import NoIdeas from "@/components/NoIdeas";
import NotFoundView from "@/components/NotFoundView";
import StatusStrip from "@/components/StatusStrip";
import { metadata } from "@/app/layout";
import { cycleFindings, formatPct, usSessionHours } from "@/lib/format";
import type { IdeasList, Me, Status } from "@/lib/types";
import nextConfig from "../next.config";

const MOCKS = join(__dirname, "..", "contract", "mocks");
const load = <T>(file: string): T => JSON.parse(readFileSync(join(MOCKS, file), "utf8")) as T;
/** The text a reader sees: tags dropped (inline ones join their words), entities decoded. */
const text = (html: string) =>
  html
    .replace(/<\/(p|div|dt|dd|li|h1|h2|span class="block[^"]*")>|<br\s*\/?>/g, " ")
    .replace(/<[^>]+>/g, "")
    .replace(/&#x27;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/&amp;/g, "&")
    .replace(/\s+/g, " ");

const list = load<IdeasList>("ideas.json");
const me = load<Me>("me.json");
const status = load<Status>("status.json");
const now = Date.parse(list.generated_at);

describe("the idea list", () => {
  const html = renderToStaticMarkup(
    createElement(IdeaList, { ideas: list.ideas, now, timeZone: "Europe/Athens", userCurrency: "EUR" }),
  );

  it("labels the price as the one in the report and the percentages as from the entry", () => {
    expect(text(html)).toContain("Reported price");
    expect(text(html)).toContain("Entry to target");
    expect(text(html)).not.toContain("To target");
  });

  it("shows the entry's figures, what the two limit orders would make or lose", () => {
    const idea = list.ideas[0];
    expect(idea.entry_upside_pct).not.toBeCloseTo(idea.upside_pct); // the two readings differ
    expect(html).toContain(formatPct(idea.entry_upside_pct));
    expect(html).toContain(formatPct(idea.entry_downside_pct));
    expect(html).not.toContain(formatPct(idea.upside_pct));
  });
});

describe("the status strip", () => {
  it("says a due cycle is starting, not that it ran 'just now'", () => {
    const due = { ...status, next_cycle_at: new Date(now - 20_000).toISOString(), cycle_running_since: null };
    const html = text(renderToStaticMarkup(createElement(StatusStrip, { status: due, now, timeZone: "Europe/Athens" })));
    expect(html).toContain("Next cycle starting");
    expect(html).not.toContain("just now");
  });

  it("says when the next one is due", () => {
    const later = { ...status, next_cycle_at: new Date(now + 4 * 60_000).toISOString() };
    const html = text(renderToStaticMarkup(createElement(StatusStrip, { status: later, now, timeZone: "UTC" })));
    expect(html).toContain("Next cycle in 4 min");
  });
});

describe("the dashboard with no ideas", () => {
  const render = (monthHasIdeas: boolean, overrides: Partial<Status> = {}) =>
    text(
      renderToStaticMarkup(
        createElement(NoIdeas, {
          days: 7,
          monthHasIdeas,
          status: { ...status, ...overrides },
          now,
          timeZone: "Europe/Athens",
        }),
      ),
    );

  it("offers the last 30 days only when they had ideas", () => {
    expect(render(true)).toContain("Show the last 30 days");
    expect(render(false)).not.toContain("Show the last 30 days");
  });

  it("on a new site, says when ideas appear and what the last cycle found", () => {
    const first = render(false);
    expect(first).toContain("falls sharply during its trading session");
    expect(first).toContain("For US stocks that is 16:30–23:00 EEST");
    expect(first).toContain("Last cycle 2 min ago: 14 new articles, 3 companies, 1 candidate, 1 opportunity, 0 alerts.");
  });

  it("says a cycle is running, or that the first one hasn't finished", () => {
    expect(render(false, { cycle_running_since: new Date(now - 60_000).toISOString() })).toContain(
      "A cycle is running now (it started 1 min ago).",
    );
    expect(render(false, { last_cycle: null, cycle_running_since: null })).toContain(
      "The first cycle hasn't finished yet",
    );
  });

  it("reads the US session in the reader's time zone, summer and winter", () => {
    expect(usSessionHours(Date.parse("2026-09-28T09:00:00Z"), "Europe/Athens")).toBe("16:30–23:00 EEST");
    expect(usSessionHours(Date.parse("2026-12-01T09:00:00Z"), "Europe/Athens")).toBe("16:30–23:00 EET");
    // the weeks when New York has changed its clocks and Europe hasn't yet
    expect(usSessionHours(Date.parse("2026-03-10T12:00:00Z"), "Europe/Athens")).toBe("15:30–22:00 EET");
    expect(usSessionHours(Date.parse("2026-09-28T09:00:00Z"), "America/New_York")).toBe("09:30–16:00 EDT");
  });

  it("keeps a cycle summary's findings only", () => {
    expect(cycleFindings("Cycle 2026-09-25 18:00 EEST: 19/20 feeds ok, 37 new articles, 0 alerts; took 41 s; 12 calls")).toBe(
      "19/20 feeds ok, 37 new articles, 0 alerts",
    );
    expect(cycleFindings("Cycle 06:10: 14 new articles, 0 alerts")).toBe("14 new articles, 0 alerts");
    expect(cycleFindings("Cycle 2026-09-25 18:00 EEST failed: feeds down")).toBe(
      "Cycle 2026-09-25 18:00 EEST failed: feeds down",
    );
  });
});

describe("the React 404", () => {
  it("keeps the header's links and menu for a signed-in visitor", () => {
    const html = renderToStaticMarkup(createElement(NotFoundView, { me }));
    for (const href of ['href="/news"', 'href="/settings"', 'href="/track"', 'action="/logout"']) {
      expect(html).toContain(href);
    }
    expect(text(html)).toContain("Page not found");
  });

  it("has no links a stranger can't use", () => {
    const html = renderToStaticMarkup(createElement(NotFoundView, { me: null }));
    expect(html).not.toContain('href="/settings"');
    expect(text(html)).toContain("Back to the ideas");
  });
});

describe("the home-screen icon", () => {
  it("is a 180x180 PNG in public/, declared by the layout", () => {
    const png = readFileSync(join(__dirname, "..", "public", "apple-touch-icon.png"));
    expect(png.subarray(0, 8).toString("hex")).toBe("89504e470d0a1a0a");
    expect([png.readUInt32BE(16), png.readUInt32BE(20)]).toEqual([180, 180]); // IHDR width, height
    expect(png[25]).toBe(2); // colour type RGB: opaque, as iOS wants
    expect(JSON.stringify(metadata.icons)).toContain('"/apple-touch-icon.png"');
  });

  it("answers Safari's other name for it here, not with a 404 from Fly", async () => {
    const rewrites = await nextConfig.rewrites!();
    expect(rewrites).toEqual([{ source: "/apple-touch-icon-precomposed.png", destination: "/apple-touch-icon.png" }]);
  });
});
