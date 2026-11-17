"""Cell 7 — plan stop-guard / step-nudge wake => outcome truth.

CONTRACT.md cell 7 (L1 outcome truth, L2 n/a — the nudge starts a turn in the
worker's own session, which is L2-covered by cell 4's harness).

The detection logic (when a stall exists, one nudge per (task, step), the
top-level skip, the sign-of-life skip) is pinned in
tests/hermes_cli/test_plan_stop_sweep.py — this file does NOT repeat it. What
it locks is the seam's *outcome truthfulness*: the sweep used to fire
``spawn_detached(["hermes", "send", ...])`` and count the nudge as fired on
process LAUNCH alone, so a wake that never reached the worker was
indistinguishable from a delivered one. Law 1: dropped is never success.

Covered here:
- ``_emit_plan_stop_nudge`` reports the verified wake result (True/False),
  and resolves the target at use time via ``wake_target_for``.
- a DROPPED nudge is not counted in the sweep's returned ``nudged`` list.
- a DROPPED nudge is recorded durably as ``delivered: false`` — never as a
  silent success — and still honours the one-per-(task, step) loop guard.
- a DELIVERED nudge is recorded as ``delivered: true``.
- no test may reach a real ``hermes send`` (hostage guard).
"""

from __future__ import annotations

import json
import sqlite3
import subprocess as _real_subprocess
import time

import pytest

from hermes_cli import execution_bindings as bindings
from hermes_cli import kanban_db as kb


@pytest.fixture(autouse=True)
def _no_live_wake(monkeypatch):
    """Hostage guard: any real wake send fails loud instead of waking an agent."""

    def blocked(*_a, **_kw):
        raise AssertionError(
            "wake test tried to run a real subprocess — fake the wake "
            "transport (cron.scheduler._bridge_cli_wake) before invoking a "
            "wake path"
        )

    monkeypatch.setattr(_real_subprocess, "run", blocked)


# ---------------------------------------------------------------------------
# Fixtures / helpers (mirroring tests/hermes_cli/test_plan_stop_sweep.py)
# ---------------------------------------------------------------------------


def _insert_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    status: str = "manual",
    steps: tuple[str, ...] = ("first", "second"),
    step_no: int = 1,
    assignee: str = "ornith",
    created_by: str | None = "wintermute",
) -> None:
    conn.execute(
        """
        INSERT INTO tasks (
            id, title, status, assignee, created_at, task_steps, task_stepno,
            task_goal, board, created_by
        ) VALUES (?, ?, ?, ?, 1, ?, ?, 'goal', 'default', ?)
        """,
        (
            task_id,
            task_id,
            status,
            assignee,
            json.dumps(list(steps)),
            step_no,
            created_by,
        ),
    )
    conn.commit()


def _key(profile: str = "ornith") -> bindings.ExecutionKey:
    return bindings.ExecutionKey(profile=profile, root_session_id="root-session")


def _bind_step_open(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    step_no: int = 1,
    opened_at: int | None = None,
    profile: str = "ornith",
) -> int:
    bindings.bootstrap_worker_binding(conn, _key(profile), task_id)
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


def _make_state_db(tmp_path, *, last_finish_reason="stop"):
    """A worker state.db whose newest message ended with the given reason."""
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
    state.execute("INSERT INTO sessions VALUES ('root-session', NULL, NULL)")
    state.execute(
        "INSERT INTO messages (session_id, role, content, finish_reason)"
        " VALUES ('root-session', 'assistant', 'text', ?)",
        (last_finish_reason,),
    )
    state.commit()
    state.close()
    return db


def _sweep(conn, db_path, *, now, threshold_seconds=60, emitted=None, emit=None):
    emitted = [] if emitted is None else emitted

    def _capture(_conn, task_id, profile, text):
        emitted.append((task_id, profile, text))
        # A capture stub returns None: "assume delivered" (legacy shape).

    nudged = bindings.sweep_stalled_plan_workers(
        conn,
        threshold_seconds=threshold_seconds,
        now=now,
        emit_fn=emit if emit is not None else _capture,
        state_db_path_fn=lambda _profile: db_path,
    )
    return nudged, emitted


def _nudge_event(conn, task_id):
    return conn.execute(
        "SELECT kind, payload FROM task_events"
        " WHERE kind = ? AND task_id = ?",
        (bindings.PLAN_STOP_NUDGE_KIND, task_id),
    ).fetchone()


# ---------------------------------------------------------------------------
# _emit_plan_stop_nudge — verified delivery, use-time target resolution
# ---------------------------------------------------------------------------


