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


class _FlexibleSessionDB:
    def get_compression_root(self, session_id: str) -> str:
        return f"root:{session_id}"


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


def test_new_approval_question_contains_full_plan(monkeypatch):
    conn = _db()
    monkeypatch.setattr(plan_tool, "_get_kanban_db", lambda board=None: conn)
    seen = {}

    def approve(question, **_kwargs):
        seen["question"] = question
        return '{"user_response":"Approve"}'

    monkeypatch.setattr(plan_tool, "clarify_tool", approve)

    plan_tool.plan_tool(
        _Agent(),
        "new",
        title="Visible plan",
        goal="Keep the full plan visible",
        steps=["Inspect the prompt", "Render every step"],
    )

    assert "## Plan: Visible plan" in seen["question"]
    assert "**Goal:** Keep the full plan visible" in seen["question"]
    assert "1. Inspect the prompt" in seen["question"]
    assert "2. Render every step" in seen["question"]


def test_approve_question_contains_full_plan(monkeypatch):
    conn = _db()
    monkeypatch.setattr(plan_tool, "_get_kanban_db", lambda board=None: conn)
    monkeypatch.setattr(
        plan_tool, "clarify_tool", lambda *_args, **_kwargs: '{"user_response":""}'
    )
    agent = _Agent()
    plan_tool.plan_tool(
        agent,
        "new",
        title="Blocked plan",
        goal="Show the blocked plan",
        steps=["Load the goal", "Load all steps"],
    )
    task_id = conn.execute("SELECT id FROM tasks").fetchone()[0]
    seen = {}

    def approve(question, _choices):
        seen["question"] = question
        return "Approve"

    agent.clarify_callback = approve

    plan_tool.plan_tool(agent, "approve", task_id=task_id)

    assert "## Plan: Blocked plan" in seen["question"]
    assert "**Goal:** Show the blocked plan" in seen["question"]
    assert "1. Load the goal" in seen["question"]
    assert "2. Load all steps" in seen["question"]


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


def test_advance_closes_binding_and_records_required_summary(monkeypatch):
    conn = _db()
    monkeypatch.setattr(plan_tool, "_get_kanban_db", lambda board=None: conn)
    monkeypatch.setattr(
        plan_tool, "clarify_tool", lambda *_args, **_kwargs: '{"user_response":"Approve"}'
    )
    agent = _Agent()
    result = plan_tool.plan_tool(agent, "new", title="Finish", goal="finish", steps=["one"])
    task_id = conn.execute("SELECT task_id FROM execution_bindings").fetchone()[0]

    missing = plan_tool.plan_tool(agent, "advance")
    advanced = plan_tool.plan_tool(agent, "advance", summary="Finished the only step")

    assert missing == "ERROR: 'advance' requires summary"
    assert "goal was: finish" in advanced
    assert conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()[0] == "done"
    assert conn.execute("SELECT COUNT(*) FROM execution_bindings").fetchone()[0] == 0
    body = conn.execute(
        "SELECT body FROM task_comments WHERE task_id=? "
        "AND body LIKE '[plan-step-summary:%'",
        (task_id,),
    ).fetchone()[0]
    assert body == "[plan-step-summary:1] Finished the only step"


def test_handoff_replaces_current_step_summary_without_advancing(monkeypatch):
    conn = _db()
    monkeypatch.setattr(plan_tool, "_get_kanban_db", lambda board=None: conn)
    monkeypatch.setattr(
        plan_tool, "clarify_tool", lambda *_args, **_kwargs: '{"user_response":"Approve"}'
    )
    agent = _Agent()
    plan_tool.plan_tool(
        agent, "new", title="Handoff", goal="resume safely", steps=["first", "second"]
    )
    task_id = conn.execute("SELECT task_id FROM execution_bindings").fetchone()[0]

    assert plan_tool.plan_tool(agent, "handoff") == "ERROR: 'handoff' requires summary"
    plan_tool.plan_tool(agent, "handoff", summary="Initial scratch note")
    result = plan_tool.plan_tool(agent, "handoff", summary="Latest scratch note")

    assert "Latest scratch note" in result
    assert conn.execute(
        "SELECT task_stepno FROM tasks WHERE id=?", (task_id,)
    ).fetchone()[0] == 1
    rows = conn.execute(
        "SELECT body FROM task_comments WHERE task_id=? "
        "AND body LIKE '[plan-step-summary:%'",
        (task_id,),
    ).fetchall()
    assert [row[0] for row in rows] == [
        "[plan-step-summary:1] Latest scratch note"
    ]


def test_remind_never_labels_an_explicit_task_historical(monkeypatch):
    conn = _db()
    monkeypatch.setattr(plan_tool, "_get_kanban_db", lambda board=None: conn)
    monkeypatch.setattr(
        plan_tool, "clarify_tool", lambda *_args, **_kwargs: '{"user_response":"Approve"}'
    )
    agent = _Agent()
    plan_tool.plan_tool(
        agent, "new", title="Work in progress", goal="finish it", steps=["keep working"]
    )
    task_id = conn.execute("SELECT task_id FROM execution_bindings").fetchone()[0]

    result = plan_tool.plan_tool(agent, "remind", task_id=task_id)

    assert "Task: Work in progress" in result
    assert "historical" not in result.casefold()


