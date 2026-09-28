/**
 * The header, main area and footer of every React page, like the Fly app's base.html so both halves are one site.
 * Links to pages the Fly app serves are plain <a> (a full page load through the proxy); signing out is Fly's
 * POST /logout form with the session's CSRF token, which works without JavaScript.
 */
import Link from "next/link";
import type { ReactNode } from "react";
import type { Me } from "@/lib/types";

export const APP_NAME = "Dip scanner";
export const DISCLAIMER = "Not investment advice. The scanner never trades.";

type Section = "ideas" | "news" | "track" | "settings" | "admin" | null;

export function BrandMark({ className = "" }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 32 32" aria-hidden="true" focusable="false">
      <rect width="32" height="32" rx="7" fill="#0b5cad" />
      <path
        d="M5 10 L12 13 L16 23 L21 16 L27 12"
        fill="none"
        stroke="#ffffff"
        strokeWidth="3"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <circle cx="16" cy="23" r="2.6" fill="#ffd166" />
    </svg>
  );
}

function NavLink({ href, current, children }: { href: string; current: boolean; children: ReactNode }) {
  // The ideas are this app's own page (client-side navigation); every other section is Fly's (a plain link).
  const props = current ? { "aria-current": "page" as const } : {};
  return href === "/" ? (
    <Link href="/" {...props}>
      {children}
    </Link>
  ) : (
    <a href={href} {...props}>
      {children}
    </a>
  );
}

export function SiteHeader({ me, section }: { me: Me | null; section: Section }) {
  return (
    <header className="border-b border-line bg-surface">
      <div className="container-page flex min-h-14 flex-wrap items-center gap-x-4 gap-y-2 min-[800px]:flex-nowrap">
        <Link
          href="/"
          className="mr-auto inline-flex min-h-10 items-center gap-2 text-[1.0625rem] font-bold tracking-[-0.01em] text-ink hover:no-underline min-[800px]:mr-3"
        >
          <BrandMark className="size-7 flex-none" />
          <span>{APP_NAME}</span>
        </Link>
        {me ? (
          <>
            <nav
              className="nav order-3 -mx-2 flex flex-[1_0_100%] gap-1 overflow-x-auto px-2 pb-2 max-[479.98px]:justify-between max-[479.98px]:gap-0 min-[800px]:order-none min-[800px]:m-0 min-[800px]:flex-[0_1_auto] min-[800px]:p-0"
              aria-label="Main"
            >
              <NavLink href="/" current={section === "ideas"}>
                Ideas
              </NavLink>
              <NavLink href="/news" current={section === "news"}>
                News
              </NavLink>
              <NavLink href="/track" current={section === "track"}>
                Track record
              </NavLink>
              <NavLink href="/settings" current={section === "settings"}>
                Settings
              </NavLink>
              {me.capabilities.admin ? (
                <NavLink href="/admin" current={section === "admin"}>
                  Admin
                </NavLink>
              ) : null}
            </nav>
            <UserMenu me={me} />
          </>
        ) : null}
      </div>
    </header>
  );
}

function UserMenu({ me }: { me: Me }) {
  return (
    <details className="menu relative min-[800px]:ml-auto">
      <summary className="inline-flex min-h-10 max-w-56 cursor-pointer items-center gap-2 rounded-full border border-line px-2 text-[0.9375rem] font-[550] hover:bg-surface-2 min-[480px]:px-3 min-[960px]:max-w-80">
        <span
          className="inline-grid size-6 flex-none place-items-center rounded-full bg-accent-soft text-xs font-bold text-accent"
          aria-hidden="true"
        >
          {me.user.label.slice(0, 1).toUpperCase()}
        </span>
        <span className="truncate max-[479.98px]:sr-only">{me.user.label}</span>
      </summary>
      <div className="menu-panel absolute right-0 top-[calc(100%+6px)] z-20 min-w-[220px] rounded-[12px] border border-line bg-surface p-2 shadow-pop">
        <div className="mb-1 border-b border-line px-3 py-2 text-[0.8125rem] text-muted [overflow-wrap:anywhere]">
          Signed in as {me.user.email}
          {me.user.role === "admin" ? " (admin)" : ""}
        </div>
        <a href="/settings">Settings</a>
        <form method="post" action="/logout">
          <input type="hidden" name="csrf_token" value={me.csrf} />
          <button type="submit">Sign out</button>
        </form>
      </div>
    </details>
  );
}

export function SiteFooter({ timeZone }: { timeZone: string | null }) {
  return (
    <footer className="flex-none border-t border-line bg-surface pb-[max(16px,env(safe-area-inset-bottom))] pt-4 text-sm text-muted">
      <div className="container-page">
        <p className="m-0 font-semibold text-ink">{DISCLAIMER}</p>
        <p className="m-0">
          A language model rates news-driven price dips; its chances and scores are estimates, not promises. Do your
          own checks before you buy or sell anything.{timeZone ? ` Times in ${timeZone}.` : ""}
        </p>
      </div>
    </footer>
  );
}

export function SiteShell({
  me,
  section,
  children,
}: {
  me: Me | null;
  section: Section;
  children: ReactNode;
}) {
  return (
    <>
      <a className="skip-link" href="#main">
        Skip to the content
      </a>
      <SiteHeader me={me} section={section} />
      <main id="main" className="container-page flex-[1_0_auto] pb-8 pt-6">
        {children}
      </main>
      <SiteFooter timeZone={me?.settings.timezone ?? null} />
    </>
  );
}
