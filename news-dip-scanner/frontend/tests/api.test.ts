// The server-side API client's pure parts, and the server-only settings.
import { describe, expect, it } from "vitest";
import {
  ApiRequestError,
  apiRequestHeaders,
  apiUrl,
  errorFor,
  hasFilters,
  ideasQueryFrom,
  ideasSearch,
  readApiResponse,
} from "@/lib/api-core";
import { ConfigError, parseServerConfig } from "@/lib/env";

const SECRET = "k".repeat(48);

function json(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

describe("apiUrl", () => {
  it("puts the path under /api/v1 of the origin", () => {
    expect(apiUrl("https://my-dips.fly.dev", "/ideas/7")).toBe("https://my-dips.fly.dev/api/v1/ideas/7");
    expect(apiUrl("https://my-dips.fly.dev/", "/ideas?days=30")).toBe("https://my-dips.fly.dev/api/v1/ideas?days=30");
  });

  it("refuses paths that could leave it", () => {
    expect(() => apiUrl("https://a.fly.dev", "ideas")).toThrow();
    expect(() => apiUrl("https://a.fly.dev", "//evil.example")).toThrow();
    expect(() => apiUrl("https://a.fly.dev", "/../login")).toThrow();
  });
});

describe("apiRequestHeaders", () => {
  const incoming = new Headers({
    host: "dips.example.com",
    cookie: "dsid=tok; _ga=1",
    "user-agent": "Safari",
    "x-real-ip": "203.0.113.7",
    "x-dip-proxy-secret": "forged",
  });
  const headers = apiRequestHeaders(incoming, "tok", { secret: SECRET, fallbackHost: "localhost" });

  it("sends only the session cookie, the browser's name and JSON", () => {
    expect(headers.get("cookie")).toBe("dsid=tok");
    expect(headers.get("user-agent")).toBe("Safari");
    expect(headers.get("accept")).toBe("application/json");
  });

  it("adds the proxy's headers", () => {
    expect(headers.get("x-dip-proxy-secret")).toBe(SECRET);
    expect(headers.get("x-dip-client-ip")).toBe("203.0.113.7");
    expect(headers.get("x-forwarded-host")).toBe("dips.example.com");
    expect(headers.get("x-forwarded-proto")).toBe("https");
  });

  it("never sends a token that could break the header", () => {
    const odd = apiRequestHeaders(new Headers(), "a;b", { secret: SECRET, fallbackHost: "localhost" });
    expect(odd.get("cookie")).toBeNull();
    expect(odd.get("x-forwarded-host")).toBe("localhost");
  });
});

describe("readApiResponse", () => {
  it("returns the JSON of a 2xx answer", async () => {
    await expect(readApiResponse(json(200, { ok: 1 }))).resolves.toEqual({ ok: 1 });
    await expect(readApiResponse(json(202, { job_id: 4 }))).resolves.toEqual({ job_id: 4 });
  });

  it("turns the contract's errors into ApiRequestError", async () => {
    const error = await readApiResponse(
      json(429, { error: { code: "limit_reached", message: "You have used your 5.", retry_after: 3600 } }),
    ).catch((e) => e);
    expect(error).toBeInstanceOf(ApiRequestError);
    expect(error).toMatchObject({ status: 429, code: "limit_reached", message: "You have used your 5.", retryAfter: 3600 });
  });

  it("names errors without a JSON body by their status", async () => {
    const error = await readApiResponse(new Response("<html>", { status: 401 })).catch((e) => e);
    expect(error).toMatchObject({ status: 401, code: "not_signed_in" });
    const server = await readApiResponse(new Response("oops", { status: 500 })).catch((e) => e);
    expect(server).toMatchObject({ status: 500, code: "server_error" });
  });

  it("treats a redirect or an HTML page as a failure (the API answers JSON only)", async () => {
    const redirect = await readApiResponse(new Response(null, { status: 303, headers: { location: "/login" } })).catch(
      (e) => e,
    );
    expect(redirect).toMatchObject({ status: 502 });
    const html = await readApiResponse(new Response("<html>", { status: 200, headers: { "content-type": "text/html" } }))
      .catch((e) => e);
    expect(html).toMatchObject({ status: 502 });
  });

  it("errorFor reads a partial error body safely", () => {
    expect(errorFor(403, { error: { code: "csrf", message: "Reload." } })).toEqual({
      code: "csrf",
      message: "Reload.",
      retry_after: null,
    });
    expect(errorFor(418, "teapot").code).toBe("server_error");
  });
});

describe("the dashboard's filters", () => {
  it("reads the API's names and the Fly pages' old ones", () => {
    expect(ideasQueryFrom({ days: "30", min_score: "65", verdict: "mixed", watchlist: "1", matching: "1", sort: "new", page: "2" }))
      .toEqual({ days: 30, min_score: 65, verdict: "mixed", watchlist: true, matching: true, sort: "new", page: 2 });
    expect(ideasQueryFrom({ score: "80", rules: "on" })).toMatchObject({ min_score: 80, matching: true });
  });

  it("falls back to the defaults for anything invalid", () => {
    expect(ideasQueryFrom({ days: "5", min_score: "70", verdict: "buy", sort: "x", page: "-1" })).toEqual({
      days: 7,
      min_score: null,
      verdict: null,
      watchlist: false,
      matching: false,
      sort: "score",
      page: 1,
    });
    expect(ideasQueryFrom({ days: ["3", "30"] }).days).toBe(3);
  });

  it("writes only what differs from the defaults", () => {
    expect(ideasSearch({})).toBe("");
    expect(ideasSearch({ days: 30, min_score: 65, matching: true, page: 3 })).toBe("?days=30&min_score=65&matching=1&page=3");
  });

  it("knows when a filter is on", () => {
    expect(hasFilters(ideasQueryFrom({ days: "30" }))).toBe(false);
    expect(hasFilters(ideasQueryFrom({ verdict: "mixed" }))).toBe(true);
  });
});

describe("server settings", () => {
  it("accepts an https origin and a long secret", () => {
    expect(parseServerConfig({ DIP_API_ORIGIN: "https://my-dips.fly.dev/", DIP_PROXY_SECRET: SECRET })).toEqual({
      origin: "https://my-dips.fly.dev",
      secret: SECRET,
    });
    expect(parseServerConfig({ DIP_API_ORIGIN: "http://127.0.0.1:8080", DIP_PROXY_SECRET: SECRET }).origin).toBe(
      "http://127.0.0.1:8080",
    );
  });

  it.each([
    [{}, /DIP_API_ORIGIN/],
    [{ DIP_API_ORIGIN: "my-dips.fly.dev", DIP_PROXY_SECRET: SECRET }, /address like/],
    [{ DIP_API_ORIGIN: "http://my-dips.fly.dev", DIP_PROXY_SECRET: SECRET }, /https/],
    [{ DIP_API_ORIGIN: "https://my-dips.fly.dev/api", DIP_PROXY_SECRET: SECRET }, /without a path/],
    [{ DIP_API_ORIGIN: "https://my-dips.fly.dev", DIP_PROXY_SECRET: "short" }, /DIP_PROXY_SECRET/],
  ])("refuses %j", (env, message) => {
    expect(() => parseServerConfig(env as Record<string, string>)).toThrow(ConfigError);
    expect(() => parseServerConfig(env as Record<string, string>)).toThrow(message);
  });

  it("never puts the secret in an error message", () => {
    try {
      parseServerConfig({ DIP_API_ORIGIN: "ftp://x", DIP_PROXY_SECRET: SECRET });
    } catch (error) {
      expect(String(error)).not.toContain(SECRET);
    }
  });
});
