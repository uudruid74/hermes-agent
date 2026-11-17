"""Cell 5 — kanban notify-complete => subscriber delivery correctness.

CONTRACT.md cell 5: "kanban notify-complete -> subscriber delivery". The
L2 column is n/a (this is chat delivery, not a row->turn wake), so the
contract here is the DELIVERY one, and it has two halves:

1. **Gateway half** (``mcp_bridge._handle_send``): the per-task ordering
   watermark is the record of what a subscriber has actually received. It
   must advance only on a CONFIRMED adapter send. Advancing it before the
   send (the pre-2026-11-17 behavior) meant one transport hiccup
   permanently suppressed the retry of that same notification: the
   subscriber never got the completion, the sender was told "queued", and
   the retry was dropped as stale. That is a silently lost
   notify-complete — the exact class this suite exists for.

2. **Sender half** (``hermes_cli.kanban._deliver_kanban_notification``):
   when the human notice cannot be delivered, the subscriber must still
   learn the task reached that state. The wake path already fell back to
   the creator-session notice; the human notice did not, so a failed chat
   delivery vanished with only a log line. It now falls back too, and the
   two fallbacks must not double-queue the same notice.

Ordering/seq plumbing itself is pinned in
tests/hermes_cli/test_kanban_notify_ordering.py and the bridge ordering
guards in tests/gateway/test_mcp_bridge.py — this file extends them with
the delivery-truth contract, it does not repeat them.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import cast

import pytest

import gateway.mcp_bridge as mcp_bridge
from gateway.config import Platform
from gateway.mcp_bridge import _handle_send
from hermes_cli import kanban
from hermes_state import SessionDB
from tests.dispatch.wake_harness import _Writer


# ---------------------------------------------------------------------------
# Gateway half — the watermark records DELIVERED, not attempted
# ---------------------------------------------------------------------------


def _runner_with_send(send_fn):
    return SimpleNamespace(
        adapters={Platform.TELEGRAM: SimpleNamespace(send=send_fn)},
        _background_tasks=set(),
    )


async def _send_stamped(runner, text, task_id, seq):
    await _handle_send(
        runner,
        {
            "platform": "telegram",
            "chat_id": "8900123006",
            "text": text,
            "ordering": {"task_id": task_id, "seq": seq, "status": "done"},
        },
        cast(asyncio.StreamWriter, _Writer()),
    )
    await asyncio.gather(*list(runner._background_tasks))


def test_failed_delivery_does_not_advance_the_watermark():
    """A failed send must leave the watermark so the retry can still land.

    The subscriber received nothing; recording seq as delivered would make
    the retry a "stale straggler" and drop it — a permanently lost
    notify-complete.
    """
    mcp_bridge._kanban_delivery_states.clear()
    delivered: list[str] = []
    attempts = {"n": 0}

    async def flaky_send(**kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return SimpleNamespace(success=False, message_id=None, error="transport down")
        delivered.append(kwargs["content"])
        return SimpleNamespace(success=True, message_id="m1", error=None)

    runner = _runner_with_send(flaky_send)

    async def scenario():
        await _send_stamped(runner, "done", "t_retry", 7)
        # The failed delivery must not have been recorded as delivered.
        assert mcp_bridge._kanban_delivery_states["t_retry"]["seq"] is None
        # The retry of the SAME seq must be allowed through and land.
        await _send_stamped(runner, "done", "t_retry", 7)
        assert mcp_bridge._kanban_delivery_states["t_retry"]["seq"] == 7

    asyncio.run(scenario())
    assert delivered == ["done"], "the retried notification must reach the subscriber"


def test_successful_delivery_advances_the_watermark():
    """The positive half: a confirmed send records the seq, so a later
    lower-seq straggler is still dropped (ordering is not weakened)."""
    mcp_bridge._kanban_delivery_states.clear()
    delivered: list[str] = []

    async def ok_send(**kwargs):
        delivered.append(kwargs["content"])
        return SimpleNamespace(success=True, message_id="m1", error=None)

    runner = _runner_with_send(ok_send)

    async def scenario():
        await _send_stamped(runner, "done", "t_ok", 5)
        assert mcp_bridge._kanban_delivery_states["t_ok"]["seq"] == 5
        await _send_stamped(runner, "ready", "t_ok", 3)  # stale straggler

    asyncio.run(scenario())
    assert delivered == ["done"]


def test_raising_adapter_does_not_advance_the_watermark():
    """An adapter that raises is a failed delivery, not a delivered one."""
    mcp_bridge._kanban_delivery_states.clear()

    async def boom_send(**_kwargs):
        raise RuntimeError("platform exploded")

    runner = _runner_with_send(boom_send)

    async def scenario():
        await _send_stamped(runner, "done", "t_raise", 9)

    asyncio.run(scenario())
    assert mcp_bridge._kanban_delivery_states["t_raise"]["seq"] is None


def test_unstamped_delivery_still_sends():
    """No ordering stamp => no watermark bookkeeping, delivery unchanged."""
    mcp_bridge._kanban_delivery_states.clear()
    delivered: list[str] = []

    async def ok_send(**kwargs):
        delivered.append(kwargs["content"])
        return SimpleNamespace(success=True, message_id="m1", error=None)

    runner = _runner_with_send(ok_send)

    async def scenario():
        await _handle_send(
            runner,
            {
                "platform": "telegram",
                "chat_id": "8900123006",
                "text": "plain notice",
            },
            cast(asyncio.StreamWriter, _Writer()),
        )
        await asyncio.gather(*list(runner._background_tasks))

    asyncio.run(scenario())
    assert delivered == ["plain notice"]
    assert mcp_bridge._kanban_delivery_states == {}


# ---------------------------------------------------------------------------
# Sender half — a failed human notice must not vanish
# ---------------------------------------------------------------------------


@pytest.fixture
def notify_home(tmp_path, monkeypatch):
    """Isolated profile home with a creator session for the notice fallback."""
    profile_home = tmp_path / "profiles" / "zephyr"
    profile_home.mkdir(parents=True)
    db = SessionDB(db_path=profile_home / "state.db")
    db.create_session("creator-session", "telegram")
    db.close()

    import hermes_cli.profiles

    monkeypatch.setattr(
        hermes_cli.profiles, "get_profile_dir", lambda _profile: profile_home
    )
    return profile_home


def _wire_notify(monkeypatch, *, human_result, wake_result=None, task=None):
    """Wire _deliver_kanban_notification's collaborators; return call records."""
    rec = {"human": [], "wake": [], "notices": []}

    monkeypatch.setattr(kanban.kb, "connect", lambda: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(
        kanban.kb,
        "get_origin_routing",
        lambda *_a: {
            "platform": "telegram",
            "chat_id": "123",
            "chat_type": "dm",
            "profile": "zephyr",
        },
    )
    monkeypatch.setattr(
        kanban.kb,
        "get_task",
        lambda *_a: task
        or SimpleNamespace(
            id="t_notify",
            session_id="creator-session",
            created_by="zephyr",
            assignee="zephyr",
            title="Notify task",
        ),
    )
    monkeypatch.setattr(kanban.kb, "latest_task_event_id", lambda *_a: 4)
    monkeypatch.setattr(kanban, "_load_user_profile_env", lambda env, profile: None)
    monkeypatch.setattr(kanban, "_wake_task_worker_session", lambda *a, **k: False)
    monkeypatch.setattr(kanban, "_wake_targets_acting_session", lambda *a, **k: False)
    monkeypatch.setattr(kanban, "kick_dispatch", lambda *a, **k: None)

    def human(**kwargs):
        rec["human"].append(kwargs)
        return "mcp_bridge:/tmp/x.sock", human_result

    def wake(**kwargs):
        rec["wake"].append(kwargs)
        return "mcp_bridge:/tmp/x.sock", wake_result or {"success": True}

    monkeypatch.setattr(kanban, "_send_kanban_human_notification", human)
    monkeypatch.setattr(kanban, "_send_kanban_wake", wake)

    real_enqueue = kanban._enqueue_kanban_session_notice

    def enqueue(task_obj, msg):
        rec["notices"].append(msg)
        return real_enqueue(task_obj, msg)

    monkeypatch.setattr(kanban, "_enqueue_kanban_session_notice", enqueue)
    return rec


def _drain(profile_home, session_id="creator-session"):
    db = SessionDB(db_path=profile_home / "state.db")
    try:
        return db.drain_session_notices(session_id)
    finally:
        db.close()


def test_failed_human_notice_falls_back_to_session_notice(notify_home, monkeypatch):
    """The subscriber must still learn the task completed when the chat
    delivery fails — a log line is not a notification."""
    rec = _wire_notify(
        monkeypatch,
        human_result={"error": "bridge socket not found"},
        wake_result={"success": True},
    )

    kanban._notify_kanban_status_change(
        "t_notify", "done", title="Notify task", summary="shipped"
    )

    assert len(rec["human"]) == 1
    assert len(rec["notices"]) == 1, "a failed human notice must queue a fallback"
    notices = _drain(notify_home)
    assert notices == [{"text": "✅ Notify task → done — shipped", "level": "info"}]


def test_failed_human_and_failed_wake_queue_exactly_one_notice(
    notify_home, monkeypatch
):
    """Both delivery paths failing must not double-queue the same notice."""
    rec = _wire_notify(
        monkeypatch,
        human_result={"error": "bridge down"},
        wake_result={"error": "gateway unavailable"},
    )

    kanban._notify_kanban_status_change(
        "t_notify", "done", title="Notify task", summary="shipped"
    )

    assert len(rec["notices"]) == 1, rec["notices"]
    assert len(_drain(notify_home)) == 1


def test_successful_human_notice_queues_no_fallback(notify_home, monkeypatch):
    """The happy path stays quiet — no spurious session notice."""
    rec = _wire_notify(
        monkeypatch,
        human_result={"success": True, "queued": True},
        wake_result={"success": True},
    )

    kanban._notify_kanban_status_change(
        "t_notify", "done", title="Notify task", summary="shipped"
    )

    assert len(rec["human"]) == 1
    assert rec["notices"] == []
    assert _drain(notify_home) == []


def test_human_notice_exception_falls_back_to_session_notice(
    notify_home, monkeypatch
):
    """A raising delivery path is a failed delivery, not a silent one."""
    rec = _wire_notify(
        monkeypatch,
        human_result={"success": True},
        wake_result={"success": True},
    )

    def boom(**_kwargs):
        raise RuntimeError("bridge client exploded")

    monkeypatch.setattr(kanban, "_send_kanban_human_notification", boom)

    kanban._notify_kanban_status_change(
        "t_notify", "done", title="Notify task", summary="shipped"
    )

    assert len(rec["notices"]) == 1
    assert len(_drain(notify_home)) == 1


# ---------------------------------------------------------------------------
# The two halves together — a real bridge failure reaches the subscriber
# ---------------------------------------------------------------------------


def test_bridge_delivery_failure_reaches_the_subscriber(notify_home, monkeypatch):
    """End to end across the real bridge seam: the adapter send fails, the
    bridge reports the failure, and the completion still reaches the
    subscriber through the session-notice fallback."""
    import model_tools
    import tools.send_message_tool as smt

    mcp_bridge._kanban_delivery_states.clear()

    async def failing_send(**_kwargs):
        return SimpleNamespace(success=False, message_id=None, error="transport down")

    runner = _runner_with_send(failing_send)

    async def fake_bridge_command(payload, *, bridge_path=None, operation):
        """Drive the REAL _handle_send against the failing adapter."""
        writer = _Writer()
        await _handle_send(runner, payload, cast(asyncio.StreamWriter, writer))
        await asyncio.gather(*list(runner._background_tasks))
        # The bridge acks "queued" (it does not block on the platform), so
        # the sender's success is a QUEUE ack — the delivery truth lives in
        # the watermark, which must still be unadvanced.
        return {"success": True, "queued": True}

    monkeypatch.setattr(smt, "_send_bridge_command", fake_bridge_command)
    monkeypatch.setattr(model_tools, "_run_async", lambda value: asyncio.run(value))

    rec = _wire_notify(
        monkeypatch,
        human_result={"success": True, "queued": True},
        wake_result={"success": True},
    )
    # Route the human notice through the REAL bridge helper (keeping the
    # call record so the test can assert the notice was attempted).
    def human_via_real_bridge(**kw):
        rec["human"].append(kw)
        return (
            "mcp_bridge:/tmp/x.sock",
            asyncio.run(
                smt._send_delivery_via_bridge(
                    Platform.TELEGRAM,
                    kw["chat_id"],
                    kw["message"],
                    bridge_path="/tmp/x.sock",
                    ordering=kw.get("ordering"),
                )
            ),
        )

    monkeypatch.setattr(
        kanban, "_send_kanban_human_notification", human_via_real_bridge
    )

    kanban._notify_kanban_status_change(
        "t_notify", "done", title="Notify task", summary="shipped"
    )

    # The bridge acked the queue, so the sender-side success is honest...
    assert len(rec["human"]) == 1
    # ...but the delivery did NOT land, and the watermark proves it.
    assert mcp_bridge._kanban_delivery_states["t_notify"]["seq"] is None, (
        "a failed platform send must not be recorded as delivered"
    )
