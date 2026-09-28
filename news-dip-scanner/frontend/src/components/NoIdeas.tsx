/**
 * The ideas page with nothing to list. When the last 30 days had ideas, a button shows them; when they had none (a
 * new site, or a quiet month) it says instead when ideas appear and what the scanner is doing, so "working, nothing
 * found yet" doesn't look like "broken".
 */
import Link from "next/link";
import { cycleFindings, plural, usSessionHours } from "@/lib/format";
import type { Status } from "@/lib/types";
import { EmptyState, TimeAgo } from "./ui";

export default function NoIdeas({
  days,
  monthHasIdeas,
  status,
  now,
  timeZone,
}: {
  days: number;
  /** Whether the last 30 days had any idea (false when days is already 30 and there are none). */
  monthHasIdeas: boolean;
  status: Status;
  now: number;
  timeZone: string;
}) {
  const title = `No ideas in the last ${plural(days, "day")}`;
  if (monthHasIdeas) {
    return (
      <EmptyState
        title={title}
        action={
          <Link className="btn btn-secondary" href="/?days=30">
            Show the last 30 days
          </Link>
        }
      >
        The scanner lists every dip it analyses here; it checks the news every {status.interval_minutes} minutes. Look
        up a stock to analyse one yourself.
      </EmptyState>
    );
  }
  const last = status.last_cycle;
  return (
    <EmptyState title={title}>
      <p className="m-0">
        An idea appears when a stock in the news falls sharply during its trading session and the models analyse it.
        For US stocks that is {usSessionHours(now, timeZone)}, so hours can go by without one. The scanner checks the
        news every {status.interval_minutes} minutes.
      </p>
      <p className="mb-0 mt-2">
        {status.cycle_running_since ? (
          <>
            A cycle is running now (it started <TimeAgo iso={status.cycle_running_since} now={now} timeZone={timeZone} />
            ).
          </>
        ) : last ? (
          <>
            Last cycle <TimeAgo iso={last.finished ?? last.started} now={now} timeZone={timeZone} />:{" "}
            {cycleFindings(last.summary)}.
          </>
        ) : (
          "The first cycle hasn't finished yet: it reads the last day's news first, so it takes longer than the others."
        )}
      </p>
      <p className="mb-0 mt-2">Look up a stock below to analyse one yourself.</p>
    </EmptyState>
  );
}
