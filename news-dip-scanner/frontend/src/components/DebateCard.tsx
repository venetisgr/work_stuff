/**
 * How two models argued over an idea (DEBATE_SPEC), laid out and worded like the Fly idea page's card
 * (templates/pages/idea.html, whose texts the API passes on: how, reason_label, ruling_title, judge_note): each
 * model's final position with what changed since its opening marked, its critique of the other and what it accepts,
 * then the ruling, which is the idea's own analysis. Two columns once the card is wide enough (a container query, so
 * the page's column counts, not the screen), one after the other on a phone. "agreed": the openings agreed and were
 * merged without a judge; "single": one model only (the other failed; reason_label says why).
 */
import { formatPercent, formatPrice, verdictLabel } from "@/lib/format";
import type { Agreement, Analysis, DebateParticipantView, DebateView } from "@/lib/types";
import { VerdictBadge } from "./ui";

const AGREEMENT_BADGE: Record<Agreement, string> = {
  high: "badge-ok",
  medium: "badge-warn",
  low: "badge-bad",
};

const capitalise = (text: string) => text.slice(0, 1).toUpperCase() + text.slice(1);

interface Figure {
  label: string;
  opening: string;
  final: string;
}

/** Chance up, target, entry, potential low and confidence, highest level first like the levels card
 * (pages.position_figures). */
export function positionFigures(opening: Analysis, final: Analysis, currency: string): Figure[] {
  const cells = (a: Analysis) => [
    formatPercent(a.probability_up_6m),
    formatPrice(a.target_price, currency),
    formatPrice(a.entry_price, currency),
    formatPrice(a.potential_low, currency),
    capitalise(a.confidence),
  ];
  const before = cells(opening);
  const after = cells(final);
  return ["Chance up", "Target", "Entry", "Potential low", "Confidence"].map((label, index) => ({
    label,
    opening: before[index],
    final: after[index],
  }));
}

