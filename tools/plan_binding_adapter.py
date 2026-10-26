"""Execution-binding-only adapters for live Plan commands.

The legacy helpers in :mod:`tools.plan` are retained for provenance,
but live command dispatch resolves runtime identity exclusively through this
module and ``hermes_cli.execution_bindings``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
import uuid
from typing import Literal, Optional, cast

logger = logging.getLogger(__name__)


def _legacy():
    from tools import plan

    return plan


def _identity(agent):
    from hermes_cli.execution_bindings import identity_for_agent

    return identity_for_agent(agent)


def _actor(agent):
    from hermes_cli.plan_authorizations import ApprovalActor

    return ApprovalActor(
        session_id=getattr(agent, "session_id", None),
        actor=_legacy()._get_agent_name(agent),
        via="plan",
    )


def render_approval_prompt(task_id: str, title: str, goal: str, steps) -> str:
    """The compact human-facing approval prompt: title + goal + step TITLES.

    Expansions are deliberately EXCLUDED (Evan 2026-10-24: "It should only
    send the small steps, not the expansions"): a fully expanded plan body
    exceeds Telegram's 4,096-char message cap, the send fails, and the
    approval gate mistook the transport failure for a denial (t_bbe67f44
    was archived that way). The stored ``tasks.body`` keeps the expansions
    for the workers — this is only the prompt. No truncation.
    """
    lines = [f"Approve plan {task_id}?", "", f"## Plan: {title}"]
    goal = str(goal or "").strip()
    if goal:
        lines.append(f"**Goal:** {goal}")
    lines.append("")
    lines.append("### Steps")
    for index, step in enumerate(steps or [], 1):
        lines.append(f"{index}. {step}")
    return "\n".join(lines)


def non_answer_response(response) -> Optional[str]:
    """Return the non-answer notice when *response* is not a human answer.

    Two sentinels must never be parsed as a review decision (D6,
    t_cc772a4c): the gateway's stand-down string (clarify timeout or
    session-boundary cancellation) and the transport-failure marker (the
    clarify prompt was never delivered).  An empty response is the caller's
    existing park-the-worker case and is handled before this runs.  Without
    these checks an expired ask in the step-review path was recorded as a
    DENIAL whose "reason" was the stand-down text itself — absence of
    evidence treated as evidence (the run-7 family).
    """
    from tools.clarify_gateway import CLARIFY_UNDELIVERABLE_MARKER

    text = str(response or "")
    if CLARIFY_UNDELIVERABLE_MARKER in text:
        return "the clarify prompt could not be delivered (transport failure)"
    if text.strip().lower().startswith("user unavailable"):
        return "no answer arrived (the prompt expired or the session reset)"
    return None


def undeliverable_approval_response(task_id: str, response) -> Optional[str]:
    """Return the transport-failure message when ``response`` is not an answer.

    A failed clarify SEND (the ``CLARIFY_UNDELIVERABLE_MARKER`` sentinel)
    means no human was ever asked — it must never fall through the approve/
    deny parsing and archive the plan as "denied" (the run-7 family: absence
    of evidence treated as evidence). The plan stays blocked for approval.
    """
    from tools.clarify_gateway import CLARIFY_UNDELIVERABLE_MARKER

    if CLARIFY_UNDELIVERABLE_MARKER in str(response or ""):
        return (
            f"Plan awaiting approval ({task_id}): the approval prompt could "
            "not be delivered (transport failure). This is NOT a denial — "
            "no human was asked. The plan remains blocked for approval; "
            f"retry with plan(command=\"approve\", task_id=\"{task_id}\") "
            "when delivery is restored."
        )
    return None


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
    expansions: Optional[list[str]] = None,
) -> str:
    """Create a blocked Plan and its pending durable authorization atomically.

    ``goal`` and ``steps`` are whitespace-compressed and head-capped here as
    well as in the adapter (Evan, 2026-09-16): this is the durable write, so
    a caller reaching the kernel directly gets the same bounded Plan rather
    than only callers that entered through ``plan``.  ``cap_text`` is
    idempotent, so applying it twice is a no-op.

    ``expansions`` are per-step spec ground truth (``plan import``): stored
    VERBATIM (never compressed or cut) in ``task_step_expansions``, parallel
    to ``task_steps``.
    """
    from hermes_cli import plan_authorizations
    from hermes_cli.kanban_db import stamp_task_dispatcher
    from hermes_cli.plan_limits import cap_expansions, cap_goal, cap_steps

    goal = cap_goal(goal)
    steps = cap_steps(steps)
    expansions = cap_expansions(expansions) if expansions else None

    legacy = _legacy()
    now = int(time.time())
    task_assignee = (assignee or "").strip() or legacy._get_agent_name(agent)
    body_lines = [
        f"## Plan: {title}",
        f"**Agent:** {task_assignee}",
        f"**Goal:** {goal}",
        "",
        "### Steps",
    ]
    for index, step in enumerate(steps, 1):
        body_lines.append(f"{index}. {step}")
        expansion = (
            expansions[index - 1] if expansions and index <= len(expansions) else ""
        )
        if expansion:
            body_lines.append("   Spec:")
            body_lines.extend(f"   {line}" for line in expansion.splitlines())
    body = "\n".join(body_lines)
    # D4 (t_cc772a4c): a child Plan created with no explicit board inherits
    # its PARENT's board — t_b085ce41 landed on 'default' while its parent
    # lived on 'pente'.  Explicit boards are honoured as given.
    resolved_board = legacy._resolve_board(board)
    if not (board or "").strip() and parent_task_id:
        parent_row = conn.execute(
            "SELECT board FROM tasks WHERE id = ?", (parent_task_id,)
        ).fetchone()
        if parent_row is not None and (parent_row["board"] or "").strip():
            resolved_board = str(parent_row["board"]).strip()
    conn.execute(
        """
        INSERT INTO tasks (
            id, title, body, status, assignee, created_at, task_steps,
            task_step_expansions, task_stepno, task_goal, block_kind,
            prev_temperature, previous_task, session_id, board, plan_kind,
            pre_approved, created_by
        ) VALUES (?, ?, ?, 'blocked', ?, ?, ?, ?, 1, ?, 'approval', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            title,
            body,
            task_assignee,
            now,
            json.dumps(steps),
            json.dumps(expansions) if expansions else None,
            goal,
            getattr(agent, "_session_temperature", None),
            parent_task_id,
            getattr(agent, "session_id", None),
            resolved_board,
            kind,
            int(bool(pre_approved)),
            legacy._get_agent_name(agent),
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
                "board": resolved_board,
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
                    profile=legacy._get_agent_name(agent),
                )
        except (sqlite3.Error, ValueError, OSError, ImportError) as exc:
            # The durable created_by column is now the primary dispatcher
            # source, so this comment is a fallback only.  Still, a silent
            # failure here used to drop dispatcher routing with no signal —
            # log it so the degradation is visible instead of invisible.
            logger.warning(
                "origin routing for plan %s could not be stored: %s",
                task_id,
                exc,
            )
    # D2 (t_cc772a4c): durable sticky-dispatcher stamp, written at creation
    # so the dispatcher survives parent archival, board moves, and nesting
    # depth.  Also inherits the parent's origin routing for use-time wakes.
    stamp_task_dispatcher(conn, task_id, parent_task_id=parent_task_id)
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
    expansions: Optional[list[str]] = None,
) -> str:
    """Create, approve and activate a Plan.

    ``repeat_of`` marks the Plan as a CORRECTIVE child of ``(parent, step)``:
    the parent's step summary is cleared now (the claim it held was wrong) and
    restored from this child's completion summary when the child closes.
    """
    from hermes_cli import execution_bindings as bindings
    from hermes_cli.kanban_db import write_txn
    from hermes_cli.plan_limits import (
        cap_expansions, cap_goal, cap_steps, expansion_text_error, goal_text_error,
        step_count_error, steps_text_error,
    )

    creator_agent_name = _legacy()._get_agent_name(agent)

    if not title or not goal or not steps:
        return "ERROR: 'new' requires title, goal, and steps[]"
    kind = (kind or "normal").strip().lower()
    if kind not in {"normal", "debug"}:
        return "ERROR: 'kind' must be 'normal' or 'debug'"
    assigned = (assignee or "").strip()
    delegated = bool(assigned and assigned != creator_agent_name)
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
    # Expansions are spec ground truth (plan import): refused when over the
    # 2K cap, NEVER truncated.
    over_limit = expansion_text_error(expansions)
    if over_limit:
        return over_limit
    if expansions is not None and len(expansions) != len(steps):
        return (
            "ERROR: expansions must line up 1:1 with steps[] "
            f"({len(expansions)} expansions, {len(steps)} steps)"
        )
    # Bound the Plan at submission (Evan, 2026-09-16).  The Plan is the
    # Protected region of the context window, so its size is a permanent
    # per-turn cost; nothing previously capped it and a live goal reached
    # 6,907 chars.  `_insert_request` re-applies these idempotently.
    goal = cap_goal(goal)
    steps = cap_steps(steps)
    if expansions is not None:
        expansions = cap_expansions(expansions)
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
        #
        # BUT nesting is for YOUR OWN plans only (Evan, 2026-09-29).  A
        # delegated Plan (assignee is another agent) must NOT inherit the
        # caller's active Plan as its parent: the parent is the *creator's*
        # task, and closing the child would restore it onto the WORKER's
        # binding — handing the worker a task assigned to someone else.
        # Observed live: a dispatched child carrying
        # previous_task=<creator's task> closed with outcome=failed and
        # rebound the worker's (profile, root_session_id) to the creator's
        # task, which the worker then resumed.  Your old plan has nothing to
        # do with the plans you dispatch.
        if delegated:
            if parent_task_id is not None:
                return (
                    f"PLAN_CONFLICT: delegated Plan for {assigned!r} cannot "
                    f"nest under {parent_task_id!r}; delegated plans start "
                    f"their own chain"
                )
            parent_task_id = None
        elif current is not None and parent_task_id is None:
            parent_task_id = current.task_id
        if not delegated and current is None and parent_task_id is not None:
            return f"PLAN_CONFLICT: expected parent {parent_task_id}, but no Plan is active"
        if (
            not delegated
            and current is not None
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
                expansions=expansions,
                agent=agent,
                board=board,
                kind=kind,
                parent_task_id=parent_task_id,
                pre_approved=pre_approved,
                debug_plan_id=debug_plan_id,
                assignee=assignee,
            )
            if kind == "debug" and pre_approved and not delegated:
                _activate(
                    conn,
                    agent=agent,
                    key=key,
                    task_id=task_id,
                    parent_task_id=parent_task_id,
                    revision=current.revision if current is not None else None,
                )
                approved = True
            elif kind == "debug" and pre_approved and delegated:
                from hermes_cli import plan_authorizations

                plan_authorizations.resolve_plan_in_txn(
                    conn, task_id, "approved", _actor(agent)
                )
                approved = True
            else:
                approved = False
    except (bindings.ExecutionBindingError, sqlite3.Error, ValueError) as exc:
        return f"ERROR: Failed to create Plan: {exc}"

    if not approved:
        # D3 (t_cc772a4c): when the STICKY dispatcher is an agent other than
        # the creator, the approval belongs to that dispatcher — durable ask
        # + wake (``send_dispatcher_message``), answered through
        # ``plan review <task_id> approved|denied|yolo``.  The interactive
        # callback stays for the human gate (no agent dispatcher resolves, or
        # self-dispatch: the resolved dispatcher IS the creator — the callback
        # carries the choices/buttons, t_c02fc2cd).
        from hermes_cli.kanban_db import send_dispatcher_message

        dispatcher = _dispatcher_for_task(conn, task_id)
        if dispatcher and dispatcher.casefold() != creator_agent_name.casefold():
            prompt = render_approval_prompt(task_id, title, goal, steps)
            text = (
                f"{task_id} → {creator_agent_name} created a Plan that needs "
                f"YOUR approval — you are its dispatcher.\n\n{prompt}\n\n"
                "Approve, deny, or yolo it with:\n"
                f"  plan review {task_id} approved|denied|yolo\n"
                "A denial must include a response "
                f"('plan review {task_id} denied <reason>')."
            )
            ok, detail = send_dispatcher_message(
                conn,
                task_id,
                direction="to-dispatcher",
                kind="approval",
                text=text,
                actor=creator_agent_name,
            )
            suffix = "" if ok else f" Delivery problem: {detail}."
            return (
                f"Plan awaiting approval ({task_id}): routed to dispatcher "
                f"{dispatcher}.{suffix} Park and wait for the review — do not "
                "work the plan until it arrives."
            )
        callback = getattr(agent, "clarify_callback", None)
        if callback is None:
            return f"Plan awaiting approval ({task_id}): no approval callback is available."
        try:
            response = json.loads(
                _legacy().clarify_tool(
                    render_approval_prompt(task_id, title, goal, steps),
                    choices=["Approve", "Deny", "Yolo"],
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
        # A failed prompt SEND is not a human answer — never let it reach the
        # approve/deny parse (which would archive the plan as "denied").
        undelivered = undeliverable_approval_response(task_id, response)
        if undelivered:
            return undelivered
        # Three answers: Approve, Deny, Yolo. Evan's rule: yolo is a property
        # of the HUMAN's answer, never something an agent specifies. Order
        # matters — "Yolo" contains no "appr", so testing plain-approve first
        # would fall through to the deny branch and ARCHIVE the plan. Match
        # the stop-asking form first, then plain approve, else deny.
        _answer = str(response).strip().lower()
        _yolo = _answer.startswith("yolo")
        _approve = _answer.startswith("appr")
        try:
            with write_txn(conn):
                if _yolo or _approve:
                    if _yolo:
                        conn.execute(
                            "UPDATE tasks SET plan_auto_approve = 1 WHERE id = ?",
                            (task_id,),
                        )
                    if delegated:
                        # Delegated plan: approve the authorization so the
                        # worker's `plan continue` binds cleanly, but do NOT
                        # activate into the creator's binding and do NOT flip
                        # the task to manual — it stays blocked, which
                        # continue_plan accepts (780f7f99cc), so the WORKER's
                        # continue does blocked→manual→bind on their session.
                        from hermes_cli import plan_authorizations

                        plan_authorizations.resolve_plan_in_txn(
                            conn, task_id, "approved", _actor(agent)
                        )
                        approved = True
                    else:
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
    if not delegated:
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
    if delegated:
        # Wake the assignee with the continue instruction; the plan stays
        # bound to their session from here, not ours.
        # Evan's exact pseudo text (2026-09-22) — verbatim, nothing added:
        worker_msg = (
            f"You have been assigned a new task.  Please call "
            f"'plan continue {task_id}' to begin the new task."
        )
        task_row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        wrapped = (
            f"Incoming message from {creator_agent_name} follows:\n"
            "---\n"
            f"{worker_msg}\n"
            "---\n"
            f"{_reviewer_identity(conn, task_row)}"
        )
        # Fire-and-forget wake: the assignee is notified out-of-band, so the
        # creator's tool loop never blocks on delivery. Delivery status lives
        # in the durable task row, not in this synchronous call.
        from hermes_cli._subprocess_compat import spawn_detached

        from hermes_cli import kanban_db as kb

        spawn_detached(
            ["hermes", "send", "-t", kb.wake_target_for(conn, task_id, assigned), wrapped]
        )
        # No binding activation on our side — surface the dispatch notice.
        # Evan's exact spec (2026-09-22): the creator sees ONLY the notify line.
        return f"{assigned} has been notified of task {task_id}"
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


def active_plan_task_id(agent) -> Optional[str]:
    """The Plan bound to this session's execution identity, or ``None``.

    Turn-end stop-guard wiring (Evan 2026-10-24): a long-lived gateway
    session on a Plan has no ``HERMES_KANBAN_TASK`` env var — the binding IS
    the worker signal. An unavailable/unbound lookup means "not a plan
    worker" here; the guard then falls back to the env-armed shape.
    """
    conn = _legacy()._get_kanban_db()
    _key, binding, _error = _current(conn, agent)
    return binding.task_id if binding is not None else None


def block_active_plan_at_iteration_limit(agent, *, used: int, maximum: int) -> bool:
    """Park an unsubmitted active Plan step after its turn budget is exhausted.

    A pending step review proves that ``plan advance`` already submitted
    the step, so that state must remain with the reviewer. Otherwise the bound
    Plan has no live worker after this turn and the task parks in the
    dedicated ``attention`` state (park_plan_task) — never ``blocked``, which
    would expose a still-bound task to the dispatcher.
    """
    from hermes_cli import execution_bindings as bindings
    from hermes_cli import kanban_db

    conn = _legacy()._get_kanban_db()
    _key, binding, error = _current(conn, agent)
    if error or binding is None:
        return False
    if bindings.pending_step_review(conn, binding.task_id) is not None:
        return False
    task = conn.execute(
        "SELECT status, task_stepno FROM tasks WHERE id = ?",
        (binding.task_id,),
    ).fetchone()
    if task is None or task["status"] not in {"manual", "running"}:
        return False
    step_no = int(task["task_stepno"] or 1)
    detail = (
        f"Step {step_no} ended at the tool-call limit "
        f"({used}/{maximum}) with no submission"
    )
    parked = kanban_db.park_plan_task(conn, binding.task_id, detail=detail)
    if not parked:
        return False
    # F2 dual-mode: write the durable tool_limit_hit flag BEFORE the wake-ping
    # so the dispatcher's 'plan review' command answers as a guide, not a step
    # review. Annotation-only write; parking has already succeeded.
    from hermes_cli.execution_bindings import (
        ExecutionBindingError as _ebb_error,
        set_clarify_flag as _set_clarify_flag,
    )

    try:
        _set_clarify_flag(
            conn,
            binding.task_id,
            kind="tool_limit_hit",
            question=detail,
            step_no=step_no,
        )
    except _ebb_error as exc:
        logger.warning("tool-limit flag for task %s failed: %s", binding.task_id, exc)
    # The parked-state prompt goes to the plan's dispatcher (an agent), not
    # through the generic kanban notification — this is the "separate prompt"
    # Evan specified. Best-effort: parking itself has already succeeded.
    try:
        full_task = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (binding.task_id,)
        ).fetchone()
        if full_task is not None:
            message = _attention_request_text(
                full_task, full_task, used=used, maximum=maximum
            )
            _deliver_attention_request(binding.task_id, message)
    except (OSError, ValueError, KeyError):
        logger.warning(
            "attention request for task %s could not be delivered",
            binding.task_id,
            exc_info=True,
        )
    return True


def _dispatcher_for_task(conn, task_id: str) -> Optional[str]:
    """Resolve the AI dispatcher that reviews this Plan's steps.

    Thin wrapper over the canonical STICKY resolver
    (``hermes_cli.kanban_db.sticky_dispatcher_for_task``): the durable
    ``tasks.dispatcher`` stamp first, then ``created_by``, the
    ``previous_task`` ancestor walk, and finally origin routing.  A
    self-created Plan's creator IS its assignee, so there is no AI
    dispatcher and the user becomes the approval gate.
    """
    from hermes_cli.kanban_db import sticky_dispatcher_for_task

    return sticky_dispatcher_for_task(conn, task_id)


_REVIEWER_NAME_RE = re.compile(
    r"\bReviewer\*{0,2}\s*[:：]\s*\*{0,2}\s*([A-Za-z0-9][A-Za-z0-9_-]*)"
)


def _reviewer_session_id(
    conn, task, name: str, owner: Optional[str]
) -> Optional[str]:
    """The session id the plan binding records for *name*, if any.

    ``plan_authorizations`` is the plan's durable record of who ran it: the
    session the plan was created from (``origin_session_id``) and the one
    that approved it (``approved_by_session_id``).  ``tasks.session_id`` is
    deliberately NOT used — claiming a plan rewrites that column to the
    worker's session (execution_bindings claim path), so it names the
    worker, never the reviewer.
    """
    row = conn.execute(
        "SELECT approved_by_actor, approved_by_session_id, origin_session_id "
        "FROM plan_authorizations WHERE plan_id = ?",
        (task["id"],),
    ).fetchone()
    if row is None:
        return None
    name_cf = name.strip().casefold()
    if str(row[0] or "").strip().casefold() == name_cf:
        return str(row[1] or "").strip() or None
    if owner and owner.strip().casefold() == name_cf:
        return str(row[2] or "").strip() or None
    return None


def _reviewer_identity(conn, task) -> str:
    """Worker-facing reviewer line: agent name AND session id.

    2026-10-22, incident t_c02fc2cd: plan payloads never named the step
    reviewer, so the worker's "if you know who it is" fallback routed a
    step handoff to the wrong agent (Gopher).  Evan 2026-10-22 (t_cc94fc2d):
    "plan remind and plan continue need to explicitly list the 'reviewer'
    by agent name AND sessionid" — identity is lifted from the plan here so
    the worker never re-reads it from a file.

    Name: the plan text's ``Reviewer:`` line (the plan file's reviewer as
    transferred into the body/steps), else the plan owner — the agent
    ``plan review`` is gated on (``_dispatcher_for_task``).  Session id:
    the plan binding's recorded session for that name, else the line says
    so rather than inventing one.
    """
    owner = _dispatcher_for_task(conn, task["id"])
    plan_text = "\n".join(
        str(task[col] or "") for col in ("title", "body", "task_goal", "task_steps")
    )
    match = _REVIEWER_NAME_RE.search(plan_text)
    name = match.group(1) if match else owner
    if not name:
        return "Reviewer: the human user (interactive session)"
    session_id = _reviewer_session_id(conn, task, name, owner)
    if session_id:
        return f"Reviewer: {name} (session_id={session_id})"
    return f"Reviewer: {name} (session_id not recorded)"


def _send_user(
    profile: str,
    message: str,
    *,
    conn=None,
    task_id: Optional[str] = None,
) -> Optional[str]:
    """Wake *profile* with *message* via ``hermes send -t`` (user-turn inject).

    When ``conn``/``task_id`` are given, the wake target is resolved at use
    time (``hermes_cli.kanban_db.wake_target_for``) — the task's origin
    session/chat or the worker's live bound session — instead of the
    profile home channel.
    """
    from hermes_cli._subprocess_compat import spawn_detached

    target = profile
    if conn is not None and task_id:
        from hermes_cli import kanban_db as kb

        target = kb.wake_target_for(conn, task_id, profile)
    proc = spawn_detached(["hermes", "send", "-t", target, message])
    if proc is None:
        # Spawn failure (binary missing, permission) — surfaced, not buried.
        return f"failed to launch 'hermes send' for {target}"
    return None


def _review_request_text(task, pending, *, auto_approved: bool = False) -> str:
    steps = json.loads(task["task_steps"] or "[]")
    lines = [
        f"{pending.worker} has completed step {pending.step_no}, task id {task['id']}",
        "",
        f"Goal: {task['task_goal'] or ''}",
        "",
        "Plan:",
    ]
    for index, step_text in enumerate(steps, 1):
        marker = "[X]" if index < pending.step_no else "[-]" if index == pending.step_no else "[ ]"
        lines.append(f"{marker} Step {index}: {step_text}")
    lines.extend(["", "Summary:", pending.summary, ""])
    if auto_approved:
        lines.append(
            "YOLO auto-approved this step. Verify the evidence as usual; no human "
            "review click is required."
        )
    else:
        lines.append(
            "Please make sure this step is completed correctly and approve or deny "
            "this step using 'plan review'. Pass the task id and decision "
            "'approved', 'denied', or 'yolo' — 'yolo' approves this step AND "
            "auto-approves the remaining steps of this plan, so you will not be "
            "asked again for this plan. A denial must include a response."
        )
    return "\n".join(lines)


def _last_step_summary(task_id: str, step_no: int) -> str:
    """Return the stored summary for *step_no*, or "" when none exists."""
    from hermes_cli import execution_bindings as bindings

    conn = _legacy()._get_kanban_db()
    summaries = bindings.plan_step_summaries(conn, task_id)
    return summaries.get(step_no, "")


def _deliver_attention_request(task_id: str, message: str) -> None:
    """Send the parked-state prompt to the plan's dispatcher, best-effort.

    A parked plan task's dispatcher is the AI that created the plan (the
    creator when it differs from the assignee), resolved the same way the
    review path resolves its reviewer. The assignee profile is notified as a
    fallback for self-created plans, which have no distinct dispatcher.
    Spawn failure or missing origin is logged and surfaced in the return
    value rather than raised — parking itself has already succeeded.
    """
    conn = _legacy()._get_kanban_db()
    recipient = _dispatcher_for_task(conn, task_id)
    if not recipient:
        row = conn.execute(
            "SELECT assignee FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        recipient = (
            str(row["assignee"]).strip()
            if row and (row["assignee"] or "").strip()
            else None
        )
    if recipient:
        error = _send_user(recipient, message, conn=conn, task_id=task_id)
        if error:
            logger.warning("attention request delivery: %s", error)
    else:
        logger.warning(
            "attention request for task %s has no dispatcher or assignee to notify",
            task_id,
        )


def _attention_request_text(task, pending, *, used: int, maximum: int) -> str:
    """Compose the parked-state prompt for the plan's dispatcher.

    A parked plan task's dispatcher is an agent (the plan's creator), not a
    human operator, so this is built as a sibling of
    :func:`_review_request_text` — same preamble, same goal/plan/summary
    blocks — with the request line steering that agent to ``plan``.
    Kanban routes (dispatch, specify, unblock, archive) are named as "do
    not": on a machine where the assignee's model cannot hold two workers,
    any of them spawns a duplicate worker and the run's measurement is lost.
    """
    steps = json.loads(task["task_steps"] or "[]")
    step_no = int(pending["task_stepno"] or 1)
    last_summary = _last_step_summary(task["id"], step_no)
    expansions = _step_expansions(task)
    lines = [
        f"{task['id']} → Needs Attention — exceeded tool call limit",
        "",
        f"Step {step_no} ended at the tool-call limit ({used}/{maximum}) "
        f"with no submission, task id {task['id']}",
        "",
        f"Goal: {task['task_goal'] or ''}",
        "",
        "Plan:",
    ]
    for index, step_text in enumerate(steps, 1):
        marker = "[X]" if index < step_no else "[-]" if index == step_no else "[ ]"
        lines.append(f"{marker} Step {index}: {step_text}")
        if index == step_no:
            # The active step's spec, verbatim (Evan 2026-10-25) — the worker
            # hit the limit on this step and whoever resumes needs its rules
            # without a plan remind round-trip.
            expansion = expansions.get(index, "")
            if expansion:
                lines.append("")
                lines.append(expansion)
    lines.extend(["", "Summary:", last_summary or "none", ""])
    lines.append(
        "---\n"
        "\n"
        "Investigate with plan. This is a plan-bound task — nothing here "
        "is a kanban problem.\n"
        "\n"
        "- The worker's session is still live and still holds the binding. "
        "Do NOT dispatch, re-specify, unblock, or archive. A state change "
        "here spawns a second worker on a machine that cannot hold one, and "
        "the run's measurement is lost.\n"
        "- Inspect the current step: 'plan remind "
        f"{task['id']}' shows the active step and whether a review is "
        "pending.\n"
        "- When you are ready to resume, re-bind with 'plan continue "
        f"{task['id']}'. That restores the task to 'manual' and wakes the "
        "worker.\n"
        "- If you need to end the step, use 'plan' — not a kanban "
        "command.\n"
        "\n"
        "No reply is required to this notification."
    )
    return "\n".join(lines)


def _worker_review_message(task, result, reviewer: str, response: Optional[str]) -> str:
    steps = json.loads(task["task_steps"] or "[]")
    # Spec ground truth rides with the title (Evan 2026-10-25: a worker
    # receiving "Now complete Step N" must not need `plan remind` to learn
    # what Step N requires). Expansions are stored verbatim
    # (task_step_expansions) and appended verbatim; absent expansion →
    # byte-identical message to the pre-expansion format.
    expansions = _step_expansions(task)
    if result.decision == "denied":
        step_text = steps[result.reviewed_step - 1]
        expansion = expansions.get(result.reviewed_step, "")
        spec = f"\n\n{expansion}" if expansion else ""
        return (
            f"Advancement has been denied. Reason: {response}\n\n"
            f"Goal: {task['task_goal'] or ''}\n"
            f"Step {result.reviewed_step}: {step_text}{spec}\n\n"
            "Please correct before attempting to advance again"
        )
    if result.closed:
        return (
            f"Task {task['id']} Step {result.reviewed_step} Approved by {reviewer}. "
            "Plan complete."
        )
    yolo_note = (
        "\n\nYOLO enabled: later steps will auto-approve after notification."
        if result.auto_approve_enabled
        else ""
    )
    expansion = expansions.get(result.next_step_no, "")
    spec = f"\n\n{expansion}" if expansion else ""
    return (
        f"Task {task['id']} Step {result.reviewed_step} Approved by {reviewer}. "
        f"Now complete Step {result.next_step_no}\n\n"
        f"Goal: {task['task_goal'] or ''}\n"
        f"Step {result.next_step_no}: {result.next_step}{spec}\n\n"
        "Please work this step and when this step is complete, use plan "
        f"to advance to the next{yolo_note}"
    )


def _resolve_review(
    agent,
    conn,
    *,
    task_id: str,
    decision: Literal["approved", "denied", "yolo"],
    reviewer: str,
    response: Optional[str],
    notify_worker: bool,
):
    from hermes_cli import execution_bindings as bindings

    try:
        result = bindings.review_plan_step(
            conn,
            task_id=task_id,
            decision=cast(Literal["approved", "denied", "yolo"], decision),
            reviewer=reviewer,
            response=response,
        )
    except (bindings.ExecutionBindingError, ValueError) as exc:
        # ValueError carries the D7 guard: a denial whose reason is an
        # approval token is refused at the kernel instead of recorded.
        return f"ERROR: {exc}", None
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if result.closed and task["plan_kind"] == "debug":
        source = conn.execute(
            "SELECT assignee FROM tasks WHERE debug_plan_id=? "
            "ORDER BY created_at DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        session_db = getattr(agent, "_session_db", None)
        if source is not None and session_db is not None:
            session_db.update_agent_rating(source["assignee"], 0.5)
    message = _worker_review_message(task, result, reviewer, response)
    if notify_worker:
        failure = _send_user(
            str(task["assignee"]), message, conn=conn, task_id=task_id
        )
        if failure:
            return f"ERROR: Review recorded, but worker notification failed: {failure}", result
    else:
        if result.decision == "approved":
            _sync_subject_from_binding(agent)
            if result.closed:
                session_db = getattr(agent, "_session_db", None)
                session_id = getattr(agent, "session_id", None)
                set_task_id = getattr(session_db, "set_session_task_id", None)
                if callable(set_task_id) and session_id:
                    set_task_id(session_id, result.restored_task_id)
                _request_completion_compression(agent)
    return message, result


def cmd_advance(agent, summary: str, step: Optional[int] = None) -> str:
    """Submit one summary, then pause until dispatcher or user review."""
    from hermes_cli import execution_bindings as bindings
    from hermes_cli.plan_limits import summary_text_error

    oversize = summary_text_error(summary)
    if oversize:
        return oversize

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
            step=step,
        )
    except bindings.ExecutionBindingError as exc:
        return f"ERROR: {exc}"

    pending = bindings.pending_step_review(conn, result.task_id)
    if pending is None:
        return f"ERROR: Plan {result.task_id} did not create a pending review"
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (result.task_id,)).fetchone()
    dispatcher = _dispatcher_for_task(conn, result.task_id)
    # A SELF-CREATED plan (creator IS assignee) has no AI dispatcher — the HUMAN
    # is the reviewer. Yolo must still work there: the answer is given by the
    # human, so the human satisfies the requirement that someone be able to
    # receive the step notice. Gating only on the dispatcher made yolo
    # impossible on exactly the plans the human runs by hand.
    _human_gate = bool(getattr(agent, "clarify_callback", None))
    if bool(task["plan_auto_approve"]):
        if not dispatcher and not _human_gate:
            return (
                f"ERROR: Plan {result.task_id} has YOLO enabled but no reviewer "
                "is available for the required step notification. Review remains pending."
            )
        message, reviewed = _resolve_review(
            agent,
            conn,
            task_id=result.task_id,
            decision="approved",
            reviewer="yolo",
            response=None,
            notify_worker=False,
        )
        if reviewed is None:
            return message
        request = _review_request_text(task, pending, auto_approved=True)
        # No dispatcher means the human is the reviewer (self-created plan) —
        # they already answered yolo, so there is no one to notify.
        failure = (
            _send_user(dispatcher, request, conn=conn, task_id=result.task_id)
            if dispatcher
            else None
        )
        suffix = f" Dispatcher notification failed: {failure}" if failure else ""
        if reviewed.closed:
            return (
                f"Task {result.task_id} Step {reviewed.reviewed_step} auto-approved. "
                f"Plan complete.{suffix}"
            )
        return (
            f"Task {result.task_id} Step {reviewed.reviewed_step} auto-approved. "
            f"Now complete Step {reviewed.next_step_no}{suffix}"
        )

    request = _review_request_text(task, pending)
    if dispatcher:
        failure = _send_user(dispatcher, request, conn=conn, task_id=result.task_id)
        suffix = f" Dispatcher notification failed: {failure}" if failure else ""
        return (
            f"Step {pending.step_no} summary submitted. Pause and wait for "
            f"verification.{suffix}"
        )

    callback = getattr(agent, "clarify_callback", None)
    if callback is None:
        return (
            f"ERROR: Step {pending.step_no} summary submitted for plan "
            f"{result.task_id}, but there is no dispatcher and no interactive "
            "user session available to approve or deny it. This step is now "
            "blocked with no reviewer — reopen this Plan from an interactive "
            "session or assign a dispatcher before advancing again."
        )
    try:
        response = json.loads(
            _legacy().clarify_tool(
                request,
                choices=["Approve", "Deny", "Yolo"],
                callback=callback,
                agent=agent,
                task_id=result.task_id,
            )
        ).get("user_response", "")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return (
            f"Step {pending.step_no} summary submitted. Pause and wait for "
            f"verification. User review is pending: {exc}"
        )
    if not response:
        return (
            f"Step {pending.step_no} summary submitted. Pause and wait for "
            "verification. User review is pending."
        )
    # D6 (t_cc772a4c): an expired or undelivered ask is a NON-answer — it must
    # never fall through to the approve/deny parse below.  Before this guard
    # the gateway's stand-down string was recorded as a DENIAL whose "reason"
    # was the stand-down text itself.
    non_answer = non_answer_response(response)
    if non_answer:
        return (
            f"Step {pending.step_no} summary submitted. Pause and wait for "
            f"verification. Review remains pending: {non_answer}. This is NOT "
            "a review decision — do not treat it as one."
        )
    _answer = str(response).strip().lower()
    # "Yolo" approves this step AND enables auto-approve for the remainder of
    # the plan, so the reviewer is never asked again. Evan's rule: yolo is a
    # property of the human's answer, never something an agent specifies.
    # Order matters — "Yolo" contains no "appr", so test it before the plain
    # approve.
    if _answer.startswith("yolo"):
        decision = "yolo"
    else:
        decision = "approved" if _answer.startswith("appr") else "denied"
    response = None
    if decision == "denied":
        try:
            response = str(
                json.loads(
                    _legacy().clarify_tool(
                        "Why is this step denied?",
                        callback=callback,
                        agent=agent,
                        task_id=result.task_id,
                    )
                ).get("user_response", "")
            ).strip()
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return f"ERROR: Denial response was not recorded: {exc}"
        if not response:
            return "ERROR: denied review requires a response"
        # D6 (t_cc772a4c): same non-answer guard — an expired "Why is this
        # step denied?" prompt must not become the recorded denial reason.
        non_answer = non_answer_response(response)
        if non_answer:
            return (
                f"ERROR: denied review requires a real reason — {non_answer} "
                "is not one. Nothing was recorded; the denial reason is still "
                "required."
            )
        if len(response) > 1024:
            return "ERROR: review response must be at most 1024 characters"
    message, _reviewed = _resolve_review(
        agent,
        conn,
        task_id=result.task_id,
        decision=cast(Literal["approved", "denied", "yolo"], decision),
        reviewer="user",
        response=response,
        notify_worker=False,
    )
    return message


