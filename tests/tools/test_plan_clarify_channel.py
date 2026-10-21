"""F2 channel tests after ``plan clarify`` removal (t_630b00b0).

Evan 2026-10-20: *"Get the god damn 'plan clarify' out of there and make
clarify work the way I ask."* There is ONE clarify surface — the bare
``clarify`` tool (worker -> dispatcher via the clarify_queue ask, answered
through the ``/review`` command). What remains of the F2 flag channel is the
tool-limit ask, answered through ``plan review``'s dual-mode.

Covers:
- ``plan clarify`` is GONE: the command is rejected, no flag is written
- plan review dual-mode: tool_limit_hit flag -> guide answer
- review without a response on a pending flag -> refused (no step review)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.tools.test_plan_step_review import (  # noqa: E402
    _delegated_plan,
)
from tools import plan  # noqa: E402
from tools import plan_binding_adapter  # noqa: E402


def test_plan_clarify_command_is_gone(monkeypatch):
    """One clarify tool only: `plan clarify` must not exist anywhere."""
    assert not hasattr(plan_binding_adapter, "cmd_clarify")
    conn, _sent, worker, _dispatcher, _task_id = _delegated_plan(monkeypatch)
    result = plan.plan(worker, "clarify")
    assert "Unknown plan command 'clarify'" in result
    # Nothing was written to the F2 flag channel
    row = conn.execute("SELECT clarify_flag FROM tasks").fetchone()
    assert row is None or row[0] is None


def test_review_refuses_without_response_on_pending_flag(monkeypatch):
    conn, _sent, worker, dispatcher, task_id = _delegated_plan(monkeypatch)
    monkeypatch.setattr(
        plan_binding_adapter, "_deliver_attention_request", lambda tid, msg: None
    )
    assert plan_binding_adapter.block_active_plan_at_iteration_limit(
        worker, used=40, maximum=40
    )
    refused = plan.plan(
        dispatcher, "review", task_id=task_id, decision="approved"
    )
    assert "response is required" in refused
    # the flag survives the refusal — the ask is still pending
    flag = json.loads(
        conn.execute(
            "SELECT clarify_flag FROM tasks WHERE id=?", (task_id,)
        ).fetchone()[0]
    )
    assert flag["kind"] == "tool_limit_hit"


def test_review_answers_tool_limit_flag_as_guide(monkeypatch):
    conn, sent, worker, dispatcher, task_id = _delegated_plan(monkeypatch)
    # Park the plan via the tool-limit path, which writes the durable flag
    monkeypatch.setattr(
        plan_binding_adapter, "_deliver_attention_request", lambda tid, msg: None
    )
    assert plan_binding_adapter.block_active_plan_at_iteration_limit(
        worker, used=40, maximum=40
    )
    flag = json.loads(
        conn.execute(
            "SELECT clarify_flag FROM tasks WHERE id=?", (task_id,)
        ).fetchone()[0]
    )
    assert flag["kind"] == "tool_limit_hit"
    assert conn.execute(
        "SELECT status FROM tasks WHERE id=?", (task_id,)
    ).fetchone()[0] == "attention"

    answered = plan.plan(
        dispatcher, "review", task_id=task_id, decision="approved",
        response="Split the step: probe the API first, then rewrite.",
    )
    assert "Clarify response delivered" in answered
    assert conn.execute(
        "SELECT clarify_flag FROM tasks WHERE id=?", (task_id,)
    ).fetchone()[0] is None
    assert sent and "Split the step" in sent[-1][4]


def _stepno(conn, task_id):  # imported shape guard mirrors the review tests
    from tests.tools.test_plan_step_review import _stepno as _s

    return _s(conn, task_id)
