"""Regression tests for operator stop semantics on Kanban workers."""

from __future__ import annotations

import argparse
import signal
from pathlib import Path
from unittest.mock import ANY

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb


@pytest.fixture
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kb.connect() as connection:
        yield connection


def _claimed_task(conn, *, title: str = "operator stop"):
    task_id = kb.create_task(conn, title=title, assignee="neo")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
    task = kb.claim_task(conn, task_id)
    assert task is not None
    return task


def test_operator_block_terminates_active_worker_without_retry(
    conn, monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _claimed_task(conn)
    kb._set_worker_pid(conn, task.id, 424242)
    alive = {424242: True}
    signals: list[tuple[int, int]] = []

    def signal_worker(pid: int, sig: int) -> None:
        signals.append((pid, sig))
        alive[pid] = False

    monkeypatch.setattr(kb, "_pid_alive", lambda pid: alive.get(pid, False))

    assert kb.block_task_and_terminate(
        conn,
        task.id,
        reason="operator hold",
        kind="needs_input",
        signal_fn=signal_worker,
    )

    stopped = kb.get_task(conn, task.id)
    assert stopped is not None
    assert stopped.status == "blocked"
    assert stopped.worker_pid is None
    assert stopped.consecutive_failures == 0
    assert signals == [(424242, signal.SIGTERM)]
    assert kb.detect_crashed_workers(conn) == []

    run = kb.latest_run(conn, task.id)
    assert run is not None
    assert run.outcome == "blocked"


def test_reclaim_refuses_to_unblock_blocked_task_with_stale_claim(conn) -> None:
    task = _claimed_task(conn, title="already blocked")
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'blocked', worker_pid = ? WHERE id = ?",
            (515151, task.id),
        )

    signalled: list[tuple[int, int]] = []
    assert not kb.reclaim_task(
        conn,
        task.id,
        signal_fn=lambda pid, sig: signalled.append((pid, sig)),
    )

    held = kb.get_task(conn, task.id)
    assert held is not None
    assert held.status == "blocked"
    assert held.claim_lock == task.claim_lock
    assert held.worker_pid == 515151
    assert signalled == []


def test_cli_block_uses_operator_stop_path(
    conn, monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _claimed_task(conn, title="cli stop")
    kb._set_worker_pid(conn, task.id, 525252)
    terminated: list[tuple[int, str | None]] = []

    def terminate(pid, claim_lock, *, signal_fn=None):
        terminated.append((int(pid), claim_lock))
        return {
            "prev_pid": int(pid),
            "host_local": True,
            "termination_attempted": True,
            "terminated": True,
            "sigkill": False,
        }

    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", terminate)
    args = argparse.Namespace(
        task_id=task.id,
        ids=[],
        reason=["operator", "hold"],
        kind="needs_input",
    )

    assert kanban_cli._cmd_block(args) == 0
    blocked = kb.get_task(conn, task.id)
    assert blocked is not None
    assert blocked.status == "blocked"
    assert terminated == [(525252, task.claim_lock)]


def test_dispatch_kills_worker_spawned_after_operator_block(
    conn, monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = kb.create_task(conn, title="spawn race", assignee="neo")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))

    terminations: list[tuple[int, str | None]] = []

    def terminate(pid, claim_lock, *, signal_fn=None):
        terminations.append((int(pid), claim_lock))
        return {
            "prev_pid": int(pid),
            "host_local": True,
            "termination_attempted": True,
            "terminated": True,
            "sigkill": False,
        }

    def spawn_then_block(claimed, workspace):
        assert kb.block_task(conn, claimed.id, reason="operator won the race")
        return 616161

    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _profile: True)
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", terminate)

    result = kb.dispatch_once(conn, spawn_fn=spawn_then_block, max_spawn=1)

    blocked = kb.get_task(conn, task_id)
    assert blocked is not None
    assert blocked.status == "blocked"
    assert blocked.worker_pid is None
    assert result.spawned == []
    assert terminations == [(616161, ANY)]
    assert kb.detect_crashed_workers(conn) == []
