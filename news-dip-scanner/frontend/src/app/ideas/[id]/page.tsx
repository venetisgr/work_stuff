/**
 * One idea: the hero (verdict, score, chance up, price), its levels, the price chart, the debate between the
 * models, the analysis, checks, risks and catalysts, the headlines, the outcome so far and every analysis of the
 * stock. On a phone the cards line up in reading order (order-*); from 960px the analysis runs on the left and the
 * levels, outcome and timeline on the right, like the Fly app's idea page.
 */
import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import AnalyseAgain from "@/components/AnalyseAgain";
import DebateCard from "@/components/DebateCard";
import { DebateLine } from "@/components/IdeaList";
import {
  AnalysisCard,
  FlaggedCard,
  HistoryCard,
  LevelsCard,
  OutcomeCard,
  PointsCard,
} from "@/components/IdeaSections";
import PriceChart from "@/components/PriceChart";
import { SiteShell } from "@/components/SiteShell";
import { Pct, PriceText, TimeAgo, VerdictBadge } from "@/components/ui";
import { getIdea, getMe } from "@/lib/api";
import { ApiRequestError } from "@/lib/api-core";
import { formatDay, formatPercent, formatPrice, formatScore, formatApprox, MONTHS } from "@/lib/format";
import type { IdeaDetail, Me } from "@/lib/types";

async function load(id: number, here: string) {
  try {
    const [me, detail] = await Promise.all([getMe(here), getIdea(id, here)]);
    return { me, detail, error: null };
  } catch (error) {
    if (error instanceof ApiRequestError) return { error };
    throw error;
  }
}

export async function generateMetadata({ params }: PageProps<"/ideas/[id]">): Promise<Metadata> {
  const { id } = await params;
  if (!/^\d{1,12}$/.test(id)) return { title: "Not found" };
  try {
    const detail = await getIdea(Number(id), `/ideas/${id}`); // the page's own request: cached, not fetched twice
    return { title: `${detail.idea.ticker}: ${detail.idea.verdict_label}` };
  } catch (error) {
    if (error instanceof ApiRequestError) return { title: "An idea" };
    throw error;
  }
}

export default async function IdeaPage({ params }: PageProps<"/ideas/[id]">) {
  const { id: raw } = await params;
  if (!/^\d{1,12}$/.test(raw)) notFound();
  const id = Number(raw);
  const here = `/ideas/${id}`;
  const data = await load(id, here);

  if (data.error) {
    return (
      <SiteShell me={null} section="ideas">
        <h1 className="mb-4">An idea</h1>
        <div className="callout callout-bad" role="alert">
          <p className="m-0">
            <strong>This idea couldn&apos;t be loaded.</strong> {data.error.message}
          </p>
        </div>
        <p className="mt-4 flex gap-3">
          <a className="btn btn-secondary" href={here}>
            Try again
          </a>
          <Link className="btn btn-ghost" href="/">
            Back to the ideas
          </Link>
        </p>
      </SiteShell>
    );
  }

  const { me, detail } = data;
  const now = Date.parse(detail.generated_at); // relative times from the API's clock
  const timeZone = me.settings.timezone;
  const { idea } = detail;
  const newer = idea.superseded ? detail.history.find((item) => item.id === idea.superseded_by) ?? detail.history[0] : null;

  return (
    <SiteShell me={me} section="ideas">
      {newer && newer.id !== idea.id ? (
        <div className="callout callout-info mb-4" role="note">
          <p className="m-0">
            <strong>There is a newer analysis of {idea.ticker}</strong> (
            <TimeAgo iso={newer.created} now={now} timeZone={timeZone} />
            ): {newer.verdict_label}, {formatPercent(newer.probability_up_6m)} chance up, score {formatScore(newer.score)}.
            If you placed orders on this idea, review them. <a href={`/ideas/${newer.id}`}>See the newest analysis</a>
          </p>
        </div>
      ) : null}

      <Hero detail={detail} me={me} now={now} />

      <div className="mt-4 flex flex-col gap-4 lg:grid lg:grid-cols-[minmax(0,1.75fr)_minmax(0,1fr)] lg:items-start lg:gap-6">
        <div className="contents lg:flex lg:min-w-0 lg:flex-col lg:gap-4">
          <ChartCard detail={detail} className="order-2 lg:order-none" />
          {detail.debate ? (
            <DebateCard
              debate={detail.debate}
              currency={idea.currency}
              final={detail.opportunity.analysis}
              className="order-3 lg:order-none"
            />
          ) : null}
          <AnalysisCard detail={detail} timeZone={timeZone} className="order-5 lg:order-none" />
          <PointsCard detail={detail} className="order-6 lg:order-none" />
          <FlaggedCard detail={detail} now={now} timeZone={timeZone} className="order-7 lg:order-none" />
        </div>
        <div className="contents lg:flex lg:min-w-0 lg:flex-col lg:gap-4">
          <LevelsCard detail={detail} className="order-1 lg:order-none" />
          <OutcomeCard detail={detail} className="order-4 lg:order-none" />
          <HistoryCard
            ticker={idea.ticker}
            history={detail.history}
            now={now}
            timeZone={timeZone}
            className="order-8 lg:order-none"
          />
        </div>
      </div>
    </SiteShell>
  );
}

