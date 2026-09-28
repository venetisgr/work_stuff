"use client";

/** Something went wrong while rendering a React page (never the details: they stay in the server's log). */
import Link from "next/link";
import { SiteShell } from "@/components/SiteShell";

export default function ErrorPage({ reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <SiteShell me={null} section={null}>
      <div className="card mx-auto mt-4 max-w-[440px]" role="alert">
        <p className="mb-2 text-sm font-bold uppercase tracking-[0.08em] text-muted">Error</p>
        <h1 className="mb-3 text-[1.375rem]">Something went wrong</h1>
        <p className="mb-4">The page couldn&apos;t be shown. Try again in a moment.</p>
        <div className="flex flex-wrap gap-3">
          <button className="btn btn-primary" type="button" onClick={() => reset()}>
            Try again
          </button>
          <Link className="btn btn-ghost" href="/">
            Back to the ideas
          </Link>
        </div>
      </div>
    </SiteShell>
  );
}
