# Azure map-reduce

LangChain's map-reduce, rebuilt from scratch on Azure OpenAI (Azure AI Foundry) for pandas and PySpark
DataFrames:

- **Map**: run a prompt on every record of a column and write the replies to an output column.
- **Reduce**: take the map outputs `reduce_group_size` at a time (say 10), join each group into one text, run the
  reduce prompt on it, and repeat on the replies, level after level, until one text is left.

Every batch of requests goes to the **Azure Batch API** first (half the price, but needs a batch deployment). If
that fails it goes to **async chat completions** (many requests in flight), and if that fails too, to a **plain
loop** of chat completions. tqdm progress bars follow along: one for the map, and for the reduce a bar over the
levels with a bar per level counting its groups.

```text
Map [batch]: 100%|██████████| 2500/2500 [14:02<00:00, 2.97record/s, jobs 3/3 done]
Reduce:  75%|███████▌  | 3/4 [31:18<10:26, 626.00s/level, level 4: 3 texts in 1 group of up to 10]
Level 4/4: 3 → 1 [batch]:   0%|          | 0/1 [02:03<?, ?group/s, jobs 0/1 done · 1 in_progress]
```

The map bar counts records; the outer reduce bar counts levels (2,500 summaries in groups of 10 take four:
2,500 → 250 → 25 → 3 → 1), and the inner bar counts the current level's groups, with the batch jobs behind them.

## Quick start

```bash
cd azure-mapreduce
pip install -e .            # add ".[spark]" for PySpark
```

```python
import pandas as pd
from azure_mapreduce import AzureChatClient, MapReduce

client = AzureChatClient(
    "https://my-resource.openai.azure.com/",  # or just "my-resource"
    deployment="gpt-4.1-mini",  # standard deployment: async and loop calls
    batch_deployment="gpt-4.1-mini-batch",  # Global Batch deployment: Batch API jobs (optional)
    api_key="...",  # leave out to sign in with Entra ID
    completion_options={"temperature": 0, "max_completion_tokens": 800},
)

mr = MapReduce(
    client,
    map_prompt="Summarize this customer review in two sentences:\n\n{text}",
    reduce_prompt="Combine these review summaries into one overview of what customers say:\n\n{text}",
    map_batch_size=1000,  # records per batch (one Batch API job each)
    reduce_group_size=10,  # map outputs joined into each reduce request
)

reviews = pd.DataFrame({"review": ["Great battery life.", "Stopped working after a week.", "Does the job."]})
result = mr.run(reviews, column="review", output_column="summary")

result.frame  # the reviews with a "summary" column
result.output  # the single reduced text
result.levels  # every level: levels[0] holds the map outputs that went into the reduce (empty ones left out),
#                and levels[-1] == [result.output]
result.complete  # True when no record or reduce group failed
```

With the Batch API first in line, even a tiny run waits for a batch job (minutes, sometimes hours). To try things
out, pass `strategies=("async", "sync")`.

The two steps also work on their own:

```python
summaries = mr.map(reviews, "review", "summary", error_column="summary_error")
overview = mr.reduce(summaries, column="summary").output
overview = mr.reduce(["first text", "second text"]).output  # any list of texts
outputs = mr.map_texts(["first text", "second text"])  # the map step on a plain list
```

`AzureChatClient.from_env()` reads `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`,
`AZURE_OPENAI_BATCH_DEPLOYMENT` and `AZURE_OPENAI_API_KEY` instead (keyword arguments override them).

### PySpark

Pass a Spark DataFrame the same way and you get a Spark DataFrame back:

```python
summaries = mr.map(spark_df, "review", "summary")  # collects the rows to the driver
summaries = mr.map(spark_df, "review", "summary", id_column="review_id")  # collects just ids and texts
result = mr.run(spark_df, "review", "summary", id_column="review_id")
```

The model calls are made from the driver, not the executors: a Batch API job needs all its prompts in one file,
and async calls are network-bound, so one process with many requests in flight goes as fast as your deployment's
quota allows.

- **Without `id_column`** the whole DataFrame is collected and rebuilt with the new columns at the end, keeping
  every column's type and the row order.
