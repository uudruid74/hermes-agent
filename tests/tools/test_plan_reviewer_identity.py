"""Reviewer identity must ride every worker-facing plan payload (t_cc94fc2d).

2026-10-22 incident t_c02fc2cd: ``plan continue`` never named the step
reviewer, so the worker's "if you know who it is" SOUL fallback routed a
step handoff to Gopher — the wrong agent — while the delegate wake still
appended "use the 'tell' command to reply", which read as permission to
hand work over via raw ``tell`` instead of the plan review machinery.

Evan 2026-10-22 (t_cc94fc2d): "plan remind and plan continue need to
explicitly list the 'reviewer' by agent name AND sessionid" and "use
'tell' to reply" must be removed from plan continue/advance calls.

The session id must come from the plan binding (``plan_authorizations``),
never ``tasks.session_id``: claiming a plan rewrites that column to the
WORKER's session (execution_bindings claim path).  The incident row proves
it — tasks.session_id holds worker session 20261021_202440_b3b8141b while
the reviewer's session 20260906_194042_f0b3c774 lives in the binding.
"""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from hermes_cli import plan_authorizations as pa
from hermes_cli.kanban_db import SCHEMA_SQL
from tools import plan
from tools import plan_binding_adapter

REVIEWER_SESSION = "20260906_194042_f0b3c774"
WORKER_SESSION = "20261021_202440_b3b8141b"
REVIEWER_LINE = f"Reviewer: zephyr (session_id={REVIEWER_SESSION})"


def _fake_wake(monkeypatch, sink=None):
    """Fake BOTH wake transports into *sink*: the fire-and-forget dispatch
    spawn and the verified send run (_bridge_cli_wake checks the JSON
    result). The autouse hostage guard below holds the real transport."""
    import subprocess

    from hermes_cli import _subprocess_compat

    def _capture(argv, **_kw):
        if sink is not None:
            sink.append(list(argv))
        return SimpleNamespace(pid=1)

    class _SendProc:
        returncode = 0
        stdout = json.dumps({"delivered": True})
        stderr = ""

    def _run(argv, **_kw):
        if sink is not None:
            sink.append(list(argv))
        return _SendProc()

    monkeypatch.setattr(_subprocess_compat, "spawn_detached", _capture)
    monkeypatch.setattr(subprocess, "run", _run)


@pytest.fixture(autouse=True)
def _no_real_wake(monkeypatch):
    """Hostage guard (agent-wake-routing): fails loud on a real wake send."""

    def _guard(*_args, **_kwargs):
        raise AssertionError(
            "real 'hermes send' attempted — fake the wake transport in the test"
        )

    monkeypatch.setattr("subprocess.run", _guard)


class _SessionDB:
    def __init__(self):
        self.task_ids = []
        self.subjects = []

    def get_compression_root(self, session_id):
        return session_id  # strict lineage = the session itself

    def set_session_task_id(self, session_id, task_id):
        self.task_ids.append((session_id, task_id))

    def set_session_subject(self, session_id, subject):
        self.subjects.append((session_id, subject))

    def update_agent_rating(self, *_args):
        return None


