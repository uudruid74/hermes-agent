"""Dispatcher/user review gate for every Plan step advancement."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

from hermes_cli.kanban_db import SCHEMA_SQL
from tools import plan_tool


class _SessionDB:
    def __init__(self) -> None:
        self.task_ids: list[tuple[str, str | None]] = []
        self.subjects: list[tuple[str, str]] = []

    def get_compression_root(self, session_id: str) -> str:
        return f"root:{session_id}"

    def set_session_task_id(self, session_id: str, task_id: str | None) -> None:
        self.task_ids.append((session_id, task_id))

    def set_session_subject(self, session_id: str, subject: str) -> None:
        self.subjects.append((session_id, subject))

    def update_agent_rating(self, *_args) -> None:
        return None


class _Agent:
    def __init__(self, name: str, session_id: str) -> None:
        self.agent_name = name
        self.profile_name = name
        self.session_id = session_id
        self.canonical_session_id = session_id
        self._session_db = _SessionDB()
        self._session_temperature = None
        self._plan_approval_timed_out = None
        self.clarify_callback = lambda *_args, **_kwargs: "Approve"


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def _install(monkeypatch):
    from hermes_cli import _subprocess_compat

    conn = _db()
    sent: list[list[str]] = []

    def spawn(argv, **_kwargs):
        sent.append(list(argv))
        # Truthy handle = "spawned successfully"; None = spawn failure.
        return SimpleNamespace()

    monkeypatch.setattr(plan_tool, "_get_kanban_db", lambda board=None: conn)
    monkeypatch.setattr(_subprocess_compat, "spawn_detached", spawn)
    monkeypatch.setenv("USERNAME", "neo")
    return conn, sent


def _delegated_plan(monkeypatch, steps=("inspect", "implement")):
    conn, sent = _install(monkeypatch)
    monkeypatch.setattr(
        plan_tool, "clarify_tool", lambda *_args, **_kwargs: '{"user_response":"Approve"}'
    )
    creator = _Agent("neo", "20260923_010101_abcdef")
    worker = _Agent("ornith", "20260923_010102_abcdef")
    dispatcher = _Agent("neo", "20260923_010103_abcdef")
    plan_tool.plan_tool(
        creator,
        "new",
        title="Review gate",
        goal="Ship verified work",
        steps=list(steps),
        assignee="ornith",
    )
    task_id = conn.execute("SELECT id FROM tasks").fetchone()[0]
    assert plan_tool.plan_tool(worker, "continue", task_id=task_id).startswith("Task:")
    sent.clear()
    return conn, sent, worker, dispatcher, task_id


def _stepno(conn: sqlite3.Connection, task_id: str):
    return conn.execute(
        "SELECT task_stepno FROM tasks WHERE id=?", (task_id,)
    ).fetchone()[0]


def test_advance_records_one_summary_holds_step_and_notifies_dispatcher(monkeypatch):
    conn, sent, worker, _dispatcher, task_id = _delegated_plan(monkeypatch)

    result = plan_tool.plan_tool(
        worker, "advance", summary="Inspected parser and documented the failure", step=1
    )

    assert "pause" in result.lower()
    assert "wait for verification" in result.lower()
    assert _stepno(conn, task_id) == 1
    summaries = conn.execute(
        "SELECT body FROM task_comments WHERE task_id=? "
        "AND body LIKE '[plan-step-summary:1]%'",
        (task_id,),
    ).fetchall()
    assert [row[0] for row in summaries] == [
        "[plan-step-summary:1] Inspected parser and documented the failure"
    ]
    assert len(sent) == 1
    assert sent[0][:4] == ["hermes", "send", "-u", "neo"]
    notice = sent[0][4]
    assert f"ornith has completed step 1, task id {task_id}" in notice
    assert "Goal: Ship verified work" in notice
    assert "[-] Step 1: inspect" in notice
    assert "[ ] Step 2: implement" in notice
    assert "Summary:\nInspected parser and documented the failure" in notice
    assert "approve or deny this step using 'plan_tool review'" in notice
    assert conn.execute(
        "SELECT plan_auto_approve FROM tasks WHERE id=?", (task_id,)
    ).fetchone()[0] == 0


def test_second_advance_is_refused_while_review_is_pending(monkeypatch):
    conn, sent, worker, _dispatcher, task_id = _delegated_plan(monkeypatch)
    plan_tool.plan_tool(worker, "advance", summary="first summary", step=1)

    result = plan_tool.plan_tool(worker, "advance", summary="second summary", step=1)

    assert "already awaiting review" in result.lower()
    assert _stepno(conn, task_id) == 1
    assert len(sent) == 1


def test_approved_review_advances_and_notifies_worker(monkeypatch):
    conn, sent, worker, dispatcher, task_id = _delegated_plan(monkeypatch)
    plan_tool.plan_tool(worker, "advance", summary="inspection complete", step=1)
    sent.clear()

    result = plan_tool.plan_tool(
        dispatcher, "review", task_id=task_id, decision="approved"
    )

    assert result == f"Task {task_id} Step 1 approved. Worker notified."
    assert _stepno(conn, task_id) == 2
    assert len(sent) == 1
    assert sent[0][:4] == ["hermes", "send", "-u", "ornith"]
    message = sent[0][4]
    assert f"Task {task_id} Step 1 Approved by neo. Now complete Step 2" in message
    assert "Goal: Ship verified work" in message
    assert "Step 2: implement" in message
    assert "Please work this step and when this step is complete, use plan_tool to advance to the next" in message


def test_yolo_review_persists_and_auto_approves_later_steps(monkeypatch):
    """One dispatcher yolo keeps evidence + notices but removes later clicks."""
    from hermes_cli import execution_bindings as bindings

    conn, sent, worker, dispatcher, task_id = _delegated_plan(
        monkeypatch, steps=("inspect", "implement", "verify")
    )
    plan_tool.plan_tool(worker, "advance", summary="inspection complete", step=1)
    sent.clear()

    enabled = plan_tool.plan_tool(
        dispatcher, "review", task_id=task_id, decision="yolo"
    )

    assert enabled == (
        f"Task {task_id} Step 1 approved. YOLO enabled. Worker notified."
    )
    assert _stepno(conn, task_id) == 2
    assert conn.execute(
        "SELECT plan_auto_approve FROM tasks WHERE id=?", (task_id,)
    ).fetchone()[0] == 1
    assert len(sent) == 1
    assert sent[0][:4] == ["hermes", "send", "-u", "ornith"]

    sent.clear()
    advanced = plan_tool.plan_tool(
        worker, "advance", summary="implementation tested", step=2
    )

    assert advanced == f"Task {task_id} Step 2 auto-approved. Now complete Step 3"
    assert _stepno(conn, task_id) == 3
    assert bindings.pending_step_review(conn, task_id) is None
    assert len(sent) == 1
    assert sent[0][:4] == ["hermes", "send", "-u", "neo"]
    notice = sent[0][4]
    assert "ornith has completed step 2" in notice
    assert "YOLO auto-approved this step" in notice
    assert "Summary:\nimplementation tested" in notice

    sent.clear()
    completed = plan_tool.plan_tool(
        worker, "advance", summary="verification complete", step=3
    )

    assert completed == f"Task {task_id} Step 3 auto-approved. Plan complete."
    assert conn.execute(
        "SELECT status FROM tasks WHERE id=?", (task_id,)
    ).fetchone()[0] == "done"
    assert bindings.pending_step_review(conn, task_id) is None
    assert conn.execute("SELECT COUNT(*) FROM execution_bindings").fetchone()[0] == 0
    assert len(sent) == 1
    assert sent[0][:4] == ["hermes", "send", "-u", "neo"]
    assert "YOLO auto-approved this step" in sent[0][4]

    kinds = [
        row[0]
        for row in conn.execute(
            "SELECT kind FROM task_events WHERE task_id=? "
            "AND kind LIKE 'plan-step-review-%' ORDER BY id",
            (task_id,),
        )
    ]
    assert kinds == [
        "plan-step-review-pending",
        "plan-step-review-approved",
        "plan-step-review-pending",
        "plan-step-review-approved",
        "plan-step-review-pending",
        "plan-step-review-approved",
    ]


def test_denied_review_requires_bounded_reason_and_keeps_step_open(monkeypatch):
    conn, sent, worker, dispatcher, task_id = _delegated_plan(monkeypatch)
    plan_tool.plan_tool(worker, "advance", summary="inspection complete", step=1)
    sent.clear()

    missing = plan_tool.plan_tool(
        dispatcher, "review", task_id=task_id, decision="denied"
    )
    too_long = plan_tool.plan_tool(
        dispatcher, "review", task_id=task_id, decision="denied", reason="x" * 1025
    )
    denied = plan_tool.plan_tool(
        dispatcher,
        "review",
        task_id=task_id,
        decision="denied",
        reason="The failure case was not reproduced.",
    )

    assert missing == "ERROR: denied review requires reason"
    assert too_long == "ERROR: review reason must be at most 1024 characters"
    assert denied == f"Task {task_id} Step 1 denied. Worker notified."
    assert _stepno(conn, task_id) == 1
    message = sent[-1][4]
    assert "Advancement has been denied. Reason: The failure case was not reproduced." in message
    assert "Step 1: inspect" in message
    assert "Please correct before attempting to advance again" in message
    # Denial resolves the pending review, so a corrected summary may be submitted.
    again = plan_tool.plan_tool(worker, "advance", summary="reproduced and fixed", step=1)
    assert "wait for verification" in again.lower()


def test_review_rejects_non_dispatcher_and_stale_resolution(monkeypatch):
    conn, _sent, worker, dispatcher, task_id = _delegated_plan(monkeypatch)
    plan_tool.plan_tool(worker, "advance", summary="inspection complete", step=1)

    intruder = _Agent("zephyr", "20260923_010104_abcdef")
    unauthorized = plan_tool.plan_tool(
        intruder, "review", task_id=task_id, decision="approved"
    )
    approved = plan_tool.plan_tool(
        dispatcher, "review", task_id=task_id, decision="approved"
    )
    stale = plan_tool.plan_tool(
        dispatcher, "review", task_id=task_id, decision="approved"
    )

    assert "dispatcher neo" in unauthorized.lower()
    assert "approved" in approved.lower()
    assert "no pending review" in stale.lower()
    assert _stepno(conn, task_id) == 2


def test_final_step_waits_for_review_then_closes(monkeypatch):
    conn, sent, worker, dispatcher, task_id = _delegated_plan(
        monkeypatch, steps=("ship",)
    )

    pending = plan_tool.plan_tool(worker, "advance", summary="shipped", step=1)
    assert "wait for verification" in pending.lower()
    assert conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()[0] == "manual"

    sent.clear()
    result = plan_tool.plan_tool(
        dispatcher, "review", task_id=task_id, decision="approved"
    )

    assert result == f"Task {task_id} Step 1 approved. Plan complete. Worker notified."
    assert conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()[0] == "done"
    assert conn.execute("SELECT COUNT(*) FROM execution_bindings").fetchone()[0] == 0
    assert f"Task {task_id} Step 1 Approved by neo. Plan complete." in sent[0][4]


def test_self_created_plan_uses_clarify_approve_deny_gate(monkeypatch):
    conn, sent = _install(monkeypatch)
    questions: list[tuple[str, list[str] | None]] = []
    responses = iter(["Approve", "Approve"])

    def clarify(question, choices=None, **_kwargs):
        questions.append((question, choices))
        return json.dumps({"user_response": next(responses)})

    monkeypatch.setattr(plan_tool, "clarify_tool", clarify)
    worker = _Agent("ornith", "20260923_010105_abcdef")
    plan_tool.plan_tool(
        worker,
        "new",
        title="Self plan",
        goal="Use the human gate",
        steps=["inspect", "implement"],
    )
    task_id = conn.execute("SELECT id FROM tasks").fetchone()[0]

    result = plan_tool.plan_tool(worker, "advance", summary="inspection complete", step=1)

    assert "Approved" in result
    assert _stepno(conn, task_id) == 2
    assert questions[-1][1] == ["Approve", "Deny", "Approve and stop asking"]
    assert "ornith has completed step 1" in questions[-1][0]
    assert sent == []


def test_approve_and_stop_asking_enables_auto_approve(monkeypatch):
    """"Approve and stop asking" approves the step AND auto-approves the rest.

    The reviewer is the human here (self-created plan, no AI dispatcher), so the
    clarify gate is the only place the choice can be made — yolo is a property of
    the human's answer, never something an agent specifies.
    """
    conn, sent = _install(monkeypatch)
    questions: list[tuple[str, list[str] | None]] = []

    def clarify(question, choices=None, **_kwargs):
        questions.append((question, choices))
        return json.dumps({"user_response": "Approve and stop asking"})

    monkeypatch.setattr(plan_tool, "clarify_tool", clarify)
    worker = _Agent("ornith", "20260923_010107_abcdef")
    plan_tool.plan_tool(
        worker,
        "new",
        title="Self plan yolo",
        goal="Stop asking",
        steps=["inspect", "implement", "ship"],
    )
    task_id = conn.execute("SELECT id FROM tasks").fetchone()[0]

    result = plan_tool.plan_tool(worker, "advance", summary="inspection complete", step=1)

    assert "Approved" in result
    assert _stepno(conn, task_id) == 2
    row = conn.execute(
        "SELECT plan_auto_approve FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    assert row[0] == 1, "yolo answer must enable plan_auto_approve"
    assert questions[-1][1] == ["Approve", "Deny", "Approve and stop asking"]
    assert sent == []


def test_self_created_denial_prompts_for_reason(monkeypatch):
    conn, _sent = _install(monkeypatch)
    responses = iter(["Approve", "Deny", "Missing regression coverage"])
    questions: list[tuple[str, list[str] | None]] = []

    def clarify(question, choices=None, **_kwargs):
        questions.append((question, choices))
        return json.dumps({"user_response": next(responses)})

    monkeypatch.setattr(plan_tool, "clarify_tool", clarify)
    worker = _Agent("ornith", "20260923_010106_abcdef")
    plan_tool.plan_tool(
        worker, "new", title="Self plan", goal="Gate it", steps=["verify", "ship"]
    )
    task_id = conn.execute("SELECT id FROM tasks").fetchone()[0]

    result = plan_tool.plan_tool(worker, "advance", summary="claimed complete", step=1)

    assert "Advancement has been denied. Reason: Missing regression coverage" in result
    assert "Please correct before attempting to advance again" in result
    assert _stepno(conn, task_id) == 1
    assert questions[-1][1] is None


def test_manifest_exposes_review_and_only_one_advance_summary():
    properties = plan_tool.PLAN_TOOL_SCHEMA["parameters"]["properties"]
    commands = properties["command"]["enum"]

    assert "review" in commands
    assert "decision" in properties
    assert properties["decision"]["enum"] == ["approved", "denied", "yolo"]
    assert "summarize what was done" in properties["summary"]["description"].lower()
    assert "proof" not in properties
    assert "review" in properties["reason"]["description"].lower()


def test_dispatcher_resolution_prefers_durable_created_by_over_origin_routing(monkeypatch):
    """The dispatcher must come from tasks.created_by even when origin routing
    points elsewhere (e.g. a stale system comment from a prior creator)."""
    from hermes_cli.kanban_db import store_origin_routing
    from tools.plan_binding_adapter import _dispatcher_for_task

    conn, _sent, _worker, _dispatcher, task_id = _delegated_plan(monkeypatch)

    # Poison origin routing with a stale profile that is NOT the creator, to
    # prove created_by (neo) wins over a wrong/stale origin comment (zephyr).
    store_origin_routing(
        conn,
        task_id,
        platform="session",
        chat_id="20260923_020000_abcdef",
        profile="zephyr",
        overwrite=True,
    )

    assert _dispatcher_for_task(conn, task_id) == "neo"


def test_headless_self_plan_reports_blocked_no_reviewer(monkeypatch):
    """A self-created plan with no clarify callback must fail loudly, not hang."""
    conn, _sent = _install(monkeypatch)
    monkeypatch.setattr(
        plan_tool, "clarify_tool", lambda *_args, **_kwargs: '{"user_response":"Approve"}'
    )
    worker = _Agent("ornith", "20260923_020103_abcdef")

    # `new` approves + binds via the (mocked) clarify gate, so the plan is active.
    plan_tool.plan_tool(
        worker, "new", title="Headless", goal="No human available", steps=["inspect"]
    )
    task_id = conn.execute("SELECT id FROM tasks").fetchone()[0]

    # Now go headless at advance time: no dispatcher, no interactive user.
    worker.clarify_callback = None
    result = plan_tool.plan_tool(worker, "advance", summary="done", step=1)

    assert result.startswith("ERROR:")
    assert "no dispatcher" in result.lower()
    assert "no reviewer" in result.lower()
    # The step must still be held (blocked), never silently advanced.
    assert _stepno(conn, task_id) == 1
