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

from hermes_cli import plan_authorizations as pa
from hermes_cli.kanban_db import SCHEMA_SQL
from tools import plan
from tools import plan_binding_adapter

REVIEWER_SESSION = "20260906_194042_f0b3c774"
WORKER_SESSION = "20261021_202440_b3b8141b"
REVIEWER_LINE = f"Reviewer: zephyr (session_id={REVIEWER_SESSION})"


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

    def spawn(argv, **_kwargs):
        sent.append(list(argv))
        return SimpleNamespace()

    monkeypatch.setattr(plan, "_get_kanban_db", lambda board=None: conn)
    monkeypatch.setattr(
        plan, "clarify_tool", lambda *_args, **_kwargs: '{"user_response":"Approve"}'
    )
    from hermes_cli import _subprocess_compat

    monkeypatch.setattr(_subprocess_compat, "spawn_detached", spawn)

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
