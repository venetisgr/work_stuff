/**
 * The Fly app answers only the front door: without the proxy secret every path but /healthz is refused, so its
 * *.fly.dev address is useless on its own. Through the front door the same paths work.
 */
import { expect, test } from "@playwright/test";
import { settings } from "./helpers";

test.describe("the Fly app without the proxy secret", () => {
  for (const path of ["/login", "/", "/ideas/1", "/settings", "/static/app.css", "/admin"]) {
    test(`refuses GET ${path}`, async ({ request }) => {
      const response = await request.get(`${settings.flyOrigin}${path}`, { maxRedirects: 0 });
      expect(response.status()).toBe(403);
      expect(response.headers()["cache-control"]).toBe("no-store");
      expect(response.headers()["set-cookie"]).toBeUndefined();
      const body = await response.text();
      expect(body).toContain(`href="${settings.baseURL}`); // points people at the front door
    });
  }

  test("refuses the JSON API with a JSON error", async ({ request }) => {
    const response = await request.get(`${settings.flyOrigin}/api/v1/me`);
    expect(response.status()).toBe(403);
    expect((await response.json()).error.code).toBe("forbidden");
  });

  test("refuses a POST before looking at it", async ({ request }) => {
    const response = await request.post(`${settings.flyOrigin}/login`, {
      form: { email: settings.email, password: settings.password, csrf_token: "x" },
      maxRedirects: 0,
    });
    expect(response.status()).toBe(403);
  });

  test("refuses a wrong secret", async ({ request }) => {
    const response = await request.get(`${settings.flyOrigin}/login`, {
      headers: { "x-dip-proxy-secret": "not-the-secret-not-the-secret-not-the-secret" },
    });
    expect(response.status()).toBe(403);
  });

  test("keeps /healthz open for Fly's health check", async ({ request }) => {
    const response = await request.get(`${settings.flyOrigin}/healthz`);
    expect(response.status()).toBe(200);
    expect((await response.json()).status).toBe("ok");
  });
});

test.describe("through the front door", () => {
  test("the Fly pages and the API answer", async ({ request }) => {
    const login = await request.get("/login");
    expect(login.status()).toBe(200);
    expect(await login.text()).toContain("Sign in");
    const css = await request.get("/static/app.css");
    expect(css.status()).toBe(200);
    expect(css.headers()["content-type"]).toContain("text/css");
    const api = await request.get("/api/v1/me");
    expect(api.status()).toBe(401);
    expect((await api.json()).error.code).toBe("not_signed_in");
  });

  test("a visitor can't pose as the proxy", async ({ request }) => {
    // The front end drops every x-dip-* header from the visitor, so a forged secret changes nothing either way.
    const response = await request.get("/api/v1/me", {
      headers: { "x-dip-proxy-secret": "forged", "x-dip-client-ip": "203.0.113.9" },
    });
    expect(response.status()).toBe(401);
  });

  test("a React page redirects to the sign-in page with a relative Location", async ({ request }) => {
    const response = await request.get("/ideas/1", { maxRedirects: 0 });
    expect(response.status()).toBe(307);
    expect(response.headers()["location"]).toBe("/login?next=%2Fideas%2F1");
  });
});