- **With `id_column`** (unique values, no nulls or NaN, not even inside a struct; numbers, strings, dates or
  structs of them, but not maps, VARIANT or strings with a collation other than the default) only the ids and
  texts are collected, and the replies are joined back on the id. The column order is kept, but, as with any Spark join,
  the row order may not be. This suits wide or large tables, and it's the way to go if rebuilding the DataFrame
  fails (Spark Connect limits how much local data a DataFrame can be built from).

On PySpark 4 the data is collected through Arrow (install `pyarrow`, which the `[spark]` extra does), which keeps
timestamps exact: a plain `collect()` can't tell apart the two occurrences of the hour repeated when daylight
saving time ends. DataFrames holding types Arrow can't carry both ways (user-defined types such as ML vectors,
VARIANT, intervals, structs with two fields of the same name) are collected as Rows instead. Year-month and
calendar intervals can't reach Python at all: pass `id_column`, or cast or drop those columns.

It works with classic Spark and Spark Connect (Databricks serverless and shared clusters; locally that needs
`pyspark[connect]`). Column names with dots are fine, and an output column replaces an existing one whose name
differs only in case unless `spark.sql.caseSensitive` is on.

### What gets sent

Each non-empty cell becomes one request. Missing values (None, NaN, NaT, `pd.NA`), blank strings and empty lists
are skipped and get None. Lists, arrays, dicts, Spark maps, structs and rows are sent as JSON, and other values
(numbers, dates) as text. Spark timestamps are written as wall-clock times in the session time zone, as
`df.show()` prints them.

## How the requests are sent

Each step (the map, and each reduce level) hands its requests to the strategies in `strategies`, in order
(default `("batch", "async", "sync")`):

| Strategy | What it does | Batch size means |
|---|---|---|
| `batch` | Uploads a JSONL file and runs an [Azure Batch API](https://learn.microsoft.com/azure/foundry/openai/how-to/batch) job on `batch_deployment`, checking on it every `batch_poll_interval` seconds. Up to `max_concurrent_batch_jobs` jobs run at once. | Requests per job, also capped at Azure's 100,000 requests and 200 MB per file. Azure works best with fewer, larger files. |
| `async` | Async chat completions on `deployment`, at most `max_concurrency` in flight. | Requests gathered per chunk |
| `sync` | One chat completion after another on `deployment`. | Only shown as chunks on the progress bar |

What happens when something goes wrong:

- **A strategy can't run at all** (the Batch API rejects the upload, a batch job fails validation because the
  deployment isn't a batch deployment, the credentials are wrong): the requests it hasn't answered go to the next
  strategy, and it's skipped for the rest of the run. Strategies the client isn't set up for are skipped quietly;
  without a `batch_deployment`, for example, the chain starts at `async`.
- **The Batch API's enqueued-token quota is full**: the job is submitted again once one of the run's own jobs
  finishes, or, if none is running, after waiting 1, 2, 4, 8, 15 and 15 minutes (one budget for the whole step,
  shared by every job that hits the full quota). If the quota is still full after that, the step's remaining
  requests fall back to full price.
- **Submitting a batch job hits a passing error** (a timeout, a 5xx): it's tried again after 30, 60 and 120
  seconds. Creating a job isn't retried blindly, since a retry after a lost response could start a second, billed
  job: the framework first looks for a job already running on the same input file.
- **A batch job expires, or runs past `batch_timeout`** (24 hours by default; Azure itself never gives up on a
  job): it's cancelled, the replies it finished are kept, and the rest go to the next strategy. Waiting for the
  cancellation to complete takes up to `batch_cancel_wait` more seconds.
- **A batch job's status can't be checked** (network trouble): the framework keeps trying for 30 minutes before
  giving up on it. Output files that can't be downloaded after three tries are left on Azure (and named in the
  log) rather than deleted, so their replies can still be recovered.
- **Individual requests fail** (throttled after the SDK's retries, timed out, a connection dropped, an empty
  reply): just those go to the next strategy.
- **A request fails because of its content** (content filter, too long for the model): it isn't retried, since
  every route would fail the same way.
