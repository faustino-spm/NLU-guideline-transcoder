"""Test doubles shared by the pipeline and server tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from lark_base import (
    FIELD_ATTACHMENT,
    FIELD_DATE,
    FIELD_PROCESS,
    FIELD_SUBCAMPAIGN,
)

# 2026-03-01 00:00 at UTC+8
MARCH_1_MS = 1772294400000

_DEFAULT = object()


def record(record_id: str, *, ticked: bool = True, sub_campaign: str | None = "Class A",
           date_ms: int | None = MARCH_1_MS, attachments: Any = _DEFAULT) -> dict[str, Any]:
    """A record shaped like the list-records response (empty fields are omitted).

    `attachments` defaults to one PDF; pass None for a row without an attachment cell.
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
    return {"record_id": record_id, "fields": fields}


def attachment(token: str = "boxA", name: str = "guide.pdf", size: int = 1234,
               mime: str = "application/pdf") -> dict[str, Any]:
    return {"file_token": token, "name": name, "size": size, "type": mime,
            "url": f"https://example.invalid/{token}", "tmp_url": f"https://example.invalid/tmp/{token}"}


class FakeClient:
    """In-memory read-only stand-in for BaseClient. Records every download."""

    def __init__(self, records: list[dict[str, Any]], fail_downloads_for: set[str] | None = None):
        self.records = {r["record_id"]: r for r in records}
        self.downloads: list[str] = []
        self.fail_downloads_for = fail_downloads_for or set()
        self.app_token = "APP"

    def list_records(self, **_: Any) -> list[dict[str, Any]]:
        return [dict(r, fields=dict(r["fields"])) for r in self.records.values()]

    def download_media(self, file_token: str, dest: Path, extra: str | None = None) -> Path:
        if file_token in self.fail_downloads_for:
            raise RuntimeError(f"download refused for {file_token}")
        self.downloads.append(file_token)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(f"bytes of {file_token}".encode())
        return dest
