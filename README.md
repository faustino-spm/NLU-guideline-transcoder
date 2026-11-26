# Lark Base row processor (prototype)

Tick rows in a Lark Base grid, click a link in Lark, and the backend runs each ticked row through an LLM and
attaches one CSV per row in the grid's `Output CSV for NLU` column. The LLM is Gemini (`gemini-4-hydrogen` by
default), prompted with `prompt.txt`. Set `LLM=stub` to use a stub instead (a fixed CSV describing its inputs),
which tests the Lark wiring without calling Gemini.

The app keeps nothing on disk or in memory between requests. The grid's `Status` and `Output CSV for NLU`
columns are the only state, so it runs on a host such as Vercel where each request may land on a different instance.

## How it works

1. A user ticks **Process Row?** on one or more rows.
2. The user clicks the link in Lark. It opens a confirmation page listing the ticked rows (this page changes nothing).
3. Clicking **Run** processes the rows inside that request, one at a time. The page shows each row as it finishes:
   - the grid is read, and the first ticked row becomes `Status = Queued` with its box unticked, so a second
     run cannot pick it up again
   - the row's attachments are downloaded and passed to the LLM
   - the CSV is uploaded as `<date>_<record_id>.csv` and attached in `Output CSV for NLU`
   - the row becomes `Integrated`, or `Failed` if anything went wrong, then the next ticked row starts
4. A run starts no new row once `RUN_BUDGET_SECONDS` (default 150) have passed. Rows it did not reach stay
   ticked, and the page says how many. Click **Run** again to continue.
5. The status page lists every processed row with its status and CSV, read from the grid.

To process a row again, tick its box again. The new CSV replaces the one in the row. If the re-run fails, the old
CSV stays in the row and the Status says `Failed`.

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

1. In the Lark developer console, enable the **`bitable:app`** scope for the app (tenant token scopes) and release a
   version. It also covers uploading the CSV; no other scope was needed.
2. Add the app to the Base itself. This is separate from the scope: the scope lets the app call the API, and this
   step lets it touch this one Base. Open the Base in a browser, click `…` at the top right, then
   **More → Add Document App**, search for the app and give it edit rights. If the Base has advanced permissions
   turned on, give it manage rights. The app only shows up in the search once it has a Base or docs scope enabled.
   Without this step every call fails with `403 / 91403 Forbidden`, even with the scope released.
3. Copy `.env.example` to `.env` and fill in `LARK_APP_ID`, `LARK_APP_SECRET`, `BASE_APP_TOKEN`, `TABLE_ID`, `RUN_KEY`,
   `GEMINI_API_KEY` (a free key from Google AI Studio). Generate the run key with
   `python -c "import secrets; print(secrets.token_urlsafe(24))"`. `GEMINI_MODEL` is optional.
4. Install dependencies: `pip install -r requirements-dev.txt`.

Check the setup (prints the row count, any missing columns, and the ticked rows):

```
python lark_base.py list
```

A `403 / 91403 Forbidden` error means step 2 is not finished (see Troubleshooting).

`BASE_APP_TOKEN` can be the token in the Base's URL even when the Base is opened from a wiki page. For uploads the
app looks up the Base's own token from that wiki node, so no extra setting is needed.

## Run it on your machine

Without the link (command line):

```
python pipeline.py --dry-run     # list what would be processed, change nothing
python pipeline.py --run         # process the ticked rows
```

With the link:

```
uvicorn app:app --port 8000
```

Open `http://localhost:8000/run?key=<RUN_KEY>`. `app.py` builds the app from `.env` when it is imported.

## Deploy to Vercel

Not yet deployed or tested on Vercel. The configuration follows Vercel's documentation for Python and FastAPI.

1. Create a Vercel project from this folder (Git import, or `vercel deploy`). Vercel finds `app.py` and the
   `app` in it, and installs `requirements.txt`. `requirements-dev.txt`, `tests/` and `.env` are left out by
   `.vercelignore`.
