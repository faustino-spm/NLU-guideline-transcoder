"""LLM interface, a stub for wiring checks, and the Gemini implementation.

`LLM=stub` in `.env` selects the stub; the default is Gemini (needs GEMINI_API_KEY).
"""

from __future__ import annotations

import base64
import csv
import io
import logging
import os
import re
import time
from datetime import date
from pathlib import Path
from typing import Callable, Protocol

import httpx

from lark_base import ConfigError, load_dotenv

log = logging.getLogger("llm")

DEFAULT_PROMPT_FILE = Path(__file__).parent / "prompt.txt"
DEFAULT_GEMINI_MODEL = "gemini-4-hydrogen"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# What every request is told, whatever prompt.txt says, so the reply can be saved as a file as is.
SYSTEM_INSTRUCTION = "Reply with the CSV only: a header row, then one record per line. No commentary, no code fences."

# Types Gemini reads (https://ai.google.dev/gemini-api/docs/document-processing). Anything else fails the row.
MIME_BY_SUFFIX = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".ppt": "application/vnd.ms-powerpoint",
    ".odp": "application/vnd.oasis.opendocument.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".html": "text/html",
    ".csv": "text/csv",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

RETRY_STATUSES = {429, 500, 502, 503, 504}


class LLM(Protocol):
    def generate_csv(self, row_date: date | None, sub_campaign: str, files: list[Path]) -> str:
        """Return the CSV text for one row. `files` are the row's downloaded attachments."""
        ...


class StubLLM:
    """Returns a fixed-shape CSV describing its inputs, so the whole round trip can be checked."""

    def generate_csv(self, row_date: date | None, sub_campaign: str, files: list[Path]) -> str:
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(["data_collection_date", "sub_campaign", "attachment_count", "attachment_names"])
        writer.writerow([
            row_date.isoformat() if row_date else "",
            sub_campaign,
            len(files),
            ";".join(f.name for f in files),
        ])
        return buffer.getvalue()


# -- Gemini ------------------------------------------------------------------


class LLMError(RuntimeError):
    """The model call failed or returned something that is not a usable CSV."""


