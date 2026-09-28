/**
 * End-to-end tests (npm run e2e): a real browser against `next start` in front of a real Fly app, through the proxy,
 * as on Vercel. They need two servers, which they don't start themselves (see e2e/README.md):
 *
 * 1. the Fly app: `dip-scanner serve --no-scanner --port 8080` on a database with a few ideas and an account, with
 *    PROXY_SECRET=<secret>, BASE_URL=http://localhost:3000, COOKIE_SECURE=false and, for "Analyse again", the
 *    stand-in models of e2e/fake-models.mjs (LLM_ANALYSIS_MODE=debate, OPENAI_BASE_URL, ANTHROPIC_BASE_URL);
 * 2. this app: `npm run build`, then `DIP_API_ORIGIN=http://127.0.0.1:8080 DIP_PROXY_SECRET=<secret> npm start`.
 *
 * Settings (environment): E2E_BASE_URL (http://localhost:3000), E2E_FLY_ORIGIN (http://127.0.0.1:8080), E2E_EMAIL
 * and E2E_PASSWORD (an admin account on that database), E2E_SHOTS (a folder: also take the screenshots of every
 * page, 390 and 1280 wide, light and dark), E2E_CHROMIUM (a Chromium to launch instead of Playwright's own).
 */
import { defineConfig, devices } from "@playwright/test";

const chromium = process.env.E2E_CHROMIUM;

export default defineConfig({
  testDir: "e2e",
  // One Fly database and one account for every test: run them one after the other.
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: 0,
  timeout: 120_000,
  expect: { timeout: 15_000 },
  reporter: [["list"]],
  globalSetup: "./e2e/global-setup.ts",
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3000",
    trace: "retain-on-failure",
    ...devices["Desktop Chrome"],
    launchOptions: chromium ? { executablePath: chromium } : {},
  },
});
