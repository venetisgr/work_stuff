/** The scanner's state in one line (the Fly app's ui.status_strip macro). */
import { formatCount, formatTokens, plural } from "@/lib/format";
import type { Status } from "@/lib/types";
import { TimeAgo } from "./ui";

export default function StatusStrip({ status, now, timeZone }: { status: Status; now: number; timeZone: string }) {
  const problem = status.state === "stopped" || status.state === "stalled";
  const running = status.cycle_running_since;
  return (
    <div
      className="mb-4 flex flex-wrap items-center gap-x-4 gap-y-1 rounded-[12px] border border-line bg-surface px-4 py-3 text-sm text-muted"
      role="status"
    >
      <span className="inline-flex items-center gap-2">
        <span className={`status-dot is-${status.state}`} aria-hidden="true" />
        <strong className="text-ink">{status.label}</strong>
      </span>
      {running ? (
        <span className="inline-flex items-center gap-2">
          <span className="spinner" aria-hidden="true" />
          Cycle running since <TimeAgo iso={running} now={now} timeZone={timeZone} />
        </span>
      ) : status.next_cycle_at && Date.parse(status.next_cycle_at) <= now ? (
        // due (the page's clock is the API's): "Next cycle just now" would read as if it had run
        status.state === "running" ? <span>Next cycle starting</span> : null
      ) : status.next_cycle_at && (status.state === "running" || status.state === "paused") ? (
        <span>
          Next cycle <TimeAgo iso={status.next_cycle_at} now={now} timeZone={timeZone} />
        </span>
      ) : null}
      {status.last_cycle ? (
        <span>
          Last cycle <TimeAgo iso={status.last_cycle.finished ?? status.last_cycle.started} now={now} timeZone={timeZone} />
          {status.last_cycle.ok ? "" : " (failed)"}
        </span>
      ) : (
        <span>No cycle yet</span>
      )}
      {status.feeds ? (
        <span className="num">
          {status.feeds.ok}/{status.feeds.total} feeds ok
        </span>
      ) : null}
      <span className="num" title={`${formatCount(status.model_today.input_tokens)} tokens in, ${formatCount(status.model_today.output_tokens)} out since 00:00 UTC`}>
        {plural(status.model_today.calls, "model call")} today ·{" "}
        {formatTokens(status.model_today.input_tokens + status.model_today.output_tokens)} tokens
      </span>
      {status.reason ? <span className={`basis-full ${problem ? "text-bad" : ""}`}>{status.reason}</span> : null}
    </div>
  );
}
