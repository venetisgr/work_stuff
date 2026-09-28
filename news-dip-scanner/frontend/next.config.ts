import type { NextConfig } from "next";

/**
 * The pages Next.js renders (/ and /ideas/<id>) get their CSP and security headers from src/proxy.ts; every other
 * path is the Fly app's (src/app/[...path]/route.ts) and keeps Fly's headers. Next.js's own files get nosniff here;
 * public/ holds only the home-screen icon.
 */
const nextConfig: NextConfig = {
  poweredByHeader: false,
  // next dev writes AGENTS.md/CLAUDE.md when it detects a coding agent; this project documents itself (README.md)
  agentRules: false,
  reactStrictMode: true,
  async rewrites() {
    // Safari asks for these by itself (on Fly's pages too): the icon in public/, not a 404 from Fly.
    return [{ source: "/apple-touch-icon-precomposed.png", destination: "/apple-touch-icon.png" }];
  },
  async headers() {
    return [
      {
        source: "/_next/:path*",
        headers: [{ key: "X-Content-Type-Options", value: "nosniff" }],
      },
      ...["/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"].map((source) => ({
        source,
        headers: [{ key: "X-Content-Type-Options", value: "nosniff" }],
      })),
    ];
  },
};

export default nextConfig;
