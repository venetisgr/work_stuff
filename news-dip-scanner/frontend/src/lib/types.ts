/**
 * The JSON API of the Fly.io app (/api/v1), as TypeScript types. They mirror contract/api-v1.schema.json one to one
 * (a test validates the mock fixtures against that schema); change both together.
 *
 * Conventions: numbers are numbers, moments are ISO 8601 strings with an offset, days are YYYY-MM-DD. Amounts are in
 * the idea's trading currency, `approx` in the reader's currency (Fx); null stands for missing data.
 */

export type IsoDateTime = string;
export type IsoDay = string;

export const VERDICTS = ["temporary_fear", "mixed", "fundamental", "unclear"] as const;
export type Verdict = (typeof VERDICTS)[number];
export type Confidence = "low" | "medium" | "high";
export type ScoreBand = "strong" | "good" | "fair" | "weak";
export type Agreement = "high" | "medium" | "low";
export type DebateMode = "debate" | "agreed" | "single";
export type Role = "admin" | "member";
export type ScannerState = "running" | "paused" | "stopped" | "disabled" | "stalled";
export type JobStatus = "queued" | "running" | "done" | "failed";
export type OutcomeStatus = "waiting_entry" | "open" | "target_hit" | "below_low" | "expired";
export type LevelKey = "target" | "price" | "entry" | "stat_low" | "potential_low";
export type ErrorCode =
  | "not_signed_in"
  | "forbidden"
  | "csrf"
  | "origin"
  | "not_found"
  | "method_not_allowed"
  | "bad_request"
  | "rate_limited"
  | "limit_reached"
  | "unavailable"
  | "server_error";

/** An amount in the trading currency and about the same in the reader's (null: nothing converted). */
export interface Money {
  amount: number;
  approx: number | null;
}

/** The exchange rate behind every `approx` amount of an idea. */
export interface Fx {
  currency: string;
  /** Units of `currency` per unit of the trading currency as quoted (per penny for GBp). */
  rate: number;
  main_currency: string;
  rate_main_unit: number;
  source: "analysis" | "today";
  as_of: IsoDateTime;
  note: string;
}

export interface ApiError {
  error: {
    code: ErrorCode;
    message: string;
    retry_after: number | null;
  };
}

export interface AlertRules {
  min_score: number;
  min_probability: number;
  verdicts: Verdict[];
  only_watchlist: boolean;
  thesis_changes: boolean;
}

/** GET /api/v1/me */
export interface Me {
  user: {
    id: number;
    email: string;
    name: string;
    label: string;
    role: Role;
  };
  settings: {
    currency: string | null;
    timezone: string;
    watchlist: string[];
    alert_rules: AlertRules;
    has_alert_channel: boolean;
  };
  /** Send as X-CSRF-Token with every POST (and as csrf_token in HTML forms such as POST /logout). */
  csrf: string;
  capabilities: {
    admin: boolean;
    analyse: {
      available: boolean;
      limit: number | null;
      remaining: number | null;
      note: string | null;
    };
  };
}

/** GET /api/v1/status */
export interface Status {
  state: ScannerState;
  label: string;
  reason: string | null;
  interval_minutes: number;
  cycle_running_since: IsoDateTime | null;
  next_cycle_at: IsoDateTime | null;
  last_cycle: {
    started: IsoDateTime;
    finished: IsoDateTime | null;
    ok: boolean;
    summary: string;
  } | null;
  feeds: { ok: number; total: number } | null;
  model_today: {
    calls: number;
    input_tokens: number;
    output_tokens: number;
    since: IsoDateTime;
  };
}

export interface DebateParticipantSummary {
  label: "A" | "B";
  model: string;
  model_label: string;
  opening_probability: number;
  final_probability: number;
  final_verdict: Verdict;
  changed_mind: boolean;
}

export interface DebateSummary {
  mode: DebateMode;
  agreement: Agreement | null;
  line: string;
  participants: DebateParticipantSummary[];
  final_probability: number;
  judge: string | null;
  judge_label: string | null;
}

