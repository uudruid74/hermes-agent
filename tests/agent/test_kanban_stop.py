"""Tests for the kanban worker turn-end stop guard."""

from __future__ import annotations

import json

import pytest

from agent.kanban_stop import (
    build_kanban_stop_nudge,
    kanban_stop_nudge_enabled,
    session_called_kanban_terminal,
    session_called_plan_terminal,
)


@pytest.fixture
def clear_kanban_env(monkeypatch):
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_STOP_NUDGE"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch




def test_env_can_disable(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    clear_kanban_env.setenv("HERMES_KANBAN_STOP_NUDGE", "0")
    assert kanban_stop_nudge_enabled() is False
    assert build_kanban_stop_nudge(messages=[]) is None

def test_nudge_when_no_terminal_tool(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_46be8aa5")
    messages = [
        {"role": "user", "content": "work kanban task"},
        {
            "role": "assistant",
            "content": "Let me write the comprehensive recipe.",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_heartbeat", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "kanban_heartbeat", "tool_call_id": "1", "content": "ok"},
    ]
    nudge = build_kanban_stop_nudge(messages=messages, attempts=0)
    assert nudge is not None
    assert "kanban_complete" in nudge
    assert "kanban_block" in nudge
    assert "t_46be8aa5" in nudge
    assert "protocol violation" in nudge.lower() or "protocol" in nudge.lower()

def test_no_nudge_after_kanban_complete(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_complete", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "kanban_complete", "tool_call_id": "1", "content": "done"},
    ]
    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages) is None


# ── Plan-bound worker shape (Evan 2026-10-24, Ornith's mid-step stall) ────
# A long-lived gateway session on a Plan has NO HERMES_KANBAN_TASK — the
# execution binding arms the guard and the vocabulary must be plan, never
# board.


def _plan_call(command: str, **args) -> dict:
    return {
        "id": "1",
        "type": "function",
        "function": {
            "name": "plan",
            "arguments": json.dumps({"command": command, **args}),
        },
    }


def test_plan_bound_worker_without_env_gets_plan_nudge(clear_kanban_env):
    # Acceptance 1: no HERMES_KANBAN_TASK, text-only stop mid-step →
    # the synthetic nudge names `plan advance`, not board tools.
    messages = [
        {"role": "user", "content": "work the step"},
        {
            "role": "assistant",
            "content": "Capture detection works. Let me verify win-by-captures:",
        },
    ]
    nudge = build_kanban_stop_nudge(
        messages=messages, attempts=0, plan_task_id="t_plan1"
    )
    assert nudge is not None
    assert "plan advance" in nudge
    assert "t_plan1" in nudge
    assert "kanban_complete" not in nudge


@pytest.mark.parametrize("command", ["advance", "fail", "block"])
def test_no_nudge_after_plan_terminal(clear_kanban_env, command):
    # Acceptance 2: guard armed via binding, worker submits → no nudge.
    messages = [{"role": "assistant", "content": "", "tool_calls": [_plan_call(command)]}]
    assert session_called_plan_terminal(messages) is True
    assert (
        build_kanban_stop_nudge(messages=messages, plan_task_id="t_plan1") is None
    )


def test_plan_call_without_command_is_not_terminal(clear_kanban_env):
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [_plan_call("remind")]}
    ]
    assert session_called_plan_terminal(messages) is False
    assert build_kanban_stop_nudge(messages=messages, plan_task_id="t_plan1")


def test_plan_bound_requires_plan_terminal_not_kanban(clear_kanban_env):
    # A kanban_complete call does NOT close a plan step — binding-armed
    # guards must keep nudging until plan advance/fail/block.
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_complete", "arguments": "{}"},
                }
            ],
        }
    ]
    nudge = build_kanban_stop_nudge(messages=messages, plan_task_id="t_plan1")
    assert nudge is not None
    assert "plan advance" in nudge


def test_plan_nudge_budget_exhausted(clear_kanban_env):
    # Acceptance 3 (loop half): budget exhausted → return None → the turn
    # exits instead of nudging again.
    assert (
        build_kanban_stop_nudge(
            messages=[], attempts=2, max_attempts=2, plan_task_id="t_plan1"
        )
        is None
    )


def test_plan_mode_wins_when_both_armed(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_env")
    nudge = build_kanban_stop_nudge(messages=[], plan_task_id="t_plan1")
    assert nudge is not None
    assert "plan advance" in nudge
    assert "kanban_complete" not in nudge


def test_plan_mode_honors_explicit_disable(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_STOP_NUDGE", "0")
    assert kanban_stop_nudge_enabled(plan_task_id="t_plan1") is False
    assert (
        build_kanban_stop_nudge(messages=[], plan_task_id="t_plan1") is None
    )




# ── Integration: agent nudge + dispatcher bounded retry ──────────────
# These tests verify the two layers compose correctly: the agent-side
# nudge fires first (up to 2 attempts), and if the worker still exits
# without a terminal call, the dispatcher's bounded retry (streak of 3)
# handles it.  See also tests/hermes_cli/test_kanban_core_functionality.py
# for the dispatcher-side streak tests.




