#!/usr/bin/env python3
"""Agent-to-agent messaging through ``hermes send -u``."""

import json
import os
import subprocess


_DM_CHAT_ID = "8900123006"


def tell_tool(agent: str, message: str) -> str:
    """Wake another Hermes profile with a wrapped agent message."""
    sender = os.environ.get("HERMES_AGENT_NAME") or os.environ.get("HERMES_PROFILE") or "agent"
    target = f"{agent}:telegram:{_DM_CHAT_ID}"
    wrapped = (
        f"Incoming message from {sender} follows:\n"
        "---\n"
        f"{message}\n"
        "---\n"
        "If a reply is required, use the 'tell' command to reply."
    )
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
