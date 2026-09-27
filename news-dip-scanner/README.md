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
 poll ──► new articles (deduplicated by link and by headline; translations and non-news pages dropped) ──► SQLite
        │  (data/scanner.sqlite3); only articles from the last 24 h go to the model
        ▼
 triage (small model, 20 articles per request)
        │  article ─► [{ticker, relation direct|indirect, direction, magnitude 1-5, event type, rationale}]
        ▼
 candidates: tickers with negative/mixed news in the last 48 h
        │  filters: [universe], [dip] news rules, 24 h cooldown, Yahoo Finance prices
        │  a symbol without prices is looked up by company name (renamed: OPAP.AT -> ALWN.AT)
        │  dip = down ≥3% on the day, or ≥6% over 5 days, or ≥10% below the 20-day high
        ▼
 analysis (stronger model, one request per candidate, at most 8 per cycle and 40 a day)
        │  input: price statistics + SEC quarterly figures (US) + flagged news + per-ticker headlines
        │         (Yahoo, Google News in English and, for Athens and 5 EU exchanges, the local language)
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
dip-scanner feeds --check      # fetches every feed once (without SEC_USER_AGENT the SEC feed shows as skipped)
dip-scanner prices AMD         # price statistics from Yahoo Finance
```

Then run one real cycle. This one uses the model: it triages the last 24 hours of news (about 280 articles, 14
requests, on the Sunday this was tested; more on weekdays) and analyses up to 8 candidates with the stronger model.
`--no-notify` keeps it off your alert channels (its results aren't sent later either):

```bash
dip-scanner run --no-notify    # one full cycle; prints a summary and the report's path
```

Older backlog is stored but never sent to the model. Later cycles only see what's new since the previous one.

## Configuration

| File | What's in it |
|---|---|
| `.env` | Secrets and service settings (see `.env.example`). Real environment variables win over it. |
| [`feeds.toml`](feeds.toml) | The news sources: 20 enabled, plus 14 switched off (six Greek sources and eight checked alternates). Each entry notes what it covers and when it was last verified; `exclude_titles` drops headlines that aren't news (see the file's header). |
| [`scanner.toml`](scanner.toml) | Thresholds, watchlist and alert rules. Every key is optional and the file shows the defaults; a misspelled key is an error, never silently ignored, and so is a `--config` or `SCANNER_CONFIG` file that doesn't exist. |

The settings you are most likely to change in `scanner.toml`:

| Setting | Default | Meaning |
|---|---|---|
| `[scan] interval_minutes` | 5 | Minutes between cycles in `watch`. |
| `[scan] max_candidates_per_cycle` | 8 | Analyses per cycle; the rest wait for the next cycle (bounds the LLM bill). |
| `[scan] cooldown_hours` | 24 | A ticker isn't analysed again within this time unless news arrives that the last analysis didn't see, or that analysis came before any trading on its news (weekend news) and the next session moved. |
| `[scan] reanalyse_same_session_hours` | 12 | Until a new session has traded since a ticker's last analysis (news in the evening, at the weekend or later the same day), new news analyses it again at most this often; the rest waits for the next session, and the same news on the same prices is never analysed twice. A further fall of `min_drop_1d_pct` lifts the wait. 0 turns it off. |
| `[scan] max_analyses_per_day` | 40 | Analyses in any 24 hours, all tickers together; candidates over it are named in the notes and wait for room (0 = no limit). |
| `[dip] min_drop_1d_pct` / `min_drop_5d_pct` / `min_drawdown_20d_pct` | 3 / 6 / 10 | What counts as a dip (any one is enough). |
| `[dip] min_magnitude`, `directions`, `include_indirect` | 2, negative + mixed, true | Which news can make a company a candidate. |
| `[universe] watchlist` | none | Tickers that skip the magnitude and relation filters (any negative or mixed news of magnitude 1 or more, direct or indirect). They still need a dip. |
| `[universe] allowed_suffixes` | all | Exchanges by Yahoo suffix: `""` US, `.DE` Xetra, `.PA` Paris, `.AT` Athens... |
| `[universe] preferred_listings` | none | The listing you buy for a company listed in several places, e.g. `{ "ASML" = "ASML.AS" }`: its news goes there (see [Investing from a euro account](#investing-from-a-euro-account)). |
| `[alerts] min_score`, `min_probability`, `verdicts` | 65, 60, temporary fear + mixed | What gets sent as an alert. Everything is in the reports. In practice the score is what binds, and 65 is rarely reached by a "mixed" verdict (see [Scoring](#scoring)). |
| `[alerts] repeat_hours`, `min_score_change` | 24, 10 | A ticker alerted within `repeat_hours` isn't alerted again unless the score rose by `min_score_change`, the verdict changed or the price fell by another `min_drop_1d_pct`. |
| `[alerts] system_notices` | true | Tell you through the same channels when the scanner stopped or can't work (see [Running it every 5 minutes](#running-it-every-5-minutes)). |
| `[alerts] notice_after_cycles` | 6 | Cycles in a row with the model unavailable, or every feed failing, before such a notice. |
| `[account] currency` | none | Your broker account's currency, e.g. `"EUR"`: reports and alerts show price, entry and target in it too, and `track` shows returns in it. |

Times in reports, alerts, summaries, notes and the log are UTC unless `DISPLAY_TZ` in `.env` names another IANA time
zone (`Europe/Athens`, `Europe/Berlin`, `America/New_York`), shown with its abbreviation: `2026-09-25 18:00 EEST`. A
name the system doesn't know is a configuration error; on Windows the names come from the `tzdata` package, which is
installed with the scanner there. The database, the report file names and the model's daily use (counted from 00:00
UTC) stay in UTC.

Tickers are Yahoo Finance symbols: `AMD`, `BRK-B`, `SAP.DE`, `ASML.AS`, `ALWN.AT`, `7203.T`, `0700.HK` (in the
watchlist and exclude lists `BRK.B` or `NASDAQ:TSLA` work too). Only company shares become candidates: ETFs, funds
and indices are left out, and so is a fund that Yahoo lists as a share but whose name says "Fund" or "ETF".

The triage model knows the symbols of its training data, and some have changed since: OPAP became Allwyn (`OPAP.AT`
is now `ALWN.AT`), Mytilineos became Metlen (`MYTIL.AT` is now `MTLN.AT`). When Yahoo has no prices for a symbol, the
scanner searches Yahoo Finance for the company name the triage gave and takes the same company's shares on the same
exchange (for a symbol without a suffix, a main US exchange, not OTC): a listing with the same name or its initials,
anywhere in Yahoo's list ("Allwyn" is Allwyn AG, "PPC" Public Power Corporation), or one with a longer name that
starts with it ("Metlen": Metlen Energy & Metals PLC) only when no other company on Yahoo's list starts the same way
and one of the stories names it. Funds, notes, partnerships and warrants never match. Its news moves to that symbol,
the notes say `OPAP.AT -> ALWN.AT (Allwyn AG)`, the answer is kept for 7 days, and `analyze ALWN.AT` and `news
--ticker ALWN.AT` include that news too. News about companies that were taken over or delisted (Hess, Credit Suisse)
stays in the "No prices" note: Yahoo's search lists related instruments for them (Hess Midstream LP, a bond fund),
which are not the company. A wrong match is undone by adding the symbol before the arrow to `[universe] exclude`.
With `only_watchlist`, or news that only the watchlist's leniency lets through, the old symbol is looked up first when
the watchlist has a symbol on its exchange, so an Allwyn story filed as `OPAP.AT` still reaches `ALWN.AT`. This needs
the current name: Yahoo no longer finds "OPAP", so a story the model filed as "OPAP" stays unresolved (the prompt asks
for the name the article uses). Greek letters are read as the Latin ones of Athens codes (`ΕΤΕ.ΑΤ` is `ETE.AT`, and a
Greek code without an exchange is an Athens one: `ΜΟΗ` is `MOH.AT`), Cyrillic ones that look like Latin letters as
those.

## Scanning Athens stocks

The enabled feeds are English and mostly about US and large European companies. To cover the Athens Exchange too:

1. **Feeds.** In `feeds.toml`, set `enabled = true` for the Greek sources you want. `mononews` (Athens companies,
   banks, analyst calls) and `ot-gr` (a business daily) carry the most company news; `naftemporiki` is more
   economy and personal finance; `newmoney`, `powergame` and `sofokleousin` add similar stories with more politics,
   world news and sport. All six answered on 2026-09-27 (see the comments there); run `dip-scanner feeds --check`
   from your own connection. Capital.gr refuses feed requests from cloud servers and was left out. Each enabled
   feed adds triage requests for its non-company stories (see [Costs](#costs)).
2. **Exchanges.** Every exchange is allowed by default. To look at US and Athens listings only, set
   `[universe] allowed_suffixes = ["", ".AT"]` in `scanner.toml` (with `preferred_listings`, add their exchanges
   too, e.g. `".AS"` for `ASML.AS`: a preferred listing the setting leaves out is a configuration error).
3. **Watchlist.** Any negative or mixed news about a watchlist ticker counts, however small or indirect. Current
   Yahoo codes, each checked with `dip-scanner prices` on 2026-09-27:

   ```toml
   [universe]
   watchlist = [
       "ETE.AT",       # National Bank of Greece
       "EUROB.AT",     # Eurobank
       "TPEIR.AT",     # Piraeus Bank
       "ALPHA.AT",     # Alpha Bank
       "HTO.AT",       # OTE (Hellenic Telecommunications Organization)
       "PPC.AT",       # PPC (Public Power Corporation)
       "ALWN.AT",      # Allwyn, formerly OPAP (OPAP.AT has no prices any more)
       "MTLN.AT",      # Metlen Energy & Metals, formerly Mytilineos (MYTIL.AT has no prices any more)
       "BELA.AT",      # Jumbo
       "MOH.AT",       # Motor Oil
       "ELPE.AT",      # HELLENiQ ENERGY
       "GEKTERNA.AT",  # GEK TERNA
       "AKTR.AT",      # Aktor
       "AEGN.AT",      # Aegean Airlines
       "TITC.AT",      # Titan
   ]
   ```

Known limits:

- **No fundamentals.** They come from SEC filings, which Athens companies don't make. The analysis rests on the news
  and the price data; the model is told that this is expected and not to lower its confidence for it, but read the
  company's latest results before acting on a verdict.
- **Thinner news.** Few English sources follow Athens companies, and the Greek feeds mix them with much else. The
  per-ticker context headlines of an analysis come from Google News in Greek as well as English ("Jumbo μετοχή" for
  BELA.AT; the same for Xetra, Paris, Milan, Madrid and Amsterdam listings in their languages), but only headlines
  that name the company in Latin letters or its symbol are kept. A one-word name counts only as a name (capitalised,
  and not in the publisher's name at the end of a Google headline), and the English search looks for Yahoo's full
  name or the symbol (`"Titan S.A." OR TITC stock`): "Titan stock" finds Titan Company in India, Titan Mining and
  "tech titan". Companies the Greek press calls by a Greek name (ΔΕΗ for PPC, ΕΤΕ or Εθνική for National Bank of
  Greece) get fewer: on 2026-09-27 Jumbo and Allwyn got 15 context headlines each, Titan 4 (3 about the Greek company;
  12 of 15 had been about other Titans before these rules), National Bank of Greece 8, PPC and OTE 1.
- **Greek text.** The triage prompt says that articles can be in any language and asks for English answers,
  symbols in Latin letters and the company's current name. The prompts haven't been tested against a live model
  (see [Limitations](#limitations)); run `dip-scanner news` after a few cycles to see what it made of the Greek
  stories.
- **Alerts.** With the default `[alerts]` only confident "temporary fear" calls alert, on any exchange (see
  [Scoring](#scoring)).

## Investing from a euro account

The prices, entries and targets are in the currency a stock trades in, and so are the returns in `track`. From a
euro account, a US stock is also a bet on the dollar:

- **Currency risk is a big share of the target.** EUR/USD moved 7.3% a year (annualised daily volatility, 2016-2026,
  Yahoo's `EURUSD=X`), about 5% over six months; since 2016 the median six-month move was 2.9% and one in four was over
  6%. Next to a typical 10-20% target from the analysis, that decides many outcomes: +15% in dollars while the dollar
  loses 6% against the euro is about +8% in euros.
- **Conversion fees.** Most brokers convert at a spread or a fee of a few tenths of a percent, on the buy and again on
  the sale; check your broker's fee schedule.
- **Minimum commissions.** On a small account these weigh more than the fee in percent: with a €7 minimum, a €500
  order pays 1.4% to buy and 1.4% to sell, before any currency cost. Fewer, larger orders cost less.

`[account] currency = "EUR"` in `scanner.toml` makes this visible. Reports and alerts then show price, entry and
target in euros too (`$132.00 ≈ €115.93`), at Yahoo Finance's exchange rate when the analysis was made (the report
names the rate; your broker's rate and fee differ), and `track` adds the return in euros, exchange-rate moves
included, next to the return in the trading currency (see [Track record](#track-record)). The rate is fetched from
Yahoo's chart API like the prices (`EURUSD=X`, `EURGBP=X`...; pence, cents and agorot are converted through pounds,
rand and shekels). Without a rate the amounts stay in the trading currency and the notes say so. Opportunities
analysed before the setting keep showing their trading currency only.

Many large European companies trade in New York too (ASML, SAP, TotalEnergies, Stellantis), and the triage names the
listing the article is about, often the US one. `[universe] preferred_listings` moves a company's news to the listing
you would buy, so it is analysed there, with prices, entry and target in euros and no conversion:

```toml
[universe]
preferred_listings = { "ASML" = "ASML.AS", "SAP" = "SAP.DE", "TTE" = "TTE.PA", "STLA" = "STLAM.MI" }
```

All eight symbols answered on 2026-09-27. The map applies to every new triage answer and to news stored before it was
set (in scans, `analyze` and `news`), watchlist and exclude entries are read through it (`"ASML"` on the watchlist
means `ASML.AS`), and news filed under both symbols counts once, for the preferred one. The analysis still gets the
company's SEC figures, looked up under the US symbol (ASML, SAP, TotalEnergies and Stellantis all file with the SEC).
Pick the company's home exchange (Amsterdam for ASML, Xetra for SAP): secondary listings trade less, at wider
spreads. A preferred listing on an exchange that `[universe] allowed_suffixes` leaves out is a configuration error:
add its suffix there.

## Commands

After `pip install -e .`, `dip-scanner` works as a shorthand for `python -m dip_scanner`.

| Command | What it does | Needs a model |
|---|---|---|
| `dip-scanner run [--no-notify]` | One cycle; prints a summary, the day's model use, notes on what was skipped and why, and the report's path. | yes |
| `dip-scanner watch [--interval MIN] [--no-notify]` | Cycles on the interval until Ctrl+C; each logs a summary ending with the day's model use. | yes |
| `dip-scanner feeds [--check]` | Lists the feeds and how their last fetch went; `--check` fetches each one now. | no |
| `dip-scanner news [--hours 24] [--ticker T]` | The news digest ("newsletter"): companies with negative news first, then the other headlines. Stories are listed under the symbol a scan reads them as (the preferred listing, or the symbol found for an old one). | no |
| `dip-scanner analyze TICKER [--no-save]` | Analyses one ticker now, whatever its price did and ignoring the cooldown, with the news filed under its old symbol or its preferred listing's other symbol. | yes |
| `dip-scanner report [--days 7] [--min-score N] [--html PATH]` | The stored opportunities of the last days. | no |
| `dip-scanner track [--days 365]` | How past opportunities played out, next to their exchange's index (see [Track record](#track-record)). | no |
| `dip-scanner prices TICKER` | Price statistics and whether they count as a dip. | no |

Options for every command: `-v` (debug logging), `--env-file PATH`, `--config PATH`, `--feeds PATH` and
`--data-dir DIR`. Exit codes: 0 ok, 1 runtime error, 2 configuration error.

### Output

Everything goes to `data/` (or `DATA_DIR`):

- `scanner.sqlite3`: articles (kept 30 days), company impacts, feed state, every model call with its tokens (kept
  30 days) and every opportunity (kept for good).
- `reports/YYYY-MM-DD/HHMMSS-opportunities.md`, `.html` and `.json`, written by each cycle that found something,
  plus `reports/latest.md` and `latest.html`. The HTML is a single email-safe file. Its score badges use the same
  bands as the track record: 80+ strong, 65-80 good, 50-65 fair, under 50 weak.
- `cache/sec/`: the SEC ticker list (a day) and each company's figures (12 hours).

Each opportunity in the report shows the price and recent moves, the chance of being higher in 6 months, the
potential low and the statistical low, the entry (limit buy) and target (limit sell idea), the upside to the target
and downside to the low twice (from the price at the analysis, and from the entry, which is what the two limit
orders would make or lose), the verdict and confidence, what the market fears, the fundamental impact, the thesis,
risks, catalysts, what to check before buying, and the headlines that flagged it. With `[account] currency` set,
price, entry and target also show their value in your currency, with the exchange rate used.

Alerts go to every configured channel. Email and generic webhooks get the full report; Slack, Discord and Telegram
get one line per opportunity. The rules:

- **Retries**: an alert that couldn't be sent is retried for 24 hours, labelled "not sent earlier" with its age. It
  counts as delivered once any one channel took it, so a channel that was down at the time doesn't get it later.
- **One analysis per ticker**: when a ticker has been analysed again since, only the newest analysis counts; an
  older unsent alert is dropped, also when the newer analysis is no longer an alert.
- **No repeats**: a busy story means a new analysis for every new article, but a ticker alerted within
  `[alerts] repeat_hours` is only alerted again when something material changed (see the table above).
- **Thesis changes**: when a ticker alerted in the last 6 months is analysed again and no longer passes `[alerts]`,
  or its chance of being higher fell by 20 points or more, you get a "Thesis change: ... review open orders" notice
  saying what changed. In `report` and `track`, an older analysis with a newer one is marked superseded.
- **Nothing is queued silently**: results of `run --no-notify` or `watch --no-notify`, of cycles run before any
  channel was set up, and of `dip-scanner analyze` (you've just read it) are never sent later.

A channel that is only half set up is skipped with a warning naming the missing setting, and Discord messages can't
ping anyone (mentions are turned off). Chat messages don't include the report's local path.

## Scoring

**Severity** only orders the candidates within a cycle (bigger, better-explained drops are analysed first):

```
severity = drop + news + corroboration + volume
  drop          = max(0, -change_1d, -change_5d / 1.5, -drawdown_from_20d_high / 2)      (in %)
  news          = 1.5 × strongest impact's magnitude × relation (direct 1, indirect 0.5)
                        × direction (negative 1, mixed 0.7, other 0.3)
  corroboration = 0.5 per further story about the company (copies of one headline count once), at most 2
  volume        = min(3, volume_ratio - 1) when the day traded above its 20-day average volume
