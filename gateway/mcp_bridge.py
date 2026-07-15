"""
MCP bridge server — gateway-side handler for /tmp/hermes/mcp_bridge.sock.

The MCP server (gimp_mcp_server.py) runs as a gateway subprocess.  This
bridge lets it call adapter.watch() / adapter.write_watched() /
adapter.unwatch() on the gateway's event loop so socket lifecycle stays
owned by the gateway process.

Protocol (newline-delimited JSON):
  -> {"action": "watch",   "url": "unix:///tmp/prometheus/sess.sock", "session_id": "..."}
  <- {"ok": true, "handle": "abc123"}
  -> {"action": "write",  "handle": "abc123", "data": "<json line>", "msg_id": "..."}
  <- {"ok": true, "response": {"msg_id": "...", ...}}   (after plugin responds)
  -> {"action": "unwatch", "handle": "abc123"}
  <- {"ok": true}

For write actions, the bridge sends the command to the persistent peer
and waits for a response with a matching msg_id.  Commands on the
session socket are tagged with msg_id; responses from the plugin carry
the same msg_id back.  The bridge matches and returns the response.
"""

import asyncio
import json
import logging
import os
import socket as _socket

logger = logging.getLogger(__name__)

BRIDGE_SOCKET = "/tmp/hermes/mcp_bridge.sock"

# Map of session_id -> watch handle.
_session_handles: dict[str, str] = {}

# Per-handle response queues.  When a write command is sent with a
# msg_id, a future is stored here.  The watch callback routes any
# incoming data with a matching msg_id to this future.
# {handle: {msg_id: asyncio.Future}}
_response_queues: dict[str, dict[str, asyncio.Future]] = {}


async def start_bridge_server(runner) -> asyncio.AbstractServer:
    """Start the MCP bridge Unix socket server."""
    try:
        os.unlink(BRIDGE_SOCKET)
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(BRIDGE_SOCKET), exist_ok=True)

    async def _handle(reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        try:
            data = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=10.0)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            writer.close()
            await writer.wait_closed()
            return

        try:
            cmd = json.loads(data.decode("utf-8"))
        except json.JSONDecodeError:
            _respond(writer, {"error": "Invalid JSON"})
            return

        action = cmd.get("action", "")
        try:
            if action == "watch":
                _handle_watch(runner, cmd, writer)
            elif action == "unwatch":
                _handle_unwatch(runner, cmd, writer)
            elif action == "write":
                await _handle_write(runner, cmd, writer)
            elif action == "inject":
                await _handle_inject(runner, cmd, writer)
            else:
                _respond(writer, {"error": f"Unknown action: {action}"})
        except Exception as e:
            logger.debug("Bridge %s error: %s", action, e)
            _respond(writer, {"error": str(e)})

    server = await asyncio.start_unix_server(_handle, BRIDGE_SOCKET)
    logger.info("MCP bridge server listening on %s", BRIDGE_SOCKET)
    return server


def _respond(writer: asyncio.StreamWriter, payload: dict) -> None:
    """Send a JSON response line and close the writer."""
    try:
        writer.write((json.dumps(payload) + "\n").encode("utf-8"))
    except Exception:
        pass
    writer.close()


def _handle_watch(runner, cmd: dict, writer: asyncio.StreamWriter) -> None:
    """Forward watch to the Telegram adapter."""
    url = cmd.get("url", "")
    if not url:
        _respond(writer, {"error": "Missing url"})
        return

    from gateway.config import Platform
    adapter = runner.adapters.get(Platform.TELEGRAM)
    if adapter is None:
        _respond(writer, {"error": "No Telegram adapter available"})
        return

    handle = None

    def _on_data(data: bytes) -> None:
        """Watch callback: fires when plugin sends data on session socket.

        Handoff data (type=handoff) -> injected as MessageEvent.
        Command responses (with msg_id) -> routed to response queue.
        """
        try:
            payload = json.loads(data.decode("utf-8"))
        except Exception:
            return

        # Command response routing
        msg_id = payload.get("msg_id")
        if msg_id and handle:
            qs = _response_queues.get(handle, {})
            fut = qs.pop(msg_id, None)
            if fut and not fut.done():
                fut.set_result(payload)
                return
            # If no waiting future, fall through to handoff path

        # Handoff data -> agent wake event
        if payload.get("type") == "handoff" or not msg_id:
            try:
                from gateway.config import load_gateway_config
                cfg = load_gateway_config()
                home = cfg.get_home_channel(Platform.TELEGRAM)
                if not home:
                    return
                from gateway.session import SessionSource
                from gateway.platforms.base import MessageEvent, MessageType
                source = SessionSource(
                    platform=Platform.TELEGRAM,
                    chat_id=home.chat_id,
                    thread_id=home.thread_id,
                    chat_type="dm",
                )
                event = MessageEvent(
                    text=json.dumps(payload, indent=2, default=str),
                    source=source,
                    message_type=MessageType.TEXT,
                    internal=True,
                )
                asyncio.create_task(adapter.handle_message(event))
            except Exception:
                logger.debug("Bridge handoff injection failed", exc_info=True)

    handle = adapter.watch(url, _on_data)

    # Initialize response queue for this handle
    _response_queues[handle] = {}

    # Store session_id -> handle mapping
    session_id = cmd.get("session_id", "")
    if session_id:
        _session_handles[session_id] = handle

    _respond(writer, {"ok": True, "handle": handle})