def cmd_review(
    agent, task_id: Optional[str], decision: str, response: Optional[str] = None
) -> str:
    """Approve, deny, or yolo the dispatcher-gated pending step review.

    ``task_id`` is Optional: an omitted task_id gates the ACTIVE plan on the
    caller's binding (the ``remind``/``archive`` contract, Evan 2026-10-24) —
    a reviewer holding the gate is reviewing the step on their own binding,
    and a flash-tier model drops the key when the schema reads ambiguous.

    ``response`` is dual-use (the F2 unification): the denial/request-changes
    message, or the answer to a worker's clarify/tool-limit ask when the plan
    carries a pending flag (see ``cmd_review``'s flag handling).
    """
    from hermes_cli import execution_bindings as bindings

    decision = (decision or "").strip().lower()
    if decision not in {"approved", "denied", "yolo"}:
        return "ERROR: 'review' decision must be 'approved', 'denied', or 'yolo'"
    response = (response or "").strip() or None
    if decision == "denied" and response is None:
        return "ERROR: denied review requires a response"
    if response is not None and len(response) > 1024:
        return "ERROR: review response must be at most 1024 characters"

    conn = _legacy()._get_kanban_db()
    if not task_id:
        _key, binding, error = _current(conn, agent)
        if binding is None:
            if error and error.startswith("PLAN_STATE_UNAVAILABLE:"):
                return error
            return (
                "ERROR: 'review' got no task_id and no plan is active in this "
                "session. Pass task_id=<id> to gate a specific plan, or run "
                "plan 'continue <task_id>' to bind one first."
            )
        task_id = binding.task_id
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if task is None:
        return f"ERROR: Task {task_id} not found"
    dispatcher = _dispatcher_for_task(conn, task_id)
    if not dispatcher:
        return f"ERROR: Task {task_id} has no AI dispatcher; user review is required"
    reviewer = _legacy()._get_agent_name(agent)
    if reviewer.casefold() != dispatcher.casefold():
        return f"ERROR: Task {task_id} must be reviewed by dispatcher {dispatcher}"

    # F2 dual-mode: a pending clarify/tool-limit flag turns this review
    # command into a clarify ANSWER instead of a step review. The flag write
    # was the durable ask; the response is delivered verbatim to the worker
    # and the flag cleared. No pending step review is required or touched.
    flag = bindings.get_clarify_flag(conn, task_id)
    if flag is not None:
        if not response:
            return (
                f"ERROR: Plan {task_id} has a pending {flag.get('kind', 'clarify')} "
                "ask from the worker — a response is required to answer it"
            )
        bindings.clear_clarify_flag(conn, task_id)
        kind = str(flag.get("kind") or "needs_clarify")
        step_no = flag.get("step")
        if kind == "tool_limit_hit":
            worker_message = (
                f"{reviewer} responded to your tool-call-limit ask on "
                f"{task_id} (Step {step_no}):\n\n{response}"
            )
        else:
            worker_message = (
                f"{reviewer} answered your clarifying question on "
                f"{task_id} (Step {step_no}):\n\n{response}"
            )
        failure = _send_user(
            str(task["assignee"]), worker_message, conn=conn, task_id=task_id
        )
        if failure:
            # The response IS durable (it was the caller's argument); the
            # worker wake is best-effort in exactly one direction: if it
            # fails the worker re-reads the flag on its next poll. But the
            # flag was just cleared, so surface the failure loudly rather
            # than pretend delivery happened.
            return (
                f"Clarify response recorded for {task_id}, but worker "
                f"notification failed: {failure}"
            )
        return (
            f"Clarify response delivered to worker for {task_id} "
            f"(Step {step_no}, {kind})."
        )

    pending = bindings.pending_step_review(conn, task_id)
    if pending is None:
        # D3 (t_cc772a4c): a Plan awaiting approval has no step review — the
        # dispatcher's decision HERE is the approval-gate answer channel.
        # Mirrors the interactive gate: approve/yolo resolve the durable
        # authorization and wake the assignee to `plan continue`; a denial
        # resolves it denied and archives the blocked plan.
        from hermes_cli import plan_authorizations
        from hermes_cli.kanban_db import write_txn

        auth = plan_authorizations.get_plan_authorization(conn, task_id)
        if (
            auth is not None
            and auth.state == "pending"
            and task["status"] == "blocked"
            and task["block_kind"] == "approval"
        ):
            approve = decision in ("approved", "yolo")
            # D7 (t_cc772a4c): a denial whose reason is an approval token is a
            # self-contradicting record — refuse before writing it.
            if not approve and bindings.review_response_is_approval_token(response):
                return (
                    f"ERROR: denial reason {str(response)!r} is an approval "
                    "token; nothing was recorded — if the plan should pass, "
                    f"run 'plan review {task_id} approved' (or 'yolo')."
                )
            try:
                with write_txn(conn):
                    if decision == "yolo":
                        conn.execute(
                            "UPDATE tasks SET plan_auto_approve = 1 WHERE id = ?",
                            (task_id,),
                        )
                    plan_authorizations.resolve_plan_in_txn(
                        conn,
                        task_id,
                        "approved" if approve else "denied",
                        _actor(agent),
                        reason=None if approve else response,
                    )
                    if not approve:
                        conn.execute(
                            "UPDATE tasks SET status='archived', completed_at=? "
                            "WHERE id=? AND status='blocked'",
                            (int(time.time()), task_id),
                        )
            except (sqlite3.Error, ValueError) as exc:
                return f"ERROR: Failed to resolve Plan approval: {exc}"
            verdict = "approved" if approve else "denied"
            assignee = str(task["assignee"] or "").strip()
            if approve:
                worker_message = (
                    f"Plan {task_id} approved by {reviewer}. Please call "
                    f"'plan continue {task_id}' to begin"
                    + (
                        " (YOLO enabled: step reviews will auto-approve)."
                        if decision == "yolo"
                        else "."
                    )
                )
            else:
                worker_message = (
                    f"Plan {task_id} was denied by {reviewer}: {response}"
                )
            suffix = ""
            if assignee:
                failure = _send_user(
                    assignee, worker_message, conn=conn, task_id=task_id
                )
                if failure:
                    suffix = f" Assignee notification failed: {failure}"
            return (
                f"Task {task_id} {verdict}: {task['title'] or ''}{suffix}"
            ).rstrip()
        return f"ERROR: Plan {task_id} has no pending review"
    message, result = _resolve_review(
        agent,
        conn,
        task_id=task_id,
        decision=cast(Literal["approved", "denied", "yolo"], decision),
        reviewer=reviewer,
        response=response,
        notify_worker=True,
    )
    if result is None:
        return message
    if decision == "denied":
        return f"Task {task_id} Step {result.reviewed_step} denied. Worker notified."
    if decision == "yolo":
        if result.closed:
            return (
                f"Task {task_id} Step {result.reviewed_step} approved. "
                "Plan complete. Worker notified."
            )
        return (
            f"Task {task_id} Step {result.reviewed_step} approved. "
            "YOLO enabled. Worker notified."
        )
    if result.closed:
        return (
            f"Task {task_id} Step {result.reviewed_step} approved. "
            "Plan complete. Worker notified."
        )
    return f"Task {task_id} Step {result.reviewed_step} approved. Worker notified."


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
    from hermes_cli.plan_limits import summary_text_error

    oversize = summary_text_error(summary)
    if oversize:
        return oversize

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


