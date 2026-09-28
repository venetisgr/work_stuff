import "server-only";
import { cookies, headers } from "next/headers";
import { notFound, redirect } from "next/navigation";
import { cache } from "react";
import { ApiRequestError, apiRequestHeaders, apiUrl, ideasSearch, readApiResponse, type IdeasQuery } from "./api-core";
import { serverConfig } from "./env";
import { SESSION_COOKIE, loginUrl } from "./session";
import type { IdeaDetail, IdeasList, Me, Status, ThesisChanges } from "./types";

/**
 * Server-side reads of the Fly app's JSON API for the React pages, on behalf of the visitor: their dsid cookie, the
 * proxy secret and their address (see api-core.ts). Never cached (cache: "no-store"): every page shows the
 * visitor's own view. Not signed in (401): redirect to Fly's sign-in page, which comes back to nextPath.
 *
 * DIP_API_ORIGIN and DIP_PROXY_SECRET are read here and in the catch-all route handler only; both are server-only.
 */

const TIMEOUT_MS = 15_000;

async function get<T>(path: string, nextPath: string): Promise<T> {
  const { origin, secret } = serverConfig();
  const incoming = await headers();
  const token = (await cookies()).get(SESSION_COOKIE)?.value;
  if (!token) redirect(loginUrl(nextPath));
  let response: Response;
  try {
    response = await fetch(apiUrl(origin, path), {
      headers: apiRequestHeaders(incoming, token, { secret, fallbackHost: "localhost" }),
      cache: "no-store",
      redirect: "manual",
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
  } catch (error) {
    console.error(`GET ${path} failed: ${error instanceof Error ? error.name : "error"}`);
    throw new ApiRequestError(502, {
      code: "unavailable",
      message: "The scanner's server can't be reached just now. Try again in a moment.",
      retry_after: null,
    });
  }
  try {
    return await readApiResponse<T>(response);
  } catch (error) {
    if (error instanceof ApiRequestError && error.status === 401) redirect(loginUrl(nextPath));
    throw error;
  }
}

/** Each is cached for the one request (React cache), so a page and its metadata share one call. */
export const getMe = cache((nextPath: string) => get<Me>("/me", nextPath));

export const getStatus = (nextPath: string) => get<Status>("/status", nextPath);

export const getIdeas = (query: IdeasQuery, nextPath: string) => get<IdeasList>(`/ideas${ideasSearch(query)}`, nextPath);

export const getThesisChanges = (days: number, nextPath: string) =>
  get<ThesisChanges>(`/thesis-changes?days=${days}`, nextPath);

/** An idea, or the not-found page. */
export const getIdea = cache(async (id: number, nextPath: string): Promise<IdeaDetail> => {
  try {
    return await get<IdeaDetail>(`/ideas/${id}`, nextPath);
  } catch (error) {
    if (error instanceof ApiRequestError && error.status === 404) notFound();
    throw error;
  }
});
