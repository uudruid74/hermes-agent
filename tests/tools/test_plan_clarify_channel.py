"""F2 clarify channel tests (t_6a55f4d6, build steps 2+3).

Covers:
- plan clarify: dispatcher routing (flag written FIRST, then wake-ping)
- plan review dual-mode: needs_clarify flag → clarify answer, flag cleared
- plan review dual-mode: tool_limit_hit flag → guide answer
- self-dispatched plan → human path (byte-identical clarify_tool callback)
- hard code guard: no dispatcher + no callback → error, nothing written
- review without a response on a pending flag → refused (no step review)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.tools.test_plan_step_review import (  # noqa: E402
    _Agent,
    _delegated_plan,
    _install,
)
from tools import plan_binding_adapter  # noqa: E402
from tools import plan  # noqa: E402
from tools.plan_binding_adapter import cmd_review  # noqa: E402


def test_clarify_writes_flag_then_pings_dispatcher(monkeypatch):
    """flag-first ordering: the durable ask exists in the DB before the send."""
    conn, sent, worker, dispatcher, task_id = _delegated_plan(monkeypatch)
    plan.plan(worker, "advance", summary="working step 1", step=1)
    sent.clear()

    result = plan.plan(
        worker, "clarify", question="Should I use WAL or a plain rollback journal?"
    )
    flag = json.loads(
        conn.execute("SELECT clarify_flag FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
    )
    assert flag["kind"] == "needs_clarify"
    assert "WAL" in flag["question"]
    assert flag["step"] == 1
    assert "routed to dispatcher" in result
    # The wake-ping reaches the dispatcher with the reply instruction
    assert sent and "plan review" in sent[-1][4]
    assert "neo" in result  # the dispatcher's profile is named


def test_review_answers_clarify_flag_instead_of_step_review(monkeypatch):
    conn, sent, worker, dispatcher, task_id = _delegated_plan(monkeypatch)
    plan.plan(worker, "advance", summary="working step 1", step=1)
    plan.plan(worker, "clarify", question="Which branch?")
    sent.clear()

    # review with no response is refused BEFORE any step resolution
    refused = plan.plan(
        dispatcher, "review", task_id=task_id, decision="approved"
    )
    assert "pending needs_clarify" in refused
    # the answer dual-modes the review: no step review happened
    answered = plan.plan(
        dispatcher, "review", task_id=task_id, decision="approved",
        response="Use the default journal mode.",
    )
    assert "Clarify response delivered" in answered
    # flag cleared, worker got the answer verbatim
    assert conn.execute(
        "SELECT clarify_flag FROM tasks WHERE id=?", (task_id,)
    ).fetchone()[0] is None
    assert sent and "Use the default journal mode." in sent[-1][4]
    # step 1 is STILL pending review — the clarify answer never advanced it
    from hermes_cli import execution_bindings as bindings
    assert bindings.pending_step_review(conn, task_id) is not None
    assert _stepno(conn, task_id) == 1


def test_review_answers_tool_limit_flag_as_guide(monkeypatch):
    conn, sent, worker, dispatcher, task_id = _delegated_plan(monkeypatch)
    # Park the plan via the tool-limit path, which now also writes the flag
    monkeypatch.setattr(
        plan_binding_adapter, "_deliver_attention_request", lambda tid, msg: None
    )
    assert plan_binding_adapter.block_active_plan_at_iteration_limit(
        worker, used=40, maximum=40
    )
    flag = json.loads(
        conn.execute("SELECT clarify_flag FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
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


def test_self_dispatched_clarify_routes_to_human(monkeypatch):
    """The hard code guard: self-dispatch goes to the human, byte-identical."""
    conn, _sent = _install(monkeypatch)
    # The human path's first clarify call is the plan APPROVAL gate; the
    # second is the clarify question under test.
    answers = iter(["Approve", "Use WAL."])
    asked: list[tuple[str, list | None]] = []

    def clarify(question, choices=None, **_kwargs):
        asked.append((question, choices))
        return json.dumps({"user_response": next(answers)})

    monkeypatch.setattr(plan, "clarify_tool", clarify)
    worker = _Agent("ornith", "20261010_010107_abcdef")
    plan.plan(
        worker, "new", title="Self plan", goal="Try it", steps=["do it"]
    )
    task_id = conn.execute("SELECT id FROM tasks").fetchone()[0]

    result = plan.plan(
        worker, "clarify", question="Journal mode for the test DB?"
    )
    assert result == "Use WAL."
    assert asked and "Journal mode" in asked[-1][0]
    # Nothing was written to the flag on the human path
    assert conn.execute(
        "SELECT clarify_flag FROM tasks WHERE id=?", (task_id,)
    ).fetchone()[0] is None


def test_clarify_without_dispatcher_or_callback_is_refused(monkeypatch):
    """No self-asking: headless worker on a self-dispatched plan can't
    route anywhere — the guard answers with an error, nothing written."""
    conn, _sent = _install(monkeypatch)
    worker = _Agent("ornith", "20261010_010108_abcdef")
    worker.clarify_callback = lambda *_args, **_kwargs: None
    # The real clarify_tool would block on the (returning-None) callback; stub
    # the boundary and assert the command surfaces the empty answer as an error.
    monkeypatch.setattr(
        plan, "clarify_tool",
        lambda *a, **k: json.dumps({"user_response": ""}),
    )
    plan.plan(
        worker, "new", title="Self plan", goal="Try", steps=["do it"]
    )
    task_id = conn.execute("SELECT id FROM tasks").fetchone()[0]

    result = plan.plan(worker, "clarify", question="Which one?")
    assert "ERROR" in result
    assert conn.execute(
        "SELECT clarify_flag FROM tasks WHERE id=?", (task_id,)
    ).fetchone()[0] is None


def test_clarify_requires_active_plan_and_question(monkeypatch):
    from tests.tools.test_plan_step_review import _install

    class _Bare:
        clarify_callback = None

    conn, _ = _install(monkeypatch)
    assert "requires question" in plan.plan(_Bare(), "clarify")
    # With a question but no resolvable identity/binding, the command reports it.
    bare_result = plan.plan(_Bare(), "clarify", question="hi?")
    assert "ERROR" in bare_result or "PLAN_STATE_UNAVAILABLE" in bare_result


def test_second_clarify_while_one_is_pending_is_refused(monkeypatch):
    conn, _sent, worker, _dispatcher, task_id = _delegated_plan(monkeypatch)
    plan.plan(worker, "advance", summary="working", step=1)
    first = plan.plan(worker, "clarify", question="One?")
    assert "routed to dispatcher" in first
    second = plan.plan(worker, "clarify", question="Two?")
    assert "already has a pending clarify ask" in second


def _stepno(conn, task_id):  # imported shape guard mirrors the review tests
    from tests.tools.test_plan_step_review import _stepno as _s

    return _s(conn, task_id)