"""Regression tests for cross-process ``hermes send -t`` injection."""

import asyncio
import json
import sys
from types import SimpleNamespace
from types import ModuleType
from typing import cast
from unittest.mock import AsyncMock, patch

import gateway.mcp_bridge as mcp_bridge
from gateway.config import Platform
from gateway.mcp_bridge import (
    BRIDGE_MAX_PAYLOAD_BYTES,
    _handle_inject,
    _handle_send,
    _read_command_line,
)
from gateway.session import build_session_key
from tools.send_message_tool import (
    _send_delivery_via_bridge,
    _send_via_bridge,
    send_message_tool,
)


class _Writer:
    def __init__(self):
        self.payload = None
        self.closed = False

    def write(self, data):
        self.payload = json.loads(data.decode("utf-8"))

    def close(self):
        self.closed = True


def _run_async_immediately(coro):
    return asyncio.run(coro)


def test_internal_send_to_explicit_telegram_dm_uses_dm_chat_type(monkeypatch):
    telegram_cfg = SimpleNamespace(enabled=True, token="token", extra={}, home_channel=SimpleNamespace(chat_type="forum"))
    config = SimpleNamespace(
        platforms={Platform.TELEGRAM: telegram_cfg},
        get_home_channel=lambda _platform: telegram_cfg.home_channel,
    )
    bridge = AsyncMock(return_value={"success": True, "queued": True})
    direct_send = AsyncMock(side_effect=AssertionError("-u must not send to the platform"))
    fake_gateway_run = ModuleType("gateway.run")
    setattr(fake_gateway_run, "_gateway_runner_ref", lambda: None)
    monkeypatch.setitem(sys.modules, "gateway.run", fake_gateway_run)

    def session_env(name, default=""):
        return {
            "HERMES_SESSION_USER_ID": "operator-id",
            "HERMES_SESSION_TELEGRAM_ID": "telegram-user-id",
            "HERMES_SESSION_USER_NAME": "Operator",
        }.get(name, default)

    with patch("gateway.config.load_gateway_config", return_value=config), \
         patch("tools.interrupt.is_interrupted", return_value=False), \
         patch("gateway.session_context.get_session_env", side_effect=session_env), \
         patch("model_tools._run_async", side_effect=_run_async_immediately), \
         patch("tools.send_message_tool._send_via_bridge", bridge), \
         patch("tools.send_message_tool._send_to_platform", direct_send):
        result = json.loads(
            send_message_tool(
                {
                    "action": "send",
                    "target": "telegram:8900123006",
                    "message": "test",
                    "internal": True,
                }
            )
        )

    assert result == {"success": True, "queued": True}
    direct_send.assert_not_awaited()
    bridge.assert_awaited_once()
    bridge_call = bridge.await_args
    assert bridge_call is not None
    assert bridge_call.kwargs["user_context"] == {
        "user_id": "operator-id",
        "platform_user_id": "telegram-user-id",
        "sender_name": "Operator",
        "chat_type": "dm",
    }


async def _bridge_payload_with_user_identity(monkeypatch):
    class FakeSocket:
        def __init__(self, *_args):
            self.payload = None
            self.connected_path = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def settimeout(self, _timeout):
            return None

        def connect(self, path):
            self.connected_path = path

        def sendall(self, payload):
            self.payload = json.loads(payload.decode("utf-8"))

        def recv(self, _size):
            return b'{"ok": true}\n'

    fake_socket = FakeSocket()
    monkeypatch.setattr("socket.socket", lambda *_args: fake_socket)
    result = await _send_via_bridge(
        Platform.TELEGRAM,
        "8900123006",
        "test",
        user_context={
            "user_id": "evan",
            "platform_user_id": "8900123006",
            "sender_name": "Evan",
            "chat_type": "forum",
        },
        bridge_path="/tmp/hermes/mcp_bridge.zephyr.sock",
    )
    assert result == {"success": True, "queued": False}
    assert fake_socket.connected_path == "/tmp/hermes/mcp_bridge.zephyr.sock"
    assert fake_socket.payload is not None
    assert fake_socket.payload["user_context"]["platform_user_id"] == "8900123006"
    assert fake_socket.payload["chat_type"] == "forum"


