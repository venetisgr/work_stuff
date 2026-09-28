/**
 * Everything this Next.js app doesn't render itself goes to the Fly app: sign-in, invites, settings, news, track
 * record, admin, tickers, jobs, the JSON API (/api/v1/...) and Fly's own static files. Next.js matches its pages
 * (/ and /ideas/[id]) and its own files (/_next/..., /icon.svg) first, so this catch-all only sees the rest.
 *
 * It streams the request to DIP_API_ORIGIN with the proxy's headers (src/lib/forward.ts) and streams Fly's answer
 * back: status, headers (each Set-Cookie), redirects as they are (redirect: "manual") and body. When Fly can't be
 * asked (not configured, unreachable, too slow, or Fly's edge answering for a stopped Machine) it answers a small page
 * of its own, or the API's JSON error (src/lib/unavailable.ts).
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
  trustsForwardedHeaders,
  UPSTREAM_TIMEOUT_MS,
  upstreamRequestHeaders,
  upstreamUrl,
  visitorHost,
} from "@/lib/forward";
import { isEdgeError, unavailableResponse, type UnavailableKind } from "@/lib/unavailable";

export const dynamic = "force-dynamic";

/** Seconds this function may run on Vercel: a little longer than UPSTREAM_TIMEOUT_MS (Hobby allows 300 with Fluid
 * compute, the default); a literal, as Next.js reads it from the source. */
export const maxDuration = 130;

async function forward(request: NextRequest): Promise<Response> {
  const url = new URL(request.url);
  let config;
  try {
    config = serverConfig();
  } catch (error) {
    const message = error instanceof ConfigError ? error.message : "The proxy isn't configured.";
    console.error(`Proxy not configured: ${message}`);
    return unavailable(request, url, "not_configured");
  }
  let target: string;
  try {
    target = upstreamUrl(config.origin, url.pathname, url.search);
  } catch {
    return new Response("Bad request", { status: 400 });
  }
  const headers = upstreamRequestHeaders(request.headers, {
    secret: config.secret,
    host: visitorHost(request.headers, url.host),
    clientIp: clientIp(request.headers, trustsForwardedHeaders()),
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
    return unavailable(request, url, timedOut ? "timeout" : "unreachable");
  }
  if (isEdgeError(upstream.status, upstream.headers)) {
    // Fly's edge answering for a Machine that is restarting or down (the app's own answers carry its CSP)
    console.error(`Fly's edge answered ${upstream.status} for ${request.method} ${url.pathname}`);
    await upstream.body?.cancel().catch(() => undefined);
    return unavailable(request, url, upstream.status === 504 ? "timeout" : "unreachable", upstream.status);
  }
  return new Response(request.method === "HEAD" ? null : upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: downstreamResponseHeaders(upstream.headers),
  });
}

/** Our own page (or the API's JSON error) when Fly can't be asked; no technical details (src/lib/unavailable.ts). */
function unavailable(request: NextRequest, url: URL, kind: UnavailableKind, upstreamStatus?: number): Response {
  return unavailableResponse(
    {
      method: request.method,
      pathname: url.pathname,
      search: url.search,
      accept: request.headers.get("accept"),
      referer: request.headers.get("referer"),
      origin: url.origin,
      https: url.protocol === "https:" || request.headers.get("x-forwarded-proto") === "https",
    },
    kind,
    upstreamStatus,
  );
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
