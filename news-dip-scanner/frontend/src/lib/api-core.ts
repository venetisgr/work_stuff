/**
 * The parts of the API client that don't need a request (pure, so the tests can call them): the URL of an
 * endpoint, the headers of a server-side call to Fly, reading an answer into data or an ApiError, and the query
 * string of the ideas list. src/lib/api.ts wires them to the visitor's request.
 */
import { clientIp, visitorHost, withProxyHeaders } from "./forward";
import type { ApiError, ErrorCode, IdeaFilters, Verdict } from "./types";
import { DAY_CHOICES, SCORE_CHOICES, VERDICTS } from "./types";

export const API_PREFIX = "/api/v1";

/** A failed API call: the HTTP status and the contract's error (code, message for people, retry_after). */
export class ApiRequestError extends Error {
  readonly status: number;
  readonly code: ErrorCode;
  readonly retryAfter: number | null;

  constructor(status: number, error: ApiError["error"]) {
    super(error.message);
    this.name = "ApiRequestError";
    this.status = status;
    this.code = error.code;
    this.retryAfter = error.retry_after;
  }
}

/** ${origin}/api/v1${path}; path must start with "/" and stay under /api/v1. */
export function apiUrl(origin: string, path: string): string {
  if (!path.startsWith("/") || path.startsWith("//") || path.includes("..")) {
    throw new Error(`Not an API path: ${path}`);
  }
  return `${new URL(origin).origin}${API_PREFIX}${path}`;
}

/** The headers of a server-side call to Fly on behalf of the visitor: their session cookie only (dsid), their
 * browser's name, and the proxy's headers (their address only where the platform vouches for it: trustForwarded,
 * see forward.ts trustsForwardedHeaders). */
export function apiRequestHeaders(
  incoming: Headers,
  sessionToken: string | undefined,
  { secret, fallbackHost, trustForwarded }: { secret: string; fallbackHost: string; trustForwarded: boolean },
): Headers {
  const headers = new Headers({ accept: "application/json" });
  const agent = incoming.get("user-agent");
  if (agent) headers.set("user-agent", agent);
  if (sessionToken && /^[A-Za-z0-9_-]{1,256}$/.test(sessionToken)) headers.set("cookie", `dsid=${sessionToken}`);
  return withProxyHeaders(headers, {
    secret,
    host: visitorHost(incoming, fallbackHost),
    clientIp: clientIp(incoming, trustForwarded),
  });
}

const CODES_BY_STATUS: Record<number, ErrorCode> = {
  400: "bad_request",
  401: "not_signed_in",
  403: "forbidden",
  404: "not_found",
  405: "method_not_allowed",
  422: "bad_request",
  429: "rate_limited",
  503: "unavailable",
};

const MESSAGES: Partial<Record<ErrorCode, string>> = {
  not_signed_in: "Sign in to continue.",
  forbidden: "You aren't allowed to do that.",
  not_found: "That doesn't exist (any more).",
  rate_limited: "Too many requests. Wait a moment and try again.",
  unavailable: "The scanner's server can't do that just now. Try again in a moment.",
  server_error: "Something went wrong on the scanner's server. Try again in a moment.",
};

function isApiError(value: unknown): value is ApiError {
  if (typeof value !== "object" || value === null) return false;
  const error = (value as { error?: unknown }).error;
  return (
    typeof error === "object" &&
    error !== null &&
    typeof (error as { code?: unknown }).code === "string" &&
    typeof (error as { message?: unknown }).message === "string"
  );
}

/** The contract's error for a failed response, from its JSON body when it has one. */
export function errorFor(status: number, body: unknown): ApiError["error"] {
  if (isApiError(body)) {
    const retry = body.error.retry_after;
    return { code: body.error.code, message: body.error.message, retry_after: typeof retry === "number" ? retry : null };
  }
  const code = CODES_BY_STATUS[status] ?? "server_error";
  return { code, message: MESSAGES[code] ?? MESSAGES.server_error!, retry_after: null };
}

/** The JSON of a successful answer, or an ApiRequestError. A redirect or a non-JSON answer is an error: the API
 * answers JSON only (an HTML page means the request reached the wrong place). */
export async function readApiResponse<T>(response: Response): Promise<T> {
  const type = response.headers.get("content-type") ?? "";
  let body: unknown = null;
  if (type.includes("application/json")) {
    try {
      body = await response.json();
    } catch {
      body = null;
    }
  }
  if (response.ok && body !== null && !(response.status >= 300)) return body as T;
  const status = response.ok || (response.status >= 300 && response.status < 400) ? 502 : response.status;
  throw new ApiRequestError(status, errorFor(status, body));
}

export type IdeasQuery = IdeaFilters & { page: number };

const DEFAULT_QUERY: IdeasQuery = {
  days: 7,
  min_score: null,
  verdict: null,
  watchlist: false,
  matching: false,
  sort: "score",
  page: 1,
};

type SearchParams = Record<string, string | string[] | undefined>;

function first(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

function flag(value: string | undefined): boolean {
  return ["1", "on", "true", "yes"].includes((value ?? "").toLowerCase());
}

/**
 * The dashboard's filters from its URL. The API's names (days, min_score, verdict, watchlist, matching, sort, page)
 * and, for links from the Fly pages, their old names (score, rules). Anything invalid falls back to the default.
 */
export function ideasQueryFrom(params: SearchParams): IdeasQuery {
  const days = Number(first(params.days));
  const score = Number(first(params.min_score) ?? first(params.score));
  const verdict = first(params.verdict);
  const sort = first(params.sort);
  const page = Number(first(params.page));
  return {
    days: (DAY_CHOICES as readonly number[]).includes(days) ? (days as IdeasQuery["days"]) : DEFAULT_QUERY.days,
    min_score: (SCORE_CHOICES as readonly number[]).includes(score) ? (score as IdeasQuery["min_score"]) : null,
    verdict: (VERDICTS as readonly string[]).includes(verdict ?? "") ? (verdict as Verdict) : null,
    watchlist: flag(first(params.watchlist)),
    matching: flag(first(params.matching) ?? first(params.rules)),
    sort: sort === "new" ? "new" : "score",
    page: Number.isInteger(page) && page > 1 && page < 10_000 ? page : 1,
  };
}

/** The query string for a set of filters, defaults left out: "?days=30&min_score=65" or "". */
export function ideasSearch(query: Partial<IdeasQuery>): string {
  const full = { ...DEFAULT_QUERY, ...query };
  const params = new URLSearchParams();
  if (full.days !== DEFAULT_QUERY.days) params.set("days", String(full.days));
  if (full.min_score !== null) params.set("min_score", String(full.min_score));
  if (full.verdict) params.set("verdict", full.verdict);
  if (full.watchlist) params.set("watchlist", "1");
  if (full.matching) params.set("matching", "1");
  if (full.sort !== "score") params.set("sort", full.sort);
  if (full.page > 1) params.set("page", String(full.page));
  const text = params.toString();
  return text ? `?${text}` : "";
}

/** Whether any filter differs from the defaults (the period aside). */
export function hasFilters(query: IdeasQuery): boolean {
  return query.min_score !== null || query.verdict !== null || query.watchlist || query.matching || query.sort !== "score";
}
