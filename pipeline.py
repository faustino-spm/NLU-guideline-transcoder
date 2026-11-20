"""Find the ticked rows of the Base grid and process them.

CLI:
    python pipeline.py --dry-run     # list the rows that would be processed, change nothing
    python pipeline.py --run         # process the ticked rows (Gemini, or the stub with LLM=stub)

Row lifecycle: ticked -> Queued (box unticked, so a second run cannot pick it up)
-> Integrated, or Failed on any error. A failed row does not stop the others.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from lark_base import (
    FIELD_ATTACHMENT,
    FIELD_DATE,
    FIELD_PROCESS,
    FIELD_STATUS,
    FIELD_SUBCAMPAIGN,
    BaseClient,
    ConfigError,
    LarkApiError,
    client_from_env,
    field_text,
    ms_to_date,
)
from llm import LLM, llm_from_env

log = logging.getLogger("pipeline")

STATUS_NOT_PROCESSED = "Not Processed"
STATUS_QUEUED = "Queued"
STATUS_INTEGRATED = "Integrated"
STATUS_FAILED = "Failed"

DEFAULT_OUTPUT_DIR = Path("output")

@dataclass(frozen=True)
class Attachment:
    file_token: str
    name: str
    size: int
    mime_type: str


@dataclass(frozen=True)
class Row:
    record_id: str
    date: date | None
    sub_campaign: str
    attachments: tuple[Attachment, ...]
    process: bool
    status: str


def parse_attachments(value: Any) -> tuple[Attachment, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        Attachment(
            file_token=str(item["file_token"]),
            name=str(item.get("name") or item["file_token"]),
            size=int(item.get("size") or 0),
            mime_type=str(item.get("type") or ""),
        )
        for item in value
        if isinstance(item, dict) and item.get("file_token")
    )


def parse_row(record: dict[str, Any]) -> Row:
    fields = record.get("fields") or {}
    raw_date = fields.get(FIELD_DATE)
    return Row(
        record_id=str(record["record_id"]),
        date=ms_to_date(raw_date) if isinstance(raw_date, (int, float)) else None,
        sub_campaign=field_text(fields.get(FIELD_SUBCAMPAIGN)),
        attachments=parse_attachments(fields.get(FIELD_ATTACHMENT)),
        process=fields.get(FIELD_PROCESS) is True,
        status=field_text(fields.get(FIELD_STATUS)),
    )


def ticked_rows(client: BaseClient) -> list[Row]:
    """Rows whose "Process Row?" box is ticked. Filtered here, not through the API."""
    rows = (parse_row(record) for record in client.list_records())
    return [row for row in rows if row.process]


# -- processing --------------------------------------------------------------


class AlreadyRunning(RuntimeError):
    """A run is in progress; only one may run at a time."""


@dataclass
class RowResult:
    record_id: str
    status: str  # STATUS_INTEGRATED or STATUS_FAILED
    detail: str = ""
    output_path: str | None = None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class RunReport:
    started_at: str = field(default_factory=_now)
    finished_at: str | None = None
    results: list[RowResult] = field(default_factory=list)
    error: str | None = None  # set when the whole run failed, e.g. the grid could not be read


RUN_LOCK = threading.Lock()


def safe_name(name: str) -> str:
    """A file name that is safe on Windows and cannot escape its folder (names are user-supplied)."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(name.replace("\\", "/")).name).strip(" .")
    return cleaned or "file"


def download_attachments(client: BaseClient, row: Row, folder: Path) -> list[Path]:
    """Download a row's attachments into `folder`. File names are prefixed with their position."""
    files: list[Path] = []
    for index, attachment in enumerate(row.attachments, start=1):
        dest = folder / f"{index}_{safe_name(attachment.name)}"
        files.append(client.download_media(attachment.file_token, dest))
    return files


