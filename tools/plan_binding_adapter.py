"""Execution-binding-only adapters for live Plan commands.

The historical helpers in :mod:`tools.plan_tool` are retained for provenance,
but live command dispatch resolves runtime identity exclusively through this
module and ``hermes_cli.execution_bindings``.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Optional


def _legacy():
    from tools import plan_tool

    return plan_tool


def _identity(agent):
    from hermes_cli.execution_bindings import identity_for_agent

    return identity_for_agent(agent)


def _actor(agent):
    from hermes_cli.plan_authorizations import ApprovalActor

    return ApprovalActor(
        session_id=getattr(agent, "session_id", None),
        actor=_legacy()._get_agent_name(agent),
        via="plan_tool",
    )


def _insert_request(
    conn, *, task_id: str, title: str, goal: str, steps: list[str], agent,
    board: Optional[str], kind: str, parent_task_id: Optional[str], pre_approved: bool,
    debug_plan_id: Optional[str],
) -> None:
    """Create a blocked Plan and its pending durable authorization atomically."""
    from hermes_cli import plan_authorizations

    legacy = _legacy()
    now = int(time.time())
    body = "\n".join(
        [
            f"## Plan: {title}",
            f"**Agent:** {legacy._get_agent_name(agent)}",
            f"**Goal:** {goal}",
            "",
            "### Steps",
            *[f"{index}. {step}" for index, step in enumerate(steps, 1)],
        ]
    )
    conn.execute(
        """
        INSERT INTO tasks (
            id, title, body, status, assignee, created_at, task_steps,
            task_stepno, task_goal, block_kind, prev_temperature,
            previous_task, session_id, board, plan_kind, pre_approved
        ) VALUES (?, ?, ?, 'blocked', ?, ?, ?, 1, ?, 'approval', ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            title,
            body,
            legacy._get_agent_name(agent),
            now,
            json.dumps(steps),
            goal,
            getattr(agent, "_session_temperature", None),
            parent_task_id,
            getattr(agent, "session_id", None),
            legacy._resolve_board(board),
            kind,
            int(bool(pre_approved)),
        ),
    )
    plan_authorizations.create_pending_plan(
        conn,
        plan_authorizations.PlanRequest(
            plan_id=task_id,
            payload={
                "title": title,
                "goal": goal,
                "steps": steps,
                "kind": "manual",
                "assign": None,
                "board": legacy._resolve_board(board),
                "root": None,
                "cron": None,
                "resume": None,
            },
            execution_task_id=task_id,
            execution_session_id=getattr(agent, "session_id", None),
            parent_task_id=parent_task_id,
            origin_session_id=getattr(agent, "session_id", None),
        ),
    )
    if kind == "debug" and debug_plan_id:
        linked = conn.execute(
            "UPDATE tasks SET debug_plan_id=? WHERE id=?",
            (task_id, debug_plan_id),
        ).rowcount
        if linked != 1:
            raise ValueError(f"debug source task {debug_plan_id} not found")


def _activate(conn, *, agent, key, task_id: str, parent_task_id: Optional[str], revision: Optional[int]):
    from hermes_cli import execution_bindings as bindings
    from hermes_cli import plan_authorizations

    plan_authorizations.resolve_plan_in_txn(conn, task_id, "approved", _actor(agent))
    return bindings.activate_plan(
        conn,
        key,
        task_id,
        expected_parent_task_id=parent_task_id,
        expected_revision=revision,
    )


