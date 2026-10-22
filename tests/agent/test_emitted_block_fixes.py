"""Emitted-block fixes — zephyr handoff t_e4812748 (2026-10-22).

Bug 1  : note units render through ``_render_unit`` (handle + stamp).
Bug 1b : one legend line with the ready-to-run ``session_search`` command,
         worked example on a REAL handle from the payload, idempotent.
Bug 2  : the LLM envelope pair / boundary line are transport — stripped at
         fold re-ingest, and the model view keeps exactly ONE marker.
Bug 3  : head-truncate commands only; paths and prose keep their tail.
Bug 4  : the rendered clock is the named row's own — never the pair's other
         row (missing clock renders no clock).
"""

import json

from agent import internal_compression_fallback as icf
from agent.internal_compression_fallback import (
    _canonical_prior_summary,
    _lexrank_summary,
    _tool_trail_lines,
    _tool_trail_scan,
    _trail_ts,
    _Unit,
    _with_handle_legend,
)
from hermes_state import SessionDB


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


def _result(cid, content, ts, row_id=None):
    row = {
        "role": "tool",
        "content": content if isinstance(content, str) else json.dumps(content),
        "tool_call_id": cid,
        "timestamp": ts,
    }
    if row_id is not None:
        row["id"] = row_id
    return row


# ----------------------------------------------------------------- bug 1

def test_note_units_render_with_handle_and_stamp():
    note = _Unit(
        text="[-] Step 2 (denied: fix the clock)",
        order=(5, 0),
        token_count=8,
        is_note=True,
        role="user",
        row_id=42,
        stamp=1792555144.0775747,
    )
    out = _lexrank_summary([note])
    assert "- [#42]" in out  # bullet kept, handle added
    assert f"({_trail_ts(1792555144.0775747)})" in out


def test_synthesized_notes_render_plain():
    note = _Unit(text="- synthesized", order=(0, 0), token_count=3, is_note=True)
    out = _lexrank_summary([note])
    assert "[#" not in out
    assert "- synthesized" in out


# ---------------------------------------------------------------- bug 1b

PAYLOAD = "[CONTEXT WINDOW COMPRESSED]\n\n[#246036] [TOOL]: terminal: make build — ok (12:19:27)"


def test_legend_carries_ready_command_and_real_handle():
    out = _with_handle_legend(PAYLOAD, "20261022_045500_ab12")
    legend = [ln for ln in out.splitlines() if ln.startswith("[#N] = session row id")]
    assert len(legend) == 1
    # the literal, copy-pasteable command — nothing to derive
    assert (
        "session_search(session_id='20261022_045500_ab12', "
        "around_message_id=246036, window=5)"
    ) in legend[0]
    # and it lands right after the transport marker, before content
    lines = out.splitlines()
    assert lines[0] == "[CONTEXT WINDOW COMPRESSED]"
    assert lines[1] == legend[0]


def test_legend_idempotent_across_refolds():
    once = _with_handle_legend(PAYLOAD, "20261022_x")
    twice = _with_handle_legend(once, "20261022_x")
    assert twice == once


def test_legend_absent_without_handles():
    out = _with_handle_legend("[CONTEXT WINDOW COMPRESSED]\n\njust prose", "s")
    assert "[#N] =" not in out


# ----------------------------------------------------------------- bug 2

def test_fold_strips_llm_envelope_and_boundary():
    prior = (
        "[PRIOR CONTEXT — for reference only; not a new message]\n"
        "old prior fact that must survive\n\n"
        "[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]\n\n"
        "another carried fact\n\n"
        "--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---"
    )
    out = _canonical_prior_summary(prior, "fresh summary text")
    assert "[PRIOR CONTEXT — for reference only" not in out
    assert "[END OF PRIOR CONTEXT — COMPACTION" not in out
    assert "--- END OF CONTEXT SUMMARY" not in out
    assert "old prior fact that must survive" in out
    assert "another carried fact" in out


def test_model_view_keeps_one_marker_and_drops_envelope():
    merged = (
        "[PRIOR CONTEXT — for reference only; not a new message]\n"
        "carried prior body\n\n"
        "[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]\n\n"
        "[CONTEXT WINDOW COMPRESSED]\n\n"
        "[#7] [USER]: fresh unit line (03:26:52)"
    )
    out = SessionDB._strip_payload_scaffolding(merged)
    markers = [
        ln for ln in out.splitlines()
        if ln.strip().startswith(("[CONTEXT WINDOW COMPRESSED]", "[PRIOR CONTEXT"))
    ]
    assert len(markers) == 1
    assert "[END OF PRIOR CONTEXT — COMPACTION" not in out
    assert "[#7]" in out


# ----------------------------------------------------------------- bug 3

def test_command_target_head_truncates():
    rows = [
        _calls("c1", "terminal", {"command": "grep -rn 'x' " + "very/long/path " * 12}, 100.0),
        _result("c1", {"exit_code": 0, "output": "ok"}, 101.0, row_id=11),
    ]
    line = _tool_trail_lines(_tool_trail_scan(rows))[0].text
    assert "grep -rn" in line  # the head survives
    assert "…" in line


def test_path_target_keeps_tail():
    tail = "profiles/gopher/state.db"
    rows = [
        _calls("c1", "read_file", {"path": "/home/ekl/.hermes/" + "x" * 90 + tail}, 100.0),
        _result("c1", {"output": "ok"}, 101.0, row_id=12),
    ]
    line = _tool_trail_lines(_tool_trail_scan(rows))[0].text
    assert tail in line  # the filename survives
    assert "…" in line


# ----------------------------------------------------------------- bug 4

def test_missing_result_clock_renders_no_clock():
    rows = [
        _calls("c1", "terminal", {"command": "make build"}, 1792555000.0),
        _result("c1", {"exit_code": 0, "output": "ok"}, None, row_id=21),
    ]
    line = _tool_trail_lines(_tool_trail_scan(rows))[0]
    assert line.row_id == 21
    assert line.stamp is None  # never the call row's clock
    rendered = icf._render_unit(line)
    assert "[#21]" in rendered
    assert "(" not in rendered.split("] ", 1)[-1].rsplit("— ok", 1)[-1]


def test_stamp_is_the_named_rows_own_clock():
    rows = [
        _calls("c1", "terminal", {"command": "make build"}, 1792555000.0),
        _result("c1", {"exit_code": 0, "output": "ok"}, 1792555144.0775747, row_id=22),
    ]
    line = _tool_trail_lines(_tool_trail_scan(rows))[0]
    assert line.stamp == 1792555144.0775747
    assert f"({_trail_ts(1792555144.0775747)})" in icf._render_unit(line)
