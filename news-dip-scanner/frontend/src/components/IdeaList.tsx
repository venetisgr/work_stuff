/**
 * The ideas of the dashboard: cards on a phone and a tablet, a table on a wide screen (both drawn; CSS shows one).
 * An idea that passes the reader's alert rules has an accent bar down its left edge and a ✓; ★ marks their
 * watchlist. The ticker's link stretches over the whole card or row. "Reported price" is the price in the analysis
 * (it may be days old); the percentages next to the entry are from the entry, what the two limit orders (buy at the
 * entry, sell at the target) would make, and how far the potential low is below the entry, as on the idea page.
 */
import Link from "next/link";
import { formatPercent } from "@/lib/format";
import type { DebateSummary, IdeaSummary } from "@/lib/types";
import { Money, Pct, RulesMarker, ScoreBadge, TimeAgo, VerdictBadge, WatchMarker } from "./ui";

interface ListProps {
  ideas: IdeaSummary[];
  now: number;
  timeZone: string;
  userCurrency: string | null;
}

const capitalize = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);

function approxCurrency(idea: IdeaSummary): string | null {
  return idea.fx?.currency ?? null;
}

function Markers({ idea }: { idea: IdeaSummary }) {
  return (
    <>
      {idea.on_my_watchlist ? <WatchMarker /> : null}
      {idea.matches_my_rules ? <RulesMarker /> : null}
    </>
  );
}

/** "GPT-5 70% · Claude 62% → 66%", with a small two-dot mark for "two models argued". */
export function DebateLine({ debate, className = "" }: { debate: DebateSummary; className?: string }) {
  return (
    <span className={`inline-flex min-w-0 items-start gap-1.5 text-[0.8125rem] leading-snug text-muted ${className}`}>
      <svg className="mt-0.5 size-3.5 flex-none" viewBox="0 0 14 14" aria-hidden="true" focusable="false">
        <circle cx="4.5" cy="7" r="3" className="fill-accent opacity-80" />
        <circle cx="9.5" cy="7" r="3" className="fill-none stroke-muted" strokeWidth="1.5" />
      </svg>
      <span className="min-w-0">
        <span className="visually-hidden">Models: </span>
        {debate.line}
      </span>
    </span>
  );
}

function IdeaCard({ idea, now, timeZone }: { idea: IdeaSummary; now: number; timeZone: string }) {
  const approx = approxCurrency(idea);
  return (
    <li
      className={`card relative flex flex-col gap-3 p-4 transition-colors hover:border-line-strong ${idea.matches_my_rules ? "matches-rules" : ""}`}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <ScoreBadge score={idea.score} band={idea.score_band} />
            <Link href={`/ideas/${idea.id}`} className="ticker stretched text-[1.0625rem] text-ink hover:no-underline">
              {idea.ticker}
            </Link>
            <Markers idea={idea} />
          </div>
          <p className="m-0 mt-1 truncate text-sm text-muted">{idea.company}</p>
        </div>
        <div className="flex-none text-right">
          <div className="text-[1.375rem] font-bold leading-none">{formatPercent(idea.probability_up_6m)}</div>
          <div className="mt-1 text-xs text-muted">up in 6 months</div>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <VerdictBadge verdict={idea.verdict} label={idea.verdict_label} />
        <span className="badge badge-outline">{capitalize(idea.confidence)} confidence</span>
        {idea.superseded ? <span className="badge badge-warn">Newer analysis</span> : null}
      </div>

      <dl className="m-0 grid grid-cols-3 gap-2 border-t border-line pt-3 text-sm">
        <div>
          <dt className="text-xs text-muted">Reported price</dt>
          <dd className="m-0 font-semibold">
            <Money value={idea.price} currency={idea.currency} approxCurrency={approx} className="flex flex-col" />
          </dd>
        </div>
        <div>
          <dt className="text-xs text-muted">Entry</dt>
          <dd className="m-0 font-semibold">
            <Money value={idea.entry} currency={idea.currency} approxCurrency={approx} className="flex flex-col" />
          </dd>
        </div>
        <div>
          <dt className="text-xs text-muted">Entry to target</dt>
          <dd className="m-0 font-semibold">
            <Pct value={idea.entry_upside_pct} />
            <span className="block text-[0.8125rem] font-medium text-muted">
              low <Pct value={idea.entry_downside_pct} className="text-muted" />
            </span>
          </dd>
        </div>
      </dl>

      {idea.debate ? <DebateLine debate={idea.debate} /> : null}

      <div className="flex items-center justify-between gap-2 text-xs text-muted">
        <span>
          Analysed <TimeAgo iso={idea.created} now={now} timeZone={timeZone} />
        </span>
        {idea.analyses_count > 1 ? <span>{idea.analyses_count} analyses</span> : null}
      </div>
    </li>
  );
}