def _step_expansions(task) -> dict[int, str]:
    """Indexed step expansions (1-based) from a task row; ``{}`` for legacy rows.

    Expansions are spec ground truth stored verbatim in
    ``task_step_expansions`` (JSON list parallel to ``task_steps``; empty
    strings for steps that carry no expansion).
    """
    try:
        raw = task["task_step_expansions"]
    except (KeyError, IndexError):
        return {}
    if not raw:
        return {}
    try:
        items = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return {
        index: str(item or "")
        for index, item in enumerate(items or [], 1)
        if item
    }


def _format_plan(conn, task, *, include_summaries: bool, minimal: bool = False) -> str:
    from hermes_cli import execution_bindings as bindings

    steps = json.loads(task["task_steps"] or "[]")
    expansions = _step_expansions(task)
    stepno = task["task_stepno"] or 1
    summaries = (
        bindings.plan_step_summaries(conn, task["id"])
        if include_summaries
        else {}
    )
    denials = (
        bindings.plan_step_denials(conn, task["id"])
        if include_summaries
        else {}
    )
    if minimal:
        current_step = steps[stepno - 1] if 0 < stepno <= len(steps) else ""
        lines = [
            f"Goal: {task['task_goal'] or ''}",
            _reviewer_identity(conn, task),
            f"Current step {stepno}/{len(steps)}: {current_step}",
        ]
        # The current step's expansion IS the spec (t_d3583771): rendering it
        # here turns "read the spec file" into "the plan already shows it".
        if 0 < stepno <= len(steps) and expansions.get(stepno):
            lines.append("Spec:")
            lines.append(expansions[stepno])
        if stepno in summaries:
            lines.append(f"Summary: {summaries[stepno]}")
        if stepno in denials:
            lines.append(f"Denied: {denials[stepno]}")
        return "\n".join(lines)

    lines = [
        f"Task: {task['title'] or task['id']}",
        f"Status: {task['status']}",
        f"Goal: {task['task_goal'] or ''}",
        _reviewer_identity(conn, task),
        f"Step {stepno}/{len(steps)}",
        "",
    ]
    for index, step in enumerate(steps, 1):
        lines.append(f"  {'→' if index == stepno else ' '} Step {index}: {step}")
        if index == stepno and expansions.get(index):
            lines.append("      Spec:")
            lines.extend(f"      {line}" for line in expansions[index].splitlines())
        if index in summaries:
            lines.append(f"      Summary: {summaries[index]}")
        if index in denials:
            lines.append(f"      Denied: {denials[index]}")
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


