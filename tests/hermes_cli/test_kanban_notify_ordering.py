"""Delivery-ordering seq plumbing for kanban status notifications (t_d979ec8d).

Every status notification carries a monotonic seq — the transition's own
``task_events.id`` — so the gateway can drop stale/out-of-order deliveries
("a subscriber never receives a status notification for a state older than
what they already received"). These tests pin:

1. ``_append_event`` returns the new row id and ``latest_task_event_id``
   reads back the newest one (the fallback for callers without a fresh id),
2. the DB transition hooks hand their exact event id to the notifier.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hermes_cli import kanban_db as kb  # noqa: E402
from hermes_cli import kanban  # noqa: E402


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_append_event_returns_monotonic_ids(kanban_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="seq", assignee="zephyr")
        id1 = kb._append_event(conn, tid, "claimed", {"lock": "L"})
        id2 = kb._append_event(conn, tid, "completed", {})
        assert id1 is not None and id2 is not None
        assert id2 == id1 + 1, "task_events ids must be strictly increasing"
        assert kb.latest_task_event_id(conn, tid) == id2
    finally:
        conn.close()


def test_latest_task_event_id_unknown_task_is_none(kanban_home):
    conn = kb.connect()
    try:
        assert kb.latest_task_event_id(conn, "t_nope") is None
    finally:
        conn.close()


def test_claim_notification_carries_its_event_id(kanban_home, monkeypatch):
    captured: dict = {}

    def fake_notify(task_id, status, **kw):
        captured["task_id"] = task_id
        captured["status"] = status
        captured["event_id"] = kw.get("event_id")

    monkeypatch.setattr(
        "hermes_cli.kanban._notify_kanban_status_change", fake_notify
    )
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="claimed", assignee="zephyr")
        claimed = kb.claim_task(conn, tid, ttl_seconds=60, claimer="zephyr")
        assert claimed is not None
        # The notified seq must equal the 'claimed' event row id exactly.
        row = conn.execute(
            "SELECT id FROM task_events WHERE task_id = ? AND kind = 'claimed'",
            (tid,),
        ).fetchone()
        assert captured["task_id"] == tid
        assert captured["status"] == "running"
        assert captured["event_id"] == row[0]
    finally:
        conn.close()


def test_completion_notification_carries_its_event_id(kanban_home, monkeypatch):
    captured: dict = {}

    def fake_notify(task_id, status, **kw):
        captured["task_id"] = task_id
        captured["status"] = status
        captured["event_id"] = kw.get("event_id")

    monkeypatch.setattr(
        "hermes_cli.kanban._notify_kanban_status_change", fake_notify
    )
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="done", assignee="zephyr")
        kb.claim_task(conn, tid, ttl_seconds=60, claimer="zephyr")
        assert kb.complete_task(conn, tid, summary="ok", result="ok") is True
        row = conn.execute(
            "SELECT id FROM task_events WHERE task_id = ? AND kind = 'completed'",
            (tid,),
        ).fetchone()
        assert captured["status"] == "done"
        assert captured["event_id"] == row[0]
    finally:
        conn.close()


def test_notify_falls_back_to_latest_event_id(kanban_home, monkeypatch):
    """Callers without a fresh id (CLI create, tools, plan bindings) get the
    fallback seq — the task's newest event row."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="fallback", assignee="zephyr")
        expected = kb.latest_task_event_id(conn, tid)
    finally:
        conn.close()

    captured: list = []
    monkeypatch.setattr(kanban, "kick_dispatch", lambda *a, **kw: None)
    monkeypatch.setattr(
        kanban,
        "_deliver_kanban_notification",
        lambda task_id, status, **kw: captured.append(kw.get("event_id")),
    )
    kanban._notify_kanban_status_change(tid, "running", title="fallback")
    assert captured == [None]  # event_id absent → delivery resolves the fallback


def test_human_notification_orders_stamped_send(kanban_home, monkeypatch):
    """The {task_id, seq, status} stamp must reach the bridge payload."""
    import asyncio

    import model_tools
    import tools.send_message_tool

    calls: list = []

    def send_delivery(platform, chat_id, message, **kwargs):
        calls.append(kwargs)
        return {"success": True, "queued": True}

    monkeypatch.setattr(
        tools.send_message_tool, "_send_delivery_via_bridge", send_delivery
    )
    monkeypatch.setattr(model_tools, "_run_async", lambda value: value)

    adapter, result = kanban._send_kanban_human_notification(
        platform="telegram",
        chat_id="123",
        thread_id="",
        message="hi",
        notify_env={},
        ordering={"task_id": "t_9", "seq": 4, "status": "done"},
    )
    assert result == {"success": True, "queued": True}
    assert calls[0]["ordering"] == {"task_id": "t_9", "seq": 4, "status": "done"}

    # Unstamped sends stay unstamped — no ordering key at all.
    calls.clear()
    kanban._send_kanban_human_notification(
        platform="telegram",
        chat_id="123",
        thread_id="",
        message="hi",
        notify_env={},
    )
    assert "ordering" not in calls[0]
    assert not asyncio.iscoroutine(result)
