"""
buzz_chat_tool — direct Buzz channel chat for Hermes agents.

Commands: send, read, members

A registered internal tool (like plan_tool) so agents can read/send
Buzz messages WITHOUT the terminal write-gate. No MCP, no terminal
dependency — shells out to the buzz CLI binary (JSON in/out), same
approach the buzz platform adapter uses.

Extensible: add new subcommands here (e.g. reply, dms, channels) —
keep the schema enum + dispatch in sync.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

DEFAULT_CLI = "/home/ekl/Documents/Programming/buzz/target/debug/buzz"
DEFAULT_RELAY = "ws://127.0.0.1:3000"
CREW_CHANNEL = "7c1dbf0d-33b3-4952-8364-6d7e2fc2f8b3"


def _get_env() -> Dict[str, str]:
    """Build the env for the buzz CLI: relay + private key."""
    env = dict(os.environ)
    env.setdefault("BUZZ_RELAY_URL", DEFAULT_RELAY)
    key = os.environ.get("BUZZ_PRIVATE_KEY", "")
    if not key:
        # Fallback: read from the profile .env (scoped secret may not be
        # exported to the tool process env).
        home = os.environ.get("HERMES_HOME", "")
        for candidate in (
            os.path.join(home, ".env") if home else "",
            os.path.expanduser("~/.hermes/profiles/gopher/.env"),
            os.path.expanduser("~/.hermes/.env"),
        ):
            if candidate and os.path.isfile(candidate):
                try:
                    with open(candidate) as f:
                        for line in f:
                            if line.startswith("BUZZ_PRIVATE_KEY="):
                                key = line.split("=", 1)[1].strip()
                                break
                except Exception:
                    pass
                if key:
                    break
    env["BUZZ_PRIVATE_KEY"] = key
    return env


def _run_cli(args: list) -> Dict[str, Any]:
    """Run the buzz CLI and return parsed JSON (or a dict with error)."""
    cli = os.environ.get("BUZZ_CLI_PATH", DEFAULT_CLI)
    try:
        proc = subprocess.run(
            [cli] + args,
            capture_output=True,
            text=True,
            timeout=30,
            env=_get_env(),
        )
    except Exception as e:  # noqa: BLE001 — tool boundary
        return {"ok": False, "error": f"buzz CLI failed: {e}"}
    if proc.returncode != 0:
        return {
            "ok": False,
            "returncode": proc.returncode,
            "stderr": proc.stderr.strip()[-500:],
        }
    out = proc.stdout.strip()
    if not out:
        return {"ok": True, "result": None}
    try:
        return {"ok": True, "result": json.loads(out)}
    except json.JSONDecodeError:
        return {"ok": True, "raw": out[:2000]}


def _cmd_send(channel: str, content: str, reply_to: Optional[str] = None) -> Dict[str, Any]:
    args = ["messages", "send", "--channel", channel, "--content", content]
    if reply_to:
        args += ["--reply-to", reply_to]
    res = _run_cli(args)
    if not res.get("ok"):
        return res
    r = res.get("result") or {}
    return {
        "ok": bool(r.get("accepted", False)),
        "event_id": r.get("event_id"),
        "mention_pubkeys": r.get("mention_pubkeys", []),
        "message": r.get("message", ""),
    }


def _cmd_read(channel: str, limit: int = 10) -> Dict[str, Any]:
    res = _run_cli(["messages", "get", "--channel", channel, "--limit", str(limit)])
    if not res.get("ok"):
        return res
    msgs = res.get("result") or []
    out = []
    for m in msgs:
        out.append(
            {
                "id": m.get("id") or m.get("event_id"),
                "pubkey": (m.get("pubkey") or "")[:12],
                "parent": m.get("parent_event_id") or "-",
                "ts": m.get("created_at") or m.get("timestamp"),
                "content": (m.get("content") or "")[:500],
            }
        )
    return {"ok": True, "count": len(out), "messages": out}


def _cmd_members(channel: str) -> Dict[str, Any]:
    res = _run_cli(["channels", "members", "--channel", channel])
    if not res.get("ok"):
        return res
    members = res.get("result") or []
    out = [
        {"pubkey": (m.get("pubkey") or "")[:16], "role": m.get("role", "member")}
        for m in members
    ]
    return {"ok": True, "count": len(out), "members": out}


def buzz_chat(agent=None, command: str = "", **kwargs) -> Dict[str, Any]:
    """Dispatch buzz_chat subcommand."""
    if command == "send":
        content = kwargs.get("content") or ""
        if not content:
            return {"ok": False, "error": "send requires content"}
        return _cmd_send(
            kwargs.get("channel") or CREW_CHANNEL,
            content,
            reply_to=kwargs.get("reply_to"),
        )
    if command == "read":
        return _cmd_read(kwargs.get("channel") or CREW_CHANNEL, int(kwargs.get("limit") or 10))
    if command == "members":
        return _cmd_members(kwargs.get("channel") or CREW_CHANNEL)
    return {
        "ok": False,
        "error": "unknown command — use send, read, or members",
    }


# --- Schema ---

BUZZ_CHAT_SCHEMA = {
    "name": "buzz_chat",
    "description": (
        "Direct Buzz channel chat (Nostr relay). Commands: "
        "send (post a message), read (fetch recent messages), "
        "members (list channel members). Bypasses the terminal write-gate "
        "like plan_tool — no active plan needed to chat."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Command: send, read, or members",
                "enum": ["send", "read", "members"],
            },
            "channel": {
                "type": "string",
                "description": "Channel UUID (defaults to the crew channel)",
            },
            "content": {
                "type": "string",
                "description": "Message text for send; supports @mentions and markdown",
            },
            "reply_to": {
                "type": "string",
                "description": "Event ID to reply to (creates a thread) for send",
            },
            "limit": {
                "type": "integer",
                "description": "Max messages for read (default 10)",
            },
        },
        "required": ["command"],
    },
}


from tools.registry import registry  # noqa: E402

registry.register(
    name="buzz_chat",
    toolset="session",
    schema=BUZZ_CHAT_SCHEMA,
    handler=lambda args, **kw: buzz_chat(
        agent=kw.get("agent"),
        command=args.get("command", ""),
        channel=args.get("channel"),
        content=args.get("content"),
        reply_to=args.get("reply_to"),
        limit=args.get("limit"),
    ),
    emoji="🐝",
)
