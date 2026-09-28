/**
 * One visit through the front door, as on Vercel: sign in on the Fly app's page, read the React ideas and an idea
 * with its debate and chart, analyse it again (the stand-in models run a whole debate), save the Fly settings page,
 * look at the other Fly pages and sign out. No page may log an error, break its Content-Security-Policy, talk to the
 * Fly app directly or be redirected to it.
 */
import { expect, test, type Page, type Response } from "@playwright/test";
import type { IdeasList } from "../src/lib/types";
import { settings, signIn, Watch } from "./helpers";

test.describe.configure({ mode: "serial" });

/** Whether a page came from Next.js (a per-request CSP nonce) or from the Fly app (its fixed CSP). */
function servedBy(response: Response | null): "next" | "fly" {
  const csp = response?.headers()["content-security-policy"] ?? "";
  return csp.includes("'nonce-") ? "next" : "fly";
}

async function flyStylesLoaded(page: Page) {
  // The Fly page's own stylesheet came through the proxy: its header is laid out, not a bare list of links.
  const header = page.locator("header.site-header");
  await expect(header).toBeVisible();
  const display = await header.locator(".header-inner").evaluate((node) => getComputedStyle(node).display);
  expect(display).toBe("flex");
}

let watch: Watch;
let page: Page;

test.beforeAll(async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  watch = await Watch.on(context);
  page = await context.newPage();
});

test.afterAll(async () => {
  await page.context().close();
});

test("signing in on the Fly page lands on the React ideas", async () => {
  const redirected = await page.goto("/");
  expect(new URL(page.url()).pathname).toBe("/login");
  expect(new URL(page.url()).searchParams.get("next")).toBe("/");
  expect(servedBy(redirected)).toBe("fly");
  await flyStylesLoaded(page);

  await signIn(page);
  await expect(page.getByRole("heading", { level: 1, name: "Ideas" })).toBeVisible();
  const reload = await page.reload();
  expect(servedBy(reload)).toBe("next");
  await expect(page.getByText(/Scanner (running|paused|stopped|off|stalled)/)).toBeVisible();

  const cookies = await page.context().cookies();
  const session = cookies.find((cookie) => cookie.name === "dsid");
  expect(session, "the session cookie").toBeDefined();
  expect(session!.domain).toBe(new URL(settings.baseURL).hostname);
  expect(session!.httpOnly).toBe(true);
  expect(session!.sameSite).toBe("Lax");
  expect(cookies.every((cookie) => cookie.domain === new URL(settings.baseURL).hostname)).toBe(true);
});

test("the ideas filter through the URL", async () => {
  await page.goto("/?days=30");
  await expect(page.getByRole("link", { name: "30 days" })).toHaveAttribute("aria-current", "true");
  await page.goto("/?days=30&verdict=fundamental");
  await expect(page.getByText(/match the filters|No idea matches these filters/)).toBeVisible();
});

let debatedId = 0;

test("an idea with a debate and a chart, in React", async () => {
  const list = (await (await page.request.get("/api/v1/ideas?days=30")).json()) as IdeasList;
  const debated = list.ideas.find((idea) => idea.debate?.mode === "debate") ?? list.ideas[0];
  expect(debated, "an idea on the database").toBeDefined();
  debatedId = debated.id;

  await page.goto("/?days=30");
  // The list renders cards on narrow screens and a table on wide ones: click the link that is showing.
  await page.locator(`a[href="/ideas/${debatedId}"]:visible`).first().click();
  await page.waitForURL(`**/ideas/${debatedId}`);
  await expect(page.getByRole("heading", { level: 1 })).toContainText(debated.ticker);

  if (debated.debate?.mode === "debate") {
    const card = page.locator("section[aria-labelledby=debate-title]");
    await expect(card.getByRole("heading", { name: "The debate" })).toBeVisible();
    for (const side of debated.debate.participants) {
      await expect(card.getByRole("article", { name: side.model_label })).toBeVisible();
    }
  }

  // The chart: hovering shows the day and the close, the keyboard moves along the closes.
  const chart = page.locator("section[aria-labelledby=chart-title] svg[role=img]");
  await chart.scrollIntoViewIfNeeded();
  const box = (await chart.boundingBox())!;
  await page.mouse.move(box.x + box.width * 0.4, box.y + box.height * 0.5);
  await expect(chart.locator(".chart-tip-value")).toHaveText(/\d/);
  await expect(chart.locator(".chart-tip-day").first()).toHaveText(/\d{4}/);
  await chart.focus();
  await page.keyboard.press("End");
  const last = await chart.locator(".chart-tip-day").first().textContent();
  await page.keyboard.press("ArrowLeft");
  await expect(chart.locator(".chart-tip-day").first()).not.toHaveText(last ?? "");
});

