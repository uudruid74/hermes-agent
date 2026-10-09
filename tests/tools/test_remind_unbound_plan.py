"""Regression tests for the remind phantom-activation bug (t_a63a4220).

Bug: `plan_tool remind <task_id>` on an unclaimed/reset manual plan rendered
"Active Step 1 of N" because of the `task_stepno or 1` fallback. A fresh
worker (post-gateway-restart, post-reset) believed the plan was running in
its session, implemented the step, then burned multiple `advance` calls
against "ERROR: No active task". Observed on ornith 2026-10-09 06:09-06:22.

Fix: cmd_remind with explicit task_id checks the caller's execution binding;
unbound -> "NOT ACTIVE in this session... Run `plan_tool continue <tid>`".
"""

import json
import sqlite3

import pytest

from tools import plan_binding_adapter


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT,
            status TEXT, workspace_kind TEXT, workspace_path TEXT,
            task_steps TEXT, task_stepno INTEGER, task_goal TEXT,
            created_at INTEGER
        );
        CREATE TABLE execution_bindings (
            profile TEXT, root_session_id TEXT, task_id TEXT,
            revision INTEGER, bound_at INTEGER, updated_at INTEGER
        );
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, run_id INTEGER,
            kind TEXT, payload TEXT, created_at INTEGER
        );
        CREATE TABLE task_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, kind TEXT,
            body TEXT, created_by TEXT, created_at INTEGER
        );
        """
    )
    return conn


class _FakeSessionDB:
    def get_compression_root(self, session_id):
        return session_id  # strict lineage = the session itself


class _Agent:
    profile_name = "ornith"
    session_id = "fresh-session-after-restart"
    _session_db = _FakeSessionDB()


def _insert_plan(conn, task_id="t_a63a4220", stepno=None):
    conn.execute(
        "INSERT INTO tasks (id, title, assignee, status, task_steps, task_stepno, task_goal, created_at) "
        "VALUES (?, 'Pente Run 4', 'ornith', 'manual', ?, ?, 'test goal', 0)",
        (
            task_id,
            json.dumps(
                [
                    "init(): DB ready",
                    "new_game + set_board",
                ]
            ),
            stepno,
        ),
    )
    conn.commit()


def test_remind_explicit_task_id_unbound_says_not_active_not_step_1(monkeypatch):
    """The bug: unbound manual plan rendered 'Active Step 1 of 2' — phantom."""
    conn = _db()
    _insert_plan(conn, stepno=None)  # reset state: stepno NULL, no binding
    monkeypatch.setattr(plan_binding_adapter._legacy(), "_get_kanban_db", lambda board=None: conn)

    out = plan_binding_adapter.cmd_remind(_Agent(), task_id="t_a63a4220")

    assert "NOT ACTIVE in this session" in out
    assert "plan_tool continue t_a63a4220" in out
    assert "Active Step 1 of" not in out  # the phantom is dead


def test_remind_explicit_task_id_bound_renders_active_step(monkeypatch):
    """Bound plan keeps the normal active-step rendering."""
    conn = _db()
    _insert_plan(conn, stepno=1)
    conn.execute(
        "INSERT INTO execution_bindings VALUES ('ornith','fresh-session-after-restart','t_a63a4220',1,0,0)"
    )
    conn.commit()
    monkeypatch.setattr(plan_binding_adapter._legacy(), "_get_kanban_db", lambda board=None: conn)

    out = plan_binding_adapter.cmd_remind(_Agent(), task_id="t_a63a4220")

    assert "Active Step 1 of 2: init(): DB ready" in out
    assert "NOT ACTIVE" not in out


def test_remind_explicit_task_id_bound_to_other_task_says_not_active(monkeypatch):
    """Session bound to a DIFFERENT task: this task is not active here."""
    conn = _db()
    _insert_plan(conn, task_id="t_aaa", stepno=2)
    _insert_plan(conn, task_id="t_bbb", stepno=1)
    conn.execute(
        "INSERT INTO execution_bindings VALUES ('ornith','fresh-session-after-restart','t_bbb',1,0,0)"
    )
    conn.commit()
    monkeypatch.setattr(plan_binding_adapter._legacy(), "_get_kanban_db", lambda board=None: conn)

    out = plan_binding_adapter.cmd_remind(_Agent(), task_id="t_aaa")

    assert "NOT ACTIVE in this session" in out
    assert "Active Step 2 of" not in out


def test_remind_no_task_id_unbound_still_steers_to_continue(monkeypatch):
    """Pre-existing behavior kept: unbound remind with no task_id steers to continue."""
    conn = _db()
    _insert_plan(conn, stepno=None)
    monkeypatch.setattr(plan_binding_adapter._legacy(), "_get_kanban_db", lambda board=None: conn)

    out = plan_binding_adapter.cmd_remind(_Agent())

    assert ("continue" in out.lower()) and ("No active plan" in out or "not claimed" in out.lower())