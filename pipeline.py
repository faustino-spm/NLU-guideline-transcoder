"""Find the ticked rows of the Base grid and transcode their attachments into CSVs.

CLI:
    python pipeline.py --dry-run     # list the rows that would be processed, change nothing
    python pipeline.py --run         # process the ticked rows and save CSVs to output/

Lark Base access is read-only: nothing is written back to Lark and nothing is kept in memory
between requests, so the web service works on serverless hosts where each request may hit a
different instance.
"""

from __future__ import annotations

import argparse
import csv
import inspect
import io
import logging
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from lark_base import (
    FIELD_ATTACHMENT,
    FIELD_DATE,
    FIELD_PROCESS,
    FIELD_SUBCAMPAIGN,
    BaseClient,
    ConfigError,
    LarkApiError,
    client_from_env,
    field_text,
    ms_to_date,
)
from llm import LLM, llm_from_env
from prompts import (
    DEFAULT_PROMPT_IDS,
    PROMPTS,
    PromptSpec,
    compose_chained_prompt,
    compose_prompt,
    resolve_prompts,
    should_chain,
)

log = logging.getLogger("pipeline")

STATUS_DONE = "Done"
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
    process: bool


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
    )


def all_rows(client: BaseClient) -> list[Row]:
    return [parse_row(record) for record in client.list_records()]


def ticked_rows(client: BaseClient) -> list[Row]:
    """Rows whose "Process Row?" box is ticked. Filtered here, not through the API."""
    return [row for row in all_rows(client) if row.process]


# -- processing --------------------------------------------------------------


@dataclass(frozen=True)
class CsvSlice:
    rule_name: str
    file_name: str
    csv_text: str


@dataclass
class RowResult:
    record_id: str
    status: str  # STATUS_DONE or STATUS_FAILED
    detail: str = ""
    file_name: str | None = None
    csv_text: str | None = None
    date: date | None = None
    sub_campaign: str = ""
    prompt_ids: tuple[str, ...] = DEFAULT_PROMPT_IDS
    slices: list[CsvSlice] = field(default_factory=list)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class RunReport:
    started_at: str = field(default_factory=_now)
    finished_at: str | None = None
    results: list[RowResult] = field(default_factory=list)
    remaining: int = 0  # ticked rows not reached before the time budget ran out


def safe_name(name: str) -> str:
    """A file name that is safe on Windows and cannot escape its folder (names are user-supplied)."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(name.replace("\\", "/")).name).strip(" .")
    return cleaned or "file"


def csv_file_name(row: Row, suffix: str = "") -> str:
    base = f"{row.date.isoformat() if row.date else 'no-date'}_{safe_name(row.record_id)}"
    if suffix:
        return f"{base}_{safe_name(suffix)}.csv"
    return f"{base}.csv"


def _write_csv(header: list[str], data_rows: list[list[str]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(data_rows)
    return buf.getvalue()


def merge_csvs(csv_texts: Sequence[str]) -> str:
    """Concatenate multiple CSVs that share the same header into one RFC-4180 CSV."""
    header: list[str] | None = None
    merged_rows: list[list[str]] = []
    for text in csv_texts:
        parsed = [r for r in csv.reader(io.StringIO(text)) if r]
        if len(parsed) < 2:
            raise ValueError("cannot merge CSV with no data rows")
        if header is None:
            header = parsed[0]
        elif parsed[0] != header:
            raise ValueError("CSV headers do not match across chained calls")
        merged_rows.extend(parsed[1:])
    if header is None:
        raise ValueError("no CSVs to merge")
    return _write_csv(header, merged_rows)


def slice_csv_by_rule_name(row: Row, csv_text: str) -> list[CsvSlice]:
    """Split a Phoenix AI 3 rule CSV into one CsvSlice per distinct `Rule Name` (column 1).

    Returns an empty list when the CSV does not have a `Rule Name` first column (e.g., StubLLM).
    """
    parsed = [r for r in csv.reader(io.StringIO(csv_text)) if r]
    if len(parsed) < 2 or not parsed[0] or parsed[0][0].strip().lower() != "rule name":
        return []
    header = parsed[0]
    grouped: dict[str, list[list[str]]] = {}
    for data_row in parsed[1:]:
        rule_name = data_row[0].strip() if data_row else ""
        if not rule_name:
            continue
        grouped.setdefault(rule_name, []).append(data_row)
    return [
        CsvSlice(
            rule_name=rule_name,
            file_name=csv_file_name(row, rule_name),
            csv_text=_write_csv(header, group_rows),
        )
        for rule_name, group_rows in grouped.items()
    ]


def _call_llm(llm: LLM, row_date: date | None, sub_campaign: str, files: list[Path], prompt_text: str) -> str:
    params = inspect.signature(llm.generate_csv).parameters
    accepts_prompt = "prompt_text" in params or any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
    )
    if accepts_prompt:
        return llm.generate_csv(row_date, sub_campaign, files, prompt_text=prompt_text)
    return llm.generate_csv(row_date, sub_campaign, files)


def download_attachments(client: BaseClient, row: Row, folder: Path) -> list[Path]:
    """Download a row's attachments into `folder`. File names are prefixed with their position."""
    files: list[Path] = []
    for index, attachment in enumerate(row.attachments, start=1):
        dest = folder / f"{index}_{safe_name(attachment.name)}"
        files.append(client.download_media(attachment.file_token, dest))
    return files


