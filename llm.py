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
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Protocol

import httpx

from lark_base import ConfigError, load_dotenv
from prompts import validate_prompts_exist

log = logging.getLogger("llm")

DEFAULT_PROMPT_FILE = Path(__file__).parent / "prompt.txt"
NANOGPT_MODEL = "google/gemini-4-hydrogen"
NANOGPT_REASONING_EFFORT = "high"
NANOGPT_URL = "https://api.nano-gpt.com/api/v1/chat/completions"

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


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _extract_embedded_images(zf: zipfile.ZipFile, prefix: str) -> list[dict]:
    """Return image_url content parts for supported raster images stored under `prefix` in a ZIP archive."""
    parts: list[dict] = []
    for name in sorted(zf.namelist()):
        if not name.startswith(prefix) or name.endswith("/"):
            continue
        suffix = Path(name).suffix.lower()
        if suffix not in IMAGE_SUFFIXES:
            continue
        mime = MIME_BY_SUFFIX[suffix]
        encoded = base64.b64encode(zf.read(name)).decode("ascii")
        parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}})
    return parts


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


def _extract_docx(zf: zipfile.ZipFile) -> tuple[str, list[dict]]:
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


def _extract_xlsx(zf: zipfile.ZipFile) -> tuple[str, list[dict]]:
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


def _extract_pptx(zf: zipfile.ZipFile) -> tuple[str, list[dict]]:
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


def _extract_odf(zf: zipfile.ZipFile) -> tuple[str, list[dict]]:
    root = ET.fromstring(zf.read("content.xml"))
    lines = [
        "".join(el.itertext()).strip()
        for el in root.iter()
        if _local_name(el.tag) in ("p", "h")
    ]
    return "\n".join(line for line in lines if line), _extract_embedded_images(zf, "Pictures/")


def _extract_office_parts(path: Path) -> list[dict]:
    """Extract text and embedded images from ZIP-based Office/OpenDocument files."""
    suffix = path.suffix.lower()
    try:
        with zipfile.ZipFile(path) as zf:
            if suffix == ".docx":
                text, images = _extract_docx(zf)
            elif suffix == ".xlsx":
                text, images = _extract_xlsx(zf)
            elif suffix == ".pptx":
                text, images = _extract_pptx(zf)
            else:
                text, images = _extract_odf(zf)
    except (zipfile.BadZipFile, ET.ParseError, KeyError, OSError) as exc:
        raise LLMError(f"could not read {path.name}: {type(exc).__name__}") from exc
    parts: list[dict] = [{"type": "text", "text": f"Attachment ({path.name}):\n{text}"}]
    parts.extend(images)
    return parts


def _attachment_parts(path: Path) -> list[dict]:
    """Encode one attachment as one or more OpenAI-compatible chat content parts."""
    suffix = path.suffix.lower()
    mime = MIME_BY_SUFFIX.get(suffix)
    if mime is None:
        raise LLMError(f"unsupported attachment type: {path.name}")
    if suffix in TEXT_SUFFIXES:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        return [{"type": "text", "text": f"Attachment ({path.name}):\n{text}"}]
    if suffix in OFFICE_SUFFIXES:
        return _extract_office_parts(path)
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    data_url = f"data:{mime};base64,{encoded}"
    if suffix in IMAGE_SUFFIXES:
        return [{"type": "image_url", "image_url": {"url": data_url}}]
    return [{"type": "file", "file": {"filename": path.name, "file_data": data_url}}]


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
        if prompt_text is not None:
            prompt = prompt_text.strip()
            if not prompt:
                raise LLMError("prompt is empty")
        else:
            prompt = self.prompt_file.read_text(encoding="utf-8-sig").strip()
            if not prompt:
                raise LLMError(f"{self.prompt_file.name} is empty")
            # The row's values reach the model only where the prompt asks for them.
            prompt = (prompt.replace("{date}", row_date.isoformat() if row_date else "unknown")
                      .replace("{sub_campaign}", sub_campaign or "unknown"))
        parts: list[dict] = []
        for path in files:
            parts.extend(_attachment_parts(path))
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
    validate_prompts_exist()
    return NanoGPTLLM(api_key, NANOGPT_MODEL)
