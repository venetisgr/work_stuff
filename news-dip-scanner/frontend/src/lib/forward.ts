/**
 * How a request reaches the Fly app, and how its answer comes back: pure functions over Headers, shared by the
 * catch-all route handler (src/app/[...path]/route.ts), the server-side API client (src/lib/api.ts) and the tests.
 *
 * To Fly: only an allow-list of the visitor's headers (FORWARDED_REQUEST_HEADERS), never an incoming x-dip-* header,
 * plus x-dip-proxy-secret (DIP_PROXY_SECRET), x-dip-client-ip (the visitor's address as Vercel reports it),
 * x-forwarded-host (the address the visitor used) and x-forwarded-proto: https. Everything else the platform adds
 * (x-vercel-*, including OIDC tokens; x-middleware-*; x-forwarded-for) stays here.
 *
 * Back: Fly's status and headers unchanged (every Set-Cookie separately, Location as Fly wrote it) except the
 * hop-by-hop ones, and the body's encoding and length (fetch hands over the body decoded).
 */

/** The visitor's request headers Fly may see. Everything else is dropped. */
export const FORWARDED_REQUEST_HEADERS: readonly string[] = [
  "accept",
  "accept-language",
  "cache-control",
  "content-type",
  "cookie",
  "if-match",
  "if-modified-since",
  "if-none-match",
  "if-unmodified-since",
  "origin",
  "pragma",
  "range",
  "referer",
  "user-agent",
  "x-csrf-token",
  "x-requested-with",
];

/** Headers that describe one connection, not the message (RFC 9110 7.6.1), plus what fetch recomputes. */
const HOP_BY_HOP = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "proxy-connection",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
]);

/** Response headers not passed back: hop-by-hop ones, and the encoding and length of a body fetch has decoded. */
const DROPPED_RESPONSE_HEADERS = new Set([...HOP_BY_HOP, "content-encoding", "content-length"]);

export const SECRET_HEADER = "x-dip-proxy-secret";
export const CLIENT_IP_HEADER = "x-dip-client-ip";
const PRIVATE_PREFIX = "x-dip-";

/** An IPv4 or IPv6 address as a header carries it (no port, no brackets), or null for anything else. */
export function cleanIp(value: string | null | undefined): string | null {
  const text = (value ?? "").trim();
  if (!text || text.length > 45) return null;
  if (/^\d{1,3}(\.\d{1,3}){3}$/.test(text)) {
    return text.split(".").every((part) => Number(part) <= 255) ? text : null;
  }
  if (/^[0-9a-fA-F:.]+$/.test(text) && text.includes(":")) return text.toLowerCase();
  return null;
}

/**
 * The visitor's address. On Vercel, x-real-ip and x-forwarded-for are set by the platform, which overwrites whatever
 * the visitor sent (vercel.com/docs/headers/request-headers), so they can be trusted there; `next start` on your own
 * computer sets x-forwarded-for from the connection.
 */
export function clientIp(headers: Headers): string | null {
  const real = cleanIp(headers.get("x-real-ip"));
  if (real) return real;
  const forwarded = headers.get("x-forwarded-for");
  return forwarded ? cleanIp(forwarded.split(",")[0]) : null;
}

/** The host the visitor asked for (the Vercel domain), for x-forwarded-host. Vercel routes by the Host header, so
 * it is always one of the project's domains there. */
export function visitorHost(headers: Headers, fallback: string): string {
  const host = (headers.get("host") || fallback).split(",")[0].trim();
  return /^[A-Za-z0-9.\-:[\]]+$/.test(host) ? host : fallback;
}

export interface ProxyIdentity {
  secret: string;
  host: string;
  clientIp: string | null;
}

/** The headers Fly gets: the allowed visitor headers, then the proxy's own (which always win). */
export function upstreamRequestHeaders(incoming: Headers, identity: ProxyIdentity): Headers {
  const headers = new Headers();
  for (const name of FORWARDED_REQUEST_HEADERS) {
    const value = incoming.get(name);
    if (value !== null) headers.set(name, value);
  }
  return withProxyHeaders(headers, identity);
}

/** headers plus x-dip-proxy-secret, x-dip-client-ip, x-forwarded-host and x-forwarded-proto (any x-dip-* removed). */
export function withProxyHeaders(headers: Headers, identity: ProxyIdentity): Headers {
  const names: string[] = [];
  headers.forEach((_value, name) => names.push(name));
  for (const name of names) {
    if (name.toLowerCase().startsWith(PRIVATE_PREFIX)) headers.delete(name);
  }
  headers.set(SECRET_HEADER, identity.secret);
  if (identity.clientIp) headers.set(CLIENT_IP_HEADER, identity.clientIp);
  headers.set("x-forwarded-host", identity.host);
  headers.set("x-forwarded-proto", "https");
  return headers;
}

/** Remove the x-dip-* headers a visitor sent (proxy.ts, before a page renders). Returns the names removed. */
export function stripPrivateHeaders(headers: Headers): string[] {
  const names: string[] = [];
  headers.forEach((_value, name) => {
    if (name.toLowerCase().startsWith(PRIVATE_PREFIX)) names.push(name);
  });
  for (const name of names) headers.delete(name);
  return names;
}

/**
 * The Fly URL of a path on this site: origin + path + query, joined as text so that a path like //evil.example can't
 * become a host. Throws if the result would leave the origin.
 */
export function upstreamUrl(origin: string, pathname: string, search = ""): string {
  const base = new URL(origin);
  const path = pathname.startsWith("/") ? pathname : `/${pathname}`;
  const query = search && !search.startsWith("?") ? `?${search}` : search;
  const target = new URL(`${base.origin}${path}${query === "?" ? "" : query}`);
  if (target.origin !== base.origin) throw new Error("The path would leave the Fly origin");
  return target.toString();
}

/** Fly's response headers for the visitor: everything but hop-by-hop headers and the body's encoding and length;
 * each Set-Cookie stays a header of its own. */
export function downstreamResponseHeaders(upstream: Headers): Headers {
  const headers = new Headers();
  upstream.forEach((value, name) => {
    const key = name.toLowerCase();
    if (key === "set-cookie" || DROPPED_RESPONSE_HEADERS.has(key)) return;
    headers.set(name, value);
  });
  for (const cookie of upstream.getSetCookie()) headers.append("set-cookie", cookie);
  return headers;
}

/** Methods whose requests carry a body. */
export function hasBody(method: string): boolean {
  return !["GET", "HEAD"].includes(method.toUpperCase());
}
