# SharePoint digest

Pick a folder and a date range, and get a digest of the PowerPoint and Word files that changed in it: a summary of
each file, rolled up into highlights, decisions, action items and risks.

There are three versions:

| Version | Reads the files from | What you need |
|---|---|---|
| **1. Local**: `local_digest.py` | A folder on your computer, such as a OneDrive-synced copy of the SharePoint folder, or a download | Your GPT deployment in Azure AI Foundry |
| **2. Online**: `online_digest.py` | SharePoint directly, through Microsoft Graph | An app registration from IT, plus your Foundry deployment |
| **3. Copilot Studio** | SharePoint, from a Copilot Studio agent built only from topics | Copilot Studio; no code, no Foundry. See [copilot-studio/README.md](copilot-studio/README.md) |

Versions 1 and 2 share the same engine, so their digests are identical; they differ only in where the files come
from. Version 3 is a lighter, chat-based alternative with limits explained in its guide.

## How versions 1 and 2 work

1. **Find**: list the folder and its subfolders, keeping the `.pptx` and `.docx` files last modified in the range.
2. **Read**: extract each file's text. From decks: slide titles, bullets, tables, chart data and speaker notes. From
   Word documents: headings, paragraphs, lists and tables, in order.
3. **Summarize**: send each file to your GPT deployment for a structured summary (overview, key points, decisions,
   action items, risks). Files too long for one request are summarized in parts, then merged.
