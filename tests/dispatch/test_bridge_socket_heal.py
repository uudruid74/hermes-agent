"""Bridge-socket self-heal cells — 2026-11-18-tmp-clean-deletes-gateway-bridge-sockets.

The nightly ``zephyr-tmp-clean`` timer deleted ``/tmp/hermes`` while all five
gateways kept running: the listeners survived on unlinked inodes, so
``ss -xlp`` still showed the paths but every external ``connect()`` died with
ENOENT.  ``hermes send -t`` reported "Bridge socket not found"; the tell tool
swallowed it.  Rows landed, wakes died, nothing recovered until a human
restarted the gateways.

These cells lock the recovery seam (follow-ups #2 and #3 of the bug file):

- Cell A (L2, the bug's requested cell): the socket path vanishes while the
  gateway runs -> the gateway re-binds -> an EXTERNAL sender
  (``send_message_tool``, i.e. the ``hermes send -t`` leg) connects to the live
  path, reaches the real intake, and the turn spawns.
- Cell B: healing never STEALS a socket it does not own.  If the path is
  owned by another live listener, we log and leave it alone.
- Cell C: a stale dead socket file (listener gone, entry left behind) is
  replaced, not trusted.

The assertion is the TURN, not the file (CONTRACT.md law 2).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import socket as _socket

import gateway.mcp_bridge as mcp_bridge
from tests.dispatch.wake_harness import patch_send_tool_cli_env


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _wait_for(predicate, timeout=5.0, interval=0.02):
    """Poll ``predicate`` until true or fail the test with a clear message."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition not met within %.1fs" % timeout)


def _connect_ok(path: str) -> bool:
    """True only when a server ACCEPTS a connection on ``path``."""
    try:
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as sock:
            sock.settimeout(2.0)
            sock.connect(path)
        return True
    except OSError:
        return False


