# news-dip-scanner front end (Next.js on Vercel)

The public front door of the website. It renders the most-used pages in React and hands every other path to the
Fly.io app, which keeps the scanner, the database, the accounts and all the business logic:

```
browser ──► Vercel: Next.js (this folder)
              ├─ /  and  /ideas/<id>        React pages; their data from Fly's JSON API (server-side)
              └─ every other path           forwarded to Fly as it is (sign-in, invites, settings, news,
                  (catch-all route handler)  track record, admin, tickers, jobs, /api/v1, Fly's /static)
                        │ + x-dip-proxy-secret, x-dip-client-ip, x-forwarded-host, x-forwarded-proto
                        ▼
            Fly.io: FastAPI + scanner + SQLite (../dip_scanner/web)
```

Setting it up on Vercel, step by step: [`../docs/VERCEL.md`](../docs/VERCEL.md).

One login: the Fly app's invite-only accounts and its session cookie, `dsid`. The browser only ever talks to the
Vercel domain, so the cookie lives there and reaches Fly with every forwarded request; the React pages send it to
Fly's API from the server. The Fly app refuses requests without the proxy secret, so its `*.fly.dev` address is
useless on its own.

## What is where

| Path | Role |
|---|---|
| `src/proxy.ts` | Runs before the React pages only (`/`, `/ideas/<id>`): a fresh CSP nonce per request, the security headers, no `x-dip-*` header from the visitor, the session cookie renewed like Fly renews it. |
| `src/app/[...path]/route.ts` | Everything Next.js doesn't serve: streamed to `DIP_API_ORIGIN` with the proxy's headers, and Fly's answer streamed back (status, every `Set-Cookie`, redirects as Fly wrote them). |
| `src/lib/forward.ts` | The header rules of both directions (allow-list to Fly, hop-by-hop headers dropped, the visitor's IP and host). |
| `src/lib/api.ts`, `api-core.ts`, `env.ts` | Server-only reads of `/api/v1` for the pages (`cache: "no-store"`; not signed in: redirect to `/login?next=...`). |
| `src/app/page.tsx`, `src/app/ideas/[id]/page.tsx` | The dashboard and the idea page. |
| `src/components/` | The site's header and footer, badges, the ideas list, the price chart, the debate card, "Analyse again". |
| `src/lib/format.ts` | Numbers, amounts and times, written the same way as the Fly pages (`report.format_price` and friends). |
| `src/app/globals.css` | Tailwind CSS 4 plus the Fly app's colour tokens, badges, score bands and verdict colours (from `dip_scanner/web/static/app.css` and `pages.css`), light and dark. |
| `contract/api-v1.schema.json` | The JSON API contract (JSON Schema 2020-12), one definition per response; `x-endpoints` maps endpoints to definitions. |
| `contract/mocks/*.json` | Realistic example responses; `tests/contract.test.ts` validates every one against the schema. |
| `scripts/mock-api.mjs` | A stand-in for the Fly app for front-end work: the mocks, a fake session, a few HTML pages. |
| `e2e/` | End-to-end tests with Playwright against `next start` and a real Fly app, and stand-in language models for them (`e2e/README.md`). |

Why a route handler and not a rewrite in `proxy.ts`: a `NextResponse.rewrite()` to an external URL with changed
request headers works (tested with `next start`: GET, POST, cookies both ways, 303 redirects), but Next.js then
names the destination in an `x-middleware-rewrite` response header, which would publish the Fly address
([vercel/next.js#58366](https://github.com/vercel/next.js/discussions/58366)). The route handler keeps it private,
behaves the same locally and on Vercel, and its header rules are unit-tested.

## The JSON API

All under `/api/v1` on the Fly app, with the `dsid` cookie; errors are JSON (`#/$defs/Error`), POSTs need
`X-CSRF-Token` (the `csrf` of `/me`). The schema file is the exact contract; the Python tests validate real
responses against it.

| Request | Response |
|---|---|
| `GET /api/v1/me` | `Me`: user, settings (currency, time zone, watchlist, alert rules), `csrf`, capabilities |
| `GET /api/v1/status` | `Status`: the scanner's status strip |
| `GET /api/v1/ideas?days=7&min_score=65&verdict=mixed&watchlist=1&matching=1&sort=score&page=1` | `IdeasList` |
| `GET /api/v1/ideas/{id}` | `IdeaDetail`: the idea, `opportunity` (`to_dict()`), levels, chart, outcome, history, debate (with the Fly idea page's texts for the debate card, so both word it alike) |
| `POST /api/v1/ideas/{id}/reanalyse` (header `X-CSRF-Token`) | 202 `ReanalyseAccepted` `{job_id, status, ticker}` |
| `GET /api/v1/jobs/{id}` | `Job`: poll until `done` (then `opportunity_id`) or `failed` |
| `GET /api/v1/thesis-changes?days=7` | `ThesisChanges` |

## Settings

| Variable | Where | Meaning |
|---|---|---|
| `DIP_API_ORIGIN` | Vercel: Production and Preview | The Fly app's address without a path, `https://<app>.fly.dev` (http only for 127.0.0.1/localhost). |
| `DIP_PROXY_SECRET` | Vercel: Production and Preview, marked Sensitive | The same value as the Fly app's `PROXY_SECRET` (32+ characters). |
| `DIP_TRUSTED_PROXY` | Only when self-hosting | `1` when a proxy of your own in front of `next start` overwrites `X-Real-IP` and `X-Forwarded-For` with the visitor's address. On Vercel (`VERCEL=1`) it isn't needed. |

Both are server-only: never prefix them with `NEXT_PUBLIC_`. `.env.example` has a template for `.env.local`.
`vercel.json` pins the functions to Frankfurt (`fra1`), next to the Fly app's region; Vercel runs `proxy.ts`
itself at the edge nearest the visitor.

## Commands

```bash
npm ci
npm run dev:mock     # next dev + the mock Fly app: http://localhost:3000, sign in with any email and password
npm run dev          # next dev against DIP_API_ORIGIN (.env.local)
npm run lint         # ESLint (next/core-web-vitals + TypeScript)
npm run typecheck    # next typegen && tsc --noEmit
npm test             # Vitest: contract, proxy headers, the "can't reach Fly" page, API helpers, formatters,
                     # chart geometry, components rendered on the server
npm run build        # next build
npm start            # next start (production mode, e.g. against a local Fly app)
npm run e2e          # Playwright through next start and a real Fly app (needs both running: e2e/README.md)
```

To try it end to end on your own computer: run the Fly app with `PROXY_SECRET=<secret>`, `COOKIE_SECURE=false`
and `BASE_URL=http://localhost:3000` (`dip-scanner serve --no-scanner` serves an existing database), then
`npm run build` and `DIP_API_ORIGIN=http://127.0.0.1:8080 DIP_PROXY_SECRET=<secret> npm start`, and open
`http://localhost:3000` (the same host as `BASE_URL`, or the Fly app's Origin check refuses the forms).
[`e2e/README.md`](e2e/README.md) has the full recipe, with stand-in models for "Analyse again".

`CONTRACT_SAMPLES=<folder> npm test` also checks answers saved from a running Fly app (named like the files in
`contract/mocks`) against the schema and the rules its computed fields follow.

## Security

- **CSP with nonces** on the React pages (`script-src 'self' 'nonce-…' 'strict-dynamic'`, `style-src 'self'
  'nonce-…'`, no `'unsafe-eval'` or `'unsafe-inline'` outside `next dev`), and the Fly app's other headers
  (`nosniff`, `Referrer-Policy: same-origin`, `X-Frame-Options: DENY`, COOP, Permissions-Policy, HSTS over https).
  No inline `style` attributes anywhere (the chart's geometry is SVG attributes; its colours are classes).
- **Headers to Fly**: an allow-list of the visitor's headers; every incoming `x-dip-*` header is dropped, and the
  platform's own (`x-vercel-*`, including OIDC tokens) never leave Vercel. The visitor's IP comes from `x-real-ip` /
  `x-forwarded-for` only on Vercel (`VERCEL=1`), which sets them itself and doesn't take them from the visitor, or
  with `DIP_TRUSTED_PROXY=1`. `next start` on its own passes on whatever the visitor sent, so there no address is
  named and the Fly app counts the front door's own.
- **When Fly can't be asked** (not configured, unreachable, over 120 seconds, or Fly's edge answering for a stopped
  Machine), the front door answers a small page of its own (`src/lib/unavailable.ts`: viewport, light and dark, a
  "Try again" link, its one inline style allowed by hash in its own CSP), or the contract's JSON error on `/api/`.
- **Paths can't leave Fly**: the target is `DIP_API_ORIGIN` + path + query joined as text, checked to stay on that
  origin.
- **Untrusted text** (headlines, the models' text, names) is rendered by React (escaped); links only for `http`
  and `https` addresses, opening in a new tab with `rel="noopener noreferrer"`.
- **Every page is per user**: `cache: "no-store"` on every API read and dynamic rendering. Vercel's CDN caches a
  function's answer only when it says `s-maxage` (or `CDN-Cache-Control`), which the Fly app never does: its pages
  are `no-store`, its static files `public, max-age` (the browser's cache only).

Checked against the documentation on 2026-09-28: Next.js 16.3
([proxy.js](https://nextjs.org/docs/app/api-reference/file-conventions/proxy),
[CSP](https://nextjs.org/docs/app/guides/content-security-policy),
[NextResponse](https://nextjs.org/docs/app/api-reference/functions/next-response),
[route handlers](https://nextjs.org/docs/app/api-reference/file-conventions/route),
[rewrites](https://nextjs.org/docs/app/api-reference/config/next-config-js/rewrites),
[fetch](https://nextjs.org/docs/app/api-reference/functions/fetch),
[caching](https://nextjs.org/docs/app/getting-started/caching),
[environment variables](https://nextjs.org/docs/app/guides/environment-variables),
[data security](https://nextjs.org/docs/app/guides/data-security)),
[Tailwind CSS 4 for Next.js](https://tailwindcss.com/docs/installation/framework-guides/nextjs), and Vercel
([rewrites](https://vercel.com/docs/routing/rewrites),
[routing middleware](https://vercel.com/docs/routing-middleware),
[request headers](https://vercel.com/docs/headers/request-headers),
[function regions](https://vercel.com/docs/functions/configuring-functions/region),
[monorepos](https://vercel.com/docs/monorepos),
[environment variables](https://vercel.com/docs/environment-variables),
[preview deployments](https://vercel.com/docs/deployments/preview-deployments),
[deployment protection](https://vercel.com/docs/deployment-protection),
[Hobby plan](https://vercel.com/docs/plans/hobby)).

Not investment advice. The scanner never trades.
