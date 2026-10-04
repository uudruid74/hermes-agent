"""Invariants of the parked plan-task state (``attention``).

The park is the fix for 2026-10-03 ``t_9c63fac7``: a plan-bound task whose
turn ended at the tool-call limit landed in ``blocked``, and every downstream
hazard followed — ``unblock_task`` restored it to ``ready``, the dispatcher
claimed it, a second worker spawned on a machine that cannot hold one, and a
re-block routed it to ``triage`` where the plan-tool path is closed entirely.

These tests assert the invariants as *relations*, not status literals (Evan's
acceptance criteria): a parked plan task is neither claimable nor promotable,
restores to ``manual``, never accrues a recurrence, and its notification
carries the fixed summary string.
"""

from __future__ import annotations

import sqlite3

from hermes_cli import execution_bindings
from hermes_cli.kanban_db import (
    PLAN_PARK_STATUS,
    PLAN_PARK_SUMMARY,
    SCHEMA_SQL,
    claim_task,
    complete_task,
    create_task,
    get_task,
    park_plan_task,
    recompute_ready,
    unblock_task,
)


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def _plan_task(conn: sqlite3.Connection, *, steps: bool = False) -> str:
    """Create a task shaped like a bound manual plan.

    ``create_task`` normalizes ``initial_status='running'`` to ``ready``
    (unclaimed task), so the row is flipped to ``manual`` directly — the
    state ``plan_tool new`` leaves a bound plan in. ``steps=True`` also
    seeds a task_steps/task_goal pair so ``continue_plan``'s step validator
    accepts the row.
    """
    task_id = create_task(
        conn,
        title="Pente tell transport",
        body="plan body",
        assignee="ornith",
        created_by="gopher",
        initial_status="running",
    )
    if steps:
        import json as _json

        conn.execute(
            "UPDATE tasks SET task_steps = ?, task_stepno = 1, task_goal = ? "
            "WHERE id = ?",
            (_json.dumps(["work until complete"]), "finish the plan", task_id),
        )
    conn.execute("UPDATE tasks SET status = 'manual' WHERE id = ?", (task_id,))
    return task_id


def _parked_task(conn: sqlite3.Connection) -> str:
    task_id = _plan_task(conn)
    assert park_plan_task(
        conn,
        task_id,
        detail="Step 5 ended at the tool-call limit (200/200) with no submission",
    )
    return task_id


def test_park_lands_in_attention_not_blocked():
    conn = _db()
    task_id = _parked_task(conn)
    task = get_task(conn, task_id)
    # ``blocked`` is never entered at any point in the sequence.
    assert task.status == "attention"
    assert task.status != "blocked"
    assert task.block_kind is None
    # Parked means unclaimed: no worker, no live run pointer.
    assert task.worker_pid is None
    assert task.current_run_id is None


