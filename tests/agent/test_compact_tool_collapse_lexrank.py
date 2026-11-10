"""Unit tests for the deterministic compressor's collapse + LexRank integration.

t_0a90cfdd rulings (Evan, 2026-10-21): 1) collapse tool rows WITH
targets/exit codes, 2) bounded tool boost in the Heap only, 3) every emitted
unit carries ``[#xx]`` + role + timestamp, 4) window stays chronological
(NO manifest headers), 5) Protected/global area untouched.

t_235275a0 smoke-test spec (the emitted-block contract): the unit format is
``[#id] [ROLE]: text (HH:MM:SS)``; tool lines keep the target/filename/
command (``write_file: <path> — ok``, ``terminal: <command> — ok``,
``patch: <file> — ok``, ``tell → tool_call_id …``) even when the call
arguments were dropped by the corrupted-args sanitizer; there are NO
``##`` section headers anywhere in the payload; and no manifest content is
duplicated between the stream and the fold.
"""

import re

from agent import internal_compression_fallback as icf
from agent.internal_compression_fallback import (
    _TOOL_SCORE_FLOOR,
    _TrailLine,
    _Unit,
    _rank_units,
    build_internal_fallback,
)

# The emitted unit format (spec bullet 3).
_UNIT_LINE_RE = re.compile(r"^\[#\d+\] \[[A-Z]+\]: .*\(\d{2}:\d{2}:\d{2}\)$", re.S)


def _system(content: str) -> dict:
    return {"role": "system", "content": content, "id": 1, "timestamp": 0.0}


def _user(content: str, ts: float, row_id: int | None = None) -> dict:
    message = {"role": "user", "content": content, "timestamp": ts}
    if row_id is not None:
        message["id"] = row_id
    return message


def _calls(
    cid: str,
    name: str,
    args: dict | str,
    ts: float,
    row_id: int | None = None,
) -> dict:
    import json as _json

    message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": cid,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": args if isinstance(args, str) else _json.dumps(args),
                },
            }
        ],
        "timestamp": ts,
    }
    if row_id is not None:
        message["id"] = row_id
    return message


def _result(
    cid: str,
    content: dict | str,
    ts: float,
    row_id: int | None = None,
) -> dict:
    import json as _json

    message = {
        "role": "tool",
        "content": content if isinstance(content, str) else _json.dumps(content),
        "tool_call_id": cid,
        "timestamp": ts,
    }
    if row_id is not None:
        message["id"] = row_id
    return message


# ------------------------------------------------------- rulings 1 + 2 + 3


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
    # t_235275a0: no section headers — the tool line is a stream unit.
    assert not re.search(r"(?m)^#{1,6}\s", summary)
    # Ruling 3: [#xx] = the outcome row's id; the stamp is that row's own.
    assert "[#4] [TOOL]: terminal: pytest tests/agent — exit 1 (00:01:41)" in summary
    assert summary.startswith(icf.INTERNAL_FALLBACK_PREFIX)


def test_pair_as_unit_error_then_fix_one_line():
    """t_490b92fc NOTE A: the error->fix pair is ONE unit, outcome fused.

    t_235275a0 supersedes the fused-interval variant: the unit format is
    ``[#id] [ROLE]: text (HH:MM:SS)`` (one stamp — the outcome row's clock),
    and the pair's span lives in the ``→``-joined outcome labels.
    """
    messages = [
        _system("sys"),
        _user("start", 1.0, row_id=2),
        _calls("c1", "terminal", {"command": "pytest tests/agent"}, 100.0, row_id=3),
        _result("c1", {"output": "boom", "exit_code": 1}, 101.0, row_id=4),
        _calls("c2", "terminal", {"command": "pytest tests/agent"}, 200.0, row_id=5),
        _result("c2", {"output": "fixed", "exit_code": 0}, 201.0, row_id=6),
        _user("thanks", 300.0, row_id=7),
    ]
    fallback = build_internal_fallback(
        messages, protect_head_count=2, protect_last_n=1, target_tokens=8_000
    )
    summary = fallback.summary
    assert (
        re.search(
            r"\[TOOL\]: terminal: pytest tests/agent — exit 1→exit 0 \(\d{2}:\d{2}:\d{2}\)",
            summary,
        )
        is not None
    )
    assert summary.count("[TOOL]: terminal: pytest tests/agent") == 1


