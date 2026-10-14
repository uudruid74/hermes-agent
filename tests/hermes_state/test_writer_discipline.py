"""Regression tests for Compressor Phase 1 (council 2026-10-14, t_de77589d).

B2 — timestamp provenance:
  * ``_ts_harvest`` recovers clocks from ARCHIVED rows (ts lost after first
    archival re-inserted rows with the compaction clock).
  * Prefix matching is containment-verified: rows sharing a header (every
    merged ``[PRIOR CONTEXT ...]`` fold row) must not steal each other's
    clocks (the payload clock collapse — 8 rows, one frozen stamp, ~2h).

B4+B5 — writer discipline:
  * one row -> one unit-id: call rows key through the call identity
    (tool_call_id riding the (target, outcome-class) key) so raw results,
    summary stubs and re-persisted copies of the same call collapse to ONE
    durable row; body rows key on content fingerprint + real clock, so
    fabricated-stamp copies (class-2 whole-window re-writes) never land
    twice while genuine repeated prose keeps every occurrence.
  * block-level payload dedup: an identical payload block is one emission.

B3 — payload true-event-time: the merge-into-tail payload row carries the
snapshot's true event time (newest summarized turn's clock) instead of a
stale inherited or compaction stamp (exercised in
tests/agent/test_context_compressor_payload_timestamp.py).
"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from hermes_state import SessionDB


def _db(tmp):
    db = SessionDB(db_path=Path(tmp) / "t.db")
    db.create_session("s1", "cli", model="test/model")
    return db


STUB_B = "[terminal] ran `long-command --flags` -> exit 0, 1 lines output"
# Real merged-fold shape: the shared header (~118 chars) is LONGER than the
# blind first-64 key — only the bodies beyond it differ. This is exactly the
# bug-2 collision surface (every merged [PRIOR CONTEXT ...] row shares it).
_FOLD_SHARED = (
    "[PRIOR CONTEXT — for reference only; not a new message]\n\n"
    "[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]\n\n"
)
FOLD_A = _FOLD_SHARED + "generation A body " + "a" * 120
FOLD_B = _FOLD_SHARED + "generation B body " + "b" * 120


class TestOneRowPerUnitId:
    def test_call_row_survives_content_rewrites_as_one_row(self):
        """B4/B5: raw result, summary stub and copy of one call = ONE row."""
        with TemporaryDirectory() as tmp:
            db = _db(tmp)
            rows = [
                {
                    "role": "tool",
                    "tool_call_id": "call_x",
                    "tool_name": "terminal",
                    "content": json.dumps({"output": "raw " + "r" * 300}),
                    "timestamp": 10_002.0,
                }
            ]
            assert db.append_messages_batch("s1", rows) == 1
            # Summary-form rewrite of the SAME call (class-1, t_4b1721cb shape).
            stub = {
                "role": "tool",
                "tool_call_id": "call_x",
                "tool_name": "terminal",
                "content": "[terminal] ran `raw-command` -> exit 0, 1 lines out",
            }
            assert db.append_messages_batch("s1", [stub]) == 0
            assert db.append_messages_batch("s1", [dict(stub)]) == 0
            assert len(db.get_messages("s1", include_inactive=True)) == 1

    def test_class_2_window_rewrite_is_not_persisted_twice(self):
        """Whole-window re-writes (with or without rewritten stamps) never
        land again — the 1,210-from-336 / 155-click-copies class."""
        with TemporaryDirectory() as tmp:
            db = _db(tmp)
            window = [
                {"role": "user", "content": "question " + "q" * 300,
                 "timestamp": 10_000.0},
                {"role": "assistant", "content": "answer " + "a" * 300,
                 "timestamp": 10_001.0},
                {"role": "tool", "tool_call_id": "call_c",
                 "tool_name": "click",
                 "content": STUB_B, "timestamp": 10_002.0},
            ]
            assert db.append_messages_batch("s1", list(window)) == 3
            # Copy batch carrying the originals' stamps.
            copy_batch = [dict(m) for m in window]
            assert db.append_messages_batch("s1", copy_batch) == 0
            # Class-2 copy batch: stamps stripped (fabricated now_ts fallback).
            stripped = [
                {k: v for k, v in m.items() if k != "timestamp"} for m in window
            ]
            assert db.append_messages_batch("s1", stripped) == 0
            assert len(db.get_messages("s1", include_inactive=True)) == 3

    def test_genuine_repeated_prose_keeps_every_occurrence(self):
        """Same words at different real times are different events."""
        with TemporaryDirectory() as tmp:
            db = _db(tmp)
            rows = [
                {"role": "user", "content": "continue", "timestamp": 1.0},
                {"role": "user", "content": "continue", "timestamp": 2.0},
                {"role": "user", "content": "continue", "timestamp": 3.0},
            ]
            assert db.append_messages_batch("s1", rows) == 3
            assert len(db.get_messages("s1")) == 3

    def test_within_batch_duplicates_collapse(self):
        """11 identical assignment rows in one batch store once."""
        with TemporaryDirectory() as tmp:
            db = _db(tmp)
            row = {
                "role": "user",
                "content": "Incoming message from neo follows: assigned.",
                "timestamp": 1_791_923_559.010257,
            }
            batch = [dict(row) for _ in range(11)]
            assert db.append_messages_batch("s1", batch) == 1
            assert len(db.get_messages("s1")) == 1

    def test_block_level_payload_dedup(self):
        """Identical payload blocks dedup; distinct fold generations don't."""
        with TemporaryDirectory() as tmp:
            db = _db(tmp)
            block = {
                "role": "assistant",
                "content": "[CONTEXT WINDOW COMPRESSED] " + "p" * 500,
                "timestamp": 42.0,
            }
            assert db.append_messages_batch("s1", [dict(block)]) == 1
            # Re-persisted copy of the same block: skipped.
            assert db.append_messages_batch("s1", [dict(block)]) == 0
            # Copy with fabricated stamp: still one emission.
            bare = {"role": "assistant", "content": block["content"]}
            assert db.append_messages_batch("s1", [bare]) == 0
            # A DIFFERENT fold generation (distinct body) is its own row,
            # even if a clock collapse gave it an inherited stamp.
            gen2 = {
                "role": "assistant",
                "content": "[CONTEXT WINDOW COMPRESSED] " + "q" * 500,
                "timestamp": 42.0,
            }
            assert db.append_messages_batch("s1", [gen2]) == 1
            assert len(db.get_messages("s1")) == 2

    def test_metric_tool_carriers_with_same_clock_stay_distinct(self):
        """Empty-content tool_calls carriers differ by their tool_calls —
        same clock must not collapse two real calls."""
        with TemporaryDirectory() as tmp:
            db = _db(tmp)
            rows = [
                {"role": "assistant", "content": "", "tool_calls":
                    [{"id": "c1", "function": {"name": "read_file",
                                               "arguments": "{}"}}],
                 "timestamp": 10.0},
            ]
            assert db.append_messages_batch("s1", rows) == 1
            # Same clock, DIFFERENT call payload — a real second call.
            rows2 = [
                {"role": "assistant", "content": "", "tool_calls":
                    [{"id": "c2", "function": {"name": "terminal",
                                               "arguments": "{}"}}],
                 "timestamp": 10.0},
            ]
            assert db.append_messages_batch("s1", rows2) == 1

    def test_rewrite_flow_reinserts_compacted_window_by_design(self):
        """archive_and_compact re-persists its window even when the unit ids
        match rows it just archived — that re-insert IS the live set."""
        with TemporaryDirectory() as tmp:
            db = _db(tmp)
            window = [
                {"role": "user", "content": "question " + "q" * 300,
                 "timestamp": 10_000.0},
                {"role": "tool", "tool_call_id": "call_r",
                 "tool_name": "read_file", "content": STUB_B,
                 "timestamp": 10_002.0},
            ]
            db.append_messages_batch("s1", list(window))
            compacted = [dict(m) for m in window]  # identical identities
            # dict stamps preserve the originals' clocks → harvest keeps them
            db.archive_and_compact("s1", compacted)
            active = db.get_messages("s1")
            assert len(active) == 2
            by_cid = {m.get("tool_call_id"): m["timestamp"] for m in active}
            assert by_cid["call_r"] == 10_002.0


