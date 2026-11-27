"""LLM interface, a stub for wiring checks, and OpenAI-compatible + Google Gemini implementations.

Change `LLM_PROVIDER` below (and redeploy) to switch providers:
- `"nanogpt"` — NanoGPT via OpenAI-compatible chat completions (uses `NANOGPT_API_KEY`)
- `"openai"`  — OpenAI or any OpenAI-compatible endpoint (uses `OPENAI_API_KEY`, optional `OPENAI_BASE_URL` / `OPENAI_MODEL`)
- `"gemini"`  — Google Gemini native `generateContent` API (uses `GEMINI_API_KEY`, optional `GEMINI_MODEL`)
- `"stub"`    — Fixed dummy CSV without calling any API (for testing the Lark wiring)

If the `LLM` environment variable is explicitly set (e.g. `LLM=stub`), it overrides `LLM_PROVIDER`.
"""

from __future__ import annotations

import base64
import csv
import io
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Protocol

import httpx

from lark_base import ConfigError, load_dotenv
from prompts import validate_prompts_exist

log = logging.getLogger("llm")

# -- Provider switch ---------------------------------------------------------
# Change this value in the backend and redeploy to switch providers:
#   "nanogpt" | "openai" | "gemini" | "stub"
LLM_PROVIDER = "gemini"

DEFAULT_PROMPT_FILE = Path(__file__).parent / "prompt.txt"

# OpenAI-compatible endpoints
NANOGPT_MODEL = "google/gemini-4-hydrogen"
NANOGPT_REASONING_EFFORT = "high"
NANOGPT_URL = "https://api.nano-gpt.com/api/v1/chat/completions"

OPENAI_MODEL = "gpt-5"
OPENAI_REASONING_EFFORT: str | None = None
OPENAI_URL = "https://api.openai.com/v1/chat/completions"

# Google Gemini native API
GEMINI_MODEL = "gemini-4-hydrogen"
GEMINI_THINKING_LEVEL: str | None = "high"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# What every request is told, whatever prompt.txt says, so the reply can be saved as a file as is.
SYSTEM_INSTRUCTION = "Reply with the CSV only: a header row, then one record per line. No commentary, no code fences."

