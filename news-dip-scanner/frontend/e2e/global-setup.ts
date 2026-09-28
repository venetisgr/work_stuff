/** Before the end-to-end tests: both servers must answer, or the run stops at once with what to start. */
import { settings } from "./helpers";

async function reachable(url: string): Promise<number | null> {
  try {
    const response = await fetch(url, { redirect: "manual", signal: AbortSignal.timeout(5000) });
    return response.status;
  } catch {
    return null;
  }
}

export default async function globalSetup() {
  const front = await reachable(`${settings.baseURL}/healthz`);
  const fly = await reachable(`${settings.flyOrigin}/healthz`);
  const missing = [];
  if (fly !== 200) {
    missing.push(
      `the Fly app at ${settings.flyOrigin} (dip-scanner serve --no-scanner --port 8080, with PROXY_SECRET, ` +
        "BASE_URL=http://localhost:3000 and COOKIE_SECURE=false)",
    );
  }
  if (front !== 200) {
    missing.push(
      `the front end at ${settings.baseURL} (npm run build, then DIP_API_ORIGIN=${settings.flyOrigin} ` +
        "DIP_PROXY_SECRET=<the same secret> npm start)",
    );
  }
  if (missing.length) throw new Error(`Start ${missing.join(" and ")} first; see e2e/README.md.`);
  if (!settings.email || !settings.password) {
    throw new Error("Set E2E_EMAIL and E2E_PASSWORD to an admin account of the Fly app's database.");
  }
}
