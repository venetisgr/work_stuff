# End-to-end tests

`npm run e2e` drives a real browser through this app's `next start` in front of a real Fly app, as on Vercel:
signing in on the Fly page through the proxy, the React ideas and an idea with its debate and chart, "Analyse again"
(a whole debate, run by stand-in models), saving the Fly settings page (a webhook on a private network is refused),
the Fly news, track record and admin pages, and signing out. No page may log an error, break its
Content-Security-Policy, send the browser to the Fly app or redirect it there; the session cookie must be set on the
front end's host. `front-door.spec.ts` checks that the Fly app refuses everything but `/healthz` without the proxy
secret.

The tests don't start the servers: they need three, each in its own terminal.

**1. Stand-in models** (OpenAI's and Anthropic's APIs, answering from the price in the prompt; no keys, no cost):

```bash
npm run e2e:models                      # http://127.0.0.1:8099
```

**2. The Fly app** on a copy of a database that has a few ideas (ideally one with a debate) and an admin account,
in proxy mode, with the stand-in models. From `news-dip-scanner/`, with a `.env.e2e` like this:

```bash
DATA_DIR=/path/to/a/copy/of/data
SECRET_KEY=<32+ random characters>
BASE_URL=http://localhost:3000
COOKIE_SECURE=false
PROXY_SECRET=<32+ random characters>
LLM_PROVIDER=openai
LLM_ANALYSIS_MODE=debate
OPENAI_API_KEY=sk-e2e
OPENAI_BASE_URL=http://127.0.0.1:8099/v1
ANTHROPIC_API_KEY=sk-ant-e2e
ANTHROPIC_BASE_URL=http://127.0.0.1:8099
```

```bash
dip-scanner --env-file .env.e2e serve --no-scanner --port 8080
dip-scanner --env-file .env.e2e users add-admin e2e@example.com   # once; set its password through the link
```

(`pip install -e ".[web,anthropic]"` first. The analysis still fetches real prices and headlines, so it needs the
network.)

**3. This app**, built, with the same secret:

```bash
npm run build
DIP_API_ORIGIN=http://127.0.0.1:8080 DIP_PROXY_SECRET=<the same secret> npm start     # http://localhost:3000
```

Then (the first time, `npx playwright install chromium` fetches the browser, or set `E2E_CHROMIUM`):

```bash
E2E_EMAIL=e2e@example.com E2E_PASSWORD='<its password>' npm run e2e
```

| Setting | Default | Meaning |
|---|---|---|
| `E2E_EMAIL`, `E2E_PASSWORD` | none | An admin account on the Fly app's database (required). |
| `E2E_BASE_URL` | `http://localhost:3000` | This app. Use `localhost`, the host of the Fly app's `BASE_URL`, or its Origin check refuses the forms. |
| `E2E_FLY_ORIGIN` | `http://127.0.0.1:8080` | The Fly app, reached directly for the refusal checks. |
| `E2E_SHOTS` | none | A folder: also take full-page screenshots of every page, React and Fly alike, 390 and 1280 wide, light and dark (and fail on sideways scrolling). |
| `E2E_CHROMIUM` | Playwright's own | The path of a Chromium to launch instead (e.g. one installed by the system). |

Each run adds an analysis to the database ("Analyse again") and saves the account's settings as they were; start
from a fresh copy of the database now and then.
