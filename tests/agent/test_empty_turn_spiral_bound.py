"""Empty-turn spiral bound + poison-row replay drop (t_c9ece94d).

Covers the three pieces landed for bug 2026-10-01-empty-turn-heal-does-not-persist:
1. drop_no_payload_non_final — turn-entry/restore-boundary poison-row drop
   (drop + merge-adjacent-users, final-row exempt, pure on caller input);
2. _msg_has_payload classification — poison rows vs designed carriers;
3. _empty_turn_spiral_limit — session-scope bound sourcing.

The per-call sanitizer's in-memory-only contract is asserted too (the repair
must NOT mutate the stored list) to lock the design boundary the fix relies on.
"""
import os

import pytest


def poison_row():
    return {"role": "assistant", "content": "", "finish_reason": "length"}


def carrier_row(tc_id="c1"):
    return {
        "role": "assistant",
        "content": "",
        "finish_reason": "tool_calls",
        "tool_calls": [
            {"id": tc_id, "type": "function",
             "function": {"name": "f", "arguments": "{}"}}
        ],
    }


# ── 1. drop_no_payload_non_final ────────────────────────────────────────

def test_drop_removes_poison_rows_but_keeps_final_row():
    from agent.agent_runtime_helpers import drop_no_payload_non_final
    seq = [
        {"role": "user", "content": "q"},
        poison_row(),
        {"role": "user", "content": "next"},
        poison_row(),  # final row: legal empty, must be kept
    ]
    out = drop_no_payload_non_final(seq)
    # 4 in − 1 dropped (idx-1 poison) − 1 merged (the two user rows join) = 2:
    # [user("q\n\nnext"), poison(final, exempt)]
    assert len(out) == 2
    assert out[0]["role"] == "user"
    assert out[0]["content"] == "q\n\nnext"
    assert out[1] == seq[3], "final empty row must be exempt"


def test_drop_merges_adjacent_users_left_behind():
    from agent.agent_runtime_helpers import drop_no_payload_non_final
    seq = [
        {"role": "user", "content": "q1"},
        poison_row(),
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a", "finish_reason": "stop"},
    ]
    out = drop_no_payload_non_final(seq)
    roles = [m["role"] for m in out]
    assert roles == ["user", "assistant"]
    assert out[0]["content"] == "q1\n\nq2"


def test_drop_keeps_designed_carriers():
    from agent.agent_runtime_helpers import drop_no_payload_non_final
    seq = [
        {"role": "user", "content": "q"},
        carrier_row(),
        {"role": "tool", "tool_call_id": "c1", "content": "r"},
        {"role": "assistant", "content": "done", "finish_reason": "stop"},
    ]
    out = drop_no_payload_non_final(seq)
    assert len(out) == 4, "carrier row (designed-empty) must never be dropped"


def test_drop_keeps_rows_with_reasoning_payload():
    from agent.agent_runtime_helpers import drop_no_payload_non_final
    reasoning_row = {"role": "assistant", "content": "", "finish_reason": "length",
                     "reasoning_content": "thinking"}
    seq = [
        {"role": "user", "content": "q"},
        reasoning_row,
        {"role": "user", "content": "next"},
        {"role": "assistant", "content": "a", "finish_reason": "stop"},
    ]
    out = drop_no_payload_non_final(seq)
    assert len(out) == 4, "reasoning-carrying row is payload, not poison"


def test_drop_pure_on_caller_input():
    from agent.agent_runtime_helpers import drop_no_payload_non_final
    import copy
    seq = [{"role": "user", "content": "a"}, poison_row(),
           {"role": "user", "content": "b"}]
    orig = copy.deepcopy(seq)
    drop_no_payload_non_final(seq)
    assert seq == orig


def test_drop_noop_when_clean():
    from agent.agent_runtime_helpers import drop_no_payload_non_final
    seq = [{"role": "user", "content": "a"},
           {"role": "assistant", "content": "b", "finish_reason": "stop"}]
    assert drop_no_payload_non_final(seq) is seq, "clean list returns unchanged"


# ── 2. the repair stays in-memory-only (design boundary) ───────────────

def test_sanitize_repair_does_not_mutate_stored_list():
    from agent.agent_runtime_helpers import sanitize_api_messages
    import copy
    stored = [{"role": "user", "content": "q"}, poison_row(),
              {"role": "assistant", "content": "a", "finish_reason": "stop"}]
    orig = copy.deepcopy(stored)
    sanitize_api_messages(stored)
    assert stored == orig, "the wire repair must never touch the stored transcript"


# ── 3. session-scope bound sourcing ─────────────────────────────────────

