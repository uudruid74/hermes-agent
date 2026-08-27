"""Plan-tool adapters must resolve runtime identity only through execution bindings."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from hermes_cli.kanban_db import SCHEMA_SQL
from tools import plan_tool


class _SessionDB:
    def get_compression_root(self, session_id: str) -> str:
        assert session_id == "current-compression-child"
        return "compression-root"


class _Agent:
    profile_name = "Neo"
    agent_name = "neo"
    session_id = "current-compression-child"
    canonical_session_id = "stale-canonical-session"
    _session_db = _SessionDB()
    _session_temperature = None
    _plan_approval_timed_out = None

    def clarify_callback(self, *_args, **_kwargs) -> str:
        return "Approve"


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def test_new_approval_binds_compression_root_not_session_or_env(monkeypatch):
    conn = _db()
    monkeypatch.setattr(plan_tool, "_get_kanban_db", lambda board=None: conn)
    monkeypatch.setattr(
        plan_tool, "clarify_tool", lambda *_args, **_kwargs: '{"user_response":"Approve"}'
    )
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_wrong_env_task")

    result = plan_tool.plan_tool(
        _Agent(), "new", title="Bound plan", goal="Bind safely", steps=["work"]
    )

    assert result.startswith("TASK APPROVED (t_")
    task = conn.execute("SELECT id, status FROM tasks").fetchone()
    binding = conn.execute(
        "SELECT profile, root_session_id, task_id FROM execution_bindings"
    ).fetchone()
    assert task["status"] == "manual"
    assert tuple(binding) == ("neo", "compression-root", task["id"])


def test_new_requires_explicit_matching_parent(monkeypatch):
    conn = _db()
    monkeypatch.setattr(plan_tool, "_get_kanban_db", lambda board=None: conn)
    monkeypatch.setattr(
        plan_tool, "clarify_tool", lambda *_args, **_kwargs: '{"user_response":"Approve"}'
    )
    agent = _Agent()

    first = plan_tool.plan_tool(agent, "new", title="Parent", goal="parent", steps=["one"])
    assert first.startswith("TASK APPROVED")
    before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]

    blocked = plan_tool.plan_tool(agent, "new", title="Implicit child", goal="no", steps=["one"])
    assert blocked.startswith("ACTIVE_PLAN")
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before

    parent_id = conn.execute("SELECT task_id FROM execution_bindings").fetchone()[0]
    nested = plan_tool.plan_tool(
        agent,
        "new",
        title="Explicit child",
        goal="yes",
        steps=["one"],
        parent_task_id=parent_id,
    )
    assert nested.startswith("TASK APPROVED")
    child = conn.execute("SELECT task_id FROM execution_bindings").fetchone()[0]
    assert conn.execute("SELECT previous_task FROM tasks WHERE id=?", (child,)).fetchone()[0] == parent_id


def test_terminal_commands_close_binding_without_legacy_session_mutation(monkeypatch):
    conn = _db()
    monkeypatch.setattr(plan_tool, "_get_kanban_db", lambda board=None: conn)
    monkeypatch.setattr(
        plan_tool, "clarify_tool", lambda *_args, **_kwargs: '{"user_response":"Approve"}'
    )
    agent = _Agent()
    result = plan_tool.plan_tool(agent, "new", title="Finish", goal="finish", steps=["one"])
    task_id = conn.execute("SELECT task_id FROM execution_bindings").fetchone()[0]

    done = plan_tool.plan_tool(agent, "done")

    assert "goal was: finish" in done
    assert conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()[0] == "done"
    assert conn.execute("SELECT COUNT(*) FROM execution_bindings").fetchone()[0] == 0
