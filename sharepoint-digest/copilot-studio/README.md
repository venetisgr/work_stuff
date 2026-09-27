# Version 3: Copilot Studio agent (topics only)

A Copilot Studio agent that writes the digest in a chat. It uses only a topic and the built-in **Create generative
answers** node, so it needs no code, no Foundry key and no tools. With the agent's default **Authenticate with
Microsoft** sign-in, it also needs no app registration: it searches SharePoint as the person chatting with it.

## How it differs from the Python versions

- **It searches instead of reading every file.** Generative answers finds the passages most relevant to a question
  and summarizes them. So the digest is organized by question (highlights, decisions, action items, risks) across
  the files, not one summary per file. In a folder where many files changed, some may not make it into the answers.
- **It lives in chat**, in Teams or Copilot Studio's test pane, rather than writing a file on your computer.
- **Encrypted files are skipped**, as in the Python versions.

## What you need

- Access to Copilot Studio in your company's environment, with generative answers allowed in topics.
- Read access to the SharePoint folders. The agent only sees what the person chatting with it can open.
- The agent's authentication left on **Authenticate with Microsoft** (the default). It works in Teams, Power Apps
  and Microsoft 365 Copilot.

Menu names below match Copilot Studio as of September 2026 and may differ slightly in your environment.

## Build it

### 1. Create the agent

1. In Copilot Studio, create an agent (for example "Folder digest") or open an existing one.
2. In **Settings → Generative AI**, keep the classic, topic-based orchestration, and turn off **Use general
   knowledge** and web search, so answers come only from SharePoint.
3. In **Settings → Security → Authentication**, keep **Authenticate with Microsoft**.

### 2. Create the topic and its variables

Create a topic from blank named **Folder digest**, then:

1. **Trigger phrases**: `folder digest`, `summarize a folder`, `what changed in the folder`, `SharePoint digest`.
2. **Ask a question**: "Which folder should the digest cover?" Set **Identify** to **Multiple choice options** and
   add `Temp Folder 1`, `Temp Folder 2`, `Temp Folder 3` and `Temp Folder 4`. Save the answer as `FolderChoice`.
   Copilot Studio adds a condition branch for each option.
3. In each branch, add **Set a variable value**. Create a variable named `DigestFolderUrl`, set its usage to
   **Global (any topic can access)**, and set it to that folder's address, for example
   `https://contoso.sharepoint.com/sites/YourSite/Shared Documents/Temp Folder 1`. To find the address, open the
   folder in SharePoint, open its **Details** pane, and copy its **Path**.
4. **Ask a question**: "From which date? For example, 1 September 2026." Set **Identify** to **Date and time**. Save
   the answer as a new **Global** variable named `DigestStart`.
5. **Ask a question**: "Up to which date?" Set **Identify** to **Date and time**. Save the answer as a new **Global**
   variable named `DigestEnd`.

### 3. Add the chosen folder as a knowledge source

1. Go to **Knowledge → Add knowledge → SharePoint**.
2. In the URL field, select the variable picker (**{x}**) and choose `Global.DigestFolderUrl`.
3. Name the source **Digest folder**, with the description "The SharePoint folder chosen in the Folder digest
   topic", and select **Add to agent**.
4. On the **Knowledge** page, open the source's **… → Edit → Advanced settings** and add two filters:
   - **Modified on** / **on or after** / the variable `Global.DigestStart`
   - **Modified on** / **on or before** / the variable `Global.DigestEnd`

   If your version only offers **on or after**, add just the first filter; the digest then runs from the start date
   up to today.

### 4. Add the digest to the topic

After the date questions, add a **Send a message** node: "Here's the digest for {FolderChoice} from {DigestStart} to
{DigestEnd}." Insert the variables with **{x}**.

Then add four sections. Each section is a **Send a message** node with the section title, followed by a
**Create generative answers** node (**Add node → Advanced → Generative answers**) whose **Input** is the text below:

