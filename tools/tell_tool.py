#!/usr/bin/env python3
"""Agent-to-agent messaging through ``hermes send -u``.

The agent→platform mapping lives in `hermes send -u`: a bare profile name
is expanded to the agent's telegram DM (see send_cmd.py
`_resolve_agent_wake_target`). This tool is a thin wrapper that passes the
bare agent name through — session ids are transient, agent names are not.
"""

import json
import os
import subprocess


def tell_tool(agent: str, message: str) -> str:
    """Wake another Hermes profile with a wrapped agent message."""
    sender = os.environ.get("HERMES_AGENT_NAME") or os.environ.get("HERMES_PROFILE") or "agent"
    wrapped = (
        f"Incoming message from {sender} follows:\n"
        "---\n"
        f"{message}\n"
        "---\n"
        "If a reply is required, use the 'tell' command to reply."
    )
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
    handler=lambda args, **_kw: tell_tool(
        agent=args["agent"],
        message=args["message"],
    ),
    emoji="📨",
)
