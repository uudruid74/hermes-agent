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


def test_new_nests_implicitly_under_the_active_plan(monkeypatch):
    """`new` while a Plan is active nests instead of refusing (Evan, 2026-09-15).

    The refusal was the bug: an active binding is the normal state, and the
    spec is that every Plan stores the previous Plan's task id in
    `previous_task` so closing the child restores the parent — nesting to
    any depth, no explicit parent_task_id required.
    """
    conn = _db()
    monkeypatch.setattr(plan_tool, "_get_kanban_db", lambda board=None: conn)
    monkeypatch.setattr(
        plan_tool, "clarify_tool", lambda *_args, **_kwargs: '{"user_response":"Approve"}'
    )
    agent = _Agent()

    first = plan_tool.plan_tool(agent, "new", title="Parent", goal="parent", steps=["one"])
    assert first.startswith("TASK APPROVED")
    parent_id = conn.execute("SELECT task_id FROM execution_bindings").fetchone()[0]

    child = plan_tool.plan_tool(
        agent, "new", title="Implicit child", goal="no parent arg", steps=["one"]
    )
    assert child.startswith("TASK APPROVED"), child
    child_id = conn.execute("SELECT task_id FROM execution_bindings").fetchone()[0]
    assert child_id != parent_id
    assert conn.execute(
        "SELECT previous_task FROM tasks WHERE id=?", (child_id,)
    ).fetchone()[0] == parent_id

    # the parent is suspended, not closed, so the chain can be walked back
    assert conn.execute(
        "SELECT status FROM tasks WHERE id=?", (parent_id,)
    ).fetchone()[0] == "manual"


def test_new_rejects_a_parent_that_is_not_the_active_plan(monkeypatch):
    conn = _db()
    monkeypatch.setattr(plan_tool, "_get_kanban_db", lambda board=None: conn)
    monkeypatch.setattr(
        plan_tool, "clarify_tool", lambda *_args, **_kwargs: '{"user_response":"Approve"}'
    )
    agent = _Agent()

    first = plan_tool.plan_tool(agent, "new", title="Parent", goal="parent", steps=["one"])
    assert first.startswith("TASK APPROVED")
    before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]

    blocked = plan_tool.plan_tool(
        agent, "new", title="Wrong parent", goal="no", steps=["one"],
        parent_task_id="t_doesnotexist",
    )
    assert blocked.startswith("PLAN_CONFLICT"), blocked
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before


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
    held = plan_tool.plan_tool(agent, "advance", summary="Finished the only step")
    advanced = plan_tool.plan_tool(
        agent, "advance", summary="Finished the only step", proof="pytest: 6 passed"
    )

    assert missing == "ERROR: 'advance' requires summary"
    assert "NOT ADVANCED" in held, "a bare claim must not close the plan"
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
    plan_tool.plan_tool(
        original,
        "advance",
        summary="Completed first step in tools/a.py",
        proof="commit 17258c7",
    )
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


# ---------------------------------------------------------------------------
# subject = full/heap compaction gate (Evan, 2026-09-18)
#
# The compressor reads `sessions.subject`; a change discards the Protected
# area (FULL compaction), an identical value keeps it (HEAP compaction, which
# leaves the cacheable prefix byte-stable).  So the subject has to track the
# Plan's ACTIVE STEP.  Before this, the four legacy writes lived only in the
# shadowed plan_tool helpers, so the live adapter path never set a subject at
# all and the gate could never fire.
# ---------------------------------------------------------------------------


class _SubjectAgent(_Agent):
    """_Agent with a subject-recording session DB."""

    def __init__(self):
        self.subjects: list = []
        outer = self

        class _DB:
            def get_compression_root(self, session_id: str) -> str:
                return f"root:{session_id}"

            def set_session_subject(self, session_id, subject) -> None:
                outer.subjects.append(subject)

        self._session_db = _DB()
        self.session_id = "subject-session"
        self.profile_name = "neo"
        self.agent_name = "neo"
        self.canonical_session_id = "subject-session"
        self._session_temperature = None
        self._plan_approval_timed_out = None

    def clarify_callback(self, *_args, **_kwargs) -> str:
        return "Approve"


def _start_plan(monkeypatch, agent, steps):
    conn = _db()
    monkeypatch.setattr(plan_tool, "_get_kanban_db", lambda board=None: conn)
    monkeypatch.setattr(
        plan_tool, "clarify_tool", lambda *_a, **_k: '{"user_response":"Approve"}'
    )
    result = plan_tool.plan_tool(
        agent, "new", title="Subject plan", goal="gate the compaction", steps=steps
    )
    assert result.startswith("TASK APPROVED (t_")
    return conn


def test_subject_tracks_the_active_step_and_changes_on_advance(monkeypatch):
    agent = _SubjectAgent()
    _start_plan(monkeypatch, agent, ["inspect", "implement", "verify"])

    # Activation named step 1.
    assert agent.subjects[-1] == "Subject plan: inspect"

    plan_tool.plan_tool(agent, "advance", summary="inspected", proof="ran it")
    assert agent.subjects[-1] == "Subject plan: implement"

    plan_tool.plan_tool(agent, "advance", summary="implemented", proof="ran it")
    assert agent.subjects[-1] == "Subject plan: verify"

    # Every transition wrote a DIFFERENT value: each is a full compaction.
    assert len(set(agent.subjects)) == len(agent.subjects)


def test_step_that_did_not_advance_leaves_the_subject_alone(monkeypatch):
    """Verify-first: a summary without proof must not fake a subject change.

    A no-op write would be harmless (same value = cache preserved), but a
    wrong value would trigger a needless full compaction, so nothing is
    written at all.
    """
    agent = _SubjectAgent()
    _start_plan(monkeypatch, agent, ["inspect", "implement"])
    assert agent.subjects[-1] == "Subject plan: inspect"

    result = plan_tool.plan_tool(agent, "advance", summary="claim only")

    assert "NOT ADVANCED" in result
    assert agent.subjects[-1] == "Subject plan: inspect"
    assert agent.subjects.count("Subject plan: implement") == 0


def test_closing_the_plan_clears_the_subject(monkeypatch):
    """No active Plan means no subject.

    A stale subject never changes again, so the Protected area would
    accumulate globals forever and never take a full compaction.
    """
    agent = _SubjectAgent()
    _start_plan(monkeypatch, agent, ["only step"])
    assert agent.subjects[-1] == "Subject plan: only step"

    plan_tool.plan_tool(agent, "advance", summary="finished", proof="ran it")

    assert agent.subjects[-1] == ""