2. In the project's Environment Variables, set `LARK_APP_ID`, `LARK_APP_SECRET`, `BASE_APP_TOKEN`, `TABLE_ID`,
   `RUN_KEY`, `GEMINI_API_KEY`. Optional: `GEMINI_MODEL`, `RUN_BUDGET_SECONDS`, `LARK_UTC_OFFSET_HOURS`.
3. Use the production domain for the link in Lark: `https://<your-project>.vercel.app/run?key=<RUN_KEY>`. Check that
   it opens in a private window without a Vercel login. Vercel's Deployment Protection settings can put a login in
   front of a deployment.

Time limit: `vercel.json` sets the function's `maxDuration` to 300 seconds, the maximum on the Hobby plan.
`RUN_BUDGET_SECONDS` must stay well below it, because a row that has started is never cut short by the app.
A row takes one Lark download, one Gemini call (waits up to 300 seconds, and a second call if the first reply is not
a valid CSV) and one upload. If Vercel ends the request while a row is running, that row stays `Queued`: tick it
again. On a plan with a longer limit, raise `maxDuration` in `vercel.json` and `RUN_BUDGET_SECONDS` together.

Edits to `prompt.txt` take effect after a redeploy, because the file is part of the deployment.

## Local tunnel (only to test the link from other machines)

Not needed on Vercel. Teammates cannot reach `localhost`, so expose the port through a tunnel. Pick one:

- **ngrok** (needs a free account and authtoken): `ngrok http 8000`
- **Cloudflare quick tunnel** (no account): `cloudflared tunnel --url http://localhost:8000`

ngrok on Windows:

1. Install it: `winget install --id Ngrok.Ngrok`. The packaged version was old (3.3.1), so run `ngrok update` once.
2. Add your authtoken yourself, from https://dashboard.ngrok.com/get-started/your-authtoken. Never paste it into
   chat or commit it: `ngrok config add-authtoken <TOKEN>`
3. In a new terminal: `ngrok http 8000`. It prints the public address and serves an inspector at http://localhost:4040.
4. Stop the tunnel or the server with Ctrl+C in its window. Closing the window takes the link down.

Run the server and the tunnel each in its own terminal window. Notes:

- ngrok's free plan gives your account one stable dev domain that is reused each time you start the tunnel.
  Cloudflare quick tunnels get a new address every time.
- On ngrok's free plan, a browser's first visit shows an ngrok warning page (`ERR_NGROK_6024`) with a button to
  continue.