/** An idea with the numbers the pages show, computed for the reader. */
export interface IdeaSummary {
  id: number;
  ticker: string;
  company: string;
  exchange: string | null;
  currency: string;
  created: IsoDateTime;
  age_seconds: number;
  score: number;
  score_band: ScoreBand;
  verdict: Verdict;
  verdict_label: string;
  confidence: Confidence;
  probability_up_6m: number;
  price: Money;
  entry: Money;
  target: Money;
  potential_low: Money;
  stat_low: Money;
  fx: Fx | null;
  upside_pct: number;
  downside_pct: number;
  entry_upside_pct: number;
  entry_downside_pct: number;
  change_1d_pct: number;
  change_5d_pct: number;
  analyses_count: number;
  superseded: boolean;
  superseded_by: number | null;
  matches_my_rules: boolean;
  on_my_watchlist: boolean;
  debate: DebateSummary | null;
}

export const DAY_CHOICES = [1, 3, 7, 30] as const;
export type Days = (typeof DAY_CHOICES)[number];
export const SCORE_CHOICES = [50, 65, 80] as const;
export type MinScore = (typeof SCORE_CHOICES)[number];
export type Sort = "score" | "new";

export interface IdeaFilters {
  days: Days;
  min_score: MinScore | null;
  verdict: Verdict | null;
  watchlist: boolean;
  matching: boolean;
  sort: Sort;
}

/** GET /api/v1/ideas */
export interface IdeasList {
  generated_at: IsoDateTime;
  filters: IdeaFilters;
  total: number;
  count: number;
  page: { number: number; pages: number; size: number };
  ideas: IdeaSummary[];
}

/** models.analysis_to_dict */
export interface Analysis {
  verdict: Verdict;
  probability_up_6m: number;
  potential_low: number;
  entry_price: number;
  target_price: number;
  confidence: Confidence;
  fear: string;
  fundamental_impact: string;
  thesis: string;
  risks: string[];
  catalysts: string[];
  checks: string[];
  warnings: string[];
}

export interface Participant {
  label: "A" | "B";
  model: string;
  opening: Analysis;
  final: Analysis;
  critique: string[];
  concessions: string[];
  changed_mind: boolean;
}

/** DEBATE_SPEC Debate, as stored (Opportunity.debate). */
export interface Debate {
  mode: DebateMode;
  reason: string | null;
  rounds: number;
  participants: Participant[];
  judge: string | null;
  summary: string | null;
  agreement: Agreement | null;
  favoured: string | null;
}

export interface DebateParticipantView extends Participant {
  /** "GPT-5", or "GPT-5 (Azure AI Foundry)" when both debaters' models would read the same. */
  model_label: string;
  provider: "openai" | "anthropic" | "azure";
  /** "OpenAI", "Anthropic", "Azure AI Foundry". */
  provider_label: string;
  /** The other debater's model_label; null when it stood alone. */
  other_label: string | null;
  /** The judge found its case stronger. */
  favoured: boolean;
  /** A rebuttal ran, so its opening is shown next to its final position. */
  compare: boolean;
}

/** The debate card, with the texts of the Fly idea page's card (pages.debate_view) so both sites word it alike. */
export interface DebateView {
  mode: DebateMode;
  reason: string | null;
  /** reason as a sentence for people, with model names instead of provider:model labels. */
  reason_label: string | null;
  rounds: number;
  title: "The debate" | "The models";
  /** How it went, in a sentence; null when one model stood alone. */
  how: string | null;
  participants: DebateParticipantView[];
  judge: string | null;
  judge_label: string | null;
  summary: string | null;
  agreement: Agreement | null;
  favoured: string | null;
  favoured_label: string | null;
  /** "The ruling by Claude Sonnet 5", "The merged analysis", "Merged by rule, without a judge"; null when alone. */
  ruling_title: string | null;
  /** Whose case the judge found stronger and what it knew; null without a judge. */
  judge_note: string | null;
  line: string;
}