def test_park_never_counts_a_block_recurrence():
    conn = _db()
    task_id = _parked_task(conn)
    row = conn.execute(
        "SELECT block_recurrences, block_kind FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    # The loop breaker is unreachable for this class: no recurrence was
    # counted, so no amount of park/restore cycles can route to ``triage``.
    assert row["block_recurrences"] == 0
    assert row["block_kind"] is None


def test_parked_task_is_not_claimable():
    conn = _db()
    task_id = _parked_task(conn)
    # claim_task only transitions ``ready``; a parked task must not be
    # claimable no matter what the dispatcher does.
    assert claim_task(conn, task_id) is None
    assert get_task(conn, task_id).status == "attention"


def test_parked_task_is_not_promotable():
    conn = _db()
    task_id = _parked_task(conn)
    # recompute_ready promotes ``blocked``/``todo`` -> ``ready``; a parked
    # task must be invisible to it.
    assert recompute_ready(conn) == 0
    assert get_task(conn, task_id).status == "attention"


def test_unblock_restores_parked_task_to_manual_never_ready():
    conn = _db()
    task_id = _parked_task(conn)
    # The task has an assignee and no parents — the conditions that would
    # send a ``blocked`` task back to ``ready`` — and must still land in
    # ``manual``.
    assert unblock_task(conn, task_id) is True
    task = get_task(conn, task_id)
    assert task.status == "manual"
    assert task.status != "ready"
    # Recurrences stay 0 through park -> restore (never incremented).
    row = conn.execute(
        "SELECT block_recurrences FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    assert row["block_recurrences"] == 0


def test_park_preserves_detail_on_run_and_event():
    conn = _db()
    task_id = _parked_task(conn)
    detail = "Step 5 ended at the tool-call limit (200/200) with no submission"
    run = conn.execute(
        "SELECT status, outcome, summary FROM task_runs "
        "WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    assert run is not None
    # A manual plan task is never claimed, so the run is synthesized: its
    # status equals the outcome. The semantic content is the outcome plus
    # the preserved long detail.
    assert run["outcome"] == "blocked"
    assert run["summary"] == detail
    event = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    assert event["kind"] == "plan-parked"
    assert detail in event["payload"]
    # The fixed summary string lives in the event payload so the human (or
    # agent) notification line is greppable and assertable.
    assert PLAN_PARK_SUMMARY in event["payload"]
    assert PLAN_PARK_SUMMARY == "exceeded tool call limit"


def test_park_is_idempotent_safe_after_restore():
    conn = _db()
    task_id = _parked_task(conn)
    assert unblock_task(conn, task_id) is True
    # After restore the task is ``manual`` — parkable again on the next
    # turn-boundary exhaustion, still without counting a recurrence.
    assert park_plan_task(conn, task_id, detail="second boundary") is True
    task = get_task(conn, task_id)
    assert task.status == "attention"
    row = conn.execute(
        "SELECT block_recurrences FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    assert row["block_recurrences"] == 0


def test_park_refuses_non_parkable_states():
    conn = _db()
    task_id = _plan_task(conn)
    # Complete the task first: ``done`` is not a parkable state.
    # complete_task only accepts running/ready/blocked, so the row is flipped
    # to ``done`` directly instead.
    conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (task_id,))
    assert park_plan_task(conn, task_id, detail="late park") is False
    assert get_task(conn, task_id).status == "done"


def test_parked_task_does_not_resurrect_completed_run_pointer():
    conn = _db()
    task_id = _parked_task(conn)
    # The park closed the (synthesized) run; restore must not leave a
    # dangling pointer behind.
    assert unblock_task(conn, task_id) is True
    row = conn.execute(
        "SELECT current_run_id, worker_pid, claim_lock FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    assert row["current_run_id"] is None
    assert row["worker_pid"] is None
    assert row["claim_lock"] is None


def test_continue_plan_restores_parked_task_to_manual():
    conn = _db()
    task_id = _plan_task(conn, steps=True)
    assert park_plan_task(conn, task_id, detail="boundary") is True
    key = execution_bindings.ExecutionKey(
        profile="ornith", root_session_id="session-restore"
    )
    binding = execution_bindings.continue_plan(
        conn, key, task_id, actor="ornith", session_id="session-restore"
    )
    assert binding.task_id == task_id
    task = get_task(conn, task_id)
    # The rebind lands the task in ``manual`` — the dispatcher-excluded
    # active state — and advance's gate is no longer stranded.
    assert task.status == "manual"
    assert task.session_id == "session-restore"


def test_parked_notification_summary_is_the_fixed_string(monkeypatch):
    conn = _db()
    notified = []
    monkeypatch.setattr(
        "hermes_cli.kanban._notify_kanban_status_change",
        lambda task_id, status, **kwargs: notified.append((status, kwargs)),
    )
    task_id = _parked_task(conn)
    assert notified == [
        (
            "attention",
            {
                "summary": "exceeded tool call limit",
                "title": "Pente tell transport",
                "assignee": "ornith",
            },
        )
    ]


def test_park_event_not_a_block_event():
    conn = _db()
    task_id = _parked_task(conn)
    kinds = [
        r["kind"]
        for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ?", (task_id,)
        ).fetchall()
    ]
    assert "plan-parked" in kinds
    assert "blocked" not in kinds
    assert "block_loop_detected" not in kinds
