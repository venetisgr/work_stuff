# News dip scanner

Reads about 20 financial news feeds every few minutes and has a language model work out which listed companies each
story affects. It then checks whether those shares actually fell, and asks a stronger model whether each drop is a
temporary fear or real damage to the business. Every dip gets a chance of being higher in 6 months, a potential low,
limit-order ideas and a score, and ends up in a ranked report and, optionally, an alert.

It is a research and alerting tool. It never connects to a broker and never places orders: you do your own checks and
decide.

## What it replicates

A user on r/PersonalFinanceGreece described a "hobby" system that, by their account, turned €2,500 into €57,500 in
15 months:

1. a "newsletter" collecting news from about 20 sites, refreshed every 5 minutes;
2. a ChatGPT-based step that reads the news and works out which listed companies are affected, directly or
   indirectly;
3. for those companies, a check whether the price dropped, and whether the drop is a passing "fear" or a sign the
   fundamentals are really hit;
4. a score for every opportunity: how likely the price is to rise within 6 months, and what the potential low is;
5. the human then does their own checks, places limit buy and sell orders, and reviews the open orders every couple
   of days.

This project rebuilds steps 1 to 4 as an open, configurable pipeline and adds a track record so you can see whether
the scores mean anything. Step 5 stays with you. Nothing here verifies the claimed result (see [Risks](#risks)).

## How it works

```
 feeds.toml (20 RSS/Atom feeds)
        │  every 5 min, conditional GETs (ETag / Last-Modified)
        ▼
 poll ──► new articles (deduplicated by link and by headline across sources) ──► SQLite (data/scanner.sqlite3)
        │  only articles from the last 24 h go to the model
        ▼
 triage (small model, 20 articles per request)
        │  article ─► [{ticker, relation direct|indirect, direction, magnitude 1-5, event type, rationale}]
        ▼
 candidates: tickers with negative/mixed news in the last 48 h
        │  filters: [universe], [dip] news rules, 24 h cooldown, Yahoo Finance prices
        │  dip = down ≥3% on the day, or ≥6% over 5 days, or ≥10% below the 20-day high
        ▼
 analysis (stronger model, one request per candidate, at most 8 per cycle)
        │  input: price statistics + SEC quarterly figures (US) + flagged news + per-ticker headlines
        │  output: verdict (temporary fear / mixed / fundamental / unclear), P(higher in 6 months),
        │          potential low, entry (limit buy), target (limit sell), fear, thesis, risks, checks
        ▼
 sanity checks on the numbers ──► score 0-100 ──► reports (Markdown, HTML, JSON) ──► alerts (email, Slack,
                                                                                        Discord, Telegram, webhook)
```

## Setup

Requires Python 3.11 or newer.

```bash
cd news-dip-scanner
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .                   # add [anthropic] or [azure] for those providers
cp .env.example .env               # Windows: copy .env.example .env
```

Then fill in `.env`:

- **A language model.** `OPENAI_API_KEY` is enough for the default: OpenAI's API (ChatGPT's models, as the original
  author used), with `gpt-5-mini` for triage and `gpt-5` for the analysis. Alternatives:
  - **Azure AI Foundry**: `LLM_PROVIDER=azure`, `FOUNDRY_ENDPOINT`, `FOUNDRY_DEPLOYMENT` (a deployment name) and
    `FOUNDRY_API_KEY`, or no key for Entra ID (`pip install -e ".[azure]"`). `LLM_TRIAGE_MODEL` and
    `LLM_ANALYSIS_MODEL` can name two different deployments.
  - **Anthropic Claude**: `LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY` (`pip install -e ".[anthropic]"`); the
    defaults are `claude-haiku-4-5` for triage and `claude-sonnet-5` for the analysis.
  - Any OpenAI-compatible server (Ollama, LM Studio, vLLM, a gateway) through `OPENAI_BASE_URL`.
- **`SEC_USER_AGENT`** (recommended): your name and email, e.g. `Jane Doe jane@example.com`. The SEC requires one.
  With it the analysis gets recent quarterly figures for US-listed companies, and the SEC 8-K feed works; without it
  that feed is skipped.
- **Notifications** (optional): email, a Slack/Discord/generic webhook, or Telegram. See the comments in
  `.env.example`.

Check that everything answers, without spending anything on the model:

```bash
dip-scanner feeds --check      # fetches every feed once
dip-scanner prices AMD         # price statistics from Yahoo Finance
dip-scanner run                # one full cycle; prints a summary and the report's path
```

The first cycle triages the last 24 hours of news (about 280 articles, 14 requests, on the Sunday this was tested;
more on weekdays). Older backlog is stored but never sent to the model. Later cycles only see what's new since the
previous one.

## Configuration

| File | What's in it |
|---|---|
| `.env` | Secrets and service settings (see `.env.example`). Real environment variables win over it. |
| [`feeds.toml`](feeds.toml) | The news sources: 20 enabled, plus 10 switched off (two Greek sources and eight checked alternates). Each entry notes what it covers and when it was last verified. |
| [`scanner.toml`](scanner.toml) | Thresholds, watchlist and alert rules. Every key is optional and the file shows the defaults; a misspelled key is an error, never silently ignored. |

The settings you are most likely to change in `scanner.toml`:

| Setting | Default | Meaning |
|---|---|---|
| `[scan] interval_minutes` | 5 | Minutes between cycles in `watch`. |
| `[scan] max_candidates_per_cycle` | 8 | Analyses per cycle; the rest wait for the next cycle (bounds the LLM bill). |
| `[scan] cooldown_hours` | 24 | A ticker isn't analysed again within this time unless new news arrives. |
| `[dip] min_drop_1d_pct` / `min_drop_5d_pct` / `min_drawdown_20d_pct` | 3 / 6 / 10 | What counts as a dip (any one is enough). |
| `[dip] min_magnitude`, `directions`, `include_indirect` | 2, negative + mixed, true | Which news can make a company a candidate. |
| `[universe] watchlist` | none | Tickers that qualify with any news of magnitude 1 or more. |
| `[universe] allowed_suffixes` | all | Exchanges by Yahoo suffix: `""` US, `.DE` Xetra, `.PA` Paris, `.AT` Athens... |
| `[alerts] min_score`, `min_probability`, `verdicts` | 65, 60, temporary fear + mixed | What gets sent as an alert. Everything is in the reports. |

Tickers are Yahoo Finance symbols: `AMD`, `BRK-B`, `SAP.DE`, `ASML.AS`, `OPAP.AT`, `7203.T`, `0700.HK`.

## Commands

After `pip install -e .`, `dip-scanner` works as a shorthand for `python -m dip_scanner`.

| Command | What it does | Needs a model |
|---|---|---|
| `dip-scanner run [--no-notify]` | One cycle; prints a summary, notes on what was skipped and why, and the report's path. | yes |
| `dip-scanner watch [--interval MIN] [--no-notify]` | Cycles on the interval until Ctrl+C. | yes |
| `dip-scanner feeds [--check]` | Lists the feeds and how their last fetch went; `--check` fetches each one now. | no |
| `dip-scanner news [--hours 24] [--ticker T]` | The news digest ("newsletter"): companies with negative news first, then the other headlines. | no |
| `dip-scanner analyze TICKER [--no-save]` | Analyses one ticker now, whatever its price did and ignoring the cooldown. | yes |
| `dip-scanner report [--days 7] [--min-score N] [--html PATH]` | The stored opportunities of the last days. | no |
| `dip-scanner track [--days 365]` | How past opportunities played out (see [Track record](#track-record)). | no |
| `dip-scanner prices TICKER` | Price statistics and whether they count as a dip. | no |

Options for every command: `-v` (debug logging), `--env-file PATH`, `--config PATH`, `--feeds PATH` and
`--data-dir DIR`. Exit codes: 0 ok, 1 runtime error, 2 configuration error.

### Output

Everything goes to `data/` (or `DATA_DIR`):

- `scanner.sqlite3`: articles (kept 30 days), company impacts, feed state and every opportunity (kept for good).
- `reports/YYYY-MM-DD/HHMMSS-opportunities.md`, `.html` and `.json`, written by each cycle that found something,
  plus `reports/latest.md` and `latest.html`. The HTML is a single email-safe file. Its score badges use the same
  bands as the track record: 80+ strong, 65-80 good, 50-65 fair, under 50 weak.
- `cache/sec/`: the SEC ticker list (a day) and each company's figures (12 hours).

Each opportunity in the report shows the price and recent moves, the chance of being higher in 6 months, the
potential low and the statistical low, the entry (limit buy) and target (limit sell idea) with upside and downside,
the verdict and confidence, what the market fears, the fundamental impact, the thesis, risks, catalysts, what to check
before buying, and the headlines that flagged it.

Alerts go to every configured channel. Email and generic webhooks get the full report; Slack, Discord and Telegram
get one line per opportunity. An alert that couldn't be sent is retried for 24 hours; one you created with
`dip-scanner analyze` isn't sent, since you've just read it. A channel that is only half set up is skipped with a
warning naming the missing setting, and Discord messages can't ping anyone (mentions are turned off).

## Scoring

**Severity** only orders the candidates within a cycle (bigger, better-explained drops are analysed first):

```
severity = drop + news + corroboration + volume
  drop          = max(0, -change_1d, -change_5d / 1.5, -drawdown_from_20d_high / 2)      (in %)
  news          = 1.5 × strongest impact's magnitude × relation (direct 1, indirect 0.5)
                        × direction (negative 1, mixed 0.7, other 0.3)
  corroboration = 0.5 per further article about the company, at most 2
  volume        = min(3, volume_ratio - 1) when the day traded above its 20-day average volume
```

**Score** (0-100) ranks the opportunities:

```
prob          = probability_up_6m / 100
up            = max(0, target_price / price - 1)
down          = max(0.01, 1 - potential_low / price)
reward_risk   = up / (up + down)                      # 0.5 = as much upside as downside
score = 100 × (0.7 × prob + 0.3 × reward_risk) × verdict_factor × confidence_factor

verdict_factor:    temporary fear 1.0, mixed 0.85, unclear 0.7, fundamental 0.5
confidence_factor: high 1.0, medium 0.93, low 0.85
```

Example: 68% chance up, low 118, entry 132, target 168 at a price of 142.50, temporary fear, medium confidence:
score 58.5.

Before scoring, the model's numbers are checked and fixed where they contradict each other, with a warning in the
report: the probability is clamped to 0-100; a potential low at or above the price becomes the lower of the
statistical 6-month low and 97% of the price, and one below 30% of the price is raised to that; the entry is clamped
between the low and the price; a target at or below the entry becomes entry × (1 + max(5%, half the annual
volatility)).

The "statistical 6-month low" in the price block is the 5th percentile of a zero-drift lognormal model:
`price × exp(-1.645 × volatility × √0.5)`. The model is told to use it, and the worst 6-month drawdown in the price
history, as anchors for its potential low.

## Costs

All numbers below were measured on a live run (real feeds, 2026-09-27) and are estimates, not quotes. Check your
provider's current prices.

- **Triage**: 20 articles per request, about 2,500-3,000 input tokens each (a 4,000-character system prompt plus
  6,000-8,000 characters of headlines and summaries) and a short JSON reply. The feeds publish a few hundred to a
  couple of thousand new articles a day, so expect roughly 15-100 triage requests a day.
- **Analysis**: one request per candidate, about 3,500 input tokens (price block, fundamentals, up to 12,000
  characters of news) and a reply of 500-1,000 tokens. The cooldown and `max_candidates_per_cycle` keep this to
  somewhere between a handful and a few dozen a day.
- **Total**: roughly 0.05-0.4 million input tokens a day. With a small model for triage this usually costs
  well under a few dollars a day. Reasoning models also bill their thinking as output tokens;
  `LLM_REASONING_EFFORT=low` keeps that down.

Feeds, Yahoo Finance and SEC data cost nothing, but be polite: the defaults poll each feed once per cycle with
conditional requests, and SEC requests are spaced out to at most 5 a second.

## Running it every 5 minutes

**Foreground**, e.g. in `tmux` or `screen`:

```bash
dip-scanner watch
```

Cycles start on the interval's boundaries (:00, :05, :10...), each logs one summary line, a failed cycle is logged
and the next one runs as usual, and Ctrl+C stops it cleanly. Bad credentials or configuration stop it with exit code
2.

**cron** (Linux/macOS), one cycle per run, with `flock` so a slow cycle doesn't overlap the next:

```cron
*/5 * * * * cd /path/to/news-dip-scanner && flock -n /tmp/dip-scanner.lock .venv/bin/dip-scanner run >> data/cron.log 2>&1
```

**systemd** (a user service that restarts after crashes or reboots):

```ini
# ~/.config/systemd/user/dip-scanner.service
[Unit]
Description=News dip scanner

[Service]
WorkingDirectory=/path/to/news-dip-scanner
ExecStart=/path/to/news-dip-scanner/.venv/bin/dip-scanner watch
Restart=on-failure
RestartSec=60

[Install]
WantedBy=default.target
```

Then `systemctl --user enable --now dip-scanner` and `journalctl --user -u dip-scanner -f` for the log.

**Windows Task Scheduler**: create a task that runs every 5 minutes, with the program
`C:\path\to\news-dip-scanner\.venv\Scripts\dip-scanner.exe`, arguments `run`, and "Start in" set to
`C:\path\to\news-dip-scanner` (so `.env`, `scanner.toml` and `data\` are found). Under Settings, choose "Do not start
a new instance" if the task is already running. Or run `dip-scanner watch` in a terminal that stays open.

## Track record

```bash
dip-scanner track
```

replays every stored opportunity against the daily prices that followed it, so you can see whether the scores,
verdicts and probabilities mean anything before trusting them:

- **Signal day**: the UTC date of the report. The window is 183 days (about 6 months) from it.
- **Entry filled**: the first day whose low reached the entry price (the limit buy). When the report's price is from
  the signal day's session, only the range between that price and the day's close counts for that day.
- **Target hit**: the first day after the fill whose high reached the target (the limit sell). The fill day doesn't
  count, because a daily bar can't show whether its low or its high came first.
- **Below low**: the first day whose low went under the potential low.
- **Status**, first match wins: target hit, below the low, expired (over 183 days), open (filled, waiting for the
  target), waiting for entry.
- It summarises fill rates, target hits, how many were higher after 6 months next to the model's average predicted
  probability, and returns, overall and by verdict and score band (<50, 50-65, 65-80, 80+).

Running `report` and `track` every couple of days matches the original author's habit of reviewing open orders.

## Limitations

- **The prompts haven't been tested against a live model.** The tests use scripted models. Before relying on it, try
  `dip-scanner analyze` on a few tickers you know and read the reasoning.
- **RSS is slow and shallow.** Headlines reach public feeds minutes to hours after professional terminals, and many
  feeds carry a title and a line of summary only. By the time a dip is flagged, fast money has usually acted.
- **Prices are daily bars from Yahoo Finance's unofficial chart API**, delayed and occasionally wrong or missing. It
  can stop working without notice.
- **Fundamentals are US-only** (SEC XBRL). IFRS filers usually have annual figures only; other listings get none.
- **Triage makes mistakes**: wrong tickers, missed indirect effects, stories about a company's stock price mistaken
  for news about the company. A reused ticker can point at a different company. Check the ticker before acting.
- **Some feeds are noisy** (Google News queries, general business news): triage filters them out, at some token
  cost. Google News links are redirects.
- The same headline from two sources within 72 hours is kept once; an 8-K filing is always new (it has its own link).

## Risks

**This is not investment advice, and the tool never trades.** Please read this before using its output with real
money:

- **The original result can't be verified.** It was described in a Reddit post, came in a very volatile year, and
  a few huge AMD spikes reportedly did much of the work. One lucky, concentrated period says little about any method.
- **Survivorship bias**: you hear about the €57,500, not about everyone who tried something similar and lost.
- **The model's probabilities are not calibrated.** "68% chance of being higher in 6 months" is the model's guess
  until your own track record says otherwise. Watch the "higher after 6 months vs predicted" line in `dip-scanner
  track` over many months before giving the numbers weight.
- **"Buy the dip" loses when the dip is right.** Some drops are the start of a long decline (a guidance cut, fraud,
  a lost customer), and a limit buy fills exactly when the price keeps falling. The potential low is an estimate, not a
  floor.
- **News can be wrong, stale or planted**, and a language model can misread it with confidence.
- Taxes, fees, currency moves and position sizing are yours to manage. Never put in money you can't afford to lose.

## Troubleshooting

| Problem | What to check |
|---|---|
| `Configuration problem: Set OPENAI_API_KEY ...` | `.env` is in the folder you run from (or pass `--env-file`), and the key for your `LLM_PROVIDER` is set. |
| `sec-8k-filings` fails or is skipped | Set `SEC_USER_AGENT` to your name and email. |
| A feed fails in `feeds --check` | Some sites block cloud IP addresses; feeds.toml notes the ones known to. Switch it off or use an alternate. |
| `No prices (unknown symbol ...)` in the notes | The triage gave a symbol Yahoo doesn't know; it is rechecked after 7 days. |
| `Analysis of X failed ...` | The model refused, was filtered, or its reply was unusable even after a corrective retry. That ticker waits 30 minutes, then 1 h, 2 h... up to a day before it is tried again. |
| Throttling (429) | Raise `interval_minutes`, lower `triage_batch_size` or `max_candidates_per_cycle`, or raise your quota. |
| SSL certificate errors on a corporate network | The tool trusts the operating system's certificates. If your proxy's root certificate isn't installed there, point `SSL_CERT_FILE` and `REQUESTS_CA_BUNDLE` at a bundle that includes it. |

Add `-v` to any command for debug logging.

## Development

```bash
pip install -e ".[dev,anthropic,azure]"
pytest
ruff check . && ruff format --check .
```

The tests use fake feeds, prices, SEC data, models and notifiers, so they run offline, without credentials and
without sleeping.

| Module | Role |
|---|---|
| `cli.py` | Commands, options, exit codes |
| `pipeline.py` | One cycle, the watch loop, manual analysis |
| `feeds.py` | Fetching and parsing RSS/Atom, link and headline normalisation, per-ticker news |
| `store.py` | SQLite: feed state, articles, impacts, ticker validity, opportunities |
| `triage.py` / `prompts.py` | News to affected companies (batched), and all prompt text |
| `prices.py` | Yahoo Finance chart API and the price statistics |
| `fundamentals.py` | SEC XBRL company facts (US filers) |
| `detect.py` | Dip rules, severity and candidate selection |
| `analyze.py` | The fear-vs-fundamentals analysis, number checks and the score |
| `report.py` / `notify.py` | Markdown/HTML/JSON reports, the news digest, and alerts |
| `track.py` | The track record |
| `llm.py` | OpenAI, Azure AI Foundry and Anthropic chat models, JSON replies |
| `config.py` / `models.py` | Settings and config files; the shared data types |
