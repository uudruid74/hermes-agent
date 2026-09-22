"""Execution-binding-only adapters for live Plan commands.

The legacy helpers in :mod:`tools.plan_tool` are retained for provenance,
but live command dispatch resolves runtime identity exclusively through this
module and ``hermes_cli.execution_bindings``.
"""

from __future__ import annotations

import json
import os
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


def _request_completion_compression(agent) -> None:
    """Force one post-tool compaction after an active Plan terminates."""
    agent._force_compression_after_plan_completion = True


def _sync_subject_from_binding(agent) -> None:
    """Re-point the session subject at the active Plan's current step.

    Called after every command that can change *what the active step is*:
    activate (new / approve / continue / corrective child), advance, and
    close (which restores a parent Plan or leaves none).

    The subject string is the full/heap compaction gate (Evan, 2026-09-18):
    a change makes the next compaction discard the Protected area and rebuild
    it — a *full* compaction — while an unchanged subject only rebuilds the
    Heap and leaves the cacheable prefix byte-stable.  Tying it to the active
    step makes a step change the full-compaction gate, and gives every
    compaction a searchable subject string.

    No binding means no active Plan, so the subject is **cleared** rather
    than left on a finished step.  A stale subject would never change
    again, so the Protected area would accumulate globals forever and
    never take a full compaction — the opposite of the intent.

    Never raises.  An unbound session or any read error leaves the
    subject untouched, which is the pre-2026-09-18 behaviour.
    """
    session_id = getattr(agent, "session_id", None)
    session_db = getattr(agent, "_session_db", None)
    if not session_id or session_db is None:
        return
    try:
        from hermes_cli import execution_bindings as bindings

        conn = _legacy()._get_kanban_db()
        _key, binding, error = _current(conn, agent)
        subject = ""
        if not error and binding is not None:
            task = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (binding.task_id,)
            ).fetchone()
            if task is not None:
                steps, step_no = bindings._steps_for_task(task)
                title = task["title"] or ""
                step_text = " ".join((steps[step_no - 1] or "").split())
                subject = f"{title}: {step_text}" if title else step_text
    except Exception:
        return
    try:
        session_db.set_session_subject(session_id, subject)
    except Exception:
        pass


