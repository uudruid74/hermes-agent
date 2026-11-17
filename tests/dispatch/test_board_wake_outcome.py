"""Cell 10 — board-topic wake routing => L1 outcome truth.

CONTRACT.md cell 10 (L1 outcome truth; L2 n/a — the wake lands in the
assignee's topic session, whose row->turn chain is L2-covered by the core
cells' harness).

The routing map itself (board -> ONE topic, DM fallback, per-task board
column, ambient-pointer rip-out, unmapped/origin-less spawn fallback) is
pinned in tests/tools/test_board_wake_route.py + test_board_wake_dispatch.py
— this file does NOT repeat it. What it locks is the seam's *outcome
truthfulness* for the wake branch of the dispatch switch:

- a wake that cannot even LAUNCH (``spawn_detached`` returns None) is a
  HARD sender-side error (RuntimeError), never a silent ack — and the
  dispatcher's except-clause turns it into a recorded spawn failure with
  the task back in ``ready`` (Law 1: dropped is never success);
- a launched wake is recorded honestly as ``delivery: "launched"`` — the
  word says exactly what the seam observed (a process start), never
  "spawned"/"delivered", so no reader mistakes a started subprocess for a
  turn that landed;
- no wake_fired event exists for a task the switch routed to spawn;
- the hostage guard: no test may reach a real ``hermes send``.
"""

from __future__ import annotations

import json
import time
import types
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _no_live_wake(monkeypatch):
    """Hostage guard: any real wake send fails loud instead of waking an agent."""

    def blocked(*_a, **_kw):
        raise AssertionError(
            "wake test tried to run a real subprocess — fake the wake "
            "transport (hermes_cli._subprocess_compat.spawn_detached) before "
            "invoking a wake path"
        )

    import hermes_cli._subprocess_compat as sc

    monkeypatch.setattr(sc, "spawn_detached", blocked)


# ---------------------------------------------------------------------------
# Fixtures — isolated kanban + projects DBs (mirrors
# tests/tools/test_board_wake_dispatch.py's wake_env, plus a topic map)
# ---------------------------------------------------------------------------


@pytest.fixture
def board_env(monkeypatch, tmp_path):
    """Isolated HERMES_HOME with kanban DB + a topic-mapped board."""
    from pathlib import Path

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)

    from hermes_cli import kanban_db as kb
    from hermes_cli import projects_db as pdb

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        monkeypatch.setattr(
            "hermes_cli.profiles.profile_exists", lambda name: True
        )
        with pdb.connect_closing() as pconn:
            # TWO opted-in boards: one with a topic, one with DM fallback.
            pdb.set_board_topic(pconn, "topic-board", "telegram:-100123:42")
            pdb.set_board_topic(pconn, "dm-board", "")
        yield conn
    finally:
        conn.close()


def _make_task(task_id: str, assignee: str, board: str):
    """Build a minimal Task without touching a DB (as test_board_wake_route
    does) — target-seam tests do not need dispatcher bookkeeping."""
    from hermes_cli.kanban_db import Task

    return Task(
        id=task_id,
        title="cell-10 L1 test",
        body=None,
        assignee=assignee,
        status="ready",
        priority=0,
        created_by="test",
        created_at=0,
        started_at=None,
        completed_at=None,
        workspace_kind="scratch",
        workspace_path="/tmp/wsx",
        claim_lock=None,
        claim_expires=None,
        tenant=None,
        board=board,
    )


def _create_claimed_task(conn, title, *, board):
    """Full dispatcher path: a ready task the tick can claim."""
    from hermes_cli import kanban_db as kb

    return kb.create_task(
        conn, title=title, assignee="test-worker", board=board
    )


def _record_origin(conn, tid, *, chat_id="-100123", thread_id="42"):
    """Stamp the task with a chat origin (``__kanban_origin__`` comment)."""
    from hermes_cli import kanban_db as kb

    conn.execute(
        "INSERT INTO task_comments (task_id, author, body, created_at) "
        "VALUES (?, 'system', ?, ?)",
        (
            tid,
            kb._ORIGIN_MARKER
            + json.dumps(
                {
                    "platform": "telegram",
                    "chat_id": chat_id,
                    "thread_id": thread_id,
                    "profile": "test-worker",
                }
            ),
            int(time.time()),
        ),
    )
    conn.commit()


def _wake_events(conn, tid):
    return conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind = 'wake_fired'",
        (tid,),
    ).fetchall()