def _handle_unwatch(runner, cmd: dict, writer: asyncio.StreamWriter) -> None:
    """Forward unwatch to the Telegram adapter."""
    handle = cmd.get("handle", "")
    if not handle:
        _respond(writer, {"error": "Missing handle"})
        return

    from gateway.config import Platform
    adapter = runner.adapters.get(Platform.TELEGRAM)
    if adapter is None:
        _respond(writer, {"error": "No Telegram adapter available"})
        return

    # Clean up mappings
    for sid, h in list(_session_handles.items()):
        if h == handle:
            del _session_handles[sid]
    _response_queues.pop(handle, None)

    adapter.unwatch(handle)
    _respond(writer, {"ok": True})


async def _handle_inject(runner, cmd: dict, writer: asyncio.StreamWriter) -> None:
    """Inject a message as an agent wake event via adapter.handle_message().

    Used by external processes (e.g. ``hermes send`` CLI) to route messages
    through the gateway's session manager when the in-process adapter is
    unavailable.  The message arrives as an ``internal=True`` MessageEvent so
    the agent sees it as a wake event in its active session.
    """
    platform_name = cmd.get("platform", "")
    chat_id = cmd.get("chat_id", "")
    text = cmd.get("text", "")
    thread_id = cmd.get("thread_id")

    if not platform_name or not chat_id or not text:
        _respond(writer, {"error": "Missing required fields: platform, chat_id, text"})
        return

    from gateway.config import Platform
    try:
        platform = Platform(platform_name)
    except (ValueError, KeyError):
        _respond(writer, {"error": f"Unknown platform: {platform_name}"})
        return

    adapter = runner.adapters.get(platform)
    if adapter is None:
        _respond(writer, {"error": f"No adapter for platform '{platform_name}'"})
        return

    try:
        from gateway.session import SessionSource
        from gateway.platforms.base import MessageEvent, MessageType
        from datetime import datetime

        session_source = SessionSource(
            platform=platform,
            chat_id=chat_id,
            thread_id=thread_id,
            chat_type="group",
        )
        notify_event = MessageEvent(
            text=text,
            source=session_source,
            message_type=MessageType.TEXT,
            internal=True,
            timestamp=datetime.now(),
        )
        await adapter.handle_message(notify_event)
        _respond(writer, {"ok": True})
    except asyncio.CancelledError:
        raise
    except Exception as e:
        _respond(writer, {"error": str(e)})


async def _handle_write(runner, cmd: dict, writer: asyncio.StreamWriter) -> None:
    """Write a drawing command to the persistent peer and wait for response.

    Expects cmd.data (JSON string with the command) and optionally
    cmd.msg_id (unique ID for response matching).  If no msg_id is
    provided, generates one.
    """
    handle = cmd.get("handle", "")
    if not handle:
        session_id = cmd.get("session_id", "")
        handle = _session_handles.get(session_id, "")
    if not handle:
        _respond(writer, {"error": "Missing handle or unknown session_id"})
        return

    data_str = cmd.get("data", "")
    if not data_str:
        _respond(writer, {"error": "Missing data"})
        return

    from gateway.config import Platform
    adapter = runner.adapters.get(Platform.TELEGRAM)
    if adapter is None:
        _respond(writer, {"error": "No Telegram adapter available"})
        return

    # Parse the command and inject msg_id if missing
    try:
        cmd_obj = json.loads(data_str)
    except json.JSONDecodeError:
        _respond(writer, {"error": "Invalid JSON data"})
        return

    msg_id = cmd.get("msg_id") or cmd_obj.get("msg_id")
    if not msg_id:
        import uuid
        msg_id = uuid.uuid4().hex[:8]
    cmd_obj["msg_id"] = msg_id

    # Create a future for the response
    qs = _response_queues.setdefault(handle, {})
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    qs[msg_id] = fut

    # Send the command
    data_bytes = (json.dumps(cmd_obj) + "\n").encode("utf-8")
    ok = adapter.write_watched(handle, data_bytes)
    if not ok:
        qs.pop(msg_id, None)
        _respond(writer, {"error": "Write failed — peer not connected"})
        return

    # Wait for response (with timeout)
    try:
        response = await asyncio.wait_for(fut, timeout=15.0)
        _respond(writer, {"ok": True, "response": response})
    except asyncio.TimeoutError:
        qs.pop(msg_id, None)
        _respond(writer, {"error": "Response timeout"})