class _Agent:
    def __init__(self, name, session_id):
        self.agent_name = name
        self.profile_name = name
        self.session_id = session_id
        self.canonical_session_id = session_id
        self._session_db = _SessionDB()
        self._session_temperature = None
        self._plan_approval_timed_out = None
        self.clarify_callback = lambda *_args, **_kwargs: "Approve"


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def _seed_plan(conn, task_id="t_reviewer1", body=""):
    """The incident's shape: zephyr's plan, ornith's task, worker not bound."""
    conn.execute(
        "INSERT INTO tasks (id, title, body, assignee, status, task_steps,"
        " task_stepno, task_goal, created_at, created_by)"
        " VALUES (?, 'Pente Run 4', ?, 'ornith', 'manual', ?, 1,"
        " 'pente new() idempotent', 0, 'zephyr')",
        (task_id, body, json.dumps(["init(): DB ready", "new_game + set_board"])),
    )
    pa.create_pending_plan(
        conn,
        pa.PlanRequest(
            plan_id=task_id,
            payload={
                "title": "Pente Run 4",
                "goal": "pente new() idempotent",
                "steps": ["init(): DB ready", "new_game + set_board"],
                "kind": "manual",
                "assign": "ornith",
                "root": None,
                "cron": None,
                "resume": None,
            },
            execution_task_id=task_id,
            execution_session_id=REVIEWER_SESSION,
            origin_session_id=REVIEWER_SESSION,
        ),
    )
    pa.resolve_plan_in_txn(
        conn,
        task_id,
        "approved",
        pa.ApprovalActor(session_id=REVIEWER_SESSION, actor="zephyr", via="plan"),
    )
    conn.execute(
        "INSERT INTO execution_bindings (profile, root_session_id, task_id,"
        " revision, bound_at, updated_at) VALUES ('ornith', ?, ?, 1, 0, 0)",
        (WORKER_SESSION, task_id),
    )
    conn.commit()
    return task_id


def test_remind_names_plan_owner_with_binding_session(monkeypatch):
    """Incident fix: remind names the reviewer by name AND session id."""
    conn = _db()
    task_id = _seed_plan(conn)
    monkeypatch.setattr(plan, "_get_kanban_db", lambda board=None: conn)

    out = plan_binding_adapter.cmd_remind(_Agent("ornith", WORKER_SESSION), task_id=task_id)

    assert REVIEWER_LINE in out
    # The claim path rewrites tasks.session_id to the WORKER's session —
    # that id must never be presented as the reviewer's.
    assert f"session_id={WORKER_SESSION}" not in out


def test_plan_text_reviewer_line_wins_and_pairs_its_session(monkeypatch):
    """A plan-file ``Reviewer:`` line names the reviewer over the owner."""
    conn = _db()
    task_id = _seed_plan(
        conn,
        body="**Producer:** Ornith · **Reviewer:** Zephyr\nTell transport only.",
    )
    monkeypatch.setattr(plan, "_get_kanban_db", lambda board=None: conn)

    out = plan_binding_adapter.cmd_remind(_Agent("ornith", WORKER_SESSION), task_id=task_id)

    # The plan text's spelling is authoritative; the session pairs via
    # casefold matching against the plan binding.
    assert f"Reviewer: Zephyr (session_id={REVIEWER_SESSION})" in out


def test_plan_text_reviewer_without_recorded_session_says_so(monkeypatch):
    """A body-named reviewer with no binding session is named, not invented."""
    conn = _db()
    task_id = _seed_plan(conn, body="Reviewer: Gopher\nTell transport only.")
    monkeypatch.setattr(plan, "_get_kanban_db", lambda board=None: conn)

    out = plan_binding_adapter.cmd_remind(_Agent("ornith", WORKER_SESSION), task_id=task_id)

    assert "Reviewer: Gopher (session_id not recorded)" in out
    assert "Reviewer: zephyr" not in out


def test_compression_contexts_keep_the_reviewer(monkeypatch):
    """The minimal format is what survives compaction — it must keep the line."""
    conn = _db()
    task_id = _seed_plan(conn)
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()

    full = plan_binding_adapter._format_plan(conn, task, include_summaries=False)
    minimal = plan_binding_adapter._format_plan(
        conn, task, include_summaries=False, minimal=True
    )

    assert REVIEWER_LINE in full
    assert REVIEWER_LINE in minimal


