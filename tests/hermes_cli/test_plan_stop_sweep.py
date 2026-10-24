"""Detection sweep for stalled plan-bound workers (dispatcher tick).

Acceptance (Evan 2026-10-24, Zephyr's spec): a plan-bound worker that stops
text-only mid-step must be nudged exactly once per step by the sweep — and
the recorded nudge must never let the sweep loop.
"""

from __future__ import annotations

import json
import sqlite3
import time

from hermes_cli import execution_bindings as bindings
from hermes_cli import kanban_db as kb


def _insert_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    status: str = "manual",
    steps: tuple[str, ...] = ("first", "second"),
    step_no: int = 1,
) -> None:
    conn.execute(
        """
        INSERT INTO tasks (
            id, title, status, assignee, created_at, task_steps, task_stepno,
            task_goal, board
        ) VALUES (?, ?, ?, 'ornith', 1, ?, ?, 'goal', 'default')
        """,
        (task_id, task_id, status, json.dumps(list(steps)), step_no),
    )
    conn.commit()


def _key() -> bindings.ExecutionKey:
    return bindings.ExecutionKey(
        profile="ornith", root_session_id="root-session"
    )


def _bind_step_open(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    step_no: int = 1,
    opened_at: int | None = None,
) -> int:
    """Bootstrap the binding and pin the step-open clock; returns that clock."""
    bindings.bootstrap_worker_binding(conn, _key(), task_id)
    if step_no != 1:
        conn.execute(
            "UPDATE tasks SET task_stepno = ? WHERE id = ?", (step_no, task_id)
        )
    if opened_at is None:
        opened_at = int(time.time())
    conn.execute(
        "UPDATE execution_bindings SET updated_at = ? WHERE task_id = ?",
        (opened_at, task_id),
    )
    conn.commit()
    return opened_at


def _make_state_db(tmp_path, *, last_finish_reason="stop", family=False):
    """A fake worker state.db whose newest message carries the given reason.

    ``family=True`` puts the rows in a compression child of the root session
    (the lineage shape the probe must follow).
    """
    db = tmp_path / "state.db"
    state = sqlite3.connect(str(db))
    state.execute(
        "CREATE TABLE sessions ("
        " id TEXT PRIMARY KEY, parent_session_id TEXT, end_reason TEXT)"
    )
    state.execute(
        "CREATE TABLE messages ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,"
        " role TEXT, content TEXT, finish_reason TEXT)"
    )
    if family:
        state.execute(
            "INSERT INTO sessions VALUES ('root-session', NULL, 'compression')"
        )
        state.execute(
            "INSERT INTO sessions VALUES ('tip', 'root-session', NULL)"
        )
        state.execute(
            "INSERT INTO messages (session_id, role, content, finish_reason)"
            " VALUES ('tip', 'assistant', 'text', ?)",
            (last_finish_reason,),
        )
    else:
        state.execute(
            "INSERT INTO sessions VALUES ('root-session', NULL, NULL)"
        )
        state.execute(
            "INSERT INTO messages (session_id, role, content, finish_reason)"
            " VALUES ('root-session', 'assistant', 'text', ?)",
            (last_finish_reason,),
        )
    state.commit()
    state.close()
    return db


def _sweep(conn, db_path, *, now, threshold_seconds=60, emitted=None):
    emitted = [] if emitted is None else emitted

    def _capture(_conn, task_id, profile, text):
        emitted.append((task_id, profile, text))

    nudged = bindings.sweep_stalled_plan_workers(
        conn,
        threshold_seconds=threshold_seconds,
        now=now,
        emit_fn=_capture,
        state_db_path_fn=lambda _profile: db_path,
    )
    return nudged, emitted


def test_sweep_fires_once_per_step_and_never_repeats(tmp_path):
    # Acceptance 3 (sweep half): stalled step → one nudge, recorded; the
    # next sweep tick must not fire again for the same step.
    with kb.connect(tmp_path / "kanban.db") as conn:
        _insert_task(conn, "t_plan")
        opened_at = _bind_step_open(conn, "t_plan")
        db_path = _make_state_db(tmp_path)

        nudged, emitted = _sweep(conn, db_path, now=opened_at + 3600)
        assert nudged == ["t_plan"]
        assert len(emitted) == 1
        task_id, profile, text = emitted[0]
        assert task_id == "t_plan"
        assert profile == "ornith"
        assert "plan advance" in text
        assert "t_plan" in text

        nudged2, emitted2 = _sweep(
            conn, db_path, now=opened_at + 3660, emitted=emitted
        )
        assert nudged2 == []
        assert len(emitted) == 1  # exactly one nudge per (task, step)

        # The record is durable in task_events.
        row = conn.execute(
            "SELECT kind, payload FROM task_events"
            " WHERE kind = ? AND task_id = 't_plan'",
            (bindings.PLAN_STOP_NUDGE_KIND,),
        ).fetchone()
        assert row is not None
        assert json.loads(row[1])["step_no"] == 1


