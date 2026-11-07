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


# --- t_ad4b4a96: the three live repros (2026-11-06) -----------------------
# 05:52 — the dispatcher's `plan review` response must answer the routed
#          clarify_queue ask (the wake command's own payload).
# 16:35 — the answer must release the park it was parked FOR.
# 06:58 — a park must always carry its durable flag (no park-without-flag),
#          and `plan continue` must be able to un-park a still-bound plan.


def _queue_ask(conn, task_id, question="How should I split this?"):
    """Durable pending ask in clarify_queue — the routed worker question."""
    import time
    import uuid

    ask_id = uuid.uuid4().hex[:16]
    conn.execute(
        "INSERT INTO clarify_queue(id, session_id, task_id, agent_name, "
        "question, choices, multi_select, answer, status, created_at, "
        "answered_at, board) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            ask_id,
            "sess_worker_1",
            task_id,
            "ornith",
            question,
            None,
            0,
            None,
            "pending",
            int(time.time()),
            None,
            "default",
        ),
    )
    conn.commit()
    return ask_id


def test_review_response_answers_routed_clarify_ask(monkeypatch):
    """05:52 repro: `plan review ... response=` — decision-less — must reach
    the clarify_queue row the wake-ping created. Before this fix the
    dispatcher answering the exact command in its own wake text got
    \"has none pending — nothing was recorded\"."""
    conn, _sent, _worker, dispatcher, task_id = _delegated_plan(monkeypatch)
    ask_id = _queue_ask(conn, task_id)

    answered = plan.plan(
        dispatcher, "review", task_id=task_id, response="Belt, watch, glasses"
    )
    assert "Clarify answer delivered" in answered
    row = conn.execute(
        "SELECT status, answer FROM clarify_queue WHERE id=?", (ask_id,)
    ).fetchone()
    assert row[0] == "answered"
    assert row[1] == "Belt, watch, glasses"


def test_review_response_releases_the_park(monkeypatch):
    """16:35 repro: the answer must un-park. The dispatcher saw 'delivered
    to worker' while the plan stayed `attention` for 8h — dead-ended."""
    conn, sent, worker, dispatcher, task_id = _delegated_plan(monkeypatch)
    monkeypatch.setattr(
        plan_binding_adapter, "_deliver_attention_request", lambda tid, msg: None
    )
    assert plan_binding_adapter.block_active_plan_at_iteration_limit(
        worker, used=40, maximum=40
    )
    assert conn.execute(
        "SELECT status FROM tasks WHERE id=?", (task_id,)
    ).fetchone()[0] == "attention"

    answered = plan.plan(
        dispatcher, "review", task_id=task_id, response="Split the step."
    )
    assert "Clarify response delivered" in answered
    assert "Park released" in answered
    # The park is gone AND the ask is cleared — the worker can advance.
    assert conn.execute(
        "SELECT status FROM tasks WHERE id=?", (task_id,)
    ).fetchone()[0] == "manual"
    assert conn.execute(
        "SELECT clarify_flag FROM tasks WHERE id=?", (task_id,)
    ).fetchone()[0] is None
    # The un-park is durable and attributable (16:35: "NO un-park event").
    kinds = [
        row[0]
        for row in conn.execute(
            "SELECT kind FROM task_events WHERE task_id=?", (task_id,)
        )
    ]
    assert "plan-unparked" in kinds
    assert sent and "Split the step." in sent[-1][4]


def test_park_without_durable_flag_aborts(monkeypatch):
    """06:58 defect (a): the flag write must precede the park — a park whose
    flag never landed is a stuck shape no plan command can exit. If the flag
    cannot be written, do not park at all."""
    from hermes_cli import execution_bindings

    conn, _sent, worker, _dispatcher, task_id = _delegated_plan(monkeypatch)
    monkeypatch.setattr(
        plan_binding_adapter, "_deliver_attention_request", lambda tid, msg: None
    )

    def boom(*_a, **_k):
        raise execution_bindings.ExecutionBindingError("flag write failed")

    monkeypatch.setattr(execution_bindings, "set_clarify_flag", boom)
    assert not plan_binding_adapter.block_active_plan_at_iteration_limit(
        worker, used=40, maximum=40
    )
    # No park happened: no attention status, no plan-parked event.
    assert conn.execute(
        "SELECT status FROM tasks WHERE id=?", (task_id,)
    ).fetchone()[0] == "manual"
    kinds = [
        row[0]
        for row in conn.execute(
            "SELECT kind FROM task_events WHERE task_id=?", (task_id,)
        )
    ]
    assert "plan-parked" not in kinds


def test_park_writes_flag_before_wake(monkeypatch):
    """06:58 contract: the durable ask flag exists when the park lands."""
    conn, _sent, worker, _dispatcher, task_id = _delegated_plan(monkeypatch)
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


def test_continue_unparks_still_bound_worker(monkeypatch):
    """16:35 dead-end exit: the worker's own `continue` on its existing
    binding must restore `attention` -> `manual` (no re-bind)."""
    conn, _sent, worker, _dispatcher, task_id = _delegated_plan(monkeypatch)
    monkeypatch.setattr(
        plan_binding_adapter, "_deliver_attention_request", lambda tid, msg: None
    )
    assert plan_binding_adapter.block_active_plan_at_iteration_limit(
        worker, used=40, maximum=40
    )

    resumed = plan.plan(worker, "continue", task_id=task_id)
    assert "ALREADY ACTIVE" in resumed
    assert conn.execute(
        "SELECT status FROM tasks WHERE id=?", (task_id,)
    ).fetchone()[0] == "manual"
