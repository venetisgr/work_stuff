/**
 * The cards of an idea's page (the Fly app's templates/pages/idea.html): levels, outcome so far, the analysis,
 * checks/risks/catalysts, why it was flagged with its headlines, and the timeline of the stock's analyses.
 */
import Link from "next/link";
import type { ReactNode } from "react";
import { formatDay, formatPercent, formatPrice, formatScore, formatWhen, plural } from "@/lib/format";
import type { HistoryItem, IdeaDetail, Level, Outcome } from "@/lib/types";
import { ExternalLink, Pct, PriceText, ScoreBadge, TimeAgo, VerdictBadge } from "./ui";

export function LevelsCard({ detail, className = "" }: { detail: IdeaDetail; className?: string }) {
  const { idea } = detail;
  const approx = idea.fx?.currency ?? null;
  return (
    <section className={`card ${className}`} aria-labelledby="levels-title">
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
        <h2 id="levels-title" className="m-0">
          Levels
        </h2>
        <span className="text-sm text-muted">limit-order ideas</span>
      </div>
      <ol className="m-0 mb-3 list-none p-0">
        {detail.levels.map((level) => (
          <LevelRow key={level.key} level={level} currency={idea.currency} approx={approx} dayChange={idea.change_1d_pct} />
        ))}
      </ol>
      <p className="mb-2 text-sm">
        From the entry: target <Pct value={idea.entry_upside_pct} />, potential low <Pct value={idea.entry_downside_pct} />,
        what the two limit orders would make or lose.
      </p>
      {detail.fx_note ? <p className="mb-2 text-sm text-muted">{detail.fx_note}.</p> : null}
      <p className="m-0 text-sm text-muted">
        The potential low is an estimate, not a floor; the statistical low is where the price is 1 in 20 times after 6
        months.
      </p>
    </section>
  );
}

function LevelRow({
  level,
  currency,
  approx,
  dayChange,
}: {
  level: Level;
  currency: string;
  approx: string | null;
  dayChange: number;
}) {
  const isPrice = level.key === "price";
  return (
    <li
      className={`grid grid-cols-[20px_minmax(0,1fr)_auto] items-center gap-x-3 gap-y-1 border-b border-line py-2.5 last:border-b-0 ${isPrice ? "-mx-2 rounded-[8px] border-b-0 bg-surface-2 px-2" : ""}`}
    >
      <span className={`level-key level-key-${level.key}`} aria-hidden="true" />
      <span className="flex min-w-0 flex-col">
        <span className="text-[0.9375rem] font-semibold">{level.label}</span>
        <span className="text-[0.8125rem] text-muted">
          {level.change_pct !== null ? (
            <>
              <Pct value={level.change_pct} /> from the report
            </>
          ) : (
            <>
              <Pct value={dayChange} /> on the day
            </>
          )}
        </span>
      </span>
      <span className="flex flex-col items-end">
        <span className="num text-[1.0625rem] font-bold">{formatPrice(level.value.amount, currency)}</span>
        {approx && level.value.approx !== null ? (
          <span className="num approx text-[0.8125rem]">≈ {formatPrice(level.value.approx, approx)}</span>
        ) : null}
      </span>
    </li>
  );
}

function Kv({ label, children, note }: { label: ReactNode; children: ReactNode; note?: ReactNode }) {
  return (
    <div>
      <dt className="mb-0.5 text-[0.8125rem] text-muted">{label}</dt>
      <dd className="m-0 font-semibold">
        {children}
        {note ? <span className="block text-[0.8125rem] font-normal text-muted">{note}</span> : null}
      </dd>
    </div>
  );
}

const STATUS_BADGES: Record<Outcome["status"], string> = {
  target_hit: "badge-ok",
  below_low: "badge-bad",
  open: "badge-info",
  waiting_entry: "badge-outline",
  expired: "badge-outline",
};

