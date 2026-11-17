"""Cell 4 — kanban wake_fired (worker spawn / task events) => outcome truth + turn.

The board→topic wake dispatch (``_make_wake_dispatch``) fires
``hermes send --task-id <id> -t <target> "work kanban task <id>"`` and
records the ``wake_fired`` event on the claimed run. The L1 leg (spawn vs
legacy fallback, per-task route, event truth) is pinned in
tests/tools/test_board_wake_dispatch.py / test_board_wake_route.py. This
file crosses the remaining gap: the fired wake must actually land — the
argv runs through the real ``cmd_send`` -> bridge -> intake, the carried
task id binds to the target session (``sessions.task_id``), and a TURN
spawns on it.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from hermes_cli import _subprocess_compat
from hermes_cli import kanban_db as kb
from hermes_cli import send_cmd
from tests.dispatch.wake_harness import (
    patch_send_tool_cli_env,
    run_cli_send_in_process,
    start_bridge_on,
)

WORKER = "wake-worker"
BOARD = "default"
TOPIC = "telegram:-100123:42"
CHAT_ID, THREAD_ID = "-100123", "42"


@pytest.fixture
def kanban_wake_env(monkeypatch, tmp_path):
    """Isolated HERMES_HOME with an init kanban DB + worker profile dir."""
    home = tmp_path / ".hermes"
    (home / "profiles" / WORKER).mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(
        "hermes_cli.profiles.profile_exists", lambda name: name == WORKER
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir", lambda _name: home / "profiles" / WORKER
    )

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        yield conn
    finally:
        conn.close()


def _record_origin(conn, tid, *, chat_id=CHAT_ID, thread_id=THREAD_ID):
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
                    "profile": WORKER,
                }
            ),
            int(time.time()),
        ),
    )
    conn.commit()


def _wake_route(conn, board):
    from types import SimpleNamespace

    return SimpleNamespace(mode="wake", is_wake=True, topic=TOPIC)


def _drop_wake_dispatch(conn, monkeypatch, *, fail_spawn=True):
    """Build the real wake dispatch with spawn captured; returns (dispatch, calls)."""
    calls = []

    def fake_spawn_detached(argv, **kw):
        calls.append([str(a) for a in argv])
        return object()

    monkeypatch.setattr(_subprocess_compat, "spawn_detached", fake_spawn_detached)
    if fail_spawn:

        def never_spawn(task, workspace, *, board=None):
            pytest.fail("legacy spawn must not run for a wake-routed board")

        fallback = never_spawn
    else:
        fallback = None
    dispatch = kb._make_wake_dispatch(conn, _wake_route, fallback_spawn=fallback)
    return dispatch, calls


def _fire_wake(conn, monkeypatch):
    """Create + claim a wake-routed task, fire the real dispatch wake, and
    return (argv, task_id)."""
    tid = kb.create_task(conn, title="wake-task", assignee=WORKER, board=BOARD)
    _record_origin(conn, tid)
    kb.claim_task(conn, tid)

    dispatch, calls = _drop_wake_dispatch(conn, monkeypatch)
    task = kb.get_task(conn, tid)
    assert task is not None
    assert dispatch(task, "/tmp/ws") is None
    assert len(calls) == 1, calls
    argv = calls[0]
    i = argv.index("send")
    assert argv[i + 1 : i + 5] == ["--task-id", tid, "-t", f"{WORKER}:{TOPIC}"]
    assert argv[i + 5] == f"work kanban task {tid}"
    wake_events = conn.execute(
        "SELECT * FROM task_events WHERE task_id = ? AND kind = 'wake_fired'",
        (tid,),
    ).fetchall()
    assert len(wake_events) == 1
    assert json.loads(wake_events[0]["payload"])["target"] == f"{WORKER}:{TOPIC}"
    return argv, tid


def test_kanban_wake_fired_records_event_and_fires_send(kanban_wake_env, monkeypatch):
    """L1: the wake fires the send argv and records wake_fired on the run."""
    conn = kanban_wake_env
    _fire_wake(conn, monkeypatch)


def test_kanban_wake_fired_wake_lands_turn_and_binds_task(
    wake_env, kanban_wake_env, monkeypatch, tmp_path
):
    """L2: the fired wake crosses the real CLI -> bridge -> intake, binds the
    carried task to the target session, and a TURN spawns on it."""
    runner, adapter, recorder = wake_env
    conn = kanban_wake_env
    smt = patch_send_tool_cli_env(monkeypatch)
    monkeypatch.setattr(send_cmd, "_load_hermes_env", lambda: None)

    argv, tid = _fire_wake(conn, monkeypatch)

    async def scenario():
        server, sock = await start_bridge_on(runner, monkeypatch, tmp_path)
        try:
            monkeypatch.setattr(
                smt, "bridge_socket_path", lambda *_a, **_k: str(sock)
            )
            code, _out = await asyncio.to_thread(run_cli_send_in_process, argv)
            await asyncio.wait_for(recorder.done.wait(), timeout=15)
            return code
        finally:
            server.close()
            await server.wait_closed()

    code = asyncio.run(scenario())

    assert adapter.errors == [], [str(e) for e in adapter.errors]
    assert code == 0, f"wake CLI exited {code}"
    assert len(recorder.turns) == 1, recorder.turns
    turn = recorder.turns[0]
    assert "work kanban task " + tid in turn["message"], turn["message"]
    assert turn["source"].chat_id == CHAT_ID
    assert turn["source"].thread_id == THREAD_ID
    # The carried task id bound to the session the turn ran on (gap 1).
    from hermes_cli.profiles import get_profile_dir
    from hermes_state import SessionDB

    sdb = SessionDB(db_path=get_profile_dir(WORKER) / "state.db")
    try:
        assert sdb.get_session_task_id(turn["session_id"]) == tid
    finally:
        sdb.close()
