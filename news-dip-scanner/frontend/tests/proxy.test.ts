// The proxy to Fly: which paths Next.js keeps, which headers Fly sees, what comes back, the CSP of the React pages
// and the renewed session cookie.
import { unstable_doesMiddlewareMatch } from "next/experimental/testing/server";
import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { contentSecurityPolicy, makeNonce, securityHeaders } from "@/lib/csp";
import {
  FORWARDED_REQUEST_HEADERS,
  cleanIp,
  clientIp,
  downstreamResponseHeaders,
  hasBody,
  stripPrivateHeaders,
  trustsForwardedHeaders,
  upstreamRequestHeaders,
  upstreamUrl,
  visitorHost,
} from "@/lib/forward";
import { loginUrl, renewedSessionCookie, safeNextPath } from "@/lib/session";
import { config, proxy } from "@/proxy";

const SECRET = "s".repeat(40);

describe("which paths run proxy.ts (the React pages)", () => {
  const matches = (url: string, headers?: Record<string, string>) =>
    unstable_doesMiddlewareMatch({ config, url, headers });

  it.each(["/", "/?days=30&min_score=65", "/ideas/7", "/ideas/123?x=1", "/ideas/abc"])("runs for %s", (url) => {
    expect(matches(url)).toBe(true);
  });

  it.each([
    "/login",
    "/logout",
    "/news",
    "/track",
    "/settings",
    "/admin",
    "/admin/users",
    "/tickers/AMD",
    "/jobs/4",
    "/static/app.css",
    "/api/v1/me",
    "/ideas",
    "/ideas/7/extra",
    "/invite/abcdef",
  ])("doesn't run for %s (Fly's)", (url) => {
    expect(matches(url)).toBe(false);
  });

  it("skips router prefetches", () => {
    expect(matches("/", { "next-router-prefetch": "1" })).toBe(false);
  });
});

describe("proxy.ts", () => {
  beforeEach(() => {
    vi.stubEnv("DIP_API_ORIGIN", "https://my-dips.fly.dev");
    vi.stubEnv("DIP_PROXY_SECRET", SECRET);
  });
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("answers the React pages with the catch-all's 'not set up' page until both settings are there", async () => {
    vi.stubEnv("DIP_PROXY_SECRET", "");
    const response = proxy(new NextRequest("https://dips.example.com/", { headers: { accept: "text/html" } }));
    expect(response.status).toBe(503);
    expect(response.headers.get("content-type")).toBe("text/html; charset=utf-8");
    expect(response.headers.get("x-middleware-next")).toBeNull(); // the page doesn't render (and doesn't throw)
    const html = await response.text();
    expect(html).toContain("This site isn&#39;t set up yet");
    expect(html).toContain('name="viewport"');
  });

  it("sets a nonce CSP and the security headers, and strips x-dip-* from the page's request", () => {
    const request = new NextRequest("https://dips.example.com/ideas/7", {
      headers: { "x-dip-proxy-secret": "forged", "x-dip-client-ip": "6.6.6.6", cookie: "dsid=" + "a".repeat(43) },
    });
    const response = proxy(request);
    const csp = response.headers.get("content-security-policy") ?? "";
    expect(csp).toMatch(/script-src 'self' 'nonce-[A-Za-z0-9+/]{24}' 'strict-dynamic'/);
    expect(csp).not.toContain("unsafe-eval");
    expect(csp).toContain("upgrade-insecure-requests");
    expect(response.headers.get("x-frame-options")).toBe("DENY");
    expect(response.headers.get("strict-transport-security")).toBe("max-age=31536000");
    // the request headers Next.js hands to the page: overridden list without x-dip-*
    const overridden = response.headers.get("x-middleware-override-headers") ?? "";
    expect(overridden).toContain("x-nonce");
    expect(overridden).not.toContain("x-dip-");
    expect(response.headers.get("set-cookie")).toMatch(/^dsid=a{43}; Max-Age=2592000; Path=\/; HttpOnly; SameSite=Lax; Secure$/);
  });

  it("gives every page view its own nonce", () => {
    const one = proxy(new NextRequest("https://dips.example.com/")).headers.get("content-security-policy");
    const two = proxy(new NextRequest("https://dips.example.com/")).headers.get("content-security-policy");
    expect(one).not.toBe(two);
  });

  it("sets no cookie without a session", () => {
    expect(proxy(new NextRequest("https://dips.example.com/")).headers.get("set-cookie")).toBeNull();
  });
});

