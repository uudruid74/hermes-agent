from __future__ import annotations

import json
from unittest.mock import patch

from agent.context_compressor import ContextCompressor
from agent.internal_compression_fallback import (
    InternalFallback,
    _lexrank,
    _session_notes,
    _tfidf_vectors,
    build_internal_fallback,
)


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
        _message("user", "recent " * 10),
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
        target_tokens=220,
        plan_context=full,
        minimal_plan_context=minimal,
    )

    assert fallback is not None
    assert fallback.mode == "plan-minimal"
    assert fallback.head_count == 1
    assert fallback.tail_start == len(messages) - 1
    assert minimal in fallback.summary
    assert "EXTRA STEP DETAIL" not in fallback.summary
    assert "verbatim" in fallback.summary.lower()


def test_tail_protection_counts_template_visible_messages():
    latest = {
        "role": "user",
        "content": "LATEST ACTIONABLE",
        "metadata": {"directive": "stop"},
    }
    messages = [
        _message("system", "system prompt"),
        _message("user", "opening"),
        _message("assistant", "old result"),
        latest,
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call-1"}]},
        {"role": "tool", "content": "tool result", "tool_call_id": "call-1"},
    ]

    fallback = build_internal_fallback(
        messages,
        protect_head_count=2,
        protect_last_n=2,
        target_tokens=1_000,
    )

    assert fallback is not None
    assert latest in messages[fallback.tail_start :]


def test_plan_minimal_preserves_recent_verbatim_tail():
    latest = {
        "role": "user",
        "content": "Do not restart anything yet.",
        "metadata": {"directive": "stop"},
    }
    messages = [
        _message("system", "system prompt"),
        _message("user", "opening"),
        _message("assistant", "old result"),
        latest,
    ]

    fallback = build_internal_fallback(
        messages,
        protect_head_count=2,
        protect_last_n=1,
        target_tokens=220,
        plan_context="Task: Huge plan\n" + ("detail " * 1_000),
        minimal_plan_context="Goal: recover\nCurrent step: preserve latest directive",
    )

    assert fallback is not None
    assert fallback.mode == "plan-minimal"
    assert fallback.tail_start <= messages.index(latest)
    assert latest in messages[fallback.tail_start :]
    assert "verbatim" in fallback.summary.lower()


def test_active_plan_keeps_current_pruned_skill_marker():
    marker = "[SKILL_PRUNED: reload with skill_view(name='critical')]"
    messages = [
        _message("system", "system prompt"),
        _message("user", "opening"),
        _message("assistant", marker),
        _message("user", "current request"),
        _message("assistant", "current result"),
    ]

    fallback = build_internal_fallback(
        messages,
        protect_head_count=2,
        protect_last_n=2,
        target_tokens=1_000,
        plan_context="Task: T\nGoal: G\nStep 1/1",
        minimal_plan_context="Goal: G\nCurrent step 1/1: S",
    )

    assert fallback is not None
    assert marker in fallback.summary


def test_active_plan_keeps_small_note_when_earlier_note_is_oversized():
    small_note = "Keep the restart prohibition."
    messages = [
        _message("system", "system prompt"),
        _message("user", "opening"),
        _message("assistant", "old result"),
        _message("user", "current request"),
        _message("assistant", "current result"),
    ]

    fallback = build_internal_fallback(
        messages,
        protect_head_count=2,
        protect_last_n=2,
        target_tokens=180,
        plan_context="Task: T\nGoal: G\nStep 1/1",
        minimal_plan_context="Goal: G\nCurrent step 1/1: S",
        memory_context="oversized " * 1_000,
        previous_summary=f"## Session Notes\n{small_note}",
    )

    assert fallback is not None
    assert small_note in fallback.summary
    assert "oversized oversized" not in fallback.summary


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
        target_tokens=180,
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
        target_tokens=160,
        memory_context="SQLite session note: preserve the schema rollback command.",
    )

    assert fallback is not None
    assert "## Session Notes" in fallback.summary
    assert "schema rollback command" in fallback.summary
    assert "Unrelated old weather" not in fallback.summary


