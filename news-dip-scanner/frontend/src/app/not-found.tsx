import type { Metadata } from "next";
import Link from "next/link";
import { SiteShell } from "@/components/SiteShell";

export const metadata: Metadata = { title: "Not found" };

/** The not-found page of the React pages (an idea that doesn't exist); Fly answers for its own paths. */
export default function NotFound() {
  return (
    <SiteShell me={null} section={null}>
      <div className="card mx-auto mt-4 max-w-[440px]">
        <p className="mb-2 text-sm font-bold uppercase tracking-[0.08em] text-muted">404</p>
        <h1 className="mb-3 text-[1.375rem]">Page not found</h1>
        <p className="mb-4">There is no idea with that number. It may have been removed.</p>
        <Link className="btn btn-secondary" href="/">
          Back to the ideas
        </Link>
      </div>
    </SiteShell>
  );
}
