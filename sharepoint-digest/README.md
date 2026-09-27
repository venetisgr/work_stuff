# SharePoint digest

Pick a SharePoint folder and a date range. The tool finds the PowerPoint and Word files changed in that range,
summarizes each one with your GPT model in Azure AI Foundry, and combines the summaries into a single digest.

## How it works

1. **Find**: lists the folder (and its subfolders) through Microsoft Graph, or reads a copy synced to your computer,
   and keeps the `.pptx` and `.docx` files
   whose last-modified date falls in the range.
2. **Read**: downloads each file and extracts its text. From decks it takes slide titles, bullets, tables, chart data
   and speaker notes; from Word documents, headings, paragraphs, lists and tables, in order.
3. **Summarize**: sends each file to your Foundry deployment for a structured summary (overview, key points,
   decisions, action items, risks). Files too long for one request are summarized in parts and then merged.
4. **Digest**: combines all the summaries into highlights, themes, decisions, action items and risks, citing the
   source files.
5. **Write**: saves a Markdown digest (with a linked index of the files and each file's own summary) and a JSON copy.

## Setup

Requires Python 3.11 or newer.

### 1. Install

```bash
cd sharepoint-digest
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e .
cp .env.example .env           # Windows: copy .env.example .env
```

### 2. SharePoint access (Microsoft Graph)

Can't get an app registration? Skip this step and [read a synced copy](#reading-a-synced-copy-instead-of-sharepoint)
instead; your own access to the folders is enough for that.

Register an app in the Azure portal (**Microsoft Entra ID → App registrations → New registration**) and note its
tenant ID and client (application) ID. Then set it up for one of these sign-in modes (`GRAPH_AUTH_MODE` in `.env`):

- **`app`** (default; runs unattended, e.g. on a schedule): under **API permissions**, add the Microsoft Graph
  *application* permission `Sites.Read.All` and have an admin grant consent. Create a client secret under
  **Certificates & secrets**. For least privilege, use `Sites.Selected` instead, and have an admin give the app read
  access to just your site.
- **`device_code`** (signs in as you, reads what you can read): add the *delegated* permission `Sites.Read.All` and
  turn on **Authentication → Allow public client flows**. Each run prints a code to enter at
  https://microsoft.com/devicelogin.
- **`default`**: uses `DefaultAzureCredential` (managed identity, Azure CLI sign-in, or a certificate via
  `AZURE_CLIENT_CERTIFICATE_PATH`).

Put `AZURE_TENANT_ID`, `AZURE_CLIENT_ID` and, for `app` mode, `AZURE_CLIENT_SECRET` in `.env`, along with
`SHAREPOINT_SITE_URL` (the site address only, e.g. `https://contoso.sharepoint.com/sites/TeamSite`) and
`SHAREPOINT_LIBRARY` (`Documents` is the default library, whose address ends in "Shared Documents").

### 3. Azure AI Foundry

You need a GPT chat model deployed in a Foundry (or Azure OpenAI) resource, such as GPT-4o, GPT-4.1, GPT-5 or
GPT-5 mini. The tool uses the Chat Completions API, so models that only offer the Responses API (such as the codex
models) won't work.

- `FOUNDRY_ENDPOINT`: the endpoint from the resource's overview page, e.g. `https://my-resource.openai.azure.com/`
  (the resource name alone also works). You can also paste the chat completions URL you already use, the
  deployment's Target URI (`.../openai/deployments/<name>/chat/completions?api-version=...`); the tool then takes the
  deployment name from it.
- `FOUNDRY_DEPLOYMENT`: the **deployment** name from **Models + endpoints**, which can differ from the model name.
- `FOUNDRY_API_KEY`: a key from the resource's **Keys and Endpoint** page. Leave it empty to use Entra ID instead;
  the identity then needs the **Cognitive Services OpenAI User** role on the resource. With Entra ID, the tool signs
  in as the app from step 2 if `AZURE_CLIENT_SECRET` is set, otherwise through `az login` or a managed identity.
- For reasoning models (such as GPT-5 or o3), `FOUNDRY_REASONING_EFFORT` (e.g. `low`) trades depth for speed and
  cost. Leave it unset for GPT-4o and GPT-4.1, which don't accept it.

### 4. Folders

The folders to choose from live in [`folders.toml`](folders.toml). It currently has four placeholders
(`temp-folder-1` to `temp-folder-4`); replace them with the real ones:

```toml
[folders.project-alpha]           # the key you pass to --folder
label = "Project Alpha"            # shown in the menu and the digest title
path = "Projects/Alpha"            # folder path inside the library ("" for the library root)
# site_url = "https://contoso.sharepoint.com/sites/Other"   # optional, if it's on another site
# library = "Board Documents"                                # optional, if it's in another library
```

### Reading a synced copy instead of SharePoint

This route needs no app registration and no SharePoint permissions beyond your own; only the Foundry settings.

1. In SharePoint, open the folder and select **Sync** (or **Add shortcut to My files**). OneDrive keeps a copy on
   your computer up to date, and the files keep their SharePoint modified dates, so date ranges still work.
2. Find the folder in File Explorer and copy its path.
3. Add it to the folder's entry in `folders.toml`, in single quotes so the backslashes stay as they are:

   ```toml
   [folders.temp-folder-1]
   label = "Temp Folder 1"
   local_path = 'C:\Users\you\Contoso\Team Site - Documents\Temp Folder 1'
   ```

   Or skip `folders.toml` and point at any folder directly:
   `python -m sharepoint_digest --local-dir "C:\path\to\folder" --days 14`.

A folder you downloaded by hand works the same way, but downloaded files may be stamped with the download date
instead of the SharePoint date, so download only the files in your range. `--date-field created` isn't available
for local copies.

## Usage

```bash
# Choose a folder from a menu; covers the last 7 days
python -m sharepoint_digest

# A given folder (by key, label or menu number) and date range
python -m sharepoint_digest --folder temp-folder-1 --start 2026-09-01 --end 2026-09-26

# The last 30 days, ignoring subfolders
python -m sharepoint_digest --folder temp-folder-2 --days 30 --no-subfolders

# See which files would be summarized, without calling the model
python -m sharepoint_digest --folder temp-folder-3 --days 14 --dry-run
```

After `pip install -e .`, `sharepoint-digest` works as a shorthand for `python -m sharepoint_digest`.

| Option | Meaning |
|---|---|
| `--folder` | Folder key, label or number from `folders.toml`. Without it you get a menu. |
| `--start` / `--end` | First and last day of the range (`YYYY-MM-DD`, both included). `--end` defaults to today. |
| `--days N` | The last N days up to `--end`, instead of `--start`. The default range is the last 7 days. |
| `--date-field created` | Filter on the file's creation date instead of its last-modified date. |
| `--local-dir PATH` | Read the files from a folder on your computer instead of SharePoint. |
| `--no-subfolders` | Only look at files directly in the folder. |
| `--workers N` | Files summarized in parallel (default 4). Lower it if Foundry throttles you. |
| `--output-dir DIR` | Where to write the results (default `./output`). |
| `--list-folders` | Show the configured folders. |
| `--dry-run` | List the matching files and stop. |

### Output

For each run, two files land in `output/`:

- `<folder>_<start>_to_<end>_digest.md`: the digest, a table of the documents with links to SharePoint, any files
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
| `cli.py` | Command-line options, folder menu, output messages |
| `pipeline.py` | The run: list files, summarize them in parallel, build the digest |
| `sharepoint.py` | Microsoft Graph: find the site, library and folder; list and download files |
| `local.py` | The same for a folder on your computer, such as a OneDrive-synced copy |
| `extract.py` | Text from `.pptx` and `.docx` files |
| `llm.py` | The GPT deployment in Azure AI Foundry (OpenAI v1 API) |
| `summarize.py` / `prompts.py` | Per-document summaries and the digest |
| `report.py` | Markdown and JSON output |
| `config.py` | `.env` settings and `folders.toml` |