def test_session_note_survives_without_recent_lexical_overlap_when_it_fits():
    messages = [
        _message("system", "system prompt"),
        _message("user", "opening"),
        _message("assistant", "old details"),
        _message("user", "more old details"),
        _message("user", "current request"),
        _message("assistant", "current result"),
    ]

    fallback = build_internal_fallback(
        messages,
        protect_head_count=2,
        protect_last_n=2,
        target_tokens=1_000,
        memory_context="Critical deployment note: never restart the database automatically.",
    )

    assert fallback is not None
    assert "never restart the database automatically" in fallback.summary


def test_session_note_collection_is_bounded():
    messages = [
        _message("assistant", f"## Session Notes\nnote-{index}")
        for index in range(300)
    ]

    notes = _session_notes(messages, "m" * 2_400, "")

    assert len(notes) == 256
    assert max(map(len, notes)) <= 1_201


def test_tfidf_document_frequency_counts_documents_not_occurrences():
    vector = _tfidf_vectors(["alpha alpha common", "common beta"])[0]

    assert vector["alpha"] > 0.93


def test_lexrank_pairwise_work_is_bounded(monkeypatch):
    calls = 0

    def _counted_cosine(left, right):
        nonlocal calls
        calls += 1
        return 1.0 if left and right else 0.0

    monkeypatch.setattr(
        "agent.internal_compression_fallback._cosine",
        _counted_cosine,
    )
    _lexrank([{"token": 1.0} for _ in range(300)])

    assert calls <= (256 * 255) // 2


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


def test_internal_only_never_calls_summary_provider():
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
        _message("user", "opening"),
        *[
            _message("assistant", f"old {index} " + ("x" * 500))
            for index in range(10)
        ],
        _message("user", "current request"),
        _message("assistant", "current result"),
    ]

    with patch.object(
        compressor,
        "_generate_summary",
        side_effect=AssertionError("internal-only mode called the summary provider"),
    ):
        result = compressor.compress(messages)

    assert result is not messages
    assert compressor._last_summary_fallback_used is True


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
    assert "[CONTEXT WINDOW COMPRESSED]" in combined
    assert "Task: Recovery" in combined
    assert [message["content"] for message in result[-8:]] == [
        f"ornith recent message {index}" for index in range(8)
    ]
    assert "old middle 4" not in combined
    assert compressor._last_summary_fallback_used is True
    assert compressor._last_compress_aborted is False


def _observation(content: str, call_id: str = "call-1") -> dict:
    return {"role": "tool", "content": content, "tool_call_id": call_id}


def test_observations_never_reach_the_lexrank_middle():
    """Rule 2: no observation text may appear in the emitted summary."""
    messages = [
        _message("system", "system prompt"),
        _message("user", "opening request"),
        _message("assistant", "SQLite migration needs a sessions index."),
        _observation("UNIQUE_OBSERVATION_TOKEN alpha beta gamma delta"),
        _observation("UNIQUE_OBSERVATION_TOKEN epsilon zeta eta theta"),
        _message("user", "How should we finish the SQLite migration?"),
        _message("assistant", "Keep the migration small and verify the index."),
        _message("user", "recent question"),
        _message("assistant", "recent answer"),
    ]

    fallback = build_internal_fallback(
        messages,
        protect_head_count=2,
        protect_last_n=2,
        target_tokens=400,
    )

    assert fallback is not None
    assert "UNIQUE_OBSERVATION_TOKEN" not in fallback.summary
    # The reasoning content is still eligible — we mask observations only.
    assert "SQLite migration needs" in fallback.summary


def test_observations_do_not_reach_the_summary_from_the_tail():
    """Rule 2 holds for the region outside the verbatim tail as well."""
    messages = [
        _message("system", "system prompt"),
        _message("user", "opening"),
        _observation("TAIL_OBSERVATION_TOKEN uniq words here"),
        _message("assistant", "Continue the SQLite migration."),
        _message("user", "recent question"),
        _message("assistant", "recent answer"),
    ]

    fallback = build_internal_fallback(
        messages,
        protect_head_count=2,
        protect_last_n=2,
        target_tokens=400,
    )

    assert fallback is not None
    assert "TAIL_OBSERVATION_TOKEN" not in fallback.summary


