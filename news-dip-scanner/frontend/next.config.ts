import type { NextConfig } from "next";

/**
 * The pages Next.js renders (/ and /ideas/<id>) get their CSP and security headers from src/proxy.ts; every other
 * path is the Fly app's (src/app/[...path]/route.ts) and keeps Fly's headers. Next.js's own files get nosniff here.
 */
const nextConfig: NextConfig = {
  poweredByHeader: false,
  // next dev writes AGENTS.md/CLAUDE.md when it detects a coding agent; this project documents itself (README.md)
  agentRules: false,
  reactStrictMode: true,
  async headers() {
    return [
      {
        source: "/_next/:path*",
        headers: [{ key: "X-Content-Type-Options", value: "nosniff" }],
      },
    ];
  },
};

export default nextConfig;
