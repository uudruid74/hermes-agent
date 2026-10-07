"""Regression tests for t_4b1721cb — compaction ghost twins / fabricated timestamps.

Two production behaviors collaborated to destroy the agent's action timeline:

1. ``make_tool_result_message`` built tool dicts with NO ``timestamp`` key, so
   the first flush stamped them with the flush clock (_insert_message_rows'
   ``now_ts`` fallback) — already minutes late — and every in-place compaction
   re-insert (archive_and_compact) stamped the same content AGAIN with the
   compaction clock. The ornith forensics session showed twins at 23:41:38
   for patches that ran 23:36-23:40.
2. Transcript-rewrite flows (archive_and_compact, replace_messages,
   publish_compression_child) re-insert existing content without any
   timestamp provenance: verbatim copies of dated rows kept their dict
   timestamp, but rewritten copies (summary stubs sharing only
   ``tool_call_id``) and timestamp-less live dicts got a fresh clock.

Fix: tool dicts carry their true execution timestamp at build time, and the
rewrite flows harvest the original rows' clocks (keyed on stored content and
tool_call_id) so re-inserted copies inherit the real time, never the
compaction clock.
"""

import json
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from hermes_state import SessionDB
from agent.tool_dispatch_helpers import make_tool_result_message


def _seed_session(db: SessionDB, sid: str, n_turns: int = 4) -> list:
    """Insert dated turns through the REAL batch flush (returns live dicts)."""
    msgs = [{"role": "system", "content": "sys", "timestamp": 1_000.0}]
    for i in range(n_turns):
        msgs.append(
            {"role": "user", "content": f"question {i} " + "x" * 400,
             "timestamp": 10_000.0 + i * 2}
        )
        msgs.append(
            {"role": "assistant", "content": "working",
             "tool_calls": [
                 {"id": f"call_{i}", "type": "function",
                  "function": {"name": "patch",
                               "arguments": json.dumps({"path": f"f{i}.py"})}}
             ],
             "timestamp": 10_001.0 + i * 2}
        )
        msgs.append(
            {"role": "tool", "tool_call_id": f"call_{i}", "name": "patch",
             "content": json.dumps({"success": True, "diff": "d" * 600}),
             "timestamp": 10_002.0 + i * 2}
        )
    db.append_messages_batch(sid, list(msgs))
    return msgs


class TestToolResultBuildTimestamp:
    def test_tool_result_message_carries_true_timestamp(self):
        """Tool dicts are stamped at build (execution) time, not flush time."""
        before = time.time()
        msg = make_tool_result_message(
            "patch", '{"success": true}', "call_probe"
        )
        after = time.time()
        assert "timestamp" in msg
        assert before <= msg["timestamp"] <= after


class TestArchiveCompactKeepsTimestamps:
    def test_verbatim_copy_keeps_original_timestamp(self):
        """A re-inserted copy of stored content inherits the original row's
        clock — never the compaction clock."""
        with TemporaryDirectory() as tmp:
            db = SessionDB(db_path=Path(tmp) / "t.db")
            sid = "20261007_ts_verbatim"
            db.create_session(sid, "cli", model="test/model")
            _seed_session(db, sid)
            active_before = db.get_messages(sid)
            ts_by_tc = {
                m["tool_call_id"]: m["timestamp"]
                for m in active_before if m.get("tool_call_id")
            }

            # Rewritten transcript: verbatim user rows + rewritten stubs.
            compacted = [
                {"role": "system", "content": "sys", "timestamp": 1_000.0},
                {"role": "user", "content": "question 0 " + "x" * 400,
                 "timestamp": 10_000.0},
                # Summary-form rewrite: only the call id survives.
                {"role": "tool", "tool_call_id": "call_0",
                 "name": "patch",
                 "content": "[patch] replace in f0.py (600 chars result)"},
                # A timestamp-less live-shape tool row (verbatim content).
                {"role": "tool", "tool_call_id": "call_1",
                 "name": "patch",
                 "content": json.dumps({"success": True, "diff": "d" * 600})},
            ]
            db.archive_and_compact(sid, compacted)

            rows = {
                r["tool_call_id"]: r["timestamp"]
                for r in db.get_messages(sid)
                if r.get("tool_call_id")
            }
            # Rewritten stub for call_0: inherited call_0's REAL clock, not
            # now_ts (which would be ~1.7e9, far beyond the fixture's clocks).
            original = ts_by_tc["call_0"]
            lower = original if isinstance(original, float) else 10_002.0
            # The stamped fixture clock for call_0's flush is 10_002.0
            # (10_002.0 + i*2 for turn 0); the harvest must carry something
            # close to that, NOT a fresh stamp.
            assert rows["call_0"] < 20_000.0
            assert abs(rows["call_0"] - lower) < 1.0 or rows["call_0"] == lower
            # Verbatim-copy tool row (no timestamp key): same guarantee.
            assert rows["call_1"] < 20_000.0

    def test_compaction_clock_never_appears_on_reinserted_tool_rows(self):
        """The ghost-twin kill test: run archive_and_compact through the real
        in-place commit shape and assert NO active row carries the compaction
        clock for content that predates it."""
        with TemporaryDirectory() as tmp:
            db = SessionDB(db_path=Path(tmp) / "t.db")
            sid = "20261007_ts_no_clock"
            db.create_session(sid, "cli", model="test/model")
            _seed_session(db, sid, n_turns=6)

            import hermes_state
            from unittest.mock import patch as mock_patch

            compacted = [
                {"role": "user", "content": "[summary] " + "s" * 300,
                 "timestamp": 10_010.0},
                {"role": "tool", "tool_call_id": "call_5", "name": "patch",
                 "content": "[patch] replace in f5.py (1,200 chars result)"},
            ]
            # Freeze hermes_state's insert clock at 50_000 — distinguishable
            # from every fixture timestamp (≤10_012). Any re-inserted row
            # carrying it is a fabricated compaction stamp.
            with mock_patch.object(
                hermes_state.time, "time", lambda: 50_000.0
            ):
                db.archive_and_compact(sid, compacted)

            act = db.get_messages(sid)
            for r in act:
                if r.get("tool_call_id") == "call_5":
                    # The rewritten stub MUST carry the original call_5 row's
                    # clock (10_012.0 fixture), never 50_000.
                    assert r["timestamp"] < 20_000.0
            # Nothing else inherited the compaction clock either.
            assert all(
                (r.get("timestamp") or 0) < 40_000.0 for r in act
            ), f"compaction clock leaked onto re-inserted rows: {act}"