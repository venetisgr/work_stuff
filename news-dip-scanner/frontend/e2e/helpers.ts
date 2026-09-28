/** Shared by the end-to-end tests: the settings, signing in, and a watch on everything a page must not do. */
import { expect, type BrowserContext, type Page } from "@playwright/test";

export const settings = {
  baseURL: (process.env.E2E_BASE_URL ?? "http://localhost:3000").replace(/\/$/, ""),
  flyOrigin: (process.env.E2E_FLY_ORIGIN ?? "http://127.0.0.1:8080").replace(/\/$/, ""),
  email: process.env.E2E_EMAIL ?? "",
  password: process.env.E2E_PASSWORD ?? "",
  shots: process.env.E2E_SHOTS ?? "",
};

const front = new URL(settings.baseURL);
const fly = new URL(settings.flyOrigin);

/** Sign in on the Fly app's sign-in page, through the proxy, and land on the React ideas page. */
export async function signIn(page: Page, next = "/") {
  await page.goto(`/login?next=${encodeURIComponent(next)}`);
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  await page.getByLabel("Email").fill(settings.email);
  await page.getByLabel("Password").fill(settings.password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL((url) => url.pathname === next.split("?")[0]);
}

/**
 * Collects what no page may do: console errors and warnings, uncaught exceptions, Content-Security-Policy
 * violations, requests that leave the front end's origin for the Fly app, and redirects that point anywhere but
 * the front end (a Location on the Fly host would bounce visitors to *.fly.dev).
 */
export class Watch {
  readonly problems: string[] = [];

  static async on(context: BrowserContext): Promise<Watch> {
    const watch = new Watch();
    await context.exposeBinding("__e2eCsp", (_source, text: string) => {
      watch.problems.push(`CSP violation: ${text}`);
    });
    await context.addInitScript(() => {
      document.addEventListener("securitypolicyviolation", (event) => {
        const report = (window as unknown as { __e2eCsp?: (text: string) => void }).__e2eCsp;
        report?.(`${event.violatedDirective} blocked ${event.blockedURI || "inline"} on ${location.pathname}`);
      });
    });
    context.on("page", (page) => watch.page(page));
    for (const page of context.pages()) watch.page(page);
    return watch;
  }

  private page(page: Page) {
    page.on("console", (message) => {
      if (message.type() === "error" || message.type() === "warning") {
        this.problems.push(`console ${message.type()} on ${new URL(page.url()).pathname}: ${message.text()}`);
      }
    });
    page.on("pageerror", (error) => this.problems.push(`page error on ${page.url()}: ${error.message}`));
    page.on("request", (request) => {
      const url = new URL(request.url());
      if (url.host === fly.host) this.problems.push(`request straight to the Fly app: ${request.url()}`);
    });
    page.on("response", (response) => {
      const location = response.headers()["location"];
      if (!location) return;
      const target = new URL(location, response.url());
      if (target.host !== front.host) {
        this.problems.push(`redirect away from the front end: ${response.url()} -> ${location}`);
      }
      if (location.includes(fly.host) || /\.fly\.dev/.test(location)) {
        this.problems.push(`redirect to the Fly app: ${response.url()} -> ${location}`);
      }
    });
  }

  /** Fails the test if anything was collected; allowed filters out expected messages (e.g. a 404's console line). */
  expectClean(allowed: RegExp[] = []) {
    const left = this.problems.filter((problem) => !allowed.some((pattern) => pattern.test(problem)));
    expect(left, left.join("\n")).toEqual([]);
  }
}