def cmd_new(
    agent, title: str, goal: str, steps: list[str], temp: Optional[str] = None,
    board: Optional[str] = None, kind: str = "normal",
    debug_plan_id: Optional[str] = None, pre_approved: bool = False,
    parent_task_id: Optional[str] = None,
) -> str:
    """Create a Plan without implicit nesting or legacy task identity reads."""
    from hermes_cli import execution_bindings as bindings
    from hermes_cli.kanban_db import write_txn

    if not title or not goal or not steps:
        return "ERROR: 'new' requires title, goal, and steps[]"
    kind = (kind or "normal").strip().lower()
    if kind not in {"normal", "debug"}:
        return "ERROR: 'kind' must be 'normal' or 'debug'"
    try:
        key = _identity(agent)
    except bindings.PlanStateUnavailable as exc:
        return f"PLAN_STATE_UNAVAILABLE: {exc}"

    conn = _legacy()._get_kanban_db(board)
    try:
        current = bindings.get_binding(conn, key)
        if current is not None and parent_task_id != current.task_id:
            return (
                f"ACTIVE_PLAN: {current.task_id} is active at binding revision "
                f"{current.revision}; supply parent_task_id={current.task_id!r} to nest."
            )
        if current is None and parent_task_id is not None:
            return f"PLAN_CONFLICT: expected parent {parent_task_id}, but no Plan is active"

        task_id = f"t_{uuid.uuid4().hex[:8]}"
        with write_txn(conn):
            _insert_request(
                conn,
                task_id=task_id,
                title=title,
                goal=goal,
                steps=steps,
                agent=agent,
                board=board,
                kind=kind,
                parent_task_id=parent_task_id,
                pre_approved=pre_approved,
                debug_plan_id=debug_plan_id,
            )
            if kind == "debug" and pre_approved:
                _activate(
                    conn,
                    agent=agent,
                    key=key,
                    task_id=task_id,
                    parent_task_id=parent_task_id,
                    revision=current.revision if current is not None else None,
                )
                approved = True
            else:
                approved = False
    except (bindings.ExecutionBindingError, sqlite3.Error, ValueError) as exc:
        return f"ERROR: Failed to create Plan: {exc}"

    if not approved:
        callback = getattr(agent, "clarify_callback", None)
        if callback is None:
            return f"Plan awaiting approval ({task_id}): no approval callback is available."
        try:
            response = json.loads(
                _legacy().clarify_tool(
                    f"Approve plan {task_id}?\n\n{title}",
                    choices=["Approve", "Deny"],
                    callback=callback,
                    agent=agent,
                    task_id=task_id,
                )
            ).get("user_response", "")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return f"User unavailable: {exc}. Plan remains blocked for approval."
        timed_out = (
            not response
            or str(response).strip().lower()
            == "user unavailable. stand down and wait for the user to return. do nothing else."
        )
        if timed_out:
            agent._plan_approval_timed_out = task_id
            return f"Plan awaiting approval ({task_id}): no user response was received."
        try:
            with write_txn(conn):
                if "appr" in str(response).lower():
                    _activate(
                        conn,
                        agent=agent,
                        key=key,
                        task_id=task_id,
                        parent_task_id=parent_task_id,
                        revision=current.revision if current is not None else None,
                    )
                    approved = True
                else:
                    from hermes_cli import plan_authorizations

                    plan_authorizations.resolve_plan_in_txn(
                        conn, task_id, "denied", _actor(agent), reason=str(response)
                    )
                    conn.execute(
                        "UPDATE tasks SET status='archived', completed_at=? "
                        "WHERE id=? AND status='blocked'",
                        (int(time.time()), task_id),
                    )
                    approved = False
        except (bindings.ExecutionBindingError, sqlite3.Error, ValueError) as exc:
            return f"ERROR: Failed to resolve Plan approval: {exc}"
        if not approved:
            return f"Plan denied ({task_id}): {response}."

    resolved_temp = _legacy()._resolve_temp(temp, agent)
    if resolved_temp is not None:
        agent._session_temperature = resolved_temp
    return f"TASK APPROVED ({task_id}): {title}\n\n>>> STEP 1: {steps[0]} <<<"


def _current(conn, agent):
    from hermes_cli import execution_bindings as bindings

    try:
        key = _identity(agent)
        binding = bindings.get_binding(conn, key)
    except bindings.PlanStateUnavailable as exc:
        return None, None, f"PLAN_STATE_UNAVAILABLE: {exc}"
    if binding is None:
        return None, None, "ERROR: No active task"
    return key, binding, None


def cmd_done(agent, status: Optional[str] = None) -> str:
    from hermes_cli import execution_bindings as bindings

    conn = _legacy()._get_kanban_db()
    key, binding, error = _current(conn, agent)
    if error:
        return error
    try:
        result = bindings.advance_plan(
            conn,
            key,
            expected_task_id=binding.task_id,
            expected_revision=binding.revision,
            status_note=status,
            actor=_legacy()._get_agent_name(agent),
        )
    except bindings.ExecutionBindingError as exc:
        return f"ERROR: {exc}"
    if result.closed:
        task = conn.execute(
            "SELECT task_goal, plan_kind FROM tasks WHERE id=?", (result.task_id,)
        ).fetchone()
        if task["plan_kind"] == "debug":
            source = conn.execute(
                "SELECT assignee FROM tasks WHERE debug_plan_id=? ORDER BY created_at DESC LIMIT 1",
                (result.task_id,),
            ).fetchone()
            session_db = getattr(agent, "_session_db", None)
            if source is not None and session_db is not None:
                session_db.update_agent_rating(source["assignee"], 0.5)
        return f"The task goal was: {task['task_goal'] or ''}"
    return f"Complete Step {result.step_no}: {result.next_step}"


