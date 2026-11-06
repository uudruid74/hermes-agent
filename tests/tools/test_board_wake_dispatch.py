"""Board→topic wake routing: dispatch switch + double-wake guard (t_a8031c32).

Phase 3 — the dispatch switch: on an opted-in board (board_topic:<board> in the
project DB) `dispatch_once` fires `hermes send -t <target> --task-id <id>
"work kanban task <id>"` instead of `_default_spawn` (Evan's design 2026-11-06).
Target = the board's ONE topic (assignee-qualified), or the assignee's DM when
the topic is empty (ruling #4). A CLI-created task (no origin chat) falls back
to `_default_spawn` (ruling #7).

Phase 4 — wake-claim liveness + double-wake guard: a `hermes send -t` wake has
no PID to poll, so its claim lives on `last_heartbeat_at` (or the pickup window
while the wake is still queued behind a busy agent). `release_stale_claims` /
`detect_stale_running` hold or extend instead of reclaiming an in-flight wake —
reclaiming it would re-fire the wake next tick (double-wake) while the session
still owns the work. Beyond the window the claim reclaims and the dispatcher
re-wakes (reclaim-on-death preserved; the wake intake de-dupes the message
itself — Gopher's coordination 2026-11-06).
"""
from __future__ import annotations

import time

import pytest


# ---------------------------------------------------------------------------
# Fixtures — isolated kanban + projects DBs
# ---------------------------------------------------------------------------

@pytest.fixture
def wake_env(monkeypatch, tmp_path):
    """Isolated HERMES_HOME with kanban DB + projects DB wake mapping."""
    from pathlib import Path

    home = tmp_path / ".hermes"
    home.mkdir()
    # Named profiles live under <root>/profiles/<id> — the wake binding
    # database (sessions.task_id) lives in the ASSIGNEE's profile state.db.
    (home / "profiles" / "test-worker").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("USERNAME", "test-worker")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

    from hermes_cli import kanban_db as kb
    from hermes_cli import projects_db as pdb

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        # Assignee must resolve as a profile so the dispatch loop does not
        # bucket the task as nonspawnable.
        monkeypatch.setattr(
            "hermes_cli.profiles.profile_exists", lambda name: True
        )
        with pdb.connect_closing() as pconn:
            yield conn, pconn, tmp_path
    finally:
        conn.close()


def _capture_wakes(monkeypatch):
    """Record `hermes send` wake launches; return the capture list."""
    calls: list[list[str]] = []

    def fake_spawn_detached(argv, **kw):
        calls.append([str(a) for a in argv])
        return object()  # non-None: launch succeeded

    monkeypatch.setattr(
        "hermes_cli._subprocess_compat.spawn_detached", fake_spawn_detached
    )
    return calls


def _events(conn, tid, kind):
    return conn.execute(
        "SELECT * FROM task_events WHERE task_id = ? AND kind = ?",
        (tid, kind),
    ).fetchall()


def _record_origin(conn, tid, *, chat_id="-100123", thread_id="42"):
    """Stamp the task with a chat origin (``__kanban_origin__`` comment)."""
    import json

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


# ---------------------------------------------------------------------------
# Phase 3 — the dispatch switch
# ---------------------------------------------------------------------------

def test_opted_in_board_dispatch_wakes_topic(wake_env, monkeypatch):
    """A wake-routed board fires `hermes send -t`, not a subprocess spawn."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import projects_db as pdb

    conn, pconn, _ = wake_env
    pdb.set_board_topic(pconn, "default", "telegram:-100123:42")
    tid = kb.create_task(conn, title="wake-me", assignee="test-worker")
    _record_origin(conn, tid)
    calls = _capture_wakes(monkeypatch)
    fake_spawn = lambda *a, **k: pytest.fail("_default_spawn must not run")
    monkeypatch.setattr(kb, "_default_spawn", fake_spawn)

    kb.dispatch_once(conn)

    assert len(calls) == 1, f"expected one wake launch, got {calls}"
    argv = calls[0]
    # Ruling #4: the board's ONE topic, assignee-qualified, and the wake
    # carries the task id so the intake can bind it (gap 1).
    assert argv[:3][-2:] == ["send"] or "send" in argv
    i = argv.index("send")
    assert argv[i + 1 : i + 5] == [
        "--task-id", tid, "-t", "test-worker:telegram:-100123:42",
    ]
    assert argv[i + 5] == f"work kanban task {tid}"
    rows = _events(conn, tid, "wake_fired")
    assert len(rows) == 1, "wake_fired must be recorded on the claimed run"
    assert kb.get_task(conn, tid).status == "running"


def test_empty_topic_targets_agent_dm(wake_env, monkeypatch):
    """"Empty topic -> the agent's DM (bare profile name), the only fallback."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import projects_db as pdb

    conn, pconn, _ = wake_env
    pdb.set_board_topic(pconn, "default", "")
    tid = kb.create_task(conn, title="dm-me", assignee="test-worker")
    _record_origin(conn, tid)
    calls = _capture_wakes(monkeypatch)

    kb.dispatch_once(conn)

    assert len(calls) == 1
    argv = calls[0]
    i = argv.index("send")
    # DM fallback = bare assignee (send_cmd expands it to the agent DM).
    assert argv[i + 4] == "test-worker"


