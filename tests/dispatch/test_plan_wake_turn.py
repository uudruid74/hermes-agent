"""Cell 6 — plan dispatch + plan-review response wake => outcome truth + turn.

- Plan dispatch: `plan.plan(agent, "dispatch", ...)` creates the task and
  kicks dispatch; the event-time kick itself is pinned in
  tests/tools/test_plan_dispatch_kick.py. Here the created task (with its
  chat origin) runs through the real dispatcher wake and the wake lands a
  TURN on the worker's session.
- Plan-review response wake: `plan_binding_adapter._send_user` (the
  t_1098032b path) verifies delivery through `_bridge_cli_wake` before
  claiming "delivered". The L1 verification contract is pinned in
  tests/tools/test_plan_wake_delivery.py; here the verified send crosses the
  REAL CLI -> bridge -> intake and a TURN spawns with the review answer.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from hermes_cli import _subprocess_compat
from hermes_cli import kanban_db as kb
from hermes_cli import send_cmd
from tests.dispatch.test_kanban_wake_turn import WORKER, _record_origin
from tests.dispatch.wake_harness import (
    patch_send_tool_cli_env,
    run_cli_send_in_process,
    start_bridge_on,
)

REVIEW_BODY = (
    "Gopher responded to your tool-call-limit ask on t_planrv — "
    "Split the step: probe first."
)


class _Agent:
    profile_name = "Neo"
    agent_name = "neo"
    session_id = "plan-sess"
    canonical_session_id = "plan-canon"
    _memory_manager = None
    _session_db = None
    _session_temperature = None
    _plan_approval_timed_out = None

    def clarify_callback(self, *_args, **_kwargs) -> str:
        return "Approve"


@pytest.fixture
def plan_env(monkeypatch, tmp_path):
    """Isolated kanban DB (file-backed) + worker profile dir + plan tool."""
    home = tmp_path / ".hermes"
    (home / "profiles" / WORKER).mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("USERNAME", "neo")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(
        "hermes_cli.profiles.profile_exists", lambda name: name == WORKER
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir", lambda _name: home / "profiles" / WORKER
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.get_active_profile_name", lambda: "neo"
    )

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        from tools import plan

        monkeypatch.setattr(plan, "_get_kanban_db", lambda board=None: conn)
        monkeypatch.setattr(
            plan, "_resolve_board", lambda project: project or "default"
        )
        kick_calls = []
        monkeypatch.setattr(
            "hermes_cli.kanban.kick_dispatch",
            lambda board=None: kick_calls.append({"board": board}),
        )
        yield conn, kick_calls
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Plan dispatch wake
# ---------------------------------------------------------------------------


def test_plan_dispatch_created_task_wakes_worker_turn(
    wake_env, plan_env, monkeypatch, tmp_path
):
    """A plan-dispatched task on a wake-routed board fires the dispatch wake
    and the wake lands a TURN on the worker's session."""
    runner, adapter, recorder = wake_env
    conn, kick_calls = plan_env
    smt = patch_send_tool_cli_env(monkeypatch)
    monkeypatch.setattr(send_cmd, "_load_hermes_env", lambda: None)

    from tools import plan

    result = plan.plan(
        _Agent(),
        "dispatch",
        title="plan-dispatch wake test",
        goal="The wake must land a turn.",
        project="hermes-agent",
        assignee=WORKER,
    )
    assert "ERROR" not in str(result), result
    assert kick_calls, "plan dispatch must kick dispatch"

    row = conn.execute("SELECT id, board FROM tasks ORDER BY created_at DESC LIMIT 1").fetchone()
    assert row is not None
    tid, board = row["id"], row["board"] or "default"
    _record_origin(conn, tid)

    from hermes_cli import projects_db as pdb

    with pdb.connect_closing() as pconn:
        pdb.set_board_topic(pconn, board, "telegram:-100123:42")

    calls = []

    def fake_spawn_detached(argv, **kw):
        calls.append([str(a) for a in argv])
        return object()

    monkeypatch.setattr(_subprocess_compat, "spawn_detached", fake_spawn_detached)
    kb.dispatch_once(conn)

    assert len(calls) == 1, calls
    argv = calls[0]
    i = argv.index("send")
    assert argv[i + 1 : i + 5] == [
        "--task-id", tid, "-t", f"{WORKER}:telegram:-100123:42",
    ]

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
    assert code == 0, f"dispatch wake CLI exited {code}"
    assert len(recorder.turns) == 1, recorder.turns
    turn = recorder.turns[0]
    assert "work kanban task " + tid in turn["message"], turn["message"]
    assert turn["source"].chat_id == "-100123"