def test_bridge_payload_carries_user_identity_and_chat_type(monkeypatch):
    asyncio.run(_bridge_payload_with_user_identity(monkeypatch))


async def _delivery_bridge_payload(monkeypatch):
    class FakeSocket:
        def __init__(self, *_args):
            self.payload = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def settimeout(self, _timeout):
            return None

        def connect(self, _path):
            return None

        def sendall(self, payload):
            self.payload = json.loads(payload.decode("utf-8"))

        def recv(self, _size):
            return b'{"ok": true, "queued": true}\n'

    fake_socket = FakeSocket()
    monkeypatch.setattr("socket.socket", lambda *_args: fake_socket)

    result = await _send_delivery_via_bridge(
        Platform.TELEGRAM,
        "8900123006",
        "task done",
        thread_id="456",
        bridge_path="/tmp/hermes/mcp_bridge.zephyr.sock",
    )

    assert result == {"success": True, "queued": True}
    assert fake_socket.payload == {
        "action": "send",
        "platform": "telegram",
        "chat_id": "8900123006",
        "text": "task done",
        "thread_id": "456",
    }


def test_delivery_bridge_queues_platform_send(monkeypatch):
    asyncio.run(_delivery_bridge_payload(monkeypatch))


async def _queued_bridge_send():
    adapter = SimpleNamespace(
        send=AsyncMock(
            return_value=SimpleNamespace(success=True, message_id="m1", error=None)
        )
    )
    runner = SimpleNamespace(
        adapters={Platform.TELEGRAM: adapter},
        _background_tasks=set(),
    )
    writer = _Writer()

    await _handle_send(
        runner,
        {
            "platform": "telegram",
            "chat_id": "8900123006",
            "text": "task done",
            "thread_id": "456",
        },
        cast(asyncio.StreamWriter, writer),
    )

    assert writer.payload == {"ok": True, "queued": True}
    assert writer.closed is True
    tasks = list(runner._background_tasks)
    assert len(tasks) == 1
    await asyncio.gather(*tasks)
    adapter.send.assert_awaited_once_with(
        chat_id="8900123006",
        content="task done",
        metadata={"thread_id": "456"},
    )


def test_bridge_send_acknowledges_before_adapter_delivery():
    asyncio.run(_queued_bridge_send())


# ---------------------------------------------------------------------------
# Kanban delivery ordering (t_d979ec8d)
# ---------------------------------------------------------------------------


def _ordering_runner(adapter):
    return SimpleNamespace(
        adapters={Platform.TELEGRAM: adapter},
        _background_tasks=set(),
    )


async def _ordered_send(runner, text, task_id, seq):
    await _handle_send(
        runner,
        {
            "platform": "telegram",
            "chat_id": "8900123006",
            "text": text,
            "ordering": {"task_id": task_id, "seq": seq, "status": "x"},
        },
        cast(asyncio.StreamWriter, _Writer()),
    )


def test_bridge_kanban_ordering_drops_stale_notifications():
    """A subscriber must never receive a state older than one already sent.

    A lower-seq notification that arrives AFTER a higher-seq one was
    delivered (the T7 late-and-reordered bug) is dropped at the gateway.
    """
    mcp_bridge._kanban_delivery_states.clear()
    delivered: list[str] = []

    async def fake_send(**kwargs):
        delivered.append(kwargs["content"])
        return SimpleNamespace(success=True, message_id="m1", error=None)

    runner = _ordering_runner(SimpleNamespace(send=fake_send))

    async def scenario():
        await _ordered_send(runner, "done", "t_order", 5)
        await asyncio.gather(*list(runner._background_tasks))
        # Late straggler for an older state — must be dropped.
        await _ordered_send(runner, "ready", "t_order", 3)
        await asyncio.gather(*list(runner._background_tasks))

    asyncio.run(scenario())
    assert delivered == ["done"]


