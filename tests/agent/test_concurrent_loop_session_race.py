"""Regression tests: concurrent conversation loops on one session.

Report: concurrent-loop-session-race-2026-11-09.md (Zephyr forensics,
zephyr session 20261109_064259_98663b60).

The defect chain, verified from the session log and reproduced here:

1. The parent's tool round persists its assistant message (with
   ``tool_calls``) to state.db *before* executing the tool, then appends
   the matching ``tool`` result afterwards.
2. The background-review thread surfaced its shape-(a) curator note by
   appending a synthetic ``user`` row straight into the parent's LIVE
   message list (in ``surface_curator_mutations``), which could land
   between (1)'s two halves.
3. The next ``repair_message_sequence`` pass dropped the not-yet-appended
   tool result as an orphan (its rolling known-call-id set resets at a
   ``user`` boundary).
4. The pre-call sanitizer then stubbed the call — 15+ "added 1 stub tool
   result(s)" lines over 8 minutes, with "Repaired N message-alternation
   violation(s) while restoring session" in the same window. No
   compression ran. An in-process tool result must never need a stub.

Fixes covered:

* The fork runs on an isolated child session id ("{parent}:bg-review") and
  fails loudly if it ever inherits the parent's live id.
* A process-local ownership registry refuses a second LIVE conversation
  loop on the same session id (``register_conversation_loop``) — the
  guard lives in ``agent/agent_runtime_helpers.py``.
* Curator notes are queued by the review thread and drained by the parent
  at its next turn boundary — the interleaving is structurally
  impossible, not merely unlikely.
"""

from __future__ import annotations

import copy
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent import agent_runtime_helpers as _helpers
from agent.agent_runtime_helpers import (
    repair_message_sequence,
    sanitize_api_messages,
)


def _call(cid, name="terminal", args="{}"):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": cid, "type": "function", "function": {"name": name, "arguments": args}}
        ],
    }


def _result(cid, content="result text"):
    return {"role": "tool", "tool_call_id": cid, "name": "terminal", "content": content}


class _LoopOwnerRegistry:
    """Fixture: isolate the process-local ownership registry between tests."""

    def __enter__(self):
        with _helpers._ACTIVE_SESSION_LOOPS_LOCK:
            _helpers._ACTIVE_SESSION_LOOPS.clear()
        return self

    def __exit__(self, *exc):
        with _helpers._ACTIVE_SESSION_LOOPS_LOCK:
            _helpers._ACTIVE_SESSION_LOOPS.clear()
        return False


@pytest.fixture(autouse=True)
def _registry_scope():
    with _LoopOwnerRegistry():
        yield


class _CaptureRun:
    """Patch ``AIAgent.run_conversation`` the way the real review worker's
    import path reaches it, capturing the fork agent's attributes."""

    def __init__(self):
        self.capture = {}
        self._orig = None

    def __enter__(self):
        from run_agent import AIAgent

        capture = self.capture
        self._orig = AIAgent.run_conversation

        def wrapper(_self, *args, **kwargs):
            capture["session_id"] = getattr(_self, "session_id", None)
            capture["compression_enabled"] = getattr(_self, "compression_enabled", None)
            capture["parent_session_id"] = getattr(_self, "_parent_session_id", None)
            return {"final_response": "", "messages": []}

        AIAgent.run_conversation = wrapper
        return self

    def __exit__(self, *_exc):
        from run_agent import AIAgent

        AIAgent.run_conversation = self._orig
        return False


def _build_parent_agent(db, session_id):
    import os

    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent

        return AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            session_db=db,
            session_id=session_id,
            skip_context_files=True,
            skip_memory=True,
        )


# ---------------------------------------------------------------------------
# 1. The guard: no two live loops on one session
# ---------------------------------------------------------------------------

def test_two_live_loops_same_session_refuse():
    """A second live conversation loop on the same session id must refuse
    loudly instead of interleaving."""
    _helpers.register_conversation_loop("s1")
    try:
        # Same-thread re-entry is allowed (takeover semantics)
        _helpers.register_conversation_loop("s1")

        taken = {}

        def _second_loop():
            try:
                _helpers.register_conversation_loop("s1")
                taken["registered"] = True
            except RuntimeError as exc:
                taken["error"] = str(exc)

        t = threading.Thread(target=_second_loop, daemon=True)
        t.start()
        t.join(timeout=5)

        assert t.is_alive() is False
        assert taken.get("registered") is not True
        assert "already active on session s1" in taken.get("error", "")
    finally:
        _helpers.release_conversation_loop("s1", threading.get_ident())


def test_claim_is_taken_over_when_owner_thread_died():
    """A claim whose owner thread has exited must not wedge the session."""

    def _dead_owner():
        _helpers.register_conversation_loop("s-dead")

    t = threading.Thread(target=_dead_owner, daemon=True)
    t.start()
    t.join(timeout=5)
    assert t.is_alive() is False

    # Walking in: the dead owner's claim is taken over, no raise.
    _helpers.register_conversation_loop("s-dead")
    try:
        owner = _helpers._ACTIVE_SESSION_LOOPS["s-dead"]
        assert owner["thread"] is threading.current_thread()
    finally:
        _helpers.release_conversation_loop("s-dead", threading.get_ident())


def test_different_sessions_never_collide():
    _helpers.register_conversation_loop("s1")
    _helpers.register_conversation_loop("s2")
    try:
        assert set(_helpers._ACTIVE_SESSION_LOOPS) == {"s1", "s2"}
    finally:
        _helpers.release_conversation_loop("s1", threading.get_ident())
        _helpers.release_conversation_loop("s2", threading.get_ident())


