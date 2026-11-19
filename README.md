# Lark Base row processor (prototype)

Tick rows in a Lark Base grid, click a link in Lark, and the backend runs each ticked row through an LLM and
saves one CSV per row in `output/`. Phase 1 uses a **stub** LLM (a fixed CSV describing its inputs), so the
Lark wiring can be proven first. The real Gemini call is Phase 2.

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
3. Copy `.env.example` to `.env` and fill in `LARK_APP_ID`, `LARK_APP_SECRET`, `BASE_APP_TOKEN`, `TABLE_ID`, `RUN_KEY`.
   Generate a key with `python -c "import secrets; print(secrets.token_urlsafe(24))"`.
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
| Server exits at startup with `RUN_KEY is required` | Set `RUN_KEY` in `.env`. The link must never be open to everyone. |
| `uvicorn` starts but never listens, with an asyncio "is not the running loop" error | Seen once on Python 3.14 on Windows. Start it again. |
| Server or ngrok vanishes when a chat or agent session closes | Processes started from that session end with it. Start them from your own terminal windows. |
| Attachment download returns 403 | A Base with advanced permissions needs the app to have manage rights, and the download call may also need the `extra` parameter (not implemented, not needed so far). |

## Status

Phase 1 (stub LLM) was tested end to end on 2026-11-19 against the real Base as the app (tenant token), through the
public ngrok link: tick a row, open the link, click Run, then the row becomes `Integrated` and unticked, the
attachment is downloaded, and one CSV is saved in `output/`.

Not yet confirmed:

- A run started from a real browser by someone other than the developer. Tests used scripted requests.
- That the date column's UTC+8 assumption holds for every row (`LARK_UTC_OFFSET_HOURS`). One date matched.
- Phase 2 does not exist yet. It needs the real prompt in `prompt.txt` and a `GEMINI_API_KEY`, and it adds a
  Gemini implementation of the interface in `llm.py` (CSV validation, one retry, then `Failed`). Grading is out of scope.

## Limits of this prototype

- CSV files stay on the machine that runs the server. People who click the link see the status page, not the files.
- One run at a time. A second click while a run is active is refused.
- Phase 1 never calls a real LLM. Phase 2 will send attachments to Gemini; the free tier may use content to improve
  Google products, so use only non-confidential files for the demo.

## Tests

```
python -m pytest
```

## Files

| File | Purpose |
| --- | --- |
| `lark_base.py` | Base client (tenant token, list/update records, attachment download), settings, column names |
| `pipeline.py` | Row model and `process_ticked_rows()`, plus the command-line entry point |
| `llm.py` | LLM interface and the stub |
| `server.py` | The link, confirmation page, run trigger and status page |
| `lark_oauth.py`, `lark_doc_client.py` | Earlier user-token login and Docx prototype. `lark_oauth.py` provides `load_dotenv` and the user-token fallback; its default scope list now has `bitable:app`. `lark_doc_client.py` is unchanged |
| `lark-prototype-research.md` | Earlier design research. Its recommended Base Button option was not available in this tenant, so it is superseded by this README |
| `prompt.txt` | The custom prompt for Phase 2 (not used by the stub) |