def _agent_identity_names(agent) -> set[str]:
    """Return the casefolded names this agent may own tasks under.

    Tasks record ``assignee`` from ``agent_name`` (USERNAME), while the
    execution binding records ``profile`` from ``profile_name``.  They are
    usually equal but not guaranteed, so a claim search must match either.
    """
    names: set[str] = set()
    for attr in ("agent_name", "profile_name"):
        value = getattr(agent, attr, None)
        if isinstance(value, str) and value.strip():
            names.add(value.strip().casefold())
    return names


def _unclaimed_plan_for_agent(conn, agent, *, exclude_task_id: Optional[str] = None):
    """Return ``(task_id, title, stepno)`` for a plan awaiting this agent's claim.

    Covers every waiting state a freshly-dispatched plan sits in before the
    receiver runs ``continue``.  It only reports — ``remind`` uses it to warn
    about an unclaimed task without claiming it.
    """
    names = _agent_identity_names(agent)
    if not names:
        return None
    placeholders = ",".join("?" for _ in names)
    params = list(names)
    where = (
        f"lower(assignee) IN ({placeholders}) "
        f"AND status IN ('blocked','review','ready','triage','todo')"
    )
    if exclude_task_id:
        where += " AND id != ?"
        params.append(exclude_task_id)
    row = conn.execute(
        f"SELECT id, title, task_stepno FROM tasks WHERE {where} "
        f"ORDER BY created_at DESC LIMIT 1",
        tuple(params),
    ).fetchone()
    if row is None:
        return None
    tid = row["id"] if not isinstance(row, tuple) else row[0]
    title = (row["title"] if not isinstance(row, tuple) else row[1]) or tid
    stepno = (row["task_stepno"] if not isinstance(row, tuple) else row[2]) or 1
    return tid, title, stepno


