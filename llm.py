"""LLM interface. Phase 1 uses StubLLM; Phase 2 adds a Gemini implementation behind the same interface."""

from __future__ import annotations

import csv
import io
from datetime import date
from pathlib import Path
from typing import Protocol


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