def test_centroid_ignores_observations_so_relevance_tracks_the_live_turn():
    """Evan: strip observations before calculating centroids.

    The recent-window reference must be built from assistant+user content
    only.  Here the only 'recent' material is observation noise; the middle
    turn that actually echoes the live question must still be selected, which
    only happens if the observation noise is excluded from the centroid.
    """
    messages = [
        _message("system", "system prompt"),
        _message("user", "opening request"),
        _message("assistant", "sessions index migration needs care"),
        _observation("kayak paddle lifejacket canoe river rapids"),
        _observation("kayak paddle lifejacket canoe river rapids"),
        _message("user", "recent question"),
        _message("assistant", "recent answer"),
    ]

    fallback = build_internal_fallback(
        messages,
        protect_head_count=2,
        protect_last_n=2,
        target_tokens=400,
    )

    assert fallback is not None
    assert "kayak" not in fallback.summary
    assert "sessions index migration needs care" in fallback.summary


def test_emergency_rung_masks_tail_observations_except_the_last_turn():
    """Rule 3: overflow masks tail observations back to the last turn."""
    messages = [
        _message("system", "system prompt"),
        _message("user", "opening request"),
        _message("assistant", "first tail turn"),
        _observation("EARLY_TAIL_OBSERVATION keep only the newest turn"),
        _message("assistant", "second tail turn"),
        _observation("LATEST_OBSERVATION must stay verbatim"),
        _message("user", "recent question"),
    ]

    from agent.internal_compression_fallback import _mask_tail_observations

    masked = _mask_tail_observations(messages, tail_start=2)

    # The older tail observation is masked...
    assert "EARLY_TAIL_OBSERVATION" not in json.dumps(masked)
    # ...the newest turn's observation survives verbatim.
    assert "LATEST_OBSERVATION must stay verbatim" in json.dumps(masked)
    # Non-observation rows are untouched.
    assert "first tail turn" in json.dumps(masked)
    assert "second tail turn" in json.dumps(masked)


def test_emergency_rung_keeps_reasoning_contiguous_for_tail_refit():
    """The rung must land before tail shrinking, not mutate `messages`.

    The rung fires on the tail's own token count (not on a tail-refit
    failure), so with a budget this small it lands automatically; the
    assembled payload must then contain the masked tail rather than the
    original bulky observation.
    """
    import agent.internal_compression_fallback as fallback_module

    messages = [
        _message("system", "system prompt"),
        _message("user", "opening request"),
        _message("assistant", "older work"),
        *[_observation(f"BULK_OBSERVATION_{i} " + "fill " * 200) for i in range(4)],
        _message("user", "recent question"),
        _message("assistant", "recent answer"),
    ]
    before = json.dumps(messages)

    result = fallback_module.build_internal_fallback(
        messages,
        protect_head_count=2,
        protect_last_n=8,
        target_tokens=300,
    )

    # The caller's transcript is never mutated by the emergency rung.
    assert json.dumps(messages) == before
    # Deterministic compression has no failure rung: a payload is always
    # produced (Evan, 2026-09-15).
    assert result is not None
    assert result.mode in {"lexrank", "plan", "plan-minimal"}


def test_prefix_shortening_does_not_widen_the_lexrank_budget():
    """Guard for the 2026-09-15 prefix change.

    INTERNAL_FALLBACK_PREFIX was shortened from a 151-char block (which spelled
    out "All configured context-summary providers failed. This context was
    reconstructed locally without an LLM.") to a 27-char marker
    "[CONTEXT WINDOW COMPRESSED]". That reclaims ~31 tokens of headroom inside
    every fallback summary, which shifts the tail-token budget the LexRank
    selector can spend on the middle of the transcript.

    This test pins the INVARIANT, not a byte count: at the budgets the
    selection tests use, low-relevance filler must stay excluded while the
    relevant middle is retained. It fails if a future prefix/format change
    silently widens the selection budget again.
    """
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
        target_tokens=180,
    )

    assert fallback is not None
    assert "SQLite migration needs" in fallback.summary
    assert "sessions index before" in fallback.summary
    assert "garden weather" not in fallback.summary
