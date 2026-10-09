"""Denial reasons must survive context compression via the plan block.

Evan 2026-10-09: plan step denials happen because something got lost or
mangled during compression; if the denial reason itself only lives in
message history / task_events, the next full compaction can wash it out
and the worker re-offends. Fix: store the reason as a
[plan-step-denial:N] task comment (the same durable channel step
summaries ride), render it in _format_plan / cmd_remind, clear it when
the step finally passes review.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools import plan_binding_adapter
from tests.tools.test_plan_step_review import (
    _Agent,
    _db,
    _delegated_plan,
    _stepno,
    _install,
)
from types import SimpleNamespace
import pytest
from hermes_cli.kanban_db import SCHEMA_SQL


def test_deny_stores_reason_and_remind_renders_it(monkeypatch):
    conn, sent, worker, dispatcher, task_id = _delegated_plan(monkeypatch)
    plan_tool = __import__("tools.plan_tool", fromlist=["plan_tool"])
    plan_tool.plan_tool(worker, "advance", summary="did the step", step=1)
    sent.clear()

    result = plan_tool.plan_tool(
        dispatcher,
        "review",
        task_id=task_id,
        decision="denied",
        reason="Missing game_logic import — NameError on init().",
    )
    assert result == f"Task {task_id} Step 1 denied. Worker notified."

    # Stored durably as a comment
    rows = conn.execute(
        "SELECT body FROM task_comments WHERE task_id=? "
        "AND body LIKE '[plan-step-denial:1] %'",
        (task_id,),
    ).fetchall()
    assert [r[0] for r in rows] == [
        "[plan-step-denial:1] Missing game_logic import — NameError on init()."
    ]

    # remind carries the reason under the active step
    remind = plan_tool.plan_tool(worker, "remind")
    assert "Last denial of this step: Missing game_logic import" in remind

    # _format_plan (remind/continue/protected-channel renderer) carries it
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    rendered = plan_binding_adapter._format_plan(conn, task, include_summaries=True)
    assert "Denied: Missing game_logic import — NameError on init()." in rendered
    minimal = plan_binding_adapter._format_plan(
        conn, task, include_summaries=True, minimal=True
    )
    assert "Denied: Missing game_logic import" in minimal


def test_active_plan_compression_context_includes_denial(monkeypatch):
    """The protected-channel text (full-compaction plan_context + per-turn
    injection) must contain the denial reason verbatim."""
    conn, sent, worker, dispatcher, task_id = _delegated_plan(monkeypatch)
    plan_tool = __import__("tools.plan_tool", fromlist=["plan_tool"])
    plan_tool.plan_tool(worker, "advance", summary="did the step", step=1)
    plan_tool.plan_tool(
        dispatcher, "review", task_id=task_id, decision="denied",
        reason="view() shadows builtin list — use fetchall() result directly.",
    )

    contexts = plan_binding_adapter.active_plan_compression_context(worker)
    assert contexts is not None
    full, minimal = contexts
    assert "Denied: view() shadows builtin list" in full
    assert "Denied: view() shadows builtin list" in minimal


def test_repeated_denial_replaces_reason(monkeypatch):
    conn, sent, worker, dispatcher, task_id = _delegated_plan(monkeypatch)
    plan_tool = __import__("tools.plan_tool", fromlist=["plan_tool"])
    plan_tool.plan_tool(worker, "advance", summary="attempt 1", step=1)
    plan_tool.plan_tool(
        dispatcher, "review", task_id=task_id, decision="denied", reason="first defect"
    )
    plan_tool.plan_tool(worker, "advance", summary="attempt 2", step=1)
    sent.clear()
    plan_tool.plan_tool(
        dispatcher, "review", task_id=task_id, decision="denied", reason="second defect — worse"
    )

    rows = conn.execute(
        "SELECT body FROM task_comments WHERE task_id=? "
        "AND body LIKE '[plan-step-denial:1] %'",
        (task_id,),
    ).fetchall()
    assert len(rows) == 1, "re-denial must REPLACE, not accumulate"
    assert rows[0][0] == "[plan-step-denial:1] second defect — worse"


def test_approval_clears_denial(monkeypatch):
    conn, sent, worker, dispatcher, task_id = _delegated_plan(monkeypatch)
    plan_tool = __import__("tools.plan_tool", fromlist=["plan_tool"])
    plan_tool.plan_tool(worker, "advance", summary="first try", step=1)
    plan_tool.plan_tool(
        dispatcher, "review", task_id=task_id, decision="denied", reason="defect A"
    )
    plan_tool.plan_tool(worker, "advance", summary="fixed", step=1)
    sent.clear()
    plan_tool.plan_tool(
        dispatcher, "review", task_id=task_id, decision="approved"
    )

    n = conn.execute(
        "SELECT COUNT(*) FROM task_comments WHERE task_id=? "
        "AND body LIKE '[plan-step-denial:1] %'",
        (task_id,),
    ).fetchone()[0]
    assert n == 0, "approved step must not keep riding its denial"
    # next step's rendered plan block has no stale denial
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    rendered = plan_binding_adapter._format_plan(conn, task, include_summaries=True)
    assert "Denied:" not in rendered


def test_denial_reason_capped(monkeypatch):
    """A 1024-char denial must not grow the Protected region unboundedly."""
    from hermes_cli.plan_limits import PLAN_TEXT_MAX_CHARS

    conn, sent, worker, dispatcher, task_id = _delegated_plan(monkeypatch)
    plan_tool = __import__("tools.plan_tool", fromlist=["plan_tool"])
    plan_tool.plan_tool(worker, "advance", summary="try", step=1)
    plan_tool.plan_tool(
        dispatcher, "review", task_id=task_id, decision="denied",
        reason="x" * 1000 + " THE-END-MARKER",
    )
    row = conn.execute(
        "SELECT body FROM task_comments WHERE task_id=? "
        "AND body LIKE '[plan-step-denial:1] %'",
        (task_id,),
    ).fetchone()
    reason = row[0].removeprefix("[plan-step-denial:1] ")
    assert len(reason) <= PLAN_TEXT_MAX_CHARS
    assert reason.endswith("THE-END-MARKER"), "cap keeps the TAIL (fix directive)"