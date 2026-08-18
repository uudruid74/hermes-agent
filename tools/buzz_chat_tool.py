"""
buzz_chat_tool — direct Buzz channel chat for Hermes agents.

Commands: send, read, members, thread

A registered internal tool (like plan_tool) so agents can read/send
Buzz messages WITHOUT the terminal write-gate. No MCP, no terminal
dependency — shells out to the buzz CLI binary (JSON in/out), same
approach the buzz platform adapter uses.

Extensible: add new subcommands here (e.g. dms, channels) —
keep the schema enum + dispatch in sync.

Contract note: handlers MUST return str (see registry._normalize_handler_result).
All outputs are formatted text; errors are prefixed with "Error:".
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
DEFAULT_RELAY = "wss://buzz.virtuallyreal.games"
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


def _short(pubkey: str, n: int = 12) -> str:
    """Shorten a pubkey for display."""
    return (pubkey or "")[:n]


def _format_errors(res: Dict[str, Any]) -> Optional[str]:
    """Return an Error string for a failed _run_cli result, else None."""
    if res.get("ok"):
        return None
    if "error" in res:
        return f"Error: {res['error']}"
    return f"Error: buzz CLI exited {res.get('returncode')}: {res.get('stderr', '')}"


def _cmd_send(channel: str, content: str, reply_to: Optional[str] = None) -> str:
    args = ["messages", "send", "--channel", channel, "--content", content]
    if reply_to:
        args += ["--reply-to", reply_to]
    res = _run_cli(args)
    err = _format_errors(res)
    if err:
        return err
    r = res.get("result") or {}
    event_id = r.get("event_id") or r.get("id") or "unknown"
    if not r.get("accepted", False):
        return f"Error: send not accepted: {r.get('message', '')}".strip()
    mentions = r.get("mention_pubkeys") or []
    extra = f" (mentions: {len(mentions)})" if mentions else ""
    return f"Sent to {channel}: {event_id}{extra}"


def _cmd_read(channel: str, limit: int = 10) -> str:
    res = _run_cli(["messages", "get", "--channel", channel, "--limit", str(limit)])
    err = _format_errors(res)
    if err:
        return err
    msgs = res.get("result") or []
    if not msgs:
        return f"No messages in {channel}."
    lines = [f"{len(msgs)} message(s) in {channel}:"]
    for m in msgs:
        mid = m.get("id") or m.get("event_id") or "-"
        pub = _short(m.get("pubkey") or "")
        parent = m.get("parent_event_id")
        prefix = "↳" if parent else "•"
        ts = m.get("created_at") or m.get("timestamp") or ""
        content = (m.get("content") or "").replace("\n", " ")[:300]
        lines.append(f"  {prefix} {mid} {pub} {ts}: {content}")
    return "\n".join(lines)


def _cmd_members(channel: str) -> str:
    res = _run_cli(["channels", "members", "--channel", channel])
    err = _format_errors(res)
    if err:
        return err
    members = res.get("result") or []
    if not members:
        return f"No members in {channel}."
    lines = [f"{len(members)} member(s) in {channel}:"]
    for m in members:
        lines.append(f"  • {_short(m.get('pubkey') or '', 16)} ({m.get('role', 'member')})")
    return "\n".join(lines)


def _cmd_thread(channel: str, event_id: str, limit: int = 50) -> str:
    args = ["messages", "thread", "--channel", channel, "--event", event_id,
            "--limit", str(limit)]
    res = _run_cli(args)
    err = _format_errors(res)
    if err:
        return err
    msgs = res.get("result") or []
    if not msgs:
        return f"No thread found for {event_id} in {channel}."
    lines = [f"Thread {event_id} in {channel} ({len(msgs)} message(s)):"]
    for m in msgs:
        mid = m.get("id") or m.get("event_id") or "-"
        pub = _short(m.get("pubkey") or "")
        ts = m.get("created_at") or m.get("timestamp") or ""
        content = (m.get("content") or "").replace("\n", " ")[:300]
        lines.append(f"  • {mid} {pub} {ts}: {content}")
    return "\n".join(lines)


def buzz_chat(agent=None, command: str = "", **kwargs) -> str:
    """Dispatch buzz_chat subcommand. Always returns a string."""
    if command == "send":
        content = kwargs.get("content") or ""
        if not content:
            return "Error: send requires content"
        return _cmd_send(
            kwargs.get("channel") or CREW_CHANNEL,
            content,
            reply_to=kwargs.get("reply_to"),
        )
    if command == "read":
        return _cmd_read(kwargs.get("channel") or CREW_CHANNEL, int(kwargs.get("limit") or 10))
    if command == "members":
        return _cmd_members(kwargs.get("channel") or CREW_CHANNEL)
    if command == "thread":
        event_id = kwargs.get("event_id") or ""
        if not event_id:
            return "Error: thread requires event_id"
        return _cmd_thread(
            kwargs.get("channel") or CREW_CHANNEL,
            event_id,
            int(kwargs.get("limit") or 50),
        )
    return "Error: unknown command — use send, read, members, or thread"


# --- Schema ---

BUZZ_CHAT_SCHEMA = {
    "name": "buzz_chat",
    "description": (
        "Direct Buzz channel chat (Nostr relay). Commands: "
        "send (post a message), read (fetch recent messages), "
        "members (list channel members), thread (fetch a message thread). "
        "Bypasses the terminal write-gate like plan_tool — no active plan "
        "needed to chat."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Command: send, read, members, or thread",
                "enum": ["send", "read", "members", "thread"],
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
            "event_id": {
                "type": "string",
                "description": "Event ID to fetch the thread for (thread command)",
            },
            "limit": {
                "type": "integer",
                "description": "Max messages for read (default 10) or thread (default 50)",
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
        event_id=args.get("event_id"),
        limit=args.get("limit"),
    ),
    emoji="🐝",
)
