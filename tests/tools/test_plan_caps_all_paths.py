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

2026-09-19 addition — the two-phase ADVANCE gate
------------------------------------------------
``execution_bindings.advance_plan`` (Evan, 2026-09-15) refuses to move a step on
a bare claim: without ``proof`` the step is recorded and returned unchanged, and
a ``RECEIPT:n:<proof>`` comment is what actually advances it.

Live ``advance`` calls do NOT resolve to ``plan_tool._cmd_advance`` — the
adapter in :mod:`tools.plan_binding_adapter` SHADOWS it, so that module's
forwarding of ``proof``/``step`` is the only thing standing between a correct
call and a silently-recorded claim.  That is a one-line refactor away from being
dropped, with no visible symptom except "my proven step won't advance".  The
tests below drive the REAL ``advance_plan`` against a real binding row and pin
the forwarding contract on the adapter, not on the legacy helper.
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


@pytest.fixture()
def hermetic_cron(monkeypatch):
    """Stop ``_cmd_cron`` from writing outside the test.

    ``_cmd_cron`` has TWO real side effects beyond the board, and the first
    version of these tests leaked both (4 orphaned 107-byte scripts in
    ``~/.hermes/scripts/``, found by auditing the directory afterwards):

    1. It writes a fire script to ``os.path.expanduser("~/.hermes/scripts")``
       — a HARDCODED path with no injection point, so it cannot be redirected
       by arguments.  This shims only ``expanduser`` back to the real
       implementation for any other path.
    2. It registers a real cron job via ``cron.jobs.create_job`` — which a test
       must never do (a stray daily job silently dispatches work forever).
    """
    import cron.jobs as cron_jobs

    tmp_scripts = tempfile.mkdtemp()
    real_expanduser = os.path.expanduser

    def _expanduser(path):
        if str(path).startswith("~/.hermes/scripts"):
            return os.path.join(tmp_scripts, os.path.basename(str(path)))
        return real_expanduser(path)

    monkeypatch.setattr(os.path, "expanduser", _expanduser)
    monkeypatch.setattr(
        cron_jobs,
        "create_job",
        lambda **kwargs: {"job_id": "test_job", "id": "test_job", **kwargs},
    )
    return tmp_scripts


def _row(conn, title):
    return conn.execute(
        "SELECT task_goal, task_steps FROM tasks WHERE title = ? ORDER BY created_at DESC LIMIT 1",
        (title,),
    ).fetchone()


def test_dispatch_refuses_a_goal_over_the_cap(board):
    """An over-cap goal is REFUSED, not silently truncated (Evan, 2026-09-18).

    Truncation destroyed a real brief: a 7,038-char dispatch goal was stored as
    246 chars and the worker spent 53 tool calls hunting for text that was never
    stored.  A refusal surfaces at the caller, which still holds the full text.
    """
    conn, plan_tool = board
    result = plan_tool._cmd_dispatch(None, "cap test", LONG_GOAL, "default", "neo")
    assert "ERROR" in result
    assert "goal" in result and "240" in result
    assert "FILE" in result, "the error must point at the supported long-form path"
    assert _row(conn, "cap test") is None, "a refused dispatch must create NOTHING"


def test_dispatch_refuses_a_step_over_the_cap(board):
    conn, plan_tool = board
    result = plan_tool._cmd_dispatch(
        None, "step cap test", "goal", "default", "neo", steps=[LONG_STEP, "short"]
    )
    assert "ERROR" in result
    assert "step 1" in result, "the error must name WHICH step is over"
    assert _row(conn, "step cap test") is None


def test_dispatch_accepts_text_exactly_at_the_cap(board):
    """Boundary: exactly 240 chars (after whitespace compression) must pass."""
    conn, plan_tool = board
    goal = "G" * 240
    result = plan_tool._cmd_dispatch(None, "at cap", goal, "default", "neo", steps=["s"])
    assert "ERROR" not in result, result
    row = _row(conn, "at cap")
    assert row is not None
    assert len(row["task_goal"]) == 240


def test_dispatch_origin_routing_uses_username_only(board, monkeypatch):
    _conn, plan_tool = board
    captured = {}
    monkeypatch.setenv("USERNAME", "neo")
    monkeypatch.setenv("HERMES_PROFILE", "wrong-route")
    monkeypatch.setenv("HERMES_SESSION_PROFILE", "wrong-session-route")
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "123")

    from hermes_cli import kanban_db

    monkeypatch.setattr(
        kanban_db,
        "store_origin_routing",
        lambda _conn, _task_id, **kwargs: captured.update(kwargs),
    )

    result = plan_tool._cmd_dispatch(
        None, "username origin", "route correctly", "default", "neo"
    )

    assert "ERROR" not in result, result
    assert captured["profile"] == "neo"


def test_dispatch_refuses_more_than_twelve_steps(board):
    conn, plan_tool = board
    result = plan_tool._cmd_dispatch(
        None, "too many", "g", "default", "neo", steps=["s"] * 13
    )
    assert "at most 12 steps" in result
    assert "SUBPLANS" in result
    assert _row(conn, "too many") is None, "refused plan must not be created"