def clean_csv(text: str) -> str:
    """Strip a code fence and a BOM from the model's reply, and end it with one newline."""
    text = text.strip().lstrip("\ufeff")
    fenced = re.match(r"^```[A-Za-z]*\s*\n(.*?)\n?```$", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    return text.strip("\r\n") + "\n" if text.strip() else ""


def validate_csv(text: str) -> None:
    """Raise LLMError unless `text` has a header, at least one record, and the same column count on every row."""
    if not text.strip():
        raise LLMError("the model returned no text")
    try:
        rows = list(csv.reader(io.StringIO(text), strict=True))
    except csv.Error as exc:
        raise LLMError(f"the reply is not valid CSV ({exc})") from exc
    rows = [row for row in rows if row]  # blank lines are harmless
    if len(rows) < 2:
        raise LLMError("the CSV has no data rows")
    width = len(rows[0])
    for number, row in enumerate(rows[1:], start=2):
        if len(row) != width:
            raise LLMError(f"CSV row {number} has {len(row)} columns, the header has {width}")


class GeminiLLM:
    """Sends the row's files and prompt.txt to Gemini and returns the CSV it writes.

    Files go inline in the request (no Files API). The prompt file is read on every call, so editing it
    needs no restart; `{date}` and `{sub_campaign}` in it are replaced by the row's values. A reply that
    is not a valid CSV is asked for once more, then the row fails.
    Rate limits and server errors (429, 5xx) are retried with a pause.
    """

    def __init__(self, api_key: str, model: str = DEFAULT_GEMINI_MODEL,
                 prompt_file: Path = DEFAULT_PROMPT_FILE, timeout: float = 120.0,
                 transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 retry_delays: tuple[float, ...] = (5.0, 20.0)):
        self.model = model
        self.prompt_file = prompt_file
        self._sleep = sleep
        self._retry_delays = retry_delays
        self._http = httpx.Client(timeout=timeout, transport=transport,
                                  headers={"x-goog-api-key": api_key})

    def generate_csv(self, row_date: date | None, sub_campaign: str, files: list[Path]) -> str:
        body = self._request_body(row_date, sub_campaign, files)
        last_error: LLMError | None = None
        for attempt in (1, 2):
            reply = self._call(body)
            try:
                text = clean_csv(reply)
                validate_csv(text)
                return text
            except LLMError as exc:
                last_error = exc
                log.warning("attempt %d: unusable CSV from %s: %s", attempt, self.model, exc)
        raise LLMError(f"no usable CSV after 2 attempts: {last_error}")

    def _request_body(self, row_date: date | None, sub_campaign: str, files: list[Path]) -> dict:
        prompt = self.prompt_file.read_text(encoding="utf-8-sig").strip()
        if not prompt:
            raise LLMError(f"{self.prompt_file.name} is empty")
        # The row's values reach the model only where the prompt asks for them.
        prompt = (prompt.replace("{date}", row_date.isoformat() if row_date else "unknown")
                  .replace("{sub_campaign}", sub_campaign or "unknown"))
        parts: list[dict] = []
        for path in files:
            mime = MIME_BY_SUFFIX.get(path.suffix.lower())
            if mime is None:
                raise LLMError(f"unsupported attachment type: {path.name}")
            parts.append({"inline_data": {"mime_type": mime,
                                          "data": base64.b64encode(path.read_bytes()).decode("ascii")}})
        parts.append({"text": prompt})
        return {"system_instruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
                "contents": [{"role": "user", "parts": parts}]}

    def _call(self, body: dict) -> str:
        """POST one generateContent request and return the reply text."""
        url = GEMINI_URL.format(model=self.model)
        for attempt in range(len(self._retry_delays) + 1):
            try:
                response = self._http.post(url, json=body)
            except httpx.HTTPError as exc:
                raise LLMError(f"could not reach Gemini: {type(exc).__name__}") from exc
            if response.status_code in RETRY_STATUSES and attempt < len(self._retry_delays):
                delay = self._retry_delays[attempt]
                log.warning("Gemini returned %s, retrying in %.0fs", response.status_code, delay)
                self._sleep(delay)
                continue
            break
        if response.status_code != 200:
            raise LLMError(f"Gemini returned {response.status_code}: {_api_error(response)}")
        return _reply_text(response.json())


def _api_error(response: httpx.Response) -> str:
    try:
        return str(response.json()["error"]["message"])[:300]
    except (ValueError, KeyError, TypeError):
        return response.text[:300]


def _reply_text(data: dict) -> str:
    candidates = data.get("candidates") or []
    if not candidates:
        reason = (data.get("promptFeedback") or {}).get("blockReason", "no candidates")
        raise LLMError(f"Gemini returned no answer ({reason})")
    candidate = candidates[0]
    finish = candidate.get("finishReason", "")
    if finish == "MAX_TOKENS":
        raise LLMError("the answer was cut off at the model's output limit, so the CSV is incomplete")
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
    if not text.strip() and finish not in ("", "STOP"):
        raise LLMError(f"Gemini stopped without an answer ({finish})")
    return text


def llm_from_env() -> LLM:
    """Gemini by default; `LLM=stub` in the environment selects the stub."""
    load_dotenv()
    choice = os.environ.get("LLM", "gemini").strip().lower()
    if choice == "stub":
        return StubLLM()
    if choice != "gemini":
        raise ConfigError(f"LLM must be 'gemini' or 'stub', got {choice!r}")
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise ConfigError("GEMINI_API_KEY is not set (set LLM=stub to run without Gemini)")
    if not DEFAULT_PROMPT_FILE.is_file():
        raise ConfigError(f"{DEFAULT_PROMPT_FILE.name} is missing")
    model = os.environ.get("GEMINI_MODEL", "").strip() or DEFAULT_GEMINI_MODEL
    return GeminiLLM(api_key, model)