def test_tool_units_interleave_chronologically_with_prose():
    """Tool rows occupy their row position, never a separate section."""
    messages = [
        _system("sys"),
        _user("start", 1.0, row_id=2),
        _user("alphaword explains the widget migration in detail", 50.0, row_id=3),
        _calls("c1", "terminal", {"command": "build the widget"}, 100.0, row_id=4),
        _result("c1", {"output": "ok", "exit_code": 0}, 101.0, row_id=5),
        _user("betaword confirms the widget migration finished", 200.0, row_id=6),
        _user("thanks", 300.0, row_id=7),
    ]
    summary = build_internal_fallback(
        messages, protect_head_count=2, protect_last_n=1, target_tokens=4_000
    ).summary
    assert summary.index("alphaword") < summary.index("[TOOL]:") < summary.index("betaword")
    # Prose units carry handles + stamps too (ruling 3: emitted units).
    assert "[#3] [USER]: alphaword" in summary
    assert "[#6] [USER]: betaword" in summary


def test_tool_boost_is_bounded():
    messages = [_system("sys"), _user("start", 1.0)]
    tool = _Unit(
        text="[TOOL]: terminal: pytest tests/agent — exit 0",
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
    # The verbatim marker span survives; it is not ALSO a ledger line.
    assert summary.count("=== verify pipeline ===") == 1
    assert "terminal: echo" not in summary


# ------------------------------------- t_235275a0 emitted-block spec items


def test_no_section_headers_anywhere():
    """Spec item 4: NO section headers — chronological stream only."""
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
        _calls("c2", "terminal", {"command": "pytest -q"}, 200.0, row_id=5),
        _result("c2", {"output": "ok", "exit_code": 0}, 201.0, row_id=6),
        _user("thanks", 300.0, row_id=7),
    ]
    summary = build_internal_fallback(
        messages,
        protect_head_count=2,
        protect_last_n=1,
        target_tokens=4_000,
        marker_recent_count=2,
        plan_context="Implement the frobnicator pipeline step by step.",
        memory_context="[0.5] remember the frobnicator",
        previous_summary=(
            "[CONTEXT WINDOW COMPRESSED]\n\n"
            "[#9] [TOOL]: terminal: make lint — ok (00:09:09)\n\n"
            "[PRIOR CONTEXT SUMMARY]\nolder paragraph\n[END OF PRIOR CONTEXT SUMMARY]"
        ),
    ).summary
    assert not re.search(r"(?m)^#{1,6}\s", summary), summary
    assert "[CONTEXT WINDOW COMPRESSED]" in summary


def test_empty_args_calls_recover_target_from_result_stub():
    """Spec item 1: dropped arguments must not cost the filename/command."""
    messages = [
        _system("sys"),
        _user("start", 1.0, row_id=2),
        _calls("c1", "terminal", "{}", 100.0, row_id=3),
        _result(
            "c1",
            "[terminal] ran `make build` -> exit 1, 1 lines output",
            101.0,
            row_id=4,
        ),
        _calls("c2", "write_file", "{}", 200.0, row_id=5),
        _result(
            "c2",
            '{"bytes_written": 1426, "verified": true, "resolved_path":'
            ' "/tmp/trace_506k.py", "files_modified": ["/tmp/trace_506k.py"]}',
            201.0,
            row_id=6,
        ),
        _calls("c3", "patch", "{}", 300.0, row_id=7),
        _result("c3", "[patch] replace in /tmp/trace_506k.py (1,634 chars result)", 301.0, row_id=8),
        _user("thanks", 400.0, row_id=9),
    ]
    summary = build_internal_fallback(
        messages, protect_head_count=2, protect_last_n=1, target_tokens=8_000
    ).summary
    assert "[#4] [TOOL]: terminal: make build — exit 1 (00:01:41)" in summary
    assert "[#6] [TOOL]: write_file: /tmp/trace_506k.py — ok (00:03:21)" in summary
    assert "[#8] [TOOL]: patch: /tmp/trace_506k.py — ok (00:05:01)" in summary


