# work_stuff

| Project | What it does |
|---|---|
| [sharepoint-digest](sharepoint-digest/) | Summarizes the PowerPoint and Word files in a SharePoint folder over a date range and rolls them up into a digest, using a GPT model in Azure AI Foundry. |
| [news-dip-scanner](news-dip-scanner/) | Polls ~20 financial news feeds, has an LLM find the listed companies each story affects, checks for price dips, and rates each dip as temporary fear or real damage with a 6-month probability, potential low and limit-order ideas. Runs from the command line or as an invite-only, mobile-first website (per-user watchlists, alert rules and channels; deploys to Fly.io). Reports and alerts only; it never trades. |
