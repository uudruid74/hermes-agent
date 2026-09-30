"""Regression tests for iterative context-summary continuity."""

from unittest.mock import MagicMock, patch

from agent.context_compressor import (
    COMPRESSED_SUMMARY_METADATA_KEY,
    ContextCompressor,
    SUMMARY_PREFIX,
    _MERGED_PRIOR_CONTEXT_HEADER,
    _MERGED_SUMMARY_DELIMITER,
    _RESTART_HANDOFF_PROBE_EXTRA_MESSAGES,
    _SUMMARY_END_MARKER,
)
from agent.internal_compression_fallback import (
    INTERNAL_FALLBACK_PREFIX,
    VERBATIM_CONTEXT_MARKER,
    build_internal_fallback,
)


def _compressor(protect_first_n: int = 1) -> ContextCompressor:
    with patch("agent.context_compressor.get_model_context_length", return_value=100000):
        return ContextCompressor(
            model="test/model",
            threshold_percent=0.85,
            protect_first_n=protect_first_n,
            protect_last_n=1,
            quiet_mode=True,
        )


def _response(content: str):
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = content
    return mock_response


def _messages_with_handoff(summary_body: str):
    return [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": f"{SUMMARY_PREFIX}\n{summary_body}"},
        {"role": "assistant", "content": "handoff acknowledged after resume"},
        {"role": "user", "content": "new user turn after resume"},
        {"role": "assistant", "content": "new assistant work after resume"},
        {"role": "user", "content": "more new work after resume"},
        {"role": "assistant", "content": "latest tail response"},
        {"role": "user", "content": "final active request stays in protected tail"},
    ]


def _messages_with_merged_handoff(summary_body: str, prior_tail: str):
    merged = {
        "role": "user",
        "content": (
            f"{_MERGED_PRIOR_CONTEXT_HEADER}\n{prior_tail}\n\n"
            f"{_MERGED_SUMMARY_DELIMITER}\n\n"
            f"{SUMMARY_PREFIX}\n{summary_body}\n\n{_SUMMARY_END_MARKER}"
        ),
        COMPRESSED_SUMMARY_METADATA_KEY: True,
    }
    messages = _messages_with_handoff(summary_body)
    messages[1] = merged
    return messages


def _messages_with_default_handoff(summary_body: str):
    return [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "original task before first compaction"},
        {"role": "assistant", "content": "original answer before first compaction"},
        {"role": "user", "content": "original follow-up before first compaction"},
        {"role": "assistant", "content": f"{SUMMARY_PREFIX}\n{summary_body}"},
        {"role": "user", "content": "new user turn after restart"},
        {"role": "assistant", "content": "new assistant work after restart"},
        {"role": "user", "content": "more new work after restart"},
        {"role": "assistant", "content": "latest tail response"},
        {"role": "user", "content": "final active request stays in protected tail"},
    ]


def _messages_with_summary_at_index(summary_index: int):
    msgs = [{"role": "system", "content": "system prompt"}]
    for idx in range(1, summary_index):
        role = "user" if idx % 2 else "assistant"
        msgs.append({"role": role, "content": f"probe filler {idx}"})
    role = "user" if summary_index % 2 else "assistant"
    msgs.append({"role": role, "content": f"{SUMMARY_PREFIX}\nboundary summary"})
    msgs.extend([
        {"role": "assistant", "content": "new answer"},
        {"role": "user", "content": "tail request"},
    ])
    return msgs








