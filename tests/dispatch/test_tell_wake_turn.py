"""Cell 3 — tell_tool with session_id (`session_wake_target`) => outcome truth + turn.

The tell tool composes the `hermes send -t` wake; a NAMED session id (the
`session_wake_target` shape) resolves at use time to the session's CHAT
(`<profile>:<platform>:<chat>[:<thread>]`), the shape proven to land as a
user turn in the gateway's active session (Evan 2026-10-20; t_a427292d).

- L1: tell_tool resolves the named session to its chat target and reports
  dispatch truthfully; the wake carries the wrapped message.
- L2: the wake argv runs through the REAL `send_cmd.cmd_send` -> the REAL
  bridge socket -> the REAL intake handler -> a TURN spawns on the target
  session (the `2026-11-16-tell-wake-delivered-no-turn` class lockout).

The session-resolution shape itself is pinned in tests/tools/test_tell_tool.py
and test_wake_target_shape.py — here we cross the intake seam to the turn.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from hermes_cli import _subprocess_compat
from hermes_cli import send_cmd
from tests.dispatch.wake_harness import (
    patch_send_tool_cli_env,
    run_cli_send_in_process,
    start_bridge_on,
)

TARGET_PROFILE = "zephyr"
TARGET_SESSION = "20261017_121500_receiver"
ORIGIN_SESSION = "cron_journal_20261017_090000"
CHAT_ID = "-100555"
THREAD_ID = "99"


@pytest.fixture
def target_profile_env(monkeypatch, tmp_path):
    """A target profile whose state.db holds one gateway session with a chat
    origin — the session_wake_target resolution shape."""
    from pathlib import Path

    from hermes_state import SessionDB

    home = tmp_path / ".hermes"
    profile_home = home / "profiles" / TARGET_PROFILE
    profile_home.mkdir(parents=True)

    session_db = SessionDB(db_path=profile_home / "state.db")
    session_db.create_session(session_id=TARGET_SESSION, source="cli")
    import sqlite3

    conn = sqlite3.connect(str(profile_home / "state.db"))
    conn.execute(
        "UPDATE sessions SET session_key=?, chat_id=?, chat_type=?, thread_id=? "
        "WHERE id=?",
        (
            f"agent:main:telegram:group:{CHAT_ID}:{THREAD_ID}",
            CHAT_ID,
            "group",
            THREAD_ID,
            TARGET_SESSION,
        ),
    )
    conn.commit()
    conn.close()
    session_db.close()

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        "hermes_cli.profiles.profile_exists", lambda name: name == TARGET_PROFILE
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir", lambda _name: profile_home
    )
    monkeypatch.setenv("USERNAME", "zephyr")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    return profile_home


def _capture_tell_spawn(monkeypatch):
    """Capture the `hermes send -t` argv tell_tool runs.

    tell_tool now VERIFIES delivery synchronously — the spawn-and-discard
    path is gone (Evan 2026-11-18: "fallbacks are evil" / dropped is never
    success). The fake completes with a CONFIRMED wake result; the argv
    assertions below still pin resolution + routing exactly as before.
    """
    import subprocess

    calls = []

    def fake_run(argv, **kw):
        calls.append([str(a) for a in argv])
        return subprocess.CompletedProcess(
            args=argv,
            returncode=0,
            stdout='{"success": true, "status": "spawned", "note": "wake spawned"}',
            stderr="",
        )

    monkeypatch.setattr("subprocess.run", fake_run)
    return calls


def _tell_wake_argv(wake_env, target_profile_env, monkeypatch):
    """Drive tell_tool with the named session and return the captured wake
    argv (L1: resolution + truthful dispatch)."""
    from tools.tell_tool import tell_tool

    calls = _capture_tell_spawn(monkeypatch)
    echoes = []

    result = json.loads(
        tell_tool(
            agent=TARGET_PROFILE,
            message="Wake me in that session.",
            session_id=TARGET_SESSION,
            origin_session_id=ORIGIN_SESSION,
            echo_callback=echoes.append,
        )
    )

    assert result == {
        "dispatched": True,
        "delivered": True,
        "status": "spawned",
        "note": "wake spawned",
    }
    assert echoes == ["zephyr: Wake me in that session."]
    assert len(calls) == 1
    argv = calls[0]
    assert argv[:4] == [
        "hermes",
        "send",
        "-t",
        f"{TARGET_PROFILE}:telegram:{CHAT_ID}:{THREAD_ID}",
    ]
    assert "Wake me in that session." in argv[4]
    return argv


def test_tell_named_session_resolves_to_chat_target_wake(
    wake_env, target_profile_env, monkeypatch
):
    """L1: the named session id resolves to the chat wake target and the
    wake argv carries the wrapped message."""
    argv = _tell_wake_argv(wake_env, target_profile_env, monkeypatch)
    assert argv[:4][3] == f"{TARGET_PROFILE}:telegram:{CHAT_ID}:{THREAD_ID}"


def test_tell_named_session_wake_lands_turn(
    wake_env, target_profile_env, monkeypatch, tmp_path
):
    """L2: the tell wake crosses the REAL cmd_send -> bridge -> intake and
    a TURN spawns on the target session."""
    runner, adapter, recorder = wake_env
    smt = patch_send_tool_cli_env(monkeypatch)
    monkeypatch.setattr(send_cmd, "_load_hermes_env", lambda: None)
    _capture_tell_spawn(monkeypatch)  # visible-copy spawn also captured

    argv = _tell_wake_argv(wake_env, target_profile_env, monkeypatch)
    # The tell spawn is detached; drive the same argv through the real CLI
    # wake path in-process (no hermes binary, no network).
    async def scenario():
        server, sock = await start_bridge_on(runner, monkeypatch, tmp_path)
        try:
            monkeypatch.setattr(
                smt, "bridge_socket_path", lambda *_a, **_k: str(sock)
            )
            code, out = await asyncio.to_thread(run_cli_send_in_process, argv)
            await asyncio.wait_for(recorder.done.wait(), timeout=15)
            return code, out
        finally:
            server.close()
            await server.wait_closed()

    code, out = asyncio.run(scenario())

    assert adapter.errors == [], [str(e) for e in adapter.errors]
    assert code == 0, f"cmd_send exited {code}: {out}"
    payload = json.loads(out)
    assert payload.get("success") is True, payload
    assert payload.get("status") == "spawned", payload
    assert len(recorder.turns) == 1, recorder.turns
    turn = recorder.turns[0]
    assert turn["source"].chat_id == CHAT_ID
    assert turn["source"].thread_id == THREAD_ID
    assert "Wake me in that session." in turn["message"]
    assert turn["session_key"].startswith("agent:main:telegram:group:")
