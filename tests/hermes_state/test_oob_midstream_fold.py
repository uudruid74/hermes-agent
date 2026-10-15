"""Replay fixture for the OOB-mid-stream fold (P1 interrupt-swallow, t_0c64b0d0).

Incident (neo session 20261013_233056_4afae3f9, 2026-10-15 03:45:52–03:46:00):
an out-of-band Telegram message ("ornith's ready when you are") aborted an
in-flight stream; the turn degenerated (bare ``cd`` tool call, then a 53-char
final that echoed the interrupt checkpoint) and three defects fired together:

1. the delivered 53-char turn-final NEVER reached the durable store — the
   writer's unit-dedup silently merged it away because its clock was the
   fabricated flush stamp and its text repeated earlier content
   (delivered-but-unpersisted, response-truth violation);
2. the rewrite flush at interrupt close stamped 179 empty tool-call carrier
   rows with ONE identical fabricated timestamp (the ``''`` content-key
   collapse in ``_ts_harvest``);
3. the interrupting message was folded into the dying turn and never
   serviced.

Assertions (the task's fixture contract):
  * no identical-ts assistant ghosts on the OOB-mid-stream fold replay
  * the final text row is persisted even when its content repeats
  * ``repair_message_sequence`` never INSERTS rows ("repair must not emit
    stubs" — verified: it only merges/drops; this locks the invariant)
  * a folded OOB message whose turn ends on interruption machinery is handed
    back for requeue as its own turn.
"""

import time
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any

from agent.agent_runtime_helpers import repair_message_sequence
from agent.message_sanitization import (
    INTERRUPT_CHECKPOINT_SENTINEL,
    INTERRUPT_CLOSE_PLACEHOLDER,
    is_degenerate_interrupt_response,
)
from hermes_state import SessionDB

CHECKPOINT = INTERRUPT_CHECKPOINT_SENTINEL


def _db(tmp):
    db = SessionDB(db_path=Path(tmp) / "t.db")
    db.create_session("s1", "cli", model="test/model")
    return db


def _carrier(call_id: str, ts: float | None = None) -> dict:
    msg: dict[str, Any] = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": call_id, "function": {"name": "terminal", "arguments": "{}"}}
        ],
    }
    if ts is not None:
        msg["timestamp"] = ts
    return msg


def _result(call_id: str, ts: float | None = None) -> dict:
    msg: dict[str, Any] = {
        "role": "tool",
        "tool_call_id": call_id,
        "tool_name": "terminal",
        "content": '{"output": "", "exit_code": 0}',
    }
    if ts is not None:
        msg["timestamp"] = ts
    return msg


class TestNoIdenticalTsAssistantGhosts:
    def test_oob_fold_rewrite_carriers_keep_distinct_clocks(self):
        """The '' content-key collapse: one rewrite batch of empty carriers
        must not inherit a single shared clock (the 179-row ghost batch)."""
        with TemporaryDirectory() as tmp:
            db = _db(tmp)
            window = [
                {"role": "user", "content": "Remove the wording.", "timestamp": 100.0},
                _carrier("call_a", 101.0),
                _result("call_a", 102.0),
                _carrier("call_b", 103.0),
                _result("call_b", 104.0),
                _carrier("call_c", 105.0),
                _result("call_c", 106.0),
            ]
            db.append_messages_batch("s1", list(window))
            # Compression-style rewrite copies: fresh dicts with NO timestamp
            # keys — exactly what reaches _insert_message_rows's harvest path.
            rewritten = [
                {k: v for k, v in m.items() if k != "timestamp"} for m in window
            ]
            db.archive_and_compact("s1", rewritten)
            carriers = [m for m in db.get_messages("s1") if m.get("tool_calls")]
            assert len(carriers) == 3
            stamps = [round(m["timestamp"], 6) for m in carriers]
            assert len(set(stamps)) == 3, (
                "empty tool-call carriers collapsed onto shared clock(s): "
                f"{stamps}"
            )
            # Each carrier resolves through its call identity to a real clock
            # (a result row outranks its carrier for the same call id — the
            # harvest's documented rule), never the rewrite-time stamp.
            assert set(stamps) == {102.0, 104.0, 106.0}
            # ...and the turn-final of the degenerate follow-up call (the
            # model echoed the checkpoint text) must still reach the durable
            # store — the incident's delivered-but-unpersisted row.
            final_msg = {"role": "assistant", "content": CHECKPOINT,
                         "timestamp": time.time()}
            assert db.append_messages_batch("s1", [final_msg]) == 1
            persisted = [
                m for m in db.get_messages("s1")
                if m.get("content") == CHECKPOINT
            ]
            assert len(persisted) == 1, "turn-final text row must be persisted"

    def test_carrier_without_result_keeps_its_own_clock(self):
        """A rewritten carrier with no result row recovers its own clock."""
        with TemporaryDirectory() as tmp:
            db = _db(tmp)
            db.append_messages_batch("s1", [_carrier("call_x", 101.0)])
            db.archive_and_compact("s1", [_carrier("call_x")])
            rows = db.get_messages("s1")
            assert len(rows) == 1
            assert round(rows[0]["timestamp"], 6) == 101.0

    def test_empty_body_rows_never_share_one_fabricated_clock(self):
        """Multiple clockless carriers in one flush must not all land on the
        same now_ts: each keeps a distinct write clock (ghost-ts kill)."""
        with TemporaryDirectory() as tmp:
            db = _db(tmp)
            batch = [_carrier("call_1"), _carrier("call_2"), _carrier("call_3")]
            assert db.append_messages_batch("s1", batch) == 3
            stamps = [m["timestamp"] for m in db.get_messages("s1")]
            assert len(set(stamps)) == 3, f"identical-ts ghosts: {stamps}"


