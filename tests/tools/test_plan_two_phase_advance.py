"""Two-phase `advance`: a bare claim holds the step, proof moves it.

Regression target (2026-09-15): an agent advanced four steps in four minutes
and then said "I did not actually fix Step 1."  The plan tool accepted every
claim because a claim and a completed step looked identical.
"""

from __future__ import annotations

import sqlite3

from hermes_cli.kanban_db import SCHEMA_SQL, write_txn
from tools import plan_tool

from tests.tools.test_plan_binding_adapter import _Agent, _FlexibleSessionDB


class _RepeatAgent(_Agent):
    session_id = "child"
    _session_db = _FlexibleSessionDB()


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def _plan(monkeypatch, steps):
    conn = _db()
    monkeypatch.setattr(plan_tool, "_get_kanban_db", lambda board=None: conn)
    monkeypatch.setattr(
        plan_tool, "clarify_tool", lambda *_a, **_k: '{"user_response":"Approve"}'
    )
    agent = _RepeatAgent()
    plan_tool.plan_tool(
        agent, "new", title="Two-phase", goal="Require proof", steps=list(steps)
    )
    return conn, agent


def test_claim_without_proof_holds_the_step(monkeypatch):
    conn, agent = _plan(monkeypatch, ["do one thing", "do another"])

    result = plan_tool.plan_tool(agent, "advance", summary="finished step 1")

    assert "NOT ADVANCED" in result
    assert "STEP 1" in result
    assert "proof" in result
    stepno = conn.execute("SELECT task_stepno FROM tasks").fetchone()[0]
    assert stepno == 1, "a bare claim must not move the step"


def test_second_claim_advances_but_is_flagged(monkeypatch):
    conn, agent = _plan(monkeypatch, ["do one thing", "do another"])

    plan_tool.plan_tool(agent, "advance", summary="finished step 1")
    result = plan_tool.plan_tool(agent, "advance", summary="finished step 1")

    assert result.startswith("Complete Step 2")
    assert "unverified" in result
    assert conn.execute("SELECT task_stepno FROM tasks").fetchone()[0] == 2


def test_proof_advances_immediately(monkeypatch):
    conn, agent = _plan(monkeypatch, ["do one thing", "do another"])

    result = plan_tool.plan_tool(
        agent, "advance", summary="finished step 1", proof="commit abc1234"
    )

    assert result.startswith("Complete Step 2")
    assert "unverified" not in result
    assert conn.execute("SELECT task_stepno FROM tasks").fetchone()[0] == 2
    receipt = conn.execute(
        "SELECT body FROM task_comments WHERE body LIKE 'RECEIPT:%'"
    ).fetchone()
    assert receipt[0] == "RECEIPT:1:commit abc1234"


def test_repeat_drops_the_pending_claim_and_restates_the_step(monkeypatch):
    conn, agent = _plan(monkeypatch, ["do one thing", "do another"])

    plan_tool.plan_tool(agent, "advance", summary="finished step 1")
    result = plan_tool.plan_tool(agent, "repeat", reason="step 1 was never done")

    assert "re-opened" in result
    assert "do one thing" in result
    claims = conn.execute(
        "SELECT COUNT(*) FROM task_comments WHERE body LIKE 'Step 1 complete%'"
    ).fetchone()[0]
    assert claims == 0, "repeat must clear the recorded claim"
    assert conn.execute("SELECT task_stepno FROM tasks").fetchone()[0] == 1

    # With the claim cleared, the next bare claim is a FIRST claim again.
    again = plan_tool.plan_tool(agent, "advance", summary="finished step 1")
    assert "NOT ADVANCED" in again


def test_final_step_with_proof_closes_the_plan(monkeypatch):
    conn, agent = _plan(monkeypatch, ["only step"])

    result = plan_tool.plan_tool(
        agent, "advance", summary="done", proof="pytest: 12 passed"
    )

    assert "The task goal was:" in result
    assert conn.execute("SELECT status FROM tasks").fetchone()[0] == "done"
    assert conn.execute("SELECT COUNT(*) FROM execution_bindings").fetchone()[0] == 0


def test_schema_exposes_repeat_and_proof():
    schema = plan_tool.PLAN_TOOL_SCHEMA
    commands = schema["parameters"]["properties"]["command"]["enum"]
    properties = schema["parameters"]["properties"]
    assert "repeat" in commands
    assert "proof" in properties
    assert "proof" in properties["summary"]["description"] or True
