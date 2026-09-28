"use client";

/**
 * "Analyse again": POST /api/v1/ideas/<id>/reanalyse with the session's CSRF token (X-CSRF-Token), then poll the
 * job (GET /api/v1/jobs/<id>) every few seconds and open the new idea when it is done. Both requests go to this
 * site and reach Fly through the proxy, with the session cookie.
 *
 * Without JavaScript the button submits the Fly app's own "Analyse now" form (POST /analyze), which shows the job's
 * page instead: the same analysis, a full page at a time.
 */
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { errorFor } from "@/lib/api-core";
import { loginUrl } from "@/lib/session";
import type { Job, ReanalyseAccepted } from "@/lib/types";

const POLL_MS = 3000;
const GIVE_UP_MS = 10 * 60 * 1000;

type State =
  | { kind: "idle" }
  | { kind: "starting" }
  | { kind: "waiting"; job: Job | null; jobId: number }
  | { kind: "failed"; message: string };

export default function AnalyseAgain({
  ideaId,
  ticker,
  csrf,
  available,
  note,
  remaining,
}: {
  ideaId: number;
  ticker: string;
  csrf: string;
  available: boolean;
  note: string | null;
  remaining: number | null;
}) {
  const router = useRouter();
  const [state, setState] = useState<State>({ kind: "idle" });
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const started = useRef(0);
  const here = `/ideas/${ideaId}`;

  useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current);
  }, []);

  async function failure(response: Response): Promise<string> {
    if (response.status === 401) {
      window.location.assign(loginUrl(here));
      return "Sign in to continue.";
    }
    let body: unknown = null;
    try {
      body = await response.json();
    } catch {
      body = null;
    }
    return errorFor(response.status, body).message;
  }

  async function poll(jobId: number) {
    try {
      const response = await fetch(`/api/v1/jobs/${jobId}`, {
        headers: { accept: "application/json" },
        credentials: "same-origin",
        cache: "no-store",
      });
      if (!response.ok) {
        setState({ kind: "failed", message: await failure(response) });
        return;
      }
      const job = (await response.json()) as Job;
      if (job.status === "done" && job.opportunity_id) {
        if (job.opportunity_id === ideaId) {
          setState({ kind: "idle" }); // the same idea (it can't be analysed again yet): show it afresh
          router.refresh();
        } else {
          setState({ kind: "waiting", job, jobId });
          router.push(`/ideas/${job.opportunity_id}`);
        }
        return;
      }
      if (job.status === "failed" || job.status === "done") {
        setState({ kind: "failed", message: job.error ?? "The analysis didn't finish. No reason was recorded." });
        return;
      }
      setState({ kind: "waiting", job, jobId });
    } catch {
      // a dropped connection on a phone: keep trying until GIVE_UP_MS
    }
    if (Date.now() - started.current > GIVE_UP_MS) {
      setState({ kind: "failed", message: "The analysis is taking unusually long. Reload the page to see if it finished." });
      return;
    }
    timer.current = setTimeout(() => poll(jobId), POLL_MS);
  }

  async function start(event: FormEvent) {
    event.preventDefault();
    setState({ kind: "starting" });
    started.current = Date.now();
    try {
      const response = await fetch(`/api/v1/ideas/${ideaId}/reanalyse`, {
        method: "POST",
        headers: { accept: "application/json", "x-csrf-token": csrf },
        credentials: "same-origin",
        cache: "no-store",
      });
      if (response.status !== 202 && !response.ok) {
        setState({ kind: "failed", message: await failure(response) });
        return;
      }
      const accepted = (await response.json()) as ReanalyseAccepted;
      setState({ kind: "waiting", job: null, jobId: accepted.job_id });
      timer.current = setTimeout(() => poll(accepted.job_id), POLL_MS);
    } catch {
      setState({ kind: "failed", message: "The scanner's server can't be reached just now. Try again in a moment." });
    }
  }

  if (!available) {
    return <p className="m-0 text-sm text-muted">{note ?? "Manual analyses aren't available."}</p>;
  }

  const busy = state.kind === "starting" || state.kind === "waiting";
  const job = state.kind === "waiting" ? state.job : null;
  return (
    <div className="flex flex-col gap-2">
      <form method="post" action="/analyze" onSubmit={start}>
        <input type="hidden" name="csrf_token" value={csrf} />
        <input type="hidden" name="ticker" value={ticker} />
        <input type="hidden" name="next" value={here} />
        <button className="btn btn-primary" type="submit" disabled={busy} aria-describedby="analyse-status">
          {busy ? <span className="spinner" aria-hidden="true" /> : null}
          {state.kind === "failed" ? "Try again" : "Analyse again now"}
        </button>
      </form>
      <p id="analyse-status" className="m-0 text-sm text-muted" role="status" aria-live="polite">
        {state.kind === "starting" ? "Starting…" : null}
        {state.kind === "waiting"
          ? job?.status === "running"
            ? "Analysing now: fetching prices and headlines and asking the models. This usually takes under a minute."
            : job?.status === "done"
              ? "Done. Opening the new analysis…"
              : `Waiting to start${job?.ahead ? ` (${job.ahead} ahead)` : ""}. Analyses run one at a time.`
          : null}
        {state.kind === "idle" && remaining !== null
          ? `You have ${remaining} manual ${remaining === 1 ? "analysis" : "analyses"} left in the last 24 hours.`
          : null}
      </p>
      {state.kind === "failed" ? (
        <p className="callout callout-bad m-0 text-sm" role="alert">
          <strong>The analysis didn&apos;t start or finish.</strong> {state.message}
        </p>
      ) : null}
    </div>
  );
}