def test_remind_shows_only_goal_and_active_step(monkeypatch):
    """remind must not leak the other steps (Evan, 2026-09-14).

    A small model that sees every step loses track of which one it is on and
    starts working on the wrong one, so remind shows exactly one step and then
    points at `advance`.
    """
    conn = _db()
    monkeypatch.setattr(plan_tool, "_get_kanban_db", lambda board=None: conn)
    monkeypatch.setattr(
        plan_tool, "clarify_tool", lambda *_args, **_kwargs: '{"user_response":"Approve"}'
    )
    agent = _Agent()
    plan_tool.plan_tool(
        agent,
        "new",
        title="Two-step plan",
        goal="ship it",
        steps=["first do this", "then do that"],
    )
    task_id = conn.execute("SELECT task_id FROM execution_bindings").fetchone()[0]

    result = plan_tool.plan_tool(agent, "remind", task_id=task_id)

    assert "ship it" in result
    assert "first do this" in result
    assert "then do that" not in result
    assert "advance" in result


def test_continue_still_shows_every_step(monkeypatch):
    """`continue` keeps the full listing — it restores first-turn context."""
    conn = _db()
    monkeypatch.setattr(plan_tool, "_get_kanban_db", lambda board=None: conn)
    monkeypatch.setattr(
        plan_tool, "clarify_tool", lambda *_args, **_kwargs: '{"user_response":"Approve"}'
    )
    agent = _Agent()
    plan_tool.plan_tool(
        agent,
        "new",
        title="Two-step plan",
        goal="ship it",
        steps=["first do this", "then do that"],
    )
    task_id = conn.execute("SELECT task_id FROM execution_bindings").fetchone()[0]

    result = plan_tool.plan_tool(agent, "continue", task_id=task_id)

    assert "first do this" in result
    assert "then do that" in result


def test_continue_rebinds_session_and_agent_and_returns_step_summaries(monkeypatch):
    conn = _db()
    monkeypatch.setattr(plan_tool, "_get_kanban_db", lambda board=None: conn)
    monkeypatch.setattr(
        plan_tool, "clarify_tool", lambda *_args, **_kwargs: '{"user_response":"Approve"}'
    )
    original = _Agent()
    plan_tool.plan_tool(
        original, "new", title="Portable plan", goal="cross sessions", steps=["first", "second"]
    )
    task_id = conn.execute("SELECT task_id FROM execution_bindings").fetchone()[0]
    plan_tool.plan_tool(original, "handoff", summary="Partial first step")
    plan_tool.plan_tool(original, "advance", summary="Completed first step in tools/a.py")
    plan_tool.plan_tool(original, "handoff", summary="Started second step in tests/test_a.py")
    resumed = SimpleNamespace(
        profile_name="Ornith",
        agent_name="ornith",
        session_id="new-session",
        canonical_session_id="ignored",
        _session_db=_FlexibleSessionDB(),
        _session_temperature=None,
    )

    result = plan_tool.plan_tool(resumed, "continue", task_id=task_id)

    assert "Task: Portable plan" in result
    assert "Summary: Completed first step in tools/a.py" in result
    assert "Summary: Started second step in tests/test_a.py" in result
    assert "→ Step 2: second" in result
    bindings = conn.execute(
        "SELECT profile, root_session_id, task_id FROM execution_bindings"
    ).fetchall()
    assert [tuple(row) for row in bindings] == [
        ("ornith", "root:new-session", task_id)
    ]
    task = conn.execute(
        "SELECT assignee, session_id, task_stepno FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    assert tuple(task) == ("ornith", "new-session", 2)


def test_active_plan_compression_context_is_read_only_and_summary_aware(monkeypatch):
    from tools import plan_binding_adapter

    conn = _db()
    monkeypatch.setattr(plan_tool, "_get_kanban_db", lambda board=None: conn)
    monkeypatch.setattr(
        plan_tool, "clarify_tool", lambda *_args, **_kwargs: '{"user_response":"Approve"}'
    )
    agent = _Agent()
    plan_tool.plan_tool(
        agent,
        "new",
        title="Compression plan",
        goal="survive provider failure",
        steps=["inspect", "implement"],
    )
    task_id = conn.execute("SELECT task_id FROM execution_bindings").fetchone()[0]
    plan_tool.plan_tool(agent, "handoff", summary="Inspection complete")
    before = [tuple(row) for row in conn.execute("SELECT * FROM execution_bindings")]

    contexts = plan_binding_adapter.active_plan_compression_context(agent)

    assert contexts is not None
    full, minimal = contexts
    assert "Task: Compression plan" in full
    assert "Summary: Inspection complete" in full
    assert "Goal: survive provider failure" in minimal
    assert "Current step 1/2: inspect" in minimal
    assert "Summary: Inspection complete" in minimal
    assert [tuple(row) for row in conn.execute("SELECT * FROM execution_bindings")] == before
    assert conn.execute("SELECT task_stepno FROM tasks WHERE id=?", (task_id,)).fetchone()[0] == 1


def test_schema_replaces_done_and_status_with_advance_summary_handoff_continue():
    properties = plan_tool.PLAN_TOOL_SCHEMA["parameters"]["properties"]
    commands = properties["command"]["enum"]

    assert "done" not in commands
    assert "status" not in properties
    assert {"advance", "handoff", "continue"}.issubset(commands)
    assert "files" in properties["summary"]["description"].lower()
    assert "advance" in properties["summary"]["description"]
    assert "handoff" in properties["summary"]["description"]
    assert "Unknown plan command 'done'" in plan_tool.plan_tool(_Agent(), "done")
