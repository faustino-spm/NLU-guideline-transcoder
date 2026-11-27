# Lark Base row processor (prototype)

Tick rows in a Lark Base grid, click a link in Lark, and the backend runs each ticked row through
an LLM and attaches the generated CSV(s) in the grid's `Output CSV for NLU` column (as well as
streaming instant download links in the browser). Supported providers are configured in `llm.py` via
`LLM_PROVIDER`: `"nanogpt"` (default, OpenAI-compatible endpoint using `google/gemini-4-hydrogen`
with `reasoning_effort="high"`), `"openai"` (any OpenAI-compatible `/chat/completions` endpoint),
`"gemini"` (Google Gemini native `generateContent` API using `GEMINI_API_KEY`), and `"stub"` (a
fixed CSV describing its inputs, for testing Lark wiring without calling an API). To switch
providers, change `LLM_PROVIDER` in `llm.py` (or set the `LLM` env var) and redeploy.

The app keeps nothing on disk or in memory between requests. The grid's `Status` and
`Output CSV for NLU` columns are the only state, so it runs on serverless hosts such as Vercel where
each request may land on a different instance.

## How it works

1. A user ticks **Process Row?** on one or more rows in Lark.
2. The user clicks the link in Lark. It opens a confirmation page listing the ticked rows and checkboxes for the disposition prompt blocks to generate (this page changes nothing). You can also pre-select prompts via deep links like `/run?key=<RUN_KEY>&prompt=rpc_ptp,claiming_paid` (or `&prompt=all`).
3. Clicking **Run** processes the ticked rows inside that request, one at a time, and streams each row to the page as it finishes:
   - the grid is read, and the first ticked row becomes `Status = Queued` with its box unticked, so a second run cannot pick it up again
   - the row's attachments are downloaded from Lark and passed to the LLM with the composed prompt blocks from `prompts/`
   - selecting 1–3 blocks runs a single composed LLM call; selecting 4+ blocks including `rpc_ptp` automatically chains Call 2 (non-RPC dispositions) on top of Call 1's `RPC + PTP` CSV to prevent structural drift and output truncation
   - the backend deterministically slices Phoenix AI 3 outputs by `Rule Name` so each disposition gets its own CSV (`<date>_<record_id>_<rule_name>.csv`), uploaded and attached in `Output CSV for NLU` (alongside instant **Download** / **Preview / Copy CSV** links on the page, and a **Download All** combined CSV link)
   - the row becomes `Integrated`, or `Failed` if anything went wrong, then the next ticked row starts
4. A run starts no new row once `RUN_BUDGET_SECONDS` (default 150) have passed. Rows it did not reach stay ticked, and the page says how many. Click **Run** again to continue.
5. The **Status of all rows** page (`/status`) lists every processed row with its status and attached CSV(s), read from the grid.

To process a row again, tick its box again. The new CSV(s) replace the ones in the row. If the re-run
fails, the old CSV(s) stay in the row and the Status says `Failed`.

## The grid

Existing columns: `Data Collection Date` (date), `Sub-campaign (Doc Class 6)` (single select),
`QA Guidelines/PhoneMon (Bank)` (attachment). Add three columns:

| Column | Type | Options |
| --- | --- | --- |
| `Process Row?` | Checkbox | |
| `Status` | Single select | `Not Processed`, `Queued`, `Integrated`, `Failed` |
| `Output CSV for NLU` | Attachment | |

The names are constants at the top of `lark_base.py`.

## One-time Lark setup

1. In the Lark developer console, enable the **`bitable:app`** scope for the app (tenant token scopes)
   and release a version. It also covers uploading the CSV; no other scope was needed.
2. Add the app to the Base itself. This is separate from the scope: the scope lets the app call the
   API, and this step lets it touch this one Base. Open the Base in a browser, click `…` at the top
   right, then **More → Add Document App**, search for the app and give it edit rights. If the Base
   has advanced permissions turned on, give it manage rights. The app only shows up in the search
   once it has a Base or docs scope enabled. Without this step every call fails with
   `403 / 91403 Forbidden`, even with the scope released.
3. Copy `.env.example` to `.env` and fill in `LARK_APP_ID`, `LARK_APP_SECRET`, `BASE_APP_TOKEN`,
   `TABLE_ID`, `RUN_KEY`, `NANOGPT_API_KEY` (from NanoGPT). Generate the run key
   with `python -c "import secrets; print(secrets.token_urlsafe(24))"`.
4. Install dependencies: `pip install -r requirements-dev.txt`.

Check the setup (prints the row count, any missing columns, and the ticked rows):

```
python lark_base.py list
```

A `403 / 91403 Forbidden` error means step 2 is not finished (see Troubleshooting).

`BASE_APP_TOKEN` can be the token in the Base's URL even when the Base is opened from a wiki page.
For uploads the app looks up the Base's own token from that wiki node, so no extra setting is needed.

## Run it on your machine

Without the link (command line, attaches CSVs in Lark; pass `--output-dir output` to also save locally):

```
python pipeline.py --list-prompts                    # list available disposition prompt blocks
python pipeline.py --dry-run                         # list what would be processed, change nothing
python pipeline.py --run                             # process ticked rows with default rpc_ptp prompt
python pipeline.py --run --prompt rpc_ptp,claiming_paid
python pipeline.py --run --prompt all                # chained Call 1 (RPC+PTP) -> Call 2 (non-RPC)
```

With the link:

```
uvicorn app:app --port 8000
```

Open `http://localhost:8000/run?key=<RUN_KEY>`. `app.py` builds the app from `.env` when it is
imported.

## Deploy to Vercel