def _fake_route(topic):
    """A resolved wake route as projects_db would return it."""
    return SimpleNamespace(mode="wake", is_wake=True, topic=topic)


def _capture_launch(monkeypatch, *, launch):
    """Fake spawn_detached; ``launch`` controls what the OS appears to do.

    Returns the list of argv lists the seam tried to launch.
    """
    calls: list[list[str]] = []

    def fake_spawn(argv, **kw):
        calls.append([str(a) for a in argv])
        return launch() if callable(launch) else launch

    monkeypatch.setattr(
        "hermes_cli._subprocess_compat.spawn_detached", fake_spawn
    )
    return calls


# ---------------------------------------------------------------------------
# L1 outcome truth — the wake-launch seam itself (_make_wake_dispatch)
# ---------------------------------------------------------------------------


class TestWakeLaunchOutcome:
    def test_launched_wake_records_launched_delivery(self, board_env, monkeypatch):
        """Success path: the event payload says ``launched``, never more."""
        from hermes_cli import kanban_db as kb

        conn = board_env
        monkeypatch.setattr(
            kb, "get_origin_routing", lambda _conn, _tid: {"chat_id": "-100123"}
        )
        dispatch = kb._make_wake_dispatch(
            conn, lambda _conn, _board: _fake_route("telegram:-100123:42")
        )
        calls = _capture_launch(monkeypatch, launch=types.SimpleNamespace(pid=4242))

        dispatch(_make_task("t_l1", "test-worker", "topic-board"), "/tmp/wsx")

        assert len(calls) == 1, "the wake launched"
        rows = _wake_events(conn, "t_l1")
        assert len(rows) == 1, "wake_fired must land on the claimed run"
        payload = json.loads(rows[0][0])
        assert payload["target"] == "test-worker:telegram:-100123:42"
        assert payload["delivery"] == "launched", (
            "the seam launched a subprocess — it must not claim a turn "
            "landed ('spawned'/'delivered' would be a false green)"
        )
        assert payload["pid"] == 4242, "the observable handle is recorded"

    def test_failed_launch_is_a_hard_error_not_an_ack(
        self, board_env, monkeypatch
    ):
        """Law 1: ``spawn_detached`` -> None raises; nothing is acked."""
        from hermes_cli import kanban_db as kb

        conn = board_env
        monkeypatch.setattr(
            kb, "get_origin_routing", lambda _conn, _tid: {"chat_id": "-100123"}
        )
        dispatch = kb._make_wake_dispatch(
            conn, lambda _conn, _board: _fake_route("telegram:-100123:42")
        )
        _capture_launch(monkeypatch, launch=None)

        with pytest.raises(RuntimeError):
            dispatch(_make_task("t_l1_drop", "test-worker", "topic-board"), "/tmp/wsx")

        # Nothing was claimed as fired: no event row, no false green.
        assert _wake_events(conn, "t_l1_drop") == []

    def test_dm_fallback_wake_records_launched_truth(self, board_env, monkeypatch):
        """A topic-less board opts in with the DM fallback (ruling #4) — and
        the outcome truth is the same: ``launched``, never 'delivered'."""
        from hermes_cli import kanban_db as kb

        conn = board_env
        monkeypatch.setattr(
            kb, "get_origin_routing", lambda _conn, _tid: {"chat_id": "-100123"}
        )
        dispatch = kb._make_wake_dispatch(
            conn, lambda _conn, _board: _fake_route(None)
        )
        calls = _capture_launch(monkeypatch, launch=types.SimpleNamespace(pid=7))

        dispatch(_make_task("t_dm", "test-worker", "dm-board"), "/tmp/wsx")

        assert len(calls) == 1
        argv = calls[0]
        i = argv.index("-t")
        assert argv[i + 1] == "test-worker", "DM fallback = bare assignee"
        payload = json.loads(_wake_events(conn, "t_dm")[0][0])
        assert payload["target"] == "test-worker"
        assert payload["delivery"] == "launched"


# ---------------------------------------------------------------------------
# L1 outcome truth — full dispatcher tick (claim + wake + requeue-on-drop)
# ---------------------------------------------------------------------------


