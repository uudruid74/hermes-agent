"""Plan's status mutators must fire kick_dispatch (Evan's design 2026-10-19).

Sibling gap of the t_084728ad fix: every state-changing verb fires one
event-time dispatch tick (kanban.py's _DISPATCH_KICK_ACTIONS pattern).
ERROR results changed nothing, so they must NOT kick.
"""

from __future__ import annotations

import pytest

from tools import plan


@pytest.fixture()
def kicks(monkeypatch):
    calls: list[tuple] = []

    import hermes_cli.kanban as kanban

    monkeypatch.setattr(
        kanban, "kick_dispatch", lambda *a, **kw: calls.append((a, kw))
    )
    return calls


@pytest.mark.parametrize(
    "command, kwargs, handler",
    [
        ("advance", {"summary": "step done"}, "_binding_cmd_advance"),
        ("review", {"decision": "approved"}, "_cmd_review"),
        ("repeat", {"reason": "wrong step"}, "_cmd_repeat"),
        ("fail", {"reason": "cannot proceed"}, "_cmd_fail"),
        ("test-complete", {}, "_cmd_test_complete"),
        ("approve", {"task_id": "t_kick1"}, "_cmd_approve"),
        ("block", {"task_id": "t_kick1"}, "_cmd_block"),
        ("archive", {"task_id": "t_kick1"}, "_cmd_archive_adapter"),
    ],
)
def test_mutator_kicks_dispatch(monkeypatch, kicks, command, kwargs, handler):
    monkeypatch.setattr(plan, handler, lambda *a, **kw: "OK: stub")
    result = plan.plan(None, command, **kwargs)
    assert result == "OK: stub"
    assert len(kicks) == 1


def test_error_result_does_not_kick(monkeypatch, kicks):
    monkeypatch.setattr(plan, "_cmd_approve", lambda *a, **kw: "OK: stub")
    result = plan.plan(None, "approve")  # missing task_id -> ERROR
    assert "ERROR" in str(result)
    assert kicks == []
