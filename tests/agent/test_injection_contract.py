"""Injection contract + model-view payload rendering (Evan, 2026-10-20).

"Everything should be injected with original timestamps, in time order, no
duplicates. Let's get rid of all the headers — just a single 'compressed
previous context'. The markers can be in the database, but let's not let it
take up space in the models context window."

Three guarantees under test:
1. A compaction payload row is clamped to its seam and the re-inserted batch
   lands in timestamp order (id order == time order for replay).
2. The archived trail keeps one copy per identity — no duplicate rows
   accumulate across compaction passes (the "ghost-twin preamble" shape).
3. The model-fed view strips payload scaffolding to the single transport
   marker; the raw DB view keeps the structure.
"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from hermes_state import SessionDB


def _db(tmp: str) -> SessionDB:
    db = SessionDB(db_path=Path(tmp) / "state.db")
    # FK: messages.session_id -> sessions.id; every test uses its own sid.
    for _sid in ("s1", "s2", "s3", "s4", "s5", "s6"):
        db.create_session(_sid, "cli", model="test/model")
    return db


PAYLOAD = (
    "[CONTEXT WINDOW COMPRESSED]\n\n"
    "## Protected Context\n\n[USER]: keep me\n\n"
    "## Relevant Earlier Context\n\nearlier fact\n\n"
    "## Session Notes\n\n- a note\n\n"
    "## Prior Context Summary\n\nfolded prior\n\n"
    "[END OF PRIOR CONTEXT SUMMARY]\n\n"
    "## Verbatim Recent Context\n"
    "The messages after this summary marker are preserved verbatim.\n"
)


class TestInjectionTimeline:
    def test_summary_clamped_to_seam_and_id_order_is_time_order(self):
        with TemporaryDirectory() as tmp:
            db = _db(tmp)
            db.append_messages_batch("s1", [
                {"role": "user", "content": "early " + "a" * 700, "timestamp": 100.0},
                {"role": "assistant", "content": "mid " + "b" * 700, "timestamp": 200.0},
                {"role": "user", "content": "late " + "c" * 700, "timestamp": 300.0},
                {"role": "assistant", "content": "latest " + "d" * 700, "timestamp": 400.0},
            ])
            # Gopher 2026-10-19 shape: the summary's stamp lands INSIDE the
            # kept tail's range (350 ∈ [300, 400]) — a broken seam that put
            # a "compressed up to here" row in the middle of live history.
            summary = {
                "role": "user",
                "content": PAYLOAD + "\n" + "S" * 700,
                "_compressed_summary": True,
                "timestamp": 350.0,
            }
            head = {"role": "user", "content": "early " + "a" * 700, "timestamp": 100.0}
            tail = [
                {"role": "user", "content": "late " + "c" * 700, "timestamp": 300.0},
                {"role": "assistant", "content": "latest " + "d" * 700, "timestamp": 400.0},
            ]
            db.archive_and_compact("s1", [head, summary] + tail)

            rows = db.get_messages("s1")
            stamps = [r["timestamp"] for r in rows]
            assert stamps == sorted(stamps), f"id order broke time order: {stamps}"
            summary_row = next(
                r for r in rows
                if str(r.get("content") or "").startswith("[CONTEXT WINDOW COMPRESSED]")
            )
            assert 100.0 < summary_row["timestamp"] < 300.0, summary_row["timestamp"]

    def test_batch_with_out_of_order_rows_is_sorted_at_injection(self):
        with TemporaryDirectory() as tmp:
            db = _db(tmp)
            db.append_messages_batch("s2", [
                {"role": "user", "content": "u " + "x" * 700, "timestamp": 100.0},
            ])
            # Deliberately out-of-order batch (a pre-existing upstream list
            # order bug): the later row is listed first.
            db.archive_and_compact("s2", [
                {"role": "assistant", "content": "b " + "y" * 700, "timestamp": 300.0},
                {"role": "user", "content": "u " + "x" * 700, "timestamp": 100.0},
                {"role": "assistant", "content": "c " + "z" * 700, "timestamp": 200.0},
            ])
            rows = db.get_messages("s2")
            stamps = [r["timestamp"] for r in rows]
            assert stamps == sorted(stamps), stamps

    def test_unresolved_stamp_keeps_tool_pair_adjacent(self):
        """Zephyr 2026-10-20 lead: a row with no resolvable stamp must not
        sort to the batch's END (which would split its tool-call/result
        pair). It inherits its neighbour's clock and keeps its position."""
        with TemporaryDirectory() as tmp:
            db = _db(tmp)
            db.append_messages_batch("s2", [
                {"role": "user", "content": "u " + "x" * 700, "timestamp": 100.0},
            ])
            db.archive_and_compact("s2", [
                {"role": "user", "content": "u " + "x" * 700, "timestamp": 100.0},
                # carrier: NO timestamp key, unharvestable content
                {
                    "role": "assistant", "content": "",
                    "tool_calls": json.dumps([{"id": "call_pair", "name": "terminal"}]),
                },
                # its tool result carries the pair's clock
                {
                    "role": "tool", "tool_call_id": "call_pair",
                    "content": "result body " + "y" * 700, "timestamp": 300.0,
                },
            ])
            rows = db.get_messages("s2")
            carrier = next(
                r for r in rows if r.get("tool_calls")
            )
            result = next(r for r in rows if r.get("tool_call_id") == "call_pair")
            assert carrier["id"] < result["id"], (
                f"pair split: carrier id={carrier['id']} result id={result['id']}"
            )


