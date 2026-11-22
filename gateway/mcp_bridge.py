"""
MCP bridge server — gateway-side handler for the profile-scoped
``/tmp/hermes/mcp_bridge.<profile>.sock`` endpoint.

The MCP server (gimp_mcp_server.py) runs as a gateway subprocess.  This
bridge lets it call adapter.watch() / adapter.write_watched() /
adapter.unwatch() on the gateway's event loop so socket lifecycle stays
owned by the gateway process.

Protocol (newline-delimited JSON):
  -> {"action": "watch",   "url": "unix:///tmp/cave-painter/sess.sock", "session_id": "..."}
  <- {"ok": true, "handle": "abc123"}
  -> {"action": "write",  "handle": "abc123", "data": "<json line>", "msg_id": "..."}
  <- {"ok": true, "response": {"msg_id": "...", ...}}   (after plugin responds)
  -> {"action": "unwatch", "handle": "abc123"}
  <- {"ok": true}
  -> {"action": "send", "platform": "telegram", "chat_id": "...", "text": "..."}
  <- {"ok": true, "queued": true}

Payload framing: the bridge reads one newline-delimited JSON command per
connection with an EXPLICIT byte cap (:data:`BRIDGE_MAX_PAYLOAD_BYTES`),
never relying on the ``asyncio.StreamReader`` default line limit.  The
default 64 KiB reader limit used to raise ``LimitOverrunError`` on a large
wake payload (the 106K-char cron journal brief, t_94b593b6) and the
exception propagated out of ``_handle`` as "Unhandled exception in
client_connected_cb" — killing the connection before it answered, so the
injection silently never happened.  The cap is generous (256 KiB) and
still bounded, so a rogue peer cannot balloon gateway memory.
"""

import asyncio
import json
import logging
import os
import socket as _socket
from collections import OrderedDict

# Largest single JSON command the bridge accepts (bytes, incl. newline).
# 256 KiB is far above any real wake/brief and still bounded.  Must match
# the reader loop in ``_read_command_line``.
BRIDGE_MAX_PAYLOAD_BYTES = 256 * 1024
BRIDGE_READ_CHUNK = 64 * 1024

logger = logging.getLogger(__name__)


def bridge_socket_path(hermes_home=None) -> str:
    """Return the Unix socket path dedicated to one Hermes profile.

    A host may run several profile gateways simultaneously. Deriving the
    socket name from the canonical Hermes home keeps a CLI subprocess talking
    to its own profile's gateway instead of whichever gateway last rebound the
    former global socket path.
    """
    if hermes_home is None:
        from hermes_constants import get_hermes_home

        hermes_home = get_hermes_home()
    profile_name = os.path.basename(os.path.normpath(os.fspath(hermes_home)))
    if profile_name == ".hermes":
        profile_name = "default"
    return os.path.join("/tmp/hermes", f"mcp_bridge.{profile_name}.sock")


BRIDGE_SOCKET = bridge_socket_path()

# Map of session_id -> watch handle.
_session_handles: dict[str, str] = {}

# Per-handle response queues.  When a write command is sent with a
# msg_id, a future is stored here.  The watch callback routes any
# incoming data with a matching msg_id to this future.
# {handle: {msg_id: asyncio.Future}}
_response_queues: dict[str, dict[str, asyncio.Future]] = {}

# Per-task delivery ordering for kanban status notifications (t_d979ec8d).
# Each stamped notification carries a monotonic seq (its task_events row id);
# deliveries for one task serialize through the entry's lock and a
# notification whose seq is not newer than the last DELIVERED one is dropped,
# so a subscriber never receives a state older than one they already got.
# {task_id: {"seq": Optional[int], "lock": asyncio.Lock}}
_KANBAN_DELIVERY_STATE_MAX = 4096
_kanban_delivery_states: "OrderedDict[str, dict]" = OrderedDict()


def _kanban_delivery_state(task_id: str) -> dict:
    """Return (and prune) the ordering state entry for one task."""
    state = _kanban_delivery_states.get(task_id)
    if state is None:
        state = {"seq": None, "lock": asyncio.Lock()}
        _kanban_delivery_states[task_id] = state
        while len(_kanban_delivery_states) > _KANBAN_DELIVERY_STATE_MAX:
            for key, st in list(_kanban_delivery_states.items()):
                if not st["lock"].locked():
                    del _kanban_delivery_states[key]
                    break
            else:
                break  # every entry is mid-delivery — keep them all
    else:
        _kanban_delivery_states.move_to_end(task_id)
    return state