TEXT_SUFFIXES = {".txt", ".md", ".html", ".csv"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
OFFICE_SUFFIXES = {".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp"}

MIME_BY_SUFFIX = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".odp": "application/vnd.oasis.opendocument.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
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
    def generate_csv(
        self,
        row_date: date | None,
        sub_campaign: str,
        files: list[Path],
        *,
        prompt_text: str | None = None,
    ) -> str:
        """Return the CSV text for one row. `files` are the row's downloaded attachments."""
        ...


class StubLLM:
    """Returns a fixed-shape CSV describing its inputs, so the whole round trip can be checked."""

    def generate_csv(
        self,
        row_date: date | None,
        sub_campaign: str,
        files: list[Path],
        *,
        prompt_text: str | None = None,
    ) -> str:
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


# -- Shared helpers ----------------------------------------------------------


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


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _extract_embedded_images(zf: zipfile.ZipFile, prefix: str) -> list[tuple[str, str]]:
    """Return `(mime, base64_data)` tuples for supported raster images under `prefix` in a ZIP archive."""
    images: list[tuple[str, str]] = []
    for name in sorted(zf.namelist()):
        if not name.startswith(prefix) or name.endswith("/"):
            continue
        suffix = Path(name).suffix.lower()
        if suffix not in IMAGE_SUFFIXES:
            continue
        mime = MIME_BY_SUFFIX[suffix]
        encoded = base64.b64encode(zf.read(name)).decode("ascii")
        images.append((mime, encoded))
    return images


def _docx_paragraph_text(p: ET.Element) -> str:
    chunks: list[str] = []
    for el in p.iter():
        tag = _local_name(el.tag)
        if tag == "t" and el.text:
            chunks.append(el.text)
        elif tag == "tab":
            chunks.append("\t")
        elif tag in ("br", "cr"):
            chunks.append("\n")
    return "".join(chunks).strip()


def _docx_xml_lines(xml_bytes: bytes) -> list[str]:
    root = ET.fromstring(xml_bytes)
    container = next((el for el in root if _local_name(el.tag) == "body"), root)
    lines: list[str] = []
    for child in container:
        name = _local_name(child.tag)
        if name == "p":
            text = _docx_paragraph_text(child)
            if text:
                lines.append(text)
        elif name == "tbl":
            for row in child:
                if _local_name(row.tag) != "tr":
                    continue
                cells: list[str] = []
                for cell in row:
                    if _local_name(cell.tag) != "tc":
                        continue
                    paras = [_docx_paragraph_text(p) for p in cell.iter() if _local_name(p.tag) == "p"]
                    cells.append(" ".join(p for p in paras if p))
                if any(cells):
                    lines.append(" | ".join(cells))
    return lines


def _extract_docx(zf: zipfile.ZipFile) -> tuple[str, list[tuple[str, str]]]:
    names = zf.namelist()
    sections: list[str] = []
    headers = sorted(n for n in names if re.fullmatch(r"word/header\d*\.xml", n))
    header_lines = [line for h in headers for line in _docx_xml_lines(zf.read(h))]
    if header_lines:
        sections.append("=== Header ===\n" + "\n".join(header_lines))
    body_lines = _docx_xml_lines(zf.read("word/document.xml"))
    if body_lines:
        sections.append("\n".join(body_lines))
    footers = sorted(n for n in names if re.fullmatch(r"word/footer\d*\.xml", n))
    footer_lines = [line for f in footers for line in _docx_xml_lines(zf.read(f))]
    if footer_lines:
        sections.append("=== Footer ===\n" + "\n".join(footer_lines))
    return "\n\n".join(sections), _extract_embedded_images(zf, "word/media/")


_EXCEL_BUILTIN_DATE_FMTS = (
    set(range(14, 23)) | set(range(27, 37)) | set(range(45, 48)) | set(range(50, 59))
)


def _is_date_format_code(code: str) -> bool:
    # Strip quoted literals and [color]/[locale] brackets before checking for date/time tokens.
    cleaned = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', "", code).lower()
    return bool(re.search(r"[ymdhs]", cleaned)) and not re.search(r"[#0?]", cleaned)


def _xlsx_date_style_indices(zf: zipfile.ZipFile) -> set[int]:
    if "xl/styles.xml" not in zf.namelist():
        return set()
    root = ET.fromstring(zf.read("xl/styles.xml"))
    custom_date_ids: set[int] = set()
    for el in root.iter():
        if _local_name(el.tag) == "numFmt":
            try:
                fmt_id = int(el.attrib["numFmtId"])
            except (KeyError, ValueError):
                continue
            if _is_date_format_code(el.attrib.get("formatCode", "")):
                custom_date_ids.add(fmt_id)
    date_styles: set[int] = set()
    cell_xfs = next((el for el in root.iter() if _local_name(el.tag) == "cellXfs"), None)
    if cell_xfs is not None:
        for idx, xf in enumerate(el for el in cell_xfs if _local_name(el.tag) == "xf"):
            try:
                fmt_id = int(xf.attrib.get("numFmtId", "-1"))
            except ValueError:
                continue
            if fmt_id in _EXCEL_BUILTIN_DATE_FMTS or fmt_id in custom_date_ids:
                date_styles.add(idx)
    return date_styles


def _excel_col_index(ref: str) -> int | None:
    match = re.match(r"([A-Za-z]+)", ref)
    if not match:
        return None
    idx = 0
    for ch in match.group(1).upper():
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx - 1


def _format_excel_serial_date(raw: str, epoch_1904: bool) -> str:
    try:
        serial = float(raw)
    except ValueError:
        return raw
    if not (0 <= serial < 2958466):
        return raw
    base = datetime(1904, 1, 1) if epoch_1904 else datetime(1899, 12, 30)
    dt = base + timedelta(days=serial)
    if abs(serial - round(serial)) < 1e-6:
        return dt.date().isoformat()
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _xlsx_sheet_entries(zf: zipfile.ZipFile) -> list[tuple[str, str]]:
    """Return [(sheet_title, zip_member_path)] in workbook order."""
    names = set(zf.namelist())
    rels: dict[str, str] = {}
    if "xl/_rels/workbook.xml.rels" in names:
        rels_root = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        for el in rels_root.iter():
            if _local_name(el.tag) == "Relationship":
                rid = el.attrib.get("Id")
                target = el.attrib.get("Target", "").lstrip("/")
                if rid and target:
                    rels[rid] = target if target.startswith("xl/") else f"xl/{target}"

    sheet_names: list[tuple[str, str | None]] = []
    if "xl/workbook.xml" in names:
        wb_root = ET.fromstring(zf.read("xl/workbook.xml"))
        for el in wb_root.iter():
            if _local_name(el.tag) == "sheet" and "name" in el.attrib:
                rid = next((v for k, v in el.attrib.items() if _local_name(k) == "id"), None)
                sheet_names.append((el.attrib["name"], rels.get(rid) if rid else None))

    resolved = [(title, path) for title, path in sheet_names if path and path in names]
    if resolved:
        return resolved

    sheet_files = sorted(
        (n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)),
        key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)),
    )
    return [
        (sheet_names[idx][0] if idx < len(sheet_names) else path.rsplit("/", 1)[-1], path)
        for idx, path in enumerate(sheet_files)
    ]


