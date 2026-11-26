"""Trigger service: a link in Lark opens a row-selection page, and Run generates downloadable CSVs.

    uvicorn app:app --port 8000          (app.py builds the app from the environment)

Configuration: everything lark_base.py and llm.py need, plus
    RUN_KEY               required, a shared secret that must be in every request URL (?key=...)
    RUN_BUDGET_SECONDS    optional, default 150. A run starts no new row after this many seconds,
                          so keep it well below the host's function time limit.

Pages (all need ?key=<RUN_KEY>):
    GET  /run      lists the rows from the read-only Lark grid with checkboxes to pick which to run.
                   Changes nothing, so link previews and reloads are harmless.
    POST /run      processes the selected rows inside this request and streams each row's result and
                   downloadable CSV as it finishes.

Nothing is written back to Lark or kept in memory between requests.
"""

from __future__ import annotations

import html
import os
import secrets
import threading
from typing import Iterator
from urllib.parse import parse_qs, quote

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from lark_base import BaseClient, ConfigError, LarkApiError, client_from_env, load_dotenv
from llm import LLM, llm_from_env
from pipeline import (
    STATUS_DONE,
    STATUS_FAILED,
    Row,
    RowResult,
    all_rows,
    run_rows,
)

DEFAULT_BUDGET_SECONDS = 150.0

STYLE = (
    "body{font-family:system-ui,sans-serif;max-width:56rem;margin:2rem auto;padding:0 1rem}"
    "table{border-collapse:collapse;width:100%;margin-bottom:1rem}"
    "td,th{border:1px solid #ccc;padding:.45rem;text-align:left;vertical-align:top}"
    "button{font-size:1rem;padding:.5rem 1rem}.failed{color:#b00020}.ok{color:#1b5e20}"
    "pre{max-height:16rem;overflow:auto;background:#f6f6f6;padding:.5rem;font-size:.8rem}"
    "details{margin-top:.35rem}details button{font-size:.8rem;padding:.2rem .5rem;margin-bottom:.3rem}"
)
FOOT = "</body></html>"


def _head(title: str) -> str:
    return (f'<!doctype html><html><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{html.escape(title)}</title><style>{STYLE}</style></head>"
            f"<body><h1>{html.escape(title)}</h1>")


def _page(title: str, body: str) -> str:
    return _head(title) + body + FOOT


def _status_cell(status: str) -> str:
    css = "failed" if status == STATUS_FAILED else "ok" if status == STATUS_DONE else ""
    return f'<td class="{css}">{html.escape(status)}</td>'


RESULTS_HEAD = "<table><tr><th>Row</th><th>Status</th><th>Detail</th><th>CSV output</th></tr>"


def _csv_cell(result: RowResult) -> str:
    if result.status != STATUS_DONE or not result.file_name or result.csv_text is None:
        return "<td></td>"
    file_name = html.escape(result.file_name)
    data_uri = f"data:text/csv;charset=utf-8,{quote(result.csv_text, safe='')}"
    preview = html.escape(result.csv_text)
    return (
        f'<td><a href="{data_uri}" download="{file_name}">Download {file_name}</a>'
        f"<details><summary>Preview</summary>"
        f'<button type="button" onclick="navigator.clipboard.writeText(this.nextElementSibling.textContent)">'
        f"Copy CSV</button><pre>{preview}</pre></details></td>"
    )


def _result_row(result: RowResult) -> str:
    return (f"<tr><td>{html.escape(result.record_id)}</td>{_status_cell(result.status)}"
            f"<td>{html.escape(result.detail)}</td>{_csv_cell(result)}</tr>")


def _rows_table(rows: list[Row]) -> str:
    items = []
    for row in rows:
        rid = html.escape(row.record_id)
        row_date = html.escape(row.date.isoformat() if row.date else "-")
        sub_campaign = html.escape(row.sub_campaign or "-")
        files = html.escape(", ".join(f"{a.name} ({a.size} bytes)" for a in row.attachments) or "no attachment")
        items.append(
            f'<tr><td><input type="checkbox" name="record_id" value="{rid}" '
            f'aria-label="Select {rid}"></td>'
            f"<td>{rid}</td><td>{row_date}</td><td>{sub_campaign}</td><td>{files}</td></tr>"
        )
    toggle = (
        '<input type="checkbox" aria-label="Select all" '
        "onclick=\"for(var c of this.form.querySelectorAll('input[name=record_id]'))c.checked=this.checked\">"
    )
    return (
        f"<table><tr><th>{toggle}</th><th>Row</th><th>Date</th><th>Sub-campaign</th>"
        f"<th>Attachments</th></tr>{''.join(items)}</table>"
    )