# ---------------------------------------------------------------------------
# Plan-review response wake
# ---------------------------------------------------------------------------


def _fake_bridge_cli_wake_subprocess(monkeypatch, runner, smt, tmp_path):
    """Route `_bridge_cli_wake`'s subprocess through the real cmd_send into
    the runner; returns a call sink."""
    import cron.scheduler as scheduler

    argv_sink = []

    def fake_subprocess_run(argv, **kw):
        argv_sink.append([str(a) for a in argv])
        code, out = run_cli_send_in_process(argv)
        return SimpleNamespace(returncode=code, stdout=out, stderr="")

    monkeypatch.setattr(scheduler.subprocess, "run", fake_subprocess_run)

    async def _serve():
        server, sock = await start_bridge_on(runner, monkeypatch, tmp_path)
        monkeypatch.setattr(smt, "bridge_socket_path", lambda *_a, **_k: str(sock))
        return server

    return argv_sink, _serve


def test_plan_review_response_wake_confirms_delivery_and_turns(
    wake_env, plan_env, monkeypatch, tmp_path
):
    """L1+L2: `_send_user` confirms delivery ONLY when the wake's turn
    actually spawns; the review answer arrives as the worker's inbound turn."""
    runner, adapter, recorder = wake_env
    conn, _kicks = plan_env
    smt = patch_send_tool_cli_env(monkeypatch)
    monkeypatch.setattr(send_cmd, "_load_hermes_env", lambda: None)

    from tools import plan_binding_adapter

    argv_sink, _serve = _fake_bridge_cli_wake_subprocess(
        monkeypatch, runner, smt, tmp_path
    )

    async def scenario():
        server = await _serve()
        try:
            failure = await asyncio.to_thread(
                plan_binding_adapter._send_user, WORKER, REVIEW_BODY
            )
            await asyncio.wait_for(recorder.done.wait(), timeout=15)
            return failure
        finally:
            server.close()
            await server.wait_closed()

    failure = asyncio.run(scenario())

    assert adapter.errors == [], [str(e) for e in adapter.errors]
    assert failure is None, f"confirmed wake must return None, got {failure!r}"
    assert len(argv_sink) == 1
    assert argv_sink[0][:3] == ["hermes", "send", "-t"]
    assert len(recorder.turns) == 1, recorder.turns
    assert REVIEW_BODY in recorder.turns[0]["message"]


def test_plan_review_response_wake_unconfirmed_fails_loud_no_turn(
    wake_env, plan_env, monkeypatch, tmp_path
):
    """A wake the intake drops must fail `_send_user` loudly — never a false
    'delivered' — and no turn can spawn."""
    runner, adapter, recorder = wake_env
    conn, _kicks = plan_env
    smt = patch_send_tool_cli_env(monkeypatch)
    monkeypatch.setattr(send_cmd, "_load_hermes_env", lambda: None)

    class SwallowAdapter:
        config = SimpleNamespace(extra={})
        _active_sessions = {}

        async def handle_message(self, event):
            return None  # silently swallows -> dropped

    runner.adapters[Platform.TELEGRAM] = SwallowAdapter()

    from tools import plan_binding_adapter

    argv_sink, _serve = _fake_bridge_cli_wake_subprocess(
        monkeypatch, runner, smt, tmp_path
    )

    async def scenario():
        server = await _serve()
        try:
            return await asyncio.to_thread(
                plan_binding_adapter._send_user, WORKER, REVIEW_BODY
            )
        finally:
            server.close()
            await server.wait_closed()

    failure = asyncio.run(scenario())

    assert failure is not None, "an unconfirmed wake must never be delivered"
    assert "did not confirm delivery" in failure
    assert recorder.turns == [], "a dropped wake must never spawn a turn"
