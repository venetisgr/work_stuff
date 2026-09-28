#!/usr/bin/env node
/**
 * A stand-in for the Fly app, for working on the React pages without the Python backend: the JSON API under
 * /api/v1 from contract/mocks/*.json (the same fixtures the contract test validates), with a fake session, and a
 * few plain HTML pages where the Fly app would answer (sign-in, sign-out, the "Analyse now" form...).
 *
 *   node scripts/mock-api.mjs            listens on 127.0.0.1:8787 (MOCK_PORT)
 *   npm run dev:mock                     this server plus `next dev` pointed at it (scripts/dev-mock.mjs)
 *
 * Like the real app with PROXY_SECRET set, every request but /healthz needs x-dip-proxy-secret (DIP_PROXY_SECRET,
 * default MOCK_SECRET below), so the proxy's headers are exercised too. Sign in with any email and password.
 * MOCK_ROLE=member serves me-member.json; MOCK_STATUS=stopped serves status-stopped.json; MOCK_JOB=fail makes
 * "Analyse again" fail; MOCK_IDEAS=none lists no ideas at all (a new site's dashboard). Only for development: it
 * binds to 127.0.0.1 and holds no real data.
 */
import { randomBytes, timingSafeEqual } from "node:crypto";
import { readFileSync, readdirSync } from "node:fs";
import http from "node:http";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

export const MOCK_SECRET = "mock-proxy-secret-for-local-development-only";
const HERE = dirname(fileURLToPath(import.meta.url));
const MOCKS = join(HERE, "..", "contract", "mocks");
const PORT = Number(process.env.MOCK_PORT || 8787);
const SECRET = process.env.DIP_PROXY_SECRET || MOCK_SECRET;
const PAGE_SIZE = 25;
const SESSIONS = new Set(["mock-session-for-screenshots-0000000000000000"]);
const jobs = new Map(); // job id -> { started, ideaId, ticker }
let nextJob = 41;

const fixtures = Object.fromEntries(
  readdirSync(MOCKS)
    .filter((name) => name.endsWith(".json"))
    .map((name) => [name.replace(/\.json$/, ""), JSON.parse(readFileSync(join(MOCKS, name), "utf8"))]),
);
const fixture = (name) => structuredClone(fixtures[name]);

function send(res, status, body, headers = {}) {
  const text = typeof body === "string" ? body : JSON.stringify(body, null, 1);
  const type = typeof body === "string" ? "text/html; charset=utf-8" : "application/json";
  res.writeHead(status, { "content-type": type, "cache-control": "no-store", ...headers });
  res.end(text);
}

const apiError = (res, status, code, message, retryAfter = null) =>
  send(res, status, { error: { code, message, retry_after: retryAfter } });

function cookies(req) {
  return Object.fromEntries(
    (req.headers.cookie || "")
      .split(";")
      .map((part) => part.trim().split("="))
      .filter(([name]) => name)
      .map(([name, ...rest]) => [name, rest.join("=")]),
  );
}

function secretOk(req) {
  const given = Buffer.from(String(req.headers["x-dip-proxy-secret"] || ""));
  const wanted = Buffer.from(SECRET);
  return given.length === wanted.length && timingSafeEqual(given, wanted);
}

