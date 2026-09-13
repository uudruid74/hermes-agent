from __future__ import annotations

from agent.context_compressor import ContextCompressor
from agent.internal_compression_fallback import build_internal_fallback


def _message(role: str, content: str) -> dict:
    return {"role": role, "content": content}


def test_active_plan_fallback_keeps_continue_context_and_fixed_tail():
    messages = [
        _message("system", "system prompt"),
        _message("user", "opening request"),
        *[
            _message("assistant" if index % 2 else "user", f"old middle {index}")
            for index in range(8)
        ],
        _message("user", "recent question"),
        _message("assistant", "recent answer"),
    ]
    plan = (
        "Task: Emergency compression\nStatus: manual\nGoal: Keep work moving\n"
        "Step 2/3\n\n    Step 1: inspect\n  → Step 2: implement\n"
        "      Summary: implementation is half complete"
    )

    fallback = build_internal_fallback(
        messages,
        protect_head_count=2,
        protect_last_n=2,
        target_tokens=2_000,
        plan_context=plan,
        minimal_plan_context=(
            "Goal: Keep work moving\nCurrent step 2/3: implement\n"
            "Summary: implementation is half complete"
        ),
    )

    assert fallback is not None
    assert fallback.mode == "plan"
    assert fallback.head_count == 2
    assert fallback.tail_start == len(messages) - 2
    assert plan in fallback.summary
    assert "verbatim" in fallback.summary.lower()


def test_active_plan_fallback_degrades_to_goal_current_step_and_summary():
    messages = [
        _message("system", "system prompt"),
        _message("user", "opening request"),
        *[_message("assistant", "old " * 200) for _ in range(5)],
        _message("user", "recent " * 400),
    ]
    full = (
        "Task: Huge plan\nGoal: Recover safely\nStep 2/20\n"
        + "EXTRA STEP DETAIL " * 500
    )
    minimal = (
        "Goal: Recover safely\nCurrent step 2/20: implement fallback\n"
        "Summary: tests are written"
    )

    fallback = build_internal_fallback(
        messages,
        protect_head_count=2,
        protect_last_n=1,
        target_tokens=120,
        plan_context=full,
        minimal_plan_context=minimal,
    )

    assert fallback is not None
    assert fallback.mode == "plan-minimal"
    assert fallback.head_count == 1
    assert fallback.tail_start == len(messages)
    assert minimal in fallback.summary
    assert "EXTRA STEP DETAIL" not in fallback.summary
    assert "verbatim" not in fallback.summary.lower()


def test_active_plan_fallback_fails_when_minimal_context_cannot_fit():
    messages = [_message("system", "system prompt")]

    fallback = build_internal_fallback(
        messages,
        protect_head_count=1,
        protect_last_n=0,
        target_tokens=1,
        plan_context="Goal: work",
        minimal_plan_context="Goal: work\nCurrent step: impossible to fit",
    )

    assert fallback is None


def test_lexrank_fallback_keeps_relevant_middle_in_chronological_order():
    messages = [
        _message("system", "system prompt"),
        _message("user", "opening request"),
        _message("assistant", "The garden weather is sunny and warm."),
        _message("user", "SQLite migration needs a new sessions index."),
        _message("assistant", "Add the sessions index before changing queries."),
        _message("user", "A recipe needs flour and butter."),
        _message("user", "How should we finish the SQLite sessions migration?"),
        _message("assistant", "Keep the migration small and verify the index."),
    ]

    fallback = build_internal_fallback(
        messages,
        protect_head_count=2,
        protect_last_n=2,
        target_tokens=200,
    )

    assert fallback is not None
    assert fallback.mode == "lexrank"
    assert "SQLite migration needs" in fallback.summary
    assert "sessions index before" in fallback.summary
    assert fallback.summary.index("SQLite migration needs") < fallback.summary.index(
        "sessions index before"
    )
    assert "garden weather" not in fallback.summary
    assert fallback.tail_start == len(messages) - 2


