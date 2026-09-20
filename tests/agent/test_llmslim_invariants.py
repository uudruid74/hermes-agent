from __future__ import annotations

from copy import deepcopy

from agent.context_compressor import ContextCompressor
from agent.internal_compression_fallback import (
    INTERNAL_FALLBACK_PREFIX,
    _ASSEMBLY_RESERVE_TOKENS,
    build_internal_fallback,
)
from agent.model_metadata import estimate_messages_tokens_rough
from tests.agent.test_compression_artifact_end_to_end import (
    _ARTIFACT,
    _BINDING_BUDGET,
    _payload as artifact_payload,
)


def _message(role: str, content, **extra) -> dict:
    return {"role": role, "content": content, **extra}


def _semantic_window() -> list[dict]:
    return [
        _message("system", "system prompt"),
        _message("user", "Implement the SQLite sessions migration."),
        _message("assistant", "The garden weather is sunny and warm."),
        _message("user", "Always preserve DATABASE_URL during the migration."),
        _message("assistant", "Create the sessions index before changing queries."),
        _message("user", "A recipe needs flour and butter."),
        _message("user", "How should we finish the SQLite sessions migration?"),
        _message("assistant", "Verify the sessions index and preserve DATABASE_URL."),
    ]


def test_semantic_fallback_is_byte_deterministic_across_ten_runs() -> None:
    results = [
        build_internal_fallback(
            deepcopy(_semantic_window()),
            protect_head_count=2,
            protect_last_n=2,
            target_tokens=190,
        )
        for _ in range(10)
    ]

    first = results[0]
    assert all(result == first for result in results[1:])
    assembled = [
        *_semantic_window()[: first.head_count],
        {"role": "assistant", "content": first.summary},
        *_semantic_window()[first.tail_start :],
    ]
    measured = estimate_messages_tokens_rough(assembled) + _ASSEMBLY_RESERVE_TOKENS
    assert first.selection_telemetry["assembled_tokens"] == measured
    assert first.selection_telemetry["over_budget"] is (measured > 190)


def test_artifact_lock_is_load_bearing_for_semantic_selection(monkeypatch) -> None:
    locked = build_internal_fallback(
        artifact_payload(),
        protect_head_count=1,
        protect_last_n=3,
        target_tokens=_BINDING_BUDGET,
    )
    assert _ARTIFACT in locked.summary

    monkeypatch.setattr(
        "agent.internal_compression_fallback._is_inviolable",
        lambda _unit: False,
    )
    unlocked = build_internal_fallback(
        artifact_payload(),
        protect_head_count=1,
        protect_last_n=3,
        target_tokens=_BINDING_BUDGET,
    )
    assert _ARTIFACT not in unlocked.summary


def test_multimodal_middle_and_tool_tail_preserve_assembly_topology() -> None:
    compressor = ContextCompressor(
        model="test/model",
        config_context_length=64_000,
        protect_first_n=1,
        protect_last_n=2,
        summary_target_ratio=0.2,
        internal_only=True,
        quiet_mode=True,
    )
    tool_call = {
        "id": "call-preserve",
        "type": "function",
        "function": {"name": "verify_schema", "arguments": '{"table":"sessions"}'},
    }
    messages = [
        _message("system", "system prompt"),
        _message("user", "Inspect the SQLite sessions migration."),
        _message(
            "assistant",
            [
                {"type": "text", "text": "Multimodal migration evidence names DATABASE_URL."},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
            ],
        ),
        *[
            _message(
                "user" if index % 2 == 0 else "assistant",
                f"Migration batch {index} keeps the sessions index. "
                + ("schema detail " * 80),
            )
            for index in range(10)
        ],
        _message("user", "Run the schema verification tool."),
        _message("assistant", "", tool_calls=[tool_call]),
        _message("tool", "sessions index verified", tool_call_id="call-preserve"),
        _message("assistant", "The sessions migration is verified."),
    ]
    original = deepcopy(messages)

    compressed = compressor.compress(messages)

    assert messages == original
    call_index = next(
        index
        for index, message in enumerate(compressed)
        if any(call.get("id") == "call-preserve" for call in message.get("tool_calls") or [])
    )
    assert compressed[call_index]["tool_calls"][0] == tool_call
    assert compressed[call_index + 1]["tool_call_id"] == "call-preserve"
    assert compressed[call_index + 1]["content"] == "sessions index verified"

    visible_roles = [
        message.get("role")
        for message in compressed
        if message.get("role") in {"user", "assistant"}
        and not (message.get("role") == "assistant" and message.get("tool_calls"))
    ]
    assert all(left != right for left, right in zip(visible_roles, visible_roles[1:]))


def test_repeated_compaction_does_not_nest_prior_payload_or_break_tool_ids() -> None:
    first = build_internal_fallback(
        _semantic_window(),
        protect_head_count=2,
        protect_last_n=2,
        target_tokens=190,
    )
    call = {
        "id": "call-repeat",
        "type": "function",
        "function": {"name": "check", "arguments": "{}"},
    }
    messages = [
        _message("system", "system prompt"),
        _message("user", "opening request"),
        _message("assistant", first.summary),
        _message("user", "Continue the sessions migration."),
        _message("assistant", "Keep DATABASE_URL unchanged."),
        _message("user", "Check it."),
        _message("assistant", "", tool_calls=[call]),
        _message("tool", "verified", tool_call_id="call-repeat"),
        _message("assistant", "Done."),
    ]

    second = build_internal_fallback(
        messages,
        protect_head_count=2,
        protect_last_n=2,
        target_tokens=220,
    )

    assert second.summary.count(INTERNAL_FALLBACK_PREFIX) == 1
    tail = messages[second.tail_start :]
    assistant_call = next(message for message in tail if message.get("tool_calls"))
    tool_result = next(message for message in tail if message.get("role") == "tool")
    assert assistant_call["tool_calls"][0]["id"] == tool_result["tool_call_id"]


def test_protected_area_does_not_duplicate_units_in_semantic_middle() -> None:
    messages = [
        _message("system", "system prompt"),
        _message("user", "opening request"),
        *[
            _message(
                "assistant" if index % 2 else "user",
                f"Dax compression architecture invariant {index % 7} "
                f"uses stable cache prefix topic {index}."
            )
            for index in range(270)
        ],
        _message("user", "Continue Dax compression architecture."),
        _message("assistant", "Keep the stable cache prefix."),
    ]

    result = build_internal_fallback(
        messages,
        protect_head_count=2,
        protect_last_n=2,
        target_tokens=2_000,
        session_subject="Dax",
    )

    assert "## Protected Context" in result.summary
    protected_body = result.summary.split("## Protected Context", 1)[1].split(
        "## Relevant Earlier Context", 1
    )[0]
    protected_lines = [line for line in protected_body.splitlines() if line.startswith("[")]
    assert protected_lines
    assert all(result.summary.count(line) == 1 for line in protected_lines)
