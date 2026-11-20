"""Trigger service: a link in Lark opens a confirmation page, and its Run button starts a run.

    uvicorn server:app_from_env --factory --port 8000

Configuration: everything lark_base.py needs, plus
    RUN_KEY    required, a shared secret that must be in every request URL (?key=...)

Pages (all need ?key=<RUN_KEY>):
    GET  /run      lists the ticked rows and shows a Run button. Changes nothing, so link
                   previews and double clicks are harmless.
    POST /run      starts a run in the background and redirects to /status
    GET  /status   per-row results of the latest run (refreshes while it is running)
"""

from __future__ import annotations

import html
import os
import secrets
import threading
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from lark_base import BaseClient, ConfigError, LarkApiError, client_from_env
from lark_oauth import load_dotenv
from llm import LLM, llm_from_env
from pipeline import (
    DEFAULT_OUTPUT_DIR,
    STATUS_FAILED,
    RunReport,
    Row,
    describe,
    process_ticked_rows,
    ticked_rows,
)

STYLE = (
    "body{font-family:system-ui,sans-serif;max-width:48rem;margin:2rem auto;padding:0 1rem}"
    "table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:.4rem;text-align:left}"
    "button{font-size:1rem;padding:.6rem 1.2rem}.failed{color:#b00020}.ok{color:#1b5e20}"
)


def _page(title: str, body: str, refresh: bool = False) -> str:
    meta = '<meta http-equiv="refresh" content="3">' if refresh else ""
    return (f'<!doctype html><html><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">{meta}'
            f"<title>{html.escape(title)}</title><style>{STYLE}</style></head>"
            f"<body><h1>{html.escape(title)}</h1>{body}</body></html>")


class RunManager:
    """Runs the pipeline in a background thread. At most one run at a time."""

    def __init__(self, client: BaseClient, llm: LLM, output_dir: Path):
        self._client = client
        self._llm = llm
        self._output_dir = output_dir
        self._state = threading.Lock()
        self._pipeline_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._running = False
        self._report: RunReport | None = None

    def start(self) -> bool:
        """Start a run. Returns False, and starts nothing, if one is already running."""
        with self._state:
            if self._running:
                return False
            self._running = True
            self._report = RunReport()
            self._thread = threading.Thread(target=self._run, args=(self._report,), daemon=True)
            self._thread.start()
            return True

    def _run(self, report: RunReport) -> None:
        try:
            process_ticked_rows(self._client, self._llm, report=report,
                                lock=self._pipeline_lock, output_dir=self._output_dir)
        except Exception as exc:  # the run as a whole failed, e.g. the grid could not be read
            report.error = f"{type(exc).__name__}: {exc}"
        finally:
            with self._state:
                self._running = False

    def snapshot(self) -> tuple[bool, RunReport | None]:
        with self._state:
            return self._running, self._report

    def wait(self, timeout: float | None = None) -> None:
        """Block until the current run finishes. Used by tests."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)


def _rows_table(rows: list[Row]) -> str:
    items = "".join(f"<tr><td>{html.escape(describe(row))}</td></tr>" for row in rows)
    return f"<table><tr><th>Rows that will be processed</th></tr>{items}</table>"


def _results_table(report: RunReport) -> str:
    rows = ""
    for result in report.results:
        css = "failed" if result.status == STATUS_FAILED else "ok"
        rows += (f"<tr><td>{html.escape(result.record_id)}</td>"
                 f'<td class="{css}">{html.escape(result.status)}</td>'
                 f"<td>{html.escape(result.detail)}</td>"
                 f"<td>{html.escape(result.output_path or '')}</td></tr>")
    return f"<table><tr><th>Row</th><th>Status</th><th>Detail</th><th>CSV file</th></tr>{rows}</table>"


def create_app(client: BaseClient, llm: LLM, run_key: str,
               output_dir: Path = DEFAULT_OUTPUT_DIR) -> FastAPI:
    if not run_key:
        raise ConfigError("RUN_KEY is required: the trigger link must not be open to everyone.")
    # No /docs or /openapi.json: the service is reachable through a public tunnel.
    app = FastAPI(title="Row processor", docs_url=None, redoc_url=None, openapi_url=None)
    runs = RunManager(client, llm, output_dir)
    app.state.runs = runs

    def check_key(key: str) -> None:
        if not secrets.compare_digest(key.encode(), run_key.encode()):
            raise HTTPException(status_code=403, detail="Invalid or missing key.")

    @app.get("/run", response_class=HTMLResponse)
    def confirm(key: str = "") -> HTMLResponse:
        check_key(key)
        try:
            rows = ticked_rows(client)
        except (LarkApiError, httpx.HTTPError) as exc:
            return HTMLResponse(_page("Cannot read the grid", f"<p>{html.escape(str(exc))}</p>"), status_code=502)
        running, _ = runs.snapshot()
        status_link = f'<p><a href="/status?key={quote(key)}">Latest run status</a></p>'
        if running:
            body = f"<p>A run is in progress.</p>{status_link}"
        elif not rows:
            body = f"<p>No rows are ticked. Tick &quot;Process Row?&quot; on the rows to process, then reload.</p>{status_link}"
        else:
            body = (f"{_rows_table(rows)}"
                    f'<form method="post" action="/run?key={quote(key)}">'
                    f'<p><button type="submit">Run {len(rows)} row(s)</button></p></form>{status_link}')
        return HTMLResponse(_page("Process ticked rows", body))

    @app.post("/run", response_model=None)
    def start(key: str = "") -> RedirectResponse | HTMLResponse:
        check_key(key)
        if not runs.start():
            body = f'<p>A run is already in progress.</p><p><a href="/status?key={quote(key)}">View status</a></p>'
            return HTMLResponse(_page("Already running", body), status_code=409)
        return RedirectResponse(f"/status?key={quote(key)}", status_code=303)

    @app.get("/status", response_class=HTMLResponse)
    def status(key: str = "") -> HTMLResponse:
        check_key(key)
        running, report = runs.snapshot()
        if report is None:
            return HTMLResponse(_page("Run status", "<p>No run has been started yet.</p>"))
        if running:
            state = f"<p>Running since {html.escape(report.started_at)}...</p>"
        else:
            state = f"<p>Finished at {html.escape(report.finished_at or '')}.</p>"
        error = f'<p class="failed">Run failed: {html.escape(report.error)}</p>' if report.error else ""
        return HTMLResponse(_page("Run status", state + error + _results_table(report), refresh=running))

    return app


def app_from_env() -> FastAPI:
    load_dotenv()
    return create_app(client_from_env(), llm_from_env(), os.environ.get("RUN_KEY", ""))