def _extract_xlsx(zf: zipfile.ZipFile) -> tuple[str, list[tuple[str, str]]]:
    names = set(zf.namelist())
    shared: list[str] = []
    if "xl/sharedStrings.xml" in names:
        ss_root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
        for si in ss_root:
            if _local_name(si.tag) == "si":
                shared.append("".join(el.text or "" for el in si.iter() if _local_name(el.tag) == "t"))

    epoch_1904 = False
    if "xl/workbook.xml" in names:
        wb_root = ET.fromstring(zf.read("xl/workbook.xml"))
        for el in wb_root.iter():
            if _local_name(el.tag) == "workbookPr" and el.attrib.get("date1904") in ("1", "true"):
                epoch_1904 = True

    date_styles = _xlsx_date_style_indices(zf)
    sections: list[str] = []
    for title, sheet_file in _xlsx_sheet_entries(zf):
        root = ET.fromstring(zf.read(sheet_file))
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        row_count = 0
        for row in root.iter():
            if _local_name(row.tag) != "row":
                continue
            vals: list[str] = []
            for cell in row:
                if _local_name(cell.tag) != "c":
                    continue
                col_idx = _excel_col_index(cell.attrib.get("r", ""))
                if col_idx is not None and col_idx > len(vals):
                    vals.extend([""] * (col_idx - len(vals)))
                ctype = cell.attrib.get("t", "")
                val = ""
                if ctype == "inlineStr":
                    val = "".join(el.text or "" for el in cell.iter() if _local_name(el.tag) == "t")
                else:
                    v_el = next((el for el in cell if _local_name(el.tag) == "v"), None)
                    if v_el is not None and v_el.text is not None:
                        if ctype == "s" and v_el.text.isdigit() and int(v_el.text) < len(shared):
                            val = shared[int(v_el.text)]
                        elif ctype == "b":
                            val = "TRUE" if v_el.text == "1" else "FALSE"
                        elif ctype == "" and cell.attrib.get("s", "").isdigit() and int(cell.attrib["s"]) in date_styles:
                            val = _format_excel_serial_date(v_el.text, epoch_1904)
                        else:
                            val = v_el.text
                if col_idx is not None and col_idx < len(vals):
                    vals[col_idx] = val.strip()
                else:
                    vals.append(val.strip())
            if any(vals):
                writer.writerow(vals)
                row_count += 1
        if row_count:
            sections.append(f"=== Sheet: {title} ===\n" + buf.getvalue().rstrip("\n"))
    return "\n\n".join(sections), _extract_embedded_images(zf, "xl/media/")


