# Lark Base row processor (prototype)

Tick rows in a Lark Base grid, click a link in Lark, and the backend runs each ticked row through an LLM and
saves one CSV per row in `output/`. The LLM is Gemini (`gemini-4-hydrogen` by default), prompted with `prompt.txt`.
Set `LLM=stub` in `.env` to use a stub instead (a fixed CSV describing its inputs), which tests the Lark wiring
without calling Gemini.

## How it works

1. A user ticks **Process Row?** on one or more rows.
2. The user clicks the link in Lark. It opens a confirmation page listing the ticked rows (this page changes nothing).
3. Clicking **Run** starts the job in the background:
   - each ticked row becomes `Status = Queued` and its box is unticked, so a second run cannot pick it up again
   - the row's attachments are downloaded and passed to the LLM
   - the CSV is saved as `output/<date>_<record_id>.csv`
   - the row becomes `Integrated`, or `Failed` if anything went wrong
4. The status page shows the result of every row.

To process a row again, tick its box again. Its CSV file is overwritten. To retry a `Failed` row, set Status back
to `Not Processed` (optional, the run does not check it) and tick the box.

## The grid

Existing columns: `Data Collection Date` (date), `Sub-campaign (Doc Class 6)` (single select),
`QA Guidelines/PhoneMon (Bank)` (attachment). Add two columns:

| Column | Type | Options |
| --- | --- | --- |
| `Process Row?` | Checkbox | |
| `Status` | Single select | `Not Processed`, `Queued`, `Integrated`, `Failed` |

The names are constants at the top of `lark_base.py`.

## One-time Lark setup

1. In the Lark developer console, enable the **`bitable:app`** scope for the app (tenant token scopes, and user token
   scopes if you will use `LARK_AUTH=user`) and release a version.
2. Add the app to the Base itself. This is separate from the scope: the scope lets the app call the API, and this
   step lets it touch this one Base. Open the Base in a browser, click `…` at the top right, then
   **More → Add Document App**, search for the app and give it edit rights. If the Base has advanced permissions
   turned on, give it manage rights. The app only shows up in the search once it has a Base or docs scope enabled.
   Without this step every call fails with `403 / 91403 Forbidden`, even with the scope released.
   If you cannot do this yet, set `LARK_AUTH=user` in `.env` and run `python lark_oauth.py login`.
   The backend then acts as the person who logged in. Use this for a single-person demo only: refresh tokens
   are single use (run one process at a time), and `python lark_oauth.py status` shows when the login's refresh
   token expires (about 7 days when I checked).
3. Copy `.env.example` to `.env` and fill in `LARK_APP_ID`, `LARK_APP_SECRET`, `BASE_APP_TOKEN`, `TABLE_ID`, `RUN_KEY`,
   `GEMINI_API_KEY` (a free key from Google AI Studio). Generate the run key with
   `python -c "import secrets; print(secrets.token_urlsafe(24))"`. `GEMINI_MODEL` is optional.
4. Install dependencies: `pip install -r requirements.txt`.

Check the setup (prints the row count, any missing columns, and the ticked rows):

```
python lark_base.py list
```

A `403 / 91403 Forbidden` error means step 2 is not finished (see Troubleshooting).

## Run it

Without the link (command line):

```
python pipeline.py --dry-run     # list what would be processed, change nothing
python pipeline.py --run         # process the ticked rows
```

With the link, run the server and the tunnel each in **its own terminal window**:

```
uvicorn server:app_from_env --factory --port 8000
```

Open `http://localhost:8000/run?key=<RUN_KEY>` to try it on your own machine.

### Make the link work for other people

Teammates cannot reach `localhost`, so expose the port through a tunnel. Pick one:

- **ngrok** (needs a free account and authtoken): `ngrok http 8000`
- **Cloudflare quick tunnel** (no account): `cloudflared tunnel --url http://localhost:8000`

ngrok on Windows:

1. Install it: `winget install --id Ngrok.Ngrok`. The packaged version was old (3.3.1), so run `ngrok update` once.
2. Add your authtoken yourself, from https://dashboard.ngrok.com/get-started/your-authtoken. Never paste it into
   chat or commit it: `ngrok config add-authtoken <TOKEN>`
3. In a new terminal: `ngrok http 8000`. It prints the public address and serves an inspector at http://localhost:4040.
4. Stop the tunnel or the server with Ctrl+C in its window. Closing the window takes the link down.

Both tunnels print a public `https://...` address. In the wiki page next to the Base, add a hyperlink to
`https://<public-address>/run?key=<RUN_KEY>`.

Notes:

- ngrok's free plan gives your account one stable dev domain that is reused each time you start the tunnel, so the
  link in Lark keeps working across restarts. Cloudflare quick tunnels get a new address every time.
- On ngrok's free plan, a browser's first visit shows an ngrok warning page ("You are about to visit...",
  `ERR_NGROK_6024`) with a button to continue. Tell teammates to expect it. It does not appear for API clients.
