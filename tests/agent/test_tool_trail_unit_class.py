"""Unit tests for the tool-trail unit class (t_490b92fc + t_235275a0).

Rows admit through `_tool_trail_scan` and collapse through `_tool_trail_lines`
only when they are paired assistant tool_calls + tool results; verbatim-tail
rows are gated (c) and never admitted.

t_235275a0 emitted-block format: lines render as ``[TOOL]: <identity> —
<outcome>`` units (the ``[#id]`` handle and the row's own stamp ride
``_render_unit``); targets are recovered from the result stub when the call
arguments were dropped; message-send tools render as
``tell → tool_call_id …``; and there are no ``##`` sections anywhere.
"""

import json
import re

from agent import internal_compression_fallback as icf
from agent.internal_compression_fallback import (
    _carried_trail_keys,
    _TrailLine,
    _collapsed_tool_units,
    _select_trail_lines,
    _tool_trail_budget,
    _tool_trail_lines,
    _tool_trail_scan,
    _trail_salience,
    build_internal_fallback,
)


def _calls(cid, name, args, ts):
    return {
        "role": "assistant",
        "content": "",
        "timestamp": ts,
        "tool_calls": [
            {
                "id": cid,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": args if isinstance(args, str) else json.dumps(args),
                },
            }
        ],
    }


def _result(cid, content, ts):
    return {
        "role": "tool",
        "content": content if isinstance(content, str) else json.dumps(content),
        "tool_call_id": cid,
        "timestamp": ts,
    }


def _system(content):
    return {"role": "system", "content": content}


def _user(content, ts):
    return {"role": "user", "content": content, "timestamp": ts}


# ---------------------------------------------------------- admission (STAGE 1)


def test_paired_call_and_result_admit():
    rows = [
        _calls("c1", "terminal", {"command": "make build"}, 100.0),
        _result("c1", {"exit_code": 0, "output": "ok"}, 101.0),
    ]
    occurrences = _tool_trail_scan(rows)
    assert len(occurrences) == 1
    assert occurrences[0].tool == "terminal"
    assert occurrences[0].outcome == "exit 0"


def test_result_only_without_a_call_is_skipped():
    rows = [_result("c1", {"exit_code": 0}, 101.0)]
    assert _tool_trail_scan(rows) == []


def test_ghost_twins_collapse_to_one_unit():
    """NOTE C: re-persisted rows (same tool_call_id) feed one unit identity."""
    rows = [
        _calls("c1", "terminal", {"command": "git status"}, 1.0),
        _result("c1", {"output": "clean", "exit_code": 0}, 2.0),
        _calls("c1", "terminal", {"command": "git status"}, 2.00001),
        _result("c1", {"output": "clean", "exit_code": 0}, 2.00002),
    ]
    occ = _tool_trail_scan(rows)
    assert len(occ) == 1, "ghost twins must not duplicate the action"
    assert len(_tool_trail_lines(occ)) == 1


def test_signal_pure_admission_includes_void_and_error():
    """COND 1: admission never filters on content shape — every row enters."""
    rows = [
        _calls("c1", "terminal", {"command": "noop"}, 1.0),
        _result("c1", "", 2.0),
        _calls("c2", "terminal", {"command": "frobnicate"}, 3.0),
        _result("c2", {"output": "x" * 2000, "exit_code": 2}, 4.0),
        _calls("c3", "terminal", {"command": "ls"}, 5.0),
        _result("c3", {"output": "y", "exit_code": 0}, 6.0),
    ]
    occ = _tool_trail_scan(rows)
    assert len(occ) == 3
    assert [item.outcome_class for item in occ] == ["void", "error", "ok"]


# --------------------------------------------------- target + outcome shape


def test_full_args_rows_render_the_command():
    rows = [
        _calls("c1", "terminal", {"command": "make build"}, 100.0),
        _result("c1", {"exit_code": 0, "output": "ok"}, 101.0),
    ]
    line = _tool_trail_lines(_tool_trail_scan(rows))[0]
    assert line.key == "terminal: make build — exit 0"