class TestHarvestProvenance:
    def test_clock_recovered_from_archived_original(self):
        """B2: ts lost after first archival is recovered from the archived
        row — a later re-insert of the same call keeps the TRUE clock, not
        the compaction clock."""
        with TemporaryDirectory() as tmp:
            db = _db(tmp)
            db.append_messages_batch("s1", [
                {"role": "tool", "tool_call_id": "call_a",
                 "tool_name": "patch",
                 "content": json.dumps({"success": True, "diff": "d" * 600}),
                 "timestamp": 10_002.0},
            ])
            # First compaction archives the call row WITHOUT re-inserting it:
            # afterwards the only row with call_a's clock is ARCHIVED.
            db.archive_and_compact("s1", [
                {"role": "user", "content": "[summary] " + "s" * 300,
                 "timestamp": 10_010.0},
            ])
            # Later rewrite re-persists a summary-form copy of the call.
            db.archive_and_compact("s1", [
                {"role": "user", "content": "[summary] " + "t" * 300,
                 "timestamp": 10_020.0},
                {"role": "tool", "tool_call_id": "call_a",
                 "tool_name": "patch",
                 "content": "[patch] replace in f.py (600 chars result)"},
            ])
            rows = [m for m in db.get_messages("s1")
                    if m.get("tool_call_id") == "call_a"]
            assert rows
            assert abs(rows[0]["timestamp"] - 10_002.0) < 1.0, (
                "stub must inherit call_a's ORIGINAL clock (10_002) "
                f"from the archived row, got {rows[0]['timestamp']}"
            )

    def test_shared_header_prefix_never_steals_clocks(self):
        """B2: the payload clock collapse — fold rows sharing a header must
        not inherit one frozen stamp; each rewrite keeps its own body's
        clock via CONTAINMENT-verified prefix matching."""
        with TemporaryDirectory() as tmp:
            db = _db(tmp)
            db.append_messages_batch("s1", [
                {"role": "assistant", "content": FOLD_A, "timestamp": 100.0},
                {"role": "assistant", "content": FOLD_B, "timestamp": 200.0},
            ])
            # Merge-into-tail shape: each row's rewrite KEEPS its stored body
            # as a prefix and appends a new summary below it.
            db.archive_and_compact("s1", [
                {"role": "assistant", "content": FOLD_A + "\n\nSUMMARY-2"},
                {"role": "assistant", "content": FOLD_B + "\n\nSUMMARY-3"},
            ])
            rows = sorted(
                (m for m in db.get_messages("s1") if "PRIOR CONTEXT" in m["content"]),
                key=lambda m: m["timestamp"],
            )
            assert len(rows) == 2
            asserted = [round(m["timestamp"], 3) for m in rows]
            assert asserted == [100.0, 200.0], (
                "each merged fold row must keep ITS OWN body's clock "
                f"(100 / 200), got {asserted}"
            )