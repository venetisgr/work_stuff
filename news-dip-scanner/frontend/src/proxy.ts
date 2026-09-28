/**
 * Runs before the pages Next.js renders itself (/ and /ideas/<id>; see config.matcher): a fresh CSP nonce for the
 * page (nextjs.org/docs/app/guides/content-security-policy), the security headers, no x-dip-* header from the
 * visitor, and the session cookie renewed like the Fly app renews it on each of its pages.
 *
 * Every other path is served by the catch-all route handler (src/app/[...path]/route.ts), which forwards it to Fly;
 * this function never runs for those, so Fly's pages keep Fly's own CSP.
 */
import { type NextRequest, NextResponse } from "next/server";
import { contentSecurityPolicy, makeNonce, securityHeaders } from "@/lib/csp";
import { stripPrivateHeaders } from "@/lib/forward";
import { renewedSessionCookie } from "@/lib/session";

export function proxy(request: NextRequest) {
  const https = request.nextUrl.protocol === "https:" || request.headers.get("x-forwarded-proto") === "https";
  const nonce = makeNonce();
  const csp = contentSecurityPolicy(nonce, { dev: process.env.NODE_ENV === "development", https });

  const requestHeaders = new Headers(request.headers);
  stripPrivateHeaders(requestHeaders);
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("content-security-policy", csp);

  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set("content-security-policy", csp);
  for (const [name, value] of Object.entries(securityHeaders({ https }))) response.headers.set(name, value);
  const cookie = renewedSessionCookie(request.cookies.get("dsid")?.value, { https });
  if (cookie) response.headers.append("set-cookie", cookie);
  return response;
}

export const config = {
  matcher: [
    // Exactly the React pages: "/" and "/ideas/<id>" (the regex keeps "/" from matching every path).
    { source: "/((?!.).*)", missing: [{ type: "header", key: "next-router-prefetch" }] },
    { source: "/ideas/:id", missing: [{ type: "header", key: "next-router-prefetch" }] },
  ],
};
