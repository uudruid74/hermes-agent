"""Two-phase `advance`: only proof moves a step. A claim holds it, forever.

Regression target (2026-09-15): an agent advanced four steps in four minutes
and then said "I did not actually fix Step 1."  The plan tool accepted every
claim because a claim and a completed step looked identical.

Evan's rule (2026-09-15): repeating a bare claim must NOT advance.  The only
ways past a held step are `proof` on `advance`, or `repeat` to re-open the
step as a corrective child plan.
"""

from __future__ import annotations

import sqlite3

from hermes_cli.kanban_db import SCHEMA_SQL
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


def _stepno(conn) -> int:
    return conn.execute("SELECT task_stepno FROM tasks WHERE status='manual'").fetchone()[0]


def test_claim_without_proof_holds_the_step(monkeypatch):
    conn, agent = _plan(monkeypatch, ["do one thing", "do another"])

    result = plan_tool.plan_tool(agent, "advance", summary="finished step 1")

    assert "NOT ADVANCED" in result
    assert "STEP 1" in result
    assert "proof" in result
    assert _stepno(conn) == 1, "a bare claim must not move the step"


def test_repeating_the_claim_still_does_not_advance(monkeypatch):
    """Evan, 2026-09-15: a second bare claim is NOT an escape hatch."""
    conn, agent = _plan(monkeypatch, ["do one thing", "do another"])

    plan_tool.plan_tool(agent, "advance", summary="finished step 1")
    result = plan_tool.plan_tool(agent, "advance", summary="finished step 1")

    assert "NOT ADVANCED" in result
    assert _stepno(conn) == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM task_comments WHERE body LIKE 'RECEIPT:%'"
    ).fetchone()[0] == 0


def test_claim_records_the_summary_for_the_step(monkeypatch):
    """The claim is not wasted: its summary lands on the step it claimed."""
    conn, agent = _plan(monkeypatch, ["do one thing", "do another"])

    plan_tool.plan_tool(agent, "advance", summary="wired the parser")

    body = conn.execute(
        "SELECT body FROM task_comments WHERE body LIKE '[plan-step-summary:1]%'"
    ).fetchone()[0]
    assert body == "[plan-step-summary:1] wired the parser"
    assert _stepno(conn) == 1


def test_proof_advances_immediately(monkeypatch):
    conn, agent = _plan(monkeypatch, ["do one thing", "do another"])

    result = plan_tool.plan_tool(
        agent, "advance", summary="finished step 1", proof="commit abc1234"
    )

    assert result.startswith("Complete Step 2")
    assert _stepno(conn) == 2
    receipt = conn.execute(
        "SELECT body FROM task_comments WHERE body LIKE 'RECEIPT:%'"
    ).fetchone()
    assert receipt[0] == "RECEIPT:1:commit abc1234"


def test_final_step_with_proof_closes_the_plan(monkeypatch):
    conn, agent = _plan(monkeypatch, ["only step"])

    result = plan_tool.plan_tool(
        agent, "advance", summary="done", proof="pytest: 12 passed"
    )

    assert "The task goal was:" in result
    assert conn.execute("SELECT status FROM tasks").fetchone()[0] == "done"
    assert conn.execute("SELECT COUNT(*) FROM execution_bindings").fetchone()[0] == 0


def test_final_step_proof_is_still_recorded(monkeypatch):
    """A close must not swallow the receipt for the step that closed it."""
    conn, agent = _plan(monkeypatch, ["only step"])

    plan_tool.plan_tool(agent, "advance", summary="done", proof="pytest: 12 passed")

    body = conn.execute(
        "SELECT body FROM task_comments WHERE body LIKE 'RECEIPT:%'"
    ).fetchone()[0]
    assert body == "RECEIPT:1:pytest: 12 passed"


# --- repeat: re-open a step as a corrective child plan -------------------


def _repeat_without_plan(monkeypatch):
    conn, agent = _plan(monkeypatch, ["do one thing", "do another"])
    plan_tool.plan_tool(agent, "advance", summary="claimed it")
    return conn, agent