class TestArchiveTrailDedup:
    def test_identical_copies_do_not_accumulate_across_passes(self):
        with TemporaryDirectory() as tmp:
            db = _db(tmp)
            batch = [
                {"role": "user", "content": "u " + "x" * 300, "timestamp": 100.0},
                {"role": "assistant", "content": "a " + "y" * 300, "timestamp": 200.0},
            ]
            db.append_messages_batch("s3", [dict(m) for m in batch])
            # Two compaction passes over byte-identical content (the
            # ghost-twin preamble shape: one 48KB row re-copied per pass).
            db.archive_and_compact("s3", [dict(m) for m in batch])
            db.archive_and_compact("s3", [dict(m) for m in batch])
            db.archive_and_compact("s3", [dict(m) for m in batch])

            all_rows = db.get_messages("s3", include_inactive=True)
            archived = [
                r for r in all_rows if not r["active"] and r.get("compacted")
            ]
            user_copies = [r for r in archived if r["content"].startswith("u ")]
            asst_copies = [r for r in archived if r["content"].startswith("a ")]
            assert len(user_copies) == 1, [r["id"] for r in user_copies]
            assert len(asst_copies) == 1, [r["id"] for r in asst_copies]
            # The survivor is the original (lowest id).
            assert user_copies[0]["timestamp"] == 100.0

    def test_distinct_rewritten_twins_are_not_collapsed(self):
        """Summary-form rewrites share a call_id but not content — both
        artifacts must survive the dedupe."""
        with TemporaryDirectory() as tmp:
            db = _db(tmp)
            db.append_messages_batch("s4", [
                {"role": "user", "content": "u " + "x" * 300, "timestamp": 100.0},
                {
                    "role": "tool", "tool_call_id": "call_1",
                    "content": "original body " + "x" * 300, "timestamp": 110.0,
                },
            ])
            db.archive_and_compact("s4", [
                {"role": "user", "content": "u " + "x" * 300, "timestamp": 100.0},
                {
                    "role": "tool", "tool_call_id": "call_1",
                    "content": "[patch] replace in f.py (314 chars result)",
                    "timestamp": 110.0,
                },
            ])
            all_rows = db.get_messages("s4", include_inactive=True)
            twins = [r for r in all_rows if r.get("tool_call_id") == "call_1"]
            assert len(twins) == 2, [(r["id"], r["content"][:30]) for r in twins]

    def test_distinct_tool_call_carriers_with_empty_content_survive(self):
        """Zephyr 2026-10-20: assistant tool-call carriers carry content=''
        and no tool_call_id — they differ ONLY in tool_calls. The dedupe
        identity must include tool_calls or every such row collapses into
        one (32,814 distinct rows measured on Gopher's trail)."""
        with TemporaryDirectory() as tmp:
            db = _db(tmp)
            carrier = lambda n: {
                "role": "assistant", "content": "",
                "tool_calls": json.dumps([{"id": f"call_{n}", "name": "read_file"}]),
                "timestamp": 100.0 + n,
            }
            batch = [carrier(1), carrier(2), carrier(3)]
            db.append_messages_batch("s3", [dict(m) for m in batch])
            for _ in range(3):
                db.archive_and_compact("s3", [dict(m) for m in batch])
            all_rows = db.get_messages("s3", include_inactive=True)
            carriers = [
                r for r in all_rows
                if not r["active"] and r.get("tool_calls")
            ]
            assert len(carriers) == 3, [(r["id"], r.get("tool_calls")) for r in carriers]

    def test_identical_content_at_distinct_timestamps_survives(self):
        """Identical text at different clocks is distinct messages (the
        repeated 'Continue' turn class) — only same-clock copies collapse."""
        with TemporaryDirectory() as tmp:
            db = _db(tmp)
            batch = [
                {"role": "user", "content": "Continue", "timestamp": 100.0},
                {"role": "user", "content": "Continue", "timestamp": 200.0},
            ]
            db.append_messages_batch("s4", [dict(m) for m in batch])
            for _ in range(3):
                db.archive_and_compact("s4", [dict(m) for m in batch])
            all_rows = db.get_messages("s4", include_inactive=True)
            continues = [
                r for r in all_rows if not r["active"] and r["content"] == "Continue"
            ]
            assert len(continues) == 2, [(r["id"], r["timestamp"]) for r in continues]
            # ...while a same-clock byte-identical copy still collapses.
            assert len({r["timestamp"] for r in continues}) == 2