def test_relevant_session_notes_receive_a_ranking_boost_but_are_budgeted():
    messages = [
        _message("system", "system prompt"),
        _message("user", "opening"),
        _message("assistant", "Unrelated old weather discussion."),
        _message("user", "Continue the SQLite schema migration."),
        _message("assistant", "Verify the SQLite migration after changing schema."),
    ]

    fallback = build_internal_fallback(
        messages,
        protect_head_count=2,
        protect_last_n=2,
        target_tokens=190,
        memory_context="SQLite session note: preserve the schema rollback command.",
    )

    assert fallback is not None
    assert "## Session Notes" in fallback.summary
    assert "schema rollback command" in fallback.summary
    assert "Unrelated old weather" not in fallback.summary


def test_pruned_skill_reload_markers_survive_without_recent_term_overlap():
    marker = "[SKILL_PRUNED: reload with skill_view(name='example')]"
    messages = [
        _message("system", "system prompt"),
        _message("user", "opening"),
        _message("assistant", marker),
        _message("user", "current request needs a detailed implementation"),
        _message("user", "unrelated current request"),
        _message("assistant", "current result"),
    ]

    fallback = build_internal_fallback(
        messages,
        protect_head_count=2,
        protect_last_n=2,
        target_tokens=180,
    )

    assert fallback is not None
    assert marker in fallback.summary


def test_pruned_skill_reload_markers_survive_from_previous_summary():
    marker = "[SKILL_PRUNED: reload with skill_view(name='example')]"
    messages = [
        _message("system", "system prompt"),
        _message("user", "opening"),
        _message("assistant", "old implementation detail"),
        _message("user", "unrelated current request"),
        _message("assistant", "current result"),
    ]

    fallback = build_internal_fallback(
        messages,
        protect_head_count=2,
        protect_last_n=2,
        target_tokens=180,
        previous_summary=f"Earlier context.\n\n{marker}",
    )

    assert fallback is not None
    assert marker in fallback.summary


def _compressor_messages() -> list[dict]:
    return [
        _message("system", "system prompt"),
        _message("user", "opening request"),
        *[
            _message(
                "assistant" if index % 2 == 0 else "user",
                f"old middle {index} " + ("x" * 1_400),
            )
            for index in range(40)
        ],
        *[
            _message(
                "assistant" if index % 2 == 0 else "user",
                f"ornith recent message {index}",
            )
            for index in range(8)
        ],
    ]


def test_context_compressor_uses_plan_fallback_after_all_summary_providers_fail(
    monkeypatch,
):
    compressor = ContextCompressor(
        model="ornith-1.5-9b-uncensored",
        config_context_length=78_080,
        threshold_percent=0.80,
        summary_target_ratio=0.15,
        quiet_mode=True,
        protect_first_n=1,
        protect_last_n=8,
    )
    assert compressor.threshold_tokens == 64_000
    assert compressor.tail_token_budget == 9_600
    monkeypatch.setattr(compressor, "_generate_summary", lambda *_args, **_kwargs: None)
    messages = _compressor_messages()

    result = compressor.compress(
        messages,
        plan_context="Task: Recovery\nGoal: keep going\nSummary: first step complete",
        minimal_plan_context="Goal: keep going\nCurrent step 2/3: implement",
    )

    combined = "\n".join(str(message.get("content", "")) for message in result)
    assert "CONTEXT WINDOW COMPRESSED — INTERNAL FALLBACK" in combined
    assert "Task: Recovery" in combined
    assert [message["content"] for message in result[-8:]] == [
        f"ornith recent message {index}" for index in range(8)
    ]
    assert "old middle 4" not in combined
    assert compressor._last_summary_fallback_used is True
    assert compressor._last_compress_aborted is False


def test_context_compressor_aborts_only_when_internal_fallback_cannot_fit(monkeypatch):
    compressor = ContextCompressor(
        model="ornith-1.5-9b-uncensored",
        config_context_length=78_080,
        threshold_percent=0.80,
        summary_target_ratio=0.15,
        quiet_mode=True,
        protect_first_n=1,
        protect_last_n=8,
        abort_on_summary_failure=True,
    )
    compressor.tail_token_budget = 1
    monkeypatch.setattr(compressor, "_generate_summary", lambda *_args, **_kwargs: None)
    messages = _compressor_messages()

    result = compressor.compress(
        messages,
        plan_context="Task: Recovery\nGoal: keep going",
        minimal_plan_context="Goal: keep going\nCurrent step 2/3: implement",
    )

    assert result == messages
    assert compressor._last_summary_fallback_used is False
    assert compressor._last_compress_aborted is True
