# work_stuff

| Project | What it does |
|---|---|
| [sharepoint-digest](sharepoint-digest/) | Summarizes the PowerPoint and Word files in a SharePoint folder over a date range and rolls them up into a digest. Three versions: a local Python script for synced or downloaded folders, an online Python script that reads SharePoint directly (both using a GPT model in Azure AI Foundry), and a Copilot Studio agent built from topics. |
| [azure-mapreduce](azure-mapreduce/) | LangChain-style map-reduce over pandas and PySpark DataFrames with Azure OpenAI: a prompt on every record of a column into an output column, then a recursive reduce of the outputs in groups down to one text. Requests go to the Azure Batch API, falling back to async chat completions, then to a plain loop, with tqdm progress per step and per reduce level. |