def test_emit_reports_verified_wake_result(monkeypatch, tmp_path):
    """The emitter is the verified ``_bridge_cli_wake`` contract, not a spawn."""
    calls = []
    monkeypatch.setattr(
        "cron.scheduler._bridge_cli_wake",
        lambda target, body, **kw: (
            calls.append((target, body)) or True
        ),
    )
    monkeypatch.setattr(
        kb, "wake_target_for", lambda _conn, task_id, profile: f"{profile}:cli:live"
    )

    conn = kb.connect(tmp_path / "kanban.db")
    try:
        ok = bindings._emit_plan_stop_nudge(conn, "t_plan", "ornith", "please act")
    finally:
        conn.close()

    assert ok is True
    assert calls == [("ornith:cli:live", "please act")]


def test_emit_reports_drop_as_false(monkeypatch, tmp_path):
    """A wake the transport could not confirm is False — never a false green."""
    monkeypatch.setattr(
        "cron.scheduler._bridge_cli_wake", lambda target, body, **kw: False
    )
    monkeypatch.setattr(kb, "wake_target_for", lambda _c, _t, p: p)

    conn = kb.connect(tmp_path / "kanban.db")
    try:
        assert (
            bindings._emit_plan_stop_nudge(conn, "t_plan", "ornith", "x") is False
        )
    finally:
        conn.close()


def test_emit_transport_crash_is_a_drop(monkeypatch, tmp_path):
    """A transport crash must not escape as an exception (the tick would die)."""

    def boom(*_a, **_kw):
        raise OSError("hermes binary gone")

    monkeypatch.setattr("cron.scheduler._bridge_cli_wake", boom)
    monkeypatch.setattr(kb, "wake_target_for", lambda _c, _t, p: p)

    conn = kb.connect(tmp_path / "kanban.db")
    try:
        assert (
            bindings._emit_plan_stop_nudge(conn, "t_plan", "ornith", "x") is False
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Sweep outcome truth — a dropped nudge is not reported as a nudge
# ---------------------------------------------------------------------------


def test_dropped_nudge_is_not_counted_as_nudged(tmp_path):
    """Law 1: the sweep must not report a nudge the worker never received."""
    with kb.connect(tmp_path / "kanban.db") as conn:
        _insert_task(conn, "t_plan")
        opened_at = _bind_step_open(conn, "t_plan")
        db_path = _make_state_db(tmp_path)

        dropped = []
        nudged, emitted = _sweep(
            conn,
            db_path,
            now=opened_at + 3600,
            emit=lambda _c, tid, _p, _t: dropped.append(tid) or False,
        )

        assert dropped == ["t_plan"], "the emit was attempted"
        assert nudged == [], "a dropped wake is never reported as a nudge"


def test_dropped_nudge_is_recorded_honestly_and_cannot_loop(tmp_path):
    """A drop is durable evidence (``delivered: false``) and still honours the
    one-per-(task, step) bound — the loop guard is not sacrificed to honesty."""
    with kb.connect(tmp_path / "kanban.db") as conn:
        _insert_task(conn, "t_plan")
        opened_at = _bind_step_open(conn, "t_plan")
        db_path = _make_state_db(tmp_path)
        attempts: list[str] = []

        def always_drops(_c, tid, _p, _t):
            attempts.append(tid)
            return False

        _sweep(conn, db_path, now=opened_at + 3600, emit=always_drops)

        row = _nudge_event(conn, "t_plan")
        assert row is not None, "the event row is the loop guard — it stays"
        payload = json.loads(row[1])
        assert payload["step_no"] == 1
        assert payload["delivered"] is False, "a drop must never look delivered"

        # A later tick does not re-fire the same (task, step).
        nudged2, _ = _sweep(
            conn, db_path, now=opened_at + 7200, emit=always_drops
        )
        assert nudged2 == []
        assert attempts == ["t_plan"], "exactly one attempt per (task, step)"


def test_delivered_nudge_is_recorded_as_delivered(tmp_path):
    """The success path stamps ``delivered: true`` so readers can trust it."""
    with kb.connect(tmp_path / "kanban.db") as conn:
        _insert_task(conn, "t_plan")
        opened_at = _bind_step_open(conn, "t_plan")
        db_path = _make_state_db(tmp_path)

        fired: list[str] = []

        def deliver(_c, tid, _p, _t):
            fired.append(tid)
            return True

        nudged, _ = _sweep(conn, db_path, now=opened_at + 3600, emit=deliver)

        assert nudged == ["t_plan"]
        assert fired == ["t_plan"], "the emit was attempted exactly once"
        payload = json.loads(_nudge_event(conn, "t_plan")[1])
        assert payload["delivered"] is True


def test_legacy_emitter_without_return_still_counts(tmp_path):
    """A stub/legacy emitter returning None keeps the pre-existing behaviour —
    only an explicit False is a drop, so no existing caller changes meaning."""
    with kb.connect(tmp_path / "kanban.db") as conn:
        _insert_task(conn, "t_plan")
        opened_at = _bind_step_open(conn, "t_plan")
        db_path = _make_state_db(tmp_path)

        nudged, _ = _sweep(
            conn, db_path, now=opened_at + 3600, emit=lambda *_a: None
        )

        assert nudged == ["t_plan"]