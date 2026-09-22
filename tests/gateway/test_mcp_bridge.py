"""Regression tests for cross-process ``hermes send -u`` injection."""

import asyncio
import json
import sys
from types import SimpleNamespace
from types import ModuleType
from typing import cast
from unittest.mock import AsyncMock, patch

import gateway.mcp_bridge as mcp_bridge
from gateway.config import Platform
from gateway.mcp_bridge import _handle_inject, _handle_send
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
    assert result == {"success": True, "queued": True}
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
        async def handle_message(self, event):
            captured.append(event)

    runner = SimpleNamespace(
        adapters={Platform.TELEGRAM: Adapter()},
        config=SimpleNamespace(get_home_channel=lambda _platform: None),
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

    assert writer.payload == {"ok": True}
    assert writer.closed is True
    assert len(captured) == 1
    event = captured[0]
    assert event.internal is False
    assert event.source.user_id == "evan"
    assert event.source.user_name == "Evan"
    assert event.source.chat_type == "forum"
    assert event.metadata["platform_user_id"] == "8900123006"