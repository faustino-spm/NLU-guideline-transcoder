from datetime import date

import pytest

import prompts
from lark_base import ConfigError


def test_all_registered_prompt_files_exist_and_are_non_empty():
    prompts.validate_prompts_exist()
    for spec in prompts.PROMPTS:
        assert spec.path.read_text(encoding="utf-8-sig").strip()


def test_resolve_prompts_defaults_to_rpc_ptp():
    resolved = prompts.resolve_prompts(None)
    assert [p.id for p in resolved] == ["rpc_ptp"]
    assert [p.id for p in prompts.resolve_prompts([])] == ["rpc_ptp"]


def test_resolve_prompts_handles_commas_repeats_dedup_and_canonical_order():
    resolved = prompts.resolve_prompts(["unrelated,claiming_paid", "rpc_ptp", "unrelated"])
    assert [p.id for p in resolved] == ["rpc_ptp", "claiming_paid", "unrelated"]


def test_resolve_prompts_all_returns_every_block():
    assert prompts.resolve_prompts(["all"]) == prompts.PROMPTS


def test_resolve_prompts_rejects_unknown_id():
    with pytest.raises(ValueError, match="Unknown prompt"):
        prompts.resolve_prompts(["rpc_ptp", "nonexistent"])


def test_should_chain_requires_four_or_more_blocks_including_rpc_ptp():
    assert not prompts.should_chain(prompts.resolve_prompts(["rpc_ptp"]))
    assert not prompts.should_chain(prompts.resolve_prompts(["rpc_ptp", "claiming_paid", "unrelated"]))
    # 5 non-RPC blocks without rpc_ptp still run in a single composed call
    assert not prompts.should_chain(
        prompts.resolve_prompts(["follow_up", "reminder", "claiming_paid", "third_party_contact", "unrelated"])
    )
    assert prompts.should_chain(
        prompts.resolve_prompts(["rpc_ptp", "follow_up", "claiming_paid", "unrelated"])
    )
    assert prompts.should_chain(prompts.resolve_prompts(["all"]))


def test_compose_prompt_substitutes_placeholders_and_lists_requested_dispositions():
    specs = prompts.resolve_prompts(["rpc_ptp", "claiming_paid"])
    text = prompts.compose_prompt(specs, date(2026, 11, 27), "TALA EDC")
    assert "TALA EDC" in text
    assert "2026-11-27" in text
    assert "1. `<SUB_CAMPAIGN_SLUG>_RIGHT_PARTY_CONTACT`" in text
    assert "2. `<SUB_CAMPAIGN_SLUG>_PTP`" in text
    assert "3. `<SUB_CAMPAIGN_SLUG>_CLAIMING_PAID`" in text
    assert "settlement_date_intent" in text


def test_compose_chained_prompt_embeds_stage1_baseline_csv():
    specs = prompts.resolve_prompts(["claiming_paid", "unrelated"])
    baseline = "Rule Name,Placement Name\nTALA_EDC_PTP,default_placement\n"
    text = prompts.compose_chained_prompt(specs, baseline, date(2026, 11, 27), "TALA EDC")
    assert "Stage 2 (Baseline-Locked Disposition Derivation)" in text
    assert "TALA_EDC_PTP,default_placement" in text
    assert "1. `<SUB_CAMPAIGN_SLUG>_CLAIMING_PAID`" in text
    assert "2. `<SUB_CAMPAIGN_SLUG>_UNRELATED`" in text
    assert "disregard_call_intent" in text


def test_validate_prompts_exist_raises_config_error_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(prompts, "BASE_PROMPT_FILE", tmp_path / "missing_base.txt")
    with pytest.raises(ConfigError, match="Missing prompt file"):
        prompts.validate_prompts_exist()