export function OutcomeCard({ detail, className = "" }: { detail: IdeaDetail; className?: string }) {
  const outcome = detail.outcome;
  const currency = detail.idea.currency;
  const measured = outcome && outcome.priced && !outcome.price_mismatch;
  return (
    <section className={`card ${className}`} aria-labelledby="outcome-title">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <h2 id="outcome-title" className="m-0">
          Outcome so far
        </h2>
        {measured ? (
          <span className={`badge ${STATUS_BADGES[outcome.status]}`}>
            {outcome.status_label[0].toUpperCase() + outcome.status_label.slice(1)}
          </span>
        ) : null}
      </div>
      {!outcome ? (
        <p className="text-muted">{detail.prices_problem ?? "No prices to measure it with."}</p>
      ) : outcome.price_mismatch ? (
        <p className="callout text-sm">
          The price in the report doesn&apos;t match Yahoo Finance&apos;s price history (probably a split Yahoo
          doesn&apos;t report), so the outcome can&apos;t be measured.
        </p>
      ) : !outcome.priced ? (
        <p className="text-muted">Nothing has traded since the report yet: the outcome starts with the next session.</p>
      ) : (
        <>
          <dl className="m-0 mb-3 grid grid-cols-2 gap-x-4 gap-y-3 min-[480px]:grid-cols-3 lg:grid-cols-2">
            <Kv label="Last close" note={formatDay(outcome.last_day)}>
              <PriceText text={formatPrice(outcome.last_price, currency)} />
            </Kv>
            <Kv label="Since the report">
              <Pct value={outcome.return_pct} />
            </Kv>
            {outcome.account_currency ? (
              <Kv label={`In ${outcome.account_currency}`}>
                <Pct value={outcome.account_return_pct} />
              </Kv>
            ) : null}
            <Kv label={<abbr title={outcome.benchmark ?? undefined}>{outcome.benchmark_name ?? "The index"}</abbr>}>
              <Pct value={outcome.benchmark_return_pct} />
              <span className="font-normal text-muted"> meanwhile</span>
            </Kv>
            <Kv label="Versus the index">
              <Pct value={outcome.excess_return_pct} />
            </Kv>
            <Kv label="Entry filled">{outcome.entry_filled ? formatDay(outcome.entry_filled) : "Not yet"}</Kv>
            <Kv label="Target hit">
              {outcome.target_hit ? formatDay(outcome.target_hit) : outcome.entry_filled ? "Not yet" : "–"}
            </Kv>
            <Kv label="Below the potential low">{outcome.low_breached ? formatDay(outcome.low_breached) : "No"}</Kv>
            <Kv label="Best and worst since">
              <Pct value={outcome.max_gain_pct} /> / <Pct value={outcome.max_loss_pct} />
            </Kv>
            {outcome.trade_return_pct !== null ? (
              <Kv label="The limit orders">
                <Pct value={outcome.trade_return_pct} />
              </Kv>
            ) : null}
            {outcome.up_after_6m !== null ? (
              <Kv label="Higher after 6 months">{outcome.up_after_6m ? "Yes" : "No"}</Kv>
            ) : null}
          </dl>
          <p className="mb-0 text-sm text-muted">
            Day {outcome.days} of the 6-month idea. Returns from the price in the report; the limit orders from the
            entry to the target (when hit) or the last close.
          </p>
        </>
      )}
      {detail.outcome_notes.map((note) => (
        <p key={note} className="mb-0 mt-2 text-sm text-muted">
          {note}
        </p>
      ))}
    </section>
  );
}

function Prose({ title, text }: { title: string; text: string }) {
  if (!text) return null;
  return (
    <>
      <h3 className="mb-1 mt-4 text-sm font-[650] uppercase tracking-[0.02em] text-muted first:mt-0">{title}</h3>
      <p className="m-0 max-w-[70ch]">{text}</p>
    </>
  );
}

export function AnalysisCard({ detail, timeZone, className = "" }: { detail: IdeaDetail; timeZone: string; className?: string }) {
  const a = detail.opportunity.analysis;
  // like the Fly idea page: a debate's outcome says so (a lone model's analysis is just "by" it)
  const debated = detail.debate !== null && detail.debate.mode !== "single";
  return (
    <section className={`card ${className}`} aria-labelledby="analysis-title">
      <h2 id="analysis-title" className="mb-3">
        The analysis
      </h2>
      <Prose title="What the market fears" text={a.fear} />
      <Prose title="Fundamental impact" text={a.fundamental_impact} />
      <Prose title="Thesis" text={a.thesis} />
      <p className="mb-0 mt-4 text-sm text-muted">
        {debated
          ? `The outcome of the debate above (${detail.opportunity.model})`
          : `By ${detail.opportunity.model || "an unknown model"}`}
        , with prices as of {formatWhen(detail.opportunity.stats.as_of, timeZone)}.{" "}
        {debated ? "Language models' reading" : "A language model's reading"} of the news: check it before you act.
      </p>
    </section>
  );
}

export function PointsCard({ detail, className = "" }: { detail: IdeaDetail; className?: string }) {
  const a = detail.opportunity.analysis;
  if (!a.risks.length && !a.catalysts.length && !a.checks.length) return null;
  const list = (items: string[], checklist = false) => (
    <ul className={`m-0 list-none space-y-1.5 p-0 ${checklist ? "checklist" : "bullets"}`}>
      {items.map((item, index) => (
        <li key={index}>{item}</li>
      ))}
    </ul>
  );
  return (
    <section className={`card grid gap-x-6 gap-y-4 sm:grid-cols-2 ${className}`} aria-labelledby="points-title">
      <h2 id="points-title" className="visually-hidden">
        Checks, risks and catalysts
      </h2>
      {a.checks.length ? (
        <div className="sm:col-span-2">
          <h3 className="mb-2">Check before buying</h3>
          {list(a.checks, true)}
        </div>
      ) : null}
      {a.risks.length ? (
        <div>
          <h3 className="mb-2">Risks</h3>
          {list(a.risks)}
        </div>
      ) : null}
      {a.catalysts.length ? (
        <div>
          <h3 className="mb-2">Catalysts</h3>
          {list(a.catalysts)}
        </div>
      ) : null}
    </section>
  );
}

