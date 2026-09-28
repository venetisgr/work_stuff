/**
 * The ideas (the dashboard): the newest analysis of every stock analysed in the last days, filtered and ranked,
 * with the scanner's status and recent thesis changes. Filters live in the URL (?days=&min_score=&verdict=
 * &watchlist=1&matching=1&sort=&page=), so every view can be bookmarked and shared.
 */
import type { Metadata } from "next";
import Link from "next/link";
import IdeaFilters from "@/components/IdeaFilters";
import IdeaList from "@/components/IdeaList";
import { SiteShell } from "@/components/SiteShell";
import StatusStrip from "@/components/StatusStrip";
import ThesisChanges from "@/components/ThesisChanges";
import { EmptyState, RulesMarker, WatchMarker } from "@/components/ui";
import { getIdeas, getMe, getStatus, getThesisChanges } from "@/lib/api";
import { ApiRequestError, hasFilters, ideasQueryFrom, ideasSearch, type IdeasQuery } from "@/lib/api-core";
import { plural, verdictLabel } from "@/lib/format";
import { DAY_CHOICES, type Me } from "@/lib/types";

export const metadata: Metadata = { title: "Ideas" };

const THESIS_DAYS = 7;
const THESIS_SHOWN = 5;

async function load(query: IdeasQuery, here: string) {
  try {
    const [me, status, list, changes] = await Promise.all([
      getMe(here),
      getStatus(here),
      getIdeas(query, here),
      getThesisChanges(THESIS_DAYS, here),
    ]);
    return { me, status, list, changes, error: null };
  } catch (error) {
    if (error instanceof ApiRequestError) return { error };
    throw error; // a redirect to the sign-in page, or a bug (error.tsx)
  }
}

export default async function Dashboard({ searchParams }: PageProps<"/">) {
  const params = await searchParams;
  const query = ideasQueryFrom(params);
  const here = `/${ideasSearch(query)}`;
  const data = await load(query, here);

  if (data.error) {
    return (
      <SiteShell me={null} section="ideas">
        <h1 className="mb-4">Ideas</h1>
        <div className="callout callout-bad" role="alert">
          <p className="m-0">
            <strong>The ideas couldn&apos;t be loaded.</strong> {data.error.message}
          </p>
        </div>
        <p className="mt-4">
          <a className="btn btn-secondary" href={here}>
            Try again
          </a>
        </p>
      </SiteShell>
    );
  }

  const { me, status, list, changes } = data;
  const now = Date.parse(list.generated_at); // relative times from the API's clock, like its age_seconds
  const timeZone = me.settings.timezone;
  const filtered = hasFilters(query);
  const periodLink = (days: number) => `/${ideasSearch({ ...query, days: days as IdeasQuery["days"], page: 1 })}`;
  const pageLink = (page: number) => `/${ideasSearch({ ...query, page })}`;

  return (
    <SiteShell me={me} section="ideas">
      <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
        <h1 className="m-0">Ideas</h1>
        <p className="m-0 basis-full text-[1.0625rem] text-muted">
          The newest analysis of every dip in the last {plural(query.days, "day")},{" "}
          {query.sort === "score" ? "best score first" : "newest first"}.
        </p>
      </div>

      <StatusStrip status={status} now={now} timeZone={timeZone} />
      <ThesisChanges changes={changes.changes.slice(0, THESIS_SHOWN)} now={now} timeZone={timeZone} />

      <nav
        className="segmented mb-4 flex w-full max-w-full gap-0.5 overflow-x-auto rounded-full bg-surface-3 p-[3px] sm:w-fit"
        aria-label="Period"
      >
        {DAY_CHOICES.map((days) => (
          <Link key={days} href={periodLink(days)} aria-current={days === query.days ? "true" : undefined} scroll={false}>
            {plural(days, "day")}
          </Link>
        ))}
      </nav>

      <IdeaFilters key={here} query={query} open={filtered} />

      <div data-dim="">
        {list.ideas.length ? (
          <>
            <p className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-muted">
              <span>
                {list.count !== list.total
                  ? `${list.count} of ${plural(list.total, "idea")} match the filters.`
                  : `${plural(list.total, "idea")}.`}
              </span>
              <span className="inline-flex flex-wrap items-center gap-1.5">
                <WatchMarker /> on your watchlist · <RulesMarker /> passes your alert rules
              </span>
            </p>
            <IdeaList ideas={list.ideas} now={now} timeZone={timeZone} userCurrency={me.settings.currency} />
            {list.page.pages > 1 ? (
              <nav className="mt-4 flex flex-wrap items-center justify-center gap-3 text-[0.9375rem]" aria-label="Pages">
                {list.page.number > 1 ? (
                  <Link className="btn btn-small" href={pageLink(list.page.number - 1)} rel="prev">
                    Previous
                  </Link>
                ) : null}
                <span className="text-muted">
                  Page {list.page.number} of {list.page.pages}
                </span>
                {list.page.number < list.page.pages ? (
                  <Link className="btn btn-small" href={pageLink(list.page.number + 1)} rel="next">
                    Next
                  </Link>
                ) : null}
              </nav>
            ) : null}
          </>
        ) : list.total ? (
          <EmptyState
            title="No idea matches these filters"
            action={
              <Link className="btn btn-secondary" href={periodLink(query.days)}>
                Show them all
              </Link>
            }
          >
            {plural(list.total, "idea")} in the last {plural(query.days, "day")} don&apos;t pass them.
          </EmptyState>
        ) : (
          <EmptyState
            title={`No ideas in the last ${plural(query.days, "day")}`}
            action={
              query.days !== 30 ? (
                <Link className="btn btn-secondary" href="/?days=30">
                  Show the last 30 days
                </Link>
              ) : undefined
            }
          >
            The scanner lists every dip it analyses here; it checks the news every {status.interval_minutes} minutes.
            Look up a stock to analyse one yourself.
          </EmptyState>
        )}
      </div>

      <SideCards me={me} />
    </SiteShell>
  );
}