| Section title | Generative answers input |
|---|---|
| **Highlights** | Summarize the main updates, results and changes described in these documents in 3 to 6 bullet points. After each point, give the file name of the document it comes from in brackets. |
| **Decisions** | What decisions were made or approved according to these documents? List each one as a bullet with the file name of the document in brackets. If there are none, reply "None stated." |
| **Action items** | What action items or next steps do these documents list? Give each as a bullet with the owner and due date if stated, and the file name of the document in brackets. If there are none, reply "None stated." |
| **Risks and open questions** | What risks, issues or open questions do these documents raise? List each one as a bullet with the file name of the document in brackets. If there are none, reply "None stated." |

In each generative answers node, open **… → Properties** and set:

- **Knowledge sources**: turn on **Search only selected sources** and select only **Digest folder**.
- **Allow the AI to use its own general knowledge**: off. Web search: off.
- **Advanced → Send a message**: leave it on, so each answer is posted with links to the documents it used.

Finish with **End current topic**, or a message such as "Want a digest for another folder?".

### 5. Test and publish

1. In **Test your agent**, type "folder digest", then pick a folder and dates. You're signed in as yourself, so the
   agent only sees what you can open.
2. **Publish**, then add the agent to Teams under **Channels → Teams and Microsoft 365 Copilot**. Colleagues who use
   it only get answers from files they can access.

## If a step isn't available in your environment

| Missing | Workaround |
|---|---|
| Variable picker in the SharePoint URL field | Add one knowledge source per folder. In each folder's branch of the topic, add its own four sections, each searching only that folder's source. Build one branch, then copy its nodes to the others. |
| Date filters on the knowledge source | Add the dates to each input, e.g. "...in documents modified between {DigestStart} and {DigestEnd}". This is less reliable, because search doesn't strictly filter by date. |
| Generative answers in topics | Copilot Studio can't do this without it; use the local Python version instead. |

## Optional: one polished digest from your Foundry GPT

If topics can use the **Send HTTP request** node and your Foundry endpoint is reachable from Copilot Studio, your
GPT deployment can combine the four sections into a single digest:

1. In each generative answers node's **Properties → Advanced**, clear **Send a message** and save the answer to a
   variable: `Highlights`, `Decisions`, `Actions` and `Risks`. Delete the four section-title messages.
2. Add **Advanced → Send HTTP request**:
   - **URL**: `https://<your-resource>.openai.azure.com/openai/v1/chat/completions`
   - **Method**: POST
   - **Headers**: `api-key` set to your Foundry key. Anyone who can edit the agent can see a key typed into the
     topic, so keep it in a secret environment variable if your environment allows it.
   - **Body**: **JSON content**, then **Edit JSON → Formula**, with your deployment name in `model`:

     ```powerfx
     {
         model: "gpt-4.1",
         messages: Table(
             {
                 role: "system",
                 content: "You write short, factual digests of internal documents for colleagues. Use only the notes you are given, and keep the file names in brackets."
             },
             {
                 role: "user",
                 content: "Combine these notes into one digest with the sections Highlights, By theme, Decisions, Action items, and Risks and open questions." & Char(10) & Char(10) &
                     "Highlights:" & Char(10) & Topic.Highlights & Char(10) & Char(10) &
                     "Decisions:" & Char(10) & Topic.Decisions & Char(10) & Char(10) &
                     "Action items:" & Char(10) & Topic.Actions & Char(10) & Char(10) &
                     "Risks and open questions:" & Char(10) & Topic.Risks
             }
         )
     }
     ```

   - **Response data type**: **From sample data**, using `{"choices": [{"message": {"content": "text"}}]}`, saved
     as `FoundryReply`.
   - **Request timeout**: 120000 milliseconds, since the default of 30 seconds can be too short.
3. Add **Send a message** with the formula `First(Topic.FoundryReply.choices).message.content`.

The combined digest loses the document links that the generative answers nodes attach.

## Why not one summary per file?

A topic can't list a folder's files or open a whole file by itself. That takes Microsoft Graph with an app
registration (as in the online Python version) or a Power Automate flow. If flows are allowed in your environment,
even ones without AI, a flow could list the files changed in the date range, and the topic could then ask one
generative answers question per file.