function Side({
  side,
  number,
  judged,
  currency,
}: {
  side: DebateParticipantView;
  number: number;
  judged: boolean;
  currency: string;
}) {
  const verdictChanged = side.compare && side.opening.verdict !== side.final.verdict;
  const tags = side.favoured || (side.compare && side.changed_mind);
  return (
    <article className={`debate-side${side.favoured ? " is-favoured" : ""}`} aria-labelledby={`debater-${number}`}>
      <header className="debate-side-head">
        {judged ? (
          <span className="analyst-mark" aria-hidden="true">
            {side.label}
          </span>
        ) : null}
        <span className="debate-who">
          <h3 id={`debater-${number}`}>{side.model_label}</h3>
          <span className="debate-provider">
            {side.provider_label}
            {judged ? ` · Analyst ${side.label}` : ""}
          </span>
        </span>
      </header>
      {tags ? (
        <p className="debate-tags">
          {side.favoured ? <span className="badge badge-info">Favoured by the judge</span> : null}
          {side.compare && side.changed_mind ? <span className="badge badge-outline">Changed its mind</span> : null}
        </p>
      ) : null}
      <p className="debate-verdict">
        {verdictChanged ? (
          <>
            <span className="visually-hidden">Opening verdict: </span>
            <s className="was">{verdictLabel(side.opening.verdict)}</s>
            <span className="debate-arrow" aria-hidden="true">
              →
            </span>
            <span className="visually-hidden">final verdict: </span>
          </>
        ) : null}
        <VerdictBadge verdict={side.final.verdict} />
      </p>
      <table className={`debate-figures${side.compare ? " is-compared" : ""}`}>
        <caption className="visually-hidden">
          {side.model_label}: {side.compare ? "opening and final position" : "its position"}
        </caption>
        {side.compare ? (
          <thead>
            <tr>
              <th scope="col">
                <span className="visually-hidden">Figure</span>
              </th>
              <th scope="col">Opening</th>
              <th scope="col">Final</th>
            </tr>
          </thead>
        ) : null}
        <tbody>
          {positionFigures(side.opening, side.final, currency).map((figure) => {
            const changed = side.compare && figure.opening !== figure.final;
            return (
              <tr key={figure.label} className={changed ? "is-changed" : undefined}>
                <th scope="row">{figure.label}</th>
                {side.compare ? <td className="num was">{changed ? <s>{figure.opening}</s> : figure.opening}</td> : null}
                <td className="num">
                  {changed ? (
                    <>
                      <mark>{figure.final}</mark>
                      <span className="visually-hidden"> (changed)</span>
                    </>
                  ) : (
                    figure.final
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {side.critique.length ? (
        <div className="debate-points is-critique">
          <h4>Its critique{side.other_label ? ` of ${side.other_label}` : ""}</h4>
          <ul>
            {side.critique.slice(0, 5).map((point, index) => (
              <li key={index}>{point}</li>
            ))}
          </ul>
        </div>
      ) : null}
      {side.concessions.length ? (
        <div className="debate-points is-concession">
          <h4>What it accepts{side.other_label ? ` from ${side.other_label}` : ""}</h4>
          <ul>
            {side.concessions.slice(0, 5).map((point, index) => (
              <li key={index}>{point}</li>
            ))}
          </ul>
        </div>
      ) : null}
    </article>
  );
}

export default function DebateCard({
  debate,
  currency,
  final,
  className = "",
}: {
  debate: DebateView;
  currency: string;
  final: Analysis;
  className?: string;
}) {
  const judged = debate.mode === "debate";
  return (
    <section className={`card debate-card ${className}`} id="debate" aria-labelledby="debate-title">
      <div className="card-header">
        <h2 id="debate-title">{debate.title}</h2>
        {debate.agreement ? (
          <span className={`badge ${AGREEMENT_BADGE[debate.agreement]}`}>{capitalise(debate.agreement)} agreement</span>
        ) : null}
      </div>
      {debate.how ? <p className="debate-how text-sm text-muted">{debate.how}</p> : null}
      {debate.mode === "single" ? (
        <div className="callout debate-alone text-sm" role="note">
          <p className="m-0">
            <strong>Only {debate.participants[0]?.model_label} answered.</strong>
            {debate.reason_label ? <span className="debate-reason"> {debate.reason_label}</span> : null}
          </p>
          <p className="mb-0 mt-2">No second model checked this analysis, so read it with more care.</p>
        </div>
      ) : null}
      <div className={`debate-sides${debate.participants.length > 1 ? " is-pair" : ""}`}>
        {debate.participants.map((side, index) => (
          <Side key={side.model} side={side} number={index + 1} judged={judged} currency={currency} />
        ))}
      </div>
      {debate.ruling_title ? (
        <div className="debate-ruling">
          <h3>{debate.ruling_title}</h3>
          <p className="debate-outcome">
            <VerdictBadge verdict={final.verdict} />{" "}
            <span className="whitespace-nowrap">
              <strong className="num">{formatPercent(final.probability_up_6m)}</strong> chance up
            </span>{" "}
            ·{" "}
            <span className="whitespace-nowrap">
              target <span className="num">{formatPrice(final.target_price, currency)}</span>
            </span>{" "}
            ·{" "}
            <span className="whitespace-nowrap">
              entry <span className="num">{formatPrice(final.entry_price, currency)}</span>
            </span>{" "}
            ·{" "}
            <span className="whitespace-nowrap">
              low <span className="num">{formatPrice(final.potential_low, currency)}</span>
            </span>{" "}
            · <span className="whitespace-nowrap">{final.confidence} confidence</span>
          </p>
          {debate.summary ? <p className="debate-summary">{debate.summary}</p> : null}
          {debate.judge_note ? <p className="text-sm text-muted">{debate.judge_note}</p> : null}
          {judged && debate.reason_label ? <p className="debate-reason text-sm text-muted">{debate.reason_label}</p> : null}
        </div>
      ) : null}
    </section>
  );
}