def _extract_pptx(zf: zipfile.ZipFile) -> tuple[str, list[tuple[str, str]]]:
    slide_files = sorted(
        (n for n in zf.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
        key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)),
    )
    sections: list[str] = []
    for idx, slide_file in enumerate(slide_files, start=1):
        root = ET.fromstring(zf.read(slide_file))
        paras: list[str] = []
        for p in root.iter():
            if _local_name(p.tag) == "p":
                text = "".join(el.text or "" for el in p.iter() if _local_name(el.tag) == "t").strip()
                if text:
                    paras.append(text)
        if paras:
            sections.append(f"=== Slide {idx} ===\n" + "\n".join(paras))
    return "\n\n".join(sections), _extract_embedded_images(zf, "ppt/media/")


def _extract_odf(zf: zipfile.ZipFile) -> tuple[str, list[tuple[str, str]]]:
    root = ET.fromstring(zf.read("content.xml"))
    lines = [
        "".join(el.itertext()).strip()
        for el in root.iter()
        if _local_name(el.tag) in ("p", "h")
    ]
    return "\n".join(line for line in lines if line), _extract_embedded_images(zf, "Pictures/")


def _extract_office_content(path: Path) -> tuple[str, list[tuple[str, str]]]:
    """Extract text and `(mime, base64_data)` embedded images from ZIP-based Office/OpenDocument files."""
    suffix = path.suffix.lower()
    try:
        with zipfile.ZipFile(path) as zf:
            if suffix == ".docx":
                return _extract_docx(zf)
            if suffix == ".xlsx":
                return _extract_xlsx(zf)
            if suffix == ".pptx":
                return _extract_pptx(zf)
            return _extract_odf(zf)
    except (zipfile.BadZipFile, ET.ParseError, KeyError, OSError) as exc:
        raise LLMError(f"could not read {path.name}: {type(exc).__name__}") from exc


def _resolve_prompt(
    prompt_file: Path,
    row_date: date | None,
    sub_campaign: str,
    prompt_text: str | None = None,
) -> str:
    if prompt_text is not None:
        prompt = prompt_text.strip()
        if not prompt:
            raise LLMError("prompt is empty")
        return prompt
    prompt = prompt_file.read_text(encoding="utf-8-sig").strip()
    if not prompt:
        raise LLMError(f"{prompt_file.name} is empty")
    # The row's values reach the model only where the prompt asks for them.
    return (
        prompt.replace("{date}", row_date.isoformat() if row_date else "unknown")
        .replace("{sub_campaign}", sub_campaign or "unknown")
    )


def _api_error(response: httpx.Response) -> str:
    try:
        err = response.json()["error"]
        if isinstance(err, dict):
            return str(err["message"])[:300]
        return str(err)[:300]
    except (ValueError, KeyError, TypeError):
        return response.text[:300]


def _chat_completions_url(url: str) -> str:
    cleaned = url.strip().rstrip("/")
    if cleaned.endswith("/chat/completions"):
        return cleaned
    return f"{cleaned}/chat/completions"