def test_tell_renders_as_call_reference_not_body():
    """Spec item 1: ``tell → tool_call_id …`` — the body is never swallowed."""
    messages = [
        _system("sys"),
        _user("start", 1.0, row_id=2),
        _calls(
            "c1",
            "tell",
            {"agent": "gopher", "message": "Forensic read of /tmp/final.txt body"},
            100.0,
            row_id=3,
        ),
        _result("c1", '{"dispatched": true, "note": "notified gopher"}', 101.0, row_id=4),
        _user("thanks", 300.0, row_id=5),
    ]
    summary = build_internal_fallback(
        messages, protect_head_count=2, protect_last_n=1, target_tokens=4_000
    ).summary
    assert "[#4] [TOOL]: tell → tool_call_id c1 — ok (00:01:41)" in summary
    assert "Forensic read of /tmp/final.txt body" not in summary


def test_every_unit_carries_a_handle_role_tag_and_stamp():
    """Spec items 2+3: ``[#id] [ROLE]: text (HH:MM:SS)`` on every row unit."""
    messages = [
        _system("sys"),
        _user("start", 1.0, row_id=2),
        _calls("c1", "terminal", {"command": "make build"}, 100.0, row_id=3),
        _result("c1", {"output": "ok", "exit_code": 0}, 101.0, row_id=4),
        _user("thanks", 300.0, row_id=5),
    ]
    summary = build_internal_fallback(
        messages, protect_head_count=2, protect_last_n=1, target_tokens=4_000
    ).summary
    # Unit rows carry a numeric handle ([#123] [ROLE]: ...); the legend
    # line ([#N] = session row id ...) is not a unit and is excluded (it is
    # pinned by the emit-marker self-description contract instead).
    unit_starts = [
        line
        for line in summary.splitlines()
        if re.match(r"^\[#\d+\]", line)
    ]
    assert unit_starts
    for line in unit_starts:
        assert re.match(r"^\[#\d+\] \[[A-Z]+\]: ", line), line
        assert re.search(r"\(\d{2}:\d{2}:\d{2}\)$", line), line


def test_the_five_rulings_hold_in_one_emitted_block():
    """The t_0a90cfdd smoke assertion, against the t_235275a0 format."""
    messages = [
        _system("sys"),
        _user("start", 1.0, row_id=2),
        _user("alphaword explains the widget migration in detail", 50.0, row_id=3),
        _calls("c1", "terminal", {"command": "pytest tests/agent"}, 100.0, row_id=4),
        _result("c1", {"output": "boom", "exit_code": 1}, 101.0, row_id=5),
        _calls("c2", "terminal", {"command": "pytest tests/agent"}, 200.0, row_id=6),
        _result("c2", {"output": "fixed", "exit_code": 0}, 201.0, row_id=7),
        _user("thanks", 300.0, row_id=8),
    ]
    summary = build_internal_fallback(
        messages, protect_head_count=2, protect_last_n=1, target_tokens=8_000
    ).summary

    # 1) collapse-don't-discard WITH target + exit codes
    assert "terminal: pytest tests/agent — exit 1→exit 0" in summary
    # 2) bounded tool boost (unit tests above pin the ceiling)
    # 3) [#xx] + role + timestamps
    assert "[#5] [TOOL]:" in summary
    assert "(00:01:41)" in summary
    # 4) chronological, NO section headers
    assert not re.search(r"(?m)^#{1,6}\s", summary)
    positions = [
        summary.find("[USER]: alphaword explains the widget migration"),
        summary.find("[TOOL]: terminal: pytest tests/agent"),
    ]
    assert -1 not in positions and positions[0] < positions[1]
    # 5) Protected/global area untouched — nothing boosted in this payload
    assert "alphaword" in summary
