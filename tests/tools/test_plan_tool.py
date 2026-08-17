"""Regression coverage for plan approval outcomes."""

import sqlite3

from tools import plan_tool


class _UnavailableAgent:
    canonical_session_id = None
    session_id = None
    agent_name = "test-agent"
    _session_temperature = None

    def __init__(self):
        self.callback_calls = 0

    def clarify_callback(self, question, choices=None, multi_select=False) -> str:
        self.callback_calls += 1
        return "User unavailable. Stand down and wait for the user to return. Do nothing else."


class _ApprovedAgent(_UnavailableAgent):
    def clarify_callback(self, question, choices=None, multi_select=False) -> str:
        self.callback_calls += 1
        return "Approve"


def _plan_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT,
            body TEXT,
            status TEXT,
            assignee TEXT,
            created_at INTEGER,
            task_steps TEXT,
            task_stepno INTEGER,
            task_goal TEXT,
            block_kind TEXT,
            prev_temperature REAL,
            previous_task TEXT,
            session_id TEXT,
            board TEXT
        )
        """
    )
    return conn


def test_new_keeps_unavailable_clarify_as_pending_approval(monkeypatch):
    """A CLI timeout must not become a fabricated unspecified denial."""
    conn = _plan_db()
    monkeypatch.setattr(plan_tool, "_get_kanban_db", lambda board=None: conn)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    agent = _UnavailableAgent()

    result = plan_tool._cmd_new(
        agent,
        title="Timeout regression",
        goal="Do not deny an unanswered plan",
        steps=["Wait for approval"],
    )

    assert result.startswith("Plan awaiting approval (")
    assert "denied" not in result.lower()
    assert "unspecified" not in result.lower()
    assert agent.callback_calls == 1
    task = conn.execute("SELECT status, block_kind FROM tasks").fetchone()
    assert task["status"] == "blocked"
    assert task["block_kind"] == "approval"


def test_new_activates_plan_after_an_approve_response(monkeypatch):
    """A real approval still activates the freshly created plan."""
    conn = _plan_db()
    monkeypatch.setattr(plan_tool, "_get_kanban_db", lambda board=None: conn)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    agent = _ApprovedAgent()

    result = plan_tool._cmd_new(
        agent,
        title="Approved plan",
        goal="Activate after approval",
        steps=["Start work"],
    )

    assert result.startswith("TASK APPROVED (")
    task = conn.execute("SELECT status, block_kind FROM tasks").fetchone()
    assert task["status"] == "manual"
    assert task["block_kind"] is None