def _insert_request(
    conn, *, task_id: str, title: str, goal: str, steps: list[str], agent,
    board: Optional[str], kind: str, parent_task_id: Optional[str], pre_approved: bool,
    debug_plan_id: Optional[str], assignee: Optional[str] = None,
) -> str:
    """Create a blocked Plan and its pending durable authorization atomically.

    ``goal`` and ``steps`` are whitespace-compressed and head-capped here as
    well as in the adapter (Evan, 2026-09-16): this is the durable write, so
    a caller reaching the kernel directly gets the same bounded Plan rather
    than only callers that entered through ``plan_tool``.  ``cap_text`` is
    idempotent, so applying it twice is a no-op.
    """
    from hermes_cli import plan_authorizations
    from hermes_cli.plan_limits import cap_steps, cap_text

    goal = cap_text(goal)
    steps = cap_steps(steps)

    legacy = _legacy()
    now = int(time.time())
    task_assignee = (assignee or "").strip() or legacy._get_agent_name(agent)
    body = "\n".join(
        [
            f"## Plan: {title}",
            f"**Agent:** {task_assignee}",
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
            task_assignee,
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
                "assign": task_assignee,
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
    # Delegated plan (assignee set): notifications must return to the
    # DISPATCHER (the agent that created this plan), not the worker profile.
    # Stamp the creator's durable session as origin routing.
    if task_assignee != legacy._get_agent_name(agent):
        try:
            from hermes_cli.kanban_db import store_origin_routing

            creator_session = (getattr(agent, "session_id", None) or "").strip()
            if creator_session:
                store_origin_routing(
                    conn,
                    task_id,
                    platform="session",
                    chat_id=creator_session,
                    profile=(os.environ.get("USERNAME") or "").strip() or "user",
                )
        except (sqlite3.Error, ValueError, OSError, ImportError):
            pass  # best-effort; status-notify falls back to session notices
    if kind == "debug" and debug_plan_id:
        linked = conn.execute(
            "UPDATE tasks SET debug_plan_id=? WHERE id=?",
            (task_id, debug_plan_id),
        ).rowcount
        if linked != 1:
            raise ValueError(f"debug source task {debug_plan_id} not found")
    return body


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
    parent_task_id: Optional[str] = None, assignee: Optional[str] = None,
) -> str:
    """Create a Plan without implicit nesting or legacy task identity reads."""
    return _create_plan(
        agent,
        title=title,
        goal=goal,
        steps=steps,
        temp=temp,
        board=board,
        kind=kind,
        debug_plan_id=debug_plan_id,
        pre_approved=pre_approved,
        parent_task_id=parent_task_id,
        assignee=assignee,
    )


def _create_plan(
    agent, *, title: str, goal: str, steps: list[str], temp: Optional[str] = None,
    board: Optional[str] = None, kind: str = "normal",
    debug_plan_id: Optional[str] = None, pre_approved: bool = False,
    parent_task_id: Optional[str] = None,
    repeat_of: Optional[tuple[str, int]] = None,
    assignee: Optional[str] = None,
) -> str:
    """Create, approve and activate a Plan.

    ``repeat_of`` marks the Plan as a CORRECTIVE child of ``(parent, step)``:
    the parent's step summary is cleared now (the claim it held was wrong) and
    restored from this child's completion summary when the child closes.
    """
    from hermes_cli import execution_bindings as bindings
    from hermes_cli.kanban_db import write_txn
    from hermes_cli.plan_limits import (
        cap_steps, cap_text, goal_text_error, step_count_error, steps_text_error,
    )

    if not title or not goal or not steps:
        return "ERROR: 'new' requires title, goal, and steps[]"
    kind = (kind or "normal").strip().lower()
    if kind not in {"normal", "debug"}:
        return "ERROR: 'kind' must be 'normal' or 'debug'"
    over_limit = step_count_error(steps)
    if over_limit:
        return over_limit
    # Refuse rather than truncate (Evan, 2026-09-18) — see text_limit_error.
    over_limit = goal_text_error(goal)
    if over_limit:
        return over_limit
    over_limit = steps_text_error(steps)
    if over_limit:
        return over_limit
    # Bound the Plan at submission (Evan, 2026-09-16).  The Plan is the
    # Protected region of the context window, so its size is a permanent
    # per-turn cost; nothing previously capped it and a live goal reached
    # 6,907 chars.  `_insert_request` re-applies these idempotently.
    goal = cap_text(goal)
    steps = cap_steps(steps)
    try:
        key = _identity(agent)
    except bindings.PlanStateUnavailable as exc:
        return f"PLAN_STATE_UNAVAILABLE: {exc}"

    conn = _legacy()._get_kanban_db(board)
    try:
        current = bindings.get_binding(conn, key)
        # Plans nest implicitly (Evan, 2026-09-15).  `new` while a Plan is
        # active creates a CHILD of that Plan: the new row stores the active
        # task id in `previous_task`, and closing the child restores the
        # parent binding (`_close_plan_in_txn`), so nesting works to any
        # depth.  Refusing instead of nesting was the bug — an active
        # binding is the normal, expected state, not a lock.
        if current is not None and parent_task_id is None:
            parent_task_id = current.task_id
        if current is None and parent_task_id is not None:
            return f"PLAN_CONFLICT: expected parent {parent_task_id}, but no Plan is active"
        if (
            current is not None
            and parent_task_id is not None
            and parent_task_id != current.task_id
        ):
            return f"PLAN_CONFLICT: active Plan is {current.task_id}, not {parent_task_id!r}"

        task_id = f"t_{uuid.uuid4().hex[:8]}"
        with write_txn(conn):
            plan_body = _insert_request(
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
                assignee=assignee,
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
                    f"Approve plan {task_id}?\n\n{plan_body}",
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
    # Active step changed (new step-1) — re-point the subject so the change
    # triggers a FULL compaction.  See _sync_subject_from_binding.
    _sync_subject_from_binding(agent)

    if repeat_of is not None:
        parent_id, parent_step = repeat_of
        with write_txn(conn):
            conn.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    task_id,
                    _legacy()._get_agent_name(agent),
                    f"REPEAT-OF:{parent_id}:{parent_step}",
                    int(time.time()),
                ),
            )
            # The claim the parent held for this step was wrong — drop it so the
            # step stands open until this child's summary replaces it.
            bindings._clear_step_summary(conn, parent_id, parent_step)

    resolved_temp = _legacy()._resolve_temp(temp, agent)
    if resolved_temp is not None:
        agent._session_temperature = resolved_temp
    if repeat_of is not None:
        parent_id, parent_step = repeat_of
        return (
            f"CORRECTIVE PLAN APPROVED ({task_id}): {title}\n\n"
            f">>> STEP {parent_step} of {parent_id} re-opened as a child plan.\n"
            f"STEP {parent_step} OF THIS PLAN: {steps[0]} <<<\n\n"
            f"Work the corrective plan. When its final step completes, the "
            f"parent reopens at step {parent_step} carrying this plan's "
            f"completion summary."
        )
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


