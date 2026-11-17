"""Cells 1-L2 and 2-L2 — bridge inject / send_message_tool => intake => turn.

Contract: CONTRACT.md law 2 — ``ROW => INTAKE => TURN``. The assertion is
the TURN (the ``inbound message:`` handler runs and the turn spawns), not
the row. These tests route through the REAL intake handler
(``gateway.run._handle_message_with_agent``) wired to the harness's fake
adapter; only the model loop is faked at the agent-run boundary.

- Cell 1-L2: bridge inject (``mcp_bridge._handle_inject``) -> adapter ->
  real intake -> turn spawns. The ``2026-11-16-tell-wake-delivered-no-turn``
  class: a row landing without a turn must be impossible to mistake for
  success.
- Cell 2-L2: ``send_message_tool`` bare/internal=True path across the REAL
  Unix-socket bridge -> ``_handle_inject`` -> real intake -> turn.

These build ON the 5 bridge outcome tests in tests/gateway/test_mcp_bridge.py
(which assert the status report with a no-op adapter) — they do not repeat
them, they extend them across the intake seam to the turn.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import cast


from gateway.config import Platform
from gateway.mcp_bridge import _handle_inject
from tests.dispatch.wake_harness import (
    _Writer,
    inject_payload,
    patch_send_tool_cli_env,
    start_bridge_on,
)


# ---------------------------------------------------------------------------
# Cell 1-L2: bridge inject -> REAL intake -> turn
# ---------------------------------------------------------------------------


def test_bridge_inject_spawns_turn(wake_env):
    """A tell-wake inject must result in an actual turn, not just a row.

    Regression class: 2026-11-16-tell-wake-delivered-no-turn — the row
    landed and the bridge acked, but no turn spawned. Here the real intake
    runs end to end; the assert is the turn (the agent-run boundary invoked
    with the injected message in the target session).
    """
    runner, adapter, recorder = wake_env
    writer = _Writer()

    async def scenario():
        await _handle_inject(
            runner,
            inject_payload(),
            cast(asyncio.StreamWriter, writer),
        )
        await asyncio.wait_for(recorder.done.wait(), timeout=15)

    asyncio.run(scenario())

    assert adapter.errors == [], [str(e) for e in adapter.errors]
    # The bridge reports the true outcome...
    assert writer.payload is not None
    assert writer.payload["status"] == "spawned", writer.payload
    # ...the row landed with the sender's identity...
    assert len(adapter.handled_events) == 1
    event = adapter.handled_events[0]
    assert event.internal is False
    assert event.source.user_id == "evan"
    assert event.source.chat_id == "8900123006"
    # ...and the TURN spawned on the real intake handler.
    assert len(recorder.turns) == 1
    turn = recorder.turns[0]
    assert turn["message"] == "Incoming message from zephyr follows: --- test"
    assert turn["session_id"]
    assert turn["source"].chat_id == "8900123006"
    assert turn["session_key"].startswith("agent:main:telegram:dm:")
    # Law 2: the row landed AND the turn ran on that row's session — the
    # intake resolved the very session the event targeted and spawned the
    # turn there.
    entries = list(runner.session_store._entries.values())
    assert entries, "the wake row must create/resolve a session"
    assert any(e.session_id == turn["session_id"] for e in entries), (
        "the turn must run on the session the row landed in"
    )


def test_bridge_inject_queued_behind_busy_session_spawns_no_turn(wake_env):
    """A wake into a busy session reports ``queued`` and spawns NO turn.

    ``queued`` must never be mistaken for a spawned turn: the wake is parked
    behind the active session and will run when it finishes. The test pins
    the truthfulness of the outcome across the real intake: busy => queued,
    no turn now.
    """
    import gateway.session as session_mod

    runner, adapter, recorder = wake_env

    # Pre-install the session guard for the exact key the inject will compute.
    source = _session_source()
    key = session_mod.build_session_key(
        source, group_sessions_per_user=True, thread_sessions_per_user=False
    )
    adapter._active_sessions[key] = asyncio.Event()

    writer = _Writer()

    async def scenario():
        await _handle_inject(
            runner,
            inject_payload(),
            cast(asyncio.StreamWriter, writer),
        )
        # Give any (wrong) turn a chance to appear; it must not.
        await asyncio.sleep(0.5)

    asyncio.run(scenario())

    assert writer.payload is not None
    assert writer.payload["status"] == "queued", writer.payload
    assert recorder.turns == [], "a queued wake must not spawn a turn yet"


def _session_source():
    from gateway.session import SessionSource

    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="8900123006",
        chat_type="dm",
        user_id="8900123006",
        user_name="CLI User",
    )


# ---------------------------------------------------------------------------
# Cell 2-L2: send_message_tool bare/internal=True across the real bridge
# ---------------------------------------------------------------------------


def test_send_message_tool_internal_wake_reaches_intake_and_turns(
    wake_env, monkeypatch, tmp_path
):
    """The bare internal=True tool path must land a real turn, end to end.

    `send_message_tool` with internal=True is the `hermes send -t` wake
    leg (Evan-locked 2026-10-18 semantics). When the gateway is not in this
    process it crosses the REAL bridge socket into the runner, whose adapter
    drives the real intake. Assert the tool reports spawned AND the turn
    spawned.
    """
    smt = patch_send_tool_cli_env(monkeypatch)
    runner, adapter, recorder = wake_env

    async def scenario():
        server, sock = await start_bridge_on(runner, monkeypatch, tmp_path)
        try:
            monkeypatch.setattr(
                smt, "bridge_socket_path", lambda *_a, **_k: str(sock)
            )
            # send_message_tool runs its own asyncio.run (patched
            # _run_async) — it must run off this loop, in a worker thread,
            # while the bridge server serves the socket on the main loop.
            result = await asyncio.to_thread(
                smt.send_message_tool,
                {
                    "action": "send",
                    "target": "telegram:8900123006",
                    "message": "wake me up",
                    "internal": True,
                },
            )
            await asyncio.wait_for(recorder.done.wait(), timeout=15)
            return json.loads(result)
        finally:
            server.close()
            await server.wait_closed()

    result = asyncio.run(scenario())

    assert adapter.errors == [], [str(e) for e in adapter.errors]
    assert result["success"] is True, result
    assert result["status"] == "spawned", result
    assert len(recorder.turns) == 1
    assert recorder.turns[0]["message"] == "wake me up"


def test_send_message_tool_internal_wake_dropped_fails_loud(
    wake_env, monkeypatch, tmp_path
):
    """A dropped wake must be an error, never a false success — across the
    real socket and real intake (the sender-side fail-loud half of law 1).
    """
    smt = patch_send_tool_cli_env(monkeypatch)
    runner, adapter, recorder = wake_env

    class SwallowAdapter:
        config = SimpleNamespace(extra={})
        _active_sessions = {}

        async def handle_message(self, event):
            return None  # silently swallows -> dropped

    # Replace the harness adapter with a swallowing one (stays the same site).
    runner.adapters[Platform.TELEGRAM] = SwallowAdapter()

    async def scenario():
        server, sock = await start_bridge_on(runner, monkeypatch, tmp_path)
        try:
            monkeypatch.setattr(
                smt, "bridge_socket_path", lambda *_a, **_k: str(sock)
            )
            result = await asyncio.to_thread(
                smt.send_message_tool,
                {
                    "action": "send",
                    "target": "telegram:8900123006",
                    "message": "do not vanish",
                    "internal": True,
                },
            )
            return json.loads(result)
        finally:
            server.close()
            await server.wait_closed()

    result = asyncio.run(scenario())

    assert "error" in result, result
    assert "dropped" in result["error"], result
    assert recorder.turns == [], "a dropped wake must never spawn a turn"
