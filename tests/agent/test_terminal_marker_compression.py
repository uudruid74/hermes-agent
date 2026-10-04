from __future__ import annotations

import json
from unittest.mock import patch

from agent.context_compressor import ContextCompressor
from agent.internal_compression_fallback import (
    INTERNAL_FALLBACK_PREFIX,
    VERBATIM_CONTEXT_MARKER,
    _echo_marker_label,
    _echo_marker_verbatim,
    _is_echo_marker_command,
    _marker_keep_label,
    _parse_exit_code,
    _render_exit_tombstone,
    _split_marker_tiers,
    _terminal_marker_units,
    build_internal_fallback,
)


def _message(role: str, content: str) -> dict:
    return {"role": role, "content": content}


def _terminal_call(
    call_id: str, command: str, content: str | None = None
) -> tuple[dict, dict]:
    assistant = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "function": {
                    "name": "terminal",
                    "arguments": json.dumps({"command": command}),
                },
            }
        ],
    }
    tool = {
        "role": "tool",
        "content": content
        if content is not None
        else json.dumps({"output": "ok", "exit_code": 0, "error": None}),
        "tool_call_id": call_id,
    }
    return assistant, tool


# --------------------------------------------------------------------------- helpers


def test_echo_marker_command_detection():
    assert _is_echo_marker_command('echo "=== deploy ===" && git push')
    assert _is_echo_marker_command("echo '=== undo (875,965) ==='")
    assert not _is_echo_marker_command("git log --oneline")
    assert not _is_echo_marker_command("")


def test_echo_marker_verbatim_preserves_agent_line_exactly():
    command = 'echo "===  deploy   service ===" && git push'
    assert _echo_marker_verbatim(command) == "===  deploy   service ==="
    # The echoed line is byte-exact, including interior spacing.
    command2 = "echo '===undo (875,965)==='"
    assert _echo_marker_verbatim(command2) == "===undo (875,965)==="


def test_echo_marker_label_strips_only_the_fences():
    assert _echo_marker_label("=== deploy ===") == "deploy"
    assert _echo_marker_label("===  deploy   service ===") == "deploy   service"


def test_marker_keep_rules():
    # >=2 ws-words (Stage 1 widened gate — t_57cb0d4e; was >3, which dropped
    # ~48% of the real label supply on the production extractor)
    assert _marker_keep_label("run the full migration now")
    assert _marker_keep_label("verify dovecot")  # 2 words, the decision spine
    assert _marker_keep_label("auth test")
    assert _marker_keep_label("git diff stat")  # 3 words, carries decision info
    # digits
    assert _marker_keep_label("undo 875")
    # opening paren
    assert _marker_keep_label("undo (875,965)")
    # 1-word labels stay cut — their top members are genuine noise
    assert not _marker_keep_label("status")
    assert not _marker_keep_label("RUN")
    # test_undo_scratch.py -> 3 words under the underscore rule -> kept (the
    # widened gate admits it; it is a real subject label)
    assert _marker_keep_label("test_undo_scratch.py")
    # 'description' excluded at ANY threshold (Evan ruling 2): it carries no
    # decision information under either interpretation — empty label.
    assert not _marker_keep_label("description")
    assert not _marker_keep_label("Description")


def test_parse_exit_code():
    assert _parse_exit_code(json.dumps({"output": "x", "exit_code": 0})) == 0
    assert _parse_exit_code(json.dumps({"output": "x", "exit_code": 1})) == 1
    assert _parse_exit_code(json.dumps({"output": "x", "exit_code": -1})) == -1
    assert _parse_exit_code("not json") is None
    assert _parse_exit_code(json.dumps({"output": "x"})) is None
    # regex fallback
    assert _parse_exit_code('prefix {"exit_code": 124} suffix') == 124


def test_render_exit_tombstone_zero_is_bare():
    assert _render_exit_tombstone("git push", 0) == "[exit 0]"


def test_render_exit_tombstone_nonzero_uses_interpretation():
    # grep exit 1 -> "No matches found"
    tombstone = _render_exit_tombstone("grep foo bar.txt", 1)
    assert tombstone.startswith("[exit 1 —")
    assert "No matches found" in tombstone
    # unknown command -> bare int
    assert _render_exit_tombstone("frobnicate --now", 7) == "[exit 7]"


