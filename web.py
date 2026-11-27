"""Trigger service: a link in Lark opens a confirmation page, and its Run button starts a run.

    uvicorn app:app --port 8000          (app.py builds the app from the environment)

Configuration: everything lark_base.py and llm.py need, plus
    RUN_KEY               required, a shared secret that must be in every request URL (?key=...)
    RUN_BUDGET_SECONDS    optional, default 150. A run starts no new row after this many seconds,
                          so keep it well below the host's function time limit.

Pages (all need ?key=<RUN_KEY>):
    GET  /run      lists the ticked rows and shows a Run button. Changes nothing, so link
                   previews and double clicks are harmless.
    POST /run      processes ticked rows inside this request and streams each row's result and
                   downloadable CSV as it finishes.
    GET  /status   output table of rows processed in this server instance, with downloadable CSVs.

Nothing is written back to Lark.
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
    CsvSlice,
    Row,
    RowResult,
    describe,
    run_ticked_rows,
    ticked_rows,
)
from prompts import PROMPTS, PROMPTS_BY_ID, PromptSpec, resolve_prompts, should_chain

DEFAULT_BUDGET_SECONDS = 150.0

STYLE = (
    "body{font-family:system-ui,sans-serif;max-width:56rem;margin:2rem auto;padding:0 1rem}"
    "table{border-collapse:collapse;width:100%;margin-bottom:1rem}"
    "td,th{border:1px solid #ccc;padding:.45rem;text-align:left;vertical-align:top}"
    "button{font-size:1rem;padding:.6rem 1.2rem}.failed{color:#b00020}.ok{color:#1b5e20}"
    "pre{max-height:16rem;overflow:auto;background:#f6f6f6;padding:.5rem;font-size:.8rem}"
    "details{margin-top:.35rem}details button{font-size:.8rem;padding:.2rem .5rem;margin-bottom:.3rem}"
    "fieldset{border:1px solid #ccc;padding:.75rem 1rem;margin-bottom:1rem}"
    "legend{font-weight:600;padding:0 .35rem}"
    ".prompt-opt{display:block;margin:.4rem 0;line-height:1.35}"
    ".prompt-desc{color:#555;font-size:.88rem;display:block;margin-left:1.5rem}"
    ".prompt-actions{margin:.5rem 0 .25rem}.prompt-actions button{font-size:.82rem;padding:.25rem .6rem;margin-right:.4rem}"
    ".hint{color:#555;font-size:.85rem;margin:.4rem 0 0}"
    ".slice-item{margin-bottom:.55rem;padding-bottom:.45rem;border-bottom:1px solid #eee}"
    ".slice-item:last-child{border-bottom:none;margin-bottom:0;padding-bottom:0}"
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


RESULTS_HEAD = "<table><tr><th>Row</th><th>Status</th><th>Detail</th><th>CSV file</th></tr>"


def _csv_download_block(file_name: str, csv_text: str, link_label: str | None = None) -> str:
    escaped_name = html.escape(file_name)
    label = html.escape(link_label or f"Download {file_name}")
    data_uri = f"data:text/csv;charset=utf-8,{quote(csv_text, safe='')}"
    preview = html.escape(csv_text)
    return (
        f'<div class="slice-item"><a href="{data_uri}" download="{escaped_name}">{label}</a>'
        f"<details><summary>Preview</summary>"
        f'<button type="button" onclick="navigator.clipboard.writeText(this.nextElementSibling.textContent)">'
        f"Copy CSV</button><pre>{preview}</pre></details></div>"
    )


def _csv_cell(result: RowResult) -> str:
    if result.status != STATUS_DONE or not result.file_name or result.csv_text is None:
        return "<td></td>"
    if result.slices:
        parts = [
            _csv_download_block(s.file_name, s.csv_text, f"Download {s.rule_name}.csv")
            for s in result.slices
        ]
        if len(result.slices) > 1:
            parts.append(
                _csv_download_block(result.file_name, result.csv_text, f"Download All ({result.file_name})")
            )
        return f"<td>{''.join(parts)}</td>"
    return f"<td>{_csv_download_block(result.file_name, result.csv_text)}</td>"


def _prompt_labels(prompt_ids: tuple[str, ...]) -> str:
    return ", ".join(PROMPTS_BY_ID[pid].label if pid in PROMPTS_BY_ID else pid for pid in prompt_ids)


def _prompt_selector(selected: tuple[PromptSpec, ...]) -> str:
    selected_ids = {spec.id for spec in selected}
    options = []
    for spec in PROMPTS:
        checked = " checked" if spec.id in selected_ids else ""
        options.append(
            f'<label class="prompt-opt">'
            f'<input type="checkbox" name="prompt" value="{html.escape(spec.id)}"{checked}> '
            f'<strong>{html.escape(spec.label)}</strong>'
            f'<span class="prompt-desc">{html.escape(spec.description)}</span>'
            f"</label>"
        )
    return (
        "<fieldset><legend>Dispositions to generate</legend>"
        + "".join(options)
        + '<div class="prompt-actions">'
        '<button type="button" onclick="this.closest(\'fieldset\').querySelectorAll(\'input[name=prompt]\').forEach(c=>c.checked=true)">Select all</button>'
        '<button type="button" onclick="this.closest(\'fieldset\').querySelectorAll(\'input[name=prompt]\').forEach(c=>c.checked=(c.value===\'rpc_ptp\'))">RPC + PTP only</button>'
        "</div>"
        '<p class="hint">1–3 blocks run in one composed call; 4+ blocks including RPC + PTP automatically '
        "chain Call 2 on top of Call 1&#39;s CSV to prevent drift. Outputs are sliced by Rule Name.</p>"
        "</fieldset>"
    )


def _result_row(result: RowResult) -> str:
    return (f"<tr><td>{html.escape(result.record_id)}</td>{_status_cell(result.status)}"
            f"<td>{html.escape(result.detail)}</td>{_csv_cell(result)}</tr>")


def _rows_table(rows: list[Row]) -> str:
    items = "".join(f"<tr><td>{html.escape(describe(row))}</td></tr>" for row in rows)
    return f"<table><tr><th>Rows that will be processed</th></tr>{items}</table>"


def _processed_table(results: list[RowResult]) -> str:
    body = "".join(
        f"<tr><td>{html.escape(result.record_id)}</td>"
        f"<td>{html.escape(result.date.isoformat() if result.date else '')}</td>"
        f"<td>{html.escape(result.sub_campaign)}</td>"
        f"<td>{html.escape(_prompt_labels(result.prompt_ids))}</td>"
        f"{_status_cell(result.status)}"
        f"<td>{html.escape(result.detail)}</td>{_csv_cell(result)}</tr>"
        for result in results)
    return ("<table><tr><th>Row</th><th>Date</th><th>Sub-campaign</th><th>Dispositions</th><th>Status</th>"
            f"<th>Detail</th><th>CSV file</th></tr>{body}</table>")


def _grid_error(exc: Exception) -> HTMLResponse:
    return HTMLResponse(_page("Cannot read the grid", f"<p>{html.escape(str(exc))}</p>"), status_code=502)


def _bad_prompt_error(exc: Exception) -> HTMLResponse:
    return HTMLResponse(_page("Invalid disposition selection", f"<p>{html.escape(str(exc))}</p>"), status_code=400)


def create_app(client: BaseClient, llm: LLM, run_key: str,
               budget_seconds: float = DEFAULT_BUDGET_SECONDS) -> FastAPI:
    if not run_key:
        raise ConfigError("RUN_KEY is required: the trigger link must not be open to everyone.")
    # No /docs or /openapi.json: the service is reachable from the internet.
    app = FastAPI(title="Row processor", docs_url=None, redoc_url=None, openapi_url=None)
    # Stops two concurrent runs in the same process (e.g. a double click).
    busy = threading.Lock()
    # Retains the latest output per (record_id, prompt_ids) for the /status output table.
    history_lock = threading.Lock()
    history: dict[tuple[str, tuple[str, ...]], RowResult] = {}

    def check_key(key: str) -> None:
        if not secrets.compare_digest(key.encode(), run_key.encode()):
            raise HTTPException(status_code=403, detail="Invalid or missing key.")

    def links(key: str) -> str:
        return (f'<p><a href="/run?key={quote(key)}">Run page</a> · '
                f'<a href="/status?key={quote(key)}">Status of all rows</a></p>')

    def stream_run(key: str, selected: tuple[PromptSpec, ...]) -> Iterator[str]:
        if not busy.acquire(blocking=False):
            yield _page("Already running", "<p>A run is already in progress.</p>" + links(key))
            return
        try:
            mode_note = " (chained 2 calls per row)" if should_chain(selected) else ""
            prompt_summary = html.escape(_prompt_labels(tuple(s.id for s in selected)))
            yield (_head("Processing ticked rows")
                   + f"<p>Dispositions: <strong>{prompt_summary}</strong>{mode_note}. "
                   "Keep this page open. Each row and its CSV download appear below when done.</p>"
                   + RESULTS_HEAD)
            results: list[RowResult] = []
            try:
                for result in run_ticked_rows(client, llm, budget_seconds, selected_prompts=selected):
                    results.append(result)
                    history_key = (result.record_id, result.prompt_ids)
                    with history_lock:
                        history.pop(history_key, None)
                        history[history_key] = result
                    yield _result_row(result)
                yield "</table>"
                left = max(0, len(ticked_rows(client)) - len(results))
            except Exception as exc:  # the run as a whole failed, e.g. the grid could not be read
                reason = html.escape(f"{type(exc).__name__}: {exc}")
                yield f'</table><p class="failed">Run failed: {reason}</p>'
                left = 0
            failed = sum(1 for r in results if r.status == STATUS_FAILED)
            if results:
                yield f"<p>Done: {len(results) - failed} succeeded, {failed} failed.</p>"
            else:
                yield "<p>No rows were ticked.</p>"
            if left:
                yield (f"<p>{left} ticked row(s) were not reached before the time budget ended.</p>")
            yield links(key) + FOOT
        finally:
            busy.release()

    @app.get("/run", response_class=HTMLResponse)
    def confirm(request: Request, key: str = "") -> HTMLResponse:
        check_key(key)
        try:
            selected = resolve_prompts(request.query_params.getlist("prompt"))
        except ValueError as exc:
            return _bad_prompt_error(exc)
        try:
            rows = ticked_rows(client)
        except (LarkApiError, httpx.HTTPError) as exc:
            return _grid_error(exc)
        if not rows:
            body = ('<p>No rows are ticked. Tick &quot;Process Row?&quot; on the rows to process, '
                    "then reload.</p>")
        else:
            onsubmit = (
                "if(!this.querySelector('input[name=prompt]:checked')){"
                "alert('Select at least one disposition.');return false;}"
                "var b=this.querySelector('button[type=submit]');"
                "b.disabled=true;b.textContent='Running...'"
            )
            body = (
                _rows_table(rows)
                + f'<form method="post" action="/run?key={quote(key)}" onsubmit="{onsubmit}">'
                + _prompt_selector(selected)
                + f'<p><button type="submit">Run {len(rows)} row(s)</button></p></form>'
            )
        return HTMLResponse(_page("Process ticked rows", body + links(key)))

    @app.post("/run", response_model=None)
    async def start(request: Request, key: str = "") -> StreamingResponse | HTMLResponse:
        check_key(key)
        raw_body = (await request.body()).decode("utf-8", errors="replace")
        form_prompts = parse_qs(raw_body).get("prompt", [])
        raw_prompts = form_prompts if form_prompts else request.query_params.getlist("prompt")
        try:
            selected = resolve_prompts(raw_prompts)
        except ValueError as exc:
            return _bad_prompt_error(exc)
        return StreamingResponse(stream_run(key, selected), media_type="text/html")

    @app.get("/status", response_class=HTMLResponse)
    def status(key: str = "") -> HTMLResponse:
        check_key(key)
        with history_lock:
            rows = list(history.values())
        if not rows:
            body = "<p>No row has been processed yet.</p>"
        else:
            body = _processed_table(rows)
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
