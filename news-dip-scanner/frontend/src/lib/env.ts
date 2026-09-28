import "server-only";

/**
 * The server-only settings: where the Fly app is and the secret that proves a request came through this proxy.
 * Both are plain (not NEXT_PUBLIC_) environment variables, so Next.js never puts them in a browser bundle, and the
 * "server-only" import above fails the build if a client component imports this module.
 *
 *   DIP_API_ORIGIN    https://my-dip-scanner.fly.dev (no path; http only for 127.0.0.1/localhost)
 *   DIP_PROXY_SECRET  the Fly app's PROXY_SECRET (at least 32 characters)
 */

export class ConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ConfigError";
  }
}

export interface ServerConfig {
  origin: string;
  secret: string;
}

const MIN_SECRET = 32;

/** Check the two variables; the message never contains the secret. */
export function parseServerConfig(env: Record<string, string | undefined>): ServerConfig {
  const rawOrigin = (env.DIP_API_ORIGIN ?? "").trim();
  const secret = (env.DIP_PROXY_SECRET ?? "").trim();
  if (!rawOrigin) throw new ConfigError("Set DIP_API_ORIGIN to the Fly app's address, e.g. https://my-dips.fly.dev.");
  let url: URL;
  try {
    url = new URL(rawOrigin);
  } catch {
    throw new ConfigError("DIP_API_ORIGIN must be an address like https://my-dips.fly.dev.");
  }
  const local = ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname);
  if (url.protocol !== "https:" && !(url.protocol === "http:" && local)) {
    throw new ConfigError("DIP_API_ORIGIN must start with https:// (http:// only for 127.0.0.1 or localhost).");
  }
  if ((url.pathname !== "/" && url.pathname !== "") || url.search || url.hash || url.username || url.password) {
    throw new ConfigError("DIP_API_ORIGIN must be the address only, without a path, query or user name.");
  }
  if (secret.length < MIN_SECRET) {
    throw new ConfigError(`Set DIP_PROXY_SECRET to the Fly app's PROXY_SECRET (at least ${MIN_SECRET} characters).`);
  }
  return { origin: url.origin, secret };
}

/** The settings of this deployment (read at request time, so a changed variable needs no new build). */
export function serverConfig(): ServerConfig {
  return parseServerConfig(process.env);
}
