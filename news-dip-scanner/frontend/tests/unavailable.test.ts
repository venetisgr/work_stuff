// What the front door answers when it can't ask the Fly app: a page of its own for people (readable on a phone, with
// a way back, its only style allowed by hash), the contract's JSON error for the API, and Fly's edge's own 502/503/504
// replaced by the same, while the app's own errors pass through.
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { join } from "node:path";
import Ajv2020 from "ajv/dist/2020";
import addFormats from "ajv-formats";
import { NextRequest } from "next/server";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import * as route from "@/app/[...path]/route";
import { errorFor } from "@/lib/api-core";
import { UPSTREAM_TIMEOUT_MS } from "@/lib/forward";
import {
  RETRY_AFTER_SECONDS,
  UNAVAILABLE_STYLE,
  isEdgeError,
  retryTarget,
  unavailableResponse,
  wantsJson,
  type UnavailableRequest,
} from "@/lib/unavailable";

const SECRET = "s".repeat(40);
const schema = JSON.parse(readFileSync(join(__dirname, "..", "contract", "api-v1.schema.json"), "utf8"));
const ajv = new Ajv2020({ strict: true, allErrors: true });
addFormats(ajv);
ajv.addKeyword("x-endpoints");
ajv.addSchema(schema);
const validateError = ajv.getSchema(`${schema.$id}#/$defs/Error`)!;

function asked(overrides: Partial<UnavailableRequest> = {}): UnavailableRequest {
  return {
    method: "GET",
    pathname: "/login",
    search: "",
    accept: "text/html,application/xhtml+xml,*/*;q=0.8",
    referer: null,
    origin: "https://dips.example.com",
    https: true,
    ...overrides,
  };
}

const styleOf = (html: string) => /<style>([\s\S]*?)<\/style>/.exec(html)?.[1] ?? "";
const hrefOf = (html: string) => /<a class="btn" href="([^"]*)">([^<]*)<\/a>/.exec(html);