def test_handoff_in_protected_head_is_replaced_not_duplicated():
    """Re-compaction must replace a protected old handoff with the updated one."""
    compressor = _compressor()
    old_summary = "OLD-PROTECTED-HANDOFF unique old summary body"

    with patch("agent.context_compressor.call_llm", return_value=_response("UPDATED summary body")):
        compressed = compressor.compress(_messages_with_handoff(old_summary))

    # The summary may be emitted standalone or merged into the first tail
    # message (alternation corner case), so detect it the same way the
    # compressor does rather than via a startswith(SUMMARY_PREFIX) check.
    summary_messages = [
        msg
        for msg in compressed
        if isinstance(msg, dict)
        and ContextCompressor._is_context_summary_content(msg.get("content"))
    ]
    assert len(summary_messages) == 1
    assert "UPDATED summary body" in str(summary_messages[0]["content"])
    assert old_summary not in str(summary_messages[0]["content"])
    assert old_summary not in "\n".join(str(msg.get("content") or "") for msg in compressed)






def test_recompression_of_current_merged_handoff_preserves_prior_tail_once():
    """Current merged handoffs lose only stale summary data on recompression.

    Composed contract after #57835: the merged handoff's genuine prior-tail
    content must be RECOVERED — either verbatim in the output (raw head
    protection) or by entering the summarizer input so the fresh summary
    folds it in (once the handoff is re-summarised). It must never be
    silently deleted, and the stale summary body must never be re-emitted
    verbatim.
    """
    compressor = _compressor()
    old_summary = "CURRENT-MERGED-OLD-SUMMARY unique continuity facts"
    prior_tail = "PRESERVED-PRIOR-TAIL real user content"

    seen_turns = []

    def _capture(turns, **kwargs):
        seen_turns.extend(turns)
        return ContextCompressor._with_summary_prefix(
            "fresh replacement summary"
        )

    with patch.object(
        compressor,
        "_generate_summary",
        side_effect=_capture,
    ):
        result = compressor.compress(
            _messages_with_merged_handoff(old_summary, prior_tail)
        )

    joined = "\n".join(str(message.get("content", "")) for message in result)
    summarizer_input = "\n".join(str(t.get("content", "")) for t in seen_turns)
    # Prior tail recovered: verbatim in output OR folded via summarizer input.
    assert prior_tail in joined or prior_tail in summarizer_input
    # Never duplicated in the output.
    assert joined.count(prior_tail) <= 1
    assert old_summary not in joined
    assert joined.count(SUMMARY_PREFIX) == 1
    assert "fresh replacement summary" in joined








def test_resume_handoff_preserves_raw_head_without_duplicating_handoff():
    """step5 no-decay keeps raw head turns while replacing the old handoff."""
    compressor = _compressor(protect_first_n=3)
    old_summary = "DEFAULT-RESTART-SUMMARY durable facts from before restart"

    with patch("agent.context_compressor.call_llm", return_value=_response("fresh summary")) as mock_call:
        result = compressor.compress(_messages_with_default_handoff(old_summary))

    prompt = mock_call.call_args.kwargs["messages"][0]["content"]
    assert "PREVIOUS SUMMARY:" in prompt
    assert prompt.count(old_summary) == 1
    # Raw protected turns stay outside the summarizer input and survive
    # verbatim in the output; only the synthetic handoff is folded/replaced.
    assert "original task before first compaction" not in prompt
    assert "original answer before first compaction" not in prompt
    assert "original follow-up before first compaction" not in prompt
    assert f"[ASSISTANT]: {SUMMARY_PREFIX}" not in prompt
    # Grounding (761a0b124e) may prepend a deterministic task-snapshot
    # section — pin the contract, not the exact stored string.
    stored_summary = compressor._previous_summary or ""
    assert stored_summary.endswith("fresh summary")
    assert old_summary not in stored_summary
    result_text = "\n".join(str(msg.get("content", "")) for msg in result)
    assert result_text.count("original task before first compaction") == 1
    assert result_text.count("original answer before first compaction") == 1
    assert result_text.count("original follow-up before first compaction") == 1
    assert all(
        old_summary not in str(msg.get("content", ""))
        for msg in result
    )