```

During the session `volume_ratio` compares the volume so far with the same share of a normal day (the session
counts as at least 10% done), so heavy early selling shows as a high pace, not as "below average".

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
score 58.5, which the default `[alerts] min_score` of 65 doesn't alert on.

What the default alert rules mean in practice: the potential low is anchored near the statistical 6-month low and
the target near the pre-drop high, so `reward_risk` is usually 0.2-0.4. A score of 65 then needs a chance up of
about 76-85% for a "temporary fear" verdict at high confidence, and 93% or more for "mixed" at high confidence,
above the 90% the model is told not to exceed. So with the defaults only confident temporary-fear calls alert; set
`min_score = 55` if you want mixed verdicts too (mixed, high confidence, 80%, reward_risk 0.3 scores 55.2).
`min_probability` only matters when the upside is over about 3.8 times the downside.

Listings outside the US (Athens, Xetra, Paris...) get no fundamentals: they come from SEC filings only. The model is
told that this is expected for such a listing and to judge its confidence on the news and the price data, not to
lower it because figures are missing. Otherwise they could hardly ever alert: "low" confidence multiplies the score by
0.85, and then even a 90% chance with a reward/risk of 0.43 scores 64.5. Their verdicts still rest on less
information, so read the company's latest report before acting on one.

Before scoring, the model's numbers are checked and fixed where they contradict each other, with a warning in the
report: the probability is clamped to 0-100; a potential low at or above the price becomes the lower of the
statistical 6-month low and 97% of the price, and one below 30% of the price is raised to that; the entry is clamped
between the low and the price; a target at or below the entry becomes entry × (1 + max(5%, half the annual
volatility)); a target above both the 52-week high and `price × exp(2 × volatility × √0.5)` (2 standard deviations of
6-month volatility) is lowered to that ceiling, so a slipped decimal can't inflate the score.

The "statistical 6-month low" in the price block is the 5th percentile of the price at the 6-month mark in a
zero-drift lognormal model: `price × exp(-1.645 × volatility × √0.5)`. The lowest price along the way falls below it
about twice as often (roughly 1 in 10), and the price block says so. The model is told to use it, and the worst
6-month drawdown in the price history, as anchors for its potential low.

## Costs

The request counts and prompt sizes below come from a live run of the feeds (2026-09-27); the output token counts are
assumptions, since no live model was run. All of it is an estimate, not a quote. Check your provider's current prices.

- **Triage**: one request per cycle that has new articles, with up to 20 articles each. Every request carries a
  fixed prompt of about 5,200 characters (about 1,300 tokens) plus about 300 characters per article, and gets a
  short JSON reply. At the 5-minute interval most requests carry only 1-3 articles (a full batch of 20 only happens
  after a backlog, about 2,500-3,000 tokens), so expect roughly 150-290 triage requests a day: the live Sunday run
  measured 162 requests for about 440 fresh articles, about 1,400 tokens each on average (with the prompt at 4,700
  characters then; about 100 tokens more per request now). That is about 0.2-0.5
  million input tokens a day for triage, most of it the fixed prompt repeated every cycle; a longer
  `interval_minutes` cuts it about proportionally (15 minutes: about 96 requests a day).
- **Analysis**: one request per candidate, about 3,500 input tokens (price block, fundamentals, up to 12,000
  characters of news) and a reply of 500-1,000 tokens. A new qualifying article about a ticker analyses it again
  (its alert is only repeated when something material changed), up to `max_candidates_per_cycle` per cycle. Until
  a new session has traded (evenings, weekends, later the same day) that happens at most every
  `reanalyse_same_session_hours` (12) unless the price fell further, and `max_analyses_per_day` (40) caps the day,
  so a busy news day can't run up the bill. Expect a handful to a few dozen a day.
- **Total**: roughly 0.25-0.6 million input tokens a day. Reasoning models also bill their thinking as output
  tokens, which is where most of the money goes.

**What that costs a month.** With OpenAI's list prices for the default models, checked on 2026-09-27 (gpt-5-mini
$0.25 per million input tokens and $2 per million output tokens, gpt-5 $1.25 and $10; prices change, check the
provider's page), and the request counts above, 30 days of `watch` at the 5-minute interval:

| Day | Triage (gpt-5-mini) | Analysis (gpt-5) | A day | A month | In euros |
|---|---|---|---:|---:|---:|
| Quiet | 150 requests, 300 output tokens each | 5 analyses, 2,000 output tokens each | $0.27 | about $8 | €7 |
| Typical | 220 requests, 500 output tokens each | 15 analyses, 3,500 output tokens each | $0.89 | about $27 | €24 |
| Busy every day | 290 requests, 1,000 output tokens each | 40 analyses (the daily cap), 5,000 each | $2.86 | about $86 | €75 |

Input is 1,500 tokens per triage request and 3,500 per analysis; the output counts include reasoning at
`LLM_TRIAGE_REASONING_EFFORT=low` and the analysis model's default effort, and are assumptions: no live model was run
for this README. The euro figures use 1 EUR = 1.1386 USD. Anthropic's defaults (claude-haiku-4-5 at $1 and $5,
claude-sonnet-5 at $2 and $10) come to about $18-95 a month for the same days. Haiku gets no reasoning (it doesn't
take the effort setting), so that assumes a triage reply of about 300 output tokens every day; the difference is
mostly Haiku's input, which costs four times gpt-5-mini's.

The six Greek feeds (see [Scanning Athens stocks](#scanning-athens-stocks)) add about 60% more articles to triage
(148 of 396 on the Sunday measured, with slightly longer items, and Greek takes more tokens per character than
English). Most of a triage request is the fixed prompt, so the cost is mostly more cycles with something new: at most
one request per cycle, 288 a day at the 5-minute interval instead of about 220, which is roughly $3 a month more on
typical days with the default OpenAI models.

Next to the Reddit author's starting capital of €2,500, a year of typical days costs about €280, 11% of the account,
and a year of busy ones about €900, 36%, before a single trade and before broker fees (see
[Investing from a euro account](#investing-from-a-euro-account)). The scanner has to find a lot of good trades to pay
for itself on an account that size. To keep the bill down:

- Set `LLM_TRIAGE_REASONING_EFFORT=low`: triage only maps headlines to companies, all day long. If a triage reply
  takes about 1,800 output tokens at the model's default effort instead of 500, the typical month costs about $17
  more. `LLM_ANALYSIS_REASONING_EFFORT=low` makes the analysis cheaper too, at some cost in quality (`medium` only
  helps with Claude, whose default is high; gpt-5's default is already medium).
- Use a longer `[scan] interval_minutes` (15 minutes: about 96 triage requests a day) and a lower
  `[scan] max_analyses_per_day`.
- Set a monthly spend limit or budget in the provider's console, and keep automatic recharge of prepaid credit off or
  low. When the money runs out, the scanner stops and sends a "dip-scanner stopped" notice (see
  [Running it every 5 minutes](#running-it-every-5-minutes)), instead of running up a bill.
- A ChatGPT Plus (or Pro) subscription doesn't include API use: the API is billed separately, per token, from
  prepaid credit on platform.openai.com.

Every summary line of `run` and `watch` ends with the day's use so far (calls and tokens in and out, since 00:00
UTC), and `run` also prints it per step and model: multiply by your provider's prices to see what the day cost. The
token counts are what the service reported, reasoning included; a gateway that reports none shows "without token
counts".

Feeds, Yahoo Finance and SEC data cost nothing, but be polite: the defaults poll each feed once per cycle with
conditional requests, and SEC requests are spaced out to at most 5 a second.

## Running it every 5 minutes

**Foreground**, e.g. in `tmux` or `screen`:

```bash
dip-scanner watch
```

Cycles start on the interval's boundaries (:00, :05, :10...), each logs one summary line, a failed cycle is logged
and the next one runs as usual, and Ctrl+C stops it cleanly. Bad credentials or configuration stop it with exit code
2 (and a notice, see below).

**Nobody watching?** An unattended scanner tells you through the alert channels (email, Slack, Discord, Telegram,
webhook) when it can't do its job, so a quiet week means a quiet market and not a dead scanner:

- "dip-scanner stopped: ..." when `run` or `watch` stops on a setup problem: a rejected or revoked key, no credit
  left (`insufficient_quota`, a spend limit), an unknown model, a broken setting;
- "the language model has been unavailable for 6 cycles" when it couldn't be reached, kept throttling or refused
  every request for `[alerts] notice_after_cycles` cycles in a row;
- "every feed has failed for 6 cycles" when no news came in at all (usually the network).

Each kind is sent at most once every 12 hours; the times are kept in the database, so a cron job running every 5
minutes doesn't repeat it either. Notices never contain keys, passwords, tokens or webhook URLs. They need a
configured channel and are off with `--no-notify` or `[alerts] system_notices = false`.

**cron** (Linux), one cycle per run, with `flock` so a slow cycle doesn't overlap the next:

```cron
*/5 * * * * cd /path/to/news-dip-scanner && mkdir -p data && flock -n /tmp/dip-scanner.lock .venv/bin/dip-scanner run >> data/cron.log 2>&1
```

The `mkdir -p data` matters: the shell opens the log before the scanner starts, so without the folder nothing runs
and nothing is logged. If you set `DATA_DIR`, point the log there as well (`mkdir -p "$DATA_DIR" && ... >>
"$DATA_DIR/cron.log"`). macOS has no `flock`: use `lockf -t 0 /tmp/dip-scanner.lock .venv/bin/dip-scanner run`
(it ships with macOS) or `brew install flock`, or run `dip-scanner watch` instead.

**systemd** (a user service that restarts after crashes or reboots, but not after a setup problem):

```ini
# ~/.config/systemd/user/dip-scanner.service
[Unit]
Description=News dip scanner