const DIRECTION_CLASS: Record<string, string> = { negative: "text-down", positive: "text-up", mixed: "text-warn" };

export function FlaggedCard({
  detail,
  now,
  timeZone,
  className = "",
}: {
  detail: IdeaDetail;
  now: number;
  timeZone: string;
  className?: string;
}) {
  const opp = detail.opportunity;
  return (
    <section className={`card ${className}`} aria-labelledby="flagged-title">
      <h2 id="flagged-title" className="mb-3">
        Why it was flagged
      </h2>
      {opp.dip_reasons.length ? <p className="mb-3">The price: {opp.dip_reasons.join("; ")}.</p> : null}
      {opp.news_after_session ? (
        <p className="callout callout-info mb-3 text-sm">
          All of this news came out after the last session in the price data: the price hadn&apos;t reacted to it yet
          when this was analysed.
        </p>
      ) : null}
      {opp.headlines.length ? (
        <>
          <h3 className="mb-2 text-[1.0625rem]">Headlines</h3>
          <ul className="m-0 list-none divide-y divide-line p-0">
            {opp.headlines.map((item, index) => (
              <li key={index} className="flex flex-col gap-0.5 py-3 first:pt-0 last:pb-0">
                <span className="font-[550] [overflow-wrap:anywhere]">
                  <ExternalLink href={item.link}>{item.title || "Untitled"}</ExternalLink>
                </span>
                <span className="flex flex-wrap gap-x-3 gap-y-1 text-[0.8125rem] text-muted">
                  {item.source ? <span>{item.source}</span> : null}
                  {item.published ? <TimeAgo iso={item.published} now={now} timeZone={timeZone} /> : null}
                  {item.direction ? (
                    <span className={`font-semibold ${DIRECTION_CLASS[item.direction] ?? ""}`}>
                      {item.direction}
                      {typeof item.magnitude === "number" ? ` ${item.magnitude}/5` : ""}
                    </span>
                  ) : null}
                </span>
              </li>
            ))}
          </ul>
        </>
      ) : !opp.dip_reasons.length ? (
        <p className="m-0">
          Nothing flagged it: this analysis was asked for by hand (&quot;Analyse now&quot;), and the price wasn&apos;t
          down by the scanner&apos;s rules.
        </p>
      ) : (
        <p className="m-0 text-sm text-muted">No headlines were stored with this analysis.</p>
      )}
      {opp.analysis.warnings.length ? (
        <div className="callout mt-3 text-sm">
          <p className="m-0 font-semibold">Numbers fixed after the analysis</p>
          <ul className="bullets m-0 mt-1 list-none space-y-1 p-0">
            {opp.analysis.warnings.map((warning, index) => (
              <li key={index}>{warning}</li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}

export function HistoryCard({
  ticker,
  history,
  now,
  timeZone,
  className = "",
}: {
  ticker: string;
  history: HistoryItem[];
  now: number;
  timeZone: string;
  className?: string;
}) {
  return (
    <section className={`card card-flush ${className}`} aria-labelledby="history-title">
      <div className="flex flex-wrap items-baseline justify-between gap-2 px-4 pt-4 sm:px-6 sm:pt-6">
        <h2 id="history-title" className="m-0">
          Analyses of <span className="ticker">{ticker}</span>
        </h2>
        <span className="text-sm text-muted">{plural(history.length, "analysis", "analyses")}</span>
      </div>
      <ol className="m-0 mt-2 list-none p-0">
        {history.map((item) => (
          <li
            key={item.id}
            className={`relative grid grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-x-3 border-t border-line px-4 py-2.5 sm:px-6 ${item.current ? "bg-accent-soft" : "hover:bg-surface-2"}`}
          >
            <ScoreBadge score={item.score} band={item.score_band} />
            <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
              {item.current ? (
                <span aria-current="page" className="text-sm font-semibold">
                  This analysis
                </span>
              ) : (
                <Link href={`/ideas/${item.id}`} className="stretched text-sm font-semibold">
                  <TimeAgo iso={item.created} now={now} timeZone={timeZone} />
                </Link>
              )}
              <VerdictBadge verdict={item.verdict} label={item.verdict_label} />
              {item.superseded && !item.current ? <span className="sr-only">(superseded)</span> : null}
            </span>
            <span className="text-right">
              <span className="block font-bold">{formatPercent(item.probability_up_6m)}</span>
              <span className="block text-xs text-muted" title={formatWhen(item.created, timeZone)}>
                {item.current ? <TimeAgo iso={item.created} now={now} timeZone={timeZone} /> : `score ${formatScore(item.score)}`}
              </span>
            </span>
          </li>
        ))}
      </ol>
    </section>
  );
}