function page(title, body) {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>${title} · Dip scanner (mock)</title></head>
<body style="font-family:system-ui,sans-serif;max-width:40rem;margin:2rem auto;padding:0 1rem;line-height:1.5">
<p style="color:#8a5a00;background:#fff4d6;padding:.5rem .75rem;border-radius:8px">Mock of the Fly app (scripts/mock-api.mjs): in production the Fly app serves this page.</p>
<h1>${title}</h1>${body}<p><a href="/">Back to the ideas</a></p></body></html>`;
}

function readBody(req) {
  return new Promise((resolve) => {
    let data = "";
    req.on("data", (chunk) => (data += chunk));
    req.on("end", () => resolve(data));
  });
}

function me() {
  return fixture(process.env.MOCK_ROLE === "member" ? "me-member" : "me");
}

/** GET /api/v1/ideas: the pool in ideas.json, filtered like the real API. */
function ideas(url) {
  const pool = fixture("ideas");
  const q = url.searchParams;
  const now = Date.parse(pool.generated_at);
  const days = [1, 3, 7, 30].includes(Number(q.get("days"))) ? Number(q.get("days")) : 7;
  const minScore = [50, 65, 80].includes(Number(q.get("min_score"))) ? Number(q.get("min_score")) : null;
  const verdicts = ["temporary_fear", "mixed", "fundamental", "unclear"];
  const verdict = verdicts.includes(q.get("verdict")) ? q.get("verdict") : null;
  const flag = (name) => ["1", "on", "true", "yes"].includes((q.get(name) || "").toLowerCase());
  const sort = q.get("sort") === "new" ? "new" : "score";
  const all = process.env.MOCK_IDEAS === "none" ? [] : pool.ideas;
  let list = all.filter((idea) => now - Date.parse(idea.created) <= days * 86_400_000);
  const total = list.length;
  if (minScore !== null) list = list.filter((idea) => idea.score >= minScore);
  if (verdict) list = list.filter((idea) => idea.verdict === verdict);
  if (flag("watchlist")) list = list.filter((idea) => idea.on_my_watchlist);
  if (flag("matching")) list = list.filter((idea) => idea.matches_my_rules);
  list.sort((a, b) => (sort === "score" ? b.score - a.score : Date.parse(b.created) - Date.parse(a.created)));
  const pages = Math.max(1, Math.ceil(list.length / PAGE_SIZE));
  const number = Math.min(pages, Math.max(1, Number(q.get("page")) || 1));
  return {
    generated_at: pool.generated_at,
    filters: { days, min_score: minScore, verdict, watchlist: flag("watchlist"), matching: flag("matching"), sort },
    total,
    count: list.length,
    page: { number, pages, size: PAGE_SIZE },
    ideas: list.slice((number - 1) * PAGE_SIZE, number * PAGE_SIZE),
  };
}

/** GET /api/v1/jobs/<id>: queued for 3 seconds, running until 9, then done (or failed with MOCK_JOB=fail). */
function job(id) {
  const entry = jobs.get(id);
  if (!entry) return null;
  const age = Date.now() - entry.started;
  const base = { ...fixture("job-queued"), id, ticker: entry.ticker, created: new Date(entry.started).toISOString() };
  if (age < 3000) return base;
  if (age < 9000) return { ...fixture("job-running"), ...base, status: "running", ahead: null };
  const finished = new Date(entry.started + 9000).toISOString();
  if (process.env.MOCK_JOB === "fail") return { ...fixture("job-failed"), ...base, status: "failed", ahead: null, finished };
  return { ...fixture("job-done"), ...base, status: "done", ahead: null, finished, opportunity_id: entry.ideaId };
}

async function api(req, res, url, signedIn) {
  const path = url.pathname.replace(/^\/api\/v1/, "");
  if (!signedIn) return apiError(res, 401, "not_signed_in", "Sign in to continue.");
  if (req.method === "GET" && path === "/me") return send(res, 200, me());
  if (req.method === "GET" && path === "/status") {
    return send(res, 200, fixture(process.env.MOCK_STATUS === "stopped" ? "status-stopped" : "status"));
  }
  if (req.method === "GET" && path === "/ideas") return send(res, 200, ideas(url));
  if (req.method === "GET" && path === "/thesis-changes") return send(res, 200, fixture("thesis-changes"));
  let match = path.match(/^\/ideas\/(\d+)$/);
  if (req.method === "GET" && match) {
    const detail = fixtures[`idea-${match[1]}`];
    return detail ? send(res, 200, structuredClone(detail)) : apiError(res, 404, "not_found", "There is no idea with that number. It may have been removed.");
  }
  match = path.match(/^\/ideas\/(\d+)\/reanalyse$/);
  if (match) {
    if (req.method !== "POST") return apiError(res, 405, "method_not_allowed", "Use POST.");
    if (req.headers["x-csrf-token"] !== me().csrf) {
      return apiError(res, 403, "csrf", "This page has expired. Reload it and try again.");
    }
    const detail = fixtures[`idea-${match[1]}`];
    if (!detail) return apiError(res, 404, "not_found", "There is no idea with that number.");
    if (!me().capabilities.analyse.available) {
      return apiError(res, 429, "limit_reached", me().capabilities.analyse.note, 3600);
    }
    const id = nextJob++;
    jobs.set(id, { started: Date.now(), ideaId: Number(match[1]), ticker: detail.idea.ticker });
    return send(res, 202, { job_id: id, status: "queued", ticker: detail.idea.ticker });
  }
  match = path.match(/^\/jobs\/(\d+)$/);
  if (req.method === "GET" && match) {
    const found = job(Number(match[1]));
    return found ? send(res, 200, found) : apiError(res, 404, "not_found", "There is no such analysis.");
  }
  return apiError(res, 404, "not_found", "Not found.");
}

async function pages(req, res, url, signedIn) {
  const next = url.searchParams.get("next") || "/";
  const safeNext = next.startsWith("/") && !next.startsWith("//") ? next : "/";
  if (url.pathname === "/login" && req.method === "GET") {
    return send(
      res,
      200,
      page(
        "Sign in",
        `<form method="post" action="/login?next=${encodeURIComponent(safeNext)}">
<p><label>Email<br><input name="email" type="email" required autocomplete="username"></label></p>
<p><label>Password<br><input name="password" type="password" required autocomplete="current-password"></label></p>
<p><button type="submit">Sign in</button></p></form>`,
      ),
    );
  }
  if (url.pathname === "/login" && req.method === "POST") {
    const form = new URLSearchParams(await readBody(req));
    if (!form.get("email") || !form.get("password")) return send(res, 400, page("Sign in", "<p>Fill in both fields.</p>"));
    const token = randomBytes(32).toString("base64url");
    SESSIONS.add(token);
    return send(res, 303, "", {
      location: safeNext,
      "set-cookie": [
        `dsid=${token}; HttpOnly; Max-Age=2592000; Path=/; SameSite=lax`,
        "dip_form=; Max-Age=0; Path=/; SameSite=lax",
      ],
    });
  }
  if (url.pathname === "/logout" && req.method === "POST") {
    const form = new URLSearchParams(await readBody(req));
    if (form.get("csrf_token") !== me().csrf) return send(res, 403, page("Not allowed", "<p>Reload the page and try again.</p>"));
    SESSIONS.delete(cookies(req).dsid);
    return send(res, 303, "", { location: "/login", "set-cookie": "dsid=; Max-Age=0; Path=/; SameSite=lax" });
  }
  if (!signedIn) return send(res, 303, "", { location: `/login?next=${encodeURIComponent(url.pathname + url.search)}` });
  if (url.pathname === "/analyze" && req.method === "POST") {
    const form = new URLSearchParams(await readBody(req));
    const id = nextJob++;
    const ideaId = Number((form.get("next") || "").split("/").pop()) || 7;
    jobs.set(id, { started: Date.now(), ideaId, ticker: form.get("ticker") || "META" });
    return send(res, 303, "", { location: `/jobs/${id}` });
  }
  const watch = url.pathname.match(/^\/tickers\/([^/]+)\/watchlist$/);
  if (watch && req.method === "POST") {
    const form = new URLSearchParams(await readBody(req));
    return send(res, 303, "", { location: form.get("next") || "/" });
  }
  const jobPage = url.pathname.match(/^\/jobs\/(\d+)$/);
  if (jobPage) {
    const found = job(Number(jobPage[1]));
    if (found?.status === "done") return send(res, 303, "", { location: `/ideas/${found.opportunity_id}` });
    return send(res, 200, page("Analysis", `<p>${found ? found.status : "No such job"}…</p>`), { refresh: "3" });
  }
  const titles = { "/news": "News", "/track": "Track record", "/settings": "Settings", "/admin": "Admin" };
  if (titles[url.pathname] || url.pathname.startsWith("/tickers/")) {
    return send(res, 200, page(titles[url.pathname] || decodeURIComponent(url.pathname.split("/")[2] || "Stock"), "<p>…</p>"));
  }
  return send(res, 404, page("Page not found", "<p>There is no such page.</p>"));
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host || "localhost"}`);
  if (url.pathname === "/healthz") return send(res, 200, { status: "ok", db: "ok", scanner: "running", last_cycle: null });
  if (!secretOk(req)) {
    return send(res, 403, page("Not here", "<p>This address only answers through the site's front door.</p>"));
  }
  const signedIn = SESSIONS.has(cookies(req).dsid);
  try {
    if (url.pathname.startsWith("/api/v1/")) return await api(req, res, url, signedIn);
    return await pages(req, res, url, signedIn);
  } catch (error) {
    console.error(error);
    return apiError(res, 500, "server_error", "Something went wrong.");
  }
});

server.listen(PORT, "127.0.0.1", () => {
  console.log(`Mock Fly app on http://127.0.0.1:${PORT} (proxy secret ${SECRET === MOCK_SECRET ? "the built-in mock one" : "from DIP_PROXY_SECRET"})`);
});