def test_unmapped_board_keeps_legacy_spawn(wake_env, monkeypatch):
    """No mapping entry -> not opted in -> `_default_spawn` unchanged."""
    from hermes_cli import kanban_db as kb

    conn, _, _ = wake_env
    tid = kb.create_task(conn, title="spawn-me", assignee="test-worker")
    calls = _capture_wakes(monkeypatch)
    spawned: list[str] = []
    monkeypatch.setattr(
        kb, "_default_spawn",
        lambda task, workspace, board=None: spawned.append(task.id) or 12345,
    )

    kb.dispatch_once(conn)

    assert calls == [], "unmapped boards must never wake"
    assert spawned == [tid]
    assert kb.get_task(conn, tid).status == "running"


def test_cli_created_task_falls_back_to_spawn(wake_env, monkeypatch):
    """Ruling #7: a task with no origin chat keeps `_default_spawn`."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import projects_db as pdb

    conn, pconn, _ = wake_env
    pdb.set_board_topic(pconn, "default", "telegram:-100123:42")
    tid = kb.create_task(conn, title="cli-created", assignee="test-worker")
    calls = _capture_wakes(monkeypatch)
    spawned: list[str] = []
    monkeypatch.setattr(
        kb, "_default_spawn",
        lambda task, workspace, board=None: spawned.append(task.id) or 12345,
    )

    kb.dispatch_once(conn)

    assert calls == [], "origin-less tasks must not enter the wake path"
    assert spawned == [tid]


def test_origin_task_on_mapped_board_wakes(wake_env, monkeypatch):
    """Flip side of the ruling #7 guard: an origin-carrying task DOES wake."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import projects_db as pdb

    conn, pconn, _ = wake_env
    pdb.set_board_topic(pconn, "default", "telegram:-100123:42")
    tid = kb.create_task(conn, title="origin-task", assignee="test-worker")
    _record_origin(conn, tid)
    calls = _capture_wakes(monkeypatch)
    monkeypatch.setattr(
        kb, "_default_spawn",
        lambda *a, **k: pytest.fail("ruling #7 fallback must not fire"),
    )

    kb.dispatch_once(conn)

    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Phase 4 — wake-claim liveness / double-wake guard
# ---------------------------------------------------------------------------

def _claim_and_wake_fire(conn, tid, *, wake=True):
    """Claim the task and (optionally) stamp a wake_fired event on the run."""
    task = kb_claim(conn, tid)
    run_id = conn.execute(
        "SELECT current_run_id FROM tasks WHERE id = ?", (tid,)
    ).fetchone()[0]
    if wake:
        from hermes_cli import kanban_db as kb

        with kb.write_txn(conn):
            kb._append_event(
                conn, tid, "wake_fired", {"target": "x", "run_id": run_id},
                run_id=run_id,
            )
    return int(run_id)


def kb_claim(conn, tid):
    from hermes_cli import kanban_db as kb

    claimed = kb.claim_task(conn, tid)
    assert claimed is not None
    return claimed


def _expire_claim(conn, tid):
    conn.execute(
        "UPDATE tasks SET claim_expires = ? WHERE id = ?",
        (int(time.time()) - 5, tid),
    )
    conn.commit()


def test_wake_claim_holds_while_wake_in_flight(wake_env):
    """Gap 4: an unconsumed wake never reclaims -> cannot double-fire."""
    from hermes_cli import kanban_db as kb

    conn, _, _ = wake_env
    tid = kb.create_task(conn, title="busy-agent", assignee="test-worker")
    _claim_and_wake_fire(conn, tid)
    _expire_claim(conn, tid)

    kb.release_stale_claims(conn)

    row = kb.get_task(conn, tid)
    assert row.status == "running", "in-flight wake must not be reclaimed"
    assert row.claim_lock, "claim must be extended, not released"
    kinds = [r["kind"] for r in _events(conn, tid, "claim_extended")]
    assert kinds == ["claim_extended"], "extension must be observable"


def test_wake_claim_kept_alive_by_heartbeat(wake_env):
    """A heartbeating woken worker keeps its claim past the pickup window."""
    from hermes_cli import kanban_db as kb

    conn, _, _ = wake_env
    tid = kb.create_task(conn, title="heartbeater", assignee="test-worker")
    run_id = _claim_and_wake_fire(conn, tid)
    # Wake fired long ago (pickup window passed) but the worker heartbeats.
    conn.execute(
        "UPDATE task_events SET created_at = ? WHERE task_id = ? AND kind = 'wake_fired'",
        (int(time.time()) - 2 * 60 * 60, tid),
    )
    conn.execute(
        "UPDATE tasks SET claim_expires = ?, last_heartbeat_at = ? WHERE id = ?",
        (int(time.time()) - 5, int(time.time()), tid),
    )
    conn.commit()

    kb.release_stale_claims(conn)

    assert kb.get_task(conn, tid).status == "running"