def test_delegate_wake_drops_tell_guidance_and_names_reviewer(monkeypatch):
    """The wake keeps Evan's dispatch text verbatim, drops the tell guidance,
    and carries the reviewer identity; `plan continue` repeats it."""
    conn = _db()
    sent: list[list[str]] = []

    monkeypatch.setattr(plan, "_get_kanban_db", lambda board=None: conn)
    monkeypatch.setattr(
        plan, "clarify_tool", lambda *_args, **_kwargs: '{"user_response":"Approve"}'
    )
    _fake_wake(monkeypatch, sent)

    creator = _Agent("zephyr", REVIEWER_SESSION)
    worker = _Agent("ornith", WORKER_SESSION)
    plan.plan(
        creator,
        "new",
        title="Pente Run 4",
        goal="pente new() idempotent",
        steps=["init(): DB ready", "new_game + set_board"],
        assignee="ornith",
    )
    task_id = conn.execute("SELECT id FROM tasks").fetchone()[0]

    wake = sent[-1][-1]
    assert "use the 'tell' command to reply" not in wake
    assert "'plan continue t_" in wake  # Evan's dispatch text, verbatim
    assert REVIEWER_LINE in wake

    continued = plan.plan(worker, "continue", task_id=task_id)
    assert REVIEWER_LINE in continued


# ---------------------------------------------------------------------------
# Sticky dispatcher routing (t_cc772a4c): plan approval and clarify routing
# follow the ASSIGNMENT.  "If an agent you dispatch makes a plan, YOU should
# approve it" and "when he is assigned to a plan, his dispatching agent
# sticks" (Evan, 2026-10-25).  The chain is recursive — no hardcoded names.
# ---------------------------------------------------------------------------

STAND_DOWN = (
    "User unavailable. Stand down and wait for the user to return. Do nothing else."
)


def _seed_dispatch_parent(conn, task_id="t_parent", board="pente"):
    """The incident's parent: zephyr dispatched ornith; origin is zephyr's
    session; ornith's binding points at it."""
    from hermes_cli.kanban_db import store_origin_routing

    conn.execute(
        "INSERT INTO tasks (id, title, body, assignee, status, task_steps,"
        " task_stepno, task_goal, created_at, created_by, board)"
        " VALUES (?, 'Pente tell transport', '', 'ornith', 'manual', ?, 1,"
        " 'pente tells', 0, 'zephyr', ?)",
        (task_id, json.dumps(["init", "build", "verify"]), board),
    )
    store_origin_routing(
        conn,
        task_id,
        platform="session",
        chat_id=REVIEWER_SESSION,
        profile="zephyr",
    )
    conn.execute(
        "INSERT INTO execution_bindings (profile, root_session_id, task_id,"
        " revision, bound_at, updated_at) VALUES ('ornith', ?, ?, 1, 0, 0)",
        (WORKER_SESSION, task_id),
    )
    conn.commit()
    return task_id


