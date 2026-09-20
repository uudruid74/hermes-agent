from __future__ import annotations

from unittest.mock import patch

from agent.context_compressor import ContextCompressor
from agent.internal_compression_fallback import build_internal_fallback


def _message(role: str, content: str, **extra) -> dict:
    return {"role": role, "content": content, **extra}


def _window() -> list[dict]:
    return [
        _message("system", "system prompt"),
        _message("user", "opening request"),
        _message("assistant", "The garden weather is sunny and warm."),
        _message("user", "SQLite migration needs a new sessions index."),
        _message("assistant", "Always preserve DATABASE_URL during migration."),
        _message("assistant", "A recipe needs flour and butter."),
        _message("user", "How should we finish the SQLite sessions migration?"),
        _message("assistant", "Verify the sessions index and preserve DATABASE_URL."),
    ]


def test_default_ranked_middle_uses_semantic_selector_not_legacy_lexrank() -> None:
    messages = _window()

    with patch(
        "agent.internal_compression_fallback._lexrank",
        side_effect=AssertionError("legacy LexRank selected the middle"),
    ):
        result = build_internal_fallback(
            messages,
            protect_head_count=2,
            protect_last_n=2,
            target_tokens=190,
        )

    assert result.mode == "lexrank"
    assert "sessions index" in result.summary
    assert "garden weather" not in result.summary


def test_semantic_selector_changes_only_the_summary_middle_boundaries() -> None:
    messages = _window()
    original = [dict(message) for message in messages]

    result = build_internal_fallback(
        messages,
        protect_head_count=2,
        protect_last_n=2,
        target_tokens=190,
    )

    assert messages == original
    assert result.head_count == 2
    assert result.tail_start == len(messages) - 2
    assert messages[: result.head_count] == original[:2]
    assert messages[result.tail_start :] == original[-2:]


def test_plan_path_uses_the_same_semantic_middle_selector() -> None:
    messages = _window()

    with patch(
        "agent.internal_compression_fallback._lexrank",
        side_effect=AssertionError("legacy LexRank selected the plan middle"),
    ):
        result = build_internal_fallback(
            messages,
            protect_head_count=2,
            protect_last_n=2,
            target_tokens=230,
            plan_context="Task: migrate SQLite\nGoal: preserve DATABASE_URL\nStep 1/1",
            minimal_plan_context="Goal: preserve DATABASE_URL\nCurrent step: migration",
        )

    assert result.mode == "plan"
    assert "## Active Plan" in result.summary
    assert "## Relevant Earlier Context" in result.summary


def test_fallback_reports_retention_and_effectiveness_telemetry() -> None:
    result = build_internal_fallback(
        _window(),
        protect_head_count=2,
        protect_last_n=2,
        target_tokens=190,
    )

    stats = result.selection_telemetry
    assert stats["selector"] == "semantic"
    assert stats["instruction_units_found"] >= 1
    assert 0 <= stats["instruction_units_kept"] <= stats["instruction_units_found"]
    assert stats["entity_units_found"] >= 1
    assert 0 <= stats["entity_units_kept"] <= stats["entity_units_found"]
    assert stats["chunk_count"] >= 1
    assert stats["candidate_count"] >= stats["selected_count"] >= 1
    assert stats["selected_tokens"] > 0
    assert 0.0 < stats["achieved_ratio"] <= 1.0
    assert stats["no_op_reason"] is None
    assert isinstance(stats["over_budget"], bool)


def test_fallback_telemetry_reports_no_candidate_no_op() -> None:
    messages = [
        _message("system", "system prompt"),
        _message("user", "opening"),
        _message("user", "latest"),
        _message("assistant", "answer"),
    ]

    result = build_internal_fallback(
        messages,
        protect_head_count=2,
        protect_last_n=2,
        target_tokens=200,
    )

    assert result.selection_telemetry["selected_count"] == 0
    assert result.selection_telemetry["no_op_reason"] == "no_candidates"


def test_context_compressor_threads_fallback_selection_telemetry() -> None:
    compressor = ContextCompressor(
        model="test/model",
        config_context_length=64_000,
        protect_first_n=1,
        protect_last_n=2,
        summary_target_ratio=0.2,
        internal_only=True,
        quiet_mode=True,
    )
    messages = [
        _message("system", "system prompt"),
        _message("user", "opening request"),
        *[
            _message(
                "assistant" if index % 2 else "user",
                f"Always preserve DATABASE_URL in migration batch {index}. "
                + ("schema index detail " * 80),
            )
            for index in range(12)
        ],
        _message("user", "verify the current migration"),
        _message("assistant", "current result"),
    ]

    compressor.compress(messages)

    telemetry = compressor._last_compression_telemetry or {}
    stats = telemetry["fallback_selection"]
    assert stats["selector"] == "semantic"
    assert stats["candidate_count"] >= stats["selected_count"]
    assert "over_budget" in stats