- The key is part of the URL, so it shows up in the server log and in the tunnel's request inspector.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `403 / 91403 Forbidden` from `python lark_base.py list`, or the link page says "Cannot read the grid" | The app is not a member of the Base. Do setup step 2. Adding the scope alone does not fix it. A user login can still read the Base, which is how to tell this apart from a wrong ID or column name. |
| `python lark_base.py list` reports a missing column | Add it to the grid with the exact name from "The grid", or change the constant in `lark_base.py`. |
| Row ends `Failed` with `Lark API error 1061044 ... parent node not exist` | The upload target is wrong. For a Base opened from a wiki, the app must be able to read the wiki node to find the Base's own token. Check that the app can open the wiki page the Base sits in. |
| Row ends `Failed` with `Gemini returned 429` | Free-tier rate limit or daily quota. The call was already retried twice. Wait, tick the row again, or set `GEMINI_MODEL` to another model your key can use. |
| Row ends `Failed` with `no usable CSV` or `cut off` | The model's reply was not a CSV with a header and consistent columns, or it hit its output limit. Make `prompt.txt` more specific, or split the attachment. |
| Row stays `Queued` and unticked | The run was cut off (the host's time limit, or the process stopped) while this row was running. Tick it again. |
| The run page says rows are "still ticked" | The time budget ran out before they were reached, or they could not be claimed. Click Run again. |
| Server exits at startup with `GEMINI_API_KEY is not set` | Add the key to `.env` or the Vercel environment, or set `LLM=stub` to run without Gemini. |
| Server exits at startup with `RUN_KEY is required` | Set `RUN_KEY`. The link must never be open to everyone. |
| `uvicorn` starts but never listens, with an asyncio "is not the running loop" error | Seen once on Python 3.14 on Windows. Start it again. |
| Server or ngrok vanishes when a chat or agent session closes | Processes started from that session end with it. Start them from your own terminal windows. |
| Attachment download returns 403 | A Base with advanced permissions needs the app to have manage rights, and the download call may also need the `extra` parameter (not implemented, not needed so far). |

## Status

Tested on 2026-11-20 against the real Base as the app (tenant token), with real Gemini, by driving `app.py`'s app
in-process: tick the test row, open the run page, click Run, then the row became `Integrated` and unticked, and a
1,129-byte CSV was attached in `Output CSV for NLU`, replacing an earlier test upload. The status page listed the
row. The CSV could be downloaded again with the same bytes.

Earlier, on 2026-11-19, the same flow was tested through a public ngrok link (stub LLM), and Gemini was tested
on the test row (`Grading_Matrix.xlsx`, about 7 KB): 18 of its 22 text cells appeared verbatim in the CSV. The other 4
were section labels written without their trailing colon, and the sheet's title cell was left out.

Not yet confirmed:

- A deployment on Vercel, and a run from a real browser by someone other than the developer.
- How long a real run takes, so the right `RUN_BUDGET_SECONDS` is a guess (150).
- That the date column's UTC+8 assumption holds for every row (`LARK_UTC_OFFSET_HOURS`). One date matched.
- Larger or multi-file rows, PDFs, Word files and images. Files are sent inline in the request; there is no Files
  API upload, so a very large attachment may be rejected by Gemini (the row then shows `Failed` with its message).
- Gemini's free-tier rate limits for `gemini-4-hydrogen` (the limits page did not load). A `429` is retried twice
  (after 5 s and 20 s) before the row fails. Grading is out of scope.

## Limits of this prototype

- A run handles as many rows as fit in its time budget; click Run again for the rest.
- Two runs started at the same moment on different server instances can pick the same row. The claim step makes
  this unlikely, not impossible. Two runs in one process are refused.
- The reason a row failed is shown on the page of the run that processed it (and in the host's logs), not in the grid.
- Attachments are sent to Gemini on the free tier, which may use content to improve Google products. Use only
  non-confidential files for the demo.
- A reply that is not a valid CSV (no data rows, or rows with a different column count than the header) is asked
  for once more; after the second try the row is `Failed`. A reply cut off at the output limit also fails the row.
- `prompt.txt` is read on every row. `{date}` and `{sub_campaign}` in it are replaced by the row's values; without
  them the model sees only the attachment and the prompt.
- Supported attachment types: PDF, Word, PowerPoint, Excel and OpenDocument files, TXT, MD, HTML, CSV, PNG, JPEG,
  WebP. Anything else fails the row with `unsupported attachment type`.
- The CSV is uploaded as UTF-8 without a byte order mark, so Excel may show non-ASCII text wrongly when it opens the
  file directly.

## Tests

```
python -m pytest
```

## Files

| File | Purpose |
| --- | --- |
| `app.py` | Entrypoint: builds the app from the environment. Used by `uvicorn app:app` and by Vercel |
| `web.py` | The link: confirmation page, streamed run, status page |
| `pipeline.py` | Row model, `run_ticked_rows()` / `process_ticked_rows()`, and the command-line entry point |
| `llm.py` | LLM interface, the stub, and the Gemini implementation (`GeminiLLM`, chosen by `llm_from_env()`) |
| `lark_base.py` | Base client (tenant token, list/update records, attachment download and upload), settings, `.env` loading, column names |
| `vercel.json`, `.vercelignore`, `.python-version`, `requirements.txt` | Vercel deployment settings. `requirements-dev.txt` adds `uvicorn` and `pytest` for local work |
| `lark-prototype-research.md` | Earlier design research. It mentions `lark_doc_client.py` and `lark_oauth.py` (the early user-token prototype), which have been removed. Its recommended Base Button option was not available in this tenant, so it is superseded by this README |
| `prompt.txt` | The custom prompt sent to Gemini with each row's files |