def cmd_advance(
    agent, summary: str, proof: Optional[str] = None, step: Optional[int] = None
) -> str:
    """Complete the active plan step.

    This adapter SHADOWS ``plan_tool._cmd_advance`` — live ``advance`` calls
    resolve here, not to the legacy helper — so when the two-phase completion
    gate (``execution_bindings.advance_plan``, Evan 2026-09-15) was added, it
    became this function's job to forward ``proof``.  It does.

    Two-phase completion: without ``proof`` the step is recorded as a bare
    claim and returned unchanged ("STEP n NOT ADVANCED — verify before
    claiming"); with ``proof`` a ``RECEIPT:n:<proof>`` comment is written and
    the step advances.  Both ``proof`` and ``step`` are load-bearing here —
    ``step`` is what makes a drifted or duplicate advance refuse instead of
    moving the plan forward.  Do not drop either when refactoring.
    """
    from hermes_cli import execution_bindings as bindings

    conn = _legacy()._get_kanban_db()
    key, binding, error = _current(conn, agent)
    if error:
        return error
    if key is None or binding is None:
        return "ERROR: No active task"
    try:
        result = bindings.advance_plan(
            conn,
            key,
            expected_task_id=binding.task_id,
            expected_revision=binding.revision,
            summary=summary,
            actor=_legacy()._get_agent_name(agent),
            proof=proof,
            step=step,
        )
    except bindings.ExecutionBindingError as exc:
        return f"ERROR: {exc}"
    if result.closed:
        task = conn.execute(
            "SELECT task_goal, plan_kind FROM tasks WHERE id=?", (result.task_id,)
        ).fetchone()
        # Execution bindings are authoritative, but sessions.task_id remains a
        # compatibility signal for session UI/context.  Mirror the atomic close:
        # clear a finished root Plan or point at the restored parent Plan.
        session_db = getattr(agent, "_session_db", None)
        session_id = getattr(agent, "session_id", None)
        set_task_id = getattr(session_db, "set_session_task_id", None)
        if callable(set_task_id) and isinstance(session_id, str) and session_id:
            set_task_id(session_id, result.restored_task_id)
        if task["plan_kind"] == "debug":
            source = conn.execute(
                "SELECT assignee FROM tasks WHERE debug_plan_id=? ORDER BY created_at DESC LIMIT 1",
                (result.task_id,),
            ).fetchone()
            session_db = getattr(agent, "_session_db", None)
            if source is not None and session_db is not None:
                session_db.update_agent_rating(source["assignee"], 0.5)
        # Closing restores a parent Plan or leaves none — either way the
        # active step is a different step (or there is no Plan).  Re-point the
        # subject so the transition takes a FULL compaction; with no Plan the
        # subject is CLEARED.  See _sync_subject_from_binding.
        _sync_subject_from_binding(agent)
        _request_completion_compression(agent)
        return f"The task goal was: {task['task_goal'] or ''}"
    if result.binding_revision == binding.revision:
        # The step did NOT move — this is the verify-first response.
        return (
            f"STEP {result.step_no} NOT ADVANCED — verify before claiming.\n\n"
            f">>> STEP {result.step_no}: {result.next_step} <<<\n\n"
            f"Return to the ground-truth source and confirm this step's goal is "
            f"actually met. If it is, submit checkable proof by calling `advance` "
            f"again for step {result.step_no} with `proof` — a git commit hash, a "
            f"test result, a file path, or the command you ran and its output. A "
            f"summary alone is a claim, not evidence.\n\n"
            f"If the step is NOT done and needs real work, call `repeat` with "
            f"step={result.step_no} plus a corrective plan (title, goal, steps) — "
            f"that opens a child plan, and the parent resumes here when it "
            f"completes."
        )
    # The active step moved — re-point the subject.  The verify-first branch
    # above returns before this line and writes nothing, so a step that did
    # not advance leaves the Protected area in place.
    _sync_subject_from_binding(agent)
    return f"Complete Step {result.step_no}: {result.next_step}"