- **The setup is wrong** (bad key, missing role, unknown deployment, no Entra ID credential, an endpoint that
  can't be reached three times in a row): the last strategy stops the run with an `LLMSetupError` that says what
  to check. Requests that fail together in one outage count once, so a network blip during a busy async run
  doesn't end the strategy; ten outages in a row after a success do. Connecting times out after 15 seconds, so an
  endpoint that silently drops traffic is noticed quickly.

Requests that fail every strategy leave `None` in the output (and their error in `error_column`, if you set
one). For the map, `on_error="warn"` (the default) sums up what failed in a warning and goes on, and
`on_error="raise"` stops with a `StepFailedError`. A failed reduce group would leave its share of the data out of
the final text, so `reduce_on_error` defaults to `"raise"`; with `"warn"`, the result's `reduce_failures` (and
`complete`) show what was left out.

Replies that arrived aren't lost when a step stops, even on Ctrl+C: the exception carries them in `.outputs` (and
the requests that failed for good in `.failures`), `map()` and `run()` add the partial DataFrame as `.frame`, a
reduce that stops adds the finished levels as `.levels`, and `run()` attaches the mapped DataFrame as `.frame` when
the reduce stops. The one exception is batch jobs still running when you press Ctrl+C: they're cancelled, and
whatever they had finished isn't downloaded (on Azure, their output files expire with the rest).

## Options

| Option | Default | Meaning |
|---|---|---|
| `map_prompt` | required | Prompt for each record; `{text}` marks where the record goes. A function of the text also works. |
| `reduce_prompt` | required | Prompt for each group of texts; `{text}` marks where the joined group goes. |
| `collapse_prompt` | `reduce_prompt` | Prompt for the intermediate levels, if they should differ from the last one (LangChain's collapse step). |
| `system_prompt` | none | System message sent with every request. |
| `map_batch_size` | 1000 | Records per batch in the map step (see the table above). |
| `reduce_group_size` | 10 | Texts joined into each reduce request (2 or more). |
| `reduce_batch_size` | `map_batch_size` | Groups per batch in the reduce step. |
| `balance_groups` | False | Spread each level's texts evenly over the same number of groups: 11 texts in groups of up to 10 become 6 + 5 instead of 10 + 1. |
| `separator` | `"\n\n"` | What goes between the texts of a group. |
| `strategies` | `("batch", "async", "sync")` | Which strategies to try, in order. |
| `max_concurrency` | 16 | Async requests in flight. Lower it if you hit throttling. |
| `batch_poll_interval` | 60 | Seconds between batch job checks. |
| `batch_timeout` | 86400 (24 h) | Seconds to wait for a batch job before cancelling it and falling back. None waits as long as it takes. |
| `batch_cancel_wait` | 600 | Seconds to wait for a cancelled batch job to stop, to collect its finished replies. |
| `max_concurrent_batch_jobs` | 4 | Batch jobs running at once. |
| `batch_cleanup` | True | Delete the uploaded input files and the downloaded output files afterwards. |
| `on_error` | `"warn"` | Records that fail every strategy: `"warn"` or `"raise"`. |
| `reduce_on_error` | `"raise"` | Reduce groups that fail every strategy: `"raise"` or `"warn"` (leave the group out). |
| `show_progress` | True | Show the tqdm bars (`tqdm.auto`: widgets in notebooks when `ipywidgets` is installed, text bars otherwise). |

Prompts replace only the `{text}` placeholder, so other braces (JSON examples, say) are left as they are. Use
`placeholder="<<TEXT>>"` for a different marker.

The reduce takes `ceil(log_g(n))` levels for `n` map outputs and a group size `g` (at least one, so a single
output still gets the reduce prompt). Levels before the last use `collapse_prompt`; the last one, whose texts
fit in a single group, uses `reduce_prompt`. Map outputs that are `None` are left out.

## Azure setup

- **Standard deployment** (`deployment`) for the async and loop strategies: any chat model deployment, such as
  GPT-4.1 or GPT-5 mini, on a Standard, Global Standard or Provisioned deployment type.
- **Batch deployment** (`batch_deployment`) for the Batch API: a deployment of type **Global Batch** or **Data
  Zone Batch** of a model that supports batch. Leave it out if you don't have one.
- **Endpoint**: the resource's endpoint from the Azure portal (`https://<name>.openai.azure.com/`,
  `.services.ai.azure.com` or `.cognitiveservices.azure.com`, with or without `https://`), its bare name, a
  Foundry project endpoint, or a deployment's Target URI. The client uses the v1 API (`/openai/v1/`), so there's
  no `api-version` to pick.
