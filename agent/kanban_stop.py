"""Turn-end guard for board/plan workers.

Workers must end their step with a terminal tool. Models (especially GLM /
Qwen families) sometimes narrate the next step ("Let me write the report
now") and stop with ``finish_reason=stop`` and no tool calls. Hermes treats
that as a clean exit and the step is left open with no worker.

Two worker shapes arm this guard (Evan 2026-10-24, Ornith's mid-step stall):

- **env-armed** — dispatcher-spawned one-shot kanban worker
  (``HERMES_KANBAN_TASK`` set). Terminal tools: ``kanban_complete`` /
  ``kanban_block``. Nudge speaks board vocabulary.
- **binding-armed** — long-lived gateway session on a Plan (``plan_task_id``
  resolved from the session's execution binding; this shape has NO env var —
  the binding is the worker signal). Terminal tools: ``plan advance`` /
  ``plan fail`` / ``plan block``. Nudge speaks plan vocabulary. When both
  arm, the binding wins: the open contract is the plan step.

This module is policy-only: given the conversation and which shape armed,
return a bounded synthetic nudge so the conversation loop continues instead
of exiting. It reads no databases and resolves no bindings — callers do.
"""

from __future__ import annotations

import json
import os
from typing import Any, Iterable, Optional


_TERMINAL_KANBAN_TOOLS = frozenset({"kanban_complete", "kanban_block"})

# A plan tool call carries the verb in its ``command`` argument.
_TERMINAL_PLAN_COMMANDS = frozenset({"advance", "fail", "block"})

_DEFAULT_MAX_ATTEMPTS = 2

def kanban_stop_nudge_enabled(plan_task_id: Optional[str] = None) -> bool:
    """Return whether the stop-guard is active for this process.

    On when ``HERMES_KANBAN_TASK`` is set (dispatcher-spawned worker) or
    ``plan_task_id`` names a bound Plan (long-lived plan worker), unless
    ``HERMES_KANBAN_STOP_NUDGE`` explicitly disables it.
    """
    env = os.environ.get("HERMES_KANBAN_STOP_NUDGE")
    if env is not None and env.strip().lower() in {"0", "false", "no", "off"}:
        return False
    if plan_task_id:
        return True
    task = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    return bool(task)

def _tool_call_name(tc: Any) -> str:
    if isinstance(tc, dict):
        fn = tc.get("function")
        if isinstance(fn, dict):
            return str(fn.get("name") or "")
        return str(tc.get("name") or "")
    fn = getattr(tc, "function", None)
    if fn is not None:
        return str(getattr(fn, "name", "") or "")
    return str(getattr(tc, "name", "") or "")


def _tool_call_arguments(tc: Any) -> str:
    if isinstance(tc, dict):
        fn = tc.get("function")
        if isinstance(fn, dict):
            return str(fn.get("arguments") or "")
        return str(tc.get("arguments") or "")
    fn = getattr(tc, "function", None)
    if fn is not None:
        return str(getattr(fn, "arguments", "") or "")
    return str(getattr(tc, "arguments", "") or "")

def session_called_kanban_terminal(messages: Iterable[dict] | None) -> bool:
    """True if this conversation already invoked a terminal kanban tool."""
    if not messages:
        return False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                if _tool_call_name(tc) in _TERMINAL_KANBAN_TOOLS:
                    return True
        elif role == "tool":
            name = str(msg.get("name") or "")
            if name in _TERMINAL_KANBAN_TOOLS:
                return True
    return False


def session_called_plan_terminal(messages: Iterable[dict] | None) -> bool:
    """True if this conversation already submitted its plan step.

    Only the assistant tool_calls row can name the verb (a bare ``plan``
    result row could be any subcommand), so this scans call arguments for
    ``command`` in {advance, fail, block}.
    """
    if not messages:
        return False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            if _tool_call_name(tc) != "plan":
                continue
            raw = _tool_call_arguments(tc)
            if not raw:
                continue
            try:
                args = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(args, dict) and str(
                args.get("command") or ""
            ) in _TERMINAL_PLAN_COMMANDS:
                return True
    return False

def build_kanban_stop_nudge(
    *,
    messages: Iterable[dict] | None = None,
    attempts: int = 0,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    task_id: Optional[str] = None,
    plan_task_id: Optional[str] = None,
) -> Optional[str]:
    """Return a synthetic follow-up when a worker exits without a terminal tool.

    Returns ``None`` when the guard should not fire (not a board/plan worker,
    already submitted/completed/blocked, or nudge budget exhausted).
    ``plan_task_id`` (a bound Plan) selects plan mode — plan terminal tools
    and plan vocabulary; otherwise the env-armed kanban shape is unchanged.
    """
    if not kanban_stop_nudge_enabled(plan_task_id=plan_task_id):
        return None
    if attempts >= max_attempts:
        return None
    if plan_task_id:
        if session_called_plan_terminal(messages):
            return None
        tid = plan_task_id.strip() or "this plan"
        return (
            "[System: You are a Hermes plan worker bound to task "
            f"`{tid}`. A plain-text reply is NOT a terminal state for the "
            "plan.\n\n"
            "The current plan step is still open. Ending now without "
            "submitting leaves the step stalled with no worker.\n\n"
            "Do this immediately in your next response — do not narrate "
            "intent:\n"
            "1. Finish the step's deliverable (write the required file(s) "
            "now).\n"
            "2. Call `plan advance` with your step summary if the step is "
            "done, OR `plan fail` if it cannot be done, OR `plan block` "
            "if you are blocked (a manual plan task cannot be blocked — "
            "it only notifies the user and stops).\n\n"
            "Never end a turn with only a promise of future action.]"
        )
    if session_called_kanban_terminal(messages):
        return None

    tid = (task_id or os.environ.get("HERMES_KANBAN_TASK") or "").strip() or "this task"
    return (
        "[System: You are a Hermes kanban worker. A plain-text reply is NOT a "
        "terminal state for the board.\n\n"
        f"Task `{tid}` is still `running`. Ending now without a board tool "
        "causes a protocol violation (clean exit with no "
        "`kanban_complete` / `kanban_block`).\n\n"
        "Do this immediately in your next response — do not narrate intent:\n"
        "1. Finish any remaining deliverable (write the required file(s) now).\n"
        "2. Call `kanban_complete(summary=..., artifacts=[...])` if the work "
        "is done, OR `kanban_block(reason=...)` if you are blocked.\n\n"
        "Never end a turn with only a promise of future action. Repeated "
        "protocol violations will block this task and require manual intervention.]"
    )


__all__ = [
    "build_kanban_stop_nudge",
    "kanban_stop_nudge_enabled",
    "session_called_kanban_terminal",
    "session_called_plan_terminal",
]
