/**
 * When the Fly app can't be reached (a deploy restarting its Machine, a crash), every page still answers something a
 * phone can read: the proxied pages the front door's own small page, the React pages their error card, the API its
 * JSON error. This spec starts a second `next start` of the same build whose DIP_API_ORIGIN is a port nothing listens
 * on (so the Fly app of the other specs keeps running).
 */
import { spawn, type ChildProcess } from "node:child_process";
import { createServer } from "node:net";
import { join } from "node:path";
import { devices, expect, test } from "@playwright/test";
import { Watch, settings } from "./helpers";

// An iPhone's screen in the Chromium the other specs use (the preset's own browser would be WebKit).
const { viewport, userAgent, deviceScaleFactor, isMobile, hasTouch } = devices["iPhone 13"];
const iPhone = { viewport, userAgent, deviceScaleFactor, isMobile, hasTouch };

let server: ChildProcess | null = null;
let base = "";

function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const probe = createServer();
    probe.once("error", reject);
    probe.listen(0, "127.0.0.1", () => {
      const address = probe.address();
      const port = typeof address === "object" && address ? address.port : 0;
      probe.close(() => resolve(port));
    });
  });
}

test.beforeAll(async () => {
  const [port, closed] = [await freePort(), await freePort()];
  base = `http://127.0.0.1:${port}`;
  server = spawn(process.execPath, [join(__dirname, "..", "node_modules", "next", "dist", "bin", "next"), "start"], {
    cwd: join(__dirname, ".."),
    env: {
      ...process.env,
      PORT: String(port),
      HOSTNAME: "127.0.0.1",
      DIP_API_ORIGIN: `http://127.0.0.1:${closed}`,
      DIP_PROXY_SECRET: "fly-is-down-in-this-test-0123456789abcdef",
    },
    stdio: "ignore",
  });
  for (let i = 0; i < 120; i++) {
    try {
      if ((await fetch(`${base}/icon.svg`)).ok) return; // one of Next.js's own files: no call to Fly
    } catch {
      // not listening yet
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`next start didn't answer on ${base}`);
});

test.afterAll(() => {
  server?.kill();
});

test.describe("with the Fly app down, on a phone", () => {
  test.use({ ...iPhone });

  for (const colorScheme of ["light", "dark"] as const) {
    test(`a proxied page is a readable page with a way back (${colorScheme})`, async ({ page, context }) => {
      const watch = await Watch.on(context);
      await page.emulateMedia({ colorScheme });
      for (const path of ["/login?next=%2F", "/settings", "/"]) {
        const response = await page.goto(`${base}${path}`);
        expect(response?.status(), path).toBe(502);
        expect(await page.evaluate(() => window.innerWidth), path).toBe(390); // the viewport meta: not 980 wide
        await expect(page.getByRole("heading", { name: "The scanner's server can't be reached just now" })).toBeVisible();
        const again = page.getByRole("link", { name: "Try again" });
        await expect(again).toBeVisible();
        // "/" without a session goes to the sign-in page (Fly's), which is the one that can't be reached
        expect(await again.getAttribute("href")).toBe(path === "/" ? "/login?next=%2F" : path);
        const size = await page.locator("p", { hasText: "Try again in a moment" }).evaluate((p) => getComputedStyle(p).fontSize);
        expect(parseFloat(size)).toBeGreaterThanOrEqual(16);
        const scroll = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
        expect(scroll).toBeLessThanOrEqual(0);
        if (settings.shots) {
          await page.screenshot({ path: join(settings.shots, `fly-down${path.split("?")[0].replace(/\//g, "-")}-390-${colorScheme}.png`) });
        }
      }
      // the only console lines are the browser's own notes about the 502s; no CSP violation, no script error (and
      // this second server's own relative redirect to /login isn't one "away from the front end")
      watch.expectClean([/status of 502/, /^redirect away from the front end: http:\/\/127\.0\.0\.1:\d+\/ -> \/login/]);
    });
  }

  test("a React page with a session shows its own card and a Try again button", async ({ page, context }) => {
    await context.addCookies([{ name: "dsid", value: "a".repeat(43), url: base }]);
    const response = await page.goto(`${base}/`);
    expect(response?.status()).toBe(200);
    await expect(page.getByRole("alert").filter({ hasText: "The ideas couldn't be loaded" })).toContainText(
      "The scanner's server can't be reached just now",
    );
    await expect(page.getByRole("link", { name: "Try again" })).toBeVisible();
  });

  test("the JSON API answers the contract's error", async ({ request }) => {
    const response = await request.get(`${base}/api/v1/me`);
    expect(response.status()).toBe(502);
    expect(response.headers()["retry-after"]).toBe("30");
    const body = await response.json();
    expect(body.error.code).toBe("unavailable");
    expect(body.error.retry_after).toBe(30);
  });
});