- **Authentication**: an API key, or Entra ID when you leave the key out (`az login`, a managed identity, or a
  service principal in `AZURE_TENANT_ID` / `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET`). For async and loop calls
  the identity needs the **Cognitive Services OpenAI User** role on the resource. The Batch API also uploads files
  and creates jobs, which that role can't do: give the identity **Cognitive Services OpenAI Contributor** (or a
  custom role with the `OpenAI/files/*` and `OpenAI/batches/*` data actions). Without it the batch strategy is
  skipped after a warning and everything is sent at full price.
- **Your own client**: pass `client=openai.OpenAI(...)` or `openai.AzureOpenAI(...)`, plus
  `async_client_factory=lambda: openai.AsyncOpenAI(...)` for the async strategy (it must return a new client each
  time); without a factory the async strategy is skipped, with a warning. With an `openai.AzureOpenAI` client on
  the classic, `api-version` based API, batch files use the classic path `/chat/completions` and no file expiry,
  which older API versions don't take (an `AzureOpenAI` client pointed at `/openai/v1/` gets the v1 settings; set
  `batch_endpoint` and `batch_file_expiry` to override).
- **Model settings** such as `temperature`, `max_completion_tokens`, `reasoning_effort` or `response_format` go
  in `completion_options` and are sent with every request, batch or not. Settings the `openai` SDK doesn't know
  by name are passed through in the request body. SDK call options (`timeout`, `extra_body`, `extra_headers`,
  `extra_query`) aren't accepted there, since they can't go in a batch file: put body fields in directly.
- **Batch files**: uploaded input files and generated output files are set to expire after 14 days
  (`batch_file_expiry`, in seconds from 14 to 30 days, the range Azure accepts; None keeps them until deleted), so
  files left behind by an interrupted run don't pile up against the resource's file limit. `batch_endpoint`
  (default `/v1/chat/completions`) is the path written into the batch input files.

## Good to know

- **Cost and speed**: the Batch API costs about half as much, but a job can take anywhere from minutes to a day.
  Lower `batch_timeout` if you'd rather pay full price than wait, or use `strategies=("async", "sync")` to skip
  it. Each reduce level is a separate step, so with the Batch API a three-level reduce means three rounds of
  batch jobs.
- **Throttling**: the SDK retries 429s six times, honouring `retry-after`. Requests still throttled after that
  go to the next strategy.
- **Notebooks**: async calls work inside Jupyter and Databricks notebooks, whose event loop is already running
  (the framework gives them a loop on a worker thread).
- **Interrupting**: stopping a run (Ctrl+C, or "Interrupt kernel" in a notebook) cancels its async requests and
  its running batch jobs.
- **Logging**: while the bars are shown, log lines from console handlers (and Python's own last-resort warning
  output, when logging isn't set up) are printed above the bars instead of through them. Handlers keep their
  levels, and none are added.
- **Odd characters**: a lone surrogate in a text (half of an emoji cut off, say) is replaced with `?` so every
  route can send it.
- **Context length**: the reduce groups by count, so choose `reduce_group_size` so that that many map outputs
  (plus the prompt) fit in the model's context window. Keep map outputs short with the map prompt or
  `max_completion_tokens`.

## Development

```bash
pip install -e ".[dev]"
pytest
```

The tests use a fake Azure client and a mock HTTP transport, so they run offline without credentials. The
PySpark tests start a local Spark session (they need Java) and are skipped when PySpark isn't installed.

| Module | Role |
|---|---|
| `mapreduce.py` | `MapReduce`: prompts, the map step, the recursive reduce, error reporting |
| `runners.py` | The strategies (`BatchRunner`, `AsyncRunner`, `SyncRunner`) and the fallback chain |
| `client.py` | `AzureChatClient`: Azure OpenAI v1 endpoint, auth, chat calls, Batch API calls, error mapping |
| `frames.py` | Reading a column from, and adding columns to, pandas and PySpark DataFrames |
| `progress.py` | The tqdm bars and log routing |
| `errors.py` | The exceptions |