async def _stop_task(task: asyncio.Task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------
# Cell A — vanish -> rebind -> external wake reaches intake -> turn
# ---------------------------------------------------------------------------


def test_socket_dir_deleted_while_running_rebinds_and_external_wake_turns(
    wake_env, monkeypatch, tmp_path
):
    """The tmp-clean incident, end to end.

    A running gateway loses its bridge socket directory (the nightly cleaner),
    re-binds without a restart, and an external ``hermes send -t`` reaches the
    real intake and spawns a turn — the exact chain that broke on 2026-11-18.
    """
    smt = patch_send_tool_cli_env(monkeypatch)
    runner, adapter, recorder = wake_env

    sock = tmp_path / "hermes" / "mcp_bridge.test.sock"
    monkeypatch.setattr(mcp_bridge, "BRIDGE_SOCKET", str(sock))

    async def scenario():
        heal = asyncio.create_task(
            mcp_bridge.run_bridge_server(runner, check_interval=0.05)
        )
        try:
            await _wait_for(lambda: sock.exists())
            assert _connect_ok(str(sock)), "bridge should be live before the deletion"

            # Simulate `rm -rf /tmp/hermes` — the dir AND the socket go away
            # while the gateway process keeps running.
            shutil.rmtree(sock.parent)
            assert not sock.exists()

            # No restart, no human: the gateway must put the socket back.
            await _wait_for(lambda: sock.exists())
            await _wait_for(lambda: _connect_ok(str(sock)))

            monkeypatch.setattr(
                smt, "bridge_socket_path", lambda *_a, **_k: str(sock)
            )
            # The sender is an external process — run it off this loop while
            # the bridge serves the healed socket on the main loop.
            result = await asyncio.to_thread(
                smt.send_message_tool,
                {
                    "action": "send",
                    "target": "telegram:8900123006",
                    "message": "wake after the cleaner ate the socket",
                    "internal": True,
                },
            )
            await asyncio.wait_for(recorder.done.wait(), timeout=15)
            return json.loads(result)
        finally:
            await _stop_task(heal)

    result = asyncio.run(scenario())

    assert adapter.errors == [], [str(e) for e in adapter.errors]
    assert result.get("success") is True, result
    assert result.get("status") == "spawned", result
    assert len(recorder.turns) == 1, "the healed socket must reach the intake"
    assert (
        recorder.turns[0]["message"] == "wake after the cleaner ate the socket"
    )


def test_heal_recreates_socket_when_only_the_socket_file_is_unlinked(
    wake_env, monkeypatch, tmp_path
):
    """Unlinking just the socket (dir intact) heals too — the real incident's
    first observable state: path gone, listener alive on the dead inode."""
    runner, adapter, recorder = wake_env

    sock = tmp_path / "hermes" / "mcp_bridge.test.sock"
    monkeypatch.setattr(mcp_bridge, "BRIDGE_SOCKET", str(sock))

    async def scenario():
        heal = asyncio.create_task(
            mcp_bridge.run_bridge_server(runner, check_interval=0.05)
        )
        try:
            await _wait_for(lambda: sock.exists())
            os.unlink(sock)
            await _wait_for(lambda: sock.exists())
            await _wait_for(lambda: _connect_ok(str(sock)))
        finally:
            await _stop_task(heal)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Cell B — never steal a socket owned by another live listener
# ---------------------------------------------------------------------------


def test_heal_does_not_steal_a_live_socket_it_does_not_own(
    wake_env, monkeypatch, tmp_path, caplog
):
    """A live foreign listener on the path must be left alone.

    Re-binding would silently steal the endpoint from a process that owns it,
    which is worse than a stale path: sends would land in the wrong gateway.
    """
    caplog.set_level("DEBUG", logger="gateway.mcp_bridge")
    runner, adapter, recorder = wake_env

    sock = tmp_path / "hermes" / "mcp_bridge.test.sock"
    monkeypatch.setattr(mcp_bridge, "BRIDGE_SOCKET", str(sock))

    async def scenario():
        ours = await mcp_bridge.start_bridge_server(runner)
        our_inode = os.stat(sock).st_ino
        try:
            # Our listener loses the path; another live server takes it.
            os.unlink(sock)
            foreign_connections = []

            async def _foreign(reader, writer):
                foreign_connections.append(True)
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()

            foreign = await asyncio.start_unix_server(_foreign, str(sock))
            foreign_inode = os.stat(sock).st_ino
            try:
                new_server, new_inode = await mcp_bridge._rebind_if_needed(
                    runner, ours, our_inode
                )
                await asyncio.sleep(0.1)

                assert new_server is ours, "a live foreign socket must not be replaced"
                assert new_inode == our_inode
                assert os.stat(sock).st_ino == foreign_inode
                assert _connect_ok(str(sock))
                assert foreign_connections, "the foreign listener must still serve"
                assert "not stealing" in caplog.text.lower()
            finally:
                foreign.close()
                await foreign.wait_closed()
        finally:
            ours.close()
            await ours.wait_closed()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Cell C — a stale dead socket entry is replaced, not trusted
# ---------------------------------------------------------------------------


def test_heal_replaces_a_stale_dead_socket_entry(
    wake_env, monkeypatch, tmp_path
):
    """A path that exists but refuses connections is dead — rebind it."""
    runner, adapter, recorder = wake_env

    sock = tmp_path / "hermes" / "mcp_bridge.test.sock"
    monkeypatch.setattr(mcp_bridge, "BRIDGE_SOCKET", str(sock))

    async def scenario():
        heal = asyncio.create_task(
            mcp_bridge.run_bridge_server(runner, check_interval=0.05)
        )
        try:
            await _wait_for(lambda: _connect_ok(str(sock)))
            # Replace our live path with a dead socket entry of the same name:
            # a listener that bound and closed leaves the file behind.
            os.unlink(sock)
            dead = await asyncio.start_unix_server(
                lambda r, w: w.close(), str(sock)
            )
            dead.close()
            await dead.wait_closed()
            assert sock.exists() and not _connect_ok(str(sock))

            await _wait_for(lambda: _connect_ok(str(sock)))
        finally:
            await _stop_task(heal)

    asyncio.run(scenario())
