"""Session wake INJECTION coverage — t_8140688e.

The enqueue-only bug: ``hermes send -t cli:<session>`` and kanban
notifications queued a ``session_notices`` row and reported success, but
nothing ever delivered it as a turn — the worker only saw the message by
reading the queue over SQL by hand. These tests assert the delivery chain
instead: the sender's signal half, the boot registration the PID lookup
needs, and the drain path handing each queued message to the agent as a real
user message (``steer()`` mid-run in single-query/worker mode, a queued user
turn in the interactive loop).
"""

from __future__ import annotations

import os
import queue
import select
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban, send_cmd
from hermes_state import SessionDB


class _SteerRecorder:
    def __init__(self):
        self.steered = []

    def steer(self, text):
        self.steered.append(text)
        return True


def _fake_cli(db, session_id, *, single_query, agent):
    import cli as cli_mod

    inst = object.__new__(cli_mod.HermesCLI)
    inst._session_db = db
    inst.session_id = session_id
    setattr(inst, "_single_query_mode", single_query)
    inst._agent_running = False
    inst.agent = agent
    inst._pending_input = queue.Queue()
    inst._should_exit = False
    return inst


def _send_args(target: str, message: str, *, json_mode: bool = False):
    import argparse

    return argparse.Namespace(
        list_targets=False,
        to=None,
        user=target,
        message=message,
        file=None,
        subject=None,
        json=json_mode,
        quiet=False,
    )


# ── drain path: notice → turn ────────────────────────────────────────────


def test_wake_notice_injects_as_steer_in_single_query_mode(tmp_path):
    """A drained notice reaches a running worker as a real user message."""
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("worker-sess", "cli")
    assert db.enqueue_session_notice("worker-sess", "WAKE hello worker")
    agent = _SteerRecorder()
    inst = _fake_cli(db, "worker-sess", single_query=True, agent=agent)

    inst._inject_pending_wake_turns()

    # The turn content arrives at the agent (the worker sees it and can act)…
    assert agent.steered == ["WAKE hello worker"]
    # …and the queue is drained — nothing left for manual SQL.
    assert db.drain_session_notices("worker-sess") == []
    db.close()


def test_wake_notice_becomes_next_user_turn_when_idle(tmp_path):
    """In the interactive loop a drained notice becomes the next user turn."""
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("cli-session", "cli")
    assert db.enqueue_session_notice("cli-session", "kanban complete")
    agent = _SteerRecorder()
    inst = _fake_cli(db, "cli-session", single_query=False, agent=agent)

    inst._inject_pending_wake_turns()

    assert inst._pending_input.get_nowait() == "kanban complete"
    assert agent.steered == []
    assert db.drain_session_notices("cli-session") == []
    db.close()


def test_wake_service_injects_on_signal_and_backstop_tick(tmp_path):
    """The wake-service thread delivers on SIGUSR1 and on its periodic tick."""
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("worker-sess", "cli")
    agent = _SteerRecorder()
    inst = _fake_cli(db, "worker-sess", single_query=True, agent=agent)

    inst._start_cli_wake_service()
    try:
        assert db.enqueue_session_notice("worker-sess", "tick backstop")
        deadline = time.time() + 8
        while time.time() < deadline and not agent.steered:
            time.sleep(0.05)
        assert agent.steered == ["tick backstop"]

        assert db.enqueue_session_notice("worker-sess", "signalled wake")
        inst._wake_flag = True  # what the SIGUSR1 handler does
        deadline = time.time() + 3
        while time.time() < deadline and len(agent.steered) < 2:
            time.sleep(0.05)
        assert agent.steered == ["tick backstop", "signalled wake"]
    finally:
        inst._should_exit = True
    db.close()


# ── boot registration: the PID lookup must find the worker ───────────────


