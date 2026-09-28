"use client";

/**
 * The dashboard's filters: a GET form that works without JavaScript ("Show"), and with it applies each change at
 * once through the router, keeping the list on screen (dimmed) while the new one loads. The URL is the state.
 */
import { useRouter } from "next/navigation";
import { useEffect, useRef, useSyncExternalStore, useTransition, type FormEvent } from "react";
import { ideasSearch, type IdeasQuery } from "@/lib/api-core";
import { SCORE_CHOICES, VERDICTS, type Verdict } from "@/lib/types";
import { VERDICT_LABELS } from "@/lib/format";

const subscribeNothing = () => () => {};

export default function IdeaFilters({ query, open }: { query: IdeasQuery; open: boolean }) {
  const router = useRouter();
  const form = useRef<HTMLFormElement>(null);
  // false while rendered on the server (the "Show" button is there), true once the page runs JavaScript
  const scripted = useSyncExternalStore(subscribeNothing, () => true, () => false);
  const [pending, startTransition] = useTransition();

  useEffect(() => {
    document.body.toggleAttribute("data-loading", pending);
  }, [pending]);

  const apply = (event?: FormEvent) => {
    event?.preventDefault();
    const data = new FormData(form.current!);
    const next = ideasSearch({
      days: query.days,
      min_score: data.get("min_score") ? (Number(data.get("min_score")) as IdeasQuery["min_score"]) : null,
      verdict: (data.get("verdict") as Verdict) || null,
      sort: data.get("sort") === "new" ? "new" : "score",
      watchlist: data.get("watchlist") === "1",
      matching: data.get("matching") === "1",
      page: 1,
    });
    startTransition(() => router.push(`/${next}`, { scroll: false }));
  };

  const summary = [
    query.min_score ? `score ${query.min_score}+` : "any score",
    query.verdict ? VERDICT_LABELS[query.verdict] : "all verdicts",
    ...(query.watchlist ? ["my watchlist"] : []),
    ...(query.matching ? ["my alert rules"] : []),
    query.sort === "new" ? "newest first" : "best score first",
  ].join(" · ");
  const active = query.min_score !== null || query.verdict !== null || query.watchlist || query.matching || query.sort !== "score";

  return (
    <details className="card disclosure mb-4 p-0 sm:p-0" open={open}>
      <summary className="chevron flex min-h-[46px] items-center gap-2 px-4 py-2">
        <span className="flex-none font-[650]">Filters</span>
        <span className="min-w-0 truncate text-sm text-muted">{summary}</span>
        <span className="ml-auto" aria-hidden="true" />
      </summary>
      <form
        ref={form}
        method="get"
        action="/"
        onSubmit={apply}
        onChange={() => apply()}
        className="flex flex-wrap items-end gap-3 px-4 pb-4"
      >
        {query.days !== 7 ? <input type="hidden" name="days" value={query.days} /> : null}
        <label className="min-w-0 flex-[1_1_140px]">
          <span className="mb-1.5 block text-[0.8125rem] font-semibold text-muted">Score</span>
          <select className="select" name="min_score" defaultValue={query.min_score ?? ""}>
            <option value="">Any score</option>
            {SCORE_CHOICES.map((score) => (
              <option key={score} value={score}>
                {score} or more
              </option>
            ))}
          </select>
        </label>
        <label className="min-w-0 flex-[1_1_140px]">
          <span className="mb-1.5 block text-[0.8125rem] font-semibold text-muted">Verdict</span>
          <select className="select" name="verdict" defaultValue={query.verdict ?? ""}>
            <option value="">All verdicts</option>
            {VERDICTS.map((verdict) => (
              <option key={verdict} value={verdict}>
                {VERDICT_LABELS[verdict]}
              </option>
            ))}
          </select>
        </label>
        <label className="min-w-0 flex-[1_1_140px]">
          <span className="mb-1.5 block text-[0.8125rem] font-semibold text-muted">Order</span>
          <select className="select" name="sort" defaultValue={query.sort}>
            <option value="score">Best score first</option>
            <option value="new">Newest first</option>
          </select>
        </label>
        <div className="grid basis-full gap-0 sm:flex sm:flex-wrap sm:gap-x-6">
          <label className="check">
            <input type="checkbox" name="watchlist" value="1" defaultChecked={query.watchlist} />
            <span>Only my watchlist</span>
          </label>
          <label className="check">
            <input type="checkbox" name="matching" value="1" defaultChecked={query.matching} />
            <span>Only ideas that pass my alert rules</span>
          </label>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          {scripted ? null : (
            <button className="btn btn-small" type="submit">
              Show
            </button>
          )}
          {active ? (
            <a className="btn btn-small btn-ghost" href={query.days !== 7 ? `/?days=${query.days}` : "/"}>
              Clear the filters
            </a>
          ) : null}
          {pending ? (
            <span className="inline-flex items-center gap-2 text-sm text-muted" role="status">
              <span className="spinner" aria-hidden="true" /> Updating…
            </span>
          ) : null}
        </div>
      </form>
    </details>
  );
}
