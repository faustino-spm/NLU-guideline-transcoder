"""Test doubles shared by the pipeline and server tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from lark_base import (
    FIELD_ATTACHMENT,
    FIELD_DATE,
    FIELD_OUTPUT,
    FIELD_PROCESS,
    FIELD_STATUS,
    FIELD_SUBCAMPAIGN,
)

# 2026-03-01 00:00 at UTC+8
MARCH_1_MS = 1772294400000

_DEFAULT = object()


def record(record_id: str, *, ticked: bool = True, status: str | None = "Not Processed",
           sub_campaign: str | None = "Class A", date_ms: int | None = MARCH_1_MS,
           attachments: Any = _DEFAULT, output: Any = None) -> dict[str, Any]:
    """A record shaped like the list-records response (empty fields are omitted).

    `attachments` defaults to one PDF; pass None for a row without an attachment cell.
    `output` is the "Output CSV for NLU" cell, e.g. [attachment("t", "x.csv")].
    """
    fields: dict[str, Any] = {}
    if attachments is _DEFAULT:
        attachments = [attachment(f"tok-{record_id}", f"{record_id}.pdf")]
    if date_ms is not None:
        fields[FIELD_DATE] = date_ms
    if sub_campaign is not None:
        fields[FIELD_SUBCAMPAIGN] = sub_campaign
    if attachments is not None:
        fields[FIELD_ATTACHMENT] = attachments
    if ticked:
        fields[FIELD_PROCESS] = True
    if status is not None:
        fields[FIELD_STATUS] = status
    if output is not None:
        fields[FIELD_OUTPUT] = output
    return {"record_id": record_id, "fields": fields}


def attachment(token: str = "boxA", name: str = "guide.pdf", size: int = 1234,
               mime: str = "application/pdf") -> dict[str, Any]:
    return {"file_token": token, "name": name, "size": size, "type": mime,
            "url": f"https://example.invalid/{token}", "tmp_url": f"https://example.invalid/tmp/{token}"}


class FakeClient:
    """In-memory stand-in for BaseClient. Records every update, download and upload."""

    def __init__(self, records: list[dict[str, Any]], fail_updates_for: set[str] | None = None,
                 fail_downloads_for: set[str] | None = None, fail_uploads: bool = False):
        self.records = {r["record_id"]: r for r in records}
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self.downloads: list[str] = []
        self.uploads: dict[str, tuple[str, bytes]] = {}  # file token -> (file name, content)
        self.fail_updates_for = fail_updates_for or set()
        self.fail_downloads_for = fail_downloads_for or set()
        self.fail_uploads = fail_uploads
        self.app_token = "APP"

    def list_records(self, **_: Any) -> list[dict[str, Any]]:
        result = []
        for r in self.records.values():
            fields = dict(r["fields"])
            if isinstance(fields.get(FIELD_OUTPUT), list):
                enriched = []
                for item in fields[FIELD_OUTPUT]:
                    if isinstance(item, dict) and "name" not in item and item.get("file_token") in self.uploads:
                        name, content = self.uploads[item["file_token"]]
                        enriched.append({**item, "name": name, "size": len(content), "type": "text/csv"})
                    else:
                        enriched.append(item)
                fields[FIELD_OUTPUT] = enriched
            result.append({**r, "fields": fields})
        return result

    def update_record(self, record_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        if record_id in self.fail_updates_for:
            raise RuntimeError(f"update refused for {record_id}")
        self.updates.append((record_id, dict(fields)))
        self.records[record_id]["fields"].update(fields)
        return self.records[record_id]

    def download_media(self, file_token: str, dest: Path, extra: str | None = None) -> Path:
        if file_token in self.fail_downloads_for:
            raise RuntimeError(f"download refused for {file_token}")
        self.downloads.append(file_token)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(f"bytes of {file_token}".encode())
        return dest

    def upload_media(self, file_name: str, content: bytes) -> str:
        if self.fail_uploads:
            raise RuntimeError("upload refused")
        token = f"up{len(self.uploads) + 1}"
        self.uploads[token] = (file_name, content)
        return token