class _BaseHTTPLLM:
    """Shared prompt validation, CSV retry (2 attempts), and HTTP retry (429/5xx) loop."""

    provider_name: str = "LLM"

    def __init__(
        self,
        model: str,
        prompt_file: Path = DEFAULT_PROMPT_FILE,
        *,
        timeout: float = 300.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        retry_delays: tuple[float, ...] = (5.0, 20.0),
        headers: dict[str, str] | None = None,
    ):
        self.model = model
        self.prompt_file = prompt_file
        self._sleep = sleep
        self._retry_delays = retry_delays
        self._http = httpx.Client(timeout=timeout, transport=transport, headers=headers)

    def generate_csv(
        self,
        row_date: date | None,
        sub_campaign: str,
        files: list[Path],
        *,
        prompt_text: str | None = None,
    ) -> str:
        body = self._request_body(row_date, sub_campaign, files, prompt_text=prompt_text)
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

    def _request_body(
        self,
        row_date: date | None,
        sub_campaign: str,
        files: list[Path],
        *,
        prompt_text: str | None = None,
    ) -> dict:
        raise NotImplementedError

    def _call(self, body: dict) -> str:
        raise NotImplementedError

    def _post_json(self, url: str, body: dict) -> dict:
        """POST JSON with retries on rate limits / 5xx and return the parsed JSON body."""
        for attempt in range(len(self._retry_delays) + 1):
            try:
                response = self._http.post(url, json=body)
            except httpx.HTTPError as exc:
                raise LLMError(f"could not reach {self.provider_name}: {type(exc).__name__}") from exc
            if response.status_code in RETRY_STATUSES and attempt < len(self._retry_delays):
                delay = self._retry_delays[attempt]
                log.warning("%s returned %s, retrying in %.0fs", self.provider_name, response.status_code, delay)
                self._sleep(delay)
                continue
            break
        if response.status_code != 200:
            raise LLMError(f"{self.provider_name} returned {response.status_code}: {_api_error(response)}")
        try:
            return response.json()
        except ValueError as exc:
            raise LLMError(f"{self.provider_name} returned invalid JSON") from exc


# -- OpenAI-compatible endpoints (NanoGPT, OpenAI, OpenRouter, etc.) ---------


def _openai_attachment_parts(path: Path) -> list[dict]:
    """Encode one attachment as one or more OpenAI-compatible chat content parts."""
    suffix = path.suffix.lower()
    mime = MIME_BY_SUFFIX.get(suffix)
    if mime is None:
        raise LLMError(f"unsupported attachment type: {path.name}")
    if suffix in TEXT_SUFFIXES:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        return [{"type": "text", "text": f"Attachment ({path.name}):\n{text}"}]
    if suffix in OFFICE_SUFFIXES:
        text, images = _extract_office_content(path)
        parts: list[dict] = [{"type": "text", "text": f"Attachment ({path.name}):\n{text}"}]
        parts.extend(
            {"type": "image_url", "image_url": {"url": f"data:{img_mime};base64,{img_b64}"}}
            for img_mime, img_b64 in images
        )
        return parts
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    data_url = f"data:{mime};base64,{encoded}"
    if suffix in IMAGE_SUFFIXES:
        return [{"type": "image_url", "image_url": {"url": data_url}}]
    return [{"type": "file", "file": {"filename": path.name, "file_data": data_url}}]


def _openai_reply_text(data: dict, provider_name: str = "OpenAI") -> str:
    choices = data.get("choices") or []
    if not choices:
        raise LLMError(f"{provider_name} returned no answer (no choices)")
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
        raise LLMError(f"{provider_name} stopped without an answer ({finish})")
    return text


class OpenAICompatibleLLM(_BaseHTTPLLM):
    """Sends attachments and prompt to any OpenAI-compatible `/chat/completions` endpoint."""

    def __init__(
        self,
        api_key: str,
        model: str = OPENAI_MODEL,
        prompt_file: Path = DEFAULT_PROMPT_FILE,
        *,
        url: str = OPENAI_URL,
        reasoning_effort: str | None = OPENAI_REASONING_EFFORT,
        provider_name: str = "OpenAI",
        timeout: float = 300.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        retry_delays: tuple[float, ...] = (5.0, 20.0),
    ):
        self.url = _chat_completions_url(url)
        self.reasoning_effort = reasoning_effort
        self.provider_name = provider_name
        super().__init__(
            model=model,
            prompt_file=prompt_file,
            timeout=timeout,
            transport=transport,
            sleep=sleep,
            retry_delays=retry_delays,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    def _request_body(
        self,
        row_date: date | None,
        sub_campaign: str,
        files: list[Path],
        *,
        prompt_text: str | None = None,
    ) -> dict:
        prompt = _resolve_prompt(self.prompt_file, row_date, sub_campaign, prompt_text=prompt_text)
        parts: list[dict] = []
        for path in files:
            parts.extend(_openai_attachment_parts(path))
        parts.append({"type": "text", "text": prompt})
        body: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_INSTRUCTION},
                {"role": "user", "content": parts},
            ],
        }
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        return body

    def _call(self, body: dict) -> str:
        return _openai_reply_text(self._post_json(self.url, body), self.provider_name)


