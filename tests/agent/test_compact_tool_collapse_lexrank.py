"""Tool rows collapse into LexRank — Evan's 2026-10-21 rulings (t_0a90cfdd).

1. Collapse, don't discard: tool rows become VCC-style one-line units
   (tool name + target + outcome, exit codes preserved, error->fix fused).
2. Feed the collapsed results into the LexRank pool with a small boost.
3. Emit [#xx] session-row handles + role tags + original timestamps.
4. Keep the window chronological — no manifest sections.
5. Boost is bounded and heap-only; the Protected/global area is untouched.
"""

import json
import re

import agent.internal_compression_fallback as icf
from agent.internal_compression_fallback import (
    _TOOL_SCORE_FLOOR,
    _rank_units,
    _Unit,
    build_internal_fallback,
)


def _calls(cid, name, args, ts, row_id=None):
    return {
        "role": "assistant",
        "content": "",
        "timestamp": ts,
        "id": row_id,
        "tool_calls": [
            {
                "id": cid,
                "call_id": cid,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
        ],
    }


def _result(cid, result, ts, row_id=None):
    content = result if isinstance(result, str) else json.dumps(result)
    return {
        "role": "tool",
        "tool_call_id": cid,
        "content": content,
        "timestamp": ts,
        "id": row_id,
    }


def _user(text, ts, row_id=None):
    return {"role": "user", "content": text, "timestamp": ts, "id": row_id}


def _system(text):
    return {"role": "system", "content": text, "timestamp": 0.0, "id": 1}


# ------------------------------------------------------- ruling 1: collapse


def test_middle_tool_rows_collapse_into_lexrank_with_provenance():
    """Rulings 1+2+3: collapsed tool line in the heap with [#xx], role, stamp."""
    messages = [
        _system("sys"),
        _user("start", 1.0, row_id=2),
        _calls("c1", "terminal", {"command": "pytest tests/agent"}, 100.0, row_id=3),
        _result("c1", {"output": "boom", "exit_code": 1}, 101.0, row_id=4),
        _user("thanks", 300.0, row_id=5),
    ]
    fallback = build_internal_fallback(
        messages, protect_head_count=2, protect_last_n=1, target_tokens=4_000
    )
    summary = fallback.summary
    assert "## Relevant Earlier Context" in summary
    assert "[TOOL]: terminal: pytest tests/agent — exit 1" in summary
    # [#xx] = the outcome row's session id; the clock is the row's own stamp.
    assert "[#4] [TOOL]:" in summary
    assert re.search(r"\[TOOL\]:[^\n]*\(\d{2}:\d{2}:\d{2}\)", summary)
    # The middle is the heap's channel now — the ledger block is for rows the
    # heap cannot see (rung-destroyed tail rows), never a duplicate copy.
    assert "## Actions & Outcomes" not in summary
    assert fallback.selection_telemetry["tool_units_selected"] == 1


def test_plan_path_also_carries_collapsed_tool_units():
    """The plan branch routes tool_units through _add_plan_lexrank_area too."""
    messages = [
        _system("sys"),
        _user("start", 1.0, row_id=2),
        _calls("c1", "terminal", {"command": "pytest tests/agent"}, 100.0, row_id=3),
        _result("c1", {"output": "boom", "exit_code": 1}, 101.0, row_id=4),
        _user("thanks", 300.0, row_id=5),
    ]
    plan = "Task: Emergency compression\nGoal: Keep work moving\nStatus: active\n"
    fallback = build_internal_fallback(
        messages,
        protect_head_count=2,
        protect_last_n=1,
        target_tokens=4_000,
        plan_context=plan,
        minimal_plan_context=plan,
    )
    summary = fallback.summary
    assert "[#4] [TOOL]: terminal: pytest tests/agent — exit 1" in summary


def test_error_fix_pair_fuses_into_one_collapsed_line():
    """Ruling 1: exit codes preserved and error->fix fusion where applicable."""
    messages = [
        _system("sys"),
        _user("start", 1.0, row_id=2),
        _calls("c1", "terminal", {"command": "run the deploy"}, 100.0, row_id=3),
        _result("c1", {"output": "boom", "exit_code": 1}, 101.0, row_id=4),
        _calls("c2", "terminal", {"command": "run the deploy"}, 200.0, row_id=5),
        _result("c2", {"output": "", "exit_code": 0}, 201.0, row_id=6),
        _user("thanks", 300.0, row_id=7),
    ]
    fallback = build_internal_fallback(
        messages, protect_head_count=2, protect_last_n=1, target_tokens=4_000
    )
    summary = fallback.summary
    assert "[TOOL]: terminal: run the deploy — exit 1→exit 0" in summary
    assert summary.count("[TOOL]:") == 1


# ------------------------------------------------- ruling 4: chronological


def test_tool_units_interleave_chronologically_with_prose():
    """The heap stays one chronological stream of role-tagged lines."""
    messages = [
        _system("sys"),
        _user("start", 1.0, row_id=2),
        _user("alphaword explains the widget migration in detail", 50.0, row_id=3),
        _calls("c1", "terminal", {"command": "build the widget"}, 100.0, row_id=4),
        _result("c1", {"output": "ok", "exit_code": 0}, 101.0, row_id=5),
        _user("betaword confirms the widget migration finished", 200.0, row_id=6),
        _user("thanks", 300.0, row_id=7),
    ]
    fallback = build_internal_fallback(
        messages, protect_head_count=2, protect_last_n=1, target_tokens=4_000
    )
    summary = fallback.summary
    heap = summary.partition("## Relevant Earlier Context")[2]
    assert heap.index("alphaword") < heap.index("[TOOL]:") < heap.index("betaword")
    # Prose units carry handles + stamps too (ruling 3: emitted units).
    assert "[#3] [USER]: alphaword" in summary
    assert "[#6] [USER]: betaword" in summary


# ------------------------------------------------- ruling 5: bounded boost


def test_tool_boost_is_bounded_and_beats_prose_only_when_enabled():
    tool = _Unit(
        text="[TOOL]: terminal: make build — exit 0",
        order=(0, 0),
        token_count=8,
        role="tool",
    )
    prose = _Unit(
        text="[USER]: please implement the frobnicator pipeline carefully",
        order=(1, 0),
        token_count=8,
        role="user",
    )
    plain = {
        unit.order: score
        for unit, score in _rank_units([tool, prose], [], [], semantic=True)
    }
    boosted = {
        unit.order: score
        for unit, score in _rank_units(
            [tool, prose], [], [], semantic=True, tool_boost=True
        )
    }
    # Without the boost the external-role floor is the natural score (<= 1.0).
    assert plain[(0, 0)] < _TOOL_SCORE_FLOOR
    # With it: floor form, bounded ceiling, tool results above ordinary prose.
    assert _TOOL_SCORE_FLOOR <= boosted[(0, 0)] <= _TOOL_SCORE_FLOOR + 0.2
    assert boosted[(0, 0)] > boosted[(1, 0)]


def test_protected_selector_never_opts_into_the_boost(monkeypatch):
    """Ruling 5: heap selection boosts; the global area ranks without it."""
    calls = []
    original = icf._rank_units

    def spy(*args, **kwargs):
        calls.append((kwargs.get("tool_boost", False), kwargs.get("semantic", False)))
        return original(*args, **kwargs)

    monkeypatch.setattr(icf, "_rank_units", spy)
    messages = [_system("sys")] + [
        _user(f"detail line {index:04d} about the widget migration", float(index))
        for index in range(260)
    ]
    build_internal_fallback(
        messages, protect_head_count=1, protect_last_n=2, target_tokens=4_000
    )
    boosted = [tool for tool, semantic in calls if tool]
    unboosted_semantic_false = [
        tool for tool, semantic in calls if not semantic
    ]
    assert boosted, "heap selection must opt into the tool boost"
    assert unboosted_semantic_false, "the Protected selector must run in this build"
    assert not any(unboosted_semantic_false), (
        "the Protected/global selector must never see the boost"
    )


# ------------------------------------------------------ handle edge cases


def test_rows_without_ids_render_without_handles():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "start", "timestamp": 1.0},
        _calls("c1", "terminal", {"command": "make build"}, 100.0),
        _result("c1", {"output": "ok", "exit_code": 0}, 101.0),
        {"role": "user", "content": "thanks", "timestamp": 300.0},
    ]
    fallback = build_internal_fallback(
        messages, protect_head_count=2, protect_last_n=1, target_tokens=4_000
    )
    summary = fallback.summary
    assert "[TOOL]: terminal: make build — exit 0" in summary
    assert "[#" not in summary