def test_spiral_limit_config_sources(monkeypatch):
    """agent.empty_turn_spiral_limit in config.yaml is the only source (no env, per AGENTS.md)."""
    from agent.conversation_loop import _empty_turn_spiral_limit

    import hermes_cli.config as cfgmod

    def _cfg(value):
        return {"agent": {"empty_turn_spiral_limit": value}}

    monkeypatch.setattr(cfgmod, "load_config_readonly", lambda: _cfg(3))
    assert _empty_turn_spiral_limit() == 3

    monkeypatch.setattr(cfgmod, "load_config_readonly", lambda: _cfg(0))
    assert _empty_turn_spiral_limit() == 10  # non-positive: reject, keep default

    monkeypatch.setattr(cfgmod, "load_config_readonly", lambda: _cfg("not-a-number"))
    assert _empty_turn_spiral_limit() == 10  # garbage: reject, keep default

    monkeypatch.setattr(cfgmod, "load_config_readonly", lambda: {})
    assert _empty_turn_spiral_limit() == 10  # key absent: default

    # Env must be inert even if someone sets it (reject-on-sight).
    monkeypatch.setenv("HERMES_EMPTY_TURN_SPIRAL_LIMIT", "3")
    monkeypatch.setattr(cfgmod, "load_config_readonly", lambda: {})
    assert _empty_turn_spiral_limit() == 10


def test_poison_increment_predicate_matches_repair_classifier():
    """The bound counts exactly what the repair heals — one predicate, two consumers.

    Guards against detector drift: if _msg_has_payload ever diverges from what
    repair_empty_non_final_messages heals, the bound undercounts and the spiral
    reopens. This test fails before such a divergence ships.
    """
    from agent.agent_runtime_helpers import (
        _msg_has_payload,
        repair_empty_non_final_messages,
    )
    # probe rows: (fixture, has_payload_expectation). Semantics from source:
    # - the heal (:3266) covers ONLY assistant/user roles — a tool row is
    #   validated by its own orphan/pairing pass and is NOT healed, so an
    #   empty tool result is not poison for this bound either (the drop also
    #   filters on role=="assistant" only);
    # - carriers (tool_calls / reasoning / codex items) are payload.
    probes = [
        (poison_row(), False),            # assistant, empty → heals & counts
        (carrier_row(), True),            # designed carrier → neither
        ({"role": "assistant", "content": "hi", "finish_reason": "stop"}, True),
        ({"role": "assistant", "content": "", "reasoning": "x"}, True),
        # empty assistant with only a codex carrier — designed-empty, payload
        ({"role": "assistant", "content": "", "codex_message_items": [{"a": 1}]}, True),
    ]
    for probe, has_payload in probes:
        wire = [{"role": "user", "content": "q"}, probe,
                {"role": "assistant", "content": "end", "finish_reason": "stop"}]
        out = repair_empty_non_final_messages(wire)
        healed = out[1].get("content") == "[response interrupted]"
        assert healed == (not has_payload), f"repair/predicate drift at {probe}"
        assert _msg_has_payload(probe) == has_payload
    # rows the repair does NOT touch: tool results are a separate concern
    # (:3259-3260 comment) — assert the repair leaves them and they are not
    # poison for either consumer.
    tool_row = {"role": "tool", "tool_call_id": "t", "content": ""}
    wire = [{"role": "user", "content": "q"}, tool_row,
            {"role": "assistant", "content": "end", "finish_reason": "stop"}]
    out = repair_empty_non_final_messages(wire)
    assert all(m.get("content") != "[response interrupted]" for m in out), \
        "tool rows must not be healed by this pass"


# ── 4. recovery-keyed counter reset (t_d9fd3ab9 item 2) ─────────────────

def _real_agent():
    """Real AIAgent — finalize_turn touches dozens of attrs; stubs whack-a-mole."""
    import os
    from unittest.mock import patch as _mock_patch

    with _mock_patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            session_db=None,
            session_id="spiral-reset-test",
            skip_context_files=True,
            skip_memory=True,
        )
    return agent


def _finalize(agent, final_response, interrupted, exit_reason):
    from agent.turn_finalizer import finalize_turn

    return finalize_turn(
        agent,
        final_response=final_response,
        api_call_count=3,
        interrupted=interrupted,
        failed=False,
        messages=[{"role": "user", "content": "q"}],
        conversation_history=[],
        effective_task_id=None,
        turn_id="t",
        user_message="q",
        original_user_message="q",
        _should_review_memory=False,
        _turn_exit_reason=exit_reason,
    )


def test_recovery_resets_counter():
    """A turn that produced a REAL final response resets the poison counter to 0."""
    from agent.turn_finalizer import finalize_turn

    a = _real_agent()
    a._poison_row_appends_session = 9  # one below the default bound
    result = _finalize(a, "Real answer after a rough patch.", False,
                       "text_response(finish_reason=stop)")
    assert a._poison_row_appends_session == 0, "a recovering turn must reset the counter"


def test_sick_turn_keeps_accumulating():
    """A zero-length final response does NOT reset — sick turns keep the count.

    Under the pre-capture design the explainer's substituted warning is
    unreadable by the reset either way; this test pins that the captured
    EMPTY value (not the substituted text) drives the gate.
    """
    from agent.turn_finalizer import finalize_turn

    a = _real_agent()
    a._poison_row_appends_session = 9
    _finalize(a, "", False, "empty_response_exhausted")
    assert a._poison_row_appends_session == 9, "a degenerate turn must not flush the counter"


def test_interrupted_turn_does_not_reset():
    """Interrupted turns do not reset: the interrupt is not proof of recovery."""
    from agent.turn_finalizer import finalize_turn

    a = _real_agent()
    a._poison_row_appends_session = 5
    _finalize(a, "partial text", True, "interrupted_during_api_call")
    assert a._poison_row_appends_session == 5, "an interrupted turn must not flush the counter"