function SideCards({ me }: { me: Me }) {
  const rules = me.settings.alert_rules;
  return (
    <div className="mt-6 grid grid-cols-1 gap-4 md:grid-cols-3">
      <section className="card" aria-labelledby="lookup-title">
        <h2 id="lookup-title" className="mb-1 text-[1.0625rem]">
          Look up a stock
        </h2>
        <form className="mt-2 flex gap-2" method="get" action="/tickers">
          <label className="visually-hidden" htmlFor="f-symbol">
            Yahoo Finance symbol
          </label>
          <input
            className="input min-w-0 flex-1 font-mono uppercase placeholder:normal-case"
            id="f-symbol"
            name="symbol"
            type="text"
            placeholder="AMD, SAP.DE"
            autoCapitalize="characters"
            autoComplete="off"
            spellCheck={false}
            required
          />
          <button className="btn" type="submit">
            Open
          </button>
        </form>
        <p className="mb-0 mt-1.5 text-sm text-muted">Prices, news and ideas of one stock, and &quot;Analyse now&quot;.</p>
      </section>

      <section className="card" aria-labelledby="watch-title">
        <h2 id="watch-title" className="mb-2 text-[1.0625rem]">
          Your watchlist
        </h2>
        {me.settings.watchlist.length ? (
          <p className="m-0 flex flex-wrap gap-2">
            {me.settings.watchlist.map((symbol) => (
              <a
                key={symbol}
                className="inline-flex min-h-10 items-center rounded-full border border-line-strong bg-surface px-3 text-[0.8125rem] text-ink hover:bg-surface-2 hover:no-underline sm:min-h-[30px]"
                href={`/tickers/${encodeURIComponent(symbol)}`}
              >
                <span className="ticker">{symbol}</span>
              </a>
            ))}
          </p>
        ) : (
          <p className="m-0 text-sm text-muted">
            Empty. Any negative news about a stock on it can make it a candidate: add symbols on a stock&apos;s page or
            in <a href="/settings#watchlist">Settings</a>.
          </p>
        )}
      </section>

      <section className="card" aria-labelledby="rules-title">
        <h2 id="rules-title" className="mb-2 text-[1.0625rem]">
          Your alert rules
        </h2>
        <p className="mb-2 text-sm">
          Score {rules.min_score} or more, a chance up of {rules.min_probability}% or more
          {rules.verdicts.length ? `, ${rules.verdicts.map((v) => verdictLabel(v).toLowerCase()).join(" or ")}` : ""}
          {rules.only_watchlist ? ", on your watchlist only" : ""}.
        </p>
        <p className="m-0 text-sm text-muted">
          {me.settings.has_alert_channel ? "Ideas that pass them are sent to you." : "No alert channel yet."}{" "}
          <a href="/settings">Change them</a>
        </p>
      </section>
    </div>
  );
}