def cmd_repeat(
    agent,
    reason: str = "",
    step: Optional[int] = None,
    title: Optional[str] = None,
    goal: Optional[str] = None,
    steps: Optional[list] = None,
) -> str:
    """Re-open a step as a CORRECTIVE CHILD PLAN.

    `repeat` does not just clear a claim — it demands a fix.  The caller passes
    the step it is re-opening plus a plan for correcting it (title, goal,
    steps).  The step text becomes the child plan's goal by default; the child
    gets its own steps and its own log.  When the child completes, the parent
    reopens at that step carrying the child's completion summary, and resumes
    where it left off.
    """
    from hermes_cli import execution_bindings as bindings

    conn = _legacy()._get_kanban_db()
    key, binding, error = _current(conn, agent)
    if error:
        return error
    if key is None or binding is None:
        return "ERROR: No active task"

    task = conn.execute("SELECT * FROM tasks WHERE id=?", (binding.task_id,)).fetchone()
    if task is None:
        return f"ERROR: Task {binding.task_id} not found"
    try:
        all_steps, active_step = bindings._steps_for_task(task)
    except bindings.ExecutionBindingError as exc:
        return f"ERROR: {exc}"

    # Explicit step: the caller names which step it is re-opening.  Omitting it
    # targets the active step (a bare `repeat` still works).
    target = step if step is not None else active_step
    if not isinstance(target, int) or not 1 <= target <= len(all_steps):
        return (
            f"ERROR: step {target} is outside 1..{len(all_steps)} for plan "
            f"{binding.task_id}"
        )
    step_title = all_steps[target - 1]

    # Think before you act: a corrective plan is REQUIRED, not optional.
    if not steps:
        return (
            f"STEP {target} RE-OPEN REQUIRES A CORRECTIVE PLAN — think first.\n\n"
            f">>> The step to fix (its text becomes the new plan's goal):\n"
            f"{step_title} <<<\n\n"
            f"Call `repeat` again with `step={target}` plus `title`, `goal` and "
            f"`steps` describing how you will actually complete it. That becomes "
            f"a child plan with its own steps and its own log."
            + (f"\n\nReason given: {reason.strip()}" if reason.strip() else "")
        )

    if not title or not goal:
        return (
            f"ERROR: 'repeat' with steps requires title and goal "
            f"(step {target}: {step_title})"
        )

    return _create_plan(
        agent,
        title=title,
        goal=goal,
        steps=list(steps),
        parent_task_id=binding.task_id,
        repeat_of=(binding.task_id, target),
    )


