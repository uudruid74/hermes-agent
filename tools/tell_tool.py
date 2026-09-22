#!/usr/bin/env python3
"""Agent-to-agent messaging through ``hermes send -u``.

The agent→platform mapping lives in `hermes send -u`: a bare profile name
is expanded to the agent's telegram DM (see send_cmd.py
`_resolve_agent_wake_target`). A caller may instead provide a full session id,
which is routed through the existing ``profile:cli:session_id`` target.

Every tell is *mandatorily* echoed and logged: the exact outbound payload is
surfaced to the calling session and written to the log. There is no opt-out —
no secret agent-to-agent traffic.
"""

import json
import logging
import os
import subprocess
from typing import Callable

logger = logging.getLogger(__name__)


def tell_tool(
    agent: str,
    message: str,
    echo_callback: Callable[[str], object],
    session_id: str | None = None,
    origin_session_id: str | None = None,
) -> str:
    """Wake another Hermes profile after exposing the exact message to the caller."""
    sender = (os.environ.get("USERNAME") or "").strip() or "agent"
    target_session_id = (session_id or "").strip()
    reply_session_id = (origin_session_id or "").strip()
    if reply_session_id:
        reply_instruction = (
            "If a reply is required, use 'tell' with "
            f"agent='{sender}' and session_id='{reply_session_id}'."
        )
    else:
        reply_instruction = "If a reply is required, use the 'tell' command to reply."
    wrapped = (
        f"Incoming message from {sender} follows:\n"
        "---\n"
        f"{message}\n"
        "---\n"
        f"{reply_instruction}"
    )

    echo = f"{agent}: {message}"
    logger.info("%s", echo)
    echo_callback(echo)

    target = f"{agent}:cli:{target_session_id}" if target_session_id else agent
    result = subprocess.run(
        ["hermes", "send", "-u", target, wrapped],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return json.dumps(
        {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    )


TELL_SCHEMA = {
    "name": "tell",
    "description": (
        "Send a wrapped message to another Hermes agent profile. By default this wakes its "
        "Telegram DM session; session_id targets one full session directly. The receiver is "
        "told which session to reply to when that information is available."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "agent": {
                "type": "string",
                "description": "Target Hermes profile name, such as zephyr, neo, wintermute, or gopher.",
            },
            "message": {
                "type": "string",
                "description": "Message body to send to the target agent.",
            },
            "session_id": {
                "type": "string",
                "description": "Optional full session ID to target directly instead of using profile DM routing.",
            },
        },
        "required": ["agent", "message"],
    },
}


from tools.registry import registry

registry.register(
    name="tell",
    toolset="session",
    schema=TELL_SCHEMA,
    handler=lambda args, **kw: tell_tool(
        agent=args["agent"],
        message=args["message"],
        echo_callback=kw["echo_callback"],
        session_id=args.get("session_id"),
        origin_session_id=kw.get("session_id"),
    ),
    emoji="📨",
)