[Service]
WorkingDirectory=/path/to/news-dip-scanner
ExecStart=/path/to/news-dip-scanner/.venv/bin/dip-scanner watch
Restart=on-failure
RestartSec=60
RestartPreventExitStatus=2

[Install]
WantedBy=default.target
```

Exit code 2 means a setup problem (a rejected key, no credit, a broken setting). Restarting can't fix it, so systemd
leaves the service stopped instead of polling every feed and calling the model once a minute: fix what the
"dip-scanner stopped" notice or `journalctl --user -u dip-scanner` names, check with `dip-scanner run --no-notify`,
then `systemctl --user restart dip-scanner`.

Then `systemctl --user enable --now dip-scanner` and `journalctl --user -u dip-scanner -f` for the log. A user
service only runs while you are logged in, unless you run `loginctl enable-linger "$USER"` once: do that on a server
you log out of, or it stops when you disconnect and doesn't start after a reboot.

**Windows Task Scheduler**, one cycle every 5 minutes with its output in `data\scanner.log`. Create the folder first
(`mkdir data` in the project folder): the redirect needs it, and a run that stops on a setup problem doesn't create it,
so nothing would run or be logged. In a Command Prompt:

```bat
schtasks /Create /TN dip-scanner /SC MINUTE /MO 5 /TR "cmd /c cd /d C:\path\to\news-dip-scanner && set PYTHONUTF8=1&& .venv\Scripts\dip-scanner.exe run >> data\scanner.log 2>&1"
```

- `cd /d` makes `.env`, `scanner.toml` and `data\` findable. `PYTHONUTF8=1` writes the log in UTF-8 (Greek
  headlines, "€"); the scanner does that for redirected output anyway, and on a console it shows "?" for what the
  code page lacks instead of failing. There is no space before the second `&&` on purpose: cmd would keep it in the
  value, and Python refuses to start with `PYTHONUTF8` set to `"1 "`.
- As created, the task only runs while you are logged on, and a console window flashes every 5 minutes. Open it in
  Task Scheduler (`taskschd.msc`), Properties: on General choose "Run whether user is logged on or not" (it asks for
  your password), which also runs it without a window. On Conditions tick "Wake the computer to run this task", and
  on a laptop untick "Start the task only if the computer is on AC power": a PC that sleeps runs nothing, and Windows
  only wakes it when the power plan allows wake timers (Power Options, advanced settings, Sleep, "Allow wake
  timers"). On Settings keep "If the task is already running: Do not start a new instance", so a slow cycle doesn't
  overlap the next.
- The `/TR` text is limited to about 260 characters. With a long path, put the part in quotes after `cmd /c` into a
  `run-scanner.cmd` file in the project folder and use `/TR "C:\path\to\news-dip-scanner\run-scanner.cmd"`.

Or run `dip-scanner watch` in a terminal that stays open. With cron and Task Scheduler the output goes where nobody
looks, so set up at least one notification channel: the system notices above are how you learn that the runs stopped
working.

## Track record

```bash
dip-scanner track
```

replays every stored opportunity against the daily prices that followed it, so you can see whether the scores,
verdicts and probabilities mean anything before trusting them:

- **Signal day**: the date of the report on the exchange's calendar (the UTC date for records made before the
  time zone was stored). The window is 183 days (about 6 months) from it.
- **Splits**: Yahoo adjusts its whole price history for every split, while the report keeps the prices it quoted.
  The report's price, entry, target and low are divided by the ratios of the splits after its quote day before
  they are compared, and the row says "after a 10:1 split". A report whose price doesn't match Yahoo's history (a
  split Yahoo doesn't report) is marked "price mismatch" and left out of the figures.
- **Entry filled**: the first day whose low reached the entry price (the limit buy). When the report's price is from
  the signal day's session, only the range between that price and the day's close counts for that day.
- **Target hit**: the first day after the fill whose high reached the target (the limit sell). The fill day doesn't
  count, because a daily bar can't show whether its low or its high came first.
- **Below low**: the first day whose low went under the potential low.
- **Status**, first match wins: target hit, below the low, expired (over 183 days), open (filled, waiting for the
  target), waiting for entry.
- **No trading yet**: until a session has traded after the report (a report written at the weekend or after the
  close), its returns show "–" and it counts in no rate or average, so a fresh report doesn't read as "0 of 8 higher,
  +0.0%". The header says how many are waiting.
- **Benchmark**: every opportunity is compared with its exchange's index over the same days, from the index's level
  at the report (stored with it, from the same session as the report's price, also while the market is open) to its
  close on the day of the last price; for reports from before the level was stored, from its close on the day of the
  report's price, and a report made during the session then compares the stock from that close too. The indices:
  `^GSPC` (S&P 500) for US listings, `^GDAXI` for Xetra, `^FCHI` Paris, `^AEX` Amsterdam, `FTSEMIB.MI` Milan, `^IBEX`
  Madrid, `GD.AT` Athens, `^FTSE` London, and the local index for Brussels, Lisbon, Stockholm, Copenhagen, Helsinki,
  Oslo, Vienna, Dublin, Zurich, Tokyo, Hong Kong and a few more (all checked on 2026-09-27); other European exchanges
  get the Euro Stoxx 50 (`^STOXX50E`), anything else the S&P 500. "vs index" is the return minus the index's. In a
  rising market almost every dip bounces, and "higher after 6 months" looks good; the average excess return, overall
  and per verdict and score band, is what says whether the picks beat simply holding the market. An index without
  prices shows "–" and a note.
- **Account currency**: with `[account] currency` set, a column shows each return in that currency, exchange-rate
  moves included: the rate stored with the report (else that day's close) against the close on the day of the last
  price. Broker fees are not in it.
- It summarises fill rates, target hits, how many were higher after 6 months next to the model's average predicted
  probability, and returns (next to the index's, and in the account currency), overall and by verdict and score band
  (<50, 50-65, 65-80, 80+).
- Tickers Yahoo no longer has prices for (delisted, renamed, taken over) are named in a "Left out" line: failed
  companies are often among them, so the figures may look better than what happened.
- An opportunity with a later analysis of the same stock is marked "superseded", with the later verdict.

The original author reviewed open orders every couple of days. The scanner helps with that: a later analysis that
undercuts an alert is sent as a "thesis change" notice, and `report` and `track` mark superseded ideas. Checking
your open orders against them is still yours to do.

## Limitations

- **The prompts haven't been tested against a live model.** The tests use scripted models. Before relying on it, try
  `dip-scanner analyze` on a few tickers you know and read the reasoning.
- **RSS is slow and shallow.** Headlines reach public feeds minutes to hours after professional terminals, and many
  feeds carry a title and a line of summary only. By the time a dip is flagged, fast money has usually acted.
- **Prices are daily bars from Yahoo Finance's unofficial chart API**, delayed and occasionally wrong or missing. It
  can stop working without notice.
- **Fundamentals are US-only** (SEC XBRL). IFRS filers usually have annual figures only; other listings get none.
  Of a company's us-gaap and ifrs-full facts the fresher set is used (companies that moved to IFRS keep their old
  US GAAP facts), proxy statements are ignored (their pay-versus-performance tables restate net income, rounded),
  and 12-16-week quarters of retail calendars count. Figures whose newest period is over 18 months old carry a note,
  and so does a newest quarter that ended over 200 days ago ("a later one may be missing").
- **Triage makes mistakes**: wrong tickers, missed indirect effects, stories about a company's stock price mistaken
  for news about the company. A reused ticker can point at a different company, and a symbol without prices is only
  replaced when Yahoo's search finds a listing with the company's name on the same exchange (see the tickers note
  under [Configuration](#configuration)). Check the ticker before acting.
- **Some feeds are noisy** (Google News queries, general business news): triage filters them out, at some token
  cost. A feed's `exclude_titles` drops known non-news pages before triage (the Reuters feed lists company, fund and
  quote pages among the stories: 42 of 100 items on 2026-09-27). Google News links are redirects. The per-ticker
  context headlines for an analysis keep only items that name the company (without "S.A.", "N.V.", "Inc." and the
  like; a one-word name only capitalised, and not as the publisher's name) or its symbol and are at most 30 days old;
  a name other companies share still lets a few strays in (one of Titan S.A.'s four on 2026-09-27 was about India's
  Titan).
- **News after the close.** A dip is often matched with news that came out after the last session (evenings,
  weekends): the drop can't be a reaction to it, unless the article only reports an earlier event or the drop itself.
  Such candidates say "all of this news came out after the last session (Fri 25 Sep)", the model is told to compare
  the dates, and the first session after the news can end the cooldown if it moves.
- The same headline (at least five words) seen again within 72 hours, from any feed, is kept once; short formulaic
  headlines ("Trading update") and SEC 8-K filings are always new. Machine translations of press releases are
  dropped, and a headline's trailing " - Publisher" only goes when it names a publisher.

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
- **Currency moves and fees can eat the target** of a 6-month idea bought in another currency (see
  [Investing from a euro account](#investing-from-a-euro-account)).
- **Costs are real before any trade**: the model bill is a noticeable share of a small account (see [Costs](#costs)).
- Taxes and position sizing are yours to manage. Never put in money you can't afford to lose.

## Troubleshooting

| Problem | What to check |
|---|---|
| `Configuration problem: Set OPENAI_API_KEY ...` | `.env` is in the folder you run from (or pass `--env-file`), and the key for your `LLM_PROVIDER` is set. |
| `Configuration problem: Scanner config not found` | The file named by `--config` or `SCANNER_CONFIG` doesn't exist; fix the path (without either, `./scanner.toml` or the defaults are used). |
| `The language model can't be used: ... no quota left (insufficient_quota)` (OpenAI) or `... billing or usage-limit reasons` (Anthropic) | The provider refused the account (credit, spend limit). Articles stay pending meanwhile; fix it and start again. |
| `sec-8k-filings` fails or is skipped | Set `SEC_USER_AGENT` to your name and email. |
| A feed fails in `feeds --check` | Some sites block cloud IP addresses; feeds.toml notes the ones known to. Switch it off or use an alternate. |
| `No prices (unknown symbol ...)` in the notes | The triage gave a symbol Yahoo doesn't know, and Yahoo's search found no listing of that company on the same exchange (or couldn't be reached: then it is asked again next cycle). Companies that were taken over or delisted end here. The symbol is rechecked after 7 days. If you know the current symbol, add it to the watchlist. |
| `Symbol renamed/resolved via Yahoo search ...: OPAP.AT -> ALWN.AT` | The triage's symbol has no prices and the company was found under another one, which was checked instead. If the match is wrong, add the triage's symbol, the one before the arrow, to `[universe] exclude`: its news is then skipped, and the found company's own news still counts. Excluding the found symbol drops all of that company's news. |
| `Daily limit of 40 analyses reached ...` in the notes | `[scan] max_analyses_per_day` was used up in the last 24 hours; the named candidates are analysed once there is room. Raise it, or set 0 for no limit, if the bill allows. |
| `Already analysed on the latest session's prices, so new news waits ...` | More news (often in the evening or at the weekend) about a ticker analysed on the same session's prices; it is analysed after the next session, 12 hours after the last analysis (`[scan] reanalyse_same_session_hours`) or when the price falls by another `min_drop_1d_pct`. |
| A "dip-scanner stopped" notice | The reason is in it (the same message `run` prints). Fix that setting; `dip-scanner run --no-notify` checks it. |
| `DISPLAY_TZ must be an IANA time zone name` | Use a name like `Europe/Athens` (not `Athens`, `EEST` or `+03:00`). On Windows, `pip install tzdata` if the scanner was installed without it. |
| `No USD/EUR exchange rate for ..., amounts in USD only` | Yahoo Finance couldn't give the rate for `[account] currency` just then; the analysis is kept, without the ≈ amounts. |
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
| `store.py` | SQLite: feed state, articles, impacts, ticker validity, symbol lookups, opportunities, model calls, notice times |
| `triage.py` / `prompts.py` | News to affected companies (batched), and all prompt text |
| `prices.py` | Yahoo Finance chart API and the price statistics |
| `fundamentals.py` | SEC XBRL company facts (US filers) |
| `detect.py` | Dip rules, severity and candidate selection |
| `symbols.py` | The current symbol of a renamed company, from Yahoo's search by name |
| `analyze.py` | The fear-vs-fundamentals analysis, number checks and the score |
| `report.py` / `notify.py` | Markdown/HTML/JSON reports, the news digest, and alerts |
| `notices.py` | System notices ("dip-scanner stopped", model unavailable, feeds failing), rate-limited and scrubbed |
| `track.py` | The track record, its benchmark indices and returns in the account currency |
| `fx.py` | Exchange rates from Yahoo Finance for `[account] currency`, and minor currency units (pence, cents, agorot) |
| `llm.py` | OpenAI, Azure AI Foundry and Anthropic chat models, JSON replies |
| `config.py` / `models.py` | Settings and config files; the shared data types |