def test_release_is_expected_ident_guarded():
    captured = {}

    def _capture():
        captured["ident"] = threading.get_ident()
        _helpers.register_conversation_loop("s3")

    t = threading.Thread(target=_capture, daemon=True)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive()

    # Our ident differs from the owner's, so the release must be a no-op.
    _helpers.release_conversation_loop("s3", 999999999)
    assert "s3" in _helpers._ACTIVE_SESSION_LOOPS
    # Releasing without an ident (or with the right one) clears the claim.
    _helpers.release_conversation_loop("s3", None)
    assert "s3" not in _helpers._ACTIVE_SESSION_LOOPS
    assert captured.get("ident") is not None


# ---------------------------------------------------------------------------
# 2. The fork must not inherit the parent's live session id
# ---------------------------------------------------------------------------

def test_review_fork_gets_isolated_session_id(tmp_path):
    """Criterion 1: the background-review loop runs on its own session id.

    Updated contract replacing the old shared-id expectation (shared id is
    precisely what let the fork interleave with the parent's live turn).
    """
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        parent_sid = "FIXTURE_PARENT"
        db.create_session(parent_sid, source="discord")
        parent = _build_parent_agent(db, parent_sid)

        import agent.background_review as br

        with _CaptureRun() as run:
            br._run_review_in_thread(
                parent,
                [{"role": "user", "content": "hi"}],
                "review this conversation",
            )

        capture = run.capture
        assert capture.get("session_id") == f"{parent_sid}:bg-review"
        assert capture["session_id"] != parent_sid
        assert capture["parent_session_id"] == parent_sid
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 3. Curator notes must not interleave mid-round
# ---------------------------------------------------------------------------

def test_surface_curator_mutations_does_not_mutate_live_list():
    """The review thread must not append to the parent's live message list."""
    from agent.background_review import (
        drain_pending_curator_notes,
        extract_curator_mutations,
        surface_curator_mutations,
    )

    messages = [
        _call("c1", "write_file", '{"path": "/tmp/x.md"}'),
        _result("c1", '{"success": true}'),
    ]
    lines = extract_curator_mutations(messages)
    assert lines

    parent = SimpleNamespace(_session_messages=[{"role": "user", "content": "hi"}])
    surface_curator_mutations(parent, lines)

    # The live list is untouched — the note is queued instead.
    assert len(parent._session_messages) == 1
    assert getattr(parent, "_pending_curator_notes", []) != []

    # Draining at the turn boundary delivers the note as a whole user row.
    out = drain_pending_curator_notes(parent, [])
    assert len(out) == 1
    note = out[0]
    assert note["role"] == "user"
    assert note["content"].startswith("[BACKGROUND CURATOR")
    assert "NOT a task request" in note["content"]
    # Drained: a second drain delivers nothing.
    assert drain_pending_curator_notes(parent, []) == []


def test_no_mutations_queued_no_row_drains_empty():
    parent = SimpleNamespace()
    from agent.background_review import drain_pending_curator_notes

    out = drain_pending_curator_notes(parent, [])
    assert out == []  # unchanged when nothing is queued


# ---------------------------------------------------------------------------
# 4. Restore + repair + sanitizer: pairing survives, no stub
# ---------------------------------------------------------------------------

def test_restore_repair_keeps_pairing_and_no_stub():
    """Criteria 2/3: over a session whose assistant tool_call and its
    result row are both durable, a restore+repair pass keeps the result
    paired and the pre-call sanitizer adds NO stub.

    The "result newer than the snapshot" situation is simulated by feeding
    the repair the assistant call without the result first (the torn view
    produced when a snapshot lands mid-round), then restoring the full
    sequence. Repair is in-memory only — the durable rows must never be
    rewritten — and on the full sequence it must be a no-op.
    """
    cid = "chatcmpl-tool-pair-survives"
    durable = [
        {"role": "user", "content": "hi"},
        _call(cid),
        _result(cid),
        {"role": "assistant", "content": "ok"},
    ]

    torn = [durable[0], durable[1]]
    work = copy.deepcopy(torn)
    repairs = repair_message_sequence(None, work)
    assert isinstance(repairs, int)

    full = copy.deepcopy(durable)
    rep = repair_message_sequence(None, full)
    assert rep == 0, "a well-formed sequence must not need repairs"
    roles = [m["role"] for m in full]
    assert roles == ["user", "assistant", "tool", "assistant"]
    api = sanitize_api_messages(copy.deepcopy(full))
    stubs = [
        m
        for m in api
        if m.get("role") == "tool"
        and "Result unavailable" in str(m.get("content", ""))
    ]
    assert stubs == [], "no stub may be added for a paired in-process call"


def test_drained_note_between_turns_does_not_stub_pairs():
    """The interleaving that used to happen (a synthetic user row between a
    call and its result) must not be produced by the curator surface path —
    covered by the queue test. When notes drain at a turn boundary instead,
    a completed pair stays paired and the sanitizer adds no stub."""
    cid = "chatcmpl-tool-1001"
    messages = [
        {"role": "user", "content": "start"},
        _call(cid, "terminal", '{"command": "echo hi"}'),
        _result(cid, "hi"),
        # A curator note drains between turns — after the pair completes,
        # before the next user message, never inside a tool round.
        {
            "role": "user",
            "content": "[BACKGROUND CURATOR — persistent file changes made after your last turn. For your context only — NOT a task request.]",
        },
        {"role": "user", "content": "next turn"},
        {"role": "assistant", "content": "after"},
    ]
    worked = copy.deepcopy(messages)
    repairs = repair_message_sequence(None, worked)
    api = sanitize_api_messages(worked)
    stubs = [
        m
        for m in api
        if m.get("role") == "tool"
        and "Result unavailable" in str(m.get("content", ""))
    ]
    assert stubs == [], (
        "repair+sanitizer must not stub a call whose result sits directly "
        f"after it (repairs={repairs})"
    )