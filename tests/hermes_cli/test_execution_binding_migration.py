"""Read-only legacy task-identity migration audit coverage."""
from __future__ import annotations

import sqlite3

from hermes_cli.execution_binding_migration import migration_manifest
from hermes_cli.kanban_db import SCHEMA_SQL


def _connections():
    state = sqlite3.connect(":memory:")
    state.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, task_id TEXT)")
    kanban = sqlite3.connect(":memory:")
    kanban.executescript(SCHEMA_SQL)
    return state, kanban


def _task(conn, task_id: str, status: str):
    conn.execute(
        "INSERT INTO tasks (id, title, status, assignee, created_at, board) "
        "VALUES (?, ?, ?, 'neo', 1, 'default')",
        (task_id, task_id, status),
    )


def test_manifest_is_sorted_read_only_and_reports_nonactive_legacy_rows():
    state, kanban = _connections()
    state.executemany(
        "INSERT INTO sessions (id, task_id) VALUES (?, ?)",
        [("z-session", "t_missing"), ("a-session", "t_done")],
    )
    _task(kanban, "t_done", "done")
    state_before = list(state.execute("SELECT id, task_id FROM sessions ORDER BY id"))
    kanban_before = list(kanban.execute("SELECT id, status FROM tasks ORDER BY id"))

    manifest = migration_manifest(state, kanban)

    assert manifest["mutates_production"] is False
    assert manifest["candidate_count"] == 0
    assert manifest["conflict_count"] == 2
    assert [row["session_id"] for row in manifest["records"]] == ["a-session", "z-session"]
    assert [row["reason"] for row in manifest["records"]] == [
        "task-not-active-plan", "task-missing"
    ]
    assert list(state.execute("SELECT id, task_id FROM sessions ORDER BY id")) == state_before
    assert list(kanban.execute("SELECT id, status FROM tasks ORDER BY id")) == kanban_before
