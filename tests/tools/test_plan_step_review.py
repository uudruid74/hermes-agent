"""Dispatcher/user review gate for every Plan step advancement."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

from hermes_cli.kanban_db import SCHEMA_SQL
from tools import plan


class _SessionDB:
    def __init__(self) -> None:
        self.task_ids: list[tuple[str, str | None]] = []
        self.subjects: list[tuple[str, str]] = []

    def get_compression_root(self, session_id: str) -> str:
        return f"root:{session_id}"

    def set_session_mood(self, session_id, delta):
        return 0.0

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

    monkeypatch.setattr(plan, "_get_kanban_db", lambda board=None: conn)
    monkeypatch.setattr(_subprocess_compat, "spawn_detached", spawn)
    monkeypatch.setenv("USERNAME", "neo")
    return conn, sent


def _delegated_plan(monkeypatch, steps=("inspect", "implement"), expansions=None):
    conn, sent = _install(monkeypatch)
    monkeypatch.setattr(
        plan, "clarify_tool", lambda *_args, **_kwargs: '{"user_response":"Approve"}'
    )
    creator = _Agent("neo", "20260923_010101_abcdef")
    worker = _Agent("ornith", "20260923_010102_abcdef")
    dispatcher = _Agent("neo", "20260923_010103_abcdef")
    plan.plan(
        creator,
        "new",
        title="Review gate",
        goal="Ship verified work",
        steps=list(steps),
        assignee="ornith",
    )
    task_id = conn.execute("SELECT id FROM tasks").fetchone()[0]
    if expansions is not None:
        # Fixture seed only: `plan new` carries no expansions (plan import is
        # the real writer of task_step_expansions — covered by its own tests).
        conn.execute(
            "UPDATE tasks SET task_step_expansions = ? WHERE id = ?",
            (json.dumps(list(expansions)), task_id),
        )
        conn.commit()
    assert plan.plan(worker, "continue", task_id=task_id).startswith("Task:")
    sent.clear()
    return conn, sent, worker, dispatcher, task_id


def _stepno(conn: sqlite3.Connection, task_id: str):
    return conn.execute(
        "SELECT task_stepno FROM tasks WHERE id=?", (task_id,)
    ).fetchone()[0]


def test_advance_records_one_summary_holds_step_and_notifies_dispatcher(monkeypatch):
    conn, sent, worker, _dispatcher, task_id = _delegated_plan(monkeypatch)

    result = plan.plan(
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
    assert sent[0][:4] == ["hermes", "send", "-t", "neo"]
    notice = sent[0][4]
    assert f"ornith has completed step 1, task id {task_id}" in notice
    assert "Goal: Ship verified work" in notice
    assert "[-] Step 1: inspect" in notice
    assert "[ ] Step 2: implement" in notice
    assert "Summary:\nInspected parser and documented the failure" in notice
    assert "approve or deny this step using 'plan review'" in notice
    assert conn.execute(
        "SELECT plan_auto_approve FROM tasks WHERE id=?", (task_id,)
    ).fetchone()[0] == 0


def test_repeat_advance_while_pending_renotifies_without_mutation(monkeypatch):
    """Evan 2026-09-29 (ornith t_e147304f): a repeat advance while a step
    review is pending is the worker's only escalation channel — it must
    re-notify the dispatcher, not refuse.  Idempotent: no second pending
    event, no step move, no revision bump, held summary untouched."""
    from hermes_cli import execution_bindings as bindings

    conn, sent, worker, _dispatcher, task_id = _delegated_plan(monkeypatch)
    first = plan.plan(worker, "advance", summary="first summary", step=1)
    assert "pause" in first.lower()
    assert len(sent) == 1
    revision_before = conn.execute(
        "SELECT revision FROM execution_bindings WHERE task_id=?", (task_id,)
    ).fetchone()[0]

    result = plan.plan(worker, "advance", summary="second summary", step=1)

    assert "pause" in result.lower()
    assert "wait for verification" in result.lower()
    assert "ERROR" not in result
    assert len(sent) == 2, "the repeat must re-send the review request"
    assert sent[1][:4] == ["hermes", "send", "-t", "neo"]
    assert sent[1][4] == sent[0][4], "the re-notify is the same review request"
    assert conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id=? "
        "AND kind='plan-step-review-pending'",
        (task_id,),
    ).fetchone()[0] == 1
    assert _stepno(conn, task_id) == 1
    revision_after = conn.execute(
        "SELECT revision FROM execution_bindings WHERE task_id=?", (task_id,)
    ).fetchone()[0]
    assert revision_after == revision_before, "a re-notify must not bump the revision"
    summaries = conn.execute(
        "SELECT body FROM task_comments WHERE task_id=? "
        "AND body LIKE '[plan-step-summary:1]%'",
        (task_id,),
    ).fetchall()
    assert [row[0] for row in summaries] == ["[plan-step-summary:1] first summary"], (
        "a repeat advance must not overwrite the held summary"
    )
    assert bindings.pending_step_review(conn, task_id).summary == "first summary"


def test_approved_review_advances_and_notifies_worker(monkeypatch):
    conn, sent, worker, dispatcher, task_id = _delegated_plan(monkeypatch)
    plan.plan(worker, "advance", summary="inspection complete", step=1)
    sent.clear()

    result = plan.plan(
        dispatcher, "review", task_id=task_id, decision="approved"
    )

    assert result == f"Task {task_id} Step 1 approved. Worker notified."
    assert _stepno(conn, task_id) == 2
    assert len(sent) == 1
    assert sent[0][:4] == ["hermes", "send", "-t", "ornith"]
    message = sent[0][4]
    assert f"Task {task_id} Step 1 Approved by neo. Now complete Step 2" in message
    assert "Goal: Ship verified work" in message
    assert "Step 2: implement" in message
    assert "Please work this step and when this step is complete, use plan to advance to the next" in message


def test_yolo_review_persists_and_auto_approves_later_steps(monkeypatch):
    """One dispatcher yolo keeps evidence + notices but removes later clicks."""
    from hermes_cli import execution_bindings as bindings

    conn, sent, worker, dispatcher, task_id = _delegated_plan(
        monkeypatch, steps=("inspect", "implement", "verify")
    )
    plan.plan(worker, "advance", summary="inspection complete", step=1)
    sent.clear()

    enabled = plan.plan(
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
    assert sent[0][:4] == ["hermes", "send", "-t", "ornith"]

    sent.clear()
    advanced = plan.plan(
        worker, "advance", summary="implementation tested", step=2
    )

    assert advanced == f"Task {task_id} Step 2 auto-approved. Now complete Step 3"
    assert _stepno(conn, task_id) == 3
    assert bindings.pending_step_review(conn, task_id) is None
    assert len(sent) == 1
    assert sent[0][:4] == ["hermes", "send", "-t", "neo"]
    notice = sent[0][4]
    assert "ornith has completed step 2" in notice
    assert "YOLO auto-approved this step" in notice
    assert "Summary:\nimplementation tested" in notice

    sent.clear()
    completed = plan.plan(
        worker, "advance", summary="verification complete", step=3
    )

    assert completed == f"Task {task_id} Step 3 auto-approved. Plan complete."
    assert conn.execute(
        "SELECT status FROM tasks WHERE id=?", (task_id,)
    ).fetchone()[0] == "done"
    assert bindings.pending_step_review(conn, task_id) is None
    assert conn.execute("SELECT COUNT(*) FROM execution_bindings").fetchone()[0] == 0
    assert len(sent) == 1
    assert sent[0][:4] == ["hermes", "send", "-t", "neo"]
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


def test_denied_review_requires_bounded_response_and_keeps_step_open(monkeypatch):
    conn, sent, worker, dispatcher, task_id = _delegated_plan(monkeypatch)
    plan.plan(worker, "advance", summary="inspection complete", step=1)
    sent.clear()

    missing = plan.plan(
        dispatcher, "review", task_id=task_id, decision="denied"
    )
    too_long = plan.plan(
        dispatcher, "review", task_id=task_id, decision="denied", response="x" * 1025
    )
    denied = plan.plan(
        dispatcher,
        "review",
        task_id=task_id,
        decision="denied",
        response="The failure case was not reproduced.",
    )

    assert missing == "ERROR: denied review requires a response"
    assert too_long == "ERROR: review response must be at most 1024 characters"
    assert denied == f"Task {task_id} Step 1 denied. Worker notified."
    assert _stepno(conn, task_id) == 1
    message = sent[-1][4]
    assert "Advancement has been denied. Reason: The failure case was not reproduced." in message
    assert "Step 1: inspect" in message
    assert "Please correct before attempting to advance again" in message
    # Denial resolves the pending review, so a corrected summary may be submitted.
    again = plan.plan(worker, "advance", summary="reproduced and fixed", step=1)
    assert "wait for verification" in again.lower()


def test_review_rejects_non_dispatcher_and_stale_resolution(monkeypatch):
    conn, _sent, worker, dispatcher, task_id = _delegated_plan(monkeypatch)
    plan.plan(worker, "advance", summary="inspection complete", step=1)

    intruder = _Agent("zephyr", "20260923_010104_abcdef")
    unauthorized = plan.plan(
        intruder, "review", task_id=task_id, decision="approved"
    )
    approved = plan.plan(
        dispatcher, "review", task_id=task_id, decision="approved"
    )
    stale = plan.plan(
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

    pending = plan.plan(worker, "advance", summary="shipped", step=1)
    assert "wait for verification" in pending.lower()
    assert conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()[0] == "manual"

    sent.clear()
    result = plan.plan(
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

    monkeypatch.setattr(plan, "clarify_tool", clarify)
    worker = _Agent("ornith", "20260923_010105_abcdef")
    plan.plan(
        worker,
        "new",
        title="Self plan",
        goal="Use the human gate",
        steps=["inspect", "implement"],
    )
    task_id = conn.execute("SELECT id FROM tasks").fetchone()[0]

    result = plan.plan(worker, "advance", summary="inspection complete", step=1)

    assert "Approved" in result
    assert _stepno(conn, task_id) == 2
    assert questions[-1][1] == ["Approve", "Deny", "Yolo"]
    assert "ornith has completed step 1" in questions[-1][0]
    assert sent == []


def test_yolo_answer_enables_auto_approve(monkeypatch):
    """"Yolo" approves the step AND auto-approves the rest.

    The reviewer is the human here (self-created plan, no AI dispatcher), so the
    clarify gate is the only place the choice can be made — yolo is a property of
    the human's answer, never something an agent specifies.
    """
    conn, sent = _install(monkeypatch)
    questions: list[tuple[str, list[str] | None]] = []

    def clarify(question, choices=None, **_kwargs):
        questions.append((question, choices))
        return json.dumps({"user_response": "Yolo"})

    monkeypatch.setattr(plan, "clarify_tool", clarify)
    worker = _Agent("ornith", "20260923_010107_abcdef")
    plan.plan(
        worker,
        "new",
        title="Self plan yolo",
        goal="Stop asking",
        steps=["inspect", "implement", "ship"],
    )
    task_id = conn.execute("SELECT id FROM tasks").fetchone()[0]

    result = plan.plan(worker, "advance", summary="inspection complete", step=1)

    assert "auto-approved" in result
    assert _stepno(conn, task_id) == 2
    row = conn.execute(
        "SELECT plan_auto_approve FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    assert row[0] == 1, "yolo answer must enable plan_auto_approve"
    assert questions[-1][1] == ["Approve", "Deny", "Yolo"]
    assert sent == []


def test_self_created_denial_prompts_for_response(monkeypatch):
    conn, _sent = _install(monkeypatch)
    responses = iter(["Approve", "Deny", "Missing regression coverage"])
    questions: list[tuple[str, list[str] | None]] = []

    def clarify(question, choices=None, **_kwargs):
        questions.append((question, choices))
        return json.dumps({"user_response": next(responses)})

    monkeypatch.setattr(plan, "clarify_tool", clarify)
    worker = _Agent("ornith", "20260923_010106_abcdef")
    plan.plan(
        worker, "new", title="Self plan", goal="Gate it", steps=["verify", "ship"]
    )
    task_id = conn.execute("SELECT id FROM tasks").fetchone()[0]

    result = plan.plan(worker, "advance", summary="claimed complete", step=1)

    assert "Advancement has been denied. Reason: Missing regression coverage" in result
    assert "Please correct before attempting to advance again" in result
    assert _stepno(conn, task_id) == 1
    assert questions[-1][1] is None


def test_manifest_exposes_review_and_only_one_advance_summary():
    properties = plan.PLAN_SCHEMA["parameters"]["properties"]
    commands = properties["command"]["enum"]

    assert "review" in commands
    assert "decision" in properties
    assert properties["decision"]["enum"] == ["approved", "denied", "yolo"]
    assert "summarize what was done" in properties["summary"]["description"].lower()
    assert "proof" not in properties
    assert "review" in properties["response"]["description"].lower()


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
        plan, "clarify_tool", lambda *_args, **_kwargs: '{"user_response":"Approve"}'
    )
    worker = _Agent("ornith", "20260923_020103_abcdef")

    # `new` approves + binds via the (mocked) clarify gate, so the plan is active.
    plan.plan(
        worker, "new", title="Headless", goal="No human available", steps=["inspect"]
    )
    task_id = conn.execute("SELECT id FROM tasks").fetchone()[0]

    # Now go headless at advance time: no dispatcher, no interactive user.
    worker.clarify_callback = None
    result = plan.plan(worker, "advance", summary="done", step=1)

    assert result.startswith("ERROR:")
    assert "no dispatcher" in result.lower()
    assert "no reviewer" in result.lower()
    # The step must still be held (blocked), never silently advanced.
    assert _stepno(conn, task_id) == 1


# ------------------------------------- review without task_id (Evan 2026-10-24)


def test_review_without_task_id_gates_the_active_plan(monkeypatch):
    """Acceptance a: with an ACTIVE binding and no task_id, review gates the
    active plan's current step — never "requires task_id". (A flash-tier
    model dropped task_id seven times running; omission must be harmless
    when the caller holds the plan. Explicit task_id stays exactly as pinned
    by test_approved_review_advances_and_notifies_worker.)"""
    conn, sent, worker, dispatcher, task_id = _delegated_plan(monkeypatch)
    plan.plan(worker, "advance", summary="inspection complete", step=1)
    # The contract precondition: the reviewer holds the plan's binding while
    # remaining its dispatcher — bound via the worker-bootstrap lineage API
    # so the delegated assignee (and the dispatcher gate) stays intact.
    from hermes_cli import execution_bindings as bindings

    bindings.bootstrap_worker_binding(
        conn, bindings.identity_for_agent(dispatcher), task_id
    )
    sent.clear()

    result = plan.plan(dispatcher, "review", decision="approved")

    assert result == f"Task {task_id} Step 1 approved. Worker notified."
    assert _stepno(conn, task_id) == 2
    assert len(sent) == 1 and sent[0][:4] == ["hermes", "send", "-t", "ornith"]


def test_review_without_task_id_or_binding_is_actionable(monkeypatch):
    """Acceptance c: no binding and no task_id fails with a clear, actionable
    message — names task_id and the claim command, not the old opaque
    "requires task_id"."""
    conn, _sent, worker, dispatcher, task_id = _delegated_plan(monkeypatch)
    plan.plan(worker, "advance", summary="inspection complete", step=1)

    result = plan.plan(dispatcher, "review", decision="approved")

    assert result.startswith("ERROR:"), result
    assert "task_id" in result
    assert "continue" in result.lower()
    assert "requires task_id" not in result
    # Nothing was gated: the step is still held.
    assert _stepno(conn, task_id) == 1


def test_plan_schema_descriptions_never_mix_required_and_optional():
    """Schema-description class (one layer before the hand-maintained
    whitelist): a property whose own description reads "optional" near the
    command that needs it made a flash-tier model drop `task_id` for
    'review' seven times (Evan 2026-10-24). One description must not carry
    both "required for" and "optional for" — split the per-command rules."""
    from tools.plan import PLAN_SCHEMA

    props = (PLAN_SCHEMA.get("parameters") or {}).get("properties") or {}
    assert props, "PLAN_SCHEMA lost its properties"
    for name, prop in props.items():
        desc = str(prop.get("description", "")).lower()
        assert not (
            "required for" in desc and "optional for" in desc
        ), f"{name}: description mixes 'required for' + 'optional for'"


# ── Step expansions ride the review/advance notification ─────────────────
# Evan 2026-10-25: "He should not need to call remind after advance. The
# advance notification should show the expanded step text." Expansions are
# appended VERBATIM under the step title; a plan without expansions must
# produce byte-identical messages to the pre-expansion format.


def _review_approved_message(monkeypatch, *, expansions=None):
    conn, sent, worker, dispatcher, task_id = _delegated_plan(
        monkeypatch, expansions=expansions
    )
    plan.plan(worker, "advance", summary="inspection complete", step=1)
    sent.clear()
    plan.plan(dispatcher, "review", task_id=task_id, decision="approved")
    assert len(sent) == 1
    return task_id, sent[0][4]


def test_approved_message_without_expansions_is_byte_identical(monkeypatch):
    task_id, message = _review_approved_message(monkeypatch)
    assert message == (
        f"Task {task_id} Step 1 Approved by neo. Now complete Step 2\n\n"
        "Goal: Ship verified work\n"
        "Step 2: implement\n\n"
        "Please work this step and when this step is complete, use plan "
        "to advance to the next"
    )


def test_approved_message_carries_next_step_expansion_verbatim(monkeypatch):
    spec = (
        "Implement `advance(summary: str) -> str`. Returns "
        '"Step N summary submitted. Pause and wait for verification." '
        "DB: task_comments rows '[plan-step-summary:N] <summary>'. "
        "Error branch: empty summary returns 'ERROR: summary required'. "
        "Acceptance: summary row survives re-import."
    )
    task_id, message = _review_approved_message(
        monkeypatch, expansions=["do the inspect thing", spec]
    )
    assert "Step 2: implement" in message
    assert spec in message  # verbatim — no compression, no elision
    # Under the title: the spec block follows the Step 2 line directly.
    assert f"Step 2: implement\n\n{spec}\n\nPlease work this step" in message
    assert "[…" not in message  # never middle-elided into the message


def test_denied_message_carries_reviewed_step_expansion(monkeypatch):
    spec = 'Inspect the parser. Report "SLOW PATH". Acceptance: findings list.'
    conn, sent, worker, dispatcher, task_id = _delegated_plan(
        monkeypatch, expansions=[spec, "implement spec"]
    )
    plan.plan(worker, "advance", summary="inspection complete", step=1)
    sent.clear()
    plan.plan(
        dispatcher, "review", task_id=task_id, decision="denied", response="redo"
    )
    assert len(sent) == 1
    message = sent[0][4]
    assert message.startswith("Advancement has been denied. Reason: redo\n\n")
    assert f"Step 1: inspect\n\n{spec}\n\nPlease correct" in message
    assert "implement spec" not in message  # only the reviewed step


def test_attention_request_carries_active_step_expansion(monkeypatch):
    # The tool-limit message (_attention_request_text): the active step's
    # spec under its listing line, other steps untouched.
    from tools.plan_binding_adapter import _attention_request_text

    spec = "Step 9 spec: build `score(board) -> int`. Returns 0 or 1."
    conn, _sent, _worker, _dispatcher, task_id = _delegated_plan(
        monkeypatch,
        steps=("first", "second", "third"),
        expansions=["", spec, ""],
    )
    conn.execute("UPDATE tasks SET task_stepno = 2 WHERE id = ?", (task_id,))
    conn.commit()
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    message = _attention_request_text(task, task, used=200, maximum=200)
    assert "[X] Step 1: first\n[-] Step 2: second" in message
    assert f"[-] Step 2: second\n\n{spec}\n[ ] Step 3: third" in message
    assert "first\n\n" not in message  # no spec under inactive steps


def _null_stepno(conn, task_id):
    """Put a plan in the fail->continue / reviewer-reset state: task_stepno
    NULL, which _steps_for_task documents as 'fresh/unstarted -> Step 1'."""
    conn.execute("UPDATE tasks SET task_stepno = NULL WHERE id = ?", (task_id,))
    conn.commit()


def test_review_approve_on_null_stepno_advances_to_step_2(monkeypatch):
    """Regression (bug doc 2026-10-25): the review CAS compared
    ``task_stepno = 1`` and ``NULL = 1`` is never true in SQL, so every
    review of a fail->continue plan raised 'plan ... changed during review'
    forever. The read path treats NULL as Step 1; the guard must agree."""
    conn, _sent, worker, dispatcher, task_id = _delegated_plan(monkeypatch)
    _null_stepno(conn, task_id)
    result = plan.plan(worker, "advance", summary="step one done", step=1)
    assert not result.startswith("ERROR:"), result
    result = plan.plan(dispatcher, "review", task_id=task_id, decision="approved")
    assert not result.startswith("ERROR:"), result
    row = conn.execute(
        "SELECT task_stepno FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    assert row["task_stepno"] == 2


def test_review_deny_on_null_stepno_reopens_step_1(monkeypatch):
    conn, _sent, worker, dispatcher, task_id = _delegated_plan(monkeypatch)
    _null_stepno(conn, task_id)
    plan.plan(worker, "advance", summary="step one done", step=1)
    result = plan.plan(
        dispatcher,
        "review",
        task_id=task_id,
        decision="denied",
        response="add the missing test",
    )
    assert not result.startswith("ERROR:"), result
    row = conn.execute(
        "SELECT body FROM task_comments WHERE task_id=? "
        "AND body LIKE '[plan-step-denial:1]%'",
        (task_id,),
    ).fetchone()
    assert row is not None and "add the missing test" in row["body"]
    # Denied leaves the same step active; NULL still reads as Step 1.
    row = conn.execute(
        "SELECT task_stepno FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    assert row["task_stepno"] in (None, 1)


def test_close_on_null_stepno_does_not_raise(monkeypatch):
    """Same guard shape in _close_plan_in_txn: a fail->continue plan reaches
    close with task_stepno NULL and must close cleanly."""
    conn, _sent, worker, _dispatcher, task_id = _delegated_plan(monkeypatch)
    _null_stepno(conn, task_id)
    result = plan.plan(worker, "fail", reason="cannot proceed")
    assert not result.startswith("ERROR:"), result
    row = conn.execute(
        "SELECT status, task_stepno FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    assert row["status"] == "archived"
    assert row["task_stepno"] is None


def test_summary_reaches_the_reviewer_whole(monkeypatch):
    """Evan 2026-10-25: the reviewer reads the WHOLE summary.

    The old middle-cut stored 240-char holes while the full text only
    survived in task_events payloads (run 7: 10/10 summaries mutilated) —
    verdicts and token measurements were both taken against cut text.
    """
    conn, sent, worker, _dispatcher, task_id = _delegated_plan(monkeypatch)
    middle = "UNIQUE-MIDDLE-SENTINEL-xyzzy"
    summary = "Head of the report. " + middle + " tail conclusion."
    result = plan.plan(worker, "advance", summary=summary, step=1)
    assert not result.startswith("ERROR:"), result
    row = conn.execute(
        "SELECT body FROM task_comments WHERE task_id=? "
        "AND body LIKE '[plan-step-summary:1]%'",
        (task_id,),
    ).fetchone()
    assert row is not None
    assert middle in row["body"], "the stored summary must keep its middle"
    assert "omitted" not in row["body"], "no elide marker — summaries are never cut"


def test_summary_over_the_limit_is_refused_not_truncated(monkeypatch):
    """Evan 2026-10-25: 1024-char limit at the tool call — DENY if longer.

    A hard refusal (the text_limit_error pattern), never a silent cut:
    over-limit input stores nothing and the worker is told the limit.
    """
    conn, _sent, worker, _dispatcher, task_id = _delegated_plan(monkeypatch)
    result = plan.plan(worker, "advance", summary="x" * 1025, step=1)
    assert result.startswith("ERROR:"), result
    assert "1024" in result
    assert "never truncated" in result
    row = conn.execute(
        "SELECT body FROM task_comments WHERE task_id=? "
        "AND body LIKE '[plan-step-summary:%'",
        (task_id,),
    ).fetchone()
    assert row is None, "an over-limit summary must store NOTHING"