class TestResponseTruth:
    def test_build_assistant_message_stamps_creation_clock(self):
        """The response-truth root cause: assistant dicts must carry their
        real creation clock at build (t_0c64b0d0), like tool results already
        do (t_4b1721cb). A clockless dict is written with the fabricated flush
        stamp and can then be silently unit-deduped away whenever its content
        repeats earlier text — the delivered-but-unpersisted 53-char final."""
        from agent.chat_completion_helpers import build_assistant_message

        class _FakeMsg:
            content = "Done — removed the wording."
            tool_calls = None
            function_call = None
            reasoning_content = None
            model_extra = None
            reasoning_details = None

            def __getattr__(self, _name):
                return None

        class _FakeAgent:
            stream_delta_callback = None
            _stream_callback = None
            reasoning_callback = None
            verbose_logging = False

            def _extract_reasoning(self, _msg):
                return None

            def _strip_think_blocks(self, text):
                return text

            def _needs_thinking_reasoning_pad(self):
                return False

        before = time.time()
        msg = build_assistant_message(_FakeAgent(), _FakeMsg(), "stop")
        after = time.time()
        assert isinstance(msg.get("timestamp"), float), (
            "assistant messages must be stamped at build time"
        )
        assert before <= msg["timestamp"] <= after

    def test_repeated_turn_final_is_never_silently_dropped(self):
        """A new turn-final whose text repeats earlier content keeps every
        occurrence when it carries its own real clock; only clockless
        re-persistence copies are merged (the class-2 contract)."""
        with TemporaryDirectory() as tmp:
            db = _db(tmp)
            assert db.append_messages_batch(
                "s1", [{"role": "assistant", "content": CHECKPOINT, "timestamp": 100.0}]
            ) == 1
            # The incident's turn-final: the model echoed the checkpoint text.
            # With its build-time clock it must still reach the durable store.
            assert db.append_messages_batch(
                "s1", [{"role": "assistant", "content": CHECKPOINT, "timestamp": 200.0}]
            ) == 1
            # A clockless copy is the class-2 re-persistence shape — merged.
            assert db.append_messages_batch(
                "s1", [{"role": "assistant", "content": CHECKPOINT}]
            ) == 0
            assert len(db.get_messages("s1", include_inactive=True)) == 2


class TestRepairNeverInserts:
    def test_repair_message_sequence_never_inserts_rows(self):
        """The alternation repair must not fabricate assistant stubs — it may
        only merge or drop. Verified on the incident's row shapes."""
        msgs = [
            {"role": "user", "content": "Remove the wording.", "timestamp": 100.0},
            _carrier("call_a", 101.0),
            _result("call_a", 102.0),
            {"role": "user", "content": "ornith's ready when you are", "timestamp": 103.0},
            _carrier("call_b", 104.0),
            _result("call_b", 105.0),
            {"role": "user", "content": "what happened?", "timestamp": 106.0},
            {"role": "assistant", "content": CHECKPOINT, "timestamp": 107.0},
            {"role": "user", "content": "and now?", "timestamp": 108.0},
            {"role": "user", "content": "hello?", "timestamp": 109.0},
        ]
        originals = list(msgs)
        orig_ids = {id(m) for m in originals}
        repair_message_sequence(None, msgs)
        assert len(msgs) <= len(originals), "repair grew the message list"
        assert all(id(m) in orig_ids for m in msgs), (
            "repair inserted fabricated rows into the sequence"
        )


# --- OOB requeue-on-unserviced -------------------------------------------