class NanoGPTLLM(OpenAICompatibleLLM):
    """NanoGPT client (OpenAI-compatible endpoint, defaulting to Hydrogen High)."""

    def __init__(
        self,
        api_key: str,
        model: str = NANOGPT_MODEL,
        prompt_file: Path = DEFAULT_PROMPT_FILE,
        *,
        url: str = NANOGPT_URL,
        reasoning_effort: str | None = NANOGPT_REASONING_EFFORT,
        timeout: float = 300.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        retry_delays: tuple[float, ...] = (5.0, 20.0),
    ):
        super().__init__(
            api_key=api_key,
            model=model,
            prompt_file=prompt_file,
            url=url,
            reasoning_effort=reasoning_effort,
            provider_name="NanoGPT",
            timeout=timeout,
            transport=transport,
            sleep=sleep,
            retry_delays=retry_delays,
        )


# -- Google Gemini native API ------------------------------------------------


def _gemini_attachment_parts(path: Path) -> list[dict]:
    """Encode one attachment as one or more Google Gemini `generateContent` parts."""
    suffix = path.suffix.lower()
    mime = MIME_BY_SUFFIX.get(suffix)
    if mime is None:
        raise LLMError(f"unsupported attachment type: {path.name}")
    if suffix in TEXT_SUFFIXES:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        return [{"text": f"Attachment ({path.name}):\n{text}"}]
    if suffix in OFFICE_SUFFIXES:
        text, images = _extract_office_content(path)
        parts: list[dict] = [{"text": f"Attachment ({path.name}):\n{text}"}]
        parts.extend(
            {"inline_data": {"mime_type": img_mime, "data": img_b64}}
            for img_mime, img_b64 in images
        )
        return parts
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return [{"inline_data": {"mime_type": mime, "data": encoded}}]


def _gemini_reply_text(data: dict) -> str:
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


class GeminiLLM(_BaseHTTPLLM):
    """Sends attachments and prompt to the Google Gemini native `generateContent` API."""

    provider_name = "Gemini"

    def __init__(
        self,
        api_key: str,
        model: str = GEMINI_MODEL,
        prompt_file: Path = DEFAULT_PROMPT_FILE,
        *,
        thinking_level: str | None = GEMINI_THINKING_LEVEL,
        url_template: str = GEMINI_URL,
        timeout: float = 300.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        retry_delays: tuple[float, ...] = (5.0, 20.0),
    ):
        self.thinking_level = thinking_level
        self.url_template = url_template
        super().__init__(
            model=model,
            prompt_file=prompt_file,
            timeout=timeout,
            transport=transport,
            sleep=sleep,
            retry_delays=retry_delays,
            headers={"x-goog-api-key": api_key},
        )

    def _request_body(
        self,
        row_date: date | None,
        sub_campaign: str,
        files: list[Path],
        *,
        prompt_text: str | None = None,
    ) -> dict:
        prompt = _resolve_prompt(self.prompt_file, row_date, sub_campaign, prompt_text=prompt_text)
        parts: list[dict] = []
        for path in files:
            parts.extend(_gemini_attachment_parts(path))
        parts.append({"text": prompt})
        body: dict = {
            "system_instruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
            "contents": [{"role": "user", "parts": parts}],
        }
        if self.thinking_level:
            body["generationConfig"] = {
                "thinkingConfig": {"thinkingLevel": self.thinking_level},
            }
        return body

    def _call(self, body: dict) -> str:
        url = self.url_template.format(model=self.model)
        return _gemini_reply_text(self._post_json(url, body))


# -- Provider builders -------------------------------------------------------


