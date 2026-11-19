"""Regression tests for initial MCP failure ownership and teardown."""

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest


def _reset_mcp_state(mcp_tool) -> None:
    mcp_tool.shutdown_mcp_servers()
    with mcp_tool._lock:
        mcp_tool._servers.clear()
        mcp_tool._server_connecting.clear()
        mcp_tool._server_connect_errors.clear()


def _cleanup_mcp_state(mcp_tool, extra_servers=()) -> None:
    with mcp_tool._lock:
        loop = mcp_tool._mcp_loop
    if loop is not None and loop.is_running():
        for server in extra_servers:
            task = getattr(server, "_task", None)
            if task is not None and not task.done():
                mcp_tool._run_on_mcp_loop(server.shutdown, timeout=5)
    mcp_tool.shutdown_mcp_servers()
    with mcp_tool._lock:
        mcp_tool._servers.clear()
        mcp_tool._server_connecting.clear()
        mcp_tool._server_connect_errors.clear()


def test_initial_connect_failure_records_cooldown_and_no_parked_entry(
    monkeypatch, tmp_path
):
    """A failed initial connect must NOT leave a parked registry entry.

    #50394 design: the failing server is recorded via `_server_connect_errors`
    plus a per-server backoff cooldown so discovery does not re-spawn it on
    every pass. The server task terminates (no parked zombie), and shutdown
    drains whatever remains. (Pre-revert behavior parked the task in
    `_servers`; the cooldown design replaced that.)
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools import mcp_tool

    _reset_mcp_state(mcp_tool)
    created = []

    class _FailingServerTask(mcp_tool.MCPServerTask):
        def __init__(self, name):
            super().__init__(name)
            created.append(self)

        async def _run_stdio(self, config):
            raise ConnectionError("deterministic initial failure")

    monkeypatch.setattr(mcp_tool, "MCPServerTask", _FailingServerTask)
    monkeypatch.setattr(mcp_tool, "_MCP_AVAILABLE", True)
    monkeypatch.setattr(mcp_tool, "_MAX_INITIAL_CONNECT_RETRIES", 0)
    monkeypatch.setattr(mcp_tool, "_PARKED_RETRY_INTERVAL", 3600)

    try:
        assert mcp_tool.register_mcp_servers({
            "initial-failure": {"command": "unused", "connect_timeout": 5}
        }) == []

        assert len(created) == 1
        with mcp_tool._lock:
            assert "initial-failure" not in mcp_tool._servers
            assert "deterministic initial failure" in (
                mcp_tool._server_connect_errors["initial-failure"]
            )
            # #50394: the backoff cooldown must be armed so the next
            # discovery pass does not re-spawn the failing server.
            assert (
                mcp_tool._server_connect_retry_after["initial-failure"]
                > time.monotonic()
            )
        # The failing server parks its task for a later reconnect, but it is
        # NOT published in `_servers` — the cooldown owns its re-spawn.
        assert created[0]._task is not None
        assert not created[0]._task.done(), "recoverable failure parks its task"

        mcp_tool.shutdown_mcp_servers()
        assert created[0]._task.done(), "shutdown must drain the parked task"
        with mcp_tool._lock:
            assert mcp_tool._mcp_loop is None
            assert mcp_tool._mcp_thread is None
    finally:
        _cleanup_mcp_state(mcp_tool, created)


def test_initial_connect_failure_recovers_after_cooldown_clear(
    monkeypatch, tmp_path
):
    """A failed connect must recover once its cooldown is cleared.

    #50394 contract: the failed server is not parked in ``_servers``; the
    backoff cooldown suppresses re-spawn until it expires (or is cleared).
    After that, a subsequent ``register_mcp_servers`` connects and registers
    tools normally.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools import mcp_tool
    from tools.registry import ToolRegistry
    import tools.registry as registry_module

    _reset_mcp_state(mcp_tool)
    created = []
    backend_up = threading.Event()
    revived = threading.Event()
    state = {"transport_calls": 0, "tool_calls": 0}
    mock_registry = ToolRegistry()

    class _Session:
        async def call_tool(self, name, arguments):
            state["tool_calls"] += 1
            return SimpleNamespace(
                isError=False,
                content=[SimpleNamespace(text=f"revived:{arguments['value']}")],
                structuredContent=None,
            )

    class _RecoveringServerTask(mcp_tool.MCPServerTask):
        def __init__(self, name):
            super().__init__(name)
            created.append(self)

        async def _run_stdio(self, config):
            state["transport_calls"] += 1
            if not backend_up.is_set():
                raise ConnectionError("backend still booting")

            self.session = _Session()
            self._tools = [SimpleNamespace(
                name="ping",
                description="Return a deterministic revival result",
                inputSchema={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
            )]
            # Match the real transports: discovery runs before _ready is set.
            self._register_discovered_tools_if_needed()
            self._ready.set()
            revived.set()
            return await self._wait_for_lifecycle_event()

    monkeypatch.setattr(mcp_tool, "MCPServerTask", _RecoveringServerTask)
    monkeypatch.setattr(mcp_tool, "_MCP_AVAILABLE", True)
    monkeypatch.setattr(mcp_tool, "_MAX_INITIAL_CONNECT_RETRIES", 0)
    monkeypatch.setattr(mcp_tool, "_PARKED_RETRY_INTERVAL", 3600)
    monkeypatch.setattr(registry_module, "registry", mock_registry)

    config = {
        "recovering": {"command": "unused", "connect_timeout": 5}
    }

    try:
        assert mcp_tool.register_mcp_servers(config) == []
        assert len(created) == 1
        with mcp_tool._lock:
            assert "recovering" not in mcp_tool._servers
            assert "backend still booting" in (
                mcp_tool._server_connect_errors["recovering"]
            )

        # A second attempt while the cooldown is armed must NOT spawn a new
        # server task (#50394 restart storm).
        assert mcp_tool.register_mcp_servers(config) == []
        assert len(created) == 1, "cooldown must suppress re-spawn"

        # Cooldown cleared and the parked task drained => a fresh register
        # connects successfully and registers tools.
        backend_up.set()
        with mcp_tool._lock:
            mcp_tool._server_connect_retry_after.pop("recovering", None)
            mcp_tool._server_connect_failures.pop("recovering", None)
        mcp_tool.shutdown_mcp_servers()

        assert mcp_tool.register_mcp_servers(config) != []
        assert revived.wait(timeout=5), "revival never connected"
        assert len(created) == 2
        with mcp_tool._lock:
            assert "recovering" in mcp_tool._servers
            assert "recovering" not in mcp_tool._server_connect_errors
        assert state["transport_calls"] == 2

        entry = mock_registry.get_entry("mcp__recovering__ping")
        assert entry is not None
        assert entry.check_fn() is True
        assert json.loads(entry.handler({"value": "ok"})) == {
            "result": "revived:ok"
        }
        assert state["tool_calls"] == 1
    finally:
        _cleanup_mcp_state(mcp_tool, created)


def test_terminal_initial_failure_is_not_retained(monkeypatch, tmp_path):
    """A non-recoverable startup error must not leave a dead cache entry."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools import mcp_tool

    _reset_mcp_state(mcp_tool)
    created = []

    class _AuthFailingServerTask(mcp_tool.MCPServerTask):
        def __init__(self, name):
            super().__init__(name)
            created.append(self)

        async def _run_stdio(self, config):
            raise PermissionError("terminal authentication failure")

    monkeypatch.setattr(mcp_tool, "MCPServerTask", _AuthFailingServerTask)
    monkeypatch.setattr(mcp_tool, "_MCP_AVAILABLE", True)
    monkeypatch.setattr(mcp_tool, "_is_auth_error", lambda exc: True)

    try:
        assert mcp_tool.register_mcp_servers({
            "auth-failure": {"command": "unused", "connect_timeout": 5}
        }) == []
        assert len(created) == 1
        assert created[0]._task.done()
        with mcp_tool._lock:
            assert "auth-failure" not in mcp_tool._servers
            assert "terminal authentication failure" in (
                mcp_tool._server_connect_errors["auth-failure"]
            )
    finally:
        _cleanup_mcp_state(mcp_tool, created)


def test_standalone_failed_connect_is_reaped_without_global_owner(monkeypatch, tmp_path):
    """Probe-only _connect_server failures must not publish parked servers."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools import mcp_tool

    _reset_mcp_state(mcp_tool)
    created = []

    class _ProbeServerTask(mcp_tool.MCPServerTask):
        def __init__(self, name):
            super().__init__(name)
            created.append(self)

        async def _run_stdio(self, config):
            raise ConnectionError("probe target unavailable")

    monkeypatch.setattr(mcp_tool, "MCPServerTask", _ProbeServerTask)
    monkeypatch.setattr(mcp_tool, "_MAX_INITIAL_CONNECT_RETRIES", 0)
    monkeypatch.setattr(mcp_tool, "_PARKED_RETRY_INTERVAL", 3600)
    mcp_tool._ensure_mcp_loop()

    try:
        with pytest.raises(ConnectionError, match="probe target unavailable"):
            mcp_tool._run_on_mcp_loop(
                lambda: mcp_tool._connect_server(
                    "probe-only", {"command": "unused"}
                ),
                timeout=5,
            )

        assert len(created) == 1
        # Probe-only connects park their task too, but MUST NOT publish the
        # server in `_servers` or the connect-error maps.
        assert created[0]._task is not None
        with mcp_tool._lock:
            assert "probe-only" not in mcp_tool._servers
            assert "probe-only" not in mcp_tool._server_connect_errors
        mcp_tool.shutdown_mcp_servers()
        assert created[0]._task.done()
    finally:
        _cleanup_mcp_state(mcp_tool, created)