def test_fold_carries_handles_without_duplicating_them():
    """Convergence guard: a re-selected [#N] line never appears twice."""
    messages = [
        _system("sys"),
        _user("start", 1.0, row_id=2),
        _calls("c1", "terminal", {"command": "pytest tests/agent"}, 100.0, row_id=3),
        _result("c1", {"output": "boom", "exit_code": 1}, 101.0, row_id=4),
        _user("thanks", 300.0, row_id=5),
    ]
    first = build_internal_fallback(
        messages, protect_head_count=2, protect_last_n=1, target_tokens=4_000
    ).summary
    second = build_internal_fallback(
        messages,
        protect_head_count=2,
        protect_last_n=1,
        target_tokens=4_000,
        previous_summary=first,
    ).summary
    assert first.count("[#4]") == 1
    assert second.count("[#4]") == 1


# -------------------------------------------- spine interaction (no dupes)


def test_echo_marker_commands_stay_on_the_spine_path():
    """The marker spine is the ONLY path for echo-marker commands."""
    messages = [
        _system("sys"),
        _user("start", 1.0, row_id=2),
        _calls(
            "c1",
            "terminal",
            {"command": 'echo "=== verify pipeline ==="; make build'},
            100.0,
            row_id=3,
        ),
        _result("c1", {"output": "ok", "exit_code": 0}, 101.0, row_id=4),
        _user("thanks", 300.0, row_id=5),
    ]
    fallback = build_internal_fallback(
        messages,
        protect_head_count=2,
        protect_last_n=1,
        target_tokens=4_000,
        marker_recent_count=2,
    )
    summary = fallback.summary
    assert "=== verify pipeline ===" in summary
    assert "[TOOL]:" not in summary