def save_csv(output_dir: Path, row: Row, csv_text: str) -> Path:
    """Write the row's CSV to output/<date>_<record_id>.csv, replacing a previous run's file."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{row.date.isoformat() if row.date else 'no-date'}_{safe_name(row.record_id)}.csv"
    path.write_text(csv_text, encoding="utf-8")
    return path


def _fail(client: BaseClient, row: Row, exc: Exception) -> RowResult:
    detail = f"{type(exc).__name__}: {exc}"
    log.warning("row %s failed: %s", row.record_id, detail)
    try:
        client.update_record(row.record_id, {FIELD_STATUS: STATUS_FAILED})
    except Exception as status_exc:  # the grid could not be updated either
        detail += f" (and could not set Status=Failed: {status_exc})"
    return RowResult(row.record_id, STATUS_FAILED, detail)


def _process_row(client: BaseClient, llm: LLM, row: Row, output_dir: Path) -> RowResult:
    try:
        if not row.attachments:
            raise ValueError("row has no attachment")
        with tempfile.TemporaryDirectory(prefix="lark-row-") as tmp:
            files = download_attachments(client, row, Path(tmp))
            csv_text = llm.generate_csv(row.date, row.sub_campaign, files)
        if not csv_text.strip():
            raise ValueError("the LLM returned an empty CSV")
        path = save_csv(output_dir, row, csv_text)
        client.update_record(row.record_id, {FIELD_STATUS: STATUS_INTEGRATED})
        return RowResult(row.record_id, STATUS_INTEGRATED, f"{len(files)} file(s) read", str(path))
    except Exception as exc:  # one bad row must not stop the others
        return _fail(client, row, exc)


def process_ticked_rows(client: BaseClient, llm: LLM, report: RunReport | None = None,
                        lock: threading.Lock = RUN_LOCK,
                        output_dir: Path = DEFAULT_OUTPUT_DIR) -> RunReport:
    """Claim every ticked row, then process them one by one.

    Each row's attachments are downloaded, passed to the LLM, and the returned CSV is saved
    in `output_dir`. Pass a `report` to watch the results fill in while the run is in progress.
    Raises AlreadyRunning if another run holds the lock.
    """
    if not lock.acquire(blocking=False):
        raise AlreadyRunning("A run is already in progress.")
    report = report if report is not None else RunReport()
    try:
        queued: list[Row] = []
        for row in ticked_rows(client):
            try:
                client.update_record(row.record_id, {FIELD_STATUS: STATUS_QUEUED, FIELD_PROCESS: False})
                queued.append(row)
            except Exception as exc:  # row stays ticked and is picked up by the next run
                report.results.append(RowResult(row.record_id, STATUS_FAILED, f"could not claim row: {exc}"))
        for row in queued:
            report.results.append(_process_row(client, llm, row, output_dir))
    finally:
        report.finished_at = _now()
        lock.release()
    return report


# -- CLI ---------------------------------------------------------------------


def describe(row: Row) -> str:
    files = ", ".join(f"{a.name} ({a.size} bytes)" for a in row.attachments) or "no attachment"
    return (f"{row.record_id}  {row.date.isoformat() if row.date else '-'}  "
            f"{row.sub_campaign or '-'}  [{row.status or 'no status'}]  {files}")


def cmd_dry_run(client: BaseClient) -> int:
    rows = ticked_rows(client)
    print(f"{len(rows)} row(s) would be processed.")
    for row in rows:
        print("  " + describe(row))
    return 0


def cmd_run(client: BaseClient, llm: LLM) -> int:
    report = process_ticked_rows(client, llm)
    failed = [r for r in report.results if r.status == STATUS_FAILED]
    print(f"Processed {len(report.results)} row(s): "
          f"{len(report.results) - len(failed)} {STATUS_INTEGRATED}, {len(failed)} {STATUS_FAILED}.")
    for result in report.results:
        where = f"  -> {result.output_path}" if result.output_path else ""
        print(f"  {result.record_id}  {result.status}  {result.detail}{where}".rstrip())
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Process the ticked rows of the Base grid.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="list ticked rows, change nothing")
    mode.add_argument("--run", action="store_true", help="process the ticked rows")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        client = client_from_env()
        return cmd_dry_run(client) if args.dry_run else cmd_run(client, llm_from_env())
    except (ConfigError, LarkApiError, AlreadyRunning) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