function Hero({ detail, me, now }: { detail: IdeaDetail; me: Me; now: number }) {
  const { idea } = detail;
  const timeZone = me.settings.timezone;
  const approx = idea.fx ? formatApprox(idea.price.approx, idea.fx.currency) : "";
  const here = `/ideas/${idea.id}`;
  return (
    <header className="card flex flex-col gap-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="m-0 mb-1 flex flex-wrap items-baseline gap-x-2.5 gap-y-0.5">
            <span className="ticker text-[1.625rem]">{idea.ticker}</span>
            <span className="text-base font-medium tracking-normal text-muted">{idea.company}</span>
          </h1>
          <p className="m-0 flex flex-wrap gap-x-3 gap-y-1 text-[0.8125rem] text-muted">
            <span>
              Analysed <TimeAgo iso={idea.created} now={now} timeZone={timeZone} />
            </span>
            {idea.exchange ? <span>{idea.exchange}</span> : null}
            <span>{idea.currency}</span>
          </p>
        </div>
        <div
          className={`band-${idea.score_band} flex min-w-[78px] flex-none flex-col items-center rounded-[8px] px-3 py-2`}
          title={`Score ${formatScore(idea.score)} of 100`}
        >
          <span className="text-[1.625rem] font-[750] leading-[1.1]">{formatScore(idea.score)}</span>
          <span className="whitespace-nowrap text-[0.6875rem] font-[650] uppercase tracking-[0.04em]">
            score · {idea.score_band}
          </span>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <VerdictBadge verdict={idea.verdict} label={idea.verdict_label} />
        <span className="badge badge-outline">{idea.confidence[0].toUpperCase() + idea.confidence.slice(1)} confidence</span>
        {idea.matches_my_rules ? <span className="badge badge-info">Passes your alert rules</span> : null}
        {idea.on_my_watchlist ? (
          <span className="badge badge-outline">
            <span aria-hidden="true">★</span> On your watchlist
          </span>
        ) : null}
      </div>

      <div className="grid grid-cols-2 gap-x-4 gap-y-3 border-t border-line pt-4 sm:grid-cols-[minmax(0,1.3fr)_repeat(2,minmax(0,1fr))]">
        <div className="col-span-2 flex flex-col gap-0.5 sm:col-span-1">
          <span className="text-5xl font-bold leading-none tracking-[-0.02em]">{formatPercent(idea.probability_up_6m)}</span>
          <span className="text-[0.9375rem] font-[550]">chance of being higher in 6 months</span>
          {idea.debate ? <DebateLine debate={idea.debate} className="mt-1" /> : null}
        </div>
        <div className="flex min-w-0 flex-col gap-0.5">
          <span className="text-[0.8125rem] text-muted">Price at the analysis</span>
          <PriceText className="text-[1.375rem] font-bold" text={formatPrice(idea.price.amount, idea.currency)} />
          {approx ? <span className="num approx text-sm">{approx}</span> : null}
          <span className="text-sm">
            <span className="whitespace-nowrap">
              <Pct value={idea.change_1d_pct} /> 1 day
            </span>{" "}
            ·{" "}
            <span className="whitespace-nowrap">
              <Pct value={idea.change_5d_pct} /> 5 days
            </span>
          </span>
        </div>
        <div className="flex min-w-0 flex-col gap-0.5">
          <span className="text-[0.8125rem] text-muted">To the target</span>
          <span className="text-[1.375rem] font-bold">
            <Pct value={idea.upside_pct} />
          </span>
          <span className="text-sm">
            <Pct value={idea.downside_pct} /> to the potential low
          </span>
        </div>
      </div>

      {detail.rule_misses.length ? (
        <p className="m-0 text-sm text-muted">No alert under your rules: {detail.rule_misses.join("; ")}.</p>
      ) : null}

      <div className="flex flex-wrap items-start gap-3 pt-1">
        <AnalyseAgain
          key={idea.id}
          ideaId={idea.id}
          ticker={idea.ticker}
          csrf={me.csrf}
          available={me.capabilities.analyse.available}
          note={me.capabilities.analyse.note}
          remaining={me.capabilities.analyse.remaining}
        />
        <form method="post" action={`/tickers/${encodeURIComponent(idea.ticker)}/watchlist`}>
          <input type="hidden" name="csrf_token" value={me.csrf} />
          <input type="hidden" name="action" value={idea.on_my_watchlist ? "remove" : "add"} />
          <input type="hidden" name="next" value={here} />
          <button className="btn" type="submit">
            {idea.on_my_watchlist ? (
              <>
                <span aria-hidden="true">★</span> On your watchlist · remove
              </>
            ) : (
              <>
                <span aria-hidden="true">☆</span> Add to watchlist
              </>
            )}
          </button>
        </form>
        <a className="btn btn-ghost" href={`/tickers/${encodeURIComponent(idea.ticker)}`}>
          All about {idea.ticker} <span aria-hidden="true">→</span>
        </a>
      </div>
    </header>
  );
}