def cmd_handoff(agent, summary: str) -> str:
    from hermes_cli import execution_bindings as bindings

    conn = _legacy()._get_kanban_db()
    key, binding, error = _current(conn, agent)
    if error:
        return error
    if key is None or binding is None:
        return "ERROR: No active task"
    try:
        result = bindings.handoff_plan(
            conn,
            key,
            expected_task_id=binding.task_id,
            expected_revision=binding.revision,
            summary=summary,
            actor=_legacy()._get_agent_name(agent),
        )
    except bindings.ExecutionBindingError as exc:
        return f"ERROR: {exc}"
    return (
        f"Handoff recorded for {result.task_id}, Step {result.step_no}: "
        f"{summary}"
    )


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
    session_id = getattr(agent, "session_id", None)
    set_task_id = getattr(session_db, "set_session_task_id", None)
    if callable(set_task_id) and isinstance(session_id, str) and session_id:
        set_task_id(session_id, result.restored_task_id)
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
    # Closing restores a parent Plan or leaves none — either way the active
    # step is a different step (or there is no Plan).  Re-point the subject so
    # the transition takes a FULL compaction; with no Plan the subject is
    # cleared.  See _sync_subject_from_binding.
    _sync_subject_from_binding(agent)
    _request_completion_compression(agent)
    return f"Plan {result.task_id} {outcome}; restored {result.restored_task_id or 'no parent'}"


def cmd_fail(agent, reason: str = "") -> str:
    return _terminal(agent, "failed", reason)


def cmd_test_complete(agent) -> str:
    return _terminal(agent, "test-complete", None)


def _format_plan(conn, task, *, include_summaries: bool, minimal: bool = False) -> str:
    from hermes_cli import execution_bindings as bindings

    steps = json.loads(task["task_steps"] or "[]")
    stepno = task["task_stepno"] or 1
    summaries = (
        bindings.plan_step_summaries(conn, task["id"])
        if include_summaries
        else {}
    )
    if minimal:
        current_step = steps[stepno - 1] if 0 < stepno <= len(steps) else ""
        lines = [
            f"Goal: {task['task_goal'] or ''}",
            f"Current step {stepno}/{len(steps)}: {current_step}",
        ]
        if stepno in summaries:
            lines.append(f"Summary: {summaries[stepno]}")
        return "\n".join(lines)

    lines = [
        f"Task: {task['title'] or task['id']}",
        f"Status: {task['status']}",
        f"Goal: {task['task_goal'] or ''}",
        f"Step {stepno}/{len(steps)}",
        "",
    ]
    for index, step in enumerate(steps, 1):
        lines.append(f"  {'→' if index == stepno else ' '} Step {index}: {step}")
        if index in summaries:
            lines.append(f"      Summary: {summaries[index]}")
    return "\n".join(lines)


def active_plan_compression_context(agent) -> Optional[tuple[str, str]]:
    """Return full and minimal context for the active Plan without rebinding it."""

    conn = _legacy()._get_kanban_db()
    _key, binding, error = _current(conn, agent)
    if error or binding is None:
        return None
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (binding.task_id,)).fetchone()
    if task is None:
        return None
    return (
        _format_plan(conn, task, include_summaries=True),
        _format_plan(conn, task, include_summaries=True, minimal=True),
    )


_STALE_PLAN_AGE_SECONDS = 15 * 24 * 3600
_DELETE_PLAN_AGE_SECONDS = 30 * 24 * 3600


def _agent_identity_names(agent) -> set[str]:
    """Return the casefolded names this agent may own tasks under.

    Tasks record ``assignee`` from ``agent_name`` (USERNAME), while the
    execution binding records ``profile`` from ``profile_name``.  They are
    usually equal but not guaranteed, so an orphan search must match either.
    """
    names: set[str] = set()
    for attr in ("agent_name", "profile_name"):
        value = getattr(agent, attr, None)
        if isinstance(value, str) and value.strip():
            names.add(value.strip().casefold())
    return names