class TestModelViewPayloadRendering:
    def test_model_view_strips_scaffolding_db_keeps_it(self):
        with TemporaryDirectory() as tmp:
            db = _db(tmp)
            db.append_messages_batch("s5", [
                {
                    "role": "user",
                    "content": PAYLOAD + "S" * 200,
                    "timestamp": 100.0,
                },
                {"role": "assistant", "content": "reply", "timestamp": 200.0},
            ])

            conv = db.get_messages_as_conversation("s5")
            payload_msg = next(
                m for m in conv
                if str(m.get("content") or "").startswith("[CONTEXT WINDOW COMPRESSED]")
            )
            text = payload_msg["content"]
            # single marker, no scaffolding
            assert text.startswith("[CONTEXT WINDOW COMPRESSED]")
            for scaffold in (
                "## Protected Context",
                "## Relevant Earlier Context",
                "## Session Notes",
                "## Prior Context Summary",
                "## Verbatim Recent Context",
                "preserved verbatim",
                "[END OF PRIOR CONTEXT SUMMARY]",
            ):
                assert scaffold not in text, scaffold
            # content survives the strip
            for kept in ("keep me", "earlier fact", "a note", "folded prior"):
                assert kept in text, kept

            # The DB view keeps the full structure (markers may live there).
            raw = db.get_messages("s5")
            raw_payload = next(
                r for r in raw
                if str(r.get("content") or "").startswith("[CONTEXT WINDOW COMPRESSED]")
            )
            assert "## Protected Context" in raw_payload["content"]
            assert "## Verbatim Recent Context" in raw_payload["content"]

    def test_non_payload_rows_are_untouched(self):
        with TemporaryDirectory() as tmp:
            db = _db(tmp)
            quote = "## Protected Context\nthis is quoted content in a normal row"
            db.append_messages_batch("s6", [
                {"role": "user", "content": quote, "timestamp": 100.0},
            ])
            conv = db.get_messages_as_conversation("s6")
            assert conv[0]["content"] == quote
