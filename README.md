# Lark Base row processor (prototype)

Tick rows in a Lark Base grid, click a link in Lark, and the backend runs each ticked row through
an LLM and streams downloadable CSVs directly in the browser. The LLM is NanoGPT
(hardcoded to Hydrogen High: `google/gemini-4-hydrogen` with `reasoning_effort="high"`), prompted with `prompt.txt`. Set `LLM=stub` to use a stub instead
(a fixed CSV describing its inputs), which tests the Lark wiring without calling NanoGPT.

Lark Base access is read-only: nothing is written back to Lark and nothing is kept on disk or in
memory between requests, so it runs on serverless hosts such as Vercel where each request may land
on a different instance.

## How it works

1. A user ticks **Process Row?** on one or more rows in Lark.
2. The user clicks the link in Lark. It opens a confirmation page listing the ticked rows and checkboxes for the disposition prompt blocks to generate (this page changes nothing). You can also pre-select prompts via deep links like `/run?key=<RUN_KEY>&prompt=rpc_ptp,claiming_paid` (or `&prompt=all`).
3. Clicking **Run** processes the ticked rows inside that request, one at a time, and streams each row to the page as it finishes:
   - the row's attachments are downloaded from Lark and passed to the LLM with the composed prompt blocks from `prompts/`
   - selecting 1–3 blocks runs a single composed LLM call; selecting 4+ blocks including `rpc_ptp` automatically chains Call 2 (non-RPC dispositions) on top of Call 1's `RPC + PTP` CSV to prevent structural drift and output truncation
   - the backend deterministically slices Phoenix AI 3 outputs by `Rule Name` so each disposition gets its own **Download** link + **Preview / Copy CSV**, alongside a **Download All** combined CSV link
4. A run starts no new row once `RUN_BUDGET_SECONDS` (default 150) have passed.
5. The **Status of all rows** page (`/status`) retains the output table of processed rows (per disposition selection) and their CSV download links for the lifetime of the server instance.

## The grid (read-only)

Required columns:

| Column | Type |
| --- | --- |
| `Data Collection Date` | Date |
| `Sub-campaign (Doc Class 6)` | Single select |
| `QA Guidelines/PhoneMon (Bank)` | Attachment |
| `Process Row?` | Checkbox |

The names are constants at the top of `lark_base.py`. `Output CSV for NLU` is no longer used, and
Lark records are never modified by the app.

## One-time Lark setup

1. In the Lark developer console, enable a Base read scope (`bitable:app:readonly` or `bitable:app`,
   tenant token scopes) and release a version.
2. Ensure the app has read access to the Base (open the Base in a browser, click `…` at the top
   right, then **More → Add Document App**, search for the app and give it at least view/read
   rights). Without this step every call fails with `403 / 91403 Forbidden`, even with the scope
   released.
3. Copy `.env.example` to `.env` and fill in `LARK_APP_ID`, `LARK_APP_SECRET`, `BASE_APP_TOKEN`,
   `TABLE_ID`, `RUN_KEY`, `NANOGPT_API_KEY` (from NanoGPT). Generate the run key
   with `python -c "import secrets; print(secrets.token_urlsafe(24))"`.
4. Install dependencies: `pip install -r requirements-dev.txt`.

Check the setup (prints the row count, any missing columns, and the ticked rows):

```
python lark_base.py list
```

A `403 / 91403 Forbidden` error means step 2 is not finished (see Troubleshooting).

## Run it on your machine

Without the link (command line, writes CSVs to `output/`):

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
short by the app. A row takes one Lark download and one NanoGPT call (waits up to 300 seconds, and a
second call if the first reply is not a valid CSV). On a plan with a longer limit, raise
`maxDuration` in `vercel.json` and `RUN_BUDGET_SECONDS` together.

Edits to `prompt.txt` take effect after a redeploy, because the file is part of the deployment.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `403 / 91403 Forbidden` from `python lark_base.py list`, or the link page says "Cannot read the grid" | The app has no read access to the Base. Do setup step 2. Adding the scope alone does not fix it. |
| `python lark_base.py list` reports a missing column | Fix the column name constants at the top of `lark_base.py`. |
| Row ends `Failed` with `NanoGPT returned 429` | Rate limit or quota exceeded. The call was already retried twice. Wait and run the row again. |
| Row ends `Failed` with `no usable CSV` or `cut off` | The model's reply was not a CSV with a header and consistent columns, or it hit its output limit. Make `prompt.txt` more specific, or split the attachment. |
| The run page says ticked rows "were not reached" | The time budget ran out before they were reached. Untick finished rows in Lark and run again, or raise `RUN_BUDGET_SECONDS`. |
| Server exits at startup with `NANOGPT_API_KEY is not set` | Add the key to `.env` or the Vercel environment, or set `LLM=stub` to run without NanoGPT. |
| Server exits at startup with `RUN_KEY is required` | Set `RUN_KEY`. The link must never be open to everyone. |
| `uvicorn` starts but never listens, with an asyncio "is not the running loop" error | Seen once on Python 3.14 on Windows. Start it again. |
| Attachment download returns 403 | A Base with advanced permissions may require the `extra` parameter on download. |

## Limits of this prototype

- Lark Base access is strictly read-only; generated CSVs are downloaded from the run results page
  (or written to `output/` by the CLI) instead of being attached back to Lark, and `Process Row?`
  remains ticked after a run.
- A run handles as many ticked rows as fit in its time budget.
- Attachments are sent to NanoGPT (`google/gemini-4-hydrogen`). Use only non-confidential files if required by your data policy.
- A reply that is not a valid CSV (no data rows, or rows with a different column count than the
  header) is asked for once more; after the second try the row is `Failed`. A reply cut off at the
  output limit also fails the row.
- `prompt.txt` is read on every row. `{date}` and `{sub_campaign}` in it are replaced by the row's
  values; without them the model sees only the attachment and the prompt.
- Supported attachment types: PDF, Word, PowerPoint, Excel and OpenDocument files, TXT, MD, HTML,
  CSV, PNG, JPEG, WebP. Anything else fails the row with `unsupported attachment type`.
- CSVs are UTF-8 without a byte order mark, so Excel may show non-ASCII text wrongly when it opens
  the file directly.

## Tests

```
python -m pytest
```

## Files

| File | Purpose |
| --- | --- |
| `app.py` | Entrypoint: builds the app from the environment. Used by `uvicorn app:app` and by Vercel |
| `web.py` | The link: confirmation page for ticked rows, streamed run with inline CSV downloads |
| `pipeline.py` | Row model, `run_ticked_rows()` / `process_ticked_rows()`, and the command-line entry point |
| `llm.py` | LLM interface, the stub, and the NanoGPT implementation (`NanoGPTLLM`, chosen by `llm_from_env()`) |
| `lark_base.py` | Read-only Base client (tenant token, list records/fields, attachment download), settings, `.env` loading, column names |
| `vercel.json`, `.vercelignore`, `.python-version`, `requirements.txt` | Vercel deployment settings. `requirements-dev.txt` adds `uvicorn` and `pytest` for local work |
| `lark-prototype-research.md` | Earlier design research (superseded by this README) |
| `prompt.txt` | The custom prompt sent to NanoGPT with each row's files |
