"""Debug-plan incentives and archival regression coverage."""

from __future__ import annotations

from contextlib import contextmanager
import json
import re
import sqlite3

from hermes_cli.kanban_db import SCHEMA_SQL
from tools import plan_tool


class _State:
    def __init__(self, task_id: str):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, task_id TEXT, subject TEXT)")
        self.conn.execute("INSERT INTO sessions VALUES ('session', ?, '')", (task_id,))
        self.ratings = {}
        self.moods = []

    @contextmanager
    def _read_ctx(self):
        yield self.conn

    def set_session_task_id(self, session_id, task_id):
        self.conn.execute("UPDATE sessions SET task_id = ? WHERE id = ?", (task_id, session_id))
        self.conn.commit()

    def clear_session_task_id(self, session_id):
        self.set_session_task_id(session_id, None)

    def set_session_subject(self, session_id, subject):
        self.conn.execute("UPDATE sessions SET subject = ? WHERE id = ?", (subject, session_id))
        self.conn.commit()

    def update_agent_rating(self, agent_name, delta):
        self.ratings[agent_name] = self.ratings.get(agent_name, 20.0) + delta
        return self.ratings[agent_name]

    def set_session_mood(self, session_id, delta):
        self.moods.append((session_id, delta))


class _Agent:
    canonical_session_id = "session"
    agent_name = "tester"
    _session_temperature = 0.4

    def __init__(self):
        self.callback_calls = 0

    def clarify_callback(self, *_args, **_kwargs):
        self.callback_calls += 1
        return "Approve"


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def _insert_task(conn, task_id, *, assignee, plan_kind="normal", status="manual", debug_plan_id=None):
    conn.execute(
        """INSERT INTO tasks
           (id, title, status, assignee, created_at, task_steps, task_stepno,
            task_goal, board, plan_kind, debug_plan_id)
           VALUES (?, ?, ?, ?, 1, '[\"test\"]', 1, 'goal', 'default', ?, ?)""",
        (task_id, task_id, status, assignee, plan_kind, debug_plan_id),
    )
    conn.commit()


def _bind(monkeypatch, conn, state):
    monkeypatch.setattr(plan_tool, "_get_kanban_db", lambda board=None: conn)
    monkeypatch.setattr(plan_tool, "_get_session_db", lambda: state)


def test_preapproved_debug_plan_skips_callback_and_links_coding_task(monkeypatch):
    conn = _db()
    _insert_task(conn, "coding", assignee="coder", status="running")
    state = _State("coding")
    agent = _Agent()
    _bind(monkeypatch, conn, state)

    result = plan_tool._cmd_new(
        agent, "debug", "exercise coding task", ["run smoke test"],
        kind="debug", pre_approved=True,
    )

    match = re.search(r"TASK APPROVED \((t_[0-9a-f]+)\)", result)
    assert match is not None
    task_id = match.group(1)
    debug = conn.execute(
        "SELECT status, plan_kind, pre_approved FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    source = conn.execute("SELECT debug_plan_id FROM tasks WHERE id = 'coding'").fetchone()
    assert agent.callback_calls == 0
    assert tuple(debug) == ("manual", "debug", 1)
    assert source["debug_plan_id"] == task_id


def test_debug_plan_completion_rewards_coder_not_tester(monkeypatch):
    conn = _db()
    _insert_task(conn, "coding", assignee="coder", status="blocked", debug_plan_id="debug")
    _insert_task(conn, "debug", assignee="tester", plan_kind="debug")
    conn.execute("UPDATE tasks SET previous_task = 'coding' WHERE id = 'debug'")
    conn.commit()
    state = _State("debug")
    agent = _Agent()
    _bind(monkeypatch, conn, state)

    plan_tool._cmd_done(agent)

    assert state.ratings == {"coder": 20.5}
    assert conn.execute("SELECT status FROM tasks WHERE id = 'debug'").fetchone()[0] == "done"
    assert state.moods == []


def test_debug_failure_uses_severity_adjusted_tester_rating(monkeypatch):
    conn = _db()
    _insert_task(conn, "coding", assignee="coder", status="blocked", debug_plan_id="debug")
    _insert_task(conn, "debug", assignee="tester", plan_kind="debug")
    state = _State("debug")
    agent = _Agent()
    _bind(monkeypatch, conn, state)

    plan_tool._cmd_fail(agent, "easy bug: 0.8")

    assert state.ratings == {"coder": 19.5, "tester": 20.25}
    assert conn.execute("SELECT status FROM tasks WHERE id = 'debug'").fetchone()[0] == "archived"
    report = conn.execute("SELECT body FROM task_comments WHERE task_id = 'coding'").fetchone()[0]
    assert "easy bug: 0.8" in report


def test_debug_crash_penalty_uses_flat_rating_delta(monkeypatch):
    conn = _db()
    _insert_task(conn, "coding", assignee="coder", status="blocked", debug_plan_id="debug")
    _insert_task(conn, "debug", assignee="tester", plan_kind="debug")
    state = _State("debug")
    agent = _Agent()
    _bind(monkeypatch, conn, state)

    plan_tool._cmd_fail(agent, json.dumps([{"bug": "process crashed", "kind": "crash", "severity": 1.0}]))

    assert state.ratings == {"coder": 19.0, "tester": 20.25}


def test_normal_failure_archives_without_rating_change(monkeypatch):
    conn = _db()
    _insert_task(conn, "normal", assignee="coder")
    state = _State("normal")
    agent = _Agent()
    _bind(monkeypatch, conn, state)

    plan_tool._cmd_fail(agent, "test failed")

    assert state.ratings == {}
    assert conn.execute("SELECT status FROM tasks WHERE id = 'normal'").fetchone()[0] == "archived"
    assert state.moods == [("session", -1.0)]


def test_test_complete_archives_without_mood_or_rating_penalty(monkeypatch):
    conn = _db()
    _insert_task(conn, "test", assignee="tester")
    state = _State("test")
    agent = _Agent()
    _bind(monkeypatch, conn, state)

    result = plan_tool.plan_tool(agent, "test-complete")

    assert "test-complete" in plan_tool.PLAN_TOOL_SCHEMA["parameters"]["properties"]["command"]["enum"]
    assert "recorded as a test outcome" in result
    assert state.ratings == {}
    assert state.moods == []
    assert state.conn.execute("SELECT task_id FROM sessions WHERE id = 'session'").fetchone()[0] is None
    assert conn.execute("SELECT status FROM tasks WHERE id = 'test'").fetchone()[0] == "archived"
    comment = conn.execute("SELECT body FROM task_comments WHERE task_id = 'test'").fetchone()[0]
    event = conn.execute("SELECT kind, payload FROM task_events WHERE task_id = 'test'").fetchone()
    assert comment.startswith("TEST COMPLETE at Step 1")
    assert event["kind"] == "test-complete"
    assert json.loads(event["payload"]) == {"outcome": "test", "step": 1}
