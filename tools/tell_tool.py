#!/usr/bin/env python3
"""Agent-to-agent messaging through ``hermes send -t`` (the from-user wake).

The agent→target mapping lives in `hermes send -t`: a bare profile name
is expanded to the agent's home channel (see send_cmd.py
`_resolve_agent_wake_target`). A caller may instead provide a full session id,
which is routed through the existing ``profile:cli:session_id`` target.

One ``-t`` call does the whole job (Evan's one-call rule): it injects the
message as a user turn so the target agent wakes, and posts the visible copy
down the ``-u`` path internally.

Every tell is *mandatorily* echoed, logged, and posted to the target's chat:
the exact outbound payload is surfaced to the calling session, written to the
log, and delivered as a visible chat message. There is no opt-out — no secret
agent-to-agent traffic.
"""

import json
import logging
import os
from typing import Callable

from hermes_cli._subprocess_compat import spawn_detached

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
    reply_session_id = (
        origin_session_id or os.environ.get("HERMES_SESSION_ID") or ""
    ).strip()
    # Two routing rules, nothing else:
    # 1. Live session origin — a caller that passes its OWN session id anchors
    #    the conversation: delivery goes to the agent's home-channel wake
    #    target and the reply is routed back into that exact session
    #    (cron/journal/kanban closure). Any other session id remains a direct
    #    delivery target on the recipient's side.
    # 2. Kanban/cron origin — no live session to anchor: the wake and the
    #    visible post default to the target's home channel (bare profile).
    if target_session_id and target_session_id == reply_session_id:
        reply_instruction = (
            "If a reply is required, use 'tell' with "
            f"agent='{sender}' and session_id='{target_session_id}' — "
            "that routes the reply into this exact session."
        )
        target_session_id = ""
    else:
        # Replies use profile discovery. A gateway session ID is not a CLI
        # session target, and presenting it here causes `tell` to misroute
        # replies into the CLI notice queue instead of the sender's
        # messaging DM.
        reply_instruction = f"If a reply is required, use 'tell' with agent='{sender}'."
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
    # Fire-and-forget: the message is echoed + logged above (the mandatory
    # transparency contract), then dispatched detached so the caller's tool
    # loop never blocks on delivery. ONE `-t` call does both halves — the
    # from-user wake into the target and the visible `-u` copy, which
    # `hermes send -t` sends internally (Evan's one-call rule). A spawn
    # failure (binary missing) is the only synchronous error we can still
    # observe and report.
    proc = spawn_detached(["hermes", "send", "-t", target, wrapped])
    if proc is None:
        return json.dumps({"error": f"failed to launch 'hermes send' for {agent}"})
    return json.dumps({"dispatched": True, "note": f"notified {agent}"})


TELL_SCHEMA = {
    "name": "tell",
    "description": (
        "Send a wrapped message to another Hermes agent profile. By default this wakes its "
        "Telegram DM session; session_id targets one full session directly. Passing your OWN "
        "session id as session_id anchors the conversation: replies are routed back into that "
        "exact session. Replies otherwise route by the sender's agent profile. Every tell is "
        "also posted as a visible message in the target's chat."
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
                "description": (
                    "Optional full session ID. Your OWN session id routes replies back into "
                    "that session (cron/journal/kanban closure); a session id belonging to the "
                    "target agent delivers directly into that session instead of profile DM "
                    "routing."
                ),
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