describe("CSP", () => {
  it("allows only this site, with the nonce, and no eval in production", () => {
    const csp = contentSecurityPolicy("abc", { dev: false, https: false });
    expect(csp).toBe(
      "default-src 'self'; script-src 'self' 'nonce-abc' 'strict-dynamic'; style-src 'self' 'nonce-abc'; " +
        "img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; " +
        "form-action 'self'; frame-ancestors 'none'",
    );
  });

  it("relaxes only what next dev needs", () => {
    const csp = contentSecurityPolicy("abc", { dev: true, https: false });
    expect(csp).toContain("'unsafe-eval'");
    expect(csp).toContain("style-src 'self' 'unsafe-inline'");
  });

  it("makes random 24-character nonces", () => {
    const nonces = new Set(Array.from({ length: 50 }, makeNonce));
    expect(nonces.size).toBe(50);
    for (const nonce of nonces) expect(nonce).toMatch(/^[A-Za-z0-9+/]{24}$/);
  });

  it("adds HSTS over https only", () => {
    expect(securityHeaders({ https: false })["Strict-Transport-Security"]).toBeUndefined();
    expect(securityHeaders({ https: true })["Strict-Transport-Security"]).toBe("max-age=31536000");
  });
});

describe("headers to Fly", () => {
  const incoming = new Headers({
    host: "dips.example.com",
    cookie: "dsid=abc; other=1",
    "content-type": "application/x-www-form-urlencoded",
    origin: "https://dips.example.com",
    referer: "https://dips.example.com/settings",
    "user-agent": "Mozilla/5.0 (iPhone)",
    "x-csrf-token": "tok",
    "x-dip-proxy-secret": "forged",
    "X-Dip-Client-Ip": "6.6.6.6",
    "x-dip-anything": "1",
    "x-real-ip": "203.0.113.9",
    "x-forwarded-for": "203.0.113.9, 10.0.0.1",
    "x-forwarded-host": "evil.example",
    "x-vercel-oidc-token": "secret-token",
    "x-vercel-id": "fra1::abc",
    authorization: "Bearer x",
    "x-middleware-subrequest": "middleware",
  });
  const headers = upstreamRequestHeaders(incoming, { secret: SECRET, host: "dips.example.com", clientIp: "203.0.113.9" });

  it("replaces the visitor's x-dip-* headers with the proxy's", () => {
    expect(headers.get("x-dip-proxy-secret")).toBe(SECRET);
    expect(headers.get("x-dip-client-ip")).toBe("203.0.113.9");
    expect(headers.get("x-dip-anything")).toBeNull();
  });

  it("adds the forwarded host and proto", () => {
    expect(headers.get("x-forwarded-host")).toBe("dips.example.com");
    expect(headers.get("x-forwarded-proto")).toBe("https");
  });

  it("keeps cookies, forms, CSRF and origin headers", () => {
    expect(headers.get("cookie")).toBe("dsid=abc; other=1");
    expect(headers.get("content-type")).toBe("application/x-www-form-urlencoded");
    expect(headers.get("origin")).toBe("https://dips.example.com");
    expect(headers.get("referer")).toBe("https://dips.example.com/settings");
    expect(headers.get("x-csrf-token")).toBe("tok");
    expect(headers.get("user-agent")).toBe("Mozilla/5.0 (iPhone)");
  });

  it("drops everything else (platform tokens, auth, forwarding chains)", () => {
    const names: string[] = [];
    headers.forEach((_value, name) => names.push(name));
    const own = ["x-dip-proxy-secret", "x-dip-client-ip", "x-forwarded-host", "x-forwarded-proto"];
    for (const name of names) expect([...FORWARDED_REQUEST_HEADERS, ...own]).toContain(name);
    for (const name of ["x-vercel-oidc-token", "x-vercel-id", "authorization", "x-forwarded-for", "x-real-ip", "host"]) {
      expect(headers.get(name), name).toBeNull();
    }
  });

  it("leaves out the client IP when it is unknown", () => {
    const bare = upstreamRequestHeaders(new Headers(), { secret: SECRET, host: "h", clientIp: null });
    expect(bare.get("x-dip-client-ip")).toBeNull();
  });
});

describe("the visitor's address and host", () => {
  it("prefers x-real-ip, then the first x-forwarded-for, where the platform sets them", () => {
    const ip = (init: Record<string, string>) => clientIp(new Headers(init), true);
    expect(ip({ "x-real-ip": "198.51.100.4", "x-forwarded-for": "1.1.1.1" })).toBe("198.51.100.4");
    expect(ip({ "x-forwarded-for": "2001:db8::1, 10.0.0.1" })).toBe("2001:db8::1");
    expect(ip({})).toBeNull();
  });

  it("believes neither header elsewhere: next start passes on what the visitor sent", () => {
    // A visitor who could name their address would pick the one the Fly app's sign-in limits count.
    const forged = new Headers({ "x-real-ip": "8.8.4.4", "x-forwarded-for": "9.9.9.9" });
    expect(clientIp(forged, false)).toBeNull();
    expect(upstreamRequestHeaders(forged, { secret: SECRET, host: "h", clientIp: clientIp(forged, false) }).get("x-dip-client-ip")).toBeNull();
  });

  it("trusts them on Vercel, or behind a proxy of your own that says so", () => {
    expect(trustsForwardedHeaders({ VERCEL: "1" })).toBe(true);
    expect(trustsForwardedHeaders({ DIP_TRUSTED_PROXY: "1" })).toBe(true);
    expect(trustsForwardedHeaders({})).toBe(false);
    expect(trustsForwardedHeaders({ VERCEL: "0", DIP_TRUSTED_PROXY: "yes" })).toBe(false);
  });

  it("refuses anything that isn't an address", () => {
    expect(cleanIp("1.2.3.4\r\nx-evil: 1")).toBeNull();
    expect(cleanIp("300.1.1.1")).toBeNull();
    expect(cleanIp("example.com")).toBeNull();
    expect(cleanIp("::1")).toBe("::1");
  });

  it("takes the host from the Host header, not a forwarded one", () => {
    expect(visitorHost(new Headers({ host: "dips.example.com", "x-forwarded-host": "evil.example" }), "x")).toBe(
      "dips.example.com",
    );
    expect(visitorHost(new Headers({ host: "bad host" }), "fallback.example")).toBe("fallback.example");
  });
});

