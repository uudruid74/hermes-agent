"""Regression coverage for plan approval outcomes."""

import sqlite3
import threading
from types import SimpleNamespace

from tools import plan_tool


class _UnavailableAgent:
    canonical_session_id = None
    session_id = None
    agent_name = "test-agent"
    _session_temperature = None
    _plan_approval_timed_out = None

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
    from hermes_cli.kanban_db import SCHEMA_SQL

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def test_get_session_id_uses_only_canonical_session_id():
    agent = SimpleNamespace(canonical_session_id=None, session_id="mutable-session")

    assert plan_tool._get_session_id(agent) is None


def test_new_keeps_unavailable_clarify_as_pending_approval(monkeypatch):
    """A CLI timeout must not become a fabricated unspecified denial."""
    conn = _plan_db()
    monkeypatch.setattr(plan_tool, "_get_kanban_db", lambda board=None: conn)
    def unavailable_bridge(question, choices, multi_select=False, callback=None, **_kwargs):
        assert callback is not None
        answer = callback(question, choices, multi_select=multi_select)
        return '{"user_response": "%s"}' % answer

    monkeypatch.setattr(
        plan_tool,
        "clarify_tool",
        unavailable_bridge,
        raising=False,
    )
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
    assert isinstance(agent._plan_approval_timed_out, str)
    assert agent._plan_approval_timed_out.startswith("t_")
    task = conn.execute("SELECT status, block_kind FROM tasks").fetchone()
    authorization = conn.execute(
        "SELECT state, execution_session_id, origin_session_id "
        "FROM plan_authorizations"
    ).fetchone()
    assert task["status"] == "blocked"
    assert task["block_kind"] == "approval"
    assert authorization["state"] == "pending"
    assert authorization["execution_session_id"] is None
    assert authorization["origin_session_id"] is None


def test_new_activates_plan_after_an_approve_response(monkeypatch):
    """A real approval still activates the freshly created plan."""
    conn = _plan_db()
    monkeypatch.setattr(plan_tool, "_get_kanban_db", lambda board=None: conn)
    monkeypatch.setattr(
        plan_tool,
        "clarify_tool",
        lambda *_args, **_kwargs: '{"user_response": "Approve"}',
        raising=False,
    )
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


def test_new_links_approval_prompt_to_its_new_plan(monkeypatch):
    """Dashboard approval must target the plan task, not the prior session task."""
    conn = _plan_db()
    monkeypatch.setattr(plan_tool, "_get_kanban_db", lambda board=None: conn)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    seen = {}

    def bridge(question, choices, multi_select=False, callback=None, agent=None, task_id=None):
        seen.update({
            "question": question,
            "choices": choices,
            "callback": callback,
            "agent": agent,
            "task_id": task_id,
        })
        return '{"user_response": "Approve"}'

    monkeypatch.setattr(plan_tool, "clarify_tool", bridge, raising=False)
    agent = _ApprovedAgent()

    result = plan_tool._cmd_new(
        agent,
        title="Dashboard-linked plan",
        goal="Route approval through clarify_queue",
        steps=["Wait for dashboard approval"],
    )

    task_id = conn.execute("SELECT id FROM tasks").fetchone()[0]
    assert result.startswith("TASK APPROVED (")
    assert seen["task_id"] == task_id
    assert seen["choices"] == ["Approve", "Deny"]
    assert seen["callback"] == agent.clarify_callback
    assert seen["agent"] is agent


def test_new_activates_from_dashboard_queue_answer(tmp_path, monkeypatch):
    """A dashboard answer wins the clarify race and activates its plan."""
    from hermes_cli import kanban_db
    from hermes_cli.kanban_db import SCHEMA_SQL

    db_path = tmp_path / "kanban.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
    monkeypatch.setattr(kanban_db, "kanban_db_path", lambda board=None: db_path)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

    def dashboard_callback(*_args, **_kwargs):
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE clarify_queue SET status='answered', answer='Approve' "
                "WHERE status='pending'"
            )
        threading.Event().wait(2)
        return "User unavailable. Stand down and wait for the user to return. Do nothing else."

    agent = SimpleNamespace(
        canonical_session_id=None,
        session_id=None,
        agent_name="neo",
        _session_temperature=None,
        clarify_callback=dashboard_callback,
    )
    result = plan_tool._cmd_new(
        agent,
        title="Dashboard race plan",
        goal="Approve through clarify_queue",
        steps=["Begin after dashboard approval"],
    )

    with sqlite3.connect(db_path) as conn:
        task = conn.execute("SELECT id, status FROM tasks").fetchone()
        queue = conn.execute(
            "SELECT task_id, status, answer FROM clarify_queue"
        ).fetchone()
    assert result.startswith("TASK APPROVED (")
    assert task[1] == "manual"
    assert queue == (task[0], "answered", "Approve")


def test_remind_returns_requested_task_status(monkeypatch):
    conn = _plan_db()
    conn.execute(
        """INSERT INTO tasks
           (id, title, status, assignee, created_at, task_steps, task_stepno,
            task_goal, board)
           VALUES ('t_remind', 'Status check', 'blocked', 'neo', 1,
                   '[\"Inspect status\"]', 1, 'Report the status', 'default')"""
    )
    conn.commit()
    monkeypatch.setattr(plan_tool, "_get_kanban_db", lambda board=None: conn)

    result = plan_tool._cmd_remind(_UnavailableAgent(), "t_remind")

    assert "Task: Status check" in result
    assert "Status: blocked" in result
