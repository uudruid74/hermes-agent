"""Phase 2 tool-trail unit class (t_490b92fc): admission+quota -> salience -> digest.

Covers the council requirements directly: pair-as-unit with interval (COND 2 /
NOTE A), signal-pure admission (COND 1), (target, outcome-class) dedup, ghost
row collapse (NOTE C), the three gates opening together, own-path salience,
floor-and-ceiling quota, and the one-line digest with provenance.
"""

import json
import re

from agent.internal_compression_fallback import (
    _carried_trail_keys,
    _content_text,
    _is_tool_flow,
    _strip_tail_tool_rows,
    _template_visible_role,
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
                "call_id": cid,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
        ],
    }


def _result(cid, result, ts):
    content = result if isinstance(result, str) else json.dumps(result)
    return {
        "role": "tool",
        "tool_call_id": cid,
        "content": content,
        "timestamp": ts,
    }


def _user(text, ts):
    return {"role": "user", "content": text, "timestamp": ts}


def _system(text):
    return {"role": "system", "content": text, "timestamp": 0.0}


# ---------------------------------------------------------------- gate (a)


def test_gate_a_admits_what_the_template_hides():
    """The trail class admits exactly the rows _template_visible_role hides."""
    tool_row = {"role": "tool", "content": "r", "tool_call_id": "c1"}
    calls_row = {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]}
    assert _template_visible_role(tool_row) is None
    assert _template_visible_role(calls_row) is None
    assert _is_tool_flow(tool_row) and _is_tool_flow(calls_row)
    assert not _is_tool_flow({"role": "user", "content": "q"})
    assert not _is_tool_flow({"role": "assistant", "content": "prose"})


# ---------------------------------------------------------------- gate (b)


def test_gate_b_strip_returns_dropped_rows():
    """Whole-removal hands the removed rows to the caller (the ledger)."""
    messages = [
        _user("first turn", 1.0),
        _calls("c1", "terminal", {"command": "make build"}, 2.0),
        _result("c1", {"output": "done", "exit_code": 0}, 3.0),
        _user("second turn", 4.0),
    ]
    out, dropped = _strip_tail_tool_rows(messages, tail_start=0)
    assert len(out) == 2 and [row["role"] for row in out] == ["user", "user"]
    assert [row["role"] for row in dropped] == ["assistant", "tool"]
    assert dropped[1]["tool_call_id"] == "c1"


# ---------------------------------------------------------------- gate (c)


def test_gate_c_structured_content_yields_text():
    """Non-str content (dict/list parts) no longer falls through to ''."""
    assert _content_text({"output": "boom"}) == "boom"
    assert _content_text({"error": "bad"}) == "bad"
    assert _content_text({"output": "x", "message": "y"}) == "x\ny"
    assert _content_text([{"type": "tool_result", "output": "x"}]) == "x"
    # Pinned pre-existing behavior for the multimodal shapes:
    assert _content_text([{"type": "text", "text": "hi"}, {"type": "image_url"}]) == "hi"


def test_gate_c_outcome_read_from_raw_content():
    """A decoded dict's exit_code survives into the ledger outcome."""
    rows = [
        _calls("c1", "terminal", {"command": "probe"}, 1.0),
        {"role": "tool", "tool_call_id": "c1", "content": {"output": "x", "exit_code": 3},
         "timestamp": 2.0},
    ]
    occ = _tool_trail_scan(rows)
    assert len(occ) == 1
    assert occ[0].outcome == "exit 3" and occ[0].outcome_class == "error"


# ---------------------------------------------------------- Stage 1: pair


def test_pair_as_unit_error_then_fix_one_line_with_interval():
    """An error->fix pair survives as ONE ledger line carrying t_first–t_second."""
    rows = [
        _calls("c1", "terminal", {"command": "pytest tests/agent"}, 100.0),
        _result("c1", {"output": "boom", "exit_code": 1}, 101.0),
        _calls("c2", "terminal", {"command": "pytest tests/agent"}, 200.0),
        _result("c2", {"output": "", "exit_code": 0}, 221.0),
    ]
    lines = _tool_trail_lines(_tool_trail_scan(rows))
    assert len(lines) == 1, "the pair must not dedup into two competing rows"
    line = lines[0]
    assert "exit 1→exit 0" in line.text
    assert line.text.startswith("- terminal: pytest tests/agent — ")
    # NOTE A: the interval (t_first–t_second), wall-clock, not one snapshot.
    match = re.search(r"\((\d{2}:\d{2}:\d{2})–(\d{2}:\d{2}:\d{2})\)$", line.text)
    assert match, f"pair line must carry its interval: {line.text}"
    assert match.group(1) != match.group(2)


