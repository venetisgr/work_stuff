/**
 * The Content-Security-Policy and the other security headers of the pages Next.js renders (proxy.ts sets them), as
 * strict as the Fly app's own (dip_scanner/web/app.py): scripts and styles only from this site, and inline ones only
 * with the request's nonce (nextjs.org/docs/app/guides/content-security-policy). Fly's pages keep Fly's headers.
 */

export interface CspOptions {
  /** `next dev` needs 'unsafe-eval' for React's debugging and 'unsafe-inline' styles; never in production. */
  dev: boolean;
  /** Served over https: add upgrade-insecure-requests (and HSTS in securityHeaders). */
  https: boolean;
}

/** A fresh nonce: 18 random bytes, base64 (24 characters). */
export function makeNonce(): string {
  const bytes = new Uint8Array(18);
  crypto.getRandomValues(bytes);
  let text = "";
  for (const byte of bytes) text += String.fromCharCode(byte);
  return btoa(text);
}

export function contentSecurityPolicy(nonce: string, { dev, https }: CspOptions): string {
  const directives: [string, string][] = [
    ["default-src", "'self'"],
    ["script-src", `'self' 'nonce-${nonce}' 'strict-dynamic'${dev ? " 'unsafe-eval'" : ""}`],
    ["style-src", dev ? "'self' 'unsafe-inline'" : `'self' 'nonce-${nonce}'`],
    ["img-src", "'self' data:"],
    ["font-src", "'self'"],
    ["connect-src", "'self'"],
    ["object-src", "'none'"],
    ["base-uri", "'none'"],
    ["form-action", "'self'"],
    ["frame-ancestors", "'none'"],
  ];
  const policy = directives.map(([name, value]) => `${name} ${value}`);
  if (https) policy.push("upgrade-insecure-requests");
  return policy.join("; ");
}

/** The Fly app's SECURITY_HEADERS (without the CSP) and HSTS over https. */
export function securityHeaders({ https }: { https: boolean }): Record<string, string> {
  const headers: Record<string, string> = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
  };
  if (https) headers["Strict-Transport-Security"] = "max-age=31536000";
  return headers;
}