def test_wake_service_registers_boot_pid_and_clears_on_exit(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("worker-sess", "cli")
    inst = _fake_cli(db, "worker-sess", single_query=True, agent=_SteerRecorder())

    inst._start_cli_wake_service()
    deadline = time.time() + 5
    while time.time() < deadline and db.get_cli_wake_pid("worker-sess") != os.getpid():
        time.sleep(0.05)
    assert db.get_cli_wake_pid("worker-sess") == os.getpid()

    inst._should_exit = True
    deadline = time.time() + 5
    while time.time() < deadline and db.get_cli_wake_pid("worker-sess") is not None:
        time.sleep(0.05)
    assert db.get_cli_wake_pid("worker-sess") is None
    db.close()


# ── sender signal half ────────────────────────────────────────────────────


def test_send_cli_target_signals_registered_process(tmp_path, monkeypatch, capsys):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("cli-session", "cli")
    db.register_cli_wake("cli-session", 4242)
    db.close()

    import hermes_constants

    monkeypatch.setattr(send_cmd, "_load_hermes_env", lambda: None)
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    killed = []
    monkeypatch.setattr(send_cmd.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    with pytest.raises(SystemExit) as exc:
        send_cmd.cmd_send(_send_args("cli:cli-session", "wake now", json_mode=True))

    assert exc.value.code == 0
    assert killed == [(4242, signal.SIGUSR1)]
    out = capsys.readouterr().out
    assert '"woken": true' in out


def test_send_cli_target_without_registration_reports_not_woken(
    tmp_path, monkeypatch, capsys
):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("cli-session", "cli")
    db.close()

    import hermes_constants

    monkeypatch.setattr(send_cmd, "_load_hermes_env", lambda: None)
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    killed = []
    monkeypatch.setattr(send_cmd.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    with pytest.raises(SystemExit) as exc:
        send_cmd.cmd_send(_send_args("cli:cli-session", "wake now", json_mode=True))

    assert exc.value.code == 0
    assert killed == []
    out = capsys.readouterr().out
    assert '"woken": false' in out


def test_kanban_worker_wake_signals_registered_process(tmp_path, monkeypatch):
    home = tmp_path / "neo"
    home.mkdir()
    import hermes_cli.profiles

    monkeypatch.setattr(hermes_cli.profiles, "get_profile_dir", lambda _name: home)
    monkeypatch.setattr(
        kanban,
        "_acting_session_identity",
        lambda: {
            "HERMES_SESSION_ID": "",
            "HERMES_SESSION_PLATFORM": "",
            "HERMES_SESSION_CHAT_ID": "",
            "HERMES_SESSION_THREAD_ID": "",
            "HERMES_SESSION_PROFILE": "",
        },
    )
    db = SessionDB(db_path=home / "state.db")
    db.create_session("worker-sess-1", "kanban")
    db.set_session_task_id("worker-sess-1", "t_task")
    db.register_cli_wake("worker-sess-1", 4242)
    db.close()

    killed = []
    monkeypatch.setattr(kanban.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    task = SimpleNamespace(id="t_task", assignee="neo")

    woken = kanban._wake_task_worker_session(task, "{}", "commented")

    assert woken is True
    assert killed == [(4242, signal.SIGUSR1)]


# ── live round-trip: real process, real SIGUSR1, real injection ──────────

_CHILD_SRC = '''
import queue
import sys
import time
from pathlib import Path

sys.path.insert(0, sys.argv[3])
from hermes_state import SessionDB
import cli as cli_mod


class Agent:
    def steer(self, text):
        print("WOKEN:" + text, flush=True)
        return True


db = SessionDB(db_path=Path(sys.argv[1]))
inst = object.__new__(cli_mod.HermesCLI)
inst._session_db = db
inst.session_id = sys.argv[2]
inst._single_query_mode = True
inst._agent_running = False
inst.agent = Agent()
inst._pending_input = queue.Queue()
inst._should_exit = False
inst._start_cli_wake_service()
print("READY", flush=True)
time.sleep(30)
'''


@pytest.mark.skipif(not hasattr(signal, "SIGUSR1"), reason="needs POSIX SIGUSR1")
def test_live_sigusr1_delivers_notice_as_injected_turn(tmp_path, monkeypatch):
    """Full chain against a real process: enqueue + SIGUSR1 → injected turn.

    The 3-second assertion window proves the SIGUSR1 signal half delivered the
    message (the missed-signal backstop tick is 5 seconds).
    """
    repo = Path(kanban.__file__).resolve().parents[1]
    script = tmp_path / "wake_child.py"
    script.write_text(_CHILD_SRC, encoding="utf-8")
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    db.create_session("live-sess", "cli")
    db.close()

    proc = subprocess.Popen(
        [sys.executable, str(script), str(db_path), "live-sess", str(repo)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "READY"
        # Boot registration must land before the sender's PID lookup.
        db = SessionDB(db_path=db_path)
        deadline = time.time() + 5
        while time.time() < deadline and db.get_cli_wake_pid("live-sess") != proc.pid:
            time.sleep(0.05)
        assert db.get_cli_wake_pid("live-sess") == proc.pid
        db.close()

        # Real production send path: enqueue + SIGUSR1 to the registered PID.
        import hermes_constants

        monkeypatch.setattr(send_cmd, "_load_hermes_env", lambda: None)
        monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
        with pytest.raises(SystemExit) as exc:
            send_cmd.cmd_send(_send_args("cli:live-sess", "WAKE-RT token"))
        assert exc.value.code == 0

        ready, _, _ = select.select([proc.stdout], [], [], 3)
        assert ready, "child did not receive the wake as a turn within 3s"
        assert proc.stdout.readline().strip() == "WOKEN:WAKE-RT token"
    finally:
        proc.kill()
        proc.wait(timeout=10)