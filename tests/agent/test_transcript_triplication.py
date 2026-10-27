"""Transcript triplication fixes (t_06eae500).

Zephyr's long-lived DM session accumulated byte-identical and lossy near-twin
rows when gateway restarts replayed the stored transcript and compaction
re-inserted it. Three mechanisms, three regressions pinned here:

1. Re-copy rule: an append-flow row whose core identity (role, content,
   tool_calls) AND exact clock already exist durably is a re-copy of that row,
   dropped even when a lossy rebuild stripped its sidecar columns. Genuine new
   messages always carry their own creation clock, so they survive — including
   thinking-different twins (the Zephyr 2026-10-20 contract).
2. Repair-path copy-on-write: _canonicalize_api_tool_calls must not mutate the
   live history's shared nested tool-call dicts (its repair fallback used to,
   leaving the next transcript rewrite to persist repaired/truncated arguments
   as a byte-different twin of the already-stored row).
3. Replay fidelity: _build_replay_entry must carry the persisted sidecar
   columns so a replay-derived re-insert is byte-identical to the stored row
   and archive_and_compact's no-duplicates DELETE collapses it.
"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from hermes_state import SessionDB


def _db(tmp: str) -> SessionDB:
    db = SessionDB(db_path=Path(tmp) / "state.db")
    for _sid in ("s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8"):
        db.create_session(_sid, "cli", model="test/model")
    return db


class TestReCopyRule:
    """Append flows drop re-copies of durable rows, never genuine messages."""

    def test_lossy_recopy_with_real_clock_dropped(self):
        with TemporaryDirectory() as tmp:
            db = _db(tmp)
            db.append_messages_batch("s1", [
                {"role": "assistant", "content": "hello world",
                 "timestamp": 1000.0, "reasoning": "think think",
                 "finish_reason": "stop"},
            ])
            # The class-2 re-persist shape: same content + same clock, the
            # sidecar columns stripped by a lossy rebuild.
            db.append_messages_batch("s1", [
                {"role": "assistant", "content": "hello world",
                 "timestamp": 1000.0, "finish_reason": "stop"},
            ])
            rows = db.get_messages("s1")
            assert len(rows) == 1
            assert rows[0]["reasoning"] == "think think"

    def test_repeated_content_with_distinct_clocks_survives(self):
        with TemporaryDirectory() as tmp:
            db = _db(tmp)
            db.append_messages_batch("s1", [
                {"role": "assistant", "content": "ok", "timestamp": 1000.0},
                {"role": "assistant", "content": "ok", "timestamp": 1000.5},
            ])
            assert len(db.get_messages("s1")) == 2

    def test_thinking_different_twin_with_own_clock_survives(self):
        # Zephyr 2026-10-20 contract: same visible body, different thinking,
        # distinct creation clocks = DISTINCT messages. Never merge.
        with TemporaryDirectory() as tmp:
            db = _db(tmp)
            db.append_messages_batch("s1", [
                {"role": "assistant", "content": "same text",
                 "timestamp": 1000.0, "reasoning": "alpha"},
                {"role": "assistant", "content": "same text",
                 "timestamp": 2000.0, "reasoning": "beta"},
            ])
            assert len(db.get_messages("s1")) == 2

    def test_fabricated_clock_sidecar_twin_kept(self):
        # Fabricated clocks keep the sidecar-aware fingerprint rule: the core
        # rule must NOT kick in and drop a thinking-different twin just
        # because its clock was never stamped.
        with TemporaryDirectory() as tmp:
            db = _db(tmp)
            db.append_messages_batch("s1", [
                {"role": "assistant", "content": "hello",
                 "timestamp": 1000.0, "reasoning": "alpha"},
                {"role": "assistant", "content": "hello",
                 "reasoning": "beta"},  # no timestamp -> fabricated
            ])
            assert len(db.get_messages("s1")) == 2

    def test_fabricated_clock_identical_recopy_still_dropped(self):
        with TemporaryDirectory() as tmp:
            db = _db(tmp)
            db.append_messages_batch("s1", [
                {"role": "assistant", "content": "hello",
                 "timestamp": 1000.0, "reasoning": "alpha"},
                {"role": "assistant", "content": "hello",
                 "reasoning": "alpha"},  # identical, fabricated clock
            ])
            assert len(db.get_messages("s1")) == 1

    def test_tool_rows_still_dedup_on_call_id(self):
        with TemporaryDirectory() as tmp:
            db = _db(tmp)
            db.append_messages_batch("s1", [
                {"role": "tool", "content": "result one",
                 "tool_call_id": "c1", "tool_name": "terminal",
                 "timestamp": 1000.0},
                {"role": "tool", "content": "result two",
                 "tool_call_id": "c1", "tool_name": "terminal",
                 "timestamp": 1000.0},
            ])
            assert len(db.get_messages("s1")) == 1


class TestRewriteFlowsUnaffected:
    """t_4b1721cb: rewrite flows harvest shared clocks for same-content rows
    BY DESIGN — the re-copy rule must never merge inside them."""

    def test_harvest_shared_clocks_do_not_merge_rewrite_batch(self):
        with TemporaryDirectory() as tmp:
            db = _db(tmp)
            db.append_messages_batch("s2", [
                {"role": "assistant", "content": "same visible text",
                 "timestamp": 1000.0, "reasoning": "alpha"},
                {"role": "assistant", "content": "same visible text",
                 "timestamp": 2000.0, "reasoning": "beta"},
            ])
            # Compaction re-insert: no timestamp keys -> both harvest the
            # SAME content-keyed clock, yet both must land (different
            # thinking = distinct rows).
            db.archive_and_compact("s2", [
                {"role": "assistant", "content": "same visible text",
                 "reasoning": "alpha"},
                {"role": "assistant", "content": "same visible text",
                 "reasoning": "beta"},
            ])
            active = db.get_messages("s2")
            assert len(active) == 2
            assert {m["reasoning"] for m in active} == {"alpha", "beta"}


class TestRepairPathCopyOnWrite:
    """_canonicalize_api_tool_calls must not mutate the live history."""

    def test_repair_fallback_leaves_live_history_untouched(self):
        from agent.conversation_loop import _canonicalize_api_tool_calls

        # A literal tab inside the string value: strict json.loads raises,
        # routing to the repair fallback (which re-serializes the args).
        bad_args = '{"command": "a\tb"}'
        live_msg = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "call_id": "call_1",
                    "response_item_id": "fc_1",
                    "type": "function",
                    "function": {"name": "terminal", "arguments": bad_args},
                }
            ],
        }
        api_messages = [live_msg.copy()]  # shallow: nested dicts are shared
        _canonicalize_api_tool_calls(api_messages)

        # The api copy carries the repaired arguments…
        repaired = api_messages[0]["tool_calls"][0]["function"]["arguments"]
        assert json.loads(repaired) == {"command": "a\tb"}
        # …but the live history keeps the original bytes: the next transcript
        # rewrite must not persist a byte-different twin of the stored row.
        assert live_msg["tool_calls"][0]["function"]["arguments"] == bad_args


class TestReplayRewriteTrailStaysClean:
    """End-to-end: replayed transcript re-inserted by compaction must collapse
    into the original rows instead of leaving a near-twin trail."""

    def test_replayed_window_reinsert_leaves_no_twins(self):
        from gateway.run import _build_gateway_agent_history

        with TemporaryDirectory() as tmp:
            db = _db(tmp)
            sid = "s3"
            db.append_messages_batch(sid, [
                {"role": "user", "content": "do the thing",
                 "timestamp": 1000.0, "message_id": "tg-1"},
                {"role": "assistant", "content": "doing the thing",
                 "timestamp": 1001.0, "reasoning": "let me think",
                 "finish_reason": "stop", "display_kind": "hidden"},
                {"role": "tool", "content": "thing done",
                 "tool_call_id": "c1", "tool_name": "terminal",
                 "timestamp": 1002.0},
            ])
            # Gateway restart: replay the stored transcript as agent history,
            # then a preflight compaction re-inserts the window — TWICE (the
            # 2026-10-27 restart storm shape).
            for _restart in range(2):
                history = db.get_messages_as_conversation(sid)
                agent_history, _ = _build_gateway_agent_history(history)
                window = [dict(m) for m in agent_history]
                db.archive_and_compact(sid, window)

            all_rows = db.get_messages(sid, include_inactive=True)
            archived = [m for m in all_rows if not m.get("active", 1)]
            active = [m for m in all_rows if m.get("active", 1)]
            assert len(active) == 3  # one live copy of each row

            def _body_key(m):
                # Exactly the no-duplicates DELETE's identity: every
                # persisted column except id/session/active/compacted.
                return (
                    m["role"], m["content"], m.get("tool_call_id"),
                    m.get("tool_calls"), m.get("tool_name"),
                    m.get("effect_disposition"), m.get("timestamp"),
                    m.get("token_count"), m.get("finish_reason"),
                    m.get("reasoning"), m.get("reasoning_content"),
                    m.get("reasoning_details"), m.get("codex_reasoning_items"),
                    m.get("codex_message_items"), m.get("platform_message_id"),
                    m.get("observed"), m.get("api_content"),
                    m.get("display_kind"), m.get("display_metadata"),
                )

            keys = [_body_key(m) for m in archived]
            assert len(keys) == len(set(keys)), (
                "archived trail holds full-identity twins"
            )
            # Stronger: no narrow twin ACROSS the archived trail either — the
            # replay shapes must be lossless so the DELETE sees the copies.
            archived_narrow = [
                (m["role"], m["content"], m.get("tool_call_id"), m.get("timestamp"))
                for m in archived
            ]
            assert len(archived_narrow) == len(set(archived_narrow)), (
                "archived trail holds lossy near-twins"
            )