def test_wake_claim_reclaims_after_pickup_window(wake_env):
    """Reclaim-on-death: a lost wake (never picked up) re-dispatches."""
    from hermes_cli import kanban_db as kb

    conn, _, _ = wake_env
    tid = kb.create_task(conn, title="lost-wake", assignee="test-worker")
    _claim_and_wake_fire(conn, tid)
    conn.execute(
        "UPDATE task_events SET created_at = ? WHERE task_id = ? AND kind = 'wake_fired'",
        (int(time.time()) - 2 * 60 * 60, tid),
    )
    _expire_claim(conn, tid)

    kb.release_stale_claims(conn)

    assert kb.get_task(conn, tid).status == "ready", "lost wake must requeue"


def test_plain_null_pid_claim_still_reclaims(wake_env):
    """Guard is wake-scoped: manually-claimed tasks keep legacy reclaim."""
    from hermes_cli import kanban_db as kb

    conn, _, _ = wake_env
    tid = kb.create_task(conn, title="manual-claim", assignee="test-worker")
    _claim_and_wake_fire(conn, tid, wake=False)
    _expire_claim(conn, tid)

    kb.release_stale_claims(conn)

    assert kb.get_task(conn, tid).status == "ready"


def test_detect_stale_skips_wake_in_flight(wake_env):
    """detect_stale_running must not reset a queued-but-unconsumed wake."""
    from hermes_cli import kanban_db as kb

    conn, _, _ = wake_env
    tid = kb.create_task(conn, title="queued-wake", assignee="test-worker")
    _claim_and_wake_fire(conn, tid)
    # Very old start, no heartbeat, but the wake is fresh (still queued).
    conn.execute(
        "UPDATE tasks SET started_at = ? WHERE id = ?",
        (int(time.time()) - 24 * 60 * 60, tid),
    )
    conn.commit()

    reclaimed = kb.detect_stale_running(conn, stale_timeout_seconds=1)

    assert tid not in reclaimed
    assert kb.get_task(conn, tid).status == "running"


# ---------------------------------------------------------------------------
# Gap 1 carry+bind — wake intake is idempotent (Gopher's edge, 2026-11-06)
# ---------------------------------------------------------------------------

def test_bind_wake_task_binds_session(wake_env):
    """The wake binds its task to the target session (`sessions.task_id`)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.profiles import get_profile_dir

    conn, _, _ = wake_env
    tid = kb.create_task(conn, title="bind-me", assignee="test-worker")
    kb_claim(conn, tid)

    assert kb.bind_wake_task("sess-wake-1", tid) == "bound"

    from hermes_state import SessionDB

    sdb = SessionDB(db_path=get_profile_dir("test-worker") / "state.db")
    try:
        assert sdb.get_session_task_id("sess-wake-1") == tid
    finally:
        sdb.close()


def test_duplicate_wake_intake_is_suppressed(wake_env):
    """A second wake for live work in the same session no-ops (no 2nd pass)."""
    from hermes_cli import kanban_db as kb

    conn, _, _ = wake_env
    tid = kb.create_task(conn, title="dup-wake", assignee="test-worker")
    kb_claim(conn, tid)

    assert kb.bind_wake_task("sess-dup-1", tid) == "bound"
    assert kb.bind_wake_task("sess-dup-1", tid) == "duplicate"

    # Completion clears the binding -> a genuinely re-opened task rebinds.
    assert kb.complete_task(conn, tid, result="ok")
    assert kb.bind_wake_task("sess-dup-1", tid) == "bound"


def test_bind_wake_task_rebinds_on_new_target(wake_env):
    """A wake to a different session is a fresh engagement, not a duplicate."""
    from hermes_cli import kanban_db as kb

    conn, _, _ = wake_env
    tid = kb.create_task(conn, title="rebind", assignee="test-worker")
    kb_claim(conn, tid)
    assert kb.bind_wake_task("sess-a", tid) == "bound"

    assert kb.bind_wake_task("sess-b", tid) == "bound"


# ---------------------------------------------------------------------------
# Gap 3 — woken workers heartbeat from the session binding
# ---------------------------------------------------------------------------

def test_heartbeat_bridge_falls_back_to_session_binding(
    wake_env, monkeypatch
):
    """No env -> the session binding carries the worker identity (gap 3)."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    conn, _, _ = wake_env
    tid = kb.create_task(conn, title="hb-me", assignee="test-worker")
    _claim_and_wake_fire(conn, tid, wake=False)
    monkeypatch.setattr(kt, "_session_bound_task_id", lambda: tid)
    monkeypatch.setattr(kt, "_auto_heartbeat_last_attempt", 0.0)

    assert kt.heartbeat_current_worker_from_env() is True

    row = conn.execute(
        "SELECT last_heartbeat_at FROM tasks WHERE id = ?", (tid,)
    ).fetchone()
    assert row[0] is not None, "binding-scoped heartbeat must touch the task"