describe("the page for people", () => {
  it("is a phone-sized page with the site's name, the reason and a way to try again", async () => {
    const response = unavailableResponse(asked(), "unreachable");
    expect(response.status).toBe(502);
    expect(response.headers.get("content-type")).toBe("text/html; charset=utf-8");
    expect(response.headers.get("retry-after")).toBe(String(RETRY_AFTER_SECONDS));
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(response.headers.get("x-content-type-options")).toBe("nosniff");
    expect(response.headers.get("x-frame-options")).toBe("DENY");
    expect(response.headers.get("strict-transport-security")).toBe("max-age=31536000");
    const html = await response.text();
    expect(html).toMatch(/^<!doctype html><html lang="en">/);
    expect(html).toContain('<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">');
    expect(html).toContain('<meta name="color-scheme" content="light dark">');
    expect(html).toContain("Dip scanner");
    expect(html).toContain("The scanner&#39;s server can&#39;t be reached just now");
    expect(hrefOf(html)?.slice(1)).toEqual(["/login", "Try again"]);
    expect(html).toContain("@media (prefers-color-scheme:dark)");
    expect(html).not.toMatch(/<script|<form|<img|https?:\/\//);
  });

  it("allows exactly its own style in its CSP, by hash", async () => {
    const response = unavailableResponse(asked(), "timeout");
    const html = await response.text();
    expect(styleOf(html)).toBe(UNAVAILABLE_STYLE);
    const hash = createHash("sha256").update(styleOf(html)).digest("base64");
    const csp = response.headers.get("content-security-policy") ?? "";
    expect(csp).toBe(
      `default-src 'none'; style-src 'sha256-${hash}'; img-src 'self'; base-uri 'none'; form-action 'none'; ` +
        "frame-ancestors 'none'",
    );
  });

  it("says which case it is: 502 unreachable, 504 too slow, 503 not set up", async () => {
    const text = async (kind: "unreachable" | "timeout" | "not_configured") => {
      const response = unavailableResponse(asked(), kind);
      return [response.status, await response.text()] as const;
    };
    const [slow, slowHtml] = await text("timeout");
    expect(slow).toBe(504);
    expect(slowHtml).toContain("took too long to answer");
    const [unset, unsetHtml] = await text("not_configured");
    expect(unset).toBe(503);
    expect(unsetHtml).toContain("This site isn&#39;t set up yet");
  });

  it("escapes the address it links back to", async () => {
    const html = await unavailableResponse(
      asked({ pathname: "/tickers/AMD", search: `?q="><script>alert('x')</script>` }),
      "unreachable",
    ).text();
    expect(html).not.toContain("<script");
    expect(hrefOf(html)?.[1]).toBe("/tickers/AMD?q=&quot;&gt;&lt;script&gt;alert(&#39;x&#39;)&lt;/script&gt;");
  });

  it("never links to another site", () => {
    expect(retryTarget(asked({ pathname: "//evil.example/x" })).href).toBe("/evil.example/x");
    expect(retryTarget(asked({ pathname: "/\\evil.example" })).href).toBe("/evil.example");
    const post = asked({ method: "POST", pathname: "/settings/test", referer: "https://evil.example/settings" });
    expect(retryTarget(post)).toEqual({ href: "/", resend: false });
  });

  it("sends a failed form back to the page it came from, without sending it again", async () => {
    const post = asked({ method: "POST", pathname: "/settings/test", referer: "https://dips.example.com/settings?x=1" });
    expect(retryTarget(post)).toEqual({ href: "/settings?x=1", resend: false });
    const html = await unavailableResponse(post, "timeout").text();
    expect(hrefOf(html)?.slice(1)).toEqual(["/settings?x=1", "Go back"]);
    expect(html).toContain("may still have been saved");
  });

  it("has no body for HEAD", async () => {
    const response = unavailableResponse(asked({ method: "HEAD" }), "unreachable");
    expect(response.status).toBe(502);
    expect(await response.text()).toBe("");
  });
});

describe("the JSON API's answer", () => {
  it("is the contract's error, which the React code reads as 'unavailable'", async () => {
    const response = unavailableResponse(asked({ pathname: "/api/v1/ideas", accept: "*/*" }), "unreachable");
    expect(response.status).toBe(502);
    expect(response.headers.get("content-type")).toBe("application/json");
    expect(response.headers.get("retry-after")).toBe("30");
    const body = await response.json();
    expect(validateError(body), JSON.stringify(validateError.errors)).toBe(true);
    expect(body.error).toMatchObject({ code: "unavailable", retry_after: 30 });
    expect(errorFor(502, body)).toEqual(body.error); // AnalyseAgain shows this message, not "something went wrong"
  });

  it("is chosen by the path, or by an Accept that wants JSON and not a page", () => {
    expect(wantsJson("/api/v1/jobs/4", "text/html")).toBe(true);
    expect(wantsJson("/login", "application/json")).toBe(true);
    expect(wantsJson("/login", "text/html,application/json;q=0.9")).toBe(false);
    expect(wantsJson("/apiary", null)).toBe(false);
  });
});

describe("Fly's edge or the app", () => {
  it("tells Fly's edge's errors from the app's own (which carry its CSP)", () => {
    expect(isEdgeError(502, new Headers())).toBe(true);
    expect(isEdgeError(503, new Headers({ "content-type": "text/plain" }))).toBe(true);
    expect(isEdgeError(503, new Headers({ "content-security-policy": "default-src 'self'" }))).toBe(false);
    expect(isEdgeError(500, new Headers())).toBe(false);
    expect(isEdgeError(200, new Headers())).toBe(false);
  });
});

describe("the catch-all route handler", () => {
  let fly: Server;
  let flyOrigin = "";
  let closedOrigin = "";

  beforeAll(async () => {
    // A stand-in for Fly: /edge answers like Fly's edge for a stopped Machine, /healthz like the app when stopped.
    fly = createServer((request, response) => {
      if (request.url === "/edge") {
        response.writeHead(502, { "content-type": "text/plain" }).end("fly edge error");
      } else if (request.url === "/healthz") {
        response
          .writeHead(503, { "content-type": "application/json", "content-security-policy": "default-src 'self'" })
          .end('{"status":"stopped"}');
      } else {
        response.writeHead(200, { "content-type": "text/plain" }).end("ok");
      }
    });
    await new Promise<void>((resolve) => fly.listen(0, "127.0.0.1", resolve));
    flyOrigin = `http://127.0.0.1:${(fly.address() as AddressInfo).port}`;
    // A port nothing listens on: Fly unreachable.
    const probe = createServer();
    await new Promise<void>((resolve) => probe.listen(0, "127.0.0.1", resolve));
    closedOrigin = `http://127.0.0.1:${(probe.address() as AddressInfo).port}`;
    await new Promise((resolve) => probe.close(resolve));
  });

  afterAll(async () => {
    await new Promise((resolve) => fly.close(resolve));
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  const call = (path: string, init: { method?: string; headers?: Record<string, string> } = {}) =>
    (init.method === "POST" ? route.POST : route.GET)(
      new NextRequest(`http://localhost:3000${path}`, { method: init.method ?? "GET", headers: init.headers }),
    );

  it("answers the page when Fly can't be reached, and JSON on the API", async () => {
    vi.stubEnv("DIP_API_ORIGIN", closedOrigin);
    vi.stubEnv("DIP_PROXY_SECRET", SECRET);
    const page = await call("/login", { headers: { accept: "text/html" } });
    expect(page.status).toBe(502);
    expect(page.headers.get("content-type")).toBe("text/html; charset=utf-8");
    expect(await page.text()).toContain('name="viewport"');
    const api = await call("/api/v1/me", { headers: { accept: "application/json" } });
    expect(api.status).toBe(502);
    expect((await api.json()).error.code).toBe("unavailable");
  });

  it("answers 'not set up' when a setting is missing", async () => {
    vi.stubEnv("DIP_API_ORIGIN", "");
    const page = await call("/settings", { headers: { accept: "text/html" } });
    expect(page.status).toBe(503);
    expect(await page.text()).toContain("This site isn&#39;t set up yet");
  });

  it("replaces Fly's edge's error but passes the app's own on", async () => {
    vi.stubEnv("DIP_API_ORIGIN", flyOrigin);
    vi.stubEnv("DIP_PROXY_SECRET", SECRET);
    const edge = await call("/edge", { headers: { accept: "text/html" } });
    expect(edge.status).toBe(502);
    expect(await edge.text()).toContain("Try again");
    const health = await call("/healthz");
    expect(health.status).toBe(503);
    expect(await health.json()).toEqual({ status: "stopped" });
    const ok = await call("/anything");
    expect(await ok.text()).toBe("ok");
  });

  it("waits for Fly longer than a test alert can take, and Vercel runs it that long", () => {
    // notify.py USER_WEBHOOK_DEADLINE is 30 s per webhook; a member may have email, Telegram and a webhook.
    expect(UPSTREAM_TIMEOUT_MS).toBeGreaterThanOrEqual(90_000);
    expect(route.maxDuration * 1000).toBeGreaterThan(UPSTREAM_TIMEOUT_MS);
    expect(route.maxDuration).toBeLessThanOrEqual(300); // the Hobby plan's limit
  });
});
