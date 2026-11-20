"""Find the ticked rows of the Base grid and process them.

CLI:
    python pipeline.py --dry-run     # list the rows that would be processed, change nothing
    python pipeline.py --run         # process the ticked rows (Gemini, or the stub with LLM=stub)

Row lifecycle: ticked -> Queued (box unticked, so a second run cannot pick it up)
-> Integrated with the CSV attached in the "Output CSV for NLU" column, or Failed on any error.
A failed row does not stop the others.

All state lives in the grid, and nothing is kept on the machine that runs this, so it also works
on a host where each request may run on a different instance.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from lark_base import (
    FIELD_ATTACHMENT,
    FIELD_DATE,
    FIELD_OUTPUT,
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
    outputs: tuple[Attachment, ...]
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
        outputs=parse_attachments(fields.get(FIELD_OUTPUT)),
        process=fields.get(FIELD_PROCESS) is True,
        status=field_text(fields.get(FIELD_STATUS)),
    )


def all_rows(client: BaseClient) -> list[Row]:
    return [parse_row(record) for record in client.list_records()]


def ticked_rows(client: BaseClient) -> list[Row]:
    """Rows whose "Process Row?" box is ticked. Filtered here, not through the API."""
    return [row for row in all_rows(client) if row.process]


# -- processing --------------------------------------------------------------


@dataclass
class RowResult:
    record_id: str
    status: str  # STATUS_INTEGRATED or STATUS_FAILED
    detail: str = ""
    file_name: str | None = None  # the CSV attached to the row


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class RunReport:
    started_at: str = field(default_factory=_now)
    finished_at: str | None = None
    results: list[RowResult] = field(default_factory=list)
    remaining: int = 0  # rows still ticked at the end: not reached in time, or could not be claimed


def safe_name(name: str) -> str:
    """A file name that is safe on Windows and cannot escape its folder (names are user-supplied)."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(name.replace("\\", "/")).name).strip(" .")
    return cleaned or "file"


def csv_file_name(row: Row) -> str:
    return f"{row.date.isoformat() if row.date else 'no-date'}_{safe_name(row.record_id)}.csv"


def download_attachments(client: BaseClient, row: Row, folder: Path) -> list[Path]:
    """Download a row's attachments into `folder`. File names are prefixed with their position."""
    files: list[Path] = []
    for index, attachment in enumerate(row.attachments, start=1):
        dest = folder / f"{index}_{safe_name(attachment.name)}"
        files.append(client.download_media(attachment.file_token, dest))
    return files


def _fail(client: BaseClient, row: Row, exc: Exception) -> RowResult:
    detail = f"{type(exc).__name__}: {exc}"
    log.warning("row %s failed: %s", row.record_id, detail)
    try:
        client.update_record(row.record_id, {FIELD_STATUS: STATUS_FAILED})
    except Exception as status_exc:  # the grid could not be updated either
        detail += f" (and could not set Status=Failed: {status_exc})"
    return RowResult(row.record_id, STATUS_FAILED, detail)


def _process_row(client: BaseClient, llm: LLM, row: Row) -> RowResult:
    """Download, run the LLM, attach the CSV and mark the row Integrated. Any error marks it Failed.

    A re-run that fails leaves the row's previous CSV in place; the Status says Failed.
    """
    try:
        if not row.attachments:
            raise ValueError("row has no attachment")
        with tempfile.TemporaryDirectory(prefix="lark-row-") as tmp:
            files = download_attachments(client, row, Path(tmp))
            csv_text = llm.generate_csv(row.date, row.sub_campaign, files)
        if not csv_text.strip():
            raise ValueError("the LLM returned an empty CSV")
        name = csv_file_name(row)
        token = client.upload_media(name, csv_text.encode("utf-8"))
        client.update_record(row.record_id, {FIELD_STATUS: STATUS_INTEGRATED,
                                             FIELD_OUTPUT: [{"file_token": token}]})
        return RowResult(row.record_id, STATUS_INTEGRATED, f"{len(files)} file(s) read", name)
    except Exception as exc:  # one bad row must not stop the others
        return _fail(client, row, exc)


def run_ticked_rows(client: BaseClient, llm: LLM, budget_seconds: float | None = None,
                    clock: Callable[[], float] = time.monotonic) -> Iterator[RowResult]:
    """Process ticked rows one at a time, yielding each row's result as soon as it is done.

    The grid is read again before every row, so a row another run already claimed is not taken
    twice and a newly ticked row is picked up. A row is claimed (Queued, box unticked) just before
    it is processed. Once `budget_seconds` have passed no new row is started; rows not reached stay
    ticked for the next run. A row already started is not interrupted, so keep the budget well
    below the host's time limit.
    """
    deadline = None if budget_seconds is None else clock() + budget_seconds
    seen: set[str] = set()
    while deadline is None or clock() < deadline:
        row = next((r for r in ticked_rows(client) if r.record_id not in seen), None)
        if row is None:
            return
        seen.add(row.record_id)
        try:
            client.update_record(row.record_id, {FIELD_STATUS: STATUS_QUEUED, FIELD_PROCESS: False})
        except Exception as exc:  # the row stays ticked and is picked up by the next run
            yield RowResult(row.record_id, STATUS_FAILED, f"could not claim row: {exc}")
            continue
        yield _process_row(client, llm, row)


def process_ticked_rows(client: BaseClient, llm: LLM, budget_seconds: float | None = None,
                        report: RunReport | None = None) -> RunReport:
    """Run `run_ticked_rows` to the end and collect the results. Pass a `report` to fill your own."""
    report = report if report is not None else RunReport()
    try:
        for result in run_ticked_rows(client, llm, budget_seconds):
            report.results.append(result)
        report.remaining = len(ticked_rows(client))
    finally:
        report.finished_at = _now()
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
        where = f"  -> {result.file_name}" if result.file_name else ""
        print(f"  {result.record_id}  {result.status}  {result.detail}{where}".rstrip())
    if report.remaining:
        print(f"{report.remaining} row(s) are still ticked.")
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
    except (ConfigError, LarkApiError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