def test_bridge_kanban_ordering_keeps_event_order():
    mcp_bridge._kanban_delivery_states.clear()
    delivered: list[str] = []

    async def fake_send(**kwargs):
        delivered.append(kwargs["content"])
        return SimpleNamespace(success=True, message_id="m1", error=None)

    runner = _ordering_runner(SimpleNamespace(send=fake_send))

    async def scenario():
        for seq, text in ((1, "ready"), (2, "running"), (3, "done")):
            await _ordered_send(runner, text, "t_seq", seq)
        await asyncio.gather(*list(runner._background_tasks))

    asyncio.run(scenario())
    assert delivered == ["ready", "running", "done"]


def test_bridge_kanban_ordering_serializes_same_task_deliveries():
    """A slow delivery must not be overtaken by a newer one for the same task.

    Without the per-task serialization the fire-and-forget ``create_task``
    fan-out lets the fast send complete first — delivery order flips even
    when arrival order was correct.
    """
    mcp_bridge._kanban_delivery_states.clear()
    delivered: list[str] = []
    release = asyncio.Event()

    async def gated_send(**kwargs):
        if kwargs["content"] == "running":
            await release.wait()
        delivered.append(kwargs["content"])
        return SimpleNamespace(success=True, message_id="m1", error=None)

    runner = _ordering_runner(SimpleNamespace(send=gated_send))

    async def scenario():
        await _ordered_send(runner, "running", "t_race", 2)  # slow delivery
        await _ordered_send(runner, "done", "t_race", 3)     # fast delivery
        tasks = list(runner._background_tasks)
        assert len(tasks) == 2
        # Let both run: 'done' must NOT overtake the stalled 'running'.
        await asyncio.sleep(0.01)
        assert delivered == []          # both still queued behind the gate
        release.set()
        await asyncio.gather(*tasks)

    asyncio.run(scenario())
    assert delivered == ["running", "done"]


def test_bridge_delivery_ordering_stamp_reaches_payload(monkeypatch):
    """The kanban notify hook stamps {task_id, seq, status} onto the bridge
    payload so the gateway guard can do its job."""

    async def _payload_with_ordering(monkeypatch):
        captured = {}

        async def fake_bridge_command(payload, *, bridge_path, operation):
            captured.update(payload)
            return {"success": True, "queued": True}

        monkeypatch.setattr(
            "tools.send_message_tool._send_bridge_command", fake_bridge_command
        )
        result = await _send_delivery_via_bridge(
            Platform.TELEGRAM,
            "8900123006",
            "task done",
            bridge_path="/tmp/hermes/mcp_bridge.zephyr.sock",
            ordering={"task_id": "t_1", "seq": 7, "status": "done"},
        )
        assert result == {"success": True, "queued": True}
        assert captured["ordering"] == {"task_id": "t_1", "seq": 7, "status": "done"}

    asyncio.run(_payload_with_ordering(monkeypatch))


async def _failed_bridge_send():
    adapter = SimpleNamespace(
        send=AsyncMock(
            return_value=SimpleNamespace(
                success=False,
                message_id=None,
                error="transport unavailable",
            )
        )
    )
    runner = SimpleNamespace(
        adapters={Platform.TELEGRAM: adapter},
        _background_tasks=set(),
    )

    await _handle_send(
        runner,
        {
            "platform": "telegram",
            "chat_id": "8900123006",
            "text": "task done",
        },
        cast(asyncio.StreamWriter, _Writer()),
    )
    await asyncio.gather(*list(runner._background_tasks))


def test_bridge_send_logs_background_delivery_failure(caplog):
    caplog.set_level("ERROR", logger="gateway.mcp_bridge")
    asyncio.run(_failed_bridge_send())
    assert "Bridge outbound delivery failed" in caplog.text
    assert "transport unavailable" in caplog.text