1. Create a Vercel project from this folder (Git import, or `vercel deploy`). Vercel finds `app.py`
   and the `app` in it, and installs `requirements.txt`. `requirements-dev.txt`, `tests/` and `.env`
   are left out by `.vercelignore`.
2. In the project's Environment Variables, set `LARK_APP_ID`, `LARK_APP_SECRET`, `BASE_APP_TOKEN`,
   `TABLE_ID`, `RUN_KEY`, `NANOGPT_API_KEY`. Optional: `RUN_BUDGET_SECONDS`,
   `LARK_UTC_OFFSET_HOURS`.
3. Use the production domain for the link in Lark: `https://<your-project>.vercel.app/run?key=<RUN_KEY>`.
   Check that it opens in a private window without a Vercel login. Vercel's Deployment Protection
   settings can put a login in front of a deployment.

Time limit: `vercel.json` sets the function's `maxDuration` to 300 seconds, the maximum on the
Hobby plan. `RUN_BUDGET_SECONDS` must stay well below it, because a row that has started is never cut
short by the app. A row takes one Lark download, one (or two chained) LLM call(s) (each waits up to
300 seconds, and a retry if the reply is not a valid CSV), and upload(s) to Lark. If Vercel ends the
request while a row is running, that row stays `Queued`: tick it again. On a plan with a longer
limit, raise `maxDuration` in `vercel.json` and `RUN_BUDGET_SECONDS` together.

Edits to `prompt.txt` or `prompts/` take effect after a redeploy, because the files are part of the deployment.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `403 / 91403 Forbidden` from `python lark_base.py list`, or the link page says "Cannot read the grid" | The app is not a member of the Base. Do setup step 2. Adding the scope alone does not fix it. |
| `python lark_base.py list` reports a missing column | Add it to the grid with the exact name from "The grid", or fix the constant in `lark_base.py`. |
| Row ends `Failed` with `Lark API error 1061044 ... parent node not exist` | The upload target is wrong. For a Base opened from a wiki, the app must be able to read the wiki node to find the Base's own token. Check that the app can open the wiki page the Base sits in. |
| Row ends `Failed` with `NanoGPT returned 429` | Rate limit or quota exceeded. The call was already retried twice. Wait, tick the row again, or switch `LLM_PROVIDER`. |
| Row ends `Failed` with `no usable CSV` or `cut off` | The model's reply was not a CSV with a header and consistent columns, or it hit its output limit. Make the prompt more specific, or split the attachment. |
| Row stays `Queued` and unticked | The run was cut off (the host's time limit, or the process stopped) while this row was running. Tick it again. |
| The run page says rows are "still ticked" | The time budget ran out before they were reached, or they could not be claimed. Click Run again. |
| Server exits at startup with `NANOGPT_API_KEY is not set` | Add the key to `.env` or the Vercel environment, or set `LLM=stub` to run without NanoGPT. |
| Server exits at startup with `RUN_KEY is required` | Set `RUN_KEY`. The link must never be open to everyone. |
| `uvicorn` starts but never listens, with an asyncio "is not the running loop" error | Seen once on Python 3.14 on Windows. Start it again. |
| Attachment download returns 403 | A Base with advanced permissions needs the app to have manage rights, and the download call may also need the `extra` parameter. |

## Limits of this prototype

- A run handles as many rows as fit in its time budget; click Run again for the rest.
- Two runs started at the same moment on different server instances can pick the same row. The claim
  step makes this unlikely, not impossible. Two runs in one process are refused.
- The reason a row failed is shown on the page of the run that processed it (and in the host's logs), not in the grid.
- Attachments are sent to the configured LLM provider (NanoGPT `google/gemini-4-hydrogen` by default). Use only non-confidential files if required by your data policy.
- A reply that is not a valid CSV (no data rows, or rows with a different column count than the
  header) is asked for once more; after the second try the row is `Failed`. A reply cut off at the
  output limit also fails the row.
- Prompt templates in `prompts/` (and legacy `prompt.txt`) substitute `{date}` and `{sub_campaign}` with the row's values.
- Supported attachment types: PDF, Word, PowerPoint, Excel and OpenDocument files, TXT, MD, HTML,
  CSV, PNG, JPEG, WebP. Anything else fails the row with `unsupported attachment type`.
- The CSV is uploaded as UTF-8 without a byte order mark, so Excel may show non-ASCII text wrongly when it opens the file directly.

## Tests

```
python -m pytest
```

## Files

| File | Purpose |
| --- | --- |
| `app.py` | Entrypoint: builds the app from the environment. Used by `uvicorn app:app` and by Vercel |
| `web.py` | The link: confirmation page, streamed run with inline CSV downloads, status page |
| `pipeline.py` | Row model, `run_ticked_rows()` / `process_ticked_rows()`, and the command-line entry point |
| `prompts.py`, `prompts/` | Modular disposition prompt registry, prompt composition, and Stage 2 chaining |
| `llm.py` | LLM interface, provider switch (`LLM_PROVIDER`), stub, OpenAI-compatible client (`OpenAICompatibleLLM` / `NanoGPTLLM`), and Google Gemini client (`GeminiLLM`) |
| `lark_base.py` | Base client (tenant token, list/update records, attachment download and upload), settings, `.env` loading, column names |
| `vercel.json`, `.vercelignore`, `.python-version`, `requirements.txt` | Vercel deployment settings. `requirements-dev.txt` adds `uvicorn` and `pytest` for local work |
| `lark-prototype-research.md` | Earlier design research (superseded by this README) |
| `prompt.txt` | Legacy single-call prompt reference |
