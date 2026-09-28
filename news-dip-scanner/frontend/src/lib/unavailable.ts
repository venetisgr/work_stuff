/**
 * What the front door answers when it can't ask the Fly app: it isn't configured (503), Fly can't be reached (502),
 * Fly took too long (504), or Fly's edge answered for a Machine that is restarting (its 502/503/504). People get a
 * small page of its own, readable on a phone and in dark mode, with a way back; the JSON API gets the contract's
 * error ({error: {code: "unavailable", message, retry_after}}), which the React code already reads. Pure (no
 * request, no environment), so the tests can call it.
 *
 * The page is self-contained: one inline stylesheet, allowed by its SHA-256 in the page's own CSP, no script, and
 * nothing fetched from Fly (the icon is this site's).
 */
import { createHash } from "node:crypto";
import { securityHeaders } from "./csp";

export const RETRY_AFTER_SECONDS = 30;

export type UnavailableKind = "not_configured" | "unreachable" | "timeout";

const STATUS: Record<UnavailableKind, 502 | 503 | 504> = { not_configured: 503, unreachable: 502, timeout: 504 };

/** The headline of each case: the texts docs/VERCEL.md's troubleshooting table quotes. */
const TITLES: Record<UnavailableKind, string> = {
  not_configured: "This site isn't set up yet",
  unreachable: "The scanner's server can't be reached just now",
  timeout: "The scanner's server took too long to answer",
};

export interface UnavailableRequest {
  method: string;
  /** The request's path and query, as the visitor sent them. */
  pathname: string;
  search: string;
  accept: string | null;
  /** The Referer header and this site's origin: a failed form post links back to the page it was sent from. */
  referer: string | null;
  origin: string;
  https: boolean;
}

/** The page's only style: the site's colours (globals.css) for light and dark, sized for a phone. */
export const UNAVAILABLE_STYLE = [
  ":root{color-scheme:light dark;--bg:#f5f6f8;--surface:#fff;--ink:#16191d;--line:#e1e5ea;",
  "--accent:#0b5cad;--accent-hover:#094b8e;--accent-ink:#fff;--focus:rgba(11,92,173,.35)}",
  "@media (prefers-color-scheme:dark){:root{--bg:#0e1115;--surface:#161a20;--ink:#e6e9ed;",
  "--line:#2a3038;--accent:#5aa2ec;--accent-hover:#7bb6f1;--accent-ink:#0b1520;--focus:rgba(90,162,236,.45)}}",
  "*{box-sizing:border-box}",
  "html{-webkit-text-size-adjust:100%;text-size-adjust:100%}",
  'body{margin:0;background:var(--bg);color:var(--ink);font:1.0625rem/1.5 system-ui,-apple-system,"Segoe UI",Roboto,',
  "sans-serif}",
  "main{max-width:32rem;margin:0 auto;padding:max(24px,env(safe-area-inset-top)) 16px 32px}",
  ".brand{margin:0 0 20px;font-weight:700;font-size:1rem;letter-spacing:-.01em}",
  ".card{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:20px}",
  "h1{margin:0 0 8px;font-size:1.375rem;line-height:1.25}",
  "p{margin:0 0 16px}",
  ".card p:last-child{margin-bottom:0}",
  ".btn{display:inline-flex;align-items:center;min-height:44px;padding:0 18px;border-radius:10px;",
  "background:var(--accent);color:var(--accent-ink);font-weight:600;text-decoration:none}",
  ".btn:hover{background:var(--accent-hover)}",
  ".btn:focus-visible{outline:3px solid var(--focus);outline-offset:2px}",
].join("");

/** 'sha256-...' of the style, for the CSP (the route handler and proxy.ts run on Node.js). */
export const UNAVAILABLE_STYLE_HASH = `sha256-${createHash("sha256").update(UNAVAILABLE_STYLE).digest("base64")}`;

export function escapeHtml(text: string): string {
  return text.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
}

/** The API answers JSON: its paths, or a request that asks for JSON rather than a page. */
export function wantsJson(pathname: string, accept: string | null): boolean {
  if (pathname === "/api" || pathname.startsWith("/api/")) return true;
  const types = (accept ?? "").toLowerCase();
  return types.includes("application/json") && !types.includes("text/html");
}

/** A path on this site: one leading slash (so "//host" can't become another site), no control characters. */
function localPath(pathname: string, search = ""): string {
  const path = `/${pathname.replace(/^[/\\]+/, "")}${search === "?" ? "" : search}`;
  return /[\u0000-\u001f\u007f]/.test(path) ? "/" : path;
}