def test_repeat_without_steps_demands_a_corrective_plan(monkeypatch):
    conn, agent = _repeat_without_plan(monkeypatch)

    result = plan_tool.plan_tool(agent, "repeat", step=1, reason="never did it")

    assert "REQUIRES A CORRECTIVE PLAN" in result
    assert "do one thing" in result, "the re-opened step's text must be shown"
    assert "step=1" in result
    # Nothing was re-opened, and the parent did not move.
    assert _stepno(conn) == 1
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_repeat_with_a_plan_creates_a_corrective_child(monkeypatch):
    conn, agent = _repeat_without_plan(monkeypatch)

    result = plan_tool.plan_tool(
        agent,
        "repeat",
        step=1,
        reason="parser was never wired",
        title="Fix step 1 parser",
        goal="wire the parser for real",
        steps=["read the parser", "wire it", "verify"],
    )

    assert "CORRECTIVE PLAN APPROVED" in result
    child = conn.execute(
        "SELECT id, previous_task, task_steps, status FROM tasks "
        "WHERE previous_task IS NOT NULL"
    ).fetchone()
    assert child["previous_task"] is not None
    assert child["status"] == "manual"
    # The child owns its own step list and log.
    import json

    assert json.loads(child["task_steps"]) == ["read the parser", "wire it", "verify"]
    assert conn.execute(
        "SELECT COUNT(*) FROM task_comments WHERE body LIKE 'REPEAT-OF:%'"
    ).fetchone()[0] == 1
    # The parent is frozen at step 1 while the child runs.
    assert _stepno(conn) == 1


def test_repeat_clears_the_parent_step_summary(monkeypatch):
    """The claim was wrong, so it must not survive as the step's summary."""
    conn, agent = _repeat_without_plan(monkeypatch)

    plan_tool.plan_tool(
        agent,
        "repeat",
        step=1,
        title="Fix step 1",
        goal="fix it",
        steps=["actually fix it"],
    )

    assert conn.execute(
        "SELECT COUNT(*) FROM task_comments WHERE task_id = "
        "(SELECT previous_task FROM tasks WHERE previous_task IS NOT NULL) "
        "AND body LIKE '[plan-step-summary:1]%'"
    ).fetchone()[0] == 0


def test_child_completion_summary_returns_to_the_parent_step(monkeypatch):
    """The whole point: the child's outcome lands on the parent's step."""
    conn, agent = _repeat_without_plan(monkeypatch)

    plan_tool.plan_tool(
        agent,
        "repeat",
        step=1,
        title="Fix step 1",
        goal="fix it",
        steps=["actually fix it"],
    )
    parent_id = conn.execute(
        "SELECT previous_task FROM tasks WHERE previous_task IS NOT NULL"
    ).fetchone()[0]

    result = plan_tool.plan_tool(
        agent, "advance", summary="parser wired and tested", proof="pytest: 4 passed"
    )

    assert "Continuing parent" in result or "The task goal was:" in result
    body = conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ? "
        "AND body LIKE '[plan-step-summary:1]%'",
        (parent_id,),
    ).fetchone()[0]
    assert "parser wired and tested" in body
    # Parent resumed on the same step it was re-opened at — not moved on.
    assert conn.execute(
        "SELECT task_stepno FROM tasks WHERE id = ?", (parent_id,)
    ).fetchone()[0] == 1
    # And the parent is bound again.
    binding = conn.execute(
        "SELECT task_id FROM execution_bindings"
    ).fetchone()
    assert binding[0] == parent_id


def test_repeat_step_must_be_in_range(monkeypatch):
    conn, agent = _repeat_without_plan(monkeypatch)

    result = plan_tool.plan_tool(agent, "repeat", step=9, steps=["x"])

    assert "outside 1..2" in result


def test_repeat_defaults_to_the_active_step(monkeypatch):
    conn, agent = _repeat_without_plan(monkeypatch)

    result = plan_tool.plan_tool(agent, "repeat", reason="needs work", steps=["x"])

    assert "requires title and goal" in result, "active step 1 is the default target"


def test_schema_exposes_repeat_and_proof():
    schema = plan_tool.PLAN_TOOL_SCHEMA
    commands = schema["parameters"]["properties"]["command"]["enum"]
    properties = schema["parameters"]["properties"]
    assert "repeat" in commands
    assert "proof" in properties
    assert "repeat" in properties["steps"]["description"]