def test_empty_args_rows_recover_the_target_from_the_result_stub():
    """t_235275a0: dropped arguments must not cost the filename/command."""
    rows = [
        _calls("c1", "terminal", "{}", 100.0),
        _result("c1", "[terminal] ran `make build` -> exit 0, 1 lines output", 101.0),
        _calls("c2", "write_file", "{}", 200.0),
        _result("c2", '{"resolved_path": "/tmp/trace_506k.py", "bytes_written": 5}', 201.0),
    ]
    lines = _tool_trail_lines(_tool_trail_scan(rows))
    assert lines[0].key == "terminal: make build — exit 0"
    assert lines[1].key == "write_file: /tmp/trace_506k.py — ok"


def test_multi_line_argument_collapses_to_one_line():
    long_path = (
        "/tmp/long/nested/dir/that/needs/truncation/because/it/is/"
        "really/quite/long/indeed/file_that_keeps_going"
    )
    rows = [
        _calls("c1", "read_file", {"path": long_path, "other": "x" * 200}, 1.0),
        _result("c1", {"exit_code": 0, "output": "ok"}, 2.0),
    ]
    line = _tool_trail_lines(_tool_trail_scan(rows))[0]
    assert line.text.count("\n") == 0
    assert "…" in line.text
    assert len(line.text) < 200


def test_line_format_is_one_line_with_provenance():
    """Unit format: one line, no raw newlines; provenance rides the fields."""
    rows = [
        _calls("c1", "read_file", {"path": "/home/ekl/agent/x.py", "offset": 3}, 50.0),
        _result("c1", {"content": "body", "exit_code": 0}, 51.0),
    ]
    lines = _tool_trail_lines(_tool_trail_scan(rows))
    assert len(lines) == 1
    text = lines[0].text
    assert text.count("\n") == 0
    assert text == "[TOOL]: read_file: /home/ekl/agent/x.py — exit 0"
    assert lines[0].stamp == 51.0
    assert icf._render_unit(lines[0]).endswith("(00:00:51)")


def test_pair_as_unit_error_then_fix_one_line():
    """COND 2 / NOTE A: a chain of one class collapses to one representative.

    The t_235275a0 unit format supersedes the fused-interval variant: the
    pair renders ONE stamp (the outcome row's clock) and the span lives in
    the ``→``-joined outcome labels.
    """
    rows = [
        _calls("c1", "terminal", {"command": "make build"}, 100.0),
        _result("c1", {"exit_code": 1, "output": "boom"}, 101.0),
        _calls("c2", "terminal", {"command": "make build"}, 200.0),
        _result("c2", {"exit_code": 0, "output": "ok"}, 201.0),
    ]
    assert len(_tool_trail_scan(rows)) == 2
    line = _tool_trail_lines(_tool_trail_scan(rows))[0]
    assert line.text == "[TOOL]: terminal: make build — exit 1→exit 0"
    assert line.key == "terminal: make build — exit 1→exit 0"
    assert line.text.count("\n") == 0
    assert line.stamp == 101.0


def test_pairs_split_on_outcome_change():
    """COND 2: runs of one class collapse; a class change starts a new
    pair-as-unit segment (segments carry at most two runs)."""
    rows = [
        _calls("c1", "terminal", {"command": "make build"}, 100.0),
        _result("c1", {"exit_code": 0, "output": "ok"}, 101.0),
        _calls("c2", "terminal", {"command": "make build"}, 200.0),
        _result("c2", {"exit_code": 1, "output": "boom"}, 201.0),
        _calls("c3", "terminal", {"command": "make build"}, 300.0),
        _result("c3", {"exit_code": 0, "output": "ok"}, 301.0),
    ]
    lines = _tool_trail_lines(_tool_trail_scan(rows))
    assert len(lines) == 2
    assert lines[0].key == "terminal: make build — exit 0→exit 1"
    assert lines[1].key == "terminal: make build — exit 0"
    assert lines[0].stamp == 101.0 and lines[1].stamp == 301.0