def _process_row(
    client: BaseClient,
    llm: LLM,
    row: Row,
    selected_prompts: tuple[PromptSpec, ...] | None = None,
) -> RowResult:
    """Download attachments and run the LLM to produce a CSV (and per-disposition slices). Never writes to Lark."""
    specs = selected_prompts if selected_prompts is not None else resolve_prompts(None)
    prompt_ids = tuple(spec.id for spec in specs)
    try:
        if not row.attachments:
            raise ValueError("row has no attachment")
        with tempfile.TemporaryDirectory(prefix="lark-row-") as tmp:
            files = download_attachments(client, row, Path(tmp))
            if should_chain(specs):
                rpc_ptp_spec = next(spec for spec in specs if spec.id == "rpc_ptp")
                non_rpc_specs = tuple(spec for spec in specs if spec.id != "rpc_ptp")
                baseline_prompt = compose_prompt((rpc_ptp_spec,), row.date, row.sub_campaign)
                baseline_csv = _call_llm(llm, row.date, row.sub_campaign, files, baseline_prompt)
                if not baseline_csv.strip():
                    raise ValueError("the LLM returned an empty CSV")
                chained_prompt = compose_chained_prompt(non_rpc_specs, baseline_csv, row.date, row.sub_campaign)
                derived_csv = _call_llm(llm, row.date, row.sub_campaign, [], chained_prompt)
                if not derived_csv.strip():
                    raise ValueError("the LLM returned an empty CSV")
                csv_text = merge_csvs((baseline_csv, derived_csv))
                detail = f"{len(files)} file(s) read (chained 2 calls)"
            else:
                prompt_text = compose_prompt(specs, row.date, row.sub_campaign)
                csv_text = _call_llm(llm, row.date, row.sub_campaign, files, prompt_text)
                if not csv_text.strip():
                    raise ValueError("the LLM returned an empty CSV")
                detail = f"{len(files)} file(s) read"
        slices = slice_csv_by_rule_name(row, csv_text)
        if slices:
            detail = f"{detail} · {len(slices)} disposition(s)"
        name = csv_file_name(row)
        return RowResult(
            row.record_id,
            STATUS_DONE,
            detail,
            name,
            csv_text,
            row.date,
            row.sub_campaign,
            prompt_ids=prompt_ids,
            slices=slices,
        )
    except Exception as exc:  # one bad row must not stop the others
        detail = f"{type(exc).__name__}: {exc}"
        log.warning("row %s failed: %s", row.record_id, detail)
        return RowResult(
            row.record_id,
            STATUS_FAILED,
            detail,
            date=row.date,
            sub_campaign=row.sub_campaign,
            prompt_ids=prompt_ids,
        )


