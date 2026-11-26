"""LLM interface, a stub for wiring checks, and the NanoGPT implementation.

`LLM=stub` in `.env` selects the stub; the default is NanoGPT (needs NANOGPT_API_KEY)
with the model hardcoded to Hydrogen High (`google/gemini-4-hydrogen`, `reasoning_effort="high"`).
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
NANOGPT_MODEL = "google/gemini-4-hydrogen"
NANOGPT_REASONING_EFFORT = "high"
NANOGPT_URL = "https://api.nano-gpt.com/api/v1/chat/completions"

# What every request is told, whatever prompt.txt says, so the reply can be saved as a file as is.
SYSTEM_INSTRUCTION = "Reply with the CSV only: a header row, then one record per line. No commentary, no code fences."

TEXT_SUFFIXES = {".txt", ".md", ".html", ".csv"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}

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


# -- NanoGPT -----------------------------------------------------------------


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


def _attachment_part(path: Path) -> dict:
    """Encode one attachment as an OpenAI-compatible chat content part."""
    suffix = path.suffix.lower()
    mime = MIME_BY_SUFFIX.get(suffix)
    if mime is None:
        raise LLMError(f"unsupported attachment type: {path.name}")
    if suffix in TEXT_SUFFIXES:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        return {"type": "text", "text": f"Attachment ({path.name}):\n{text}"}
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    data_url = f"data:{mime};base64,{encoded}"
    if suffix in IMAGE_SUFFIXES:
        return {"type": "image_url", "image_url": {"url": data_url}}
    return {"type": "file", "file": {"filename": path.name, "file_data": data_url}}


class NanoGPTLLM:
    """Sends the row's files and prompt.txt to NanoGPT (Hydrogen High) and returns the CSV it writes.

    The prompt file is read on every call, so editing it needs no restart; `{date}` and `{sub_campaign}`
    in it are replaced by the row's values. A reply that is not a valid CSV is asked for once more,
    then the row fails. Rate limits and server errors (429, 5xx) are retried with a pause.
    """

    def __init__(self, api_key: str, model: str = NANOGPT_MODEL,
                 prompt_file: Path = DEFAULT_PROMPT_FILE, timeout: float = 300.0,
                 transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 retry_delays: tuple[float, ...] = (5.0, 20.0)):
        self.model = model
        self.prompt_file = prompt_file
        self._sleep = sleep
        self._retry_delays = retry_delays
        self._http = httpx.Client(timeout=timeout, transport=transport,
                                  headers={"Authorization": f"Bearer {api_key}"})

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
        parts = [_attachment_part(path) for path in files]
        parts.append({"type": "text", "text": prompt})
        return {
            "model": self.model,
            "reasoning_effort": NANOGPT_REASONING_EFFORT,
            "messages": [
                {"role": "system", "content": SYSTEM_INSTRUCTION},
                {"role": "user", "content": parts},
            ],
        }

    def _call(self, body: dict) -> str:
        """POST one chat completions request and return the reply text."""
        for attempt in range(len(self._retry_delays) + 1):
            try:
                response = self._http.post(NANOGPT_URL, json=body)
            except httpx.HTTPError as exc:
                raise LLMError(f"could not reach NanoGPT: {type(exc).__name__}") from exc
            if response.status_code in RETRY_STATUSES and attempt < len(self._retry_delays):
                delay = self._retry_delays[attempt]
                log.warning("NanoGPT returned %s, retrying in %.0fs", response.status_code, delay)
                self._sleep(delay)
                continue
            break
        if response.status_code != 200:
            raise LLMError(f"NanoGPT returned {response.status_code}: {_api_error(response)}")
        return _reply_text(response.json())


def _api_error(response: httpx.Response) -> str:
    try:
        err = response.json()["error"]
        if isinstance(err, dict):
            return str(err["message"])[:300]
        return str(err)[:300]
    except (ValueError, KeyError, TypeError):
        return response.text[:300]


def _reply_text(data: dict) -> str:
    choices = data.get("choices") or []
    if not choices:
        raise LLMError("NanoGPT returned no answer (no choices)")
    choice = choices[0]
    finish = choice.get("finish_reason") or ""
    if finish == "length":
        raise LLMError("the answer was cut off at the model's output limit, so the CSV is incomplete")
    message = choice.get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        text = "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    else:
        text = content or ""
    if not text.strip() and finish not in ("", "stop"):
        raise LLMError(f"NanoGPT stopped without an answer ({finish})")
    return text


def llm_from_env() -> LLM:
    """NanoGPT (Hydrogen High) by default; `LLM=stub` in the environment selects the stub."""
    load_dotenv()
    choice = os.environ.get("LLM", "nanogpt").strip().lower()
    if choice == "stub":
        return StubLLM()
    if choice != "nanogpt":
        raise ConfigError(f"LLM must be 'nanogpt' or 'stub', got {choice!r}")
    api_key = os.environ.get("NANOGPT_API_KEY", "").strip()
    if not api_key:
        raise ConfigError("NANOGPT_API_KEY is not set (set LLM=stub to run without NanoGPT)")
    if not DEFAULT_PROMPT_FILE.is_file():
        raise ConfigError(f"{DEFAULT_PROMPT_FILE.name} is missing")
    return NanoGPTLLM(api_key, NANOGPT_MODEL)
