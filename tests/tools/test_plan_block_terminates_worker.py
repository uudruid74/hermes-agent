"""Regression: plan block hard-stops the worker process (Evan 2026-11-18).

Live case t_c7840ef7: blocking a task flipped the DB row but the worker
kept heartbeating and editing the shared tree for minutes. ``plan block``
must SIGTERM (then SIGKILL after the grace window) the task's worker_pid
as part of blocking — the kanban_* tool surface stays on the shared API,
but the plan tool is ours and stops a task NOW.
"""

import os
import socket
import sqlite3
import subprocess
import sys

import pytest

from tools import plan
from hermes_cli.kanban_db import SCHEMA_SQL


def _make_db(
    tmp_path,
    status="running",
    task_id="t_block",
    worker_pid=None,
    claim_lock=None,
):
    db_path = tmp_path / "kanban.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            "INSERT INTO tasks (id, title, status, assignee, created_at, "
            "worker_pid, claim_lock, current_run_id, board) "
            "VALUES (?, ?, ?, ?, 1, ?, ?, ?, 'default')",
            (task_id, "Block me", status, "gopher", worker_pid, claim_lock, 1),
        )
        if worker_pid:
            conn.execute(
                "INSERT INTO task_runs (id, task_id, profile, status, started_at) "
                "VALUES (1, ?, 'gopher', 'running', 1)",
                (task_id,),
            )
    return db_path


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _local_claim_lock():
    return f"{socket.gethostname()}:999999"


def test_block_terminates_live_worker_process(tmp_path, monkeypatch):
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"]
    )
    try:
        db_path = _make_db(
            tmp_path, worker_pid=proc.pid, claim_lock=_local_claim_lock()
        )
        monkeypatch.setattr(plan, "_enumerate_kanban_dbs", lambda: [str(db_path)])

        result = plan._cmd_block("t_block")

        assert "BLOCKED:" in result
        assert f"worker {proc.pid}" in result
        assert "terminated" in result

        # The process must be gone within the ~5s grace window. Use
        # wait() so a zombie (killed but unreaped) counts as dead.
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            pytest.fail("worker process survived plan block")

        with sqlite3.connect(db_path) as conn:
            status = conn.execute(
                "SELECT status FROM tasks WHERE id='t_block'"
            ).fetchone()[0]
            run = conn.execute(
                "SELECT status, outcome FROM task_runs WHERE id=1"
            ).fetchone()
            ev = conn.execute(
                "SELECT kind, payload FROM task_events "
                "WHERE kind='worker_terminated'"
            ).fetchone()
        assert status == "blocked"
        assert run[0] == "terminated"
        assert run[1] == "blocked"
        assert ev is not None
        assert "termination" in ev[1]
    finally:
        if _alive(proc.pid):
            proc.kill()


def test_block_without_worker_pid_does_not_attempt_termination(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    monkeypatch.setattr(plan, "_enumerate_kanban_dbs", lambda: [str(db_path)])

    result = plan._cmd_block("t_block")

    assert "BLOCKED:" in result
    assert " (worker" not in result
    with sqlite3.connect(db_path) as conn:
        status = conn.execute(
            "SELECT status FROM tasks WHERE id='t_block'"
        ).fetchone()[0]
        ev = conn.execute(
            "SELECT kind FROM task_events WHERE kind='worker_terminated'"
        ).fetchone()
    assert status == "blocked"
    assert ev is None  # nothing to terminate — no event, no note


def test_block_non_host_local_worker_left_to_dispatcher(tmp_path, monkeypatch):
    # A claim lock from another host cannot be signalled from here; the
    # task still blocks and the attempt is recorded for provenance.
    db_path = _make_db(
        tmp_path, worker_pid=999999999, claim_lock="otherhost:123"
    )
    monkeypatch.setattr(plan, "_enumerate_kanban_dbs", lambda: [str(db_path)])

    result = plan._cmd_block("t_block")

    assert "BLOCKED:" in result
    assert "not host-local" in result
    with sqlite3.connect(db_path) as conn:
        ev = conn.execute(
            "SELECT kind, payload FROM task_events "
            "WHERE kind='worker_terminated'"
        ).fetchone()
    assert ev is not None
    assert ev[1].startswith('{"worker_pid": 999999999')
