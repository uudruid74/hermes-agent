"""Regression: plan block on a MANUAL task notifies and stops (Evan 2026-11-01).

The t_f022fa1b incident: block on a bound plan task -> 'blocked' ->
unblock -> 'ready' -> dispatcher claims -> second worker on a live
binding. A manual task is a live plan; block must never write it.
"""

import sqlite3
import sys

from tools import plan
from hermes_cli.kanban_db import SCHEMA_SQL


def _make_db(tmp_path, status, task_id="t_manual"):
    db_path = tmp_path / "kanban.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            "INSERT INTO tasks (id, title, status, assignee, created_at, "
            "task_steps, task_stepno, task_goal, board) VALUES (?, ?, ?, ?, 1, "
            "'[\"Step 1\"]', 1, 'Goal', 'default')",
            (task_id, "Manual plan", status, "gopher"),
        )
    return db_path


def test_block_on_manual_task_notifies_and_does_not_write(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, "manual")
    monkeypatch.setattr(plan, "_enumerate_kanban_dbs", lambda: [str(db_path)])
    events = {}
    monkeypatch.setattr(
        plan,
        "_manual_block_notice",
        lambda task_id: events.update({"called_with": task_id})
        or "NOTIFY: manual task - not blocked",
    )

    result = plan._cmd_block("t_manual")

    assert result == "NOTIFY: manual task - not blocked"
    assert events == {"called_with": "t_manual"}
    with sqlite3.connect(db_path) as conn:
        status = conn.execute("SELECT status FROM tasks WHERE id='t_manual'").fetchone()[0]
    assert status == "manual"  # the invariant: never written to blocked


def test_block_on_ready_task_still_blocks(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, "ready", task_id="t_ready")
    monkeypatch.setattr(plan, "_enumerate_kanban_dbs", lambda: [str(db_path)])
    monkeypatch.setattr(plan, "_manual_block_notice", lambda task_id: "NOTIFY")

    result = plan._cmd_block("t_ready")

    assert "BLOCKED:" in result
    with sqlite3.connect(db_path) as conn:
        status = conn.execute("SELECT status FROM tasks WHERE id='t_ready'").fetchone()[0]
    assert status == "blocked"


def test_manual_block_notice_text(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, "manual")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    from tools.plan_binding_adapter import _send_user
    from tools.plan_binding_adapter import _dispatcher_for_task

    monkeypatch.setattr(plan, "_get_kanban_db", lambda board=None: conn)
    monkeypatch.setattr(
        sys.modules["tools.plan_binding_adapter"],
        "_send_user",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        sys.modules["tools.plan_binding_adapter"],
        "_dispatcher_for_task",
        lambda *a, **k: None,  # no AI dispatcher; fall back to assignee row
    )

    text = plan._manual_block_notice("t_manual")
    assert "manual plan task" in text
    assert "not blocked" in text
    assert "nothing more to do" in text.lower()
