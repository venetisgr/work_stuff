/**
 * The Fly app's session cookie, dsid (dip_scanner/web/auth.py). The browser only talks to the Vercel domain, so the
 * cookie lives there; Fly renews it (30 days from the last visit) on every page it serves. The React pages are
 * rendered here, so proxy.ts renews it the same way: someone who only opens the ideas stays signed in.
 */

export const SESSION_COOKIE = "dsid";
/** auth.SESSION_LIFETIME: 30 days. */
export const SESSION_MAX_AGE_SECONDS = 30 * 24 * 60 * 60;

/** Set-Cookie for the same token with a fresh Max-Age, or null when there is no plausible token (Fly's tokens are
 * secrets.token_urlsafe(32): 43 characters of A-Z a-z 0-9 - _). */
export function renewedSessionCookie(token: string | undefined, { https }: { https: boolean }): string | null {
  if (!token || !/^[A-Za-z0-9_-]{20,128}$/.test(token)) return null;
  const secure = https ? "; Secure" : "";
  return `${SESSION_COOKIE}=${token}; Max-Age=${SESSION_MAX_AGE_SECONDS}; Path=/; HttpOnly; SameSite=Lax${secure}`;
}

/** Only a login redirect's own paths: "/", "/ideas/7?x=1"; anything else becomes "/". */
export function safeNextPath(path: string | null | undefined): string {
  if (!path || !path.startsWith("/") || path.startsWith("//") || path.includes("\\")) return "/";
  return path;
}

/** Where to sign in and come back: /login?next=%2Fideas%2F7 */
export function loginUrl(nextPath: string | null | undefined): string {
  return `/login?next=${encodeURIComponent(safeNextPath(nextPath))}`;
}