def _terminal(agent, outcome: str, reason: Optional[str]) -> str:
    from hermes_cli import execution_bindings as bindings

    conn = _legacy()._get_kanban_db()
    key, binding, error = _current(conn, agent)
    if error:
        return error
    task = conn.execute("SELECT plan_kind FROM tasks WHERE id=?", (binding.task_id,)).fetchone()
    plan_kind = task["plan_kind"] if task is not None else "normal"
    source = None
    if plan_kind == "debug":
        source = conn.execute(
            "SELECT id, assignee FROM tasks WHERE debug_plan_id=? ORDER BY created_at DESC LIMIT 1",
            (binding.task_id,),
        ).fetchone()
    try:
        result = bindings.close_plan(
            conn,
            key,
            expected_task_id=binding.task_id,
            expected_revision=binding.revision,
            outcome=outcome,
            reason=reason,
            actor=_legacy()._get_agent_name(agent),
        )
    except bindings.ExecutionBindingError as exc:
        return f"ERROR: {exc}"

    session_db = getattr(agent, "_session_db", None)
    if plan_kind == "debug" and source is not None and session_db is not None:
        coder = source["assignee"]
        if outcome == "done":
            session_db.update_agent_rating(coder, 0.5)
        elif outcome == "failed":
            bugs = _legacy()._parse_debug_bugs(reason or "")
            if any(bug["crash"] for bug in bugs):
                session_db.update_agent_rating(coder, -1.0)
                session_db.update_agent_rating(_legacy()._get_agent_name(agent), 0.25)
            else:
                session_db.update_agent_rating(coder, -min(1.0, 0.5 * len(bugs)))
                reward = min(
                    1.0,
                    sum(
                        0.5 if bug["severity"] <= 0.5 else max(0.25, 1.0 - bug["severity"])
                        for bug in bugs
                    ),
                )
                session_db.update_agent_rating(_legacy()._get_agent_name(agent), reward)
            conn.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
                (source["id"], _legacy()._get_agent_name(agent),
                 f"DEBUG FAILURE from {result.task_id}: {reason or 'unspecified'}", int(time.time())),
            )
            conn.commit()
    elif outcome == "failed" and session_db is not None:
        session_db.set_session_mood(getattr(agent, "session_id", None), -1.0)
    return f"Plan {result.task_id} {outcome}; restored {result.restored_task_id or 'no parent'}"


def cmd_fail(agent, reason: str = "") -> str:
    return _terminal(agent, "failed", reason)


def cmd_test_complete(agent) -> str:
    return _terminal(agent, "test-complete", None)


def cmd_remind(agent, task_id: Optional[str] = None) -> str:
    conn = _legacy()._get_kanban_db()
    historical = task_id is not None
    if task_id is None:
        _key, binding, error = _current(conn, agent)
        if error:
            return error
        task_id = binding.task_id
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if task is None:
        return f"Task {task_id} not found"
    steps = json.loads(task["task_steps"] or "[]")
    stepno = task["task_stepno"] or 1
    lines = [
        ("Historical task: " if historical else "Task: ") + (task["title"] or task_id),
        f"Status: {task['status']}",
        f"Goal: {task['task_goal'] or ''}",
        f"Step {stepno}/{len(steps)}",
        "",
    ]
    lines.extend(
        f"  {'→' if index == stepno else ' '} Step {index}: {step}"
        for index, step in enumerate(steps, 1)
    )
    return "\n".join(lines)


def cmd_approve(agent, task_id: str) -> str:
    from hermes_cli import execution_bindings as bindings
    from hermes_cli.kanban_db import write_txn

    conn = _legacy()._get_kanban_db()
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if task is None:
        return f"ERROR: Task {task_id} not found"
    if task["status"] != "blocked" or task["block_kind"] != "approval":
        return f"ERROR: Task {task_id} is not awaiting approval"
    callback = getattr(agent, "clarify_callback", None)
    if callback is None:
        return f"ERROR: No clarify callback available. Cannot present plan {task_id} for approval."
    response = callback(f"Approve plan {task_id}: {task['title']}", ["Approve", "Deny"])
    if not response or "appr" not in str(response).lower():
        return f"Plan awaiting approval ({task_id}): no approval was recorded."
    try:
        key = _identity(agent)
        current = bindings.get_binding(conn, key)
        with write_txn(conn):
            _activate(
                conn,
                agent=agent,
                key=key,
                task_id=task_id,
                parent_task_id=task["previous_task"],
                revision=current.revision if current is not None else None,
            )
    except (bindings.ExecutionBindingError, sqlite3.Error, ValueError) as exc:
        return f"ERROR: Failed to activate task: {exc}"
    return f"Task {task_id} approved: {task['title']}"
