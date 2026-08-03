"""Direct best-effort notifications for terminal Kanban transitions.

This module deliberately has no polling, subscription, or database ownership.
The transition owner calls :func:`notify_task_closed` after the SQLite commit.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Any, Optional

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = frozenset({"done", "blocked"})


def _notification_target(origin: Optional[dict[str, Any]]) -> tuple[str, str]:
    """Return ``(hermes send target, chat_type)`` for a task close."""
    if origin and origin.get("platform") and origin.get("chat_id"):
        platform = str(origin["platform"]).strip().lower()
        chat_id = str(origin["chat_id"]).strip()
        thread_id = str(origin.get("thread_id") or "").strip()
        target = f"{platform}:{chat_id}"
        if thread_id:
            target = f"{target}:{thread_id}"
        chat_type = str(origin.get("chat_type") or "").strip()
        if not chat_type:
            chat_type = "group" if thread_id else "dm"
        return target, chat_type

    # ``hermes send -u telegram`` resolves the configured Telegram home
    # channel. This is the no-origin path for CLI/dashboard-created tasks.
    return "telegram", ""


def _send_command(target: str, payload: str) -> list[str]:
    """Build the internal-send command in the dispatching gateway's profile."""
    command = ["hermes"]
    notify_profile = os.environ.get("HERMES_KANBAN_NOTIFY_PROFILE", "").strip()
    if notify_profile:
        command.extend(["-p", notify_profile])
    command.extend(["send", "-u", target, payload])
    return command


def notify_task_closed(
    task_id: str,
    status: str,
    *,
    summary: Optional[str] = None,
    title: Optional[str] = None,
    origin: Optional[dict[str, Any]] = None,
) -> bool:
    """Invoke ``hermes send -u`` once for a durable done/blocked transition.

    Delivery failure is logged and returned as ``False`` but never raised: a
    notification transport cannot invalidate a committed task transition.
    """
    if status not in _TERMINAL_STATUSES:
        return False

    summary_line = summary.splitlines()[0][:300] if summary else None
    payload = json.dumps(
        {
            "source": "kanban",
            "type": status,
            "task_id": task_id,
            "title": title or task_id,
            "summary": summary_line,
        },
        ensure_ascii=False,
    )
    target, chat_type = _notification_target(origin)
    env = os.environ.copy()
    if chat_type:
        env["HERMES_NOTIFY_CHAT_TYPE"] = chat_type
    else:
        env.pop("HERMES_NOTIFY_CHAT_TYPE", None)

    try:
        result = subprocess.run(
            _send_command(target, payload),
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("kanban close notification failed for %s: %s", task_id, exc)
        return False

    if result.returncode != 0:
        error = (result.stderr or result.stdout or "unknown error").strip()
        logger.warning(
            "kanban close notification failed for %s (exit %s): %s",
            task_id,
            result.returncode,
            error[:300],
        )
        return False
    return True
