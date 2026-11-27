"""Composable disposition prompt registry and prompt assembly helpers.

Each disposition is defined as a modular building block under `prompts/` paired with:
- `prompts/_base.txt`: shared Phoenix AI 3 v3b 1:1 schema rules for direct attachment transcoding
- `prompts/_chained_base.txt`: Call 2 rules for deriving non-RPC dispositions on top of Call 1's CSV
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Sequence

from lark_base import ConfigError

PROMPTS_DIR = Path(__file__).parent / "prompts"
BASE_PROMPT_FILE = PROMPTS_DIR / "_base.txt"
CHAINED_BASE_PROMPT_FILE = PROMPTS_DIR / "_chained_base.txt"


@dataclass(frozen=True)
class PromptSpec:
    id: str
    label: str
    description: str
    path: Path
    rule_suffixes: tuple[str, ...]


PROMPTS: tuple[PromptSpec, ...] = (
    PromptSpec(
        id="rpc_ptp",
        label="RPC + PTP (1:1 Baseline)",
        description="Generates RIGHT_PARTY_CONTACT and PTP together from the client rubric with exact parity.",
        path=PROMPTS_DIR / "rpc_ptp.txt",
        rule_suffixes=("RIGHT_PARTY_CONTACT", "PTP"),
    ),
    PromptSpec(
        id="follow_up",
        label="Follow-Up / Broken Promise",
        description="Generates PTP_FOLLOW_UP: prunes initial negotiation, swaps in broken-promise & new-PTP checks.",
        path=PROMPTS_DIR / "follow_up.txt",
        rule_suffixes=("PTP_FOLLOW_UP",),
    ),
    PromptSpec(
        id="reminder",
        label="PTP / Payment Reminder",
        description="Generates PTP_REMINDER: prunes probing/negotiation, swaps in due-date & amount reminder checks.",
        path=PROMPTS_DIR / "reminder.txt",
        rule_suffixes=("PTP_REMINDER",),
    ),
    PromptSpec(
        id="claiming_paid",
        label="Claiming Paid / Fully Paid",
        description="Generates CLAIMING_PAID: prunes negotiation/PTP, swaps in settlement date/amount/channel/ref checks.",
        path=PROMPTS_DIR / "claiming_paid.txt",
        rule_suffixes=("CLAIMING_PAID",),
    ),
    PromptSpec(
        id="third_party_contact",
        label="Third-Party Contact (3PC)",
        description="Generates THIRD_PARTY_CONTACT: strips borrower PID/OB/PTP, enforces Data Privacy & 3PC callback probing.",
        path=PROMPTS_DIR / "third_party_contact.txt",
        rule_suffixes=("THIRD_PARTY_CONTACT",),
    ),
    PromptSpec(
        id="unrelated",
        label="Unrelated Party",
        description="Generates UNRELATED: stripped to opening, Data Privacy, callee ID, and apologize/disregard-call checks.",
        path=PROMPTS_DIR / "unrelated.txt",
        rule_suffixes=("UNRELATED",),
    ),
)

PROMPTS_BY_ID: dict[str, PromptSpec] = {spec.id: spec for spec in PROMPTS}
DEFAULT_PROMPT_IDS: tuple[str, ...] = ("rpc_ptp",)
# When 4+ blocks include rpc_ptp, Call 1 generates RPC+PTP from the attachment and
# Call 2 derives the non-RPC dispositions on top of Call 1's CSV to avoid drift and token cutoffs.
CHAIN_THRESHOLD = 4


def _flatten_prompt_ids(raw_ids: Sequence[str] | None) -> list[str]:
    if not raw_ids:
        return []
    tokens: list[str] = []
    for item in raw_ids:
        for part in item.split(","):
            cleaned = part.strip().lower()
            if cleaned:
                tokens.append(cleaned)
    return tokens


def resolve_prompts(raw_ids: Sequence[str] | None = None) -> tuple[PromptSpec, ...]:
    """Resolve user-supplied prompt IDs (comma-separated, repeated, or 'all') into canonical order."""
    tokens = _flatten_prompt_ids(raw_ids)
    if not tokens:
        return tuple(PROMPTS_BY_ID[pid] for pid in DEFAULT_PROMPT_IDS)
    if "all" in tokens:
        unknown = [t for t in tokens if t != "all" and t not in PROMPTS_BY_ID]
        if unknown:
            raise ValueError(f"Unknown prompt(s): {', '.join(unknown)}")
        return PROMPTS
    unknown = [t for t in tokens if t not in PROMPTS_BY_ID]
    if unknown:
        valid = ", ".join(PROMPTS_BY_ID)
        raise ValueError(f"Unknown prompt(s): {', '.join(unknown)}. Valid options: {valid}, all")
    selected = set(tokens)
    return tuple(spec for spec in PROMPTS if spec.id in selected)


def should_chain(specs: Sequence[PromptSpec]) -> bool:
    """True when selection should run as Call 1 (RPC+PTP) -> Call 2 (non-RPC built on Call 1 CSV)."""
    ids = {spec.id for spec in specs}
    return len(specs) >= CHAIN_THRESHOLD and "rpc_ptp" in ids


def validate_prompts_exist() -> None:
    """Raise ConfigError if any base or disposition prompt file is missing or empty."""
    root = Path(__file__).parent
    required = [BASE_PROMPT_FILE, CHAINED_BASE_PROMPT_FILE, *(spec.path for spec in PROMPTS)]
    missing = []
    for path in required:
        if not path.is_file():
            try:
                missing.append(str(path.relative_to(root)))
            except ValueError:
                missing.append(str(path))
    if missing:
        raise ConfigError(f"Missing prompt file(s): {', '.join(missing)}")


def _read_template(path: Path) -> str:
    text = path.read_text(encoding="utf-8-sig").strip()
    if not text:
        raise ConfigError(f"{path.name} is empty")
    return text


def _apply_placeholders(text: str, row_date: date | None, sub_campaign: str) -> str:
    return (
        text.replace("{date}", row_date.isoformat() if row_date else "unknown")
        .replace("{sub_campaign}", sub_campaign or "unknown")
    )


def _scope_section(specs: Sequence[PromptSpec]) -> str:
    lines = [
        "## Requested Dispositions",
        "Generate rules for **EXACTLY** the following disposition(s) in this single CSV, in this exact order. "
        "Do NOT generate any other dispositions:",
    ]
    index = 1
    for spec in specs:
        for suffix in spec.rule_suffixes:
            lines.append(f"{index}. `<SUB_CAMPAIGN_SLUG>_{suffix}`")
            index += 1
    return "\n".join(lines)


def compose_prompt(specs: Sequence[PromptSpec], row_date: date | None, sub_campaign: str) -> str:
    """Build a single-call prompt from _base.txt + the selected disposition blocks."""
    if not specs:
        raise ValueError("At least one prompt block must be selected")
    sections = [
        _read_template(BASE_PROMPT_FILE),
        _scope_section(specs),
        *(_read_template(spec.path) for spec in specs),
    ]
    return _apply_placeholders("\n\n".join(sections), row_date, sub_campaign)


def compose_chained_prompt(
    specs: Sequence[PromptSpec],
    baseline_csv: str,
    row_date: date | None,
    sub_campaign: str,
) -> str:
    """Build Call 2's prompt to derive non-RPC dispositions on top of Call 1's RPC+PTP CSV."""
    if not specs:
        raise ValueError("At least one non-RPC prompt block must be selected for chained derivation")
    sections = [
        _read_template(CHAINED_BASE_PROMPT_FILE),
        _scope_section(specs),
        *(_read_template(spec.path) for spec in specs),
        "## Stage 1 Baseline CSV (Locked Reference)\n" + baseline_csv.strip(),
    ]
    return _apply_placeholders("\n\n".join(sections), row_date, sub_campaign)
