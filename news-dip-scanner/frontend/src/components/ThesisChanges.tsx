/** "Thesis changes": newer analyses that undercut an idea that passed the reader's rules (review open orders). */
import Link from "next/link";
import { formatPercent, formatPrice, formatScore } from "@/lib/format";
import type { ThesisChange } from "@/lib/types";
import { TimeAgo, VerdictBadge } from "./ui";

export default function ThesisChanges({
  changes,
  now,
  timeZone,
}: {
  changes: ThesisChange[];
  now: number;
  timeZone: string;
}) {
  if (!changes.length) return null;
  return (
    <section className="card mb-4 border-l-4 border-l-warn" aria-labelledby="changes-title">
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
        <h2 id="changes-title" className="m-0">
          Thesis changes
        </h2>
        <span className="badge badge-warn">Review open orders</span>
      </div>
      <p className="mb-3 text-sm text-muted">
        A newer analysis undercuts an idea that passed your alert rules. If you placed orders on the earlier idea,
        check them.
      </p>
      <ul className="m-0 list-none divide-y divide-line p-0">
        {changes.map((change) => (
          <li key={`${change.current.id}-${change.previous.id}`} className="py-3 first:pt-0 last:pb-0">
            <p className="m-0 mb-0.5">
              <Link className="ticker" href={`/ideas/${change.current.id}`}>
                {change.ticker}
              </Link>{" "}
              {change.reason}: now <VerdictBadge verdict={change.current.verdict} label={change.current.verdict_label} />{" "}
              <span className="num">{formatPercent(change.current.probability_up_6m)}</span> chance up, score{" "}
              <span className="num">{formatScore(change.current.score)}</span>.
            </p>
            <p className="m-0 flex flex-wrap gap-x-3 gap-y-1 text-[0.8125rem] text-muted">
              <span>
                Was {change.previous.verdict_label}, {formatPercent(change.previous.probability_up_6m)}, entry{" "}
                {formatPrice(change.previous.entry.amount, change.previous.currency)}, target{" "}
                {formatPrice(change.previous.target.amount, change.previous.currency)}
              </span>
              <span>
                analysed <TimeAgo iso={change.current.created} now={now} timeZone={timeZone} />
              </span>
              <Link href={`/ideas/${change.previous.id}`}>the earlier idea</Link>
            </p>
          </li>
        ))}
      </ul>
    </section>
  );
}