def _find_orphaned_plan_id(conn, agent) -> Optional[str]:
    """Return the most recent open manual plan owned by this agent, or None.

    A ``/new`` session keeps the plan row open as ``status='manual'`` but
    loses its execution binding, so ``_current`` cannot see it.  This search
    keys on the agent's identity names instead of the session lineage.

    Stale plans are cleaned up as a side effect (bug spec 2026-09-10): plans
    older than 15 days are archived, and plans older than 30 days are hard
    deleted, so a long-dead plan is never resurrected or re-claimed.
    """
    from hermes_cli.kanban_db import delete_task, write_txn

    names = _agent_identity_names(agent)
    if not names:
        return None
    now = int(time.time())
    stale_cutoff = now - _STALE_PLAN_AGE_SECONDS
    delete_cutoff = now - _DELETE_PLAN_AGE_SECONDS
    placeholders = ",".join("?" for _ in names)
    with write_txn(conn):
        rows = conn.execute(
            f"SELECT id, created_at FROM tasks WHERE status = 'manual' "
            f"AND lower(assignee) IN ({placeholders}) AND created_at < ?",
            tuple(names) + (stale_cutoff,),
        ).fetchall()
        for row in rows:
            task_id = row["id"]
            if row["created_at"] < delete_cutoff:
                # Hard delete (cascades child rows, clears the binding FK).
                delete_task(conn, task_id)
            else:
                conn.execute(
                    "DELETE FROM execution_bindings WHERE task_id = ?", (task_id,)
                )
                conn.execute(
                    "UPDATE tasks SET status = 'archived', completed_at = ? "
                    "WHERE id = ?",
                    (now, task_id),
                )
        candidate = conn.execute(
            f"SELECT id FROM tasks WHERE status = 'manual' "
            f"AND lower(assignee) IN ({placeholders}) ORDER BY created_at DESC LIMIT 1",
            tuple(names),
        ).fetchone()
        return candidate["id"] if candidate is not None else None


def _reclaim_orphaned_plan(conn, agent) -> Optional[str]:
    """Reattach the most recent orphaned open plan to the current session.

    Returns the re-attached plan id, or None if there is nothing to reclaim.
    """
    from hermes_cli import execution_bindings as bindings
    from hermes_cli.kanban_db import write_txn

    task_id = _find_orphaned_plan_id(conn, agent)
    if task_id is None:
        return None
    try:
        key = _identity(agent)
    except bindings.PlanStateUnavailable:
        return None
    session_id = getattr(agent, "session_id", None)
    if not isinstance(session_id, str) or not session_id:
        return None
    try:
        with write_txn(conn):
            bindings.continue_plan(
                conn,
                key,
                task_id,
                actor=_legacy()._get_agent_name(agent),
                session_id=session_id,
            )
    except (bindings.ExecutionBindingError, ValueError):
        return None
    return task_id


def resolve_plan_id_for_archive(agent) -> Optional[str]:
    """Resolve the plan id that ``archive`` (no task_id) should close.

    Prefers the active binding; falls back to the most recent orphaned open
    manual plan (without re-attaching it — archiving it needs no binding).
    """
    conn = _legacy()._get_kanban_db()
    _key, binding, error = _current(conn, agent)
    if binding is not None:
        return binding.task_id
    return _find_orphaned_plan_id(conn, agent)


def cmd_archive(agent, task_id: Optional[str] = None) -> str:
    """Archive the resolved plan (active binding or orphaned manual plan).

    The no-task_id form is the escape hatch the reclaim note points at: close
    the plan that was just (possibly wrongly) re-attached.  Runs against the
    consolidated ``_get_kanban_db()`` connection so it shares the board
    resolution of every other live plan command (unlike the legacy
    ``_cmd_archive`` which enumerates board files).
    """
    from hermes_cli.kanban_db import write_txn

    conn = _legacy()._get_kanban_db()
    _key, active_binding, _error = _current(conn, agent)
    if not task_id:
        task_id = resolve_plan_id_for_archive(agent)
        if not task_id:
            return (
                "ERROR: 'archive' requires task_id, and no active or orphaned "
                "plan was found for this agent"
            )
    now = int(time.time())
    with write_txn(conn):
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            return f"ERROR: Task {task_id} not found"
        if row["status"] == "archived":
            return f"ARCHIVED: {task_id}"
        # Clear any execution binding so the archived plan is not re-claimed.
        conn.execute("DELETE FROM execution_bindings WHERE task_id = ?", (task_id,))
        changed = conn.execute(
            "UPDATE tasks SET status = 'archived', completed_at = ?, block_kind = NULL "
            "WHERE id = ?",
            (now, task_id),
        ).rowcount
        if changed != 1:
            return f"ERROR: Task {task_id} could not be archived"
    # Drop the session's task pointer if it still references this plan.
    session_db = getattr(agent, "_session_db", None)
    session_id = getattr(agent, "session_id", None)
    if session_db is not None and isinstance(session_id, str) and session_id:
        try:
            session_db.set_session_task_id(session_id, None)
        except Exception:
            pass
    # No Plan remains active — clear the subject (full compaction).
    _sync_subject_from_binding(agent)
    if active_binding is not None and active_binding.task_id == task_id:
        _request_completion_compression(agent)
    return f"ARCHIVED: {task_id}"


