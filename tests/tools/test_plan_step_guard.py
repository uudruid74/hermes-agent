"""`step=N` guard — a drifted or duplicated advance is refused, not applied.

Regression target (2026-09-15, Ornith t_4e289ee7): the same "Complete Step 1"
advance call was issued twice in one second.  Nothing checked which step the
caller thought it was on, so the plan moved forward on both.
"""

from __future__ import annotations

import sqlite3

from hermes_cli.kanban_db import SCHEMA_SQL
from tools import plan_tool

from tests.tools.test_plan_binding_adapter import _Agent, _FlexibleSessionDB


class _StepAgent(_Agent):
    session_id = "child"
    _session_db = _FlexibleSessionDB()


def _plan(monkeypatch, steps):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    monkeypatch.setattr(plan_tool, "_get_kanban_db", lambda board=None: conn)
    monkeypatch.setattr(
        plan_tool, "clarify_tool", lambda *_a, **_k: '{"user_response":"Approve"}'
    )
    agent = _StepAgent()
    plan_tool.plan_tool(agent, "new", title="Guarded", goal="stay on step", steps=list(steps))
    return conn, agent


def test_matching_step_advances(monkeypatch):
    conn, agent = _plan(monkeypatch, ["one", "two"])

    result = plan_tool.plan_tool(agent, "advance", summary="did one", step=1)

    assert "Now complete Step 2" in result
    assert conn.execute("SELECT task_stepno FROM tasks").fetchone()[0] == 2


def test_wrong_step_is_refused_and_plan_does_not_move(monkeypatch):
    conn, agent = _plan(monkeypatch, ["one", "two"])

    result = plan_tool.plan_tool(agent, "advance", summary="did two", step=2)

    assert "step 2 is not the active step" in result
    assert "remind" in result
    assert conn.execute("SELECT task_stepno FROM tasks").fetchone()[0] == 1, (
        "a drifted step claim must not move the plan"
    )
    assert conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE kind='plan-step-review-pending'"
    ).fetchone()[0] == 0


def test_duplicate_advance_cannot_double_advance(monkeypatch):
    """The exact 2026-09-15 shape: the same call issued twice."""
    conn, agent = _plan(monkeypatch, ["one", "two", "three"])

    first = plan_tool.plan_tool(agent, "advance", summary="did one", step=1)
    second = plan_tool.plan_tool(agent, "advance", summary="did one", step=1)

    assert "Now complete Step 2" in first
    assert "not the active step" in second, "the repeat must be refused"
    assert conn.execute("SELECT task_stepno FROM tasks").fetchone()[0] == 2


def test_omitting_step_still_works(monkeypatch):
    conn, agent = _plan(monkeypatch, ["one", "two"])

    result = plan_tool.plan_tool(agent, "advance", summary="did one")

    assert "Now complete Step 2" in result


def test_schema_exposes_step():
    props = plan_tool.PLAN_TOOL_SCHEMA["parameters"]["properties"]
    assert props["step"]["type"] == "integer"