async def _real_socket_delivery(monkeypatch, tmp_path):
    bridge_path = str(tmp_path / "mcp_bridge.test.sock")
    monkeypatch.setattr(mcp_bridge, "BRIDGE_SOCKET", bridge_path)
    release = asyncio.Event()

    async def delayed_send(**_kwargs):
        await release.wait()
        return SimpleNamespace(success=True, message_id="m1", error=None)

    adapter = SimpleNamespace(send=AsyncMock(side_effect=delayed_send))
    runner = SimpleNamespace(
        adapters={Platform.TELEGRAM: adapter},
        _background_tasks=set(),
    )
    server = await mcp_bridge.start_bridge_server(runner)
    try:
        result = await asyncio.to_thread(
            asyncio.run,
            _send_delivery_via_bridge(
                Platform.TELEGRAM,
                "8900123006",
                "task done",
                bridge_path=bridge_path,
            ),
        )

        assert result == {"success": True, "queued": True}
        assert len(runner._background_tasks) == 1
        release.set()
        await asyncio.gather(*list(runner._background_tasks))
        adapter.send.assert_awaited_once_with(
            chat_id="8900123006",
            content="task done",
            metadata=None,
        )
    finally:
        server.close()
        await server.wait_closed()


def test_bridge_server_routes_delivery_without_waiting_for_adapter(
    monkeypatch, tmp_path
):
    asyncio.run(_real_socket_delivery(monkeypatch, tmp_path))


def test_inject_with_user_context_creates_real_inbound_user_event():
    captured = []

    class Adapter:
        def __init__(self):
            self._active_sessions = {}

        async def handle_message(self, event):
            captured.append(event)
            self._active_sessions[_session_key_for(event)] = asyncio.Event()

    runner = SimpleNamespace(
        adapters={Platform.TELEGRAM: Adapter()},
        config=SimpleNamespace(get_home_channel=lambda _platform: None),
        session_store=SimpleNamespace(get_or_create_session=lambda _s: None),
    )
    writer = _Writer()

    asyncio.run(
        _handle_inject(
            runner,
            {
                "platform": "telegram",
                "chat_id": "8900123006",
                "text": "test",
                "chat_type": "forum",
                "user_context": {
                    "user_id": "evan",
                    "platform_user_id": "8900123006",
                    "sender_name": "Evan",
                },
            },
            cast(asyncio.StreamWriter, writer),
        )
    )

    assert writer.payload == {"ok": True, "status": "spawned"}
    assert writer.closed is True
    assert len(captured) == 1
    event = captured[0]
    assert event.internal is False
    assert event.source.user_id == "evan"
    assert event.source.user_name == "Evan"
    assert event.source.chat_type == "forum"
    assert event.metadata["platform_user_id"] == "8900123006"

# ---------------------------------------------------------------------------
# Wake-intake outcome reporting — 2026-11-16-tell-wake-delivered-no-turn
# ---------------------------------------------------------------------------
# A bridge inject MUST NOT ack "ok" as if a turn spawned when the wake was
# only queued behind a busy session — or dropped entirely by an adapter that
# returned without starting processing. These tests lock that contract so a
# "delivered but no wake turn" loss is visible to the caller instead of
# silent (3rd occurrence of the class in one month).


def _session_key_for(event):
    return build_session_key(
        event.source,
        group_sessions_per_user=True,
        thread_sessions_per_user=False,
    )


def _runner_with_adapter(adapter, store=None):
    return SimpleNamespace(
        adapters={Platform.TELEGRAM: adapter},
        config=SimpleNamespace(get_home_channel=lambda _platform: None),
        session_store=store or SimpleNamespace(get_or_create_session=lambda _s: None),
    )