def test_split_marker_tiers_recent_first_ranked_chrono():
    units = _terminal_marker_units(
        [
            *_terminal_call("a", 'echo "=== first thing done now ===" && x'),
            *_terminal_call("b", 'echo "=== second thing done now ===" && x'),
            *_terminal_call("c", 'echo "=== third thing done now ===" && x'),
        ]
    )
    recent, ranked = _split_marker_tiers(units, recent_count=1)
    assert [u.text for u in recent] == ["=== third thing done now ===\n[exit 0]"]
    # ranked tier stays chronological
    assert [u.text for u in ranked] == [
        "=== first thing done now ===\n[exit 0]",
        "=== second thing done now ===\n[exit 0]",
    ]


def test_split_marker_tiers_zero_sends_all_to_ranked():
    units = _terminal_marker_units(
        [
            *_terminal_call("a", 'echo "=== first thing done now ===" && x'),
            *_terminal_call("b", 'echo "=== second thing done now ===" && x'),
        ]
    )
    recent, ranked = _split_marker_tiers(units, recent_count=0)
    assert recent == []
    assert len(ranked) == 2


# --------------------------------------------------------------------------- integration


def _fallback_with_markers(marker_recent_count: int = 2):
    assistant_a, tool_a = _terminal_call(
        "a", 'echo "=== run migration on all shards ===" && migrate'
    )
    assistant_b, tool_b = _terminal_call(
        "b", 'echo "=== undo (875,965) ===" && git revert'
    )
    assistant_c, tool_c = _terminal_call(
        "c", 'echo "=== verify the new index exists ===" && check'
    )
    messages = [
        _message("system", "system prompt"),
        _message("user", "migrate the database then verify."),
        assistant_a,
        tool_a,
        _message("assistant", "Migration is running across every shard."),
        assistant_b,
        tool_b,
        _message("assistant", "That revert was the wrong call; undone."),
        assistant_c,
        tool_c,
        _message("user", "recent question"),
        _message("assistant", "recent answer"),
    ]
    fallback = build_internal_fallback(
        messages,
        protect_head_count=2,
        protect_last_n=2,
        target_tokens=3_000,
        marker_recent_count=marker_recent_count,
    )
    return fallback, messages


def test_marker_and_tombstone_survive_compaction():
    # Stage 1 spine (t_57cb0d4e): markers survive via the recency-bounded
    # spine with its own ceiling. marker_recent_count=2 promises the two most
    # recent markers ([b, c]); the ceiling then cuts oldest-first.
    fallback, _ = _fallback_with_markers(marker_recent_count=2)
    summary = fallback.summary
    assert "## Decision Markers" in summary
    # Byte-exact marker + adjacent tombstone.
    assert "=== undo (875,965) ===\n[exit 0]" in summary
    assert "[exit 0]" in summary
    # The oldest of the candidate tier is ceiling-cut when it no longer fits:
    # undo (8 tok) + verify (11 tok) = 19 > ceiling (0.006 * 3,000 = 18), so
    # verify — the NEWEST candidate — should NOT be the one cut... but the
    # cut is oldest-first, so verify fits only if undo leaves. Ceiling keeps
    # the FIRST-fitting run in chronological order within the tier: undo
    # stays, verify is cut (19 > 18). The promise is a selection-order bound,
    # and the ceiling is the guard (anti-bug-4): a marker is retained because
    # it is a decision mark, never beyond the payload's honest share.
    assert "=== verify the new index exists ===" not in summary
    # The oldest marker overall (a) is recency-cut: outside the promise.
    assert "=== run migration on all shards ===" not in summary


