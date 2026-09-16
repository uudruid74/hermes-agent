"""Regression tests: the Plan-text caps must cover EVERY creation path.

Evan, 2026-09-16 spec: cap goal/steps/summaries at 240 chars on submission.
The first implementation capped only ``plan_tool new``.  Measured on the live
board afterwards: **141 tasks over the limit**, including a ``ready`` one at
505 chars — work waiting to be picked up that was already over budget.
``dispatch`` is the most common way work enters the board, and it writes the
same ``task_goal`` / ``task_steps`` columns ``_format_plan`` renders into the
Protected region.

These tests exist so that gap cannot reopen: they exercise each creation path
against a temp copy of a real board and assert on the STORED row, not on the
function's return value.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, ".")

REAL_BOARD = "/home/ekl/.hermes/kanban/kanban.db"

LONG_GOAL = "Dispatch goal " + "Q" * 700 + " THE REAL END"
LONG_STEP = "s" * 400


@pytest.fixture()
def board(monkeypatch):
    """A throwaway copy of the real board, wired into plan_tool.

    A temp copy, never the live DB: ``_cmd_dispatch``/``_cmd_cron`` write
    rows, and a test that pollutes the production board is worse than no test.
    """
    if not os.path.exists(REAL_BOARD):
        pytest.skip("live kanban DB not present")
    from tools import plan_tool

    tmp = tempfile.mkdtemp()
    db_path = os.path.join(tmp, "kanban.db")
    shutil.copy(REAL_BOARD, db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    monkeypatch.setattr(plan_tool, "_get_kanban_db", lambda board=None: contextlib.nullcontext(conn))
    yield conn, plan_tool
    conn.close()


def _row(conn, title):
    return conn.execute(
        "SELECT task_goal, task_steps FROM tasks WHERE title = ? ORDER BY created_at DESC LIMIT 1",
        (title,),
    ).fetchone()


def test_dispatch_caps_the_goal(board):
    conn, plan_tool = board
    plan_tool._cmd_dispatch(None, "cap test", LONG_GOAL, "default", "neo")
    row = _row(conn, "cap test")
    assert row is not None, "dispatch did not create the task"
    assert len(row["task_goal"]) == 240
    # Head, not tail: a goal states its objective up front.
    assert row["task_goal"].startswith("Dispatch goal")


def test_dispatch_caps_each_step(board):
    conn, plan_tool = board
    plan_tool._cmd_dispatch(
        None, "step cap test", "goal", "default", "neo", steps=[LONG_STEP, "short"]
    )
    row = _row(conn, "step cap test")
    steps = json.loads(row["task_steps"])
    assert [len(s) for s in steps] == [240, 5]


def test_dispatch_refuses_more_than_twelve_steps(board):
    conn, plan_tool = board
    result = plan_tool._cmd_dispatch(
        None, "too many", "g", "default", "neo", steps=["s"] * 13
    )
    assert "at most 12 steps" in result
    assert "SUBPLANS" in result
    assert _row(conn, "too many") is None, "refused plan must not be created"


def test_cron_caps_the_template(board):
    """A cron template's text is copied into every task the job fires."""
    conn, plan_tool = board
    plan_tool._cmd_cron(
        None, "0 9 * * *", "/tmp", "cron cap", LONG_GOAL, [LONG_STEP]
    )
    row = _row(conn, "cron cap")
    if row is None:
        pytest.skip("cron path unavailable in this environment")
    assert len(row["task_goal"]) == 240
    assert [len(s) for s in json.loads(row["task_steps"])] == [240]


def test_cron_refuses_more_than_twelve_steps(board):
    conn, plan_tool = board
    result = plan_tool._cmd_cron(
        None, "0 9 * * *", "/tmp", "cron too many", "g", ["s"] * 13
    )
    assert "at most 12 steps" in result


def test_insert_request_caps_the_durable_write(board):
    """The kernel-level write caps too, so no caller can bypass by skipping `new`.

    ``_cmd_new`` resolves agent identity through the execution-binding kernel
    and returns ``PLAN_STATE_UNAVAILABLE`` without a live profile, so it cannot
    be driven from a test harness.  ``_insert_request`` is where its text
    actually lands, and capping there means the durable row is bounded
    regardless of which entry point produced it.
    """
    conn, _plan_tool = board
    from tools import plan_binding_adapter as adapter

    adapter._insert_request(
        conn,
        task_id="t_insertcap",
        title="insert cap",
        goal=LONG_GOAL,
        steps=[LONG_STEP, "short"],
        agent=None,
        board="default",
        kind="normal",
        parent_task_id=None,
        pre_approved=False,
        debug_plan_id=None,
    )
    conn.commit()
    row = conn.execute(
        "SELECT task_goal, task_steps FROM tasks WHERE id = 't_insertcap'"
    ).fetchone()
    assert row is not None
    assert len(row["task_goal"]) == 240
    assert [len(s) for s in json.loads(row["task_steps"])] == [240, 5]


def test_insert_request_cap_is_idempotent(board):
    """Applied at both the adapter and the kernel — must not double-truncate."""
    conn, _plan_tool = board
    from tools import plan_binding_adapter as adapter
    from hermes_cli.plan_limits import cap_text

    adapter._insert_request(
        conn,
        task_id="t_insertcap2",
        title="insert cap 2",
        goal=LONG_GOAL,
        steps=[LONG_STEP],
        agent=None,
        board="default",
        kind="normal",
        parent_task_id=None,
        pre_approved=False,
        debug_plan_id=None,
    )
    conn.commit()
    stored = conn.execute(
        "SELECT task_goal FROM tasks WHERE id = 't_insertcap2'"
    ).fetchone()["task_goal"]
    assert cap_text(stored) == stored
