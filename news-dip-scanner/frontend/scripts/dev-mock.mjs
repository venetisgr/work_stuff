#!/usr/bin/env node
/**
 * npm run dev:mock: the mock Fly app (scripts/mock-api.mjs) on 127.0.0.1:8787 and `next dev` pointed at it, so the
 * React pages work without the Python backend. Open http://localhost:3000 and sign in with any email and password.
 * Extra arguments go to next dev (npm run dev:mock -- --port 3001).
 */
import { spawn } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { MOCK_SECRET } from "./mock-api.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const port = process.env.MOCK_PORT || "8787";
const env = {
  ...process.env,
  MOCK_PORT: port,
  DIP_API_ORIGIN: `http://127.0.0.1:${port}`,
  DIP_PROXY_SECRET: MOCK_SECRET,
};
// mock-api.mjs is already listening in this process (imported above); start Next.js next to it.
const next = spawn(process.execPath, [join(HERE, "..", "node_modules", "next", "dist", "bin", "next"), "dev", ...process.argv.slice(2)], {
  env,
  stdio: "inherit",
});
const stop = () => next.kill("SIGTERM");
process.on("SIGINT", stop);
process.on("SIGTERM", stop);
next.on("exit", (code) => process.exit(code ?? 0));
