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
    # Reply routing (Evan 2026-10-20: "the tell response path was supposed to
    # name a specific session id to be consistent"): the reply instruction
    # ALWAYS names the sender's session, so the reply lands in that exact
    # session instead of drifting to a home channel. Passing the caller's OWN
    # session id as `session_id` is the anchor form — not a delivery target —
    # and only clears the target.
    if target_session_id and target_session_id == reply_session_id:
        target_session_id = ""
    if reply_session_id:
        reply_instruction = (
            "If a reply is required, use 'tell' with "
            f"agent='{sender}' and session_id='{reply_session_id}' — "
            "that routes the reply into this exact session."
        )
    else:
        # No live session origin (cron/kanban): profile discovery. The bare
        # profile wake lands in the target's DM (send_cmd's -t expansion).
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

    # Use-time resolution (agent-wake-routing rule 2): a named session id is
    # rolled forward to its live successor and delivered to its CHAT; a
    # chatless session keeps the cli: notice-queue shape; anything
    # unresolvable falls back to the bare profile (the target's DM).
    target = agent
    if target_session_id:
        try:
            from hermes_cli.kanban_db import session_wake_target

            target = session_wake_target(agent, target_session_id) or agent
        except (ImportError, ValueError, OSError):
            target = agent
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
        "Telegram DM session; session_id targets one full session directly (resolved at use "
        "time: a stale id rolls forward to its live successor). Replies are ALWAYS routed "
        "back into the sender's session — the message names the session id to use. Passing "
        "your OWN session id as session_id is the anchor form (it only names the reply "
        "target). Every tell is also posted as a visible message in the target's chat."
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
                    "Optional full session ID. A session id belonging to the target agent "
                    "delivers directly into that session (its live successor) instead of the "
                    "DM default; your OWN session id is the anchor form and only names the "
                    "reply target."
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