- Anyone who has the link can start a run. The key is the only protection, so share the link only in the team.
- The key is part of the URL, so it shows up in the server log and in the tunnel's request inspector.
- Run the server and the tunnel on a machine that stays on while people use the link.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `403 / 91403 Forbidden` from `python lark_base.py list`, or the link page says "Cannot read the grid" | The app is not a member of the Base. Do setup step 2. Adding the scope alone does not fix it. A user login can still read the Base, which is how to tell this apart from a wrong ID or column name. |
| Link opens an ngrok page ("You are about to visit...", `ERR_NGROK_6024`) | Normal on ngrok's free plan for a browser's first visit. Click the button to continue. |
| ngrok page with `ERR_NGROK_8012` | The tunnel is up but nothing is listening on port 8000. Start the server. |
| Row ends `Failed` with `Gemini returned 429` | Free-tier rate limit or daily quota. The call was already retried twice. Wait, tick the row again, or set `GEMINI_MODEL` to another model your key can use. |
| Row ends `Failed` with `no usable CSV` or `cut off` | The model's reply was not a CSV with a header and consistent columns, or it hit its output limit. Make `prompt.txt` more specific, or split the attachment. |
| Server exits at startup with `GEMINI_API_KEY is not set` | Add the key to `.env`, or set `LLM=stub` to run without Gemini. |
| Server exits at startup with `RUN_KEY is required` | Set `RUN_KEY` in `.env`. The link must never be open to everyone. |
| `uvicorn` starts but never listens, with an asyncio "is not the running loop" error | Seen once on Python 3.14 on Windows. Start it again. |
| Server or ngrok vanishes when a chat or agent session closes | Processes started from that session end with it. Start them from your own terminal windows. |
| Attachment download returns 403 | A Base with advanced permissions needs the app to have manage rights, and the download call may also need the `extra` parameter (not implemented, not needed so far). |

## Status

Phase 1 (stub LLM) was tested end to end on 2026-11-19 against the real Base as the app (tenant token), through the
public ngrok link: tick a row, open the link, click Run, then the row becomes `Integrated` and unticked, the
attachment is downloaded, and one CSV is saved in `output/`.

Phase 2 (Gemini) was tested the same day with `python pipeline.py --run` on the test row (`Grading_Matrix.xlsx`,
about 7 KB): the row became `Integrated` and the CSV held the sheet's content, with 18 of its 22 text cells verbatim.
The other 4 were section labels written without their trailing colon, and the sheet's title cell was left out.
The Gemini call has not yet been run through the public link, and only one small `.xlsx` has been tried.

Not yet confirmed:

- A run started from a real browser by someone other than the developer. Tests used scripted requests.
- That the date column's UTC+8 assumption holds for every row (`LARK_UTC_OFFSET_HOURS`). One date matched.
- Larger or multi-file rows, PDFs, Word files and images. Files are sent inline in the request; there is no Files
  API upload, so a very large attachment may be rejected by Gemini (the row then shows `Failed` with its message).
- Gemini's free-tier rate limits for `gemini-4-hydrogen` (the limits page did not load). A `429` is retried twice
  (after 5 s and 20 s) before the row fails. Grading is out of scope.

## Limits of this prototype

- CSV files stay on the machine that runs the server. People who click the link see the status page, not the files.
- One run at a time. A second click while a run is active is refused.
- Attachments are sent to Gemini on the free tier, which may use content to improve Google products. Use only
  non-confidential files for the demo.
- A reply that is not a valid CSV (no data rows, or rows with a different column count than the header) is asked
  for once more; after the second try the row is `Failed`. A reply cut off at the output limit also fails the row.
- `prompt.txt` is read on every row, so edits apply without a restart. `{date}` and `{sub_campaign}` in it are
  replaced by the row's values; without them the model sees only the attachment and the prompt.
- Supported attachment types: PDF, Word, PowerPoint, Excel and OpenDocument files, TXT, MD, HTML, CSV, PNG, JPEG,
  WebP. Anything else fails the row with `unsupported attachment type`.

## Tests

```
python -m pytest
```

## Files

| File | Purpose |
| --- | --- |
| `lark_base.py` | Base client (tenant token, list/update records, attachment download), settings, column names |
| `pipeline.py` | Row model and `process_ticked_rows()`, plus the command-line entry point |
| `llm.py` | LLM interface, the stub, and the Gemini implementation (`GeminiLLM`, chosen by `llm_from_env()`) |
| `server.py` | The link, confirmation page, run trigger and status page |
| `lark_oauth.py`, `lark_doc_client.py` | Earlier user-token login and Docx prototype. `lark_oauth.py` provides `load_dotenv` and the user-token fallback; its default scope list now has `bitable:app`. `lark_doc_client.py` is unchanged |
| `lark-prototype-research.md` | Earlier design research. Its recommended Base Button option was not available in this tenant, so it is superseded by this README |
| `prompt.txt` | The custom prompt sent to Gemini with each row's files |