def cmd_remind(agent, task_id: Optional[str] = None) -> str:
    """Show the plan goal and the ACTIVE step only.

    Deliberately does not list the other steps (2026-09-14, Evan): an agent that
    sees every step loses track of which one it is on and starts working on the
    wrong one. One step, then act — the closing line tells it to `advance`.
    `cmd_continue` still prints the whole plan with summaries; that one is for
    first-turn context restoration.
    """
    conn = _legacy()._get_kanban_db()
    reclaimed = False
    if task_id is None:
        _key, binding, error = _current(conn, agent)
        if binding is None:
            # A /new session lost its execution binding while its plan stayed
            # open as 'manual'.  Reclaim the most recent orphaned plan so the
            # work is not silently dropped (2026-09-10 bug).
            task_id = _reclaim_orphaned_plan(conn, agent)
            if task_id is not None:
                reclaimed = True
            elif error and error.startswith("PLAN_STATE_UNAVAILABLE:"):
                return error
            else:
                return "No active plan."
        else:
            task_id = binding.task_id
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if task is None:
        return f"Task {task_id} not found"

    steps = json.loads(task["task_steps"] or "[]")
    stepno = task["task_stepno"] or 1
    lines = [
        f"Task: {task['title'] or task['id']}",
        f"Goal: {task['task_goal'] or ''}",
    ]
    if not steps:
        lines.append("Active Step: (no steps defined)")
    elif 0 < stepno <= len(steps):
        lines.append(f"Active Step {stepno} of {len(steps)}: {steps[stepno - 1]}")
    else:
        lines.append(f"Active Step: (out of range: {stepno} of {len(steps)})")
    lines.append("")
    lines.append(
        "If this step is complete, call `plan_tool advance` with a summary "
        "to get the next step."
    )
    if reclaimed:
        # Re-point the session subject at the recovered plan (full compaction).
        _sync_subject_from_binding(agent)
        lines.append("")
        lines.append(
            "NOTE: This plan was reclaimed from a previous session. If it is "
            "not the plan you intend, call `plan_tool archive` (no task_id) "
            "to delete it."
        )
    return "\n".join(lines)



def cmd_continue(agent, task_id: str) -> str:
    from hermes_cli import execution_bindings as bindings

    conn = _legacy()._get_kanban_db()
    session_id = getattr(agent, "session_id", None)
    if not isinstance(session_id, str) or not session_id:
        return "PLAN_STATE_UNAVAILABLE: agent session is unavailable"
    try:
        key = _identity(agent)
        bindings.continue_plan(
            conn,
            key,
            task_id,
            actor=_legacy()._get_agent_name(agent),
            session_id=session_id,
        )
    except (bindings.ExecutionBindingError, ValueError) as exc:
        return f"ERROR: {exc}"
    # The Plan was transferred to this session — point the subject at its
    # active step.  See _sync_subject_from_binding.
    _sync_subject_from_binding(agent)
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return _format_plan(conn, task, include_summaries=True)


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
    response = callback(f"Approve plan {task_id}?\n\n{task['body']}", ["Approve", "Deny"])
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
    except bindings.ExecutionBindingError as exc:
        return f"ERROR: {exc}"
    # Activating a blocked Plan changes the active step — re-point the subject
    # so the transition triggers a FULL compaction.  See
    # _sync_subject_from_binding.
    _sync_subject_from_binding(agent)
    return f"Task {task_id} approved: {task['title']}"