class TestDispatchTickOutcome:
    """The seam inside the real ``dispatch_once`` loop."""

    def test_failed_launch_records_spawn_failure_and_requeues(
        self, board_env, monkeypatch
    ):
        """A wake that could not launch is a recorded spawn failure and the
        task returns to ``ready`` for the next tick — never a dead claim
        with no worker, never counted as spawned."""
        from hermes_cli import kanban_db as kb

        conn = board_env
        tid = _create_claimed_task(conn, "requeue-me", board="topic-board")
        _record_origin(conn, tid)

        monkeypatch.setattr(
            "hermes_cli._subprocess_compat.spawn_detached",
            lambda argv, **kw: None,
        )

        result = kb.dispatch_once(conn)

        assert tid not in [r[0] for r in result.spawned], (
            "the wake never launched — the tick must not count it spawned"
        )
        row = kb.get_task(conn, tid)
        assert row is not None
        assert row.status == "ready", (
            "a wake that failed to launch must put the task back in ready"
        )
        assert row.claim_lock is None, "the dead claim must be released"
        # The drop is durable evidence: a closed spawn_failed run.
        failures = conn.execute(
            "SELECT ended_at IS NOT NULL FROM task_runs "
            "WHERE task_id = ? AND outcome = 'spawn_failed'",
            (tid,),
        ).fetchall()
        assert failures, "the drop must be recorded, not a silent gap"
        assert all(f[0] for f in failures), "the claimed run must be closed"
        assert _wake_events(conn, tid) == [], "no wake_fired without a wake"

    def test_launched_wake_via_full_tick_records_target_and_liveness(
        self, board_env, monkeypatch
    ):
        """The happy path through the real tick: wake fires, event lands with
        ``delivery: launched`` + pid, task is running with no worker pid —
        wake-claim liveness (heartbeat) is what keeps that claim alive."""
        from hermes_cli import kanban_db as kb

        conn = board_env
        tid = _create_claimed_task(conn, "tick-wake-me", board="topic-board")
        _record_origin(conn, tid)

        calls = _capture_launch(
            monkeypatch, launch=types.SimpleNamespace(pid=991)
        )

        result = kb.dispatch_once(conn)

        assert tid in [r[0] for r in result.spawned]
        assert len(calls) == 1, "exactly one wake launch"
        argv = calls[0]
        i = argv.index("-t")
        assert argv[i + 1] == "test-worker:telegram:-100123:42"
        assert "--task-id" in argv, "the wake carries the task id (gap 1)"
        payload = json.loads(_wake_events(conn, tid)[0][0])
        assert payload["delivery"] == "launched"
        assert payload["pid"] == 991
        assert kb.get_task(conn, tid).status == "running"


# ---------------------------------------------------------------------------
# L1 outcome truth — no event without a wake
# ---------------------------------------------------------------------------


class TestNoFalseEvents:
    def test_spawn_path_records_no_wake_event(self, board_env, monkeypatch):
        """Ruling #7: an origin-less task takes the spawn fallback — the tick
        must never stamp a wake_fired event for it. A spawn is not a wake;
        its outcome rides the spawn path, not the wake ledger."""
        from hermes_cli import kanban_db as kb

        conn = board_env
        tid = _create_claimed_task(conn, "spawn-me", board="topic-board")
        # NOTE: no _record_origin — origin-less by construction.

        spawned = []
        monkeypatch.setattr(
            kb,
            "_default_spawn",
            lambda task, workspace, board=None: spawned.append(task.id) or 12345,
        )

        kb.dispatch_once(conn)

        assert spawned == [tid], "the origin-less task must take the spawn path"
        assert _wake_events(conn, tid) == [], (
            "the spawn path must never stamp a wake event"
        )

    def test_launch_argv_carries_task_id_for_binding(self, board_env, monkeypatch):
        """The launched wake is bindable: ``--task-id <id>`` rides the argv
        so the intake can bind the session to the task (gap 1) — without it
        the 'launched' wake is unattributable at intake time."""
        from hermes_cli import kanban_db as kb

        conn = board_env
        monkeypatch.setattr(
            kb, "get_origin_routing", lambda _conn, _tid: {"chat_id": "-100123"}
        )
        dispatch = kb._make_wake_dispatch(
            conn, lambda _conn, _board: _fake_route("telegram:-100123:42")
        )
        calls = _capture_launch(
            monkeypatch, launch=types.SimpleNamespace(pid=1)
        )

        dispatch(_make_task("t_bind", "test-worker", "topic-board"), "/tmp/wsx")

        argv = calls[0]
        i = argv.index("--task-id")
        assert argv[i + 1] == "t_bind"