def test_two_runs_of_one_class_collapse_to_one_line():
    """COND 2: repeats of one class collapse to one representative."""
    rows = [
        _calls("c1", "terminal", {"command": "make build"}, 100.0),
        _result("c1", {"exit_code": 0, "output": "ok"}, 101.0),
        _calls("c2", "terminal", {"command": "make build"}, 200.0),
        _result("c2", {"exit_code": 0, "output": "ok"}, 201.0),
    ]
    lines = _tool_trail_lines(_tool_trail_scan(rows))
    assert len(lines) == 1
    assert lines[0].text == "[TOOL]: terminal: make build — exit 0"


def test_empty_args_rows_still_admit_the_action():
    """Empty args must not exclude an action (bug-5 spill fixture)."""
    rows = [
        _calls("c1", "terminal", {}, 100.0),
        _result("c1", {"exit_code": 0, "output": "ok"}, 101.0),
    ]
    occurrences = _tool_trail_scan(rows)
    assert len(occurrences) == 1
    line = _tool_trail_lines(occurrences)[0]
    # Nothing recoverable — the line references the call, never a bare name.
    assert line.key == "terminal → tool_call_id c1 — exit 0"


def test_tell_renders_as_a_call_reference():
    """``tell → tool_call_id …`` — the message body is never swallowed."""
    rows = [
        _calls("c1", "tell", {"agent": "gopher", "message": "the long body"}, 100.0),
        _result("c1", '{"dispatched": true, "note": "notified gopher"}', 101.0),
    ]
    line = _tool_trail_lines(_tool_trail_scan(rows))[0]
    assert line.key == "tell → tool_call_id c1 — ok"
    assert "long body" not in line.text


def test_echo_marker_spine_never_reaches_the_ledger():
    """The echo marker is a spine unit, not a tool-trail line."""
    rows = [
        _calls("c1", "terminal", {}, 100.0),
        _result("c1", "[other noise]\n=== verify pipeline ===", 101.0),
        _calls("c2", "terminal", {"command": "echo noise"}, 200.0),
        _result("c2", {"exit_code": 0, "output": "=== verify pipeline ==="}, 201.0),
    ]
    lines = _tool_trail_lines(_tool_trail_scan(rows))
    # Both rows still admit as tool actions; the marker label is not duplicated
    assert len(lines) == 2
    assert "===" not in lines[0].key


# --------------------------------------------------------- Stage 2: salience


def test_own_path_salience_orders_path_command_edit_exit():
    """Text-shape salience on the trail's own path (never llmslim_selector)."""
    rich = _trail_salience("[TOOL]: patch: /home/ekl/agent/x.py — exit 1→exit 0")
    poor = _trail_salience("[TOOL]: terminal: hello — ok")
    assert rich >= 3 and poor == 0
    assert rich > poor


# ------------------------------------------------------------- Stage 3: quota


def test_quota_ceiling_caps_emission():
    """CEILING binds: the trail never outranks real content (anti-bug-4)."""
    rows = []
    for index in range(40):
        rows.append(_calls(f"c{index}", "terminal", {"command": f"step-{index} run"}, 10.0 + index))
        rows.append(_result(f"c{index}", {"output": "ok", "exit_code": 0}, 10.5 + index))
    lines = _tool_trail_lines(_tool_trail_scan(rows))
    assert len(lines) == 40
    budget = _tool_trail_budget(400)  # 5% -> 20 tokens
    telemetry: dict = {}
    kept = _select_trail_lines(lines, budget, selection_telemetry=telemetry)
    info = telemetry["tool_trail"]
    assert 1 <= len(kept) < 40, "cap must bite on a 40-line trail"
    assert info["emitted_lines"] == len(kept)
    assert info["emitted_tokens"] <= budget + max(line.token_count for line in lines)
    assert sum(line.token_count for line in kept) <= info["emitted_tokens"]


