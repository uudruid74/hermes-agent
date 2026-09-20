#!/usr/bin/env python3
"""Agent-to-agent messaging through ``hermes send -u``.

The agent→platform mapping lives in `hermes send -u`: a bare profile name
is expanded to the agent's telegram DM (see send_cmd.py
`_resolve_agent_wake_target`). This tool is a thin wrapper that passes the
bare agent name through — session ids are transient, agent names are not.

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
) -> str:
    """Wake another Hermes profile after exposing the exact message to the caller."""
    sender = (os.environ.get("USERNAME") or "").strip() or "agent"
    wrapped = (
        f"Incoming message from {sender} follows:\n"
        "---\n"
        f"{message}\n"
        "---\n"
        "If a reply is required, use the 'tell' command to reply."
    )

    echo = f"{agent}: {message}"
    logger.info("%s", echo)
    echo_callback(echo)

    result = subprocess.run(
        ["hermes", "send", "-u", agent, wrapped],
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
        "Send a wrapped message to another Hermes agent profile and wake its Telegram DM "
        "session. The receiver is told to reply with tell if a reply is required."
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
    ),
    emoji="📨",
)
