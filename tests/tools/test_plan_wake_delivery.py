"""Verified worker wakes for plan-review responses (t_1098032b).

2026-11-10 incident: Gopher answered a tool_limit_hit ask via ``plan review
... response=``; the tool returned "Clarify response delivered to worker …
Park released" while the wake never reached Ornith's worker session
(Evan: "the review didn't land. He never got the wake message"). Two defects
stacked: ``wake_target_for`` addressed the bare profile (the binding lives
in ``execution_bindings``, not ``sessions.task_id`` — shape side pinned in
``test_wake_target_shape``), and ``_send_user`` claimed delivery on process
LAUNCH alone (the detached-spawn false green).

These tests pin the delivery leg:
- ``_send_user`` returns success only on CONFIRMED delivery (the canonical
  ``_bridge_cli_wake`` contract: run to completion + JSON result check)
- unconfirmed/failed sends are reported honestly — never "delivered"
- the wake carries the review answer as the worker's inbound user turn

Wake restraint (agent-wake-routing): an autouse hostage guard holds
``subprocess.run`` — a test that forgets to fake the transport fails loud
instead of firing a real 'hermes send'.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.tools.test_plan_step_review import (  # noqa: E402
    _delegated_plan,
)
from tools import plan  # noqa: E402
from tools import plan_binding_adapter  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_wake(monkeypatch):
    """Hostage guard: fails loud on a real wake send."""

    def _guard(*_args, **_kwargs):
        raise AssertionError(
            "real 'hermes send' attempted — fake the wake transport in the test"
        )

    monkeypatch.setattr("subprocess.run", _guard)


def _fake_send(monkeypatch, *, ok=True, raises=None, sink=None):
    """Fake the verified send transport. ``raises`` is an exception class to
    throw instead of returning (transport crash / timeout)."""

    class _Proc:
        returncode = 0 if ok else 1
        stdout = json.dumps(
            {"delivered": True} if ok else {"error": "injection refused"}
        )
        stderr = ""

    def _run(argv, **_kwargs):
        if sink is not None:
            sink.append(list(argv))
        if raises is not None:
            if raises is subprocess.TimeoutExpired:
                raise raises(cmd=list(argv), timeout=90)
            raise raises("transport crashed")
        return _Proc()

    monkeypatch.setattr("subprocess.run", _run)


def test_send_user_confirmed_delivery_returns_none(monkeypatch):
    _fake_send(monkeypatch, ok=True)
    assert plan_binding_adapter._send_user("ornith", "hello") is None


def test_send_user_unconfirmed_delivery_returns_failure(monkeypatch):
    _fake_send(monkeypatch, ok=False)
    failure = plan_binding_adapter._send_user("ornith", "hello")
    assert failure is not None
    assert "did not confirm delivery" in failure
    assert "ornith" in failure


@pytest.mark.parametrize("exc", [OSError, subprocess.TimeoutExpired])
def test_send_user_transport_crash_is_a_failure(monkeypatch, exc):
    _fake_send(monkeypatch, raises=exc)
    failure = plan_binding_adapter._send_user("ornith", "hello")
    assert failure is not None
    assert "did not confirm delivery" in failure


def _parked_plan(monkeypatch):
    """A delegated plan parked at the tool-call limit with the ask pending."""
    conn, sent, worker, dispatcher, task_id = _delegated_plan(monkeypatch)
    monkeypatch.setattr(
        plan_binding_adapter, "_deliver_attention_request", lambda tid, msg: None
    )
    assert plan_binding_adapter.block_active_plan_at_iteration_limit(
        worker, used=40, maximum=40
    )
    return conn, sent, worker, dispatcher, task_id


def test_review_reports_delivered_only_on_confirmed_wake(monkeypatch):
    conn, _sent, _worker, dispatcher, task_id = _parked_plan(monkeypatch)
    _fake_send(monkeypatch, ok=True)
    answered = plan.plan(
        dispatcher,
        "review",
        task_id=task_id,
        decision="approved",
        response="Split the step: probe first.",
    )
    assert "Clarify response delivered" in answered
    assert "notification failed" not in answered


def test_review_reports_failure_not_delivered_when_wake_unconfirmed(
    monkeypatch,
):
    """The t_1098032b acceptance: an unconfirmed wake must NEVER be claimed
    delivered — the dispatcher sees the failure instead of a false green."""
    conn, _sent, _worker, dispatcher, task_id = _parked_plan(monkeypatch)
    _fake_send(monkeypatch, ok=False)
    answered = plan.plan(
        dispatcher,
        "review",
        task_id=task_id,
        decision="approved",
        response="Split the step: probe first.",
    )
    assert "Clarify response delivered" not in answered
    assert "notification failed" in answered
    # The response itself is durable even when the wake fails.
    assert "recorded for" in answered


def test_review_reports_failure_on_transport_crash(monkeypatch):
    _conn, _sent, _worker, dispatcher, task_id = _parked_plan(monkeypatch)
    _fake_send(monkeypatch, raises=OSError)
    answered = plan.plan(
        dispatcher,
        "review",
        task_id=task_id,
        decision="approved",
        response="Split the step: probe first.",
    )
    assert "Clarify response delivered" not in answered
    assert "notification failed" in answered


def test_the_send_carries_the_review_answer_as_the_worker_turn(monkeypatch):
    """The wake body is the inbound user turn the worker must see: the
    ask, the task/step, and the answer verbatim."""
    _conn, _sent, _worker, dispatcher, task_id = _parked_plan(monkeypatch)
    sink: list[list[str]] = []
    _fake_send(monkeypatch, ok=True, sink=sink)
    plan.plan(
        dispatcher,
        "review",
        task_id=task_id,
        decision="approved",
        response="Split the step: probe first.",
    )
    wake = sink[-1]
    assert wake[:3] == ["hermes", "send", "-t"]
    body = wake[4]
    assert "responded to your tool-call-limit ask on" in body
    assert task_id in body
    assert "Split the step: probe first." in body