/**
 * Where "Try again" goes: the same address for a page (GET); for a form post, the page the form was on when the
 * browser said so (same site only), else the ideas. A link never sends the form again.
 */
export function retryTarget(request: Pick<UnavailableRequest, "method" | "pathname" | "search" | "referer" | "origin">) {
  const method = request.method.toUpperCase();
  if (method === "GET" || method === "HEAD") return { href: localPath(request.pathname, request.search), resend: true };
  try {
    const from = new URL(request.referer ?? "");
    if (from.origin === request.origin) return { href: localPath(from.pathname, from.search), resend: false };
  } catch {
    // no Referer, or not a URL
  }
  return { href: "/", resend: false };
}

function explanation(kind: UnavailableKind, resend: boolean): string {
  if (kind === "not_configured") return "Its administrator has to finish the settings on Vercel.";
  if (kind === "timeout") {
    return resend ? "Try again in a moment." : "What you sent may still have been saved: check before sending it again.";
  }
  const restart = "It may be restarting after an update, which takes up to a minute.";
  return resend ? `${restart} Try again in a moment.` : `${restart} What you sent probably wasn't saved: send it again in a moment.`;
}

/** The status of a case; Fly's edge's own status (502/503/504) when it answered for the app. */
export function unavailableStatus(kind: UnavailableKind, upstreamStatus?: number): number {
  return upstreamStatus && [502, 503, 504].includes(upstreamStatus) ? upstreamStatus : STATUS[kind];
}

export function unavailableResponse(request: UnavailableRequest, kind: UnavailableKind, upstreamStatus?: number): Response {
  const status = unavailableStatus(kind, upstreamStatus);
  const title = TITLES[kind];
  const retry = retryTarget(request);
  const message = explanation(kind, retry.resend);
  const common = {
    "cache-control": "no-store",
    "retry-after": String(RETRY_AFTER_SECONDS),
    "x-robots-tag": "noindex",
    ...Object.fromEntries(Object.entries(securityHeaders({ https: request.https })).map(([k, v]) => [k.toLowerCase(), v])),
  };
  const head = request.method.toUpperCase() === "HEAD";

  if (wantsJson(request.pathname, request.accept)) {
    const body = { error: { code: "unavailable", message: `${title}. ${message}`, retry_after: RETRY_AFTER_SECONDS } };
    return new Response(head ? null : JSON.stringify(body), {
      status,
      headers: {
        ...common,
        "content-type": "application/json",
        "content-security-policy": "default-src 'none'; frame-ancestors 'none'",
      },
    });
  }

  const csp = [
    "default-src 'none'",
    `style-src '${UNAVAILABLE_STYLE_HASH}'`,
    "img-src 'self'",
    "base-uri 'none'",
    "form-action 'none'",
    "frame-ancestors 'none'",
  ].join("; ");
  const html = [
    '<!doctype html><html lang="en"><head><meta charset="utf-8">',
    '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">',
    '<meta name="color-scheme" content="light dark"><meta name="robots" content="noindex">',
    '<link rel="icon" href="/icon.svg" type="image/svg+xml">',
    `<title>${escapeHtml(title)} · Dip scanner</title><style>${UNAVAILABLE_STYLE}</style></head>`,
    '<body><main><p class="brand">Dip scanner</p><div class="card" role="alert">',
    `<h1>${escapeHtml(title)}</h1><p>${escapeHtml(message)}</p>`,
    `<p><a class="btn" href="${escapeHtml(retry.href)}">${retry.resend ? "Try again" : "Go back"}</a></p>`,
    "</div></main></body></html>",
  ].join("");
  return new Response(head ? null : html, {
    status,
    headers: { ...common, "content-type": "text/html; charset=utf-8", "content-security-policy": csp },
  });
}

/**
 * Whether a 502/503/504 came from Fly's edge rather than from the app (a Machine that is restarting or down): every
 * answer of the app carries its Content-Security-Policy (dip_scanner/web/app.py SECURITY_HEADERS), Fly's edge's
 * don't. The app's own 503s (/healthz while the scanner is stopped, the API's "unavailable") pass through.
 */
export function isEdgeError(status: number, headers: Headers): boolean {
  return [502, 503, 504].includes(status) && !headers.has("content-security-policy");
}
