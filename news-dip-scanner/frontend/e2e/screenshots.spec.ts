/**
 * Screenshots of every page through the front door, React and Fly alike, on a phone (390×844) and a desktop
 * (1280×800), light and dark, so the two halves can be compared side by side. Only with E2E_SHOTS=<folder>. Each page
 * is also checked for sideways scrolling, and the run for console errors and CSP violations. The stock whose price is
 * the longest to write (272,750.00 KRW beats $12.71) gets its idea and ticker pages taken too, since a long price is
 * what pushes a phone's layout sideways.
 */
import { mkdirSync } from "node:fs";
import { join } from "node:path";
import { expect, test } from "@playwright/test";
import { formatPrice } from "../src/lib/format";
import type { IdeasList } from "../src/lib/types";
import { settings, signIn, Watch } from "./helpers";

test.skip(!settings.shots, "Set E2E_SHOTS to a folder to take the screenshots.");
test.describe.configure({ mode: "serial" });

const VIEWPORTS = [
  { width: 390, height: 844 },
  { width: 1280, height: 800 },
] as const;
const SCHEMES = ["light", "dark"] as const;

/** The idea pages worth a picture: one per kind of debate on the database, and one without. */
async function ideaPages(request: import("@playwright/test").APIRequestContext): Promise<[string, string][]> {
  const list = (await (await request.get("/api/v1/ideas?days=30")).json()) as IdeasList;
  const pages: [string, string][] = [];
  for (const mode of ["debate", "agreed", "single"] as const) {
    const idea = list.ideas.find((item) => item.debate?.mode === mode);
    if (idea) pages.push([`idea-${mode}`, `/ideas/${idea.id}`]);
  }
  const plain = list.ideas.find((item) => item.debate === null);
  if (plain) pages.push(["idea-plain", `/ideas/${plain.id}`]);
  const written = (item: IdeasList["ideas"][number]) => formatPrice(item.price.amount, item.currency).length;
  const longest = [...list.ideas].sort((a, b) => written(b) - written(a))[0];
  if (longest) {
    pages.push(["idea-long-price", `/ideas/${longest.id}`]);
    pages.push(["ticker-long-price", `/tickers/${encodeURIComponent(longest.ticker)}`]);
  }
  return pages;
}

for (const viewport of VIEWPORTS) {
  for (const scheme of SCHEMES) {
    test(`every page at ${viewport.width} wide, ${scheme}`, async ({ browser }) => {
      test.setTimeout(240_000);
      mkdirSync(settings.shots, { recursive: true });
      const context = await browser.newContext({ viewport, colorScheme: scheme, deviceScaleFactor: 1 });
      const watch = await Watch.on(context);
      const page = await context.newPage();
      const shoot = async (name: string) => {
        await page.waitForLoadState("networkidle");
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
        expect(overflow, `${name} scrolls sideways at ${viewport.width}px`).toBeLessThanOrEqual(0);
        await page.screenshot({
          path: join(settings.shots, `${name}-${viewport.width}-${scheme}.png`),
          fullPage: true,
          animations: "disabled",
        });
      };

      await page.goto("/login");
      await shoot("login");
      await signIn(page);
      const pages: [string, string][] = [
        ["dashboard", "/"],
        ...(await ideaPages(page.request)),
        ["ticker", "/tickers/F"],
        ["news", "/news"],
        ["track", "/track?days=730"],
        ["settings", "/settings"],
        ["admin", "/admin"],
        ["admin-users", "/admin/users"],
        ["admin-invites", "/admin/invites"],
      ];
      for (const [name, path] of pages) {
        await page.goto(path);
        await shoot(name);
      }
      watch.expectClean();
      await context.close();
    });
  }
}