def test_cron_refuses_an_over_cap_template(board, hermetic_cron):
    """A cron template's text is copied into every task the job fires."""
    conn, plan_tool = board
    result = plan_tool._cmd_cron(
        None, "0 9 * * *", "/tmp", "cron cap", LONG_GOAL, [LONG_STEP]
    )
    if "cron path unavailable" in result:
        pytest.skip("cron path unavailable in this environment")
    assert "ERROR" in result
    assert _row(conn, "cron cap") is None
    # Proof the fixture held: nothing leaked into ~/.hermes/scripts
    leaked = os.path.join(os.path.expanduser("~/.hermes/scripts"),
                          "plan_cron_cron cap.py")
    assert not os.path.exists(leaked)


def test_cron_refuses_more_than_twelve_steps(board, hermetic_cron):
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


# --------------------------------------------------------------------------
# Two-phase ADVANCE gate — the adapter must forward `proof` and `step`.
#
# These drive the REAL ``advance_plan`` against a real binding row on a temp
# board rather than monkeypatching it away: the failure mode here is a
# FORWARDING contract, and a mocked callee cannot observe what was forwarded.
# --------------------------------------------------------------------------

STEP_TEXT = "make the end() regression read the DB"


@pytest.fixture()
def bound_plan(board):
    """A copy of the real board carrying an active one-step Plan + binding.

    Reuses a real binding row so the identity/resolution path is the
    production one; only ``status``/``task_steps`` are forced into a clean,
    claimable state.
    """
    conn, _plan_tool = board
    from hermes_cli import execution_bindings as B

    task_id = conn.execute(
        "SELECT task_id FROM execution_bindings "
        "ORDER BY updated_at DESC LIMIT 1"
    ).fetchone()
    if task_id is None:
        pytest.skip("no binding row in the board copy")
    task_id = task_id["task_id"]

    conn.execute(
        "UPDATE tasks SET status='manual', task_steps=?, task_stepno=1, "
        "task_goal='advance gate fixture', plan_kind='normal' WHERE id=?",
        (json.dumps([STEP_TEXT]), task_id),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM execution_bindings WHERE task_id=?", (task_id,)
    ).fetchone()
    key = B.ExecutionKey(profile=row["profile"], root_session_id=row["root_session_id"])
    return conn, B, key, task_id, row["revision"]


def test_bare_claim_does_not_advance(bound_plan):
    """No proof => the step is recorded as a claim and comes back unchanged."""
    conn, B, key, task_id, revision = bound_plan
    result = B.advance_plan(
        conn, key, expected_task_id=task_id, expected_revision=revision,
        summary="claim only", actor="gopher", proof=None, step=1,
    )
    assert result.binding_revision == revision, "a bare claim must not move the step"
    assert not result.closed
    assert B._step_receipts(conn, task_id) == set()


def test_proof_advances_and_writes_a_receipt(bound_plan):
    """Proof => the step closes and a RECEIPT comment lands on the task row."""
    conn, B, key, task_id, revision = bound_plan
    result = B.advance_plan(
        conn, key, expected_task_id=task_id, expected_revision=revision,
        summary="verified", actor="gopher", proof="git commit 09f34a1", step=1,
    )
    assert result.closed, "the last step of a one-step plan closes it"
    receipts = B._step_receipts(conn, task_id)
    assert receipts == {1}, "advancing with proof must record a receipt"
    stored = conn.execute(
        "SELECT body FROM task_comments WHERE task_id=? "
        "AND body LIKE 'RECEIPT:%'",
        (task_id,),
    ).fetchone()["body"]
    assert "09f34a1" in stored


def test_adapter_forwards_proof_and_step(bound_plan, monkeypatch):
    """The SHADOWING adapter must pass `proof` and `step` through.

    This contract has no other guard: if a refactor calls the legacy helper
    with ``summary`` alone, every proven advance degrades into a recorded
    claim and the only symptom is a step that will not move.
    """
    conn, B, key, task_id, _revision = bound_plan
    from tools import plan_binding_adapter as adapter

    class _Agent:
        session_id = "sess"
        profile_name = key.profile
        _session_db = None
        _session_temperature = None

    seen = {}
    real = B.advance_plan

    def _spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(B, "advance_plan", _spy)
    # The adapter reaches the board via `_legacy()._get_kanban_db()` WITHOUT a
    # `with`, so it needs the connection itself — the `board` fixture's
    # nullcontext (correct for `plan_tool`'s `with` sites) would blow up here.
    monkeypatch.setattr(adapter._legacy(), "_get_kanban_db", lambda board=None: conn)
    monkeypatch.setattr(
        adapter, "_current", lambda c, a: (key, B.require_binding(c, key), None)
    )
    monkeypatch.setattr(adapter, "_sync_subject_from_binding", lambda agent: None)

    adapter.cmd_advance(_Agent(), "verified step", proof="git commit 09f34a1", step=1)
    assert seen.get("proof") == "git commit 09f34a1", "adapter dropped `proof`"
    assert seen.get("step") == 1, "adapter dropped `step`"


def test_step_guard_refuses_a_drifted_step(bound_plan):
    """A wrong step number is refused instead of silently advancing."""
    conn, B, key, task_id, revision = bound_plan
    with pytest.raises(B.InvalidTaskState):
        B.advance_plan(
            conn, key, expected_task_id=task_id, expected_revision=revision,
            summary="lost agent", actor="gopher", proof="x", step=7,
        )