def test_sweep_nudges_again_on_the_next_step(tmp_path):
    with kb.connect(tmp_path / "kanban.db") as conn:
        _insert_task(conn, "t_plan")
        opened_at = _bind_step_open(conn, "t_plan")
        db_path = _make_state_db(tmp_path)
        nudged, emitted = _sweep(conn, db_path, now=opened_at + 3600)
        assert nudged == ["t_plan"]

        # The plan advanced to step 2 and stalled again — one more nudge.
        # The new step's clock sits after the step-1 nudge record so only the
        # per-(task, step) bound can distinguish the two.
        opened_at2 = _bind_step_open(
            conn, "t_plan", step_no=2, opened_at=opened_at + 7200
        )
        nudged2, emitted2 = _sweep(
            conn, db_path, now=opened_at2 + 3600, emitted=emitted
        )
        assert nudged2 == ["t_plan"]
        # One nudge per (task, step): two stalled steps → two nudges total.
        assert len(emitted2) == 2
        assert "step 2" in emitted2[-1][2]


def test_sweep_skips_sign_of_life(tmp_path):
    # A task_events row newer than the step-open clock is a sign of life.
    with kb.connect(tmp_path / "kanban.db") as conn:
        _insert_task(conn, "t_plan")
        opened_at = _bind_step_open(conn, "t_plan")
        db_path = _make_state_db(tmp_path)
        conn.execute(
            "INSERT INTO task_events (task_id, kind, created_at)"
            " VALUES ('t_plan', 'kanban-heartbeat', ?)",
            (opened_at + 10,),
        )
        conn.commit()

        nudged, emitted = _sweep(conn, db_path, now=opened_at + 3600)
        assert nudged == []
        assert emitted == []


def test_sweep_skips_live_worker(tmp_path):
    # The worker's newest message did not end text-only — it is mid-work.
    with kb.connect(tmp_path / "kanban.db") as conn:
        _insert_task(conn, "t_plan")
        opened_at = _bind_step_open(conn, "t_plan")
        db_path = _make_state_db(tmp_path, last_finish_reason="tool_calls")

        nudged, emitted = _sweep(conn, db_path, now=opened_at + 3600)
        assert nudged == []
        assert emitted == []


def test_sweep_skips_below_threshold(tmp_path):
    with kb.connect(tmp_path / "kanban.db") as conn:
        _insert_task(conn, "t_plan")
        opened_at = _bind_step_open(conn, "t_plan")
        db_path = _make_state_db(tmp_path)

        nudged, emitted = _sweep(
            conn, db_path, now=opened_at + 30, threshold_seconds=60
        )
        assert nudged == []
        assert emitted == []


def test_sweep_skips_pending_step_review(tmp_path):
    # The worker submitted its step — the ball is with the human reviewer.
    with kb.connect(tmp_path / "kanban.db") as conn:
        _insert_task(conn, "t_plan")
        current = bindings.bootstrap_worker_binding(conn, _key(), "t_plan")
        db_path = _make_state_db(tmp_path)
        bindings.advance_plan(
            conn,
            _key(),
            expected_task_id="t_plan",
            expected_revision=current.revision,
            summary="step one done",
            actor="ornith",
        )
        opened_at = int(time.time())

        nudged, emitted = _sweep(conn, db_path, now=opened_at + 3600)
        assert nudged == []
        assert emitted == []


def test_sweep_follows_compression_family_for_stop_check(tmp_path):
    # The stop signal lives in the compression child of the root session.
    with kb.connect(tmp_path / "kanban.db") as conn:
        _insert_task(conn, "t_plan")
        opened_at = _bind_step_open(conn, "t_plan")
        db_path = _make_state_db(tmp_path, family=True)

        nudged, emitted = _sweep(conn, db_path, now=opened_at + 3600)
        assert nudged == ["t_plan"]
        assert len(emitted) == 1


def test_sweep_missing_state_db_is_fail_safe(tmp_path):
    with kb.connect(tmp_path / "kanban.db") as conn:
        _insert_task(conn, "t_plan")
        opened_at = _bind_step_open(conn, "t_plan")

        nudged, emitted = _sweep(
            conn, tmp_path / "nope.db", now=opened_at + 3600
        )
        assert nudged == []
        assert emitted == []