def run_ticked_rows(
    client: BaseClient,
    llm: LLM,
    budget_seconds: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    selected_prompts: tuple[PromptSpec, ...] | None = None,
) -> Iterator[RowResult]:
    """Process ticked rows one at a time, yielding each row's result as soon as it is done.

    Lark is read-only, so `Process Row?` stays ticked after a run. Once `budget_seconds` have
    passed no new row is started; a row already started is not interrupted, so keep the budget
    well below the host's time limit.
    """
    deadline = None if budget_seconds is None else clock() + budget_seconds
    for row in ticked_rows(client):
        if deadline is not None and clock() >= deadline:
            return
        yield _process_row(client, llm, row, selected_prompts=selected_prompts)


def process_ticked_rows(
    client: BaseClient,
    llm: LLM,
    budget_seconds: float | None = None,
    report: RunReport | None = None,
    selected_prompts: tuple[PromptSpec, ...] | None = None,
) -> RunReport:
    """Run `run_ticked_rows` to the end and collect the results. Pass a `report` to fill your own."""
    report = report if report is not None else RunReport()
    try:
        for result in run_ticked_rows(client, llm, budget_seconds, selected_prompts=selected_prompts):
            report.results.append(result)
        report.remaining = max(0, len(ticked_rows(client)) - len(report.results))
    finally:
        report.finished_at = _now()
    return report


# -- CLI ---------------------------------------------------------------------


def describe(row: Row) -> str:
    files = ", ".join(f"{a.name} ({a.size} bytes)" for a in row.attachments) or "no attachment"
    return (f"{row.record_id}  {row.date.isoformat() if row.date else '-'}  "
            f"{row.sub_campaign or '-'}  {files}")


def cmd_dry_run(client: BaseClient) -> int:
    rows = ticked_rows(client)
    print(f"{len(rows)} row(s) would be processed.")
    for row in rows:
        print("  " + describe(row))
    return 0


def cmd_list_prompts() -> int:
    print("Available disposition prompt blocks:")
    for spec in PROMPTS:
        print(f"  {spec.id:<20} {spec.label} — {spec.description}")
    return 0


def cmd_run(
    client: BaseClient,
    llm: LLM,
    output_dir: Path = Path("output"),
    selected_prompts: tuple[PromptSpec, ...] | None = None,
) -> int:
    report = process_ticked_rows(client, llm, selected_prompts=selected_prompts)
    failed = [r for r in report.results if r.status == STATUS_FAILED]
    print(f"Processed {len(report.results)} row(s): "
          f"{len(report.results) - len(failed)} {STATUS_DONE}, {len(failed)} {STATUS_FAILED}.")
    for result in report.results:
        where = ""
        if result.status == STATUS_DONE and result.file_name and result.csv_text is not None:
            output_dir.mkdir(parents=True, exist_ok=True)
            saved_paths: list[str] = []
            dest = output_dir / result.file_name
            dest.write_text(result.csv_text, encoding="utf-8")
            saved_paths.append(str(dest))
            for csv_slice in result.slices:
                slice_dest = output_dir / csv_slice.file_name
                slice_dest.write_text(csv_slice.csv_text, encoding="utf-8")
                saved_paths.append(str(slice_dest))
            where = "  -> " + ", ".join(saved_paths)
        print(f"  {result.record_id}  {result.status}  {result.detail}{where}".rstrip())
    if report.remaining:
        print(f"{report.remaining} ticked row(s) were not reached.")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Process the ticked rows of the Base grid.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="list ticked rows, change nothing")
    mode.add_argument("--run", action="store_true", help="process the ticked rows and save CSVs locally")
    mode.add_argument("--list-prompts", action="store_true", help="list available disposition prompt blocks")
    parser.add_argument("--output-dir", type=Path, default=Path("output"),
                        help="directory for generated CSV files (default: output)")
    parser.add_argument(
        "--prompt",
        action="append",
        dest="prompts",
        help="disposition prompt ID (repeatable, comma-separated, or 'all'; default: rpc_ptp)",
    )
    args = parser.parse_args(argv)
    if args.list_prompts:
        return cmd_list_prompts()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        selected = resolve_prompts(args.prompts)
        client = client_from_env()
        return cmd_dry_run(client) if args.dry_run else cmd_run(client, llm_from_env(), args.output_dir, selected)
    except (ConfigError, LarkApiError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