def test_dedup_key_collapses_same_class_repeats():
    """Repeats of one (target, outcome-class) collapse to one representative."""
    rows = []
    for index in range(3):
        rows.append(_calls(f"c{index}", "read_file", {"path": "/home/ekl/a.py"}, 10.0 + index))
        rows.append(_result(f"c{index}", {"content": "x", "exit_code": 0}, 10.5 + index))
    lines = _tool_trail_lines(_tool_trail_scan(rows))
    assert len(lines) == 1
    assert "/home/ekl/a.py" in lines[0].text


def test_ghost_twins_collapse_by_tool_call_id():
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


# --------------------------------------------------------- Stage 2: salience


def test_own_path_salience_orders_path_command_edit_exit():
    """Text-shape salience on the trail's own path (never llmslim_selector)."""
    rich = _trail_salience("- patch: /home/ekl/agent/x.py — exit 1→exit 0 (10:00–10:05)")
    poor = _trail_salience("- terminal: hello — ok (10:00)")
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
    telemetry = {}
    out = _carry(lines, budget, telemetry)
    kept = telemetry["tool_trail"]["emitted_lines"]
    assert 1 <= kept < 40, "cap must bite on a 40-line trail"
    assert telemetry["tool_trail"]["emitted_tokens"] <= budget + max(
        line.token_count for line in lines
    )
    assert out.count("## Actions & Outcomes") == 1


def _carry(lines, budget, telemetry):
    from agent.internal_compression_fallback import _carry_tool_trail

    return _carry_tool_trail(
        "base summary", lines, budget, selection_telemetry=telemetry
    )


# ------------------------------------------------- emission through the build


def test_build_emits_digest_for_middle_actions():
    """The compressed middle's actions land in the one-line digest block."""
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
    assert "## Actions & Outcomes" in fallback.summary
    assert "pytest tests/agent — exit 1→exit 0" in fallback.summary
    assert fallback.selection_telemetry["tool_trail"]["emitted_lines"] == 1


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
    assert "## Actions & Outcomes" in summary
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
    assert "## Actions & Outcomes" not in fallback.summary


def test_carried_lines_are_not_re_admitted():
    """Convergence guard: the fold's carried lines earn no new seat."""
    rows = [
        _calls("c1", "terminal", {"command": "pytest tests/agent"}, 100.0),
        _result("c1", {"output": "", "exit_code": 0}, 101.0),
    ]
    lines = _tool_trail_lines(_tool_trail_scan(rows))
    telemetry: dict = {}
    first = _carry(lines, 500, telemetry)
    assert first.count("pytest tests/agent") == 1
    from agent.internal_compression_fallback import _carry_tool_trail

    second = _carry_tool_trail(
        first, lines, 500, previous_summary=first, selection_telemetry={}
    )
    assert second.count("pytest tests/agent") == 1, "carried line must not regrow"
    keys = _carried_trail_keys(first)
    assert any("pytest tests/agent" in key for key in keys)


def test_line_format_is_one_line_with_provenance():
    """STAGE 3: one line per action — tool, target, outcome, interval."""
    rows = [
        _calls("c1", "read_file", {"path": "/home/ekl/agent/x.py", "offset": 3}, 50.0),
        _result("c1", {"content": "body", "exit_code": 0}, 51.0),
    ]
    lines = _tool_trail_lines(_tool_trail_scan(rows))
    assert len(lines) == 1
    text = lines[0].text
    assert text.count("\n") == 0
    assert re.fullmatch(
        r"- read_file: /home/ekl/agent/x\.py — exit 0 \(\d{2}:\d{2}:\d{2}–\d{2}:\d{2}:\d{2}\)",
        text,
    ), text