def test_restart_simulation_fresh_compressor_preserves_head_without_decay():
    """Fresh and live compressors use the same persistent head boundary."""
    # Live process: has already compacted once; step5 says protection persists.
    live = _compressor(protect_first_n=3)
    live.compression_count = 1

    # Restarted process: brand-new compressor, all in-memory state fresh.
    restarted = _compressor(protect_first_n=3)
    assert restarted.compression_count == 0
    assert not restarted._previous_summary

    msgs = _messages_with_default_handoff(
        "PERSISTED-HANDOFF durable facts from before restart"
    )

    # The configured raw head remains protected in both processes.
    assert restarted._effective_protect_first_n(msgs) == 3
    assert restarted._protect_head_size(msgs) == live._protect_head_size(msgs) == 4
    restarted_start = restarted._align_boundary_forward(
        msgs, restarted._protect_head_size(msgs)
    )
    assert restarted_start == 4

    # End-to-end: the first post-restart compaction keeps the raw head but
    # replaces the synthetic old handoff.
    with patch("agent.context_compressor.call_llm", return_value=_response("fresh summary")):
        result = restarted.compress(msgs)
    result_text = "\n".join(str(msg.get("content", "")) for msg in result)
    assert "PERSISTED-HANDOFF durable facts" not in result_text
    assert result_text.count("original task before first compaction") == 1
    assert result_text.count("original answer before first compaction") == 1








def test_zero_protect_first_n_still_folds_restart_fossil():
    """protect_first_n=0 should still self-heal restarted summaries."""
    compressor = _compressor(protect_first_n=0)
    old_summary = "OLD-SUMMARY-ZERO-PROTECT durable facts"
    msgs = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "task one"},
        {"role": "assistant", "content": "answer one"},
        {"role": "user", "content": "task two"},
        {"role": "assistant", "content": f"{SUMMARY_PREFIX}\n{old_summary}"},
        {"role": "user", "content": "active request"},
    ]

    with patch("agent.context_compressor.call_llm", return_value=_response("fresh summary")):
        result = compressor.compress(msgs)

    result_text = "\n".join(str(msg.get("content", "")) for msg in result)
    assert old_summary not in result_text
    assert result_text.index(_SUMMARY_END_MARKER) < result_text.index("active request")
    assert sum(
        1 for msg in result if ContextCompressor._is_context_summary_message(msg)
    ) == 1




def test_restart_fossil_is_folded_into_internal_fallback_state():
    """A recovered fossil becomes part of the canonical local summary."""
    compressor = _compressor(protect_first_n=1)
    compressor.abort_on_summary_failure = True
    old_summary = "ABORT-RETRY-OLD-SUMMARY durable facts"
    msgs = [{"role": "system", "content": "system prompt"}]
    msgs += [
        {
            "role": "user" if idx % 2 else "assistant",
            # Long enough to clear the rankable-content floor (Evan,
            # 2026-09-15): the assertion below checks that middle content is
            # carried into the fallback, so the fixture must be real content,
            # not two-token filler that the floor now drops by design.
            "content": f"filler {idx}: migrating the sessions table needs care",
        }
        for idx in range(1, 6)
    ]
    msgs += [
        {"role": "assistant", "content": f"{SUMMARY_PREFIX}\n{old_summary}"},
        {"role": "user", "content": "active request"},
    ]

    with patch.object(compressor, "_generate_summary", return_value=None):
        result = compressor.compress([dict(m) for m in msgs])

    assert compressor._last_compress_aborted is False
    assert compressor._last_summary_fallback_used is True
    assert compressor.compression_count == 1
    assert old_summary in (compressor._previous_summary or "")
    # filler 1 is the configured raw protected head under step5 no-decay.  It
    # may remain verbatim instead of being duplicated into the fallback body.
    result_text = "\n".join(str(message.get("content", "")) for message in result)
    assert "filler 1" in (compressor._previous_summary or "") or "filler 1" in result_text
    assert any(old_summary in str(msg.get("content", "")) for msg in result)
    assert sum(
        1 for msg in result if ContextCompressor._is_context_summary_message(msg)
    ) == 1