def test_plan_approval_routes_to_dispatcher_and_continue_gates_on_it(monkeypatch):
    """The incident, replayed: ornith's child plan must send the approval ask
    to its DISPATCHER, not to the human callback; `plan review` is the answer
    channel; and `plan continue` must refuse the plan while its authorization
    is still pending (the t_b085ce41 fail-open)."""
    conn = _db()
    _seed_dispatch_parent(conn)
    monkeypatch.setattr(plan, "_get_kanban_db", lambda board=None: conn)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True)
    wakes = []
    _fake_wake(monkeypatch, wakes)
    calls = []

    def interactive_stub(question, choices=None, **kwargs):
        calls.append((question, choices))
        return '{"user_response":"Approve"}'

    monkeypatch.setattr(plan, "clarify_tool", interactive_stub)

    worker = _Agent("ornith", WORKER_SESSION)
    out = plan.plan(
        worker,
        "new",
        title="list and leaderboard",
        goal="Implement the two remaining tell stubs",
        steps=["Implement the list tell", "Implement the leaderboard tell"],
    )
    assert out.startswith("Plan awaiting approval"), out
    task_id = out.split("Plan awaiting approval (", 1)[1].split(")", 1)[0]
    # The ask went to the sticky dispatcher — and the human was not consulted.
    assert "zephyr" in out
    assert calls == [], "the human clarify path must not run when a dispatcher resolves"
    event = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id=?"
        " ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    assert event["kind"] == "review-approval"
    assert (
        f"plan review {task_id} approved|denied|yolo"
        in json.loads(event["payload"])["text"]
    )
    assert wakes and "zephyr" in wakes[0], f"no dispatcher wake: {wakes}"
    row = conn.execute(
        "SELECT status, block_kind FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    assert (row[0], row[1]) == ("blocked", "approval")
    # D5: the gate must not fail open — pending authorization refuses continue.
    refused = plan_binding_adapter.cmd_continue(worker, task_id)
    assert refused.startswith("ERROR:"), refused
    assert "not approved" in refused
    # The dispatcher answers through `plan review` — the durable gate.
    reviewer = _Agent("zephyr", REVIEWER_SESSION)
    reply = plan_binding_adapter.cmd_review(reviewer, task_id, "approved")
    assert "approved" in reply.lower(), reply
    assert (
        conn.execute(
            "SELECT state FROM plan_authorizations WHERE plan_id=?", (task_id,)
        ).fetchone()[0]
        == "approved"
    )
    # With the authorization approved, the worker may continue (the
    # delegated 780f7f99cc behaviour is preserved).
    continued = plan_binding_adapter.cmd_continue(worker, task_id)
    assert not continued.startswith("ERROR:"), continued
    assert (
        conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
        == "manual"
    )


def test_self_created_top_level_plan_keeps_interactive_buttons(monkeypatch):
    """Self-dispatch is not a route: a worker's own top-level plan resolves NO
    agent dispatcher, and the interactive callback keeps the choices (the
    platforms render BUTTONS — Approve / Deny / Yolo)."""
    conn = _db()
    monkeypatch.setattr(plan, "_get_kanban_db", lambda board=None: conn)
    seen = {}

    def stub(question, choices=None, **kwargs):
        seen["choices"] = choices
        return '{"user_response":"Approve"}'

    monkeypatch.setattr(plan, "clarify_tool", stub)
    wakes = []
    _fake_wake(monkeypatch, wakes)
    worker = _Agent("ornith", WORKER_SESSION)
    out = plan.plan(worker, "new", title="My own plan", goal="g", steps=["one"])
    assert out.startswith("TASK APPROVED"), out
    assert seen["choices"] == ["Approve", "Deny", "Yolo"]
    assert not wakes


def test_child_plan_inherits_parent_board(monkeypatch):
    """D4 (t_cc772a4c): a child plan created with no explicit board lands on
    its PARENT's board, not 'default'."""
    conn = _db()
    _seed_dispatch_parent(conn, board="pente")
    monkeypatch.setattr(plan, "_get_kanban_db", lambda board=None: conn)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True)
    _fake_wake(monkeypatch)
    worker = _Agent("ornith", WORKER_SESSION)
    out = plan.plan(worker, "new", title="child plan", goal="g", steps=["one"])
    task_id = out.split("(", 1)[1].split(")", 1)[0]
    board = conn.execute(
        "SELECT board FROM tasks WHERE id=?", (task_id,)
    ).fetchone()[0]
    assert board == "pente"


def test_expired_review_ask_is_recorded_as_nothing(monkeypatch):
    """D6 (t_cc772a4c): an expired ask is a NON-answer — it must never be
    recorded as a review decision.  Before the guard the gateway's stand-down
    string was recorded as a DENIAL whose "reason" was the stand-down text
    itself (absence of evidence treated as evidence)."""
    conn = _db()
    monkeypatch.setattr(plan, "_get_kanban_db", lambda board=None: conn)
    responses = iter(
        [
            '{"user_response":"Approve"}',
            json.dumps({"user_response": STAND_DOWN}),
        ]
    )
    monkeypatch.setattr(plan, "clarify_tool", lambda *a, **k: next(responses))
    worker = _Agent("ornith", WORKER_SESSION)
    out = plan.plan(worker, "new", title="Self plan", goal="g", steps=["one", "two"])
    assert out.startswith("TASK APPROVED"), out
    task_id = out.split("(", 1)[1].split(")", 1)[0]
    advanced = plan_binding_adapter.cmd_advance(worker, summary="step one complete")
    assert "Review remains pending" in advanced, advanced
    kinds = [
        r[0]
        for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id=? AND kind IN"
            " ('plan-step-review-approved','plan-step-review-denied')",
            (task_id,),
        )
    ]
    assert kinds == [], f"a non-answer was recorded as a review decision: {kinds}"
    assert (
        conn.execute(
            "SELECT count(*) FROM task_events WHERE task_id=?"
            " AND kind='plan-step-review-pending'",
            (task_id,),
        ).fetchone()[0]
        == 1
    )