def test_context_compressor_uses_protected_tail_size_for_recent_marker_tier():
    messages = [
        _message("system", "system prompt"),
        _message("user", "Run each checkpoint and preserve the decision spine."),
    ]
    marker_lines = []
    for index in range(10):
        marker = f"=== complete migration checkpoint {index} now ==="
        marker_lines.append(marker)
        messages.extend(
            _terminal_call(
                f"call-{index}",
                f'echo "{marker}" && migrate --checkpoint {index}',
            )
        )
        messages.append(
            _message(
                "assistant",
                f"Checkpoint {index} completed and its result was verified.",
            )
        )

    # Keep the terminal calls out of the protected tail: these eight visible
    # turns become the contiguous tail selected by protect_last_n=8.
    for index in range(4):
        messages.extend([
            _message("user", f"Recent follow-up request {index} with details."),
            _message("assistant", f"Recent follow-up answer {index} with details."),
        ])

    with patch(
        "agent.context_compressor.get_model_context_length",
        return_value=100_000,
    ):
        compressor = ContextCompressor(
            model="test/model",
            protect_first_n=1,
            protect_last_n=8,
            summary_target_ratio=0.50,
            quiet_mode=True,
            internal_only=True,
            config_context_length=100_000,
        )

    with patch(
        "agent.context_compressor.build_internal_fallback",
        wraps=build_internal_fallback,
    ) as fallback_builder:
        compressed = compressor.compress(
            messages,
            current_tokens=100_000,
            force=True,
        )

    assert fallback_builder.call_args.kwargs.get("marker_recent_count") == 8
    rendered = "\n".join(str(message.get("content") or "") for message in compressed)
    for marker in marker_lines[-8:]:
        assert f"{marker}\n[exit 0]" in rendered

    summary_indexes = [
        index
        for index, message in enumerate(compressed)
        if str(message.get("content") or "").startswith(INTERNAL_FALLBACK_PREFIX)
    ]
    assert len(summary_indexes) == 1
    summary_index = summary_indexes[0]
    summary = str(compressed[summary_index]["content"])
    assert summary.count(VERBATIM_CONTEXT_MARKER) == 1

    expected_tail = [
        text
        for index in range(4)
        for text in (
            f"Recent follow-up request {index} with details.",
            f"Recent follow-up answer {index} with details.",
        )
    ]
    assert [
        message["content"] for message in compressed[summary_index + 1 :]
    ] == expected_tail
    assert all(text not in summary for text in expected_tail)


def test_marker_is_not_emitted_without_its_tombstone():
    # A marker whose tool result is missing must not be emitted at all.
    assistant = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "ghost",
                "function": {
                    "name": "terminal",
                    "arguments": json.dumps(
                        {"command": 'echo "=== never ran at all ===" && x'}
                    ),
                },
            }
        ],
    }
    messages = [
        _message("system", "system prompt"),
        _message("user", "do something."),
        assistant,
        # no matching tool result
        _message("assistant", "it never finished."),
        _message("user", "recent question"),
        _message("assistant", "recent answer"),
    ]
    fallback = build_internal_fallback(
        messages,
        protect_head_count=2,
        protect_last_n=2,
        target_tokens=1_000,
        marker_recent_count=2,
    )
    assert "never ran" not in fallback.summary


def test_terse_marker_is_cut():
    # 'git log' is 2 words and stage 1 WIDENED the gate to >=2 words
    # (t_57cb0d4e) — but git log is still excluded here? No: the widened gate
    # KEEPS it. What stays cut is the 1-word residue ('status', 'RUN') and
    # 'description'. This test now pins the widened behavior: 2-word labels
    # survive ('git log' now kept per Evan's "we were preserving more of
    # those"), while a 1-word boilerplate label is still cut.
    assistant, tool = _terminal_call(
        "g", 'echo "=== git log ===" && git log'
    )
    assistant2, tool2 = _terminal_call(
        "h", 'echo "=== status ===" && status'
    )
    messages = [
        _message("system", "system prompt"),
        _message("user", "inspect the repo."),
        assistant,
        tool,
        _message("assistant", "checked the log."),
        assistant2,
        tool2,
        _message("assistant", "service looks down."),
        _message("user", "recent question"),
        _message("assistant", "recent answer"),
    ]
    fallback = build_internal_fallback(
        messages,
        protect_head_count=2,
        protect_last_n=2,
        target_tokens=1_000,
        marker_recent_count=2,
    )
    assert "=== git log ===" in fallback.summary
    assert "=== status ===" not in fallback.summary


def test_marker_with_nonzero_exit_renders_semantics():
    assistant, tool = _terminal_call(
        "g",
        'echo "=== grep for the migration string ===" && grep migration file',
        content=json.dumps({"output": "", "exit_code": 1, "error": None}),
    )
    messages = [
        _message("system", "system prompt"),
        _message("user", "find the migration string."),
        assistant,
        tool,
        _message("assistant", "no matches found."),
        _message("user", "recent question"),
        _message("assistant", "recent answer"),
    ]
    fallback = build_internal_fallback(
        messages,
        protect_head_count=2,
        protect_last_n=2,
        target_tokens=1_000,
        marker_recent_count=2,
    )
    assert "=== grep for the migration string ===" in fallback.summary
    assert "No matches found" in fallback.summary