test("Analyse again runs a new debate and opens it", async () => {
  test.setTimeout(180_000);
  await page.goto(`/ideas/${debatedId}`);
  const button = page.getByRole("button", { name: "Analyse again now" });
  const posted = page.waitForResponse((response) => response.url().endsWith(`/api/v1/ideas/${debatedId}/reanalyse`));
  await button.click();
  const accepted = await posted;
  expect(accepted.status()).toBe(202);
  expect(accepted.request().headers()["x-csrf-token"]).toBeTruthy();
  await expect(page.locator("#analyse-status")).toContainText(/Waiting to start|Analysing now|Done/);

  await page.waitForURL((url) => /^\/ideas\/\d+$/.test(url.pathname) && url.pathname !== `/ideas/${debatedId}`, {
    timeout: 150_000,
  });
  await expect(page.getByRole("heading", { name: "The debate" })).toBeVisible();
  await expect(page.getByText(/The ruling by|Merged by rule|The merged analysis/)).toBeVisible();
  await expect(page.getByText("Analysed just now").or(page.getByText(/Analysed \d+ (s|min) ago/))).toBeVisible();
});

test("the Fly settings page saves, and refuses a webhook on a private network", async () => {
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Settings" }).click();
  await page.waitForURL("**/settings");
  await flyStylesLoaded(page);
  const webhook = page.getByLabel("Webhook address");
  await webhook.fill("https://127.0.0.1/hooks/alerts");
  const refused = page.waitForResponse((response) => response.url().endsWith("/settings") && response.request().method() === "POST");
  await page.getByRole("button", { name: "Save settings" }).click();
  expect((await refused).status()).toBe(400);
  await expect(page.getByText("The address must be on the public internet")).toBeVisible();

  await page.getByLabel("Webhook address").fill("");
  const name = page.getByLabel("Your name");
  const before = await name.inputValue();
  await name.fill("E2E Tester");
  await page.getByRole("button", { name: "Save settings" }).click();
  await page.waitForURL("**/settings");
  await expect(page.getByText("Settings saved.")).toBeVisible();
  await expect(page.locator("details.menu summary")).toContainText("E2E Tester");
  // and the React pages show it too
  await page.goto("/");
  await expect(page.locator("details.menu summary")).toContainText("E2E Tester");
  await page.goto("/settings");
  await page.getByLabel("Your name").fill(before);
  await page.getByRole("button", { name: "Save settings" }).click();
  await expect(page.getByText("Settings saved.")).toBeVisible();
});

test("the other Fly pages work through the proxy", async () => {
  const nav = page.getByRole("navigation", { name: "Main" });
  for (const [name, path, heading] of [
    ["News", "/news", "News"],
    ["Track record", "/track", "Track record"],
    ["Admin", "/admin", "Admin"],
  ] as const) {
    await nav.getByRole("link", { name }).click();
    await page.waitForURL(`**${path}`);
    await expect(page.getByRole("heading", { level: 1 })).toContainText(heading);
    await flyStylesLoaded(page);
  }
  await page.goto("/tickers/F");
  await expect(page.getByRole("heading", { level: 1 })).toContainText("F");
  // Back to React through the Fly page's own link
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Ideas" }).click();
  await page.waitForURL((url) => url.pathname === "/");
  await expect(page.getByRole("heading", { level: 1, name: "Ideas" })).toBeVisible();
});

test("signing out from a React page ends the session", async () => {
  await page.goto("/");
  await page.locator("details.menu summary").click();
  await page.getByRole("button", { name: "Sign out" }).click();
  await page.waitForURL("**/login**");
  const cookies = await page.context().cookies();
  expect(cookies.find((cookie) => cookie.name === "dsid")).toBeUndefined();
  await page.goto("/");
  expect(new URL(page.url()).pathname).toBe("/login");
});

test("nothing went wrong on any page", async () => {
  // The refused settings form answers 400 on purpose, which Chrome logs as a failed load.
  watch.expectClean([/^console error on \/settings: Failed to load resource: .* 400 \(Bad Request\)$/]);
});