def test_denial_with_approval_shaped_reason_records_verbatim(monkeypatch):
    """Pin (e) (Evan, 2026-10-26): decision and reason are separate fields —
    a denial whose reason is approval-shaped prose is RECORDED verbatim with
    NO refusal. The deleted approval-token guard refused exactly this."""
    conn = _db()
    task_id = _seed_plan(conn)
    monkeypatch.setattr(plan, "_get_kanban_db", lambda board=None: conn)
    _fake_wake(monkeypatch)
    worker = _Agent("ornith", WORKER_SESSION)
    submitted = plan_binding_adapter.cmd_advance(worker, summary="step one done")
    assert "Pause and wait" in submitted, submitted
    reviewer = _Agent("zephyr", REVIEWER_SESSION)
    recorded = plan_binding_adapter.cmd_review(reviewer, task_id, "denied", "Approved")
    assert not recorded.startswith("ERROR:"), recorded
    assert "denied" in recorded.lower(), recorded
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=?"
        " AND kind='plan-step-review-denied'",
        (task_id,),
    ).fetchone()
    assert row is not None, "the denial must be recorded, not refused"
    assert json.loads(row[0])["reason"] == "Approved"
    body = conn.execute(
        "SELECT body FROM task_comments WHERE task_id=?"
        " AND body LIKE '[plan-step-denial:1] %'",
        (task_id,),
    ).fetchone()
    assert body[0] == "[plan-step-denial:1] Approved"
    # The log is append-only: the original pending event remains, but the
    # latest review event is the denial — the review is resolved.
    assert conn.execute(
        "SELECT count(*) FROM task_events WHERE task_id=?"
        " AND kind='plan-step-review-pending'",
        (task_id,),
    ).fetchone()[0] == 1


def test_approved_review_with_reason_is_refused(monkeypatch):
    """Pin (f) (Evan, 2026-10-26): approval needs no reason — an approved
    review carrying one is refused and nothing is recorded."""
    conn = _db()
    task_id = _seed_plan(conn)
    monkeypatch.setattr(plan, "_get_kanban_db", lambda board=None: conn)
    _fake_wake(monkeypatch)
    worker = _Agent("ornith", WORKER_SESSION)
    submitted = plan_binding_adapter.cmd_advance(worker, summary="step one done")
    assert "Pause and wait" in submitted, submitted
    reviewer = _Agent("zephyr", REVIEWER_SESSION)
    refused = plan_binding_adapter.cmd_review(
        reviewer, task_id, "approved", "looks good to me"
    )
    assert refused.startswith("ERROR:"), refused
    assert "approval needs no reason" in refused
    # The kernel enforces the same rule directly — no caller can write one.
    from hermes_cli import execution_bindings as bindings

    try:
        bindings.review_plan_step(
            conn,
            task_id=task_id,
            decision="approved",
            reviewer="zephyr",
            reason="looks good to me",
        )
        raise AssertionError("kernel must refuse a reason on an approved review")
    except ValueError as exc:
        assert "approval needs no reason" in str(exc)
    assert (
        conn.execute(
            "SELECT count(*) FROM task_events WHERE task_id=?"
            " AND kind IN ('plan-step-review-approved','plan-step-review-denied')",
            (task_id,),
        ).fetchone()[0]
        == 0
    )
    # The review is still pending — nothing was half-recorded.
    assert conn.execute(
        "SELECT count(*) FROM task_events WHERE task_id=?"
        " AND kind='plan-step-review-pending'",
        (task_id,),
    ).fetchone()[0] == 1