describe("the Fly URL of a path", () => {
  const origin = "https://my-dips.fly.dev";

  it("joins origin, path and query", () => {
    expect(upstreamUrl(origin, "/login", "?next=%2F")).toBe("https://my-dips.fly.dev/login?next=%2F");
    expect(upstreamUrl(origin, "/static/app.css", "")).toBe("https://my-dips.fly.dev/static/app.css");
    expect(upstreamUrl("http://127.0.0.1:8080", "/api/v1/me")).toBe("http://127.0.0.1:8080/api/v1/me");
  });

  it("can't be turned into another host", () => {
    expect(new URL(upstreamUrl(origin, "//evil.example/x")).host).toBe("my-dips.fly.dev");
    expect(new URL(upstreamUrl(origin, "/@evil.example")).host).toBe("my-dips.fly.dev");
    expect(new URL(upstreamUrl(origin, "/..%2f..%2fetc")).host).toBe("my-dips.fly.dev");
  });
});

describe("headers back to the visitor", () => {
  it("keeps every Set-Cookie separately, Location and the rest, drops hop-by-hop and encoding", () => {
    const upstream = new Headers();
    upstream.append("set-cookie", "dsid=abc; HttpOnly; Path=/; SameSite=lax; Secure");
    upstream.append("set-cookie", "dip_form=xyz; HttpOnly; Path=/");
    upstream.set("location", "/login?next=%2F");
    upstream.set("content-type", "text/html; charset=utf-8");
    upstream.set("content-security-policy", "default-src 'self'");
    upstream.set("cache-control", "no-store");
    upstream.set("content-encoding", "gzip");
    upstream.set("content-length", "123");
    upstream.set("connection", "keep-alive");
    upstream.set("transfer-encoding", "chunked");
    const headers = downstreamResponseHeaders(upstream);
    expect(headers.getSetCookie()).toEqual([
      "dsid=abc; HttpOnly; Path=/; SameSite=lax; Secure",
      "dip_form=xyz; HttpOnly; Path=/",
    ]);
    expect(headers.get("location")).toBe("/login?next=%2F");
    expect(headers.get("content-security-policy")).toBe("default-src 'self'");
    expect(headers.get("cache-control")).toBe("no-store");
    for (const name of ["content-encoding", "content-length", "connection", "transfer-encoding"]) {
      expect(headers.get(name), name).toBeNull();
    }
  });

  it("knows which methods carry a body", () => {
    expect(hasBody("GET")).toBe(false);
    expect(hasBody("HEAD")).toBe(false);
    expect(hasBody("POST")).toBe(true);
    expect(hasBody("delete")).toBe(true);
  });

  it("strips x-dip-* headers in place", () => {
    const headers = new Headers({ "x-dip-proxy-secret": "a", "X-DIP-Client-IP": "b", accept: "*/*" });
    expect(stripPrivateHeaders(headers).sort()).toEqual(["x-dip-client-ip", "x-dip-proxy-secret"]);
    expect(headers.get("accept")).toBe("*/*");
  });
});

describe("the session cookie", () => {
  it("renews a plausible token for 30 days", () => {
    const token = "Ab_-" + "x".repeat(39);
    expect(renewedSessionCookie(token, { https: false })).toBe(
      `dsid=${token}; Max-Age=2592000; Path=/; HttpOnly; SameSite=Lax`,
    );
  });

  it("ignores missing or odd tokens", () => {
    expect(renewedSessionCookie(undefined, { https: true })).toBeNull();
    expect(renewedSessionCookie("short", { https: true })).toBeNull();
    expect(renewedSessionCookie("a".repeat(40) + ";Domain=evil", { https: true })).toBeNull();
  });

  it("sends people back only to paths of this site", () => {
    expect(safeNextPath("/ideas/7?x=1")).toBe("/ideas/7?x=1");
    expect(safeNextPath("//evil.example")).toBe("/");
    expect(safeNextPath("https://evil.example")).toBe("/");
    expect(safeNextPath("/\\evil.example")).toBe("/");
    expect(loginUrl("/ideas/7")).toBe("/login?next=%2Fideas%2F7");
  });
});
