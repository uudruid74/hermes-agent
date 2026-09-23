from __future__ import annotations

import json

from agent.internal_compression_fallback import (
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
    # >3 words (underscores count as whitespace)
    assert _marker_keep_label("run the full migration now")
    # digits
    assert _marker_keep_label("undo 875")
    # opening paren
    assert _marker_keep_label("undo (875,965)")
    # terse boilerplate is cut
    assert not _marker_keep_label("git log")
    assert not _marker_keep_label("status")
    # test_undo_scratch.py -> 3 words, no digit/paren -> cut
    assert not _marker_keep_label("test_undo_scratch.py")


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
    fallback, _ = _fallback_with_markers(marker_recent_count=2)
    summary = fallback.summary
    # verbatim marker lines survive byte-exact
    assert "=== run migration on all shards ===" in summary
    assert "=== verify the new index exists ===" in summary
    # sibling exit tombstones survive
    assert "[exit 0]" in summary
    # echo + exit stay adjacent
    assert "=== verify the new index exists ===\n[exit 0]" in summary


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
    # "git log" is <3 words, no digit, no paren -> dropped whole.
    assistant, tool = _terminal_call(
        "g", 'echo "=== git log ===" && git log'
    )
    messages = [
        _message("system", "system prompt"),
        _message("user", "inspect the repo."),
        assistant,
        tool,
        _message("assistant", "checked the log."),
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
    assert "=== git log ===" not in fallback.summary


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