async def _read_command_line(
    reader: asyncio.StreamReader,
    *,
    timeout: float = 30.0,
    max_bytes: int = BRIDGE_MAX_PAYLOAD_BYTES,
) -> bytes | None:
    """Read one newline-delimited JSON command, bounded by ``max_bytes``.

    The stdlib ``StreamReader.readuntil`` honours the reader's *line limit*
    (64 KiB by default) and raises ``LimitOverrunError`` past it — the
    failure that used to kill large wake payloads mid-connection
    (t_94b593b6).  Read fixed-size chunks instead and assemble the line
    ourselves, so arbitrarily long (but bounded) payloads survive while a
    non-conforming peer cannot exhaust gateway memory.

    Returns the raw line (newline included) or ``None`` on timeout/oversize/
    EOF.
    """
    data = bytearray()
    try:
        while True:
            if len(data) > max_bytes:
                return None
            chunk = await asyncio.wait_for(reader.read(BRIDGE_READ_CHUNK), timeout=timeout)
            if not chunk:
                return None  # EOF before a newline
            data.extend(chunk)
            if b"\n" in chunk or b"\n" in data:
                return bytes(data)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError):
        return None
    except Exception:
        return None


async def start_bridge_server(runner) -> asyncio.AbstractServer:
    """Start the MCP bridge Unix socket server."""
    try:
        os.unlink(BRIDGE_SOCKET)
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(BRIDGE_SOCKET), exist_ok=True)

    async def _handle(reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        data = await _read_command_line(reader)
        if data is None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return

        try:
            cmd = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
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
            elif action == "send":
                await _handle_send(runner, cmd, writer)
            else:
                _respond(writer, {"error": f"Unknown action: {action}"})
        except Exception as e:
            logger.debug("Bridge %s error: %s", action, e)
            _respond(writer, {"error": str(e)})

    server = await asyncio.start_unix_server(_handle, BRIDGE_SOCKET)
    logger.info("MCP bridge server listening on %s", BRIDGE_SOCKET)
    return server


# How often the owner loop re-checks that its socket path still exists and
# still accepts connections.  A stat + a local connect every 30s is free
# compared to a night of silently dead wakes.
BRIDGE_HEAL_INTERVAL = 30.0

# Upper bound on the liveness probe's connect.  Local AF_UNIX connect either
# succeeds instantly or fails instantly; the bound only guards a wedged peer.
BRIDGE_PROBE_TIMEOUT = 2.0


def _socket_inode(path: str) -> int | None:
    """Return the inode of the socket entry at ``path`` (None if absent)."""
    try:
        return os.stat(path).st_ino
    except OSError:
        return None


def _probe_sync(path: str, timeout: float) -> bool:
    """True only when a server ACCEPTS a connection on ``path``.

    An unlinked-but-listening socket is invisible here: the path is what
    external senders resolve, so a path that cannot be connected to is dead
    regardless of what the kernel still holds open (the 2026-11-18 shape).
    """
    try:
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(path)
        return True
    except OSError:
        return False


async def _socket_is_live(path: str, timeout: float = BRIDGE_PROBE_TIMEOUT) -> bool:
    return await asyncio.to_thread(_probe_sync, path, timeout)


async def _rebind_if_needed(
    runner, server: asyncio.AbstractServer, bound_inode: int | None
) -> tuple[asyncio.AbstractServer, int | None]:
    """Return ``(server, inode)``, re-binding when our socket path is gone.

    Healthy = the path exists, still points at OUR inode, and accepts
    connections.  Anything else is healed by closing the old listener (which
    may still hold an unlinked inode) and binding a fresh one — but a path
    that a DIFFERENT live listener owns is never stolen: that would silently
    redirect sends into the wrong gateway, which is worse than a stale path.
    """
    inode_now = _socket_inode(BRIDGE_SOCKET)
    live_now = await _socket_is_live(BRIDGE_SOCKET) if inode_now is not None else False

    if live_now and inode_now == bound_inode:
        return server, bound_inode
    if live_now:
        logger.error(
            "bridge socket %s is served by another live listener (inode %s, "
            "ours was %s) — not stealing it; external sends stay on that path",
            BRIDGE_SOCKET,
            inode_now,
            bound_inode,
        )
        return server, bound_inode

    logger.warning(
        "bridge socket %s %s — re-binding so external sends reach this gateway",
        BRIDGE_SOCKET,
        "vanished" if inode_now is None else "is stale (no listener)",
    )
    # Close the old listener first: it may still hold an unlinked inode, and
    # leaving both alive would make `ss` show a path that cannot be connected
    # to (the confusing state that hid this bug for a night).
    try:
        server.close()
        await server.wait_closed()
    except Exception:  # a listener we cannot close must not block recovery
        logger.debug("bridge socket close failed", exc_info=True)
    new_server = await start_bridge_server(runner)
    return new_server, _socket_inode(BRIDGE_SOCKET)


async def run_bridge_server(
    runner, *, check_interval: float = BRIDGE_HEAL_INTERVAL
) -> None:
    """Own the bridge socket for the gateway's lifetime, healing it if lost.

    ``start_bridge_server`` binds once at startup.  Gateways run for days, so
    anything that deletes the socket path out from under a live gateway (the
    nightly ``/tmp`` cleaner did exactly this on 2026-11-18) used to leave the
    process listening on an unlinked inode: every external ``hermes send -t``
    failed with "Bridge socket not found" while the gateway looked healthy.
    This loop turns that into a self-healing condition — no restart, no
    human — and logs loudly when it happens.
    """
    server = await start_bridge_server(runner)
    bound_inode = _socket_inode(BRIDGE_SOCKET)
    try:
        while True:
            await asyncio.sleep(check_interval)
            try:
                server, bound_inode = await _rebind_if_needed(
                    runner, server, bound_inode
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failed heal must not kill the owner loop; try again next
                # tick (the socket is still missing, so this stays visible).
                logger.exception("bridge socket heal failed for %s", BRIDGE_SOCKET)
    except asyncio.CancelledError:
        try:
            server.close()
            await server.wait_closed()
        except Exception:
            pass
        raise


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

        # Handoff data -> agent wake event.
        # Only inject handoff-typed payloads.  Command responses
        # (with or without msg_id) are routed above or silently
        # dropped — the relay path forwards them to MCP directly.
        if payload.get("type") == "handoff":
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
    """Inject a message via adapter.handle_message().

    Used by external processes (e.g. ``hermes send`` CLI) to route messages
    through the gateway's session manager when the in-process adapter is
    unavailable.  Without ``user_context`` the message arrives as an
    ``internal=True`` wake event.  With ``user_context`` (``hermes send -t``)
    it arrives as a simulated user message with full user identity.
    """
    platform_name = cmd.get("platform", "")
    chat_id = cmd.get("chat_id", "")
    text = cmd.get("text", "")
    thread_id = cmd.get("thread_id")
    user_context = cmd.get("user_context")

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

        # Resolve chat_type: prefer the explicit field (set by
        # kanban/cron notification hooks via bridge payload),
        # then HomeChannel config, fall back to "group".
        _chat_type = cmd.get("chat_type")
        if not _chat_type:
            home = runner.config.get_home_channel(platform) if runner.config else None
            _chat_type = home.chat_type if home else "group"
        session_source = SessionSource(
            platform=platform,
            chat_id=chat_id,
            thread_id=thread_id,
            chat_type=_chat_type,
        )
        # Build internal flag and user identity
        is_internal = True
        event_metadata = {}
        if user_context:
            is_internal = False
            session_source.user_id = user_context.get("user_id")
            session_source.user_name = user_context.get("sender_name")
            event_metadata["platform_user_id"] = user_context.get("platform_user_id")
        # Wake intake — topic-wake-no-bound-session: a wake targeted at a
        # chat/thread that has NO existing session must still create/bind one
        # BEFORE delivery, or the message posts into the void ("Mirror: no
        # session found") and the worker never starts — the claimed task then
        # strands until an unrelated tick reclaims it. Resolve the target
        # session unconditionally for every injected wake; task-id binding and
        # duplicate suppression below apply only to dispatch wakes.
        try:
            runner.session_store.get_or_create_session(session_source)
        except Exception as exc:
            logger.warning(
                "wake session resolve failed for %s:%s:%s: %s",
                platform_name, chat_id, thread_id, exc,
            )
        # Wake intake (t_a8031c32 gap 1/4): bind the carried task id to the
        # exact session this wake lands in before the turn runs; a duplicate
        # (the session is already engaged on live work) is suppressed — never
        # a second work pass. Cron wakes carry no task id and never enter
        # this path (Gopher's coordination 2026-11-06).
        wake_task_id = str((user_context or {}).get("task_id") or "").strip()
        if wake_task_id:
            from hermes_cli.kanban_db import wake_task_intake

            if (
                wake_task_intake(runner.session_store, session_source, wake_task_id)
                == "duplicate"
            ):
                _respond(writer, {
                    "ok": True,
                    "suppressed": True,
                    "note": (
                        f"duplicate wake for {wake_task_id} suppressed — "
                        "session already engaged on it"
                    ),
                })
                return
        notify_event = MessageEvent(
            text=text,
            source=session_source,
            message_type=MessageType.TEXT,
            internal=is_internal,
            timestamp=datetime.now(),
            metadata=event_metadata,
        )
        # Wake-intake outcome reporting (2026-11-16-tell-wake-delivered-no-turn):
        # an inject must not ack as if a turn spawned when the wake was only
        # QUEUED behind a busy session — or DROPPED entirely by an adapter that
        # returned without starting processing (e.g. no handler attached).
        # Report the true intake outcome and warn on non-spawned wakes so the
        # silent "delivered but no turn" class becomes visible to callers
        # instead of being swallowed as a false success.
        try:
            from gateway.session import build_session_key

            _cfg_extra = getattr(getattr(adapter, "config", None), "extra", {}) or {}
            session_key = build_session_key(
                session_source,
                group_sessions_per_user=_cfg_extra.get("group_sessions_per_user", True),
                thread_sessions_per_user=_cfg_extra.get("thread_sessions_per_user", False),
            )
        except Exception:
            session_key = None
        _active_before = getattr(adapter, "_active_sessions", None) or {}
        was_busy = bool(session_key and session_key in _active_before)
        await adapter.handle_message(notify_event)
        _active_after = getattr(adapter, "_active_sessions", None) or {}
        if was_busy:
            status = "queued"
        elif session_key and session_key in _active_after:
            status = "spawned"
        else:
            status = "dropped"
        if status != "spawned":
            logger.warning(
                "wake intake %s for %s:%s:%s — no autonomous turn started",
                status, platform_name, chat_id, thread_id,
            )
        _respond(writer, {"ok": True, "status": status})
    except asyncio.CancelledError:
        raise
    except Exception as e:
        _respond(writer, {"error": str(e)})


async def _handle_send(runner, cmd: dict, writer: asyncio.StreamWriter) -> None:
    """Queue an outbound message on the gateway's live platform adapter."""
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

    metadata = {}
    if thread_id:
        metadata["thread_id"] = thread_id
    if platform_name == "ntfy":
        metadata["publish_topic"] = chat_id

    # Optional kanban ordering stamp ({task_id, seq, status}). Only a valid
    # (task_id, int seq) pair engages the per-task delivery guard below.
    ordering = cmd.get("ordering") or {}
    order_task = str(ordering.get("task_id") or "").strip()
    order_seq = ordering.get("seq")
    if not order_task or not isinstance(order_seq, int):
        order_task, order_seq = "", None

    async def _do_send() -> bool:
        """Deliver once; True only when the adapter confirmed the send.

        Delivery contract (tests/dispatch/CONTRACT.md cell 5): a failed
        platform send must never be recorded as an advanced delivery
        watermark — the subscriber did not receive the notification, so a
        retry of that same seq must still be allowed through.
        """
        try:
            result = await adapter.send(
                chat_id=chat_id,
                content=text,
                metadata=metadata or None,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Bridge outbound delivery raised platform=%s chat_id=%s",
                platform_name,
                chat_id,
            )
            return False
        if not getattr(result, "success", False):
            logger.error(
                "Bridge outbound delivery failed platform=%s chat_id=%s: %s",
                platform_name,
                chat_id,
                getattr(result, "error", None) or "unknown error",
            )
            return False
        return True

    async def _deliver() -> None:
        if order_task:
            state = _kanban_delivery_state(order_task)
            async with state["lock"]:
                last = state["seq"]
                if last is not None and order_seq <= last:
                    logger.info(
                        "Bridge kanban notify dropped stale task=%s seq=%s "
                        "(already delivered seq=%s)",
                        order_task,
                        order_seq,
                        last,
                    )
                    return
                # Advance the watermark only after a CONFIRMED delivery. The
                # per-task lock already serializes this task's notifications,
                # so marking post-send keeps event order while ensuring a
                # failed send does not permanently suppress the retry of that
                # same notification (2026-11-17 cell 5 — a pre-send watermark
                # turned one transport hiccup into a silently lost
                # notify-complete).
                if await _do_send():
                    state["seq"] = order_seq
                else:
                    logger.error(
                        "Bridge kanban notify NOT delivered task=%s seq=%s — "
                        "watermark left at %s so a retry can still land",
                        order_task,
                        order_seq,
                        last,
                    )
        else:
            await _do_send()

    task = asyncio.create_task(_deliver())
    background_tasks = getattr(runner, "_background_tasks", None)
    if not isinstance(background_tasks, set):
        background_tasks = set()
        runner._background_tasks = background_tasks
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    _respond(writer, {"ok": True, "queued": True})


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
