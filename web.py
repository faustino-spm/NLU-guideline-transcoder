"""Trigger service: a link in Lark opens a confirmation page, and its Run button starts a run.

    uvicorn app:app --port 8000          (app.py builds the app from the environment)

Configuration: everything lark_base.py and llm.py need, plus
    RUN_KEY               required, a shared secret that must be in every request URL (?key=...)
    RUN_BUDGET_SECONDS    optional, default 150. A run starts no new row after this many seconds,
                          so keep it well below the host's function time limit.

Pages (all need ?key=<RUN_KEY>):
    GET  /run      lists the ticked rows and shows a Run button. Changes nothing, so link
                   previews and double clicks are harmless.
    POST /run      processes ticked rows inside this request and streams each row's result as it
                   finishes. Rows not reached before the budget ends stay ticked for the next click.
    GET  /status   every row that has been processed, read from the grid

Nothing is kept in memory between requests: the grid's Status and Output columns are the state.
"""

from __future__ import annotations

import html
import os
import secrets
import threading
from typing import Iterator
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse

from lark_base import BaseClient, ConfigError, LarkApiError, client_from_env, load_dotenv
from llm import LLM, llm_from_env
from pipeline import (
    STATUS_FAILED,
    STATUS_INTEGRATED,
    STATUS_NOT_PROCESSED,
    Row,
    RowResult,
    all_rows,
    describe,
    run_ticked_rows,
    ticked_rows,
)

DEFAULT_BUDGET_SECONDS = 150.0

STYLE = (
    "body{font-family:system-ui,sans-serif;max-width:48rem;margin:2rem auto;padding:0 1rem}"
    "table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:.4rem;text-align:left}"
    "button{font-size:1rem;padding:.6rem 1.2rem}.failed{color:#b00020}.ok{color:#1b5e20}"
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
    css = "failed" if status == STATUS_FAILED else "ok" if status == STATUS_INTEGRATED else ""
    return f'<td class="{css}">{html.escape(status)}</td>'


RESULTS_HEAD = "<table><tr><th>Row</th><th>Status</th><th>Detail</th><th>CSV file</th></tr>"


def _result_row(result: RowResult) -> str:
    return (f"<tr><td>{html.escape(result.record_id)}</td>{_status_cell(result.status)}"
            f"<td>{html.escape(result.detail)}</td><td>{html.escape(result.file_name or '')}</td></tr>")


def _rows_table(rows: list[Row]) -> str:
    items = "".join(f"<tr><td>{html.escape(describe(row))}</td></tr>" for row in rows)
    return f"<table><tr><th>Rows that will be processed</th></tr>{items}</table>"


def _processed_table(rows: list[Row]) -> str:
    body = "".join(
        f"<tr><td>{html.escape(row.record_id)}</td>"
        f"<td>{html.escape(row.date.isoformat() if row.date else '')}</td>"
        f"<td>{html.escape(row.sub_campaign)}</td>{_status_cell(row.status)}"
        f"<td>{html.escape(', '.join(a.name for a in row.outputs))}</td></tr>"
        for row in rows)
    return ("<table><tr><th>Row</th><th>Date</th><th>Sub-campaign</th><th>Status</th><th>CSV file</th></tr>"
            f"{body}</table>")


def _grid_error(exc: Exception) -> HTMLResponse:
    return HTMLResponse(_page("Cannot read the grid", f"<p>{html.escape(str(exc))}</p>"), status_code=502)


def create_app(client: BaseClient, llm: LLM, run_key: str,
               budget_seconds: float = DEFAULT_BUDGET_SECONDS) -> FastAPI:
    if not run_key:
        raise ConfigError("RUN_KEY is required: the trigger link must not be open to everyone.")
    # No /docs or /openapi.json: the service is reachable from the internet.
    app = FastAPI(title="Row processor", docs_url=None, redoc_url=None, openapi_url=None)
    # Stops two runs in the same process (e.g. a double click). Runs on different instances are
    # kept apart only by the claim step: a claimed row is no longer ticked.
    busy = threading.Lock()

    def check_key(key: str) -> None:
        if not secrets.compare_digest(key.encode(), run_key.encode()):
            raise HTTPException(status_code=403, detail="Invalid or missing key.")

    def links(key: str) -> str:
        return (f'<p><a href="/run?key={quote(key)}">Run page</a> · '
                f'<a href="/status?key={quote(key)}">Status of all rows</a></p>')

    def stream_run(key: str) -> Iterator[str]:
        if not busy.acquire(blocking=False):
            yield _page("Already running", "<p>A run is already in progress.</p>" + links(key))
            return
        try:
            yield (_head("Processing ticked rows")
                   + "<p>Keep this page open. Each row appears below when it is done.</p>" + RESULTS_HEAD)
            results: list[RowResult] = []
            try:
                for result in run_ticked_rows(client, llm, budget_seconds):
                    results.append(result)
                    yield _result_row(result)
                yield "</table>"
                left = len(ticked_rows(client))
            except Exception as exc:  # the run as a whole failed, e.g. the grid could not be read
                reason = html.escape(f"{type(exc).__name__}: {exc}")
                yield f'</table><p class="failed">Run failed: {reason}</p>'
                left = 0
            failed = sum(1 for r in results if r.status == STATUS_FAILED)
            if results:
                yield f"<p>Done: {len(results) - failed} integrated, {failed} failed.</p>"
            else:
                yield "<p>No rows were ticked.</p>"
            if left:
                yield (f"<p>{left} row(s) are still ticked: the time budget ran out before they were "
                       "reached, or they could not be claimed. Open the run page and click Run again.</p>")
            yield links(key) + FOOT
        finally:
            busy.release()

    @app.get("/run", response_class=HTMLResponse)
    def confirm(key: str = "") -> HTMLResponse:
        check_key(key)
        try:
            rows = ticked_rows(client)
        except (LarkApiError, httpx.HTTPError) as exc:
            return _grid_error(exc)
        if not rows:
            body = ('<p>No rows are ticked. Tick &quot;Process Row?&quot; on the rows to process, '
                    "then reload.</p>")
        else:
            body = (_rows_table(rows)
                    + f'<form method="post" action="/run?key={quote(key)}" '
                    "onsubmit=\"var b=this.querySelector('button');b.disabled=true;b.textContent='Running...'\">"
                    f'<p><button type="submit">Run {len(rows)} row(s)</button></p></form>')
        return HTMLResponse(_page("Process ticked rows", body + links(key)))

    @app.post("/run", response_model=None)
    def start(key: str = "") -> StreamingResponse:
        check_key(key)
        return StreamingResponse(stream_run(key), media_type="text/html")

    @app.get("/status", response_class=HTMLResponse)
    def status(key: str = "") -> HTMLResponse:
        check_key(key)
        try:
            rows = [r for r in all_rows(client) if r.status not in ("", STATUS_NOT_PROCESSED)]
        except (LarkApiError, httpx.HTTPError) as exc:
            return _grid_error(exc)
        if not rows:
            body = "<p>No row has been processed yet.</p>"
        else:
            body = (_processed_table(rows)
                    + "<p>A row stuck in Queued was cut off before it finished; tick it again. "
                    "The reason a row Failed is shown on the page of the run that processed it.</p>")
        return HTMLResponse(_page("Processed rows", body + links(key)))

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
