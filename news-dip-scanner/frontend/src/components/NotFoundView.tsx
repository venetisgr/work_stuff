import Link from "next/link";
import type { Me } from "@/lib/types";
import { SiteShell } from "./SiteShell";

/** The React pages' 404, with the header's links and menu when the visitor is signed in, like the Fly app's 404: an
 * old link from an alert mustn't look as if they had been signed out. */
export default function NotFoundView({ me }: { me: Me | null }) {
  return (
    <SiteShell me={me} section={null}>
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