def _inject_payload(**overrides):
    payload = {
        "platform": "telegram",
        "chat_id": "8900123006",
        "text": "Incoming message from zephyr follows: --- test",
        "chat_type": "dm",
        "user_context": {
            "user_id": "evan",
            "platform_user_id": "8900123006",
            "sender_name": "CLI User",
        },
    }
    payload.update(overrides)
    return payload


def test_inject_into_idle_session_reports_spawned():
    """A wake delivered to an idle session must report the turn as spawned."""

    captured = []

    class Adapter:
        def __init__(self):
            self._active_sessions = {}

        async def handle_message(self, event):
            captured.append(event)
            # Real handle_message installs the session guard before spawning
            # the background processing task.
            self._active_sessions["agent:main:telegram:dm:8900123006"] = (
                asyncio.Event()
            )

    writer = _Writer()
    asyncio.run(
        _handle_inject(
            _runner_with_adapter(Adapter()),
            _inject_payload(),
            cast(asyncio.StreamWriter, writer),
        )
    )

    assert writer.payload is not None
    assert writer.payload["ok"] is True
    assert writer.payload["status"] == "spawned"
    assert len(captured) == 1


def test_inject_into_busy_session_reports_queued():
    """A wake delivered into an ACTIVE session must report it as queued.

    The wake is not lost — it lands in the session's pending-message queue
    and runs when the current turn finishes — but the caller must be told
    no turn started *yet*, so a stuck busy session cannot silently swallow
    agent-to-agent tells.
    """

    captured = []

    class Adapter:
        def __init__(self):
            self._active_sessions = {
                "agent:main:telegram:dm:8900123006": asyncio.Event()
            }

        async def handle_message(self, event):
            captured.append(event)  # busy path: queued, no new turn spawned

    writer = _Writer()
    asyncio.run(
        _handle_inject(
            _runner_with_adapter(Adapter()),
            _inject_payload(),
            cast(asyncio.StreamWriter, writer),
        )
    )

    assert writer.payload is not None
    assert writer.payload["ok"] is True
    assert writer.payload["status"] == "queued"
    assert len(captured) == 1


def test_inject_when_adapter_noops_reports_dropped():
    """A wake the adapter silently swallows (no handler / no processing task)
    must report ``dropped`` so the sender surfaces a real failure instead of
    a false "dispatched" success.
    """

    captured = []

    class Adapter:
        def __init__(self):
            self._active_sessions = {}

        async def handle_message(self, event):
            # Simulates the silent early-return path: the event is accepted
            # but no session guard is installed and no task is spawned.
            captured.append(event)

    writer = _Writer()
    asyncio.run(
        _handle_inject(
            _runner_with_adapter(Adapter()),
            _inject_payload(),
            cast(asyncio.StreamWriter, writer),
        )
    )

    assert writer.payload is not None
    assert writer.payload["ok"] is True
    assert writer.payload["status"] == "dropped"
    assert len(captured) == 1


def _patch_fake_telegram_config(monkeypatch):
    """Give send_message_tool a configured-but-fake telegram platform so the
    internal wake branch is reached without real credentials.

    ``_handle_send`` imports ``load_gateway_config`` locally, so the module
    under test resolves it from ``gateway.config`` at call time.
    """
    from tools import send_message_tool as smt

    fake_pconfig = SimpleNamespace(enabled=True, token="x", extra={})
    fake_config = SimpleNamespace(
        platforms={Platform.TELEGRAM: fake_pconfig},
        get_home_channel=lambda _p: None,
    )
    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: fake_config)
    return smt


def test_send_message_tool_surfaces_queued_wake_status(monkeypatch):
    """The sender-side bridge result must carry the wake intake outcome."""

    smt = _patch_fake_telegram_config(monkeypatch)

    async def _fake_bridge(*_a, **_kw):
        return {"success": True, "queued": True, "status": "queued"}

    monkeypatch.setattr(smt, "_send_via_bridge", _fake_bridge)
    result = json.loads(
        smt.send_message_tool(
            {
                "action": "send",
                "target": "telegram:8900123006",
                "message": "test",
                "internal": True,
            }
        )
    )
    assert result["success"] is True
    assert result["status"] == "queued"
    assert "queued behind active session" in result.get("note", "")


