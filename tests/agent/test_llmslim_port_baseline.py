"""Contract checks for the fixed LLMSlim port baseline corpus."""

from __future__ import annotations

import json
from pathlib import Path

from tests.agent.llmslim_port_corpus import compression_cases


_BASELINE = Path(__file__).parent / "fixtures" / "llmslim_legacy_baseline.json"
_REQUIRED_METRICS = {
    "original_tokens",
    "assembled_tokens",
    "summary_tokens",
    "dropped_messages",
    "instruction_found",
    "instruction_kept",
    "entity_found",
    "entity_kept",
    "fallback_mode",
}


def test_corpus_is_sanitized_and_covers_required_shapes() -> None:
    cases = compression_cases()
    assert {case.name for case in cases} == {
        "lowercase_machine_output",
        "mixed_density",
        "active_plan",
        "artifact_and_reingested_payload",
        "multilingual",
        "real_session_shape",
    }
    for case in cases:
        assert case.messages[0]["role"] == "system"
        assert case.target_tokens > 0
        assert case.instruction_markers
        assert case.entity_markers
        serialized = json.dumps(case.messages, ensure_ascii=False)
        assert "/home/ekl" not in serialized
        assert "state.db" not in serialized
        assert "api_key" not in serialized.casefold()


def test_legacy_baseline_records_each_case_and_quality_denominators() -> None:
    baseline = json.loads(_BASELINE.read_text(encoding="utf-8"))
    assert baseline["selector"] == "legacy_lexrank"
    assert baseline["source_commit"]
    assert set(baseline["cases"]) == {case.name for case in compression_cases()}
    for case in compression_cases():
        metrics = baseline["cases"][case.name]
        assert _REQUIRED_METRICS <= metrics.keys()
        assert metrics["instruction_found"] == len(case.instruction_markers)
        assert metrics["entity_found"] == len(case.entity_markers)
        assert 0 <= metrics["instruction_kept"] <= metrics["instruction_found"]
        assert 0 <= metrics["entity_kept"] <= metrics["entity_found"]
        assert metrics["assembled_tokens"] > 0
        assert metrics["original_tokens"] > 0
