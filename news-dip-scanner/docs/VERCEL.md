# The Vercel front door

This puts the Next.js front end in [`frontend/`](../frontend/) on [Vercel](https://vercel.com), as the only address
people use. It draws the ideas (`/`) and each idea's page (`/ideas/<id>`) in React, with the data from the Fly app's
JSON API, and passes every other request (signing in, invites, settings, news, track record, admin, tickers, the Fly
app's files) on to the Fly app unchanged. The scanner, the database, the accounts and every rule stay on Fly.io.

```
 browser ──► Vercel: Next.js (frontend/)
               ├─ /  and  /ideas/<id>     React pages; their data from the Fly app's /api/v1, read on the server
               └─ every other path        streamed to the Fly app and back (status, cookies, redirects)
                     │ + x-dip-proxy-secret, x-dip-client-ip, x-forwarded-host, x-forwarded-proto
                     ▼
             Fly.io: FastAPI + scanner + SQLite (dip-scanner serve), which answers nobody without the secret
```

There is one login, the Fly app's: the browser only ever talks to the Vercel address, so the session cookie (`dsid`)
lives there and travels with every request; the React pages send it to the Fly API from the server.

Set up the Fly app first, following [DEPLOY.md](DEPLOY.md) up to step 8 (it works on its own
`https://<your-app>.fly.dev` address until step 6 below). Everything here was checked against Vercel's and Next.js's
documentation on 2026-09-28 (the pages are listed at the [end](#what-was-checked-and-when)); if a screen looks
different, the linked page is what counts.

## Before you start

- **The Fly app running** at `https://<your-app>.fly.dev`, with your admin account (DEPLOY.md steps 1 to 8), and the
  repository on GitHub with a `main` branch (DEPLOY.md step 10).
- **A Vercel account**: sign up at https://vercel.com/signup with your GitHub account. The **Hobby** plan is free and
  enough for a site you and a few friends use, but it is for personal, non-commercial use only (Vercel's fair use
  guidelines): if you ever charge people for the site, move to Pro. See [Hobby plan](#hobby-plan).
- **Python** on your computer, to make a secret.

## 1. Make the shared secret

The front door proves each request to the Fly app with a secret that only the two know. Make a new one (not the
Fly app's `SECRET_KEY`):

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Keep it at hand for steps 3 and 6; it must be at least 32 characters, and it is the same value on both sides.

## 2. Import the repository

1. On https://vercel.com/new, "Import Git Repository": pick this repository (the first time, allow Vercel's GitHub
   app to see it).
2. **Root Directory**: click "Edit" and choose `news-dip-scanner/frontend`. Vercel only sees that folder; the front
   end needs nothing outside it (the API contract is in `frontend/contract/`).
3. **Framework Preset**: Next.js (Vercel detects it). Leave the build, output and install commands at their defaults:
   Vercel installs with npm from `package-lock.json` and runs `next build`. The Node.js version comes from
   `package.json` (`"engines": {"node": "22.x"}`).
4. Open "Environment Variables" before deploying and add the two below (step 3), then "Deploy".

`vercel.json` pins the functions (the pages and the proxy) to Frankfurt, `fra1`, next to the Fly app's region `fra`,
so each page's calls to the Fly API stay within Frankfurt (new projects default to Washington, `iad1`); check it
under Settings, Functions, "Function Regions".
Static files come from Vercel's CDN nearest to the visitor.

## 3. The environment variables

| Name | Value | Environments |
|---|---|---|
| `DIP_API_ORIGIN` | The Fly app's address without a path: `https://<your-app>.fly.dev` | Production and Preview |
| `DIP_PROXY_SECRET` | The secret from step 1 | Production and Preview, with **Sensitive** on |

Both are read only on the server: never name them `NEXT_PUBLIC_...`, which would put them in the pages' JavaScript.
A Sensitive variable can't be read back in the dashboard afterwards (it can only be replaced), and Vercel hides its
value in build logs; Vercel only offers Sensitive for Production and Preview, which is all the front end needs. A
changed variable applies to the next deployment only: after changing one, redeploy (Deployments, the newest one,
"Redeploy").

Until both are set, every page answers "This site isn't set up yet"; `npm run dev` on your computer reads them from
`frontend/.env.local` (see `frontend/.env.example`).

## 4. Production branch and builds

- **Production branch**: `main` (Settings, Environments, Production, "Branch Tracking"; Vercel picks `main` by
  itself when the repository has one). Every push to `main` becomes the production site, from the same commits
  GitHub Actions deploys to Fly. Every other branch and pull request gets a preview deployment (step 8).
- **Only build when the front end changed** (optional; saves builds): Settings, Build and Deployment, "Ignored Build
  Step", "Only build if there are changes in a folder", with the folder `.` (the command runs in the Root
  Directory, `news-dip-scanner/frontend`). A change to the Python app alone then doesn't rebuild the front end.
- GitHub Actions checks the front end too (lint, type check, tests and a build, on every change to `frontend/`), so
  a pull request shows both Vercel's preview and the checks.

## 5. Check the deployment

Open the production address Vercel shows (`https://<project>.vercel.app`). With the Fly app still open to everyone,
the sign-in page appears (it is the Fly app's, through the proxy) and signing in works only after step 6, because the
Fly app refuses form posts from an address other than its `BASE_URL`. `https://<project>.vercel.app/healthz` should
show the Fly app's `{"status":"ok",...}`.

## 6. Close the Fly address to everyone but the front door

Give the Fly app the same secret and make the Vercel address the site's address, in the `news-dip-scanner` folder:

```bash
fly secrets set \
  PROXY_SECRET=paste-the-secret-from-step-1 \
  BASE_URL=https://<project>.vercel.app \
  TRUSTED_ORIGINS='https://<project>-git-*-<team>.vercel.app'
```

`TRUSTED_ORIGINS` is optional: it lets preview deployments post forms (step 8); `<team>` is your Vercel team's slug,
the last part of a preview's address. Setting secrets restarts the Machine. From then on:

- `https://<your-app>.fly.dev` answers "Not here" with a link to `BASE_URL` for every page, file and API call (only
  `/healthz` still answers, for Fly's health check);
- the Fly app believes the visitor's address the front door names (`x-dip-client-ip`) only with the secret, so the
  sign-in limits and the log count each visitor, not Vercel's servers;
- invite and password links (`dip-scanner users invite`, the admin pages) point at the Vercel address. A link made
  before the switch pointed at `fly.dev` and no longer opens: make a new one (`dip-scanner users reset-link`).

Sign in at `https://<project>.vercel.app`. To check the Fly side from your computer:

```bash
curl -si https://<your-app>.fly.dev/login | head -1      # HTTP/2 403
curl -si https://<your-app>.fly.dev/healthz | head -1    # HTTP/2 200
```

To go back to the Fly address alone: `fly secrets unset PROXY_SECRET TRUSTED_ORIGINS` and
`fly secrets set BASE_URL=https://<your-app>.fly.dev`.

## 7. Your own domain (optional)

In the Vercel project: Settings, Domains, "Add Domain", e.g. `dips.example.com`, and add the DNS record Vercel shows
at your DNS provider (a `CNAME` for a subdomain, an `A` record for the bare domain); Vercel issues the certificate.
Then make it the site's address on Fly:

```bash
fly secrets set BASE_URL=https://dips.example.com
```

The session cookie belongs to one address: after the switch everyone signs in once more at the new one. Keep the
Fly app's own domain (DEPLOY.md step 9) only if you run it without the front door.

## 8. Preview deployments

Every push to a branch other than `main`, and every pull request, gets a preview at its own address
(`https://<project>-git-<branch>-<team>.vercel.app`, and one per commit), with the Preview environment variables:

- **They use the production Fly app.** The data a preview shows is real, and so is everything done on it: settings
  saved, invites, "Analyse again" (which costs a model analysis like any other). A preview is for trying a new page on
  real data, not a sandbox. A change that also needs a new Fly API has to reach `main` (and Fly) first.
- **Signing in**: the session cookie belongs to one address, so you sign in again on each preview. Its form posts
  need the preview's address in `TRUSTED_ORIGINS` (step 6): either exact addresses (a branch's
  `https://<project>-git-<branch>-<team>.vercel.app`) or the one pattern `https://<project>-git-*-<team>.vercel.app`
  for every branch. Vercel shortens an address whose first part would be over 63 characters, so a long branch name
  may not match the pattern; use shorter branch names. Why a pattern is safe enough, and how it is checked, is under
  [Security model](../README.md#security-model) in the README.
- **Keep previews private**: Settings, Deployment Protection: check that "Vercel Authentication" is on with
  "Standard Protection" (available on every plan, Hobby included). Then only you, signed in to Vercel, can open a
  preview, while the production address stays open for your friends. Password Protection isn't available on Hobby.

## Hobby plan

Free, for personal and non-commercial use only. Its monthly allowances include 1,000,000 function invocations, 4 hours
of active CPU, 100 GB of fast data transfer and 10 GB of fast origin transfer, and a function may run for up to 5
minutes. Every React page and every request passed on to Fly (a page, a form, a stylesheet, an API call) is one
function invocation that mostly waits for Fly, which costs little CPU: a few people reading the site on their phones
use a small fraction of it. The proxy gives up on Fly after 30 seconds. Vercel's usage page (the team's Usage tab)
shows where you are.

## Updating

- **Front end only** (`frontend/`): merge into `main`; Vercel builds and deploys it, and the Fly app is untouched.
- **Fly app only**: merge into `main`; GitHub Actions deploys Fly (DEPLOY.md step 10). While the Machine restarts
  (seconds to a minute) the site answers "The scanner's server can't be reached just now".
- **Both, with a change to the API contract** (`frontend/contract/api-v1.schema.json`, checked by the tests on both
  sides): in one pull request; add new fields to the API before the front end relies on them, since Vercel is usually
  live a minute before Fly.
- **A new secret**: set it in Vercel (and redeploy) and with `fly secrets set PROXY_SECRET=...`; in between, pages
  answer "Not here", so do it when nobody is using the site.

## Troubleshooting

| Problem | What to check |
|---|---|
| Every page says "This site isn't set up yet" | `DIP_API_ORIGIN` or `DIP_PROXY_SECRET` is missing or malformed in this environment (Production or Preview); the function log says which. Set it and redeploy. |
| Every page says "Not here: This address only answers through the website's front door" | The two secrets differ: `DIP_PROXY_SECRET` on Vercel must equal `PROXY_SECRET` on Fly. Set both again and redeploy on Vercel. |
| "The scanner's server can't be reached just now" or "took too long" | The Fly app is down or restarting: `fly status`, `fly logs`. A deploy restarts it for up to a minute. |
| Signing in answers "This form was sent from another site" | The address you use isn't the Fly app's `BASE_URL` (or in `TRUSTED_ORIGINS` for a preview): `fly secrets set BASE_URL=https://<the address you use>`. |
| Signed in, but the ideas page goes back to the sign-in page | The session cookie wasn't kept: open the site on `BASE_URL`'s exact address (with or without `www`), over https. |
| An invite or password link opens "Not here" | It was made before `BASE_URL` pointed at Vercel: make a new one. |
| A preview can't be opened by a friend | Deployment Protection keeps previews to you (step 8). Share the production address instead. |
| The build fails on Vercel | Settings, Build and Deployment: the Root Directory must be `news-dip-scanner/frontend`. `npm ci && npm run build` in that folder shows the same error locally. |

## What was checked, and when

All on 2026-09-28, with Next.js 16.3.6:

- Monorepos and the Root Directory: https://vercel.com/docs/monorepos and
  https://vercel.com/docs/builds/configure-a-build#root-directory
- Environment variables, per environment, and Sensitive ones: https://vercel.com/docs/environment-variables and
  https://vercel.com/docs/environment-variables/sensitive-environment-variables
- The production branch and preview branches: https://vercel.com/docs/git and
  https://vercel.com/docs/deployments/preview-deployments
- Ignored Build Step: https://vercel.com/docs/project-configuration/project-settings
- Function regions (`regions` in `vercel.json`): https://vercel.com/docs/functions/configuring-functions/region
- Deployment Protection (Standard Protection, Vercel Authentication, what Hobby has):
  https://vercel.com/docs/deployment-protection
- The Hobby plan and its limits: https://vercel.com/docs/plans/hobby and https://vercel.com/docs/limits
- Custom domains: https://vercel.com/docs/domains/working-with-domains/add-a-domain
- Request headers Vercel sets (`x-real-ip`, `x-forwarded-for`): https://vercel.com/docs/headers/request-headers
- Next.js: `proxy.ts` (formerly middleware), route handlers, CSP with nonces, `fetch` caching and environment
  variables, listed in [`frontend/README.md`](../frontend/README.md#security).