def test_send_message_tool_fails_loudly_on_dropped_wake(monkeypatch):
    """A dropped wake must become an ERROR, never a false success."""

    smt = _patch_fake_telegram_config(monkeypatch)

    async def _fake_bridge(*_a, **_kw):
        return {"success": True, "queued": False, "status": "dropped"}

    monkeypatch.setattr(smt, "_send_via_bridge", _fake_bridge)
    result = json.loads(
        smt.send_message_tool(
            {
                "action": "send",
                "target": "telegram:8900123006",
                "message": "test",
                "internal": True,
            }
        )
    )
    assert "error" in result
    assert "dropped" in result["error"]


# ---------------------------------------------------------------------------
# Framing limit — t_94b593b6 (large wake payloads must survive the bridge)
# ---------------------------------------------------------------------------

def test_inject_into_fresh_topic_binds_session_before_delivery(monkeypatch, tmp_path):
    """Regression for 2026-11-16-topic-wake-no-bound-session.

    A wake targeted at a chat/thread with NO existing session must create/
    bind the target session at intake, so the injected turn actually lands
    instead of posting into the void ("Mirror: no session found") and
    stranding the claimed task until an unrelated reclaim tick.
    """
    import hermes_state as hermes_state_mod
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore

    monkeypatch.setattr(hermes_state_mod, "DEFAULT_DB_PATH", tmp_path / "state.db")
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())

    captured = []

    class Adapter:
        def __init__(self):
            self._active_sessions = {}

        async def handle_message(self, event):
            captured.append(event)
            self._active_sessions[_session_key_for(event)] = asyncio.Event()

    runner = SimpleNamespace(
        adapters={Platform.TELEGRAM: Adapter()},
        config=SimpleNamespace(get_home_channel=lambda _platform: None),
        session_store=store,
    )
    writer = _Writer()

    asyncio.run(
        _handle_inject(
            runner,
            {
                "platform": "telegram",
                "chat_id": "-1004406953125",
                "text": "work kanban task t_test123",
                "thread_id": "6",
                "chat_type": "group",
                "user_context": {
                    "user_id": "cli",
                    "platform_user_id": "cli",
                    "sender_name": "CLI User",
                },
            },
            cast(asyncio.StreamWriter, writer),
        )
    )

    assert writer.payload == {"ok": True, "status": "spawned"}
    assert len(captured) == 1
    assert captured[0].source.chat_type == "group"
    assert captured[0].source.thread_id == "6"
    # The intake must have created a LIVE session bound to the topic origin
    # before delivery — the mirror/visible-copy then lands in it.
    assert store._db is not None
    sid = store._db.find_session_by_origin(
        platform="telegram",
        chat_id="-1004406953125",
        thread_id="6",
    )
    assert sid is not None


def test_bridge_reader_reads_over_64k_payloads():
    """A 106K-char wake brief (the gopher journal defect) frames cleanly."""
    payload = json.dumps({"action": "send", "text": "B" * (106 * 1024)}).encode() + b"\n"
    reader = asyncio.StreamReader()
    reader.feed_data(payload)
    reader.feed_eof()
    assert asyncio.run(_read_command_line(reader, timeout=5.0)) == payload


def test_bridge_reader_is_bounded_by_max_bytes():
    payload = json.dumps({"action": "x", "text": "Y" * 300_000}).encode() + b"\n"
    reader = asyncio.StreamReader()
    reader.feed_data(payload)
    reader.feed_eof()
    assert asyncio.run(_read_command_line(reader, timeout=5.0, max_bytes=100)) is None


def test_bridge_default_cap_is_above_the_64k_line_limit():
    assert BRIDGE_MAX_PAYLOAD_BYTES > 64 * 1024
    assert BRIDGE_MAX_PAYLOAD_BYTES <= 1024 * 1024
