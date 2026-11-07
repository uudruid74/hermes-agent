"""Slash-command plan helpers — /review and /goal on the Manual backend.

t_6a55f4d6 step 5 of the plan/clarify/review unification (approved spec,
vault/wiki/Projects/hermes-agent/design/2026-10-10-plan-clarify-review-unification.md,
workstream 3): ``/review <task_id>`` reads the Manual kanban backend; ``/goal``
additionally writes its plan row there.

API-compatible with upstream (F8): upstream ``/review [instructions]`` spawns a
reviewer subagent and ``/plan [task]`` writes markdown — this fork's surfaces
instead ride the one ask-a-reviewer loop (review the current step of an
in-progress plan, reply ``plan review``; a completed task's final result). The
``/goal`` fusion risk (F9) is dodged deliberately: goal semantics (judge,
budget, subgoals) are unchanged; only WHERE the plan is written changed
(Manual kanban, yolo'd — no approval process, no goal-manager state fusion).
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Optional, Tuple

from hermes_cli.plan_limits import cap_text

#: Cap for the completion-notice text written to the task row (the plain-prose
#: sibling of the plan-text caps; four steps' worth is plenty for a notice).
_RESULT_TEXT_MAX_CHARS = 4 * 240

#: Agent-identifying attributes probed on the session harness object. Both
#: surfaces may run without a live agent (agent=None is legal throughout) —
#: identity is best-effort and only used for the /goal plan-row write.
_AGENT_ATTRS = ("agent", "_agent")

_DISPATCH_HINT = (
    "reply 'plan review' (task_id + decision + response) to review the step"
)


def _resolve_agent(harness):
    for attr in _AGENT_ATTRS:
        obj = getattr(harness, attr, None)
        if obj is not None:
            return obj
    return None


def _open_conn(board: Optional[str] = None) -> sqlite3.Connection:
    from hermes_cli.kanban_db import kanban_db_path

    conn = sqlite3.connect(str(kanban_db_path(board)))
    conn.row_factory = sqlite3.Row
    return conn


def _task_row(conn, task_id: str):
    return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()


def _render_step_review(task, pending) -> str:
    """Render the in-progress branch: the current step awaiting a plan review."""
    steps = json.loads(task["task_steps"] or "[]")
    stepno = task["task_stepno"]
    plan_kind = task["plan_kind"] if "plan_kind" in task.keys() else "normal"
    debug_note = ""
    if plan_kind == "debug":
        debug_note = (
            "\n\nThis is a debug plan — rate the coder's output and respond "
            "with a rating decision."
        )
    step_text = (
        steps[stepno - 1] if steps and stepno and 0 < stepno <= len(steps) else ""
    )
    summary = pending.summary if pending is not None else "(no step summary submitted yet)"
    return (
        f"Plan {task['id']} is in progress (status: {task['status']}) — "
        f"review Step {stepno} of {len(steps)}.\n\n"
        f"Step: {step_text}\n\n"
        f"Worker summary: {summary}\n\n"
        f"Worker: {pending.worker if pending is not None else task['assignee']} "
        f"(profile "
        f"{pending.profile if pending is not None else task['assignee']}). "
        f"{_DISPATCH_HINT}{debug_note}"
    )


def _render_final_review(task) -> str:
    """Render the completed-task branch: the final result for review."""
    steps = json.loads(task["task_steps"] or "[]")
    result = (task["result"] or "").strip()
    if result:
        result_block = f"Final result: {result}"
    elif steps:
        last = steps[-1]
        result_block = f"Final step was: {last} (no result text recorded)"
    else:
        result_block = "No result text recorded."
    goal = task["task_goal"] or ""
    return (
        f"Task {task['id']} is completed (status: {task['status']}) — "
        f"review the final result.\n\n"
        f"Goal: {goal}\n\n{result_block}"
    )


def _pending_clarify_ask(conn, task_id: str):
    """Newest unanswered ``clarify_queue`` ask parked against ``task_id``."""
    return conn.execute(
        "SELECT id, task_id, agent_name, question FROM clarify_queue "
        "WHERE task_id=? AND status='pending' ORDER BY created_at DESC LIMIT 1",
        (task_id,),
    ).fetchone()


def answer_clarify_ask(conn, ask_id: str, answer: str) -> None:
    """Record a dispatcher's answer to a ``clarify_queue`` ask (F2 channel).

    The worker is parked inside ``clarify_tool`` polling this row, so this
    write IS the delivery — the answer reaches the worker verbatim through
    the parked tool call. One writer for both responder surfaces (the
    ``/review`` command and ``cmd_review``'s ``response`` field) so the two
    can never disagree on what "answered" means (t_ad4b4a96).
    """
    from hermes_cli.kanban_db import write_txn

    with write_txn(conn):
        conn.execute(
            "UPDATE clarify_queue SET status='answered', answer=?, "
            "answered_at=? WHERE id=?",
            (answer, int(time.time()), ask_id),
        )


def plan_review_command(args: str) -> Tuple[bool, str]:
    """``/review <task_id> [answer]`` — the review command (F2 dual-mode).

    With answer text and a pending clarifying question on the task, this is
    the **clarify answer** channel (Evan 2026-10-20: a worker's clarify is
    answered with ``/review`` — NOT ``hermes kanban answer``, NOT ``kanban
    review``, NOT ``plan review``, NOT anything new). The response is recorded
    on the durable ask and returned to the parked worker verbatim.

    With no answer text this is the read-only Manual-backend review surface:
    in-progress returns the current step's pending review with the worker's
    summary so the human can reply ``plan review`` (the existing cmd_review
    loop — identical behavior, no second review system built). Completed:
    returns the task's final result. Upstream /review semantics (independent
    reviewer subagent) are out of scope — F8, implementation is ours.
    """
    parts = (args or "").strip().split(None, 1)
    task_id = parts[0] if parts else ""
    response = parts[1].strip() if len(parts) > 1 else ""
    if not task_id:
        return False, "Usage: /review <task_id> [answer]"
    try:
        conn = _open_conn()
    except (OSError, sqlite3.Error) as exc:
        return False, f"/review failed: {exc}"
    try:
        task = _task_row(conn, task_id)
        if task is None:
            return False, f"Task {task_id} not found"
        # F2 dual-mode: a pending clarify ask turns this review command into
        # a clarify ANSWER instead of a step review — "the response string is
        # literally what the worker receives" (the channel spec).
        ask = _pending_clarify_ask(conn, task_id)
        if ask is not None:
            if not response:
                return True, (
                    f"Task {task_id} has a pending clarifying question from "
                    f"{ask['agent_name']} — answer this clarifying question "
                    f"about task {task_id}:\n\n{ask['question']}\n\n"
                    f"Answer it with the review command:\n"
                    f"  /review {task_id} <your answer>\n"
                    "(or the dashboard). The worker is parked until the "
                    "answer arrives."
                )
            answer_clarify_ask(conn, ask["id"], response)
            return True, (
                f"Clarify answer delivered to worker for {task_id} "
                f"(ask {ask['id']})."
            )
        if response:
            return (
                False,
                f"Task {task_id} has no pending clarifying question to answer.",
            )
        status = task["status"]
        if status == "done":
            return True, _render_final_review(task)
        if status in {"manual", "running", "attention"}:
            from hermes_cli import execution_bindings as bindings

            pending = bindings.pending_step_review(conn, task_id)
            return True, _render_step_review(task, pending)
        # Other statuses (blocked/ready/todo/archived/review/triage/failed):
        # not reviewable through this surface — say what the row is instead
        # of pretending it is a plan under review.
        return (
            False,
            f"Task {task_id} is not reviewable here (status: {status}). "
            "Plan-backed tasks are 'manual'/'running' while in progress and "
            "'done' once completed.",
        )
    except sqlite3.Error as exc:
        return False, f"/review failed: {exc}"
    finally:
        conn.close()


def plan_row_for_goal(agent, title: str, goal_text: str) -> Tuple[bool, str]:
    """``/goal <text>`` — write the plan row to the Manual backend, yolo'd.

    F9 boundary: goal semantics (judge, budget, subgoals, preemption) live in
    the goal manager and are untouched; this writes the durable plan row that
    backs the goal — title = goal headline, single step = goal text, status
    forced ``manual`` (the Manual backend), ``plan_auto_approve=1`` (yolo'd:
    no approval process), durable authorization pre-approved with the goal
    itself as approval reason. The row is claimable with ``plan continue
    <task_id>`` exactly like any other plan.

    Returns ``(ok, message)``. Failure is non-fatal for /goal — the caller
    surfaces it as a notice; the session goal is already set by then.
    """
    title = cap_text((title or "").strip())
    goal_text = (goal_text or "").strip()
    if not goal_text:
        return False, "/goal: goal text is empty"
    if not title:
        title = goal_text[:240]

    creator = "user"
    creator_session = None
    if agent is not None:
        creator = (
            getattr(agent, "agent_name", None) or getattr(agent, "username", None) or "user"
        )
        creator_session = getattr(agent, "session_id", None)

    task_id = f"t_{uuid.uuid4().hex[:8]}"
    body = "\n".join(
        [
            f"## Goal: {title}",
            f"**Agent:** {creator}",
            f"**Goal:** {goal_text}",
            "",
            "### Steps",
            f"1. {goal_text}",
        ]
    )
    try:
        conn = _open_conn()
    except (OSError, sqlite3.Error) as exc:
        return False, f"/goal plan-row write failed: {exc}"
    try:
        from hermes_cli.kanban_db import write_txn
        from hermes_cli import plan_authorizations

        with write_txn(conn):
            # Durable authorization, yolo'd: create the pending row then
            # resolve it approved in the same transaction (F9 — no approval
            # process; the goal itself is the approval reason).
            plan_authorizations.create_pending_plan(
                conn,
                plan_authorizations.PlanRequest(
                    plan_id=task_id,
                    payload={
                        "title": title,
                        "goal": goal_text,
                        "steps": [goal_text],
                        "kind": "manual",
                        "assign": creator,
                        "board": "default",
                        "root": None,
                        "cron": None,
                        "resume": None,
                    },
                    execution_task_id=task_id,
                    execution_session_id=creator_session,
                    parent_task_id=None,
                    origin_session_id=creator_session,
                ),
            )
            plan_authorizations.resolve_plan_in_txn(
                conn,
                task_id,
                "approved",
                plan_authorizations.ApprovalActor(
                    session_id=creator_session,
                    actor=creator,
                    via="slash /goal (yolo'd, F9)",
                ),
                reason=f"goal-approved: {goal_text[:_RESULT_TEXT_MAX_CHARS]}",
            )
            conn.execute(
                """
                INSERT INTO tasks (
                    id, title, body, status, assignee, created_at, task_steps,
                    task_stepno, task_goal, block_kind, prev_temperature,
                    previous_task, session_id, board, plan_kind, pre_approved,
                    created_by, plan_auto_approve
                ) VALUES (?, ?, ?, 'manual', ?, ?, ?, 1, ?, NULL, ?, NULL, ?, ?, 'normal', 1, ?, 1)
                """,
                (
                    task_id,
                    title,
                    body,
                    creator,
                    int(time.time()),
                    json.dumps([goal_text]),
                    goal_text,
                    getattr(agent, "_session_temperature", None) if agent is not None else None,
                    creator_session,
                    "default",
                    creator,
                ),
            )
            plan_authorizations.resolve_plan_in_txn(
                conn,
                task_id,
                "approved",
                plan_authorizations.ApprovalActor(
                    session_id=creator_session,
                    actor=creator,
                    via="slash /goal (yolo'd, F9)",
                ),
                reason=f"goal-approved: {goal_text[:_RESULT_TEXT_MAX_CHARS]}",
            )
        return True, task_id
    except sqlite3.Error as exc:
        return False, f"/goal plan-row write failed: {exc}"
    finally:
        conn.close()