def test_child_close_restores_parked_parent_and_parent_advances(monkeypatch):
    """The addendum deadlock, replayed: the child's final step must be
    reviewable by the DISPATCHER (no human), the child's close must restore
    the parent's binding even though the parent is parked in ``attention``,
    and the parent must then advance toward its next step."""
    from hermes_cli import plan_authorizations as pa

    conn = _db()
    parent = _seed_dispatch_parent(conn)
    # Park the parent exactly like the live deadlock: attention at step 8 of 9.
    conn.execute(
        "UPDATE tasks SET status='attention', task_stepno=8, task_steps=? WHERE id=?",
        (json.dumps([f"step {i}" for i in range(1, 10)]), parent),
    )
    # The child: ornith's nested plan at its final step; the binding lives on
    # the child (revision 12 is the live shape).
    conn.execute(
        "INSERT INTO tasks (id, title, body, assignee, status, task_steps,"
        " task_stepno, task_goal, created_at, created_by, previous_task, board)"
        " VALUES ('t_child', 'list and leaderboard', '', 'ornith', 'manual', ?, 3,"
        " 'implement the tells', 0, 'ornith', ?, 'pente')",
        (json.dumps(["one", "two", "three"]), parent),
    )
    pa.create_pending_plan(
        conn,
        pa.PlanRequest(
            plan_id="t_child",
            payload={
                "title": "list and leaderboard",
                "goal": "implement the tells",
                "steps": ["one", "two", "three"],
                "kind": "manual",
                "assign": "ornith",
                "root": None,
                "cron": None,
                "resume": None,
            },
            execution_task_id="t_child",
            execution_session_id=WORKER_SESSION,
            parent_task_id=parent,
            origin_session_id=WORKER_SESSION,
        ),
    )
    pa.resolve_plan_in_txn(
        conn,
        "t_child",
        "approved",
        pa.ApprovalActor(session_id=REVIEWER_SESSION, actor="zephyr", via="plan"),
    )
    conn.execute(
        "UPDATE execution_bindings SET task_id='t_child', revision=12"
        " WHERE profile='ornith'"
    )
    conn.commit()
    monkeypatch.setattr(plan, "_get_kanban_db", lambda board=None: conn)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True)
    _fake_wake(monkeypatch)
    worker = _Agent("ornith", WORKER_SESSION)
    submitted = plan_binding_adapter.cmd_advance(worker, summary="step three done")
    assert "Pause and wait" in submitted, submitted
    # The DISPATCHER reviews the child's final step — the human is not needed.
    reply = plan_binding_adapter.cmd_review(
        _Agent("zephyr", REVIEWER_SESSION), "t_child", "approved"
    )
    assert "approved" in reply.lower(), reply
    assert (
        conn.execute("SELECT status FROM tasks WHERE id='t_child'").fetchone()[0]
        == "done"
    )
    # The parked parent's binding is RESTORED.
    assert (
        conn.execute(
            "SELECT task_id FROM execution_bindings WHERE profile='ornith'"
        ).fetchone()[0]
        == parent
    )
    assert (
        conn.execute("SELECT status FROM tasks WHERE id=?", (parent,)).fetchone()[0]
        == "manual"
    )
    # And the parent advances: step 8 submits, its review approves, step 9
    # becomes the active step.
    advanced = plan_binding_adapter.cmd_advance(worker, summary="step eight done")
    assert advanced.startswith("Step 8"), advanced
    approve = plan_binding_adapter.cmd_review(
        _Agent("zephyr", REVIEWER_SESSION), parent, "approved"
    )
    assert "approved" in approve.lower(), approve
    assert (
        conn.execute(
            "SELECT task_stepno FROM tasks WHERE id=?", (parent,)
        ).fetchone()[0]
        == 9
    )