def _grid_error(exc: Exception) -> HTMLResponse:
    return HTMLResponse(_page("Cannot read the grid", f"<p>{html.escape(str(exc))}</p>"), status_code=502)


def create_app(client: BaseClient, llm: LLM, run_key: str,
               budget_seconds: float = DEFAULT_BUDGET_SECONDS) -> FastAPI:
    if not run_key:
        raise ConfigError("RUN_KEY is required: the trigger link must not be open to everyone.")
    # No /docs or /openapi.json: the service is reachable from the internet.
    app = FastAPI(title="Row processor", docs_url=None, redoc_url=None, openapi_url=None)
    # Stops two concurrent runs in the same process (e.g. a double click).
    busy = threading.Lock()

    def check_key(key: str) -> None:
        if not secrets.compare_digest(key.encode(), run_key.encode()):
            raise HTTPException(status_code=403, detail="Invalid or missing key.")

    def back_link(key: str) -> str:
        return f'<p><a href="/run?key={quote(key)}">Back to Run page</a></p>'

    def stream_run(key: str, selected_ids: list[str]) -> Iterator[str]:
        if not busy.acquire(blocking=False):
            yield _page("Already running", "<p>A run is already in progress.</p>" + back_link(key))
            return
        try:
            if not selected_ids:
                yield _page("No rows selected", "<p>No rows were selected.</p>" + back_link(key))
                return
            yield (_head("Processing selected rows")
                   + "<p>Keep this page open. Each row and its CSV download appear below when done.</p>"
                   + RESULTS_HEAD)
            results: list[RowResult] = []
            try:
                for result in run_rows(client, llm, selected_ids, budget_seconds):
                    results.append(result)
                    yield _result_row(result)
                yield "</table>"
            except Exception as exc:  # the run as a whole failed, e.g. the grid could not be read
                reason = html.escape(f"{type(exc).__name__}: {exc}")
                yield f'</table><p class="failed">Run failed: {reason}</p>'
            else:
                done_ids = {r.record_id for r in results}
                unreached = [rid for rid in selected_ids if rid not in done_ids]
                failed = sum(1 for r in results if r.status == STATUS_FAILED)
                yield f"<p>Done: {len(results) - failed} succeeded, {failed} failed.</p>"
                if unreached:
                    shown = html.escape(", ".join(unreached))
                    yield (f"<p>{len(unreached)} selected row(s) were not reached before the time "
                           f"budget ended ({shown}). Go back to the Run page to run them.</p>")
            yield back_link(key) + FOOT
        finally:
            busy.release()

    @app.get("/run", response_class=HTMLResponse)
    def confirm(key: str = "") -> HTMLResponse:
        check_key(key)
        try:
            rows = all_rows(client)
        except (LarkApiError, httpx.HTTPError) as exc:
            return _grid_error(exc)
        if not rows:
            body = "<p>No rows found in the grid.</p>"
        else:
            body = (
                "<p>Select the rows to transcode. Lark is read-only, so generated CSVs appear on "
                "the next page for download.</p>"
                f'<form method="post" action="/run?key={quote(key)}" '
                "onsubmit=\"var b=this.querySelector('button[type=submit]');b.disabled=true;b.textContent='Running...'\">"
                + _rows_table(rows)
                + '<p><button type="submit">Run selected rows</button></p></form>'
            )
        return HTMLResponse(_page("Select rows to transcode", body))

    @app.post("/run", response_model=None)
    async def start(request: Request, key: str = "") -> StreamingResponse:
        check_key(key)
        # Parse form body with stdlib so python-multipart is not required.
        raw_body = (await request.body()).decode("utf-8", errors="replace")
        params = parse_qs(raw_body)
        selected_ids = list(dict.fromkeys(params.get("record_id", [])))
        return StreamingResponse(stream_run(key, selected_ids), media_type="text/html")

    return app


def app_from_env() -> FastAPI:
    load_dotenv()
    raw = os.environ.get("RUN_BUDGET_SECONDS", "").strip()
    try:
        budget = float(raw) if raw else DEFAULT_BUDGET_SECONDS
    except ValueError:
        budget = 0.0
    if budget <= 0:
        raise ConfigError(f"RUN_BUDGET_SECONDS must be a positive number, got {raw!r}")
    return create_app(client_from_env(), llm_from_env(), os.environ.get("RUN_KEY", ""), budget)
