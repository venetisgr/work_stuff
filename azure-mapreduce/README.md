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
Map [batch]: 100%|██████████| 2500/2500 [14:02<00:00, 2.97record/s, jobs 5/5 done]
Reduce:  67%|██████▋   | 2/3 [00:41<00:20, level 3: 3 texts in 1 group of up to 10]
Level 3/3: 3 → 1 [async]:   0%|          | 0/1 [00:00<?, ?group/s]
```

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
    map_batch_size=500,  # records per batch (one Batch API job each)
    reduce_group_size=10,  # map outputs joined into each reduce request
)

reviews = pd.DataFrame({"review": ["Great battery life...", "Stopped working after a week...", ...]})
result = mr.run(reviews, column="review", output_column="summary")

result.frame  # the reviews with a "summary" column
result.output  # the single reduced text
result.levels  # every level: levels[0] are the summaries, levels[-1] == [result.output]
```

The two steps also work on their own:

```python
summaries = mr.map(reviews, "review", "summary", error_column="summary_error")
overview = mr.reduce(summaries, column="summary").output
overview = mr.reduce(["text one", "text two", ...]).output  # any list of texts
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
quota allows. Without `id_column` the whole DataFrame is collected and rebuilt with the new column at the end
(types and row order kept). With `id_column` (unique, non-empty values) only the ids and texts are collected
and the replies are joined back on the id, which suits wide tables. It works with classic Spark and Spark
Connect (Databricks serverless and shared clusters).

## How the requests are sent

Each step (the map, and each reduce level) hands its requests to the strategies in `strategies`, in order
(default `("batch", "async", "sync")`):

| Strategy | What it does | Batch size means |
|---|---|---|
| `batch` | Uploads a JSONL file and runs an [Azure Batch API](https://learn.microsoft.com/azure/ai-foundry/openai/how-to/batch) job on `batch_deployment`, polling every `batch_poll_interval` seconds. Up to `max_concurrent_batch_jobs` jobs run at once. | Requests per job (also capped at Azure's 100,000 requests and 200 MB per file) |
| `async` | Async chat completions on `deployment`, at most `max_concurrency` in flight. | Requests gathered per chunk |
| `sync` | One chat completion after another on `deployment`. | Only shown as chunks on the progress bar |

What happens when something goes wrong:

- **A strategy can't run at all** (no `batch_deployment`, the Batch API rejects the upload, the async client
  can't start): all its requests go to the next strategy, and it's skipped for the rest of the run. Strategies
  the client isn't set up for are skipped quietly; for example, without a `batch_deployment` the chain starts
  at `async`.
- **A batch job fails, expires, or runs past `batch_timeout`**: the replies it did finish are kept, the job is
  cancelled if it's still running, and the requests without a reply go to the next strategy.
- **Individual requests fail** (throttled after the SDK's retries, timed out, empty reply): just those go to the
  next strategy.
- **A request fails because of its content** (content filter, too long for the model): it isn't retried, since
  every route would fail the same way.
- **The setup is wrong** (bad key, missing role, unknown deployment or endpoint): the last strategy stops the
  run with an `LLMSetupError` that says what to check.

Requests that fail every strategy leave `None` in the output (and their error in `error_column`, if you set
one). With `on_error="warn"` (the default) a warning sums up what failed and the run goes on; with
`on_error="raise"` a `StepFailedError` stops it, carrying the outputs that did succeed in `.outputs`.

## Options

| Option | Default | Meaning |
|---|---|---|
| `map_prompt` | required | Prompt for each record; `{text}` marks where the record goes. A function of the text also works. |
| `reduce_prompt` | required | Prompt for each group of texts; `{text}` marks where the joined group goes. |
| `collapse_prompt` | `reduce_prompt` | Prompt for the intermediate levels, if they should differ from the last one (LangChain's collapse step). |
| `system_prompt` | none | System message sent with every request. |
| `map_batch_size` | 100 | Records per batch in the map step (see the table above). |
| `reduce_group_size` | 10 | Texts joined into each reduce request (2 or more). |
| `reduce_batch_size` | `map_batch_size` | Groups per batch in the reduce step. |
| `separator` | `"\n\n"` | What goes between the texts of a group. |
| `strategies` | `("batch", "async", "sync")` | Which strategies to try, in order. |
| `max_concurrency` | 16 | Async requests in flight. Lower it if you hit throttling. |
| `batch_poll_interval` | 30 | Seconds between batch job checks. |
| `batch_timeout` | none | Seconds to wait for a batch job before cancelling it and falling back. None waits for the 24-hour window. |
| `max_concurrent_batch_jobs` | 4 | Batch jobs running at once. |
| `batch_cleanup` | True | Delete the uploaded input and downloaded output files afterwards. |
| `on_error` | `"warn"` | `"warn"` or `"raise"`, see above. |
| `show_progress` | True | Show the tqdm bars (`tqdm.auto`, so they render as widgets in notebooks). |

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
  `.services.ai.azure.com` or `.cognitiveservices.azure.com`), its bare name, a Foundry project endpoint, or a
  deployment's Target URI. The client uses the v1 API (`/openai/v1/`), so there's no `api-version` to pick.
- **Authentication**: an API key, or Entra ID when you leave the key out (`az login`, a managed identity, or a
  service principal in `AZURE_TENANT_ID` / `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET`). The identity needs the
  **Cognitive Services OpenAI User** role on the resource.
- **Your own client**: pass `client=openai.OpenAI(...)` or `openai.AzureOpenAI(...)`, plus
  `async_client_factory=lambda: openai.AsyncOpenAI(...)` for the async strategy.
- **Model settings** such as `temperature`, `max_completion_tokens`, `reasoning_effort` or `response_format` go
  in `completion_options` and are sent with every request, batch or not.

## Good to know

- **Cost and speed**: the Batch API costs about half as much, but a job can take anywhere from minutes to 24
  hours. Set `batch_timeout` if you'd rather pay full price than wait, or `strategies=("async", "sync")` to skip
  it. Each reduce level is a separate step, so with the Batch API a three-level reduce means three rounds of
  batch jobs.
- **Throttling**: the SDK retries 429s six times, honouring `retry-after`. Requests still throttled after that
  go to the next strategy.
- **Notebooks**: async calls work inside Jupyter and Databricks notebooks, whose event loop is already running
  (the framework gives them a loop on a worker thread).
- **Interrupting**: stopping a run (Ctrl+C) cancels its running batch jobs.
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
| `progress.py` | The tqdm bars |
| `errors.py` | The exceptions |
