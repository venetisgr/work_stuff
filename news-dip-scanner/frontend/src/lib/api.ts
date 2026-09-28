import "server-only";
import { cookies, headers } from "next/headers";
import { notFound, redirect } from "next/navigation";
import { cache } from "react";
import { ApiRequestError, apiRequestHeaders, apiUrl, ideasSearch, readApiResponse, type IdeasQuery } from "./api-core";
import { ConfigError, serverConfig } from "./env";
import { trustsForwardedHeaders } from "./forward";
import { SESSION_COOKIE, loginUrl } from "./session";
import type { IdeaDetail, IdeasList, Me, Status, ThesisChanges } from "./types";

/**
 * Server-side reads of the Fly app's JSON API for the React pages, on behalf of the visitor: their dsid cookie, the
 * proxy secret and their address (see api-core.ts). Never cached (cache: "no-store"): every page shows the
 * visitor's own view. Not signed in (401): redirect to Fly's sign-in page, which comes back to nextPath.
 *
 * DIP_API_ORIGIN and DIP_PROXY_SECRET are read here, in the catch-all route handler and in proxy.ts (which only
 * checks that they are set); all three run on the server only.
 */

const TIMEOUT_MS = 15_000;

/** What the React pages say when DIP_API_ORIGIN or DIP_PROXY_SECRET is missing (the catch-all's words too). */
export const NOT_SET_UP = "This site isn't set up yet: its administrator has to finish the settings on Vercel.";

/** GET path of the API as the visitor with this session: its JSON, or an ApiRequestError. */
async function fetchApi<T>(path: string, token: string): Promise<T> {
  let config;
  try {
    config = serverConfig();
  } catch (error) {
    if (!(error instanceof ConfigError)) throw error;
    console.error(`Not configured: ${error.message}`);
    throw new ApiRequestError(503, { code: "unavailable", message: NOT_SET_UP, retry_after: null });
  }
  const incoming = await headers();
  let response: Response;
  try {
    response = await fetch(apiUrl(config.origin, path), {
      headers: apiRequestHeaders(incoming, token, {
        secret: config.secret,
        fallbackHost: "localhost",
        trustForwarded: trustsForwardedHeaders(),
      }),
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
  return readApiResponse<T>(response);
}

async function get<T>(path: string, nextPath: string): Promise<T> {
  const token = (await cookies()).get(SESSION_COOKIE)?.value;
  if (!token) redirect(loginUrl(nextPath));
  try {
    return await fetchApi<T>(path, token);
  } catch (error) {
    if (error instanceof ApiRequestError && error.status === 401) redirect(loginUrl(nextPath));
    throw error;
  }
}

/** Each is cached for the one request (React cache), so a page and its metadata share one call. */
export const getMe = cache((nextPath: string) => get<Me>("/me", nextPath));

/** The signed-in visitor, or null (no session, or the API can't say), without sending anyone to the sign-in page:
 * for pages that show the site's header either way (the not-found page). */
export const getMeIfSignedIn = cache(async (): Promise<Me | null> => {
  const token = (await cookies()).get(SESSION_COOKIE)?.value;
  if (!token) return null;
  try {
    return await fetchApi<Me>("/me", token);
  } catch {
    return null;
  }
});

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