4. **Digest**: combine the summaries into highlights, themes, decisions, action items and risks, citing the files.
5. **Write**: save a Markdown digest (with a linked index of the files and each file's own summary) and a JSON copy.

## Setup (versions 1 and 2)

Requires Python 3.11 or newer.

### 1. Install

```bash
cd sharepoint-digest
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e .
cp .env.example .env           # Windows: copy .env.example .env
```

### 2. Azure AI Foundry

You need a GPT chat model deployed in a Foundry (or Azure OpenAI) resource, such as GPT-4o, GPT-4.1, GPT-5 or
GPT-5 mini. The tool uses the Chat Completions API, so models that only offer the Responses API (such as the codex
models) won't work. In `.env`:

- `FOUNDRY_ENDPOINT`: the endpoint from the resource's overview page, e.g. `https://my-resource.openai.azure.com/`
  (the resource name alone also works). You can also paste the chat completions URL you already use, the
  deployment's Target URI (`.../openai/deployments/<name>/chat/completions?api-version=...`); the tool then takes the
  deployment name from it.
- `FOUNDRY_DEPLOYMENT`: the **deployment** name from **Models + endpoints**, which can differ from the model name.
- `FOUNDRY_API_KEY`: a key from the resource's **Keys and Endpoint** page. Leave it empty to use Entra ID instead;
  the identity then needs the **Cognitive Services OpenAI User** role on the resource. With Entra ID, the tool signs
  in as the app from version 2's setup if `AZURE_CLIENT_SECRET` is set, otherwise through `az login` or a managed
  identity.
- For reasoning models (such as GPT-5 or o3), `FOUNDRY_REASONING_EFFORT` (e.g. `low`) trades depth for speed and
  cost. Leave it unset for GPT-4o and GPT-4.1, which don't accept it.

### 3. Folders

The folders to choose from live in [`folders.toml`](folders.toml). It has four placeholders (`temp-folder-1` to
`temp-folder-4`) until the real ones are known. Each folder can have a SharePoint `path` (used by version 2) and a
`local_path` (used by version 1):

```toml
[folders.project-alpha]           # the key you pass to --folder
label = "Project Alpha"            # shown in the menu and the digest title
path = "Projects/Alpha"            # version 2: folder path inside the library ("" for the library root)
local_path = 'C:\Users\you\Contoso\Team Site - Documents\Projects\Alpha'   # version 1, in single quotes
# site_url = "https://contoso.sharepoint.com/sites/Other"   # version 2, optional: another site
# library = "Board Documents"                                # version 2, optional: another library
```

## Version 1: local

Needs nothing beyond your own access to the files and the Foundry settings.

**Get the files onto your computer.** In SharePoint, open the folder and select **Sync** (or **Add shortcut to My
files**). OneDrive keeps a copy on your computer up to date, and the files keep their SharePoint modified dates, so
date ranges work. You can also select the folder and choose **Download** to get a ZIP; unzip it and check the
**Date modified** column, because downloaded files may carry the download date instead of the SharePoint one.

**Run it**, pointing `--folder` at the folder's path or at a `folders.toml` folder that has a `local_path`:

```bash
# A folder path; covers the last 14 days
python local_digest.py --folder "C:\Users\you\Contoso\Team Site - Documents\Temp Folder 1" --days 14

# A folder from folders.toml, with a date range
python local_digest.py --folder temp-folder-1 --start 2026-09-01 --end 2026-09-26

# No --folder: choose from the folders that have a local_path
python local_digest.py --days 7

# See which files would be summarized, without calling the model
python local_digest.py --folder temp-folder-1 --days 14 --dry-run
```

## Version 2: online

Reads SharePoint directly, so there's nothing to sync, but it needs an **app registration** in Microsoft Entra ID
(Azure portal → **Microsoft Entra ID → App registrations**). Most companies have IT create it. Choose one sign-in
mode with `GRAPH_AUTH_MODE` in `.env`:

- **`device_code`** (runs as you, recommended for running it yourself): the Microsoft Graph *delegated* permission
  `Sites.Read.All` with admin consent, and **Authentication → Allow public client flows** turned on. No secret is
  needed. Each run prints a code to enter at https://microsoft.com/devicelogin, and the tool can read only what you
  can open yourself.
- **`app`** (runs unattended, e.g. on a schedule): the Graph *application* permission `Sites.Read.All` with admin
  consent, plus a client secret. For least privilege, IT can use `Sites.Selected` instead and give the app read
  access to just your site.
- **`default`**: uses `DefaultAzureCredential` (managed identity, Azure CLI sign-in, or a certificate via
  `AZURE_CLIENT_CERTIFICATE_PATH`).

Then fill in `AZURE_TENANT_ID`, `AZURE_CLIENT_ID` (and `AZURE_CLIENT_SECRET` for `app` mode), `SHAREPOINT_SITE_URL`
(the site address only, e.g. `https://contoso.sharepoint.com/sites/TeamSite`) and `SHAREPOINT_LIBRARY` (`Documents`
is the default library, whose address ends in "Shared Documents").

```bash
# Choose a folder from a menu; covers the last 7 days
python online_digest.py

# A given folder (by key, label or menu number) and date range
python online_digest.py --folder temp-folder-1 --start 2026-09-01 --end 2026-09-26

# Filter on creation date instead of last-modified date
python online_digest.py --folder temp-folder-2 --days 30 --date-field created

# Check SharePoint access and the date filter without calling the model
python online_digest.py --folder temp-folder-3 --days 14 --dry-run
```

## Options

Both versions accept these options (after `pip install -e .`, `digest-local` and `digest-online` work as shorthands
for the two scripts):

| Option | Meaning |
|---|---|
| `--folder` | Version 1: a folder path, or a `folders.toml` folder with a `local_path`. Version 2: a folder key, label or number from `folders.toml`. Without it you get a menu. |
| `--start` / `--end` | First and last day of the range (`YYYY-MM-DD`, both included). `--end` defaults to today. |
| `--days N` | The last N days up to `--end`, instead of `--start`. The default range is the last 7 days. |
| `--date-field created` | Version 2 only: filter on the file's creation date instead of its last-modified date. |
| `--no-subfolders` | Only look at files directly in the folder. |
| `--workers N` | Files summarized in parallel (default 4). Lower it if Foundry throttles you. |
| `--output-dir DIR` | Where to write the results (default `./output`). |
| `--list-folders` | Show the configured folders. |
| `--dry-run` | List the matching files and stop. |

## Output

For each run, two files land in `output/`:

- `<folder>_<start>_to_<end>_digest.md`: the digest, a table of the documents with links to the files, any files
  that couldn't be included (and why), then each document's own summary.
- `<folder>_<start>_to_<end>_summaries.json`: the same content in machine-readable form.

## Good to know

- **Dates** are whole days in your local time zone, both ends included.
- **Formats**: `.pptx`, `.pptm` and `.docx`. Older `.ppt`/`.doc` files, `.docm` and slide shows (`.pps`/`.ppsx`) are
  listed under "Not included"; save them as `.pptx` or `.docx` to include them. Files protected by a password or by
  sensitivity-label encryption can't be read and are listed there too.
- **What the model sees**: text only. Images and SmartArt are skipped; charts contribute their title and data.
- **Long documents** are split at `LLM_MAX_INPUT_CHARS` (default 200,000 characters, about 50,000 tokens),
  summarized part by part, then merged. Lower it if your model has a small context window.
- **Where data goes**: document text is sent only to the Foundry deployment you configure.
- **Changing the summaries**: the prompts are in [`sharepoint_digest/prompts.py`](sharepoint_digest/prompts.py).

## Troubleshooting

| Problem | What to check |
|---|---|
| `Local folder not found` | The path in `--folder` or `local_path`. In `folders.toml`, use single quotes around Windows paths. |
| 401 or 403 from Microsoft Graph | The app's Graph permission (`Sites.Read.All` or `Sites.Selected`) and admin consent. With `Sites.Selected`, the app also needs a grant on the site. |
| `Document library "..." not found` | The error lists the libraries on the site; set `SHAREPOINT_LIBRARY` to one of them. |
| `Folder "..." not found` | The `path` in `folders.toml` is relative to the library, e.g. `Projects/Alpha`, not the full URL. |
| `No deployment named ...` | `FOUNDRY_DEPLOYMENT` must be the deployment name, and `FOUNDRY_ENDPOINT` the resource that hosts it. |
| Throttling (429) | Use fewer `--workers` or raise the deployment's tokens-per-minute quota. |
| SSL certificate errors on a corporate network | The tool trusts the operating system's certificates. If your proxy's root certificate isn't installed there, point `SSL_CERT_FILE` and `REQUESTS_CA_BUNDLE` at a bundle that includes it. |

Add `-v` to any command for debug logging.

## Development

```bash
pip install -e ".[dev]"
pytest
```

The tests use fake SharePoint and model backends, so they run offline without credentials.

| Module | Role |
|---|---|
| `cli.py` | The two command-line versions: options, folder menu, output messages |
| `pipeline.py` | The run: list files, summarize them in parallel, build the digest |
| `sharepoint.py` | Version 2's source: find the site, library and folder through Microsoft Graph; list and download files |
| `local.py` | Version 1's source: a folder on your computer |
| `extract.py` | Text from `.pptx` and `.docx` files |
| `llm.py` | The GPT deployment in Azure AI Foundry (OpenAI v1 API) |
| `summarize.py` / `prompts.py` | Per-document summaries and the digest |
| `report.py` | Markdown and JSON output |
| `config.py` | `.env` settings and `folders.toml` |