def _openai_config_from_env(
    *,
    api_key_env: str,
    model_env: str,
    url_env: str,
    reasoning_effort_env: str,
    default_model: str,
    default_url: str,
    default_reasoning_effort: str | None,
    provider_name: str,
) -> tuple[str, str, str, str | None]:
    api_key = os.environ.get(api_key_env, "").strip()
    if not api_key:
        raise ConfigError(f"{api_key_env} is not set (set LLM=stub to run without {provider_name})")
    model = os.environ.get(model_env, "").strip() or default_model
    url = os.environ.get(url_env, "").strip() or default_url
    raw_effort = os.environ.get(reasoning_effort_env)
    reasoning_effort = (
        (raw_effort.strip() or None) if raw_effort is not None else default_reasoning_effort
    )
    return api_key, model, url, reasoning_effort


def openai_llm_from_env() -> OpenAICompatibleLLM:
    """Build a generic OpenAI-compatible chat completions client from environment variables."""
    api_key, model, url, reasoning_effort = _openai_config_from_env(
        api_key_env="OPENAI_API_KEY",
        model_env="OPENAI_MODEL",
        url_env="OPENAI_BASE_URL",
        reasoning_effort_env="OPENAI_REASONING_EFFORT",
        default_model=OPENAI_MODEL,
        default_url=OPENAI_URL,
        default_reasoning_effort=OPENAI_REASONING_EFFORT,
        provider_name="OpenAI",
    )
    return OpenAICompatibleLLM(
        api_key,
        model=model,
        url=url,
        reasoning_effort=reasoning_effort,
        provider_name="OpenAI",
    )


def nanogpt_llm_from_env() -> NanoGPTLLM:
    """Build the NanoGPT client (OpenAI-compatible endpoint) from environment variables."""
    api_key, model, url, reasoning_effort = _openai_config_from_env(
        api_key_env="NANOGPT_API_KEY",
        model_env="NANOGPT_MODEL",
        url_env="NANOGPT_URL",
        reasoning_effort_env="NANOGPT_REASONING_EFFORT",
        default_model=NANOGPT_MODEL,
        default_url=NANOGPT_URL,
        default_reasoning_effort=NANOGPT_REASONING_EFFORT,
        provider_name="NanoGPT",
    )
    return NanoGPTLLM(api_key, model=model, url=url, reasoning_effort=reasoning_effort)


def gemini_llm_from_env() -> GeminiLLM:
    """Build the Google Gemini native API client from environment variables."""
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise ConfigError("GEMINI_API_KEY is not set (set LLM=stub to run without Gemini)")
    model = os.environ.get("GEMINI_MODEL", "").strip() or GEMINI_MODEL
    raw_level = os.environ.get("GEMINI_THINKING_LEVEL")
    thinking_level = (
        (raw_level.strip() or None) if raw_level is not None else GEMINI_THINKING_LEVEL
    )
    return GeminiLLM(api_key, model=model, thinking_level=thinking_level)


def llm_from_env() -> LLM:
    """Build the configured LLM provider.

    Uses `LLM` from the environment if set, otherwise `LLM_PROVIDER` at the top of this file.
    """
    load_dotenv()
    choice = os.environ.get("LLM", LLM_PROVIDER).strip().lower() or LLM_PROVIDER.strip().lower()
    if choice == "stub":
        return StubLLM()
    builders: dict[str, Callable[[], LLM]] = {
        "nanogpt": nanogpt_llm_from_env,
        "openai": openai_llm_from_env,
        "gemini": gemini_llm_from_env,
    }
    builder = builders.get(choice)
    if builder is None:
        valid = ", ".join(repr(name) for name in (*builders, "stub"))
        raise ConfigError(f"LLM must be one of {valid}, got {choice!r}")
    client = builder()
    if not DEFAULT_PROMPT_FILE.is_file():
        raise ConfigError(f"{DEFAULT_PROMPT_FILE.name} is missing")
    validate_prompts_exist()
    return client