export interface PriceStats {
  ticker: string;
  name?: string | null;
  currency: string;
  exchange?: string | null;
  as_of: IsoDateTime;
  price: number;
  change_1d_pct: number;
  change_5d_pct: number;
  stat_low_6m: number;
  volatility_pct: number;
  timezone?: string | null;
  [key: string]: unknown;
}

export interface Headline {
  title?: string | null;
  link?: string | null;
  source?: string | null;
  published?: IsoDateTime | null;
  direction?: string | null;
  magnitude?: number | null;
}

/** Opportunity.to_dict(), unchanged. */
export interface Opportunity {
  id: number;
  ticker: string;
  company: string;
  created: IsoDateTime;
  price: number;
  currency: string;
  score: number;
  analysis: Analysis;
  stats: PriceStats;
  article_ids?: string[] | null;
  headlines: Headline[];
  dip_reasons: string[];
  model: string;
  news_after_session: boolean;
  account_currency?: string | null;
  fx_rate?: number | null;
  benchmark?: string | null;
  benchmark_level?: number | null;
  fx_rates?: Record<string, number>;
  debate?: Debate | null;
  [key: string]: unknown;
}

export interface Level {
  key: LevelKey;
  label: string;
  short_label: string;
  value: Money;
  change_pct: number | null;
  from_entry_pct: number | null;
}

export interface ChartData {
  currency: string;
  /** Oldest first: [day, close]. */
  closes: [IsoDay, number][];
  levels: {
    reported: number;
    entry: number;
    target: number;
    potential_low: number;
    stat_low: number;
  };
  signal_day: IsoDay;
  split_factor: number;
  split_note: string | null;
}

export interface Outcome {
  status: OutcomeStatus;
  status_label: string;
  priced: boolean;
  price_mismatch: boolean;
  days: number;
  last_price: number | null;
  last_day: IsoDay | null;
  return_pct: number | null;
  account_currency: string | null;
  account_return_pct: number | null;
  benchmark: string | null;
  benchmark_name: string | null;
  benchmark_return_pct: number | null;
  excess_return_pct: number | null;
  entry_filled: IsoDay | null;
  target_hit: IsoDay | null;
  low_breached: IsoDay | null;
  max_gain_pct: number | null;
  max_loss_pct: number | null;
  trade_return_pct: number | null;
  up_after_6m: boolean | null;
  split_factor: number;
}

export interface HistoryItem {
  id: number;
  created: IsoDateTime;
  verdict: Verdict;
  verdict_label: string;
  score: number;
  score_band: ScoreBand;
  probability_up_6m: number;
  superseded: boolean;
  current: boolean;
}

/** GET /api/v1/ideas/{id} */
export interface IdeaDetail {
  generated_at: IsoDateTime;
  idea: IdeaSummary;
  opportunity: Opportunity;
  levels: Level[];
  fx_note: string | null;
  chart: ChartData | null;
  prices_problem: string | null;
  outcome: Outcome | null;
  outcome_notes: string[];
  history: HistoryItem[];
  rule_misses: string[];
  debate: DebateView | null;
}

/** POST /api/v1/ideas/{id}/reanalyse (202) */
export interface ReanalyseAccepted {
  job_id: number;
  status: "queued" | "running";
  ticker: string;
}

/** GET /api/v1/jobs/{id} */
export interface Job {
  id: number;
  ticker: string;
  status: JobStatus;
  created: IsoDateTime;
  finished: IsoDateTime | null;
  opportunity_id: number | null;
  error: string | null;
  ahead: number | null;
  remaining: number | null;
}

export interface ThesisSide {
  id: number;
  created: IsoDateTime;
  verdict: Verdict;
  verdict_label: string;
  probability_up_6m: number;
  score: number;
  score_band: ScoreBand;
  currency: string;
  entry: Money;
  target: Money;
}

export interface ThesisChange {
  ticker: string;
  company: string;
  reason: string;
  current: ThesisSide;
  previous: ThesisSide;
}

/** GET /api/v1/thesis-changes */
export interface ThesisChanges {
  days: number;
  changes: ThesisChange[];
}