class _FinalizeAgent:
    """Minimal finalize_turn agent (mirrors the harness in
    tests/agent/test_turn_finalizer_final_response_persistence.py)."""

    def __init__(self, folded=None):
        self.max_iterations = 90
        self.iteration_budget = SimpleNamespace(remaining=10, used=1, max_total=90)
        self.quiet_mode = True
        self.model = "test-model"
        self.provider = "test-provider"
        self.base_url = ""
        self.session_id = "sess-test"
        self.context_compressor = SimpleNamespace(last_prompt_tokens=0)
        self.session_input_tokens = 0
        self.session_output_tokens = 0
        self.session_cache_read_tokens = 0
        self.session_cache_write_tokens = 0
        self.session_reasoning_tokens = 0
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_total_tokens = 0
        self.session_estimated_cost_usd = 0
        self.session_cost_status = "unknown"
        self.session_cost_source = "test"
        self._tool_guardrail_halt_decision = None
        self._interrupt_message = None
        self._response_was_previewed = False
        self._skill_nudge_interval = 0
        self._iters_since_skill = 0
        self.valid_tool_names = []
        self._persist_user_message_idx = None
        self._persist_user_message_override = None
        self._persist_user_message_timestamp = None
        self._turn_folded_redirect_texts = list(folded or [])

    def _handle_max_iterations(self, messages, api_call_count):
        raise AssertionError("not expected")

    def _emit_status(self, *_args, **_kwargs):
        pass

    def _safe_print(self, *_args, **_kwargs):
        pass

    def _save_trajectory(self, *_args, **_kwargs):
        pass

    def _cleanup_task_resources(self, *_args, **_kwargs):
        pass

    def _drop_trailing_empty_response_scaffolding(self, messages):
        pass

    def _persist_session(self, messages, conversation_history):
        pass

    def _apply_persist_user_message_override(self, messages):
        pass

    def _file_mutation_verifier_enabled(self):
        return False

    def _turn_completion_explainer_enabled(self):
        return False

    def _drain_pending_steer(self):
        return None

    def clear_interrupt(self):
        pass

    def _sync_external_memory_for_turn(self, **_kwargs):
        pass


def _finalize(agent, messages, final_response):
    from agent.turn_finalizer import finalize_turn

    return finalize_turn(
        agent,
        final_response=final_response,
        api_call_count=2,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="ornith's ready when you are",
        original_user_message="ornith's ready when you are",
        _should_review_memory=False,
        _turn_exit_reason="text_response(finish_reason=stop)",
    )


class TestOobRequeueOnUnserviced:
    def test_degenerate_classifier(self):
        assert is_degenerate_interrupt_response(None)
        assert is_degenerate_interrupt_response("")
        assert is_degenerate_interrupt_response(CHECKPOINT)
        assert is_degenerate_interrupt_response(INTERRUPT_CLOSE_PLACEHOLDER)
        assert is_degenerate_interrupt_response(
            "Operation interrupted: waiting for model response (2.0s elapsed)."
        )
        assert not is_degenerate_interrupt_response("Done — proceeding now.")
        assert not is_degenerate_interrupt_response(
            "Ornith's ready — I'll fire the plan."
        )

    def test_unserviced_oob_text_is_handed_back(self, monkeypatch):
        monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
        agent = _FinalizeAgent(folded=["ornith's ready when you are"])
        messages = [{"role": "user", "content": "ornith's ready when you are"}]
        result = _finalize(agent, messages, CHECKPOINT)
        assert result["unserviced_redirect_text"] == "ornith's ready when you are"
        # Drained once — a later turn must not requeue it again.
        assert agent._turn_folded_redirect_texts == []

    def test_serviced_oob_text_is_not_requeued(self, monkeypatch):
        monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
        agent = _FinalizeAgent(folded=["ornith's ready when you are"])
        messages = [{"role": "user", "content": "ornith's ready when you are"}]
        result = _finalize(agent, messages, "Ornith's ready — firing the plan now.")
        assert "unserviced_redirect_text" not in result

    def test_no_folded_text_no_handoff(self, monkeypatch):
        monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
        agent = _FinalizeAgent()
        result = _finalize(agent, [{"role": "user", "content": "hi"}], "")
        assert "unserviced_redirect_text" not in result

    def test_apply_active_turn_redirect_records_the_fold(self):
        from agent.conversation_loop import _apply_active_turn_redirect

        class _Agent:
            def __init__(self):
                self._current_streamed_assistant_text = ""
                self._stream_needs_break = False
                self._turn_folded_redirect_texts = []

            def _strip_think_blocks(self, text):
                return text

        agent = _Agent()
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": "Remove the wording."}
        ]
        _apply_active_turn_redirect(agent, messages, "ornith's ready when you are")
        assert agent._turn_folded_redirect_texts == ["ornith's ready when you are"]
        assert messages[-1]["role"] == "user"
        assert messages[-1]["content"] == "ornith's ready when you are"
        # Every appended dict carries a real creation clock (ghost-ts kill).
        for msg in messages[1:]:
            assert isinstance(msg.get("timestamp"), float)
            assert 0 < msg["timestamp"] <= time.time() + 1
