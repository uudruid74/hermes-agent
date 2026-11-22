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
import time
from pathlib import Path
from typing import Callable


logger = logging.getLogger(__name__)

# Bounded durable retry for dropped wakes (t_ca783b1c): a transient delivery
# failure (bridge down / restarting / queue full) is retried up to
# TELL_MAX_ATTEMPTS times with backoff while the sender session lives. On final
# failure the message is written to a persistent undelivered record so it
# cannot silently vanish.
TELL_MAX_ATTEMPTS = 3
TELL_BACKOFF_SECONDS = (1.0, 2.0)

# Failure markers that mean "transient delivery problem — retry may help" as
# opposed to permanent configuration/usage errors (unknown profile, no home
# channel, failed to launch hermes, ...) which are surfaced immediately.
_TRANSIENT_DELIVERY_MARKERS = (
    "gateway may not be running",   # bridge socket not found / refused / timeout
    "Bridge returned empty response",
    "Bridge send failed",
    "Bridge inject failed",
    "wake dropped",
)


def _is_retryable_failure(detail: dict) -> bool:
    """True when the failure is a transient wake transport/intake problem.

    ``status: dropped`` from the bridge envelope is the canonical drop class
    (the gateway accepted the inject but started no processing task); the
    marker strings cover the bridge-unreachable family. Everything else —
    unknown profile, usage errors, unverifiable output — is permanent and is
    reported without retrying.
    """
    if isinstance(detail, dict) and detail.get("status") == "dropped":
        return True
    text = ""
    if isinstance(detail, dict) and detail.get("error"):
        text = str(detail["error"])
    return any(marker in text for marker in _TRANSIENT_DELIVERY_MARKERS)


def _undelivered_log_path() -> Path:
    """Path of the durable undelivered-wakes record (JSONL outbox).

    ``HERMES_UNDELIVERED_LOG`` overrides the location (tests / ops); the
    default lives with the other shared runtime state under the Hermes home.
    The record is append-only: a message that exhausts its retries must never
    silently vanish.
    """
    override = os.environ.get("HERMES_UNDELIVERED_LOG", "").strip()
    if override:
        return Path(override)
    from hermes_cli.config import get_hermes_home

    return get_hermes_home() / "undelivered_wakes.jsonl"


def _record_undelivered(
    *,
    agent: str,
    target: str,
    message: str,
    session_id: str,
    attempts: int,
    last_error: str,
) -> str:
    """Append one durable undelivered-wake record; return its file path."""
    import datetime

    record = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "sender": (os.environ.get("USERNAME") or "agent").strip(),
        "target": agent,
        "resolved_target": target,
        "session_id": session_id or None,
        "attempts": attempts,
        "last_error": last_error,
        "message": message,
    }
    path = _undelivered_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError as exc:
        # The record is doctrine: a failed write must never masquerade as
        # delivery. Log it and surface the failure in the error payload.
        logger.error("failed to write undelivered wake record %s: %s", path, exc)
        return f"<failed to write undelivered record: {exc}>"
    return str(path)


def _send_once(agent: str, target: str, wrapped: str, *, env=None) -> dict:
    """Run one verified ``hermes send -t`` and report its real outcome.

    Returns the same outcome shapes as the pre-retry logic: a success envelope
    for confirmed delivery, or an ``{"error": ...}`` dict. Raises
    ``subprocess.TimeoutExpired`` / ``OSError`` for launch-level failures,
    which the caller treats as final (no retry).
    """
    import subprocess

    run = subprocess.run(
        ["hermes", "send", "-t", target, "--json", wrapped],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )

    out = (run.stdout or "").strip()
    detail = {}
    # --json makes the CLI emit the machine envelope (indent=2 => multi-line);
    # parse the WHOLE payload, not the last line (the old last-line parse could
    # never succeed against multi-line JSON — every successful tell reported
    # UNVERIFIABLE until 2026-11-19).
    try:
        detail = json.loads(out) if out else {}
    except json.JSONDecodeError:
        detail = {}

    if run.returncode != 0:
        payload = {
            "error": (
                f"tell to {agent} FAILED (exit {run.returncode}): "
                f"{(run.stderr or out or 'no output')[-300:]}"
            )
        }
        # Carry the bridge's intake status through so the retry classifier can
        # see a dropped wake (DROPPED IS NEVER SUCCESS) even when the CLI
        # already exited 1 for it.
        if isinstance(detail, dict) and detail.get("status"):
            payload["status"] = detail["status"]
        return payload
    if isinstance(detail, dict) and detail.get("error"):
        return {"error": f"tell to {agent} NOT delivered: {detail['error']}"}
    if isinstance(detail, dict) and detail.get("success"):
        status = detail.get("status") or (
            "suppressed" if detail.get("suppressed") else "delivered"
        )
        note = detail.get("note", f"wake {status} for {agent}")
        return {
            "dispatched": True,
            "delivered": True,
            "status": status,
            "note": note,
        }
    return {
        "error": (
            f"tell to {agent} returned an UNVERIFIABLE result (exit 0, "
            f"unrecognized output: {out[-200:]}) — treat as FAILED"
        )
    }


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
    # VERIFIED delivery (Evan 2026-11-18: "fallbacks are evil" / "dropped is
    # never success"): `hermes send -t` does both halves — the from-user wake
    # into the target and the visible `-u` copy. We WAIT for the real outcome
    # and report it. Durable redelivery (t_ca783b1c): transient delivery
    # failures (bridge down/restarting, queue full) are retried with bounded
    # backoff while this sender session lives; only the FIRST attempt posts the
    # visible copy (`HERMES_SEND_SKIP_COPY` tells later attempts not to
    # duplicate it). When the retries are exhausted the message is recorded in
    # the persistent undelivered outbox so it cannot silently vanish.
    import subprocess

    unsuccessful: dict | None = None
    attempts = 0
    for attempt in range(TELL_MAX_ATTEMPTS):
        env = None
        if attempt > 0:
            env = dict(os.environ)
            env["HERMES_SEND_SKIP_COPY"] = "1"
        try:
            outcome = _send_once(agent, target, wrapped, env=env)
        except subprocess.TimeoutExpired:
            outcome = {
                "error": (
                    f"tell to {agent} timed out after 60s — delivery UNVERIFIED, "
                    "treat as FAILED and retry or escalate"
                )
            }
            unsuccessful = outcome
            attempts = attempt + 1
            break
        except OSError as exc:
            return json.dumps({
                "error": f"failed to launch 'hermes send' for {agent}: {exc}"
            })
        attempts = attempt + 1
        if "error" not in outcome:
            return json.dumps(outcome)
        unsuccessful = outcome
        if not _is_retryable_failure(outcome):
            return json.dumps(outcome)
        if attempt < TELL_MAX_ATTEMPTS - 1:
            logger.warning(
                "tell to %s failed transiently (attempt %d/%d), retrying: %s",
                agent,
                attempt + 1,
                TELL_MAX_ATTEMPTS,
                outcome["error"],
            )
            time.sleep(TELL_BACKOFF_SECONDS[attempt])

    # Retries exhausted: leave a durable undelivered record so the message
    # cannot vanish. The error stays the primary outcome; the record path and
    # attempt count ride alongside it.
    record_path = _record_undelivered(
        agent=agent,
        target=target,
        message=wrapped,
        session_id=target_session_id,
        attempts=attempts,
        last_error=(unsuccessful["error"] if unsuccessful else "unknown"),
    )
    error_payload = dict(unsuccessful) if unsuccessful else {}
    error_payload["undelivered_record"] = record_path
    error_payload["attempts"] = attempts
    return json.dumps(error_payload)


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
