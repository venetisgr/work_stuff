/**
 * Everything this Next.js app doesn't render itself goes to the Fly app: sign-in, invites, settings, news, track
 * record, admin, tickers, jobs, the JSON API (/api/v1/...) and Fly's own static files. Next.js matches its pages
 * (/ and /ideas/[id]) and its own files (/_next/..., /icon.svg) first, so this catch-all only sees the rest.
 *
 * It streams the request to DIP_API_ORIGIN with the proxy's headers (src/lib/forward.ts) and streams Fly's answer
 * back: status, headers (each Set-Cookie), redirects as they are (redirect: "manual") and body.
 *
 * Why a route handler and not a rewrite in proxy.ts: a rewrite with changed request headers works (tested with
 * `next start`: GET, POST, cookies both ways, 303 redirects), but Next.js then tells the browser where it went in an
 * x-middleware-rewrite response header, which would publish DIP_API_ORIGIN (github.com/vercel/next.js/discussions/58366).
 */
import type { NextRequest } from "next/server";
import { ConfigError, serverConfig } from "@/lib/env";
import {
  clientIp,
  downstreamResponseHeaders,
  hasBody,
  upstreamRequestHeaders,
  upstreamUrl,
  visitorHost,
} from "@/lib/forward";

export const dynamic = "force-dynamic";

/** How long Fly may take to answer (a manual analysis is a job; no page should take this long). */
const UPSTREAM_TIMEOUT_MS = 30_000;

async function forward(request: NextRequest): Promise<Response> {
  let config;
  try {
    config = serverConfig();
  } catch (error) {
    const message = error instanceof ConfigError ? error.message : "The proxy isn't configured.";
    console.error(`Proxy not configured: ${message}`);
    return unavailable("This site isn't set up yet: its administrator has to finish the Vercel settings.");
  }
  const url = new URL(request.url);
  let target: string;
  try {
    target = upstreamUrl(config.origin, url.pathname, url.search);
  } catch {
    return new Response("Bad request", { status: 400 });
  }
  const headers = upstreamRequestHeaders(request.headers, {
    secret: config.secret,
    host: visitorHost(request.headers, url.host),
    clientIp: clientIp(request.headers),
  });
  const init: RequestInit & { duplex?: "half" } = {
    method: request.method,
    headers,
    redirect: "manual",
    cache: "no-store",
    signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
  };
  if (hasBody(request.method) && request.body) {
    init.body = request.body;
    init.duplex = "half"; // stream the body instead of reading it all first
    const length = request.headers.get("content-length");
    if (length) headers.set("content-length", length);
  }
  let upstream: Response;
  try {
    upstream = await fetch(target, init);
  } catch (error) {
    const timedOut = error instanceof Error && error.name === "TimeoutError";
    console.error(`Fly didn't answer ${request.method} ${url.pathname}: ${timedOut ? "timed out" : String(error)}`);
    return unavailable(
      timedOut
        ? "The scanner's server took too long to answer. Try again in a moment."
        : "The scanner's server can't be reached just now. Try again in a moment.",
      timedOut ? 504 : 502,
    );
  }
  return new Response(request.method === "HEAD" ? null : upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: downstreamResponseHeaders(upstream.headers),
  });
}

/** A short page (or JSON for the API) when Fly can't be asked; no technical details. */
function unavailable(message: string, status = 503): Response {
  return new Response(`${message}\n`, {
    status,
    headers: {
      "content-type": "text/plain; charset=utf-8",
      "cache-control": "no-store",
      "retry-after": "30",
      "x-content-type-options": "nosniff",
    },
  });
}

export {
  forward as GET,
  forward as HEAD,
  forward as POST,
  forward as PUT,
  forward as PATCH,
  forward as DELETE,
  forward as OPTIONS,
};
