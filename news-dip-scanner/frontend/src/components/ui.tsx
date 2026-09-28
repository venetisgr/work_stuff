/**
 * Small pieces the pages share, the React twins of the Fly app's Jinja macros (templates/_macros.html): score and
 * verdict badges, amounts with their ≈ in the reader's currency, signed percentages, times. Everything they print
 * goes through React, which escapes it.
 */
import type { ReactNode } from "react";
import {
  formatApprox,
  formatPct,
  formatPrice,
  formatScore,
  formatWhen,
  pctTone,
  relativeTime,
  safeUrl,
  verdictLabel,
} from "@/lib/format";
import type { Money as MoneyValue, ScoreBand } from "@/lib/types";

export function ScoreBadge({ score, band }: { score: number; band: ScoreBand }) {
  return (
    <span className={`badge badge-score band-${band}`} title={`Score ${formatScore(score)} of 100: ${band}`}>
      {formatScore(score)}
    </span>
  );
}

export function VerdictBadge({ verdict, label }: { verdict: string; label?: string }) {
  return <span className={`badge verdict-${verdict}`}>{label ?? verdictLabel(verdict)}</span>;
}

/**
 * A price as formatPrice writes it, for a narrow column: "272,750.00 KRW" may break between the number and its
 * currency code on a phone, never inside the number, so a price in won or yen can't run into the column beside it.
 * A price with a sign ("$132.00") stays one piece. The twin of the Fly app's ui.price_text macro.
 */
export function PriceText({ text, className = "" }: { text: string; className?: string }) {
  const cut = text.lastIndexOf(" ");
  if (cut <= 0) return <span className={`num ${className}`.trim()}>{text}</span>;
  return (
    <span className={className || undefined}>
      <span className="num">{text.slice(0, cut)}</span> <span className="num">{text.slice(cut + 1)}</span>
    </span>
  );
}

/** "$132.00 ≈ €115.93": the ≈ part may wrap onto its own line on a phone, and a long price before its code. */
export function Money({
  value,
  currency,
  approxCurrency,
  stacked = false,
  className = "",
}: {
  value: MoneyValue;
  currency: string;
  approxCurrency: string | null;
  stacked?: boolean;
  className?: string;
}) {
  const approx = approxCurrency ? formatApprox(value.approx, approxCurrency) : "";
  return (
    <span className={`${stacked ? "inline-flex flex-col items-end" : ""} ${className}`}>
      <PriceText text={formatPrice(value.amount, currency)} />
      {approx ? (
        <>
          {stacked ? null : " "}
          <span className="num approx text-[0.8125rem]">{approx}</span>
        </>
      ) : null}
    </span>
  );
}

/** A signed percentage, green above zero and red below (as shown: -0.04% reads +0.0% and gets no colour). */
export function Pct({ value, className = "" }: { value: number | null | undefined; className?: string }) {
  const tone = pctTone(value);
  return <span className={`num ${tone ?? ""} ${className}`}>{formatPct(value)}</span>;
}

/** "5 min ago", with the exact time in the reader's zone as its tooltip. */
export function TimeAgo({ iso, now, timeZone }: { iso: string | null; now: number; timeZone: string }) {
  if (!iso) return <>–</>;
  return (
    <time dateTime={iso} title={formatWhen(iso, timeZone)}>
      {relativeTime(iso, now)}
    </time>
  );
}

export function WatchMarker() {
  return (
    <span className="marker marker-watch" title="On your watchlist">
      <span aria-hidden="true">★</span>
      <span className="visually-hidden">On your watchlist</span>
    </span>
  );
}

export function RulesMarker() {
  return (
    <span className="marker marker-rules" title="Passes your alert rules">
      <span aria-hidden="true">✓</span>
      <span className="visually-hidden">Passes your alert rules</span>
    </span>
  );
}

/** A link to a headline's article: http(s) only, new tab, no referrer; plain text otherwise. */
export function ExternalLink({ href, children }: { href: string | null | undefined; children: ReactNode }) {
  const url = safeUrl(href);
  if (!url) return <>{children}</>;
  return (
    <a href={url} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  );
}

export function Card({
  children,
  className = "",
  labelledBy,
  as: Tag = "section",
}: {
  children: ReactNode;
  className?: string;
  labelledBy?: string;
  as?: "section" | "div" | "header" | "aside";
}) {
  return (
    <Tag className={`card ${className}`} aria-labelledby={labelledBy}>
      {children}
    </Tag>
  );
}

export function CardHeader({ id, title, aside }: { id: string; title: ReactNode; aside?: ReactNode }) {
  return (
    <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
      <h2 id={id} className="m-0">
        {title}
      </h2>
      {aside ? <span className="text-sm text-muted">{aside}</span> : null}
    </div>
  );
}

export function EmptyState({ title, children, action }: { title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="rounded-[12px] border border-dashed border-line-strong bg-surface px-4 py-8 text-center text-muted">
      <p className="mb-1 text-[1.0625rem] font-[650] text-ink">{title}</p>
      {children ? <div className="text-[0.9375rem]">{children}</div> : null}
      {action ? <div className="mt-3">{action}</div> : null}
    </div>
  );
}