def resolve_plan_id_for_archive(agent) -> Optional[str]:
    """Resolve the plan id that ``archive`` (no task_id) should close: the
    ACTIVE binding only. There is no orphan fallback — an unbound plan is not
    this session's plan, and archive must never guess.
    """
    conn = _legacy()._get_kanban_db()
    _key, binding, error = _current(conn, agent)
    if binding is not None:
        return binding.task_id
    return None


def cmd_archive(agent, task_id: Optional[str] = None) -> str:
    """Archive the resolved plan (the ACTIVE binding only; pass task_id to
    archive a specific plan).  Runs against the
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
                "ERROR: 'archive' requires task_id, and no active plan was "
                "found for this agent"
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


def _identity_task_is_unbound(conn, agent, task_id: str, task) -> Optional[str]:
    """When rendering an explicitly-named task, is it unbound in this session?

    Returns the plan body (via _format_plan) if the current session has no
    execution binding for ``task_id`` — i.e. the agent is looking at a plan
    that is not running here. Returns None when the caller's session holds
    the live binding (or identity can't be resolved — defer to the caller's
    existing error paths).
    """
    from hermes_cli import execution_bindings as bindings

    try:
        key = _identity(agent)
    except bindings.PlanStateUnavailable:
        return None  # identity unavailable: let existing paths handle it
    binding = bindings.get_binding(conn, key)
    if binding is not None and binding.task_id == task_id:
        return None  # genuinely active here — render normally
    return _format_plan(conn, task, include_summaries=False)


def cmd_remind(agent, task_id: Optional[str] = None) -> str:
    """Show the plan goal and the ACTIVE step only.

    Deliberately does not list the other steps (2026-09-14, Evan): an agent that
    sees every step loses track of which one it is on and starts working on the
    wrong one. One step, then act — the closing line tells it to `advance`.
    `cmd_continue` still prints the whole plan with summaries; that one is for
    first-turn context restoration.
    """
    conn = _legacy()._get_kanban_db()
    if task_id is None:
        _key, binding, error = _current(conn, agent)
        if binding is None:
            if error and error.startswith("PLAN_STATE_UNAVAILABLE:"):
                # The identity checks (profile/session/db) gate the BINDING
                # lookup only. With no task_id there is nothing else to show,
                # so surface the unavailable-state message — but never let it
                # mask a resolvable explicit task lookup below.
                return error
            # No active binding. Say the plan is not active and steer the
            # worker to claim semantics (Evan, 2026-10-06): with an assigned
            # unclaimed task, name it; otherwise a plain "no active plan".
            names = list({n.casefold() for n in _agent_identity_names(agent) if n})
            if names:
                ph = ",".join("?" for _ in names)
                waiting = conn.execute(
                    f"SELECT id, title FROM tasks "
                    f"WHERE lower(assignee) IN ({ph}) "
                    f"AND status IN ('blocked','review','ready','triage','todo') "
                    f"ORDER BY created_at DESC LIMIT 1",
                    names,
                ).fetchone()
                if waiting is not None:
                    tid = waiting["id"]
                    title = waiting["title"] or tid
                    return (
                        f"No active plan in this session. You are assigned to "
                        f"task {tid} ({title}) but have not claimed it yet. "
                        f"Run `plan continue {tid}` to claim it."
                    )
            return "ERROR: No active plan in this session. Run `plan continue <task_id>` with a valid task id to claim the task."
        else:
            task_id = binding.task_id
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if task is None:
        return f"Task {task_id} not found"

    steps = json.loads(task["task_steps"] or "[]")
    lines = [
        f"Task: {task['title'] or task['id']}",
        f"Goal: {task['task_goal'] or ''}",
        _reviewer_identity(conn, task),
    ]
    # Terminal plans must never render as active (t_b4a93053, 2026-09-22):
    # close_plan NULLs task_stepno, and the old `stepno or 1` fallback made a
    # DONE plan read as "Active Step 1 of 1". Report the real status instead.
    from hermes_cli import execution_bindings as _eb

    denials = _eb.plan_step_denials(conn, task_id)
    if task["status"] not in {"manual", "running"}:
        lines.append(f"Status: {task['status']} — this plan is closed, not active.")
        if task["status"] == "done" and steps:
            lines.append(f"Final step was: {steps[-1]}")
        lines.append("")
        lines.append(
            "If this plan needs more work, call `plan repeat` with "
            "step=1 plus a corrective plan, or `plan archive` to close it."
        )
        return "\n".join(lines)
    stepno = task["task_stepno"]
    unbound = _identity_task_is_unbound(conn, agent, task_id, task)
    if unbound is not None:
        # Unclaimed-in-this-session plan asked about by explicit task_id
        # (t_a63a4220, 2026-10-09): `task_stepno or 1` made a reset/unclaimed
        # manual plan render as "Active Step 1 of N", so a fresh worker
        # believed the plan was running and burned advances against
        # "No active task". Say where things actually stand instead.
        return (
            f"Task {task_id} is assigned to you but NOT ACTIVE in this session "
            f"(no execution binding — maybe the gateway restarted or the plan "
            f"was reset). Run `plan continue {task_id}` to claim it, "
            f"then implement Step {task['task_stepno'] or 1}.\n\n"
            f"{unbound}"
        )
    claim_warning = _unclaimed_plan_for_agent(conn, agent, exclude_task_id=task_id)
    expansions = _step_expansions(task)
    if not steps:
        lines.append("Active Step: (no steps defined)")
    elif stepno is not None and 0 < stepno <= len(steps):
        lines.append(f"Active Step {stepno} of {len(steps)}: {steps[stepno - 1]}")
        # The current step's expansion IS the spec (t_d3583771): remind shows
        # it so the worker never re-reads an external spec file mid-run.
        if expansions.get(stepno):
            lines.append("Spec:")
            lines.append(expansions[stepno])
    else:
        lines.append(f"Active Step: (out of range: {stepno} of {len(steps)})")
    if stepno in denials:
        lines.append(f"Last denial of this step: {denials[stepno]}")
    if claim_warning is not None:
        wtid, wtitle, wstepno = claim_warning
        lines.append("")
        lines.append(
            f"UNCLAIMED: task {wtid} ({wtitle}) is also assigned to you and "
            f"has not been claimed — its row sits at step {wstepno}. Any step "
            f"position shown above comes from a stored row, which an earlier "
            f"session may have advanced. Confirm which plan you mean by "
            f"claiming it: `plan continue {wtid}`."
        )
    lines.append("")
    lines.append(
        "If this step is complete, call `plan advance` with a summary "
        "to get the next step."
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
        # Already bound to this exact session? Tell the model the plan is
        # active and to use `advance` — do NOT re-bind. continue_plan is
        # now a no-op in this case (revision preserved), so surface the
        # same steer here instead of a confusing re-bind.
        current = bindings.get_binding(conn, key)
        if current is not None and current.task_id == task_id:
            task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return (
                f"Plan {task_id} is ALREADY ACTIVE in this session. "
                f"No need to call `continue` — use `plan advance` with a "
                f"summary to move to the next step.\n\n"
                f"{_format_plan(conn, task, include_summaries=True)}"
            )
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

    creator_agent_name = _legacy()._get_agent_name(agent)
    assigned = (task["assignee"] or "").strip()
    delegated = bool(assigned and assigned != creator_agent_name)

    _approval_steps: list = []
    try:
        _approval_steps = json.loads(task["task_steps"] or "[]")
    except (TypeError, ValueError):
        _approval_steps = []
    response = callback(
        render_approval_prompt(
            task_id, task["title"] or "", task["task_goal"] or "", _approval_steps
        ),
        ["Approve", "Deny", "Yolo"],
    )
    # A failed prompt SEND is not a human answer — never report it as one.
    undelivered = undeliverable_approval_response(task_id, response)
    if undelivered:
        return undelivered
    _answer = str(response or "").strip().lower()
    _stop_asking = _answer.startswith("yolo")
    if not _answer or not (_stop_asking or _answer.startswith("appr")):
        return f"Plan awaiting approval ({task_id}): no approval was recorded."
    # "Yolo" also enables auto-approve for the whole plan, so the reviewer is
    # not asked again at each step. Evan's rule: yolo is a property of the
    # human's answer, never something an agent specifies. "Yolo" contains no
    # "appr", so it must be matched before the plain-approve test above.
    try:
        key = _identity(agent)
        current = bindings.get_binding(conn, key)
        with write_txn(conn):
            if _stop_asking:
                conn.execute(
                    "UPDATE tasks SET plan_auto_approve = 1 WHERE id = ?",
                    (task_id,),
                )
            if delegated:
                # Delegated plan: approve the authorization so the worker's
                # `plan continue` binds cleanly, but do NOT activate into the
                # creator's binding and do NOT flip the task to manual — it
                # stays blocked, which continue_plan accepts (780f7f99cc), so
                # the WORKER's continue does blocked→manual→bind on their
                # session. (Mirrors cmd_new's delegated branch — the separate
                # `approve` command lost this when it was split out, silently
                # dropping the worker wake, t_ddf573ea.)
                from hermes_cli import plan_authorizations

                plan_authorizations.resolve_plan_in_txn(
                    conn, task_id, "approved", _actor(agent)
                )
            else:
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
    if not delegated:
        # Activating a blocked Plan changes the active step — re-point the subject
        # so the transition triggers a FULL compaction. See
        # _sync_subject_from_binding.
        _sync_subject_from_binding(agent)
        return f"Task {task_id} approved: {task['title']}"
    # Delegated: wake the assignee with the continue instruction; the plan
    # stays bound to their session from here, not ours.
    # Evan's exact pseudo text (2026-09-22) — verbatim, nothing added:
    worker_msg = (
        f"You have been assigned a new task.  Please call "
        f"'plan continue {task_id}' to begin the new task."
    )
    wrapped = (
        f"Incoming message from {creator_agent_name} follows:\n"
        "---\n"
        f"{worker_msg}\n"
        "---\n"
        f"{_reviewer_identity(conn, task)}"
    )
    # Fire-and-forget wake: the assignee is notified out-of-band, so the
    # creator's tool loop never blocks on delivery. Delivery status lives
    # in the durable task row, not in this synchronous call.
    from hermes_cli._subprocess_compat import spawn_detached

    from hermes_cli import kanban_db as kb

    spawn_detached(
        ["hermes", "send", "-t", kb.wake_target_for(conn, task_id, assigned), wrapped]
    )
    # No binding activation on our side — surface the dispatch notice.
    # Evan's exact spec (2026-09-22): the creator sees ONLY the notify line.
    return f"{assigned} has been notified of task {task_id}"


# ---------------------------------------------------------------------------
# flat-file plan spec: import / export (t_d3583771)
# ---------------------------------------------------------------------------

_H1_RE = re.compile(r"^#\s+(.*)$")
_H2_RE = re.compile(r"^##\s+(.*)$")
# Run-record entry lines written by ``plan export`` (see ``_quote``).
_RECORD_LINE_RE = re.compile(r"^>\s*\*\*(?:Summary|Denied|Claims and denials):")


def _quote(text: str) -> str:
    """Render text as a markdown blockquote (every line ``> ``-prefixed)."""
    return "\n".join(f"> {line}".rstrip() for line in (text or "").splitlines())


def _strip_run_record(body: list[str]) -> str:
    """Drop export's trailing blockquote run-record from a step body.

    The run record always starts with a ``> **Summary:**`` / ``> **Denied:**``
    / ``> **Claims and denials:**`` line and runs to the end of the body. A
    step body ending in authored blockquotes whose run does NOT start with a
    record marker is kept verbatim.
    """
    end = len(body)
    while end > 0 and not body[end - 1].strip():
        end -= 1
    start = end
    while start > 0 and body[start - 1].lstrip().startswith(">"):
        start -= 1
    if start < end and _RECORD_LINE_RE.match(body[start].lstrip()):
        return "\n".join(body[:start]).strip()
    return "\n".join(body[:end]).strip()


def parse_plan_spec(text: str) -> tuple[str, str, list[str], list[str]]:
    """Parse the flat markdown plan format.

    Returns ``(title, global_description, step_titles, step_expansions)``:

        # <Plan Title>

        <global description — the goal and any run-wide context>

        ## <Step 1 title>
        <step 1 expansion — the spec section governing this step>

    The first H1 before any step section is the title; later ``#`` lines are
    body content (spec text is never silently dropped). A trailing blockquote
    run marked as the run record (written by ``plan export``) is stripped
    from each step body, so an exported plan re-imports as the original
    spec.
    """
    title = ""
    global_lines: list[str] = []
    titles: list[str] = []
    expansions: list[str] = []
    section: Optional[list[str]] = None

    def _finish_section() -> None:
        nonlocal section
        if section is not None:
            expansions.append(_strip_run_record(section))
            section = None

    for line in (text or "").splitlines():
        h1 = _H1_RE.match(line)
        if h1 and not title and section is None and not titles:
            title = h1.group(1).strip()
            continue
        h2 = _H2_RE.match(line)
        if h2:
            _finish_section()
            titles.append(h2.group(1).strip())
            section = []
            continue
        if section is not None:
            section.append(line)
        elif title:
            global_lines.append(line)
    _finish_section()
    return title, "\n".join(global_lines).strip(), titles, expansions


def _step_run_history(conn, task_id: str) -> dict[int, list[str]]:
    """Full claim/denial history per step from the append-only event log."""
    history: dict[int, list[str]] = {}
    rows = conn.execute(
        "SELECT kind, payload, created_at FROM task_events WHERE task_id = ? "
        "AND kind IN ('plan-step-review-pending', 'plan-step-review-denied', "
        "'plan-step-review-approved') ORDER BY id",
        (task_id,),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            continue
        step = payload.get("step")
        if not isinstance(step, int):
            continue
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row["created_at"]))
        kind = row["kind"]
        if kind == "plan-step-review-pending":
            entry = (
                f"claimed ({stamp}, {payload.get('worker', '')}): "
                f"{payload.get('summary', '')}"
            )
        elif kind == "plan-step-review-denied":
            entry = (
                f"denied ({stamp}, {payload.get('reviewer', '')}): "
                f"{payload.get('response', '')}"
            )
        else:
            yolo = ", yolo" if payload.get("auto_approve_enabled") else ""
            entry = f"approved ({stamp}, {payload.get('reviewer', '')}{yolo})"
        history.setdefault(step, []).append(entry)
    return history


def render_plan_spec(conn, task) -> str:
    """The plan as flat markdown: spec + per-step run record.

    The run record carries every step's completion summary and denial
    message plus the full history of claims and denials — a durable, diffable
    record of what the worker claimed and what the reviewer rejected.
    """
    from hermes_cli import execution_bindings as bindings

    task_id = task["id"]
    steps = json.loads(task["task_steps"] or "[]")
    expansions = _step_expansions(task)
    summaries = bindings.plan_step_summaries(conn, task_id)
    denials = bindings.plan_step_denials(conn, task_id)
    history = _step_run_history(conn, task_id)

    parts = [f"# {task['title'] or task_id}", "", (task["task_goal"] or "").strip()]
    for index, step in enumerate(steps, 1):
        parts.extend(["", f"## {step}", expansions.get(index, "")])
        record: list[str] = []
        if index in summaries:
            record.append(_quote(f"**Summary:** {summaries[index]}"))
        if index in denials:
            record.append(_quote(f"**Denied:** {denials[index]}"))
        entries = history.get(index) or []
        if entries:
            record.append(_quote("**Claims and denials:**"))
            for entry in entries:
                record.append(_quote(f"- {entry}"))
        if record:
            parts.append("")
            parts.extend(record)
    return "\n".join(parts).rstrip() + "\n"


def cmd_import(
    agent, file: str, assignee: Optional[str] = None, board: Optional[str] = None
) -> str:
    """Create a Plan from a flat markdown spec file (``new`` + ``assign``).

    The file is the prep/authoring surface: written and worked out offline,
    then imported. It becomes the spec for the run INSIDE the plan — each
    step's expansion is immutable ground truth in the protected plan block,
    so no read_file, no re-read, no collapse-to-receipt.
    """
    from hermes_cli.plan_limits import (
        expansion_text_error, goal_text_error, step_count_error, steps_text_error,
    )

    if not file or not file.strip():
        return "ERROR: 'import' requires file"
    path = os.path.abspath(os.path.expanduser(file.strip()))
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        return f"ERROR: cannot read plan spec {path}: {exc}"
    except UnicodeDecodeError as exc:
        return f"ERROR: plan spec {path} is not valid UTF-8: {exc}"

    title, goal, titles, expansions = parse_plan_spec(text)
    if not title:
        return "ERROR: plan spec has no '# <Plan Title>' heading. Nothing was created."
    if not titles:
        return (
            "ERROR: plan spec has no '## <Step title>' sections. Nothing was created."
        )
    for index, step_title in enumerate(titles, 1):
        if not step_title:
            return (
                f"ERROR: step {index} has an empty '## ' heading. "
                "Nothing was created — give every step a title."
            )
    # Settled limits (Gopher tested, Evan approved 2026-10-23): 128-byte step
    # titles, 2K expansion per step, 2K global. Hard refusals — spec text is
    # never truncated.
    for over_limit in (
        step_count_error(titles),
        goal_text_error(goal),
        steps_text_error(titles),
        expansion_text_error(expansions),
    ):
        if over_limit:
            return over_limit
    return _create_plan(
        agent,
        title=title,
        goal=goal,
        steps=titles,
        expansions=expansions,
        assignee=assignee,
        board=board,
    )


def cmd_export(
    agent, task_id: Optional[str] = None, file: Optional[str] = None
) -> str:
    """Write a Plan back to flat markdown (spec + run record).

    Includes every step's completion summary and denial message; at plan
    completion the run record is the full history of claims and denials —
    what the worker claimed and what the reviewer rejected.
    """
    if not task_id:
        return "ERROR: 'export' requires task_id"
    conn = _legacy()._get_kanban_db()
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if task is None:
        # Plan tasks live on any board — search them all (same rule as block).
        for db_path in _legacy()._enumerate_kanban_dbs():
            other = sqlite3.connect(db_path)
            other.row_factory = sqlite3.Row
            try:
                task = other.execute(
                    "SELECT * FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
            except sqlite3.Error:
                task = None
            if task is not None:
                conn = other
                break
            other.close()
    if task is None:
        return f"ERROR: Task {task_id} not found"
    text = render_plan_spec(conn, task)
    if file and file.strip():
        path = os.path.abspath(os.path.expanduser(file.strip()))
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
        except OSError as exc:
            return f"ERROR: cannot write plan export {path}: {exc}"
        return f"Plan {task_id} exported to {path}"
    return text