def test_carried_lines_converge_across_folds():
    """Convergence guard: already-carried lines are skipped on re-emission."""
    rows = [
        _calls("c1", "terminal", {"command": "pytest tests/agent"}, 100.0),
        _result("c1", {"output": "", "exit_code": 0}, 101.0),
    ]
    lines = _tool_trail_lines(_tool_trail_scan(rows))
    first = _select_trail_lines(lines, 500)
    assert first
    rendered = "\n".join(line.text for line in first)
    assert any("pytest tests/agent" in key for key in _carried_trail_keys(rendered))
    second = _select_trail_lines(lines, 500, previous_summary=rendered)
    assert second == [], "carried line must not regrow"


# ------------------------------------- t_235275a0: collapsed tool units = heap


def test_collapsed_tool_units_carry_provenance():
    rows = [
        _calls("c1", "terminal", {"command": "make build"}, 100.0),
        _result("c1", {"exit_code": 0, "output": "ok"}, 101.0),
    ]
    rows[1]["id"] = 7
    units = _collapsed_tool_units(rows)
    assert len(units) == 1
    unit = units[0]
    assert unit.role == "tool"
    assert unit.row_id == 7
    assert unit.stamp == 101.0
    assert unit.text == "[TOOL]: terminal: make build — exit 0"
    assert icf._render_unit(unit) == "[#7] [TOOL]: terminal: make build — exit 0 (00:01:41)"


# ------------------------------------------------- emission through the build


def test_build_collapses_middle_actions_into_the_stream():
    """t_0a90cfdd + t_235275a0: middle actions collapse into the chronological
    stream; the trail seat is only the ledger for rows the heap cannot see
    (rung-destroyed tail rows) — no duplication across regions.
    """
    messages = [
        _system("sys"),
        _user("start", 1.0),
        _calls("c1", "terminal", {"command": "pytest tests/agent"}, 100.0),
        _result("c1", {"output": "boom", "exit_code": 1}, 101.0),
        _calls("c2", "terminal", {"command": "pytest tests/agent"}, 200.0),
        _result("c2", {"output": "", "exit_code": 0}, 201.0),
        _user("thanks", 300.0),
    ]
    fallback = build_internal_fallback(
        messages, protect_head_count=2, protect_last_n=1, target_tokens=4_000
    )
    summary = fallback.summary
    assert not re.search(r"(?m)^#{1,6}\s", summary)
    assert "pytest tests/agent — exit 1→exit 0" in summary
    assert fallback.selection_telemetry["tool_units_selected"] == 1


def test_build_captures_rung_destroyed_tail_actions():
    """Gate (b): rows the tail rungs destroy are digested, not lost."""
    messages = [
        _system("sys"),
        _user("start", 1.0),
        _calls("c1", "terminal", {"command": "git status"}, 100.0),
        _result("c1", {"output": "clean", "exit_code": 0}, 101.0),
        _calls("c2", "terminal", {"command": "deploy script --env prod"}, 200.0),
        _result("c2", {"output": "x" * 20000, "exit_code": 1}, 201.0),
        _user("continue", 300.0),
    ]
    fallback = build_internal_fallback(
        messages, protect_head_count=2, protect_last_n=2, target_tokens=1_500
    )
    summary = fallback.summary
    assert fallback.selection_telemetry["tool_trail"]["captured_tail_rows"] > 0, (
        "the rungs must hand their drops to the ledger"
    )
    assert "deploy script --env prod" in summary
    assert "git status" in summary


def test_no_duplication_with_kept_verbatim_tail():
    """Rows the tail keeps raw are never restated in the digest."""
    messages = [
        _system("sys"),
        _user("open", 1.0),
        _calls("c1", "terminal", {"command": "keep-me-raw"}, 100.0),
        _result("c1", {"output": "fine", "exit_code": 0}, 101.0),
        _user("hi", 200.0),
    ]
    fallback = build_internal_fallback(
        messages, protect_head_count=1, protect_last_n=2, target_tokens=4_000
    )
    assert "keep-me-raw" not in fallback.summary
    assert fallback.selection_telemetry["tool_trail"]["candidate_lines"] == 0