function IdeaTable({ ideas, now, timeZone }: ListProps) {
  return (
    <div className="card card-flush">
      <table className="w-full border-collapse text-[0.9375rem]">
        <thead className="bg-surface-2 text-left text-[0.8125rem] text-muted">
          <tr>
            <th scope="col" className="px-4 py-2.5 font-semibold">
              Score
            </th>
            <th scope="col" className="px-3 py-2.5 font-semibold">
              Stock
            </th>
            <th scope="col" className="px-3 py-2.5 font-semibold">
              Verdict
            </th>
            <th scope="col" className="px-3 py-2.5 text-right font-semibold">
              Chance up
            </th>
            <th scope="col" className="px-3 py-2.5 text-right font-semibold">
              Reported
            </th>
            <th scope="col" className="px-3 py-2.5 text-right font-semibold">
              Entry
            </th>
            <th scope="col" className="px-3 py-2.5 text-right font-semibold">
              Entry to target
            </th>
            <th scope="col" className="px-4 py-2.5 text-right font-semibold">
              Analysed
            </th>
          </tr>
        </thead>
        <tbody>
          {ideas.map((idea) => {
            const approx = approxCurrency(idea);
            return (
              <tr
                key={idea.id}
                className={`relative border-t border-line align-top transition-colors hover:bg-surface-2 ${idea.matches_my_rules ? "matches-rules" : ""}`}
              >
                <td className="px-4 py-3">
                  <ScoreBadge score={idea.score} band={idea.score_band} />
                </td>
                <td className="max-w-[18rem] px-3 py-3">
                  <div className="flex items-center gap-1.5">
                    <Link href={`/ideas/${idea.id}`} className="ticker stretched text-ink hover:no-underline">
                      {idea.ticker}
                    </Link>
                    <Markers idea={idea} />
                  </div>
                  <div className="truncate text-sm text-muted">{idea.company}</div>
                  {idea.debate ? <DebateLine debate={idea.debate} className="mt-1 max-w-full" /> : null}
                </td>
                <td className="px-3 py-3">
                  <VerdictBadge verdict={idea.verdict} label={idea.verdict_label} />
                  <div className="mt-1 text-[0.8125rem] text-muted">{capitalize(idea.confidence)} confidence</div>
                </td>
                <td className="px-3 py-3 text-right">
                  <span className="text-[1.0625rem] font-bold">{formatPercent(idea.probability_up_6m)}</span>
                </td>
                <td className="px-3 py-3 text-right">
                  <Money value={idea.price} currency={idea.currency} approxCurrency={approx} stacked />
                </td>
                <td className="px-3 py-3 text-right">
                  <Money value={idea.entry} currency={idea.currency} approxCurrency={approx} stacked />
                </td>
                <td className="px-3 py-3 text-right">
                  <Pct value={idea.entry_upside_pct} className="font-semibold" />
                  <div className="text-[0.8125rem] text-muted">
                    low <Pct value={idea.entry_downside_pct} className="text-muted" />
                  </div>
                </td>
                <td className="whitespace-nowrap px-4 py-3 text-right text-sm text-muted">
                  <TimeAgo iso={idea.created} now={now} timeZone={timeZone} />
                  {idea.analyses_count > 1 ? <div className="text-[0.8125rem]">{idea.analyses_count} analyses</div> : null}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export default function IdeaList(props: ListProps) {
  return (
    <>
      <ul className="m-0 grid list-none grid-cols-1 gap-3 p-0 md:grid-cols-2 lg:hidden">
        {props.ideas.map((idea) => (
          <IdeaCard key={idea.id} idea={idea} now={props.now} timeZone={props.timeZone} />
        ))}
      </ul>
      <div className="hidden lg:block">
        <IdeaTable {...props} />
      </div>
    </>
  );
}