def test_repeated_internal_fallback_keeps_prior_summary_single_level():
    messages = [
        {"role": "system", "content": "system prompt"},
        {
            "role": "user",
            "content": "Preserve the durable migration decision across compactions.",
        },
        {
            "role": "assistant",
            "content": "The migration decision remains active and fully verified.",
        },
        {"role": "user", "content": "Current request remains in the recent tail."},
        {
            "role": "assistant",
            "content": "Current response remains in the recent tail.",
        },
    ]
    previous_summary = "DURABLE-PRIOR-FACT: schema migration uses revision 42."

    for _ in range(3):
        fallback = build_internal_fallback(
            messages,
            protect_head_count=1,
            protect_last_n=1,
            target_tokens=4_000,
            previous_summary=previous_summary,
        )
        previous_summary = fallback.summary.removeprefix(
            INTERNAL_FALLBACK_PREFIX
        ).lstrip()

    assert previous_summary.count("DURABLE-PRIOR-FACT") == 1
    assert previous_summary.count(INTERNAL_FALLBACK_PREFIX) == 0
    assert previous_summary.count("## Prior Context Summary") == 1
    assert previous_summary.count(VERBATIM_CONTEXT_MARKER) == 1


def test_forced_leading_merged_summary_strips_live_tail_from_summary_body():
    """Rehydrating a forced-leading merged summary should ignore live tail."""
    merged = (
        f"{SUMMARY_PREFIX}\nSUMMARY_BODY\n\n"
        f"{_SUMMARY_END_MARKER}\n\n"
        "LIVE_TAIL_REQUEST"
    )

    assert ContextCompressor._is_context_summary_content(merged) is True
    assert ContextCompressor._strip_summary_prefix(merged) == "SUMMARY_BODY"










def test_empty_post_handoff_window_noops_without_summary_call():
    """A latest handoff that consumes the window must not trigger an empty summary.

    Regression test from PR #59526 (#59496), fixture adapted to current main:
    the standalone handoff sits alone in the compressible window, strips to
    None via _strip_context_summary_handoff_message, and leaves
    turns_to_summarize empty — the guard must skip _generate_summary
    entirely instead of wasting an aux LLM call on empty input.
    """
    compressor = _compressor()
    old_summary = "WINDOW-END-SUMMARY durable facts already captured"
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": f"{SUMMARY_PREFIX}\n{old_summary}"},
        {"role": "assistant", "content": "recent tail response"},
        {"role": "user", "content": "tail request"},
        {"role": "assistant", "content": "tail answer"},
        {"role": "user", "content": "latest tail request"},
        {"role": "assistant", "content": "latest tail answer"},
    ]

    with (
        patch.object(compressor, "_find_tail_cut_by_tokens", return_value=2),
        patch.object(compressor, "_generate_summary") as mock_generate_summary,
    ):
        result = compressor.compress(messages, current_tokens=90_000)

    mock_generate_summary.assert_not_called()
    assert result == messages
    # The rehydrated summary state is deliberately kept: the handoff is
    # genuinely present in the returned (unchanged) transcript.
    assert compressor._previous_summary == old_summary
    assert compressor.compression_count == 0
    # Mirrors the sibling no-compressible-window guard (#40803): the shape
    # cannot shrink, so it counts as an ineffective strike (routed through
    # the durable write-through helper) to arm the anti-thrash breaker.
    assert compressor._ineffective_compression_count == 1
    assert compressor._last_compression_savings_pct == 0.0
    assert compressor._last_summary_dropped_count == 0
    assert compressor._last_summary_fallback_used is False
    assert compressor._last_compress_aborted is False
    telemetry = compressor._last_compression_telemetry or {}
    assert telemetry.get("failure_class") == "empty_post_handoff_window"
