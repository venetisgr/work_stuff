# Deploying to Fly.io

This puts the website and the scanner (`dip-scanner serve`) on [Fly.io](https://fly.io): one small virtual machine
(a Fly Machine) in Frankfurt, running around the clock, with the database on a 1 GB disk (a Fly volume) and HTTPS at
`https://<your-app>.fly.dev` or your own domain. Fly costs about $3.85 a month for this (see [Costs](#costs)); the
language models are billed separately by their providers (see [Costs](../README.md#costs) in the README).

The setup described here, and the one `fly.toml` holds: OpenAI's small model triages the news, every dip's analysis is
a debate between OpenAI's and Anthropic's analysis models (`LLM_ANALYSIS_MODE=debate`, see
[Debate](../README.md#debate) in the README), alerts reach each person through their own Slack or Discord webhook,
GitHub Actions deploys every push to the `main` branch, and a Next.js front end on Vercel is the address people use
([The Vercel front door](#the-vercel-front-door), and its own guide, [VERCEL.md](VERCEL.md)). Set up Fly first, on its
own address, with the steps below; then Vercel.

Everything below was checked against Fly's documentation on 2026-09-28 (the pages are listed at the
[end](#what-was-checked-and-when)). Fly changes quickly: if a command answers differently, its `--help` and the linked
page are what count.

The files involved, in the `news-dip-scanner` folder except the workflow:

| File | What it does |
|---|---|
| [`Dockerfile`](../Dockerfile) | Builds the image: Python 3.12, the package with its `web` and `anthropic` extras, `scanner.toml` and `feeds.toml` in `/app`, runs as the user `app` (not root), `DATA_DIR=/data`, `dip-scanner serve` on port 8080. |
| [`.dockerignore`](../.dockerignore) | Keeps `.env`, `data/`, caches, tests and the front end (`frontend/`, Vercel's) out of the build (and off Fly's builder). |
| [`fly.toml`](../fly.toml) | The app: region, the volume at `/data`, the HTTP service with its `/healthz` check, one Machine that never stops, 512 MB, and the settings that aren't secret (the debate among them). |
| [`.github/workflows/news-dip-scanner.yml`](../../.github/workflows/news-dip-scanner.yml) | Tests every change (the Python app and the Next.js front end), and deploys the Python app from `main` when a Fly token is set up. |

## One Machine, always

The scanner runs inside the website's process. Exactly one Machine must run the app:

- two Machines would run two scanners: every article triaged twice, every dip analysed twice (twice the model bill),
  and every alert sent twice;
- the SQLite database lives on the volume, and a volume belongs to one Machine; a second Machine would get an empty
  database of its own.

So `fly.toml` keeps the Machine running when nobody visits (`auto_stop_machines = "off"`, `min_machines_running =
1`), every deploy uses `--ha=false` so that Fly never adds a second Machine for availability (with a volume it makes
only one anyway), and [step 6](#6-check-that-exactly-one-machine-runs) checks with `fly scale count 1`. A deploy stops
the Machine before starting the new version, so the site is unavailable for a few seconds to a minute each time; that
is the price of never running two scanners.

## Before you start

- **A Fly.io account with a payment card.** The free trial gives 2 hours of Machine time or 7 days, whichever comes
  first, and trial Machines stop after 5 minutes: not enough for a scanner that runs all day.
- **API keys for OpenAI and Anthropic**: an OpenAI key from https://platform.openai.com/api-keys and an Anthropic key
  from https://console.anthropic.com/settings/keys, each with prepaid credit and a monthly spend limit (see
  [Costs](../README.md#costs) in the README: the debate costs about two to five times as much per dip as one model).
  The website starts without them and says that the scanner is stopped, so you can also add them later. With only an
  OpenAI key, remove `LLM_ANALYSIS_MODE = "debate"` from `fly.toml` (one model analyses each dip).
- This repository on your computer, and a terminal in the `news-dip-scanner` folder.

## 1. Install flyctl and sign in

flyctl is Fly's command line tool; the commands are `fly ...` (or `flyctl ...`).

```bash
brew install flyctl                                  # macOS with Homebrew
curl -L https://fly.io/install.sh | sh               # macOS or Linux; add ~/.fly/bin to PATH as it says
pwsh -Command "iwr https://fly.io/install.ps1 -useb | iex"   # Windows PowerShell
```

Then create an account (it opens the browser) or sign in, and add a card under Billing in the dashboard:

```bash
fly auth signup      # or: fly auth login
fly version          # v0.4.108 was the current one on 2026-09-28
```

## 2. Create the app

App names are global on Fly and become the address, so pick one nobody has taken:

```bash
fly apps create my-dip-scanner
```

Then put that name in `fly.toml`:

```toml
app = "my-dip-scanner"
```

Commit the change if you are going to deploy from GitHub (step 10). `fly apps create` leaves `fly.toml` alone.
`fly launch --no-deploy` also creates an app, but offers to rewrite `fly.toml` with its own defaults (among them
`auto_stop_machines = "stop"`, which would stop the scanner whenever nobody visits); if you use it, keep this
repository's file (`--copy-config`) and check `git diff fly.toml` afterwards.

**Region.** `primary_region = "fra"` (Frankfurt) is the nearest Fly region to Greece. Fly's European regions on
2026-09-28 were Amsterdam (`ams`), Stockholm (`arn`), Paris (`cdg`), Frankfurt (`fra`) and London (`lhr`); there is
none in Greece or the Balkans any more. `fly platform regions` shows the current list. If you choose another one,
change `primary_region` and use the same code in step 3: a volume and its Machine are always in the same region.

## 3. Create the volume

```bash
fly volumes create scanner_data --region fra --size 1 --snapshot-retention 14
```

Fly warns that a single volume means downtime and data loss if its server fails, and asks whether to go on: answer
yes. That risk is accepted here, and [Backups](#12-backups) covers it. The name must match `[mounts] source` in
`fly.toml`. 1 GB lasts a long time: in testing, a day's news (about 250 articles) and 24 analyses took under 1 MB
of database and about 750 kB of reports, about 30 kB per analysis. Articles are deleted after 30 days, but reports and
opportunities are kept, so at 40 analyses a day (the default daily limit) the reports grow by about 450 MB a year.
`fly.toml` lets Fly grow the volume by 1 GB when it is 80% full, up to 5 GB. Volumes are encrypted at rest by default.

## 4. Set the secrets

Secrets are environment variables that Fly keeps encrypted and gives to the Machine at boot; they never go into the
image or the repository. First make a secret key for the website:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Then set it, the site's address and the two model keys in one go (before the first deploy they are only stored; after
it, every change restarts the Machine):

```bash
fly secrets set \
  SECRET_KEY=paste-the-generated-key \
  BASE_URL=https://my-dip-scanner.fly.dev \
  OPENAI_API_KEY=sk-proj-... \
  ANTHROPIC_API_KEY=sk-ant-... \
  SEC_USER_AGENT="Your Name you@example.com"
```

Or put the lines (`NAME=value`, one per line) in a file outside the repository and import it, so the values don't end
up in your shell history: `fly secrets import < fly-secrets.env`, then delete the file. `fly secrets list` shows the
names, never the values.

| Setting | Needed? | What for |
|---|---|---|
| `SECRET_KEY` | yes | Signs the sign-in forms and the site's short messages; at least 32 characters. Sessions don't depend on it: changing it only makes a sign-in page that is open at that moment ask to be reloaded. |
| `BASE_URL` | yes | The site's address, without a trailing slash: links in invites and password emails point at it, and form posts from any other address are refused. |
| `OPENAI_API_KEY` | for the scanner | Triage (`gpt-5-mini`) and one of the two debaters (`gpt-5`). |
| `ANTHROPIC_API_KEY` | for the debate | The other debater (`claude-sonnet-5`). Without it the scanner stops with a message naming it; the image already has Anthropic's package. Other models or providers: `LLM_DEBATERS` and `LLM_DEBATE_JUDGE` under `[env]` (see `.env.example`). |
| `SEC_USER_AGENT` | recommended | US quarterly figures and the SEC feed. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | not needed | Only if users should be able to choose email alerts, or invites should be emailed. |
| `TELEGRAM_BOT_TOKEN` | not needed | Only if users should be able to choose Telegram alerts. |
| `WEBHOOK_URL`, `EMAIL_TO`, `TELEGRAM_CHAT_ID`... | optional | The command line's own channels (see `.env.example`): they get every alert under `scanner.toml`'s `[alerts]` rules, and the "scanner stopped" notices. On the website each user sets up their own channels instead, and admins get those notices through theirs. |
| `PROXY_SECRET`, `TRUSTED_ORIGINS` | with the Vercel front door | Only once the Next.js front end on Vercel is the site's address: see [The Vercel front door](#the-vercel-front-door). |

Settings that aren't secret go under `[env]` in `fly.toml` (they need a deploy to change). It already has
`DISPLAY_TZ = "Europe/Athens"` (users choose their own time zone on the website), `LLM_PROVIDER = "openai"` (triage)
and `LLM_ANALYSIS_MODE = "debate"`, and it is the place for `LLM_DEBATERS`, `LLM_DEBATE_JUDGE`,
`ANALYZE_LIMIT_PER_USER`, `SCANNER_ENABLED` or `LLM_TRIAGE_REASONING_EFFORT=low` (see [Costs](../README.md#costs)).
How the debate runs (only on disagreement, the number of rebuttal rounds) is set in `scanner.toml`'s `[debate]`. A
secret wins over an `[env]` entry of the same name. Leave `COOKIE_SECURE` alone: on Fly the site is always HTTPS.

**Alerts need no secrets here.** Each person (you included) sets up their own Slack or Discord webhook on the
website's Settings page, and the alerts go straight to their phone through the Slack or Discord app: no SMTP server or
Telegram bot is needed (see [step 8](#8-invite-people)). The `SMTP_*` and `TELEGRAM_*` settings only add email and
Telegram as further choices.

## 5. Deploy

```bash
fly deploy --ha=false
```

Fly's builder builds the image from the `Dockerfile` (a few minutes the first time, less afterwards), creates one
Machine in Frankfurt with the volume at `/data`, starts `dip-scanner serve` and waits until `/healthz` answers. Then
open `https://my-dip-scanner.fly.dev/healthz`:

```json
{"status":"ok","db":"ok","scanner":"running","last_cycle":"2026-09-28T09:05:12+00:00"}
```

`"scanner":"stopped"` with no `last_cycle` usually means that the model key is missing or refused: the website works,
and its admin page says why once you have signed in (step 7). The first cycle starts right away and triages the last
24 hours of news, like `dip-scanner run` does.

## 6. Check that exactly one Machine runs

```bash
fly scale count 1
fly machine list       # one Machine, state "started", with a volume (vol_...)
fly volumes list       # one volume, attached to that Machine
```

`fly scale count 1` changes nothing when there is one Machine, and removes the others when there are more. A removed
Machine's volume stays behind, unattached (and billed, $0.15 per GB a month); once you are sure the right Machine
kept the right volume, remove the spare with `fly volumes destroy <volume id>`. Never use `fly machine clone` or
`fly scale count 2` for this app.

## 7. Create your admin account

```bash
fly ssh console -C "dip-scanner users add-admin you@example.com"
```

It prints a link to choose your password (at least 10 characters), valid for 48 hours and usable once. Open it on
your phone or computer, choose the password, and you are signed in. Your name can be set on the Settings page, or
with `--name` after the address.

`fly ssh console` runs commands as root in the running Machine, with the app's secrets and settings, in `/app`. The
`users` and `backup` commands only change the database, which the website keeps open, so running them as root is
fine. Don't run `dip-scanner run`, `watch` or `analyze` there: the scanner already runs (use "Run a cycle now" on the
admin page, or "Analyse now"), and the report and cache files they write would belong to root, which the website
then can't update.

## 8. Invite people

On the website, open Admin, then Invites: "Create invite link", optionally for one email address and as admin or
member. Copy the link and send it; it works once, for 7 days. With `SMTP_*` set, the site can email it too. The same
from the command line:

```bash
fly ssh console -C "dip-scanner users invite friend@example.com"
```

There is no sign-up page. Each person then sets up their watchlist, alert rules, currency, time zone and alert
channels under Settings, and "Send test alert" checks the channels. The Users page disables accounts, changes roles
and makes password links for people who forgot theirs.

**Alerts on the phone through Slack or Discord.** The usual channel is a webhook of one's own, which posts into a
Slack or Discord channel; the Slack or Discord app on the phone then shows each alert as a notification. Each person
makes one (Slack's steps checked against its documentation on 2026-09-28; Discord's help centre refused automated
reading, so its labels are as guides quoting it give them):

- **Slack:** at https://api.slack.com/apps?new_app=1 create an app ("From scratch", any name, your workspace), open
  **Incoming Webhooks**, switch on **Activate Incoming Webhooks**, click **Add New Webhook to Workspace**, pick the
  channel (a private one, or a direct message to yourself) and **Authorize**. Copy the URL, which starts with
  `https://hooks.slack.com/services/`.
- **Discord:** in a server where you may manage webhooks (your own), open the channel's **Edit Channel**, then
  **Integrations**, **Webhooks**, **New Webhook** (or **Create Webhook**), and **Copy Webhook URL**. It starts with
  `https://discord.com/api/webhooks/`.

On the website: Settings, the webhook channel, paste the URL, choose Slack or Discord as the format, save, then "Send
test alert". The URL is a secret (anyone who has it can post to that channel): the site stores it for that person
only, and never shows it in a notice or a log. Admins get the "dip-scanner stopped" and "unavailable" notices through
their own channels the same way.

## 9. Your own domain (optional)

```bash
fly certs add dips.example.com
```

It shows the DNS records to create at your domain's DNS provider:

- **a subdomain** (`dips.example.com`): a CNAME record pointing at the target that `fly certs setup
  dips.example.com` shows (the app's own `fly.dev` name or a unique one);
- **the domain itself** (`example.com`): the A and AAAA records from the output (`fly ips list` shows them too).

`fly certs check dips.example.com` shows when the certificate is issued (usually minutes after DNS is right). Then
switch the site to the new address:

```bash
fly secrets set BASE_URL=https://dips.example.com
```

From then on use only the new address: invite and password links point at it, and a form sent from the old
`fly.dev` address is refused as coming from another site. Behind Cloudflare's proxy (the orange cloud), add the
`_fly-ownership` TXT record that `fly certs setup` shows and use the SSL mode "Full (strict)". The first 10
single-name certificates of an organisation are free, then $0.10 a month each.

## 10. Deploy from GitHub Actions

The workflow in `.github/workflows/news-dip-scanner.yml` checks every pull request and push that touches
`news-dip-scanner/`, each part on the changes that concern it: ruff and the Python tests when the Python app (or the
API contract in `frontend/contract/`, which its tests check the API against) changed, and the Next.js front end's
lint, type check, tests and build when `frontend/` changed. After the Python checks pass it deploys the `main`
branch:

- on every push to `main` that changed the Python app (a change to the front end alone is Vercel's to deploy, see
  [VERCEL.md](VERCEL.md), and never restarts the scanner),
- or when you start it by hand on `main`: Actions, news-dip-scanner, "Run workflow", branch `main` (on any other
  branch it only runs the checks).

Other branches are never deployed: work on a branch, open a pull request, and merge it into `main` to release it.
`main` is also the production branch of the Vercel front end, so both go out from the same commits. If the repository
has no `main` yet, create it from the branch you deploy today and push it:

```bash
git switch -c main
git push -u origin main
```

Then, in the repository on GitHub, Settings, General, "Default branch": switch it to `main`, so that pull requests
target it. A branch protection rule (Settings, Branches) that requires the workflow's "Lint and test" and "Front end
(Next.js)" checks before merging keeps a failing change from reaching the site (a check that didn't need to run
counts as passed).

It needs a deploy token, which can manage this one app and nothing else in your account:

```bash
fly tokens create deploy --name github-actions --expiry 8760h
```

`8760h` is a year (Fly's default is 20 years, and Fly recommends a shorter expiry). Copy the whole output, starting
with `FlyV1 ` and its space. In the repository on GitHub: Settings, Secrets and variables, Actions, "New repository
secret", name `FLY_API_TOKEN`, value the token. Commit `fly.toml` with your app name and push.

Without the secret the deploy job only notes "Not deployed". When the token expires, deploys fail with an
authentication error: make a new token and replace the secret. `fly tokens list` and `fly tokens revoke <id>` manage
them. The app's own secrets (step 4) stay on Fly; GitHub only ever has the deploy token.

## 11. Logs and health

```bash
fly logs               # follows the log; Ctrl+C to stop
fly logs --no-tail     # what is in the buffer now
fly status             # the Machine, its state and its health check
```

Each scanner cycle logs one summary line (feeds, new articles, candidates, opportunities, alerts, the day's model use),
and each request a line without its query string or any link token. The Admin page shows the same from the database:
the scanner's state with pause, resume and "Run a cycle now", recent cycles with their notes, feed health and the
model's use and estimated cost. `https://my-dip-scanner.fly.dev/healthz` suits an uptime monitor: it answers 200
while the database works (503 when it can't be read), and a monitor that looks for `"scanner":"running"` in it also
notices a stopped or stalled scanner.

## 12. Backups

Three layers, from least to most effort:

1. **Fly's volume snapshots.** Fly snapshots the whole volume every day and keeps each snapshot for 14 days here
   (`--snapshot-retention 14` in step 3; Fly's default is 5, the range 1 to 60). They may be up to a day old, and Fly
   says not to rely on them alone. Take one by hand before anything risky:

   ```bash
   fly volumes list                              # the volume's id, vol_...
   fly volumes snapshots create vol_xxxxxxxxxxxx
   fly volumes snapshots list vol_xxxxxxxxxxxx
   ```

2. **`dip-scanner backup`.** A consistent copy of the database, taken with SQLite's backup API while the scanner
   runs, into `/data/backups/scanner-YYYYmmdd-HHMMSS.sqlite3` (UTC); the newest 7 are kept (`--keep N`). It is on
   the same volume, so it helps against mistakes, not against losing the volume.

   ```bash
   fly ssh console -C "dip-scanner backup"
   ```

3. **A copy on your computer.** Download a backup now and then:

   ```bash
   fly ssh console -C "ls -l /data/backups"
   fly ssh sftp get /data/backups/scanner-20260928-091500.sqlite3 scanner-20260928-091500.sqlite3
   ```

   Keep it private: it holds the accounts (email addresses and password hashes), users' alert settings (webhook
   addresses, Telegram chat ids) and everything the scanner found.

## 13. Restoring

**From a backup file** (to undo a mistake, or to move to a new app). Everything after the backup is lost.

1. Put the file on the volume: `fly ssh sftp put scanner-20260928-091500.sqlite3 /data/restore.sqlite3`, or for one
   already in `/data/backups`, `fly ssh console -C "cp /data/backups/scanner-20260928-091500.sqlite3
   /data/restore.sqlite3"`.
2. Open a shell with `fly ssh console` and paste this. It copies the backup into the live database with SQLite's
   backup API, which is safe while the website runs (it waits a moment meanwhile):

   ```bash
   python - <<'EOF'
   import sqlite3
   source = sqlite3.connect("file:/data/restore.sqlite3?mode=ro", uri=True)
   target = sqlite3.connect("/data/scanner.sqlite3", timeout=60)
   source.backup(target)
   target.close()
   source.close()
   print("Restored.")
   EOF
   rm -f /data/restore.sqlite3*
   exit
   ```

3. Restart, so that the website and the scanner start from the restored data: `fly apps restart my-dip-scanner`.

Don't copy a backup over `scanner.sqlite3` while the site runs: the database's `-wal` file next to it belongs to the
running database and would be replayed into the copy.

**From a volume snapshot** (the volume or its server is gone). This makes a new Machine with a new volume filled from
the snapshot:

```bash
fly volumes list                                  # the old volume's id
fly volumes snapshots list vol_xxxxxxxxxxxx       # pick a snapshot, vs_...
fly scale count 0                                 # removes the Machine; the old volume stays
fly scale count 1 --region fra --with-new-volumes --from-snapshot vs_xxxxxxxxxxxx
```

`--from-snapshot last` takes the newest one. Check the site, then remove the old volume with `fly volumes destroy
vol_xxxxxxxxxxxx`.

## 14. Changing the scanner's settings

The image carries `scanner.toml` and `feeds.toml` (in `/app`). There are two ways to change them:

- **In the repository** (recommended): edit the files and deploy (`fly deploy --ha=false`, or push and let GitHub
  Actions do it). The change is in git with everything else.
- **On the volume**, without a deploy: a `scanner.toml` or `feeds.toml` in `/data` is used instead of the image's
  copy, until you delete it.

  ```bash
  fly ssh sftp get /app/scanner.toml scanner.toml           # the current file, to edit
  fly ssh sftp put scanner.toml /data/scanner.toml          # upload the edited file
  fly ssh console -C "dip-scanner --config /data/scanner.toml news --hours 1"   # check it
  fly apps restart my-dip-scanner                           # use it
  ```

  For the feed list, the same with `feeds.toml`, checked with `dip-scanner --feeds /data/feeds.toml feeds`. Always
  check before restarting: the website reads the files when it starts, and a mistake in one (a misspelled key is an
  error, never ignored) stops it from starting; see [Troubleshooting](#troubleshooting) if that happened. To go
  back to the image's copy: `fly ssh console -C "rm /data/scanner.toml"` and restart. Remember that the next change
  to the file in the repository won't be used while a copy is on the volume.

## 15. Updating

Merge or push to `main` (GitHub Actions deploys after the tests pass), or run `fly deploy --ha=false` here. The
Machine stops (the scanner finishes or abandons its cycle within about 10 seconds), the new version starts, and
database changes are applied automatically when it opens the database. `fly version upgrade` updates flyctl itself.

Memory and the other Machine settings come from `[[vm]]` in `fly.toml` on every deploy: a `fly scale memory 1024`
without changing `fly.toml` lasts until the next deploy.

## The Vercel front door

The site can have a front door on Vercel: the Next.js app in `frontend/` (its guide is [VERCEL.md](VERCEL.md))
becomes the only address people use. It shows the ideas and each idea's page in React, with the data from this app's
JSON API (`/api/v1`), and passes every other request (signing in, settings, news, track record, admin) on to this app.
Every request it makes here carries a shared secret, and this app then answers nobody without it.

Set up and deploy the Vercel project first, with `DIP_API_ORIGIN` (this app's address,
`https://my-dip-scanner.fly.dev`) and `DIP_PROXY_SECRET` (a new random secret, not `SECRET_KEY`):

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Then give this app the same secret, and make the Vercel address the site's address:

```bash
fly secrets set \
  PROXY_SECRET=paste-the-same-secret \
  BASE_URL=https://dips.example.com \
  TRUSTED_ORIGINS='https://my-dips-git-*-my-team.vercel.app'
```

| Setting | What for |
|---|---|
| `PROXY_SECRET` | At least 32 characters, exactly the front door's `DIP_PROXY_SECRET`. Every request but `/healthz` must carry it (compared in constant time); without it the answer is 403 with a link to `BASE_URL`, so `my-dip-scanner.fly.dev` is of no use in a browser any more. `/healthz` still answers everybody, for Fly's health check and uptime monitors. |
| `BASE_URL` | Now the Vercel address: your domain on Vercel, or `https://<project>.vercel.app`. Invite and password links point there, form posts from it are accepted, and the refusal page links to it. It is required with `PROXY_SECRET`. |
| `TRUSTED_ORIGINS` | Optional: other addresses whose form posts are accepted, comma-separated. For Vercel's preview deployments, either their exact addresses (a branch's `https://my-dips-git-main-my-team.vercel.app`) or one pattern for every preview of the project: `https://my-dips-git-*-my-team.vercel.app` (branch addresses) or `https://my-dips-*-my-team.vercel.app` (commit addresses too), with your project's name before the `*` and your team's (its slug) after it. |

What changes:

- **Addresses.** Every redirect is relative (`Location: /login?next=%2F`), so visitors stay on the Vercel address. The
  `fly.dev` address answers "Not here" and points at `BASE_URL`; that includes `/static/` and the JSON API.
- **Limits and the log.** The front door names the visitor's address in `x-dip-client-ip`, and this app believes it
  only on a request that carried the secret: the sign-in limits then count each visitor, not Vercel's servers, and
  the log shows the visitor.
- **Preview deployments use this app**: the data they show is real and so is everything done on them (settings
  saved, "Analyse again", which costs a model analysis like any other). The session cookie belongs to one address, so
  on a preview you sign in again. Keep previews private with Vercel's Deployment Protection. A pattern in
  `TRUSTED_ORIGINS` is looser than an exact address (see [Security model](../README.md#security-model) in the
  README); and Vercel shortens a preview's address when its first part would be over 63 characters, so a preview of
  a long branch name may not match: use a shorter branch name, or list that address.
- **Order.** Once `PROXY_SECRET` is set (it restarts the Machine), the site only works through the front door, so
  set up Vercel first. To go back: `fly secrets unset PROXY_SECRET TRUSTED_ORIGINS` and `fly secrets set
  BASE_URL=https://my-dip-scanner.fly.dev`.
- **A new secret.** Set it in Vercel (and redeploy there) and with `fly secrets set PROXY_SECRET=...`; between the two,
  pages answer "Not here", so do it when nobody is using the site.

To check it from your computer (the second command puts the secret in your shell history; clear it afterwards):

```bash
curl -si https://my-dip-scanner.fly.dev/login | head -1                                   # HTTP/2 403
curl -si -H "x-dip-proxy-secret: paste-the-secret" https://my-dip-scanner.fly.dev/login | head -1   # HTTP/2 200
curl -si https://my-dip-scanner.fly.dev/healthz | head -1                                 # HTTP/2 200
```

## Costs

Fly's prices on 2026-09-28, for Frankfurt (15% above the US East price), 30 days of running around the clock:

| What | Price |
|---|---:|
| Machine: shared-cpu-1x, 512 MB | $3.69 |
| Volume: 1 GB | $0.15 |
| Snapshots: $0.08 per GB a month after the first 10 GB, which this app stays far below | $0.00 |
| Shared IPv4 and IPv6 address | $0.00 |
| Certificate for your own domain (first 10 free) | $0.00 |
| Data out: $0.02 per GB in Europe; pages are a few kB | cents |
| **Total** | **about $3.85 (about €3.40)** |

The euro figure uses 1 EUR = 1.1386 USD, like the README. For comparison: 256 MB would be $2.24 but is too small (see
below), 1 GB $6.57. Fly bills by the second, so a Machine stopped for part of the month costs less, but this one is
meant to run all the time. The models are billed by their providers: with the debate on disagreement, as `fly.toml`
sets it up, and half the dips debated, roughly $19 a month when the news is quiet, $76 on typical days and $261 when
every day is busy enough to reach the daily limit of 40 analyses (see [Costs](../README.md#costs), which also shows
how to bring it down).

**Why 512 MB.** Measured on 2026-09-28 with this image (Python 3.12, limited to 1 CPU): the website and the scanner on
the real feeds, with a stand-in model that sent up to 8 tickers a cycle to analysis (24 analyses in all, with their
prices, SEC figures and context news fetched for real, reports written and alerts worked out), and every kind of page
loaded over 2,000 times. The process used 117 MB after the first cycle and levelled off at about 170 MB after 7 cycles
(the highest reading was 169 MB): it keeps memory it has used once, so the figure grows over the first cycles and then
stays. Without `MALLOC_ARENA_MAX=2` (set in the `Dockerfile`) it levelled off about 75 MB higher. The debate adds
Anthropic's package, which took about 27 MB more once loaded (measured on 2026-09-28: 64 MB before importing it and
creating a client, 91 MB after), so expect about 200 MB. A 256 MB Machine leaves about 210 MB for the app, too little;
512 MB leaves room for busy moments, and `swap_size_mb = 512` in `fly.toml` catches a spike instead of the kernel
stopping the process.

## Troubleshooting

| Problem | What to do |
|---|---|
| `fly deploy` says the volume `scanner_data` doesn't exist, or can't be mounted | Create it in `primary_region` (step 3). A volume in another region doesn't count. |
| The deploy fails its health check, and `fly logs` says `Configuration problem: SECRET_KEY ...` | Set `SECRET_KEY` (step 4); it must be at least 32 characters. |
| The site says "Stopped: Configuration problem: Set OPENAI_API_KEY in .env ..." | On Fly that means the secret: `fly secrets set OPENAI_API_KEY=...` (it restarts the Machine). For "no credit" and other account problems, fix them at the provider, then "Start again" on the admin page. |
| "Stopped: Configuration problem: LLM_ANALYSIS_MODE=debate uses anthropic:claude-sonnet-5 (LLM_DEBATERS), but ANTHROPIC_API_KEY isn't set" | `fly secrets set ANTHROPIC_API_KEY=...`; or, to analyse with OpenAI alone, remove `LLM_ANALYSIS_MODE` from `fly.toml`'s `[env]` and deploy. |
| A notice "dip-scanner: Anthropic unavailable, analysing with OpenAI only" (or the other way round) | One of the two debaters failed (a rejected key, no credit, an outage); the other analyses each dip alone meanwhile and the scanner keeps running. The notice names the error; fix it at that provider. At most one such notice per provider every 12 hours. |
| Forms answer "This form was sent from another site, so it was refused." | You are on another address than `BASE_URL` (e.g. the `fly.dev` one after moving to your own domain). Use the `BASE_URL` address, or correct the secret. |
| Links in invites point to the wrong address | `BASE_URL` (step 4, or step 9). The links already sent keep the old address. |
| Every page says "Not here: This address only answers through the website's front door" | `PROXY_SECRET` is set, and the request didn't carry it: you opened the `fly.dev` address (use `BASE_URL`), or the front door's `DIP_PROXY_SECRET` differs from it (set both again, see [The Vercel front door](#the-vercel-front-door)). |
| The deploy fails its health check, and `fly logs` says `PROXY_SECRET is set, so ... set BASE_URL` | `fly secrets set BASE_URL=https://<the Vercel address>`. |
| Forms on a Vercel preview answer "This form was sent from another site" | Add the preview's address, or the project's pattern, to `TRUSTED_ORIGINS` (see [The Vercel front door](#the-vercel-front-door)). |
| `fly machine list` shows two Machines | `fly scale count 1`, then remove the spare volume (step 6). |
| `Permission denied` or `attempt to write a readonly database` in the log | A file under `/data` belongs to root (made over `fly ssh` or `sftp`). `fly ssh console -C "chown -R app:app /data"`, then `fly apps restart my-dip-scanner`. |
| The Machine keeps restarting after a settings change | A broken `/data/scanner.toml` or `feeds.toml` (step 14): `fly logs` names it. `fly ssh` needs a running Machine, so run it without the website for a moment: `fly machine update <machine id> --command "sleep infinity" --skip-health-checks`, then `fly ssh console -C "rm /data/scanner.toml"` (or fix the file), then `fly deploy --ha=false`, which puts the normal command back. |
| `Out of memory: Killed process` in the log | Set `memory = "1gb"` under `[[vm]]` in `fly.toml` and deploy. |
| `fly ssh console` says the app has no started VMs | `fly machine list`, then `fly machine start <machine id>`. |
| Times on the site or in alerts are in the wrong zone | Each user chooses theirs under Settings; `DISPLAY_TZ` in `[env]` is the default and the log's. |
| No alerts arrive | Settings, "Send test alert", shows each channel's answer. Email and Telegram need the server's `SMTP_*` or `TELEGRAM_BOT_TOKEN` secrets. |

## Security on Fly

- The image runs the website as the user `app`, and the code in it belongs to root. `.dockerignore` keeps `.env` out
  of the build, so secrets only ever reach the Machine as Fly secrets.
- The Machine is only reachable through Fly's proxy, which ends HTTPS (`force_https` sends plain HTTP to HTTPS). The
  server trusts the proxy's `X-Forwarded-Proto` and `X-Forwarded-For`, and uses `Fly-Client-IP` for the sign-in
  limits, or behind the Vercel front door the visitor's address it names, on requests with `PROXY_SECRET` only (see
  [Security model](../README.md#security-model) in the README, which lists the rest of the site's safeguards).
- Anyone with the deploy token can deploy code that reads the app's secrets: keep `FLY_API_TOKEN` in GitHub's secrets
  only. The workflow deploys `main` only, so protect `main` (step 10) and review what is merged into it.
- Backups contain password hashes and users' settings: keep downloaded copies private.

## What was checked, and when

All on 2026-09-28, with flyctl v0.4.108. `fly.toml` was also loaded with flyctl's own configuration code (its
validation and the strict check for unknown keys passed); `fly config validate` itself needs a Fly account.

- App configuration (`fly.toml`: `kill_signal`, `kill_timeout`, `swap_size_mb`, `[http_service]` and its
  `auto_stop_machines` values `"off"`, `"stop"`, `"suspend"`, `[[http_service.checks]]`, `[mounts]` with
  `snapshot_retention` and auto-extend, `[[vm]]`, deploy strategies): https://fly.io/docs/reference/configuration/
- Volumes and snapshots (daily, 5 days by default, 1 to 60): https://fly.io/docs/volumes/overview/ and
  https://fly.io/docs/volumes/snapshots/
- Regions: https://fly.io/docs/reference/regions/
- One Machine for a process group with a volume, and `--ha=false`: https://fly.io/docs/apps/app-availability/
- `fly launch` and `fly deploy` flags (including `--ha`, `--remote-only`): https://fly.io/docs/flyctl/launch/ and
  https://fly.io/docs/flyctl/deploy/
- Secrets: https://fly.io/docs/apps/secrets/
- Continuous deployment with GitHub Actions (`superfly/flyctl-actions/setup-flyctl`, `fly tokens create deploy`):
  https://fly.io/docs/launch/continuous-deployment-with-github-actions/ and
  https://fly.io/docs/flyctl/tokens-create-deploy/
- `fly ssh console` (runs as root by default), `fly ssh sftp get`/`put`, `fly scale count`, `fly volumes create`,
  `fly apps create`, `fly certs add`: the pages under https://fly.io/docs/flyctl/
- Custom domains and certificates: https://fly.io/docs/networking/custom-domain/
- Prices (Machines, volumes, snapshots, certificates, data transfer, the free trial):
  https://fly.io/docs/about/pricing/ and https://fly.io/docs/about/free-trial/
- Installing flyctl: https://fly.io/docs/flyctl/install/
- Slack incoming webhooks (creating the app, "Incoming Webhooks", "Add New Webhook to Workspace", the URL's form):
  https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/
- Discord webhooks (Edit Channel, Integrations, Webhooks, Copy Webhook URL):
  https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks, as quoted by
  https://hookdeck.com/webhooks/platforms/how-to-get-started-with-discord-webhooks (Discord's page answered 403 to
  automated reading)
- From Fly's community forum (not in the docs): the volume's mount point is given to the image's user, and `fly ssh
  console` starts in the image's `WORKDIR`, https://community.fly.io/t/1773 and https://community.fly.io/t/13144