function ChartCard({ detail, className = "" }: { detail: IdeaDetail; className?: string }) {
  const chart = detail.chart;
  const first = chart?.closes[0]?.[0];
  const title = first ? `Price since ${MONTHS[Number(first.slice(5, 7)) - 1]} ${first.slice(0, 4)}` : "Price";
  return (
    <section className={`card ${className}`} aria-labelledby="chart-title">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <h2 id="chart-title" className="m-0">
          {title}
        </h2>
        <span className="text-sm text-muted">daily closes, {detail.idea.currency}</span>
      </div>
      {chart ? (
        <figure className="m-0">
          <PriceChart data={chart} name={detail.idea.ticker} />
          <figcaption className="mt-3 text-sm text-muted">
            Dashed and dotted lines: this idea&apos;s levels (their key is in Levels). The dot on the Reported line
            marks the report. Hover, touch or use the arrow keys for each day&apos;s close.
            {chart.split_note ? ` ${chart.split_note}` : ""}
          </figcaption>
          <details className="disclosure mt-3">
            <summary className="chevron inline-flex items-center gap-2 py-2 font-semibold text-accent">
              The closes as a table
            </summary>
            <div className="max-h-80 overflow-y-auto rounded-[8px] border border-line">
              <table className="w-full border-collapse text-sm">
                <thead className="sticky top-0 bg-surface-2 text-[0.8125rem] text-muted">
                  <tr>
                    <th scope="col" className="px-3 py-1.5 text-left font-semibold">
                      Day
                    </th>
                    <th scope="col" className="px-3 py-1.5 text-right font-semibold">
                      Close
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {[...chart.closes].reverse().map(([day, close]) => (
                    <tr key={day} className="border-t border-line">
                      <td className="px-3 py-1.5">{formatDay(day)}</td>
                      <td className="num px-3 py-1.5 text-right">{formatPrice(close, chart.currency)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
        </figure>
      ) : (
        <div className="rounded-[12px] border border-dashed border-line-strong px-4 py-6 text-center text-muted">
          <p className="mb-1 font-[650] text-ink">No chart</p>
          <p className="m-0">{detail.prices_problem ?? "Yahoo Finance has no daily closes for this stock."}</p>
        </div>
      )}
    </section>
  );
}
