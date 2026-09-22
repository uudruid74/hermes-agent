"""Authoritative execution binding and manual Plan lifecycle kernel.

The consolidated Kanban database owns active-task identity.  Session rows own
transcript lineage only; callers must resolve a stable lineage root before
constructing :class:`ExecutionKey`.  Environment variables are bootstrap
inputs, never read fallbacks.

Only this module mutates ``execution_bindings`` or combines a binding change
with a manual Plan lifecycle transition.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
import time
from typing import Literal, Optional

from hermes_cli.kanban_db import kanban_db_path, write_txn


class ExecutionBindingError(RuntimeError):
    """Base class for deterministic execution-binding failures."""


class BindingNotFound(ExecutionBindingError):
    """No durable binding exists for the requested execution key."""


class ActiveTaskConflict(ExecutionBindingError):
    """A different task is already bound to this execution lineage."""


class BindingRevisionConflict(ExecutionBindingError):
    """The caller acted on a stale execution-binding revision."""


class InvalidTaskState(ExecutionBindingError):
    """The target task is missing or cannot participate in this transition."""


class PlanAuthorizationRequired(ExecutionBindingError):
    """A manual Plan has no durable approved authorization."""


class PlanStateUnavailable(ExecutionBindingError):
    """The authoritative binding or strict session lineage could not be read."""


@dataclass(frozen=True)
class ExecutionKey:
    profile: str
    root_session_id: str


def identity_for_agent(agent) -> ExecutionKey:
    """Resolve an agent to its normalized, compression-stable execution key."""
    profile = getattr(agent, "profile_name", None)
    session_id = getattr(agent, "session_id", None)
    session_db = getattr(agent, "_session_db", None)
    if not isinstance(profile, str) or not profile.strip():
        raise PlanStateUnavailable("agent profile is unavailable")
    if not isinstance(session_id, str) or not session_id.strip():
        raise PlanStateUnavailable("agent session is unavailable")
    if session_db is None:
        raise PlanStateUnavailable("agent session database is unavailable")
    try:
        root_session_id = session_db.get_compression_root(session_id)
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        raise PlanStateUnavailable("strict compression lineage is unavailable") from exc
    return ExecutionKey(profile=profile.strip().casefold(), root_session_id=root_session_id)


@dataclass(frozen=True)
class ExecutionBinding:
    profile: str
    root_session_id: str
    task_id: str
    revision: int
    bound_at: int
    updated_at: int


@dataclass(frozen=True)
class PlanStepResult:
    task_id: str
    step_no: Optional[int]
    next_step: Optional[str]
    closed: bool
    restored_task_id: Optional[str]
    binding_revision: Optional[int]


PlanOutcome = Literal["done", "failed", "test-complete"]
_EXECUTABLE_BOOTSTRAP_STATUSES = frozenset({"manual", "running"})


def _validate_key(key: ExecutionKey) -> None:
    if not isinstance(key, ExecutionKey):
        raise TypeError("key must be an ExecutionKey")
    if not key.profile or not key.profile.strip():
        raise ValueError("execution profile must not be empty")
    if not key.root_session_id or not key.root_session_id.strip():
        raise ValueError("root_session_id must not be empty")


def _binding_from_row(row: sqlite3.Row) -> ExecutionBinding:
    return ExecutionBinding(
        profile=str(row["profile"]),
        root_session_id=str(row["root_session_id"]),
        task_id=str(row["task_id"]),
        revision=int(row["revision"]),
        bound_at=int(row["bound_at"]),
        updated_at=int(row["updated_at"]),
    )


def _get_binding_row(
    conn: sqlite3.Connection, key: ExecutionKey
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT profile, root_session_id, task_id, revision, bound_at, updated_at "
        "FROM execution_bindings WHERE profile = ? AND root_session_id = ?",
        (key.profile, key.root_session_id),
    ).fetchone()


def get_binding(
    conn: sqlite3.Connection, key: ExecutionKey
) -> Optional[ExecutionBinding]:
    """Read the sole durable binding for ``key`` without inferring a fallback."""
    _validate_key(key)
    row = _get_binding_row(conn, key)
    return _binding_from_row(row) if row is not None else None


def resolve_active_for_agent(
    agent, board: Optional[str] = None
) -> Optional[ExecutionBinding]:
    """Read the sole runtime binding without environment/session fallback."""
    key = identity_for_agent(agent)
    try:
        conn = sqlite3.connect(str(kanban_db_path(board)))
        conn.row_factory = sqlite3.Row
        try:
            return get_binding(conn, key)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise PlanStateUnavailable("execution binding database is unavailable") from exc


def require_binding(conn: sqlite3.Connection, key: ExecutionKey) -> ExecutionBinding:
    """Return the durable binding or raise a typed fail-closed error."""
    binding = get_binding(conn, key)
    if binding is None:
        raise BindingNotFound(
            f"no active task for {key.profile}:{key.root_session_id}"
        )
    return binding


def _task_row(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise InvalidTaskState(f"task {task_id} does not exist")
    return row


def _append_event(
    conn: sqlite3.Connection,
    task_id: str,
    kind: str,
    *,
    payload: Optional[dict] = None,
    now: Optional[int] = None,
) -> None:
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) "
        "VALUES (?, ?, ?, ?)",
        (
            task_id,
            kind,
            json.dumps(payload, sort_keys=True, separators=(",", ":"))
            if payload is not None
            else None,
            int(time.time()) if now is None else now,
        ),
    )


def _binding_payload(key: ExecutionKey, revision: int) -> dict:
    return {
        "profile": key.profile,
        "root_session_id": key.root_session_id,
        "binding_revision": revision,
    }


def _set_step_summary(
    conn: sqlite3.Connection,
    task_id: str,
    step_no: int,
    summary: str,
    *,
    actor: str,
    now: int,
) -> None:
    """Replace the stored summary for one Plan step.

    The summary is whitespace-compressed and cut to its LAST
    ``PLAN_TEXT_MAX_CHARS`` characters (Evan, 2026-09-16): the conclusion is
    at the end of a summary, and the Plan is the Protected region, so an
    uncapped summary grows the context window permanently.  This one function
    is the choke point for every summary write — ``advance`` (claim and
    proof paths), ``handoff``, and plan closure all route through it — so the
    cap cannot be bypassed by taking a different command.
    """
    from hermes_cli.plan_limits import cap_summary

    summary = cap_summary(summary)
    marker = f"[plan-step-summary:{step_no}] "
    row = conn.execute(
        "SELECT id FROM task_comments WHERE task_id = ? AND body LIKE ?",
        (task_id, f"{marker}%"),
    ).fetchone()
    body = marker + summary
    if row is None:
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES (?, ?, ?, ?)",
            (task_id, actor, body, now),
        )
    else:
        conn.execute(
            "UPDATE task_comments SET author = ?, body = ?, created_at = ? "
            "WHERE id = ?",
            (actor, body, now, row["id"]),
        )


def plan_step_summaries(conn: sqlite3.Connection, task_id: str) -> dict[int, str]:
    """Return the stored summary for each Plan step."""
    summaries = {}
    rows = conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ? "
        "AND body LIKE '[plan-step-summary:%' ORDER BY id",
        (task_id,),
    ).fetchall()
    prefix = "[plan-step-summary:"
    for row in rows:
        marker, separator, summary = row["body"].partition("] ")
        step = marker.removeprefix(prefix)
        if separator and step.isdigit():
            summaries[int(step)] = summary
    return summaries


def bootstrap_worker_binding(
    conn: sqlite3.Connection,
    key: ExecutionKey,
    task_id: str,
) -> ExecutionBinding:
    """Persist one verified dispatcher assignment as the lineage binding.

    Repeating the exact bootstrap is idempotent.  A different existing task is
    a hard conflict: worker environment never overwrites durable state.
    """
    _validate_key(key)
    if not task_id:
        raise ValueError("task_id must not be empty")
    now = int(time.time())
    with write_txn(conn):
        task = _task_row(conn, task_id)
        if task["status"] not in _EXECUTABLE_BOOTSTRAP_STATUSES:
            raise InvalidTaskState(
                f"task {task_id} is not executable (status: {task['status']})"
            )
        existing_row = _get_binding_row(conn, key)
        if existing_row is not None:
            existing = _binding_from_row(existing_row)
            if existing.task_id == task_id:
                return existing
            raise ActiveTaskConflict(
                f"{key.profile}:{key.root_session_id} is already bound to "
                f"{existing.task_id}, not {task_id}"
            )
        conn.execute(
            "INSERT INTO execution_bindings "
            "(profile, root_session_id, task_id, revision, bound_at, updated_at) "
            "VALUES (?, ?, ?, 1, ?, ?)",
            (key.profile, key.root_session_id, task_id, now, now),
        )
        _append_event(
            conn,
            task_id,
            "execution-bound",
            payload={**_binding_payload(key, 1), "source": "worker-bootstrap"},
            now=now,
        )
        stored_row = _get_binding_row(conn, key)
        if stored_row is None:
            raise ExecutionBindingError("worker binding disappeared after insert")
        return _binding_from_row(stored_row)


def _authorization_is_valid(conn: sqlite3.Connection, task: sqlite3.Row) -> bool:
    if bool(task["pre_approved"]) and task["plan_kind"] == "debug":
        return True
    row = conn.execute(
        "SELECT state, execution_task_id FROM plan_authorizations WHERE plan_id = ?",
        (task["id"],),
    ).fetchone()
    return bool(
        row is not None
        and row["state"] == "approved"
        and row["execution_task_id"] == task["id"]
    )


def activate_plan(
    conn: sqlite3.Connection,
    key: ExecutionKey,
    plan_id: str,
    *,
    expected_parent_task_id: Optional[str],
    expected_revision: Optional[int],
) -> ExecutionBinding:
    """Activate one approved Plan and bind it atomically.

    Ambient state never implies nesting: when a binding exists, the caller must
    provide its exact task ID and revision as the expected parent.
    """
    _validate_key(key)
    if not plan_id:
        raise ValueError("plan_id must not be empty")
    now = int(time.time())
    with write_txn(conn):
        plan = _task_row(conn, plan_id)
        if not _authorization_is_valid(conn, plan):
            raise PlanAuthorizationRequired(
                f"plan {plan_id} has no approved durable authorization"
            )
        if plan["status"] not in {"blocked", "manual"}:
            raise InvalidTaskState(
                f"plan {plan_id} cannot activate from status {plan['status']}"
            )
        if plan["status"] == "blocked" and plan["block_kind"] not in {
            None,
            "approval",
        }:
            raise InvalidTaskState(
                f"plan {plan_id} is blocked for {plan['block_kind']}, not approval"
            )

        current_row = _get_binding_row(conn, key)
        current = _binding_from_row(current_row) if current_row is not None else None
        if current is not None and current.task_id == plan_id:
            if expected_parent_task_id not in {None, plan["previous_task"]}:
                raise ActiveTaskConflict(
                    f"plan {plan_id} is already active with a different parent"
                )
            if expected_revision is not None and expected_revision != current.revision:
                raise BindingRevisionConflict(
                    f"expected revision {expected_revision}, found {current.revision}"
                )
            return current

        if current is None:
            if expected_parent_task_id is not None:
                raise ActiveTaskConflict(
                    f"expected parent {expected_parent_task_id}, but no task is bound"
                )
            if expected_revision is not None:
                raise BindingRevisionConflict(
                    f"expected revision {expected_revision}, but no binding exists"
                )
            new_revision = 1
            conn.execute(
                "INSERT INTO execution_bindings "
                "(profile, root_session_id, task_id, revision, bound_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    key.profile,
                    key.root_session_id,
                    plan_id,
                    new_revision,
                    now,
                    now,
                ),
            )
        else:
            if expected_parent_task_id != current.task_id:
                raise ActiveTaskConflict(
                    f"active task is {current.task_id}; explicit parent was "
                    f"{expected_parent_task_id or 'not supplied'}"
                )
            if expected_revision != current.revision:
                raise BindingRevisionConflict(
                    f"expected revision {expected_revision}, found {current.revision}"
                )
            parent = _task_row(conn, current.task_id)
            if parent["status"] not in {"manual", "running", "blocked"}:
                raise InvalidTaskState(
                    f"parent {current.task_id} is not executable "
                    f"(status: {parent['status']})"
                )
            new_revision = current.revision + 1
            updated = conn.execute(
                "UPDATE execution_bindings SET task_id = ?, revision = ?, updated_at = ? "
                "WHERE profile = ? AND root_session_id = ? "
                "AND task_id = ? AND revision = ?",
                (
                    plan_id,
                    new_revision,
                    now,
                    key.profile,
                    key.root_session_id,
                    current.task_id,
                    current.revision,
                ),
            ).rowcount
            if updated != 1:
                raise BindingRevisionConflict("execution binding changed during activation")
            _append_event(
                conn,
                current.task_id,
                "execution-suspended",
                payload={
                    **_binding_payload(key, new_revision),
                    "child_task_id": plan_id,
                },
                now=now,
            )

        changed = conn.execute(
            "UPDATE tasks SET status = 'manual', block_kind = NULL, previous_task = ? "
            "WHERE id = ? AND status IN ('blocked', 'manual')",
            (expected_parent_task_id, plan_id),
        ).rowcount
        if changed != 1:
            raise InvalidTaskState(f"plan {plan_id} changed during activation")
        _append_event(
            conn,
            plan_id,
            "plan-activated",
            payload={
                **_binding_payload(key, new_revision),
                "previous_task": expected_parent_task_id,
            },
            now=now,
        )
        stored_row = _get_binding_row(conn, key)
        if stored_row is None:
            raise ExecutionBindingError("plan binding disappeared after activation")
        return _binding_from_row(stored_row)


def _assert_expected_binding(
    conn: sqlite3.Connection,
    key: ExecutionKey,
    expected_task_id: str,
    expected_revision: int,
) -> ExecutionBinding:
    current = require_binding(conn, key)
    if current.task_id != expected_task_id:
        raise ActiveTaskConflict(
            f"active task is {current.task_id}, not {expected_task_id}"
        )
    if current.revision != expected_revision:
        raise BindingRevisionConflict(
            f"expected revision {expected_revision}, found {current.revision}"
        )
    return current


def _steps_for_task(task: sqlite3.Row) -> tuple[list[str], int]:
    try:
        steps = json.loads(task["task_steps"] or "[]")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise InvalidTaskState(f"task {task['id']} has invalid task_steps") from exc
    if not isinstance(steps, list) or not steps or not all(
        isinstance(step, str) and step for step in steps
    ):
        raise InvalidTaskState(f"task {task['id']} has no valid steps")
    try:
        step_no = int(task["task_stepno"])
    except (TypeError, ValueError) as exc:
        raise InvalidTaskState(f"task {task['id']} has no active step") from exc
    if not 1 <= step_no <= len(steps):
        raise InvalidTaskState(
            f"task {task['id']} step {step_no} is outside 1..{len(steps)}"
        )
    return steps, step_no


def _close_plan_in_txn(
    conn: sqlite3.Connection,
    key: ExecutionKey,
    current: ExecutionBinding,
    *,
    outcome: PlanOutcome,
    reason: Optional[str],
    actor: str,
    status_note: Optional[str] = None,
) -> PlanStepResult:
    task = _task_row(conn, current.task_id)
    if task["status"] != "manual":
        raise InvalidTaskState(
            f"plan {current.task_id} is not active (status: {task['status']})"
        )
    steps, step_no = _steps_for_task(task)
    step_title = steps[step_no - 1]
    now = int(time.time())

    if outcome == "done":
        terminal_status = "done"
        event_kind = "plan-completed"
        note = f"Step {step_no} complete"
        if status_note:
            note += f" — {status_note}"
    elif outcome == "failed":
        terminal_status = "archived"
        event_kind = "plan-failed"
        note = f"FAILED at Step {step_no}: {step_title}. {reason or 'unspecified'}"
    else:
        terminal_status = "archived"
        event_kind = "plan-test-complete"
        note = f"TEST COMPLETE at Step {step_no}: {step_title}."

    if outcome == "done" and status_note:
        _set_step_summary(
            conn,
            current.task_id,
            step_no,
            status_note,
            actor=actor,
            now=now,
        )
    conn.execute(
        "INSERT INTO task_comments (task_id, author, body, created_at) "
        "VALUES (?, ?, ?, ?)",
        (current.task_id, actor, note, now),
    )
    changed = conn.execute(
        "UPDATE tasks SET status = ?, completed_at = ?, task_stepno = NULL "
        "WHERE id = ? AND status = 'manual' AND task_stepno = ?",
        (terminal_status, now, current.task_id, step_no),
    ).rowcount
    if changed != 1:
        raise InvalidTaskState(f"plan {current.task_id} changed during closure")
    _append_event(
        conn,
        current.task_id,
        event_kind,
        payload={
            **_binding_payload(key, current.revision),
            "outcome": outcome,
            "step": step_no,
            "step_title": step_title,
            **({"reason": reason} if reason else {}),
        },
        now=now,
    )

    previous_task_id = task["previous_task"]
    if previous_task_id:
        parent = _task_row(conn, previous_task_id)
        if parent["status"] == "blocked":
            if parent["block_kind"] not in {None, "approval"}:
                raise InvalidTaskState(
                    f"parent {previous_task_id} is blocked for "
                    f"{parent['block_kind']}, not child execution"
                )
            conn.execute(
                "UPDATE tasks SET status = 'running', block_kind = NULL "
                "WHERE id = ? AND status = 'blocked'",
                (previous_task_id,),
            )
        elif parent["status"] not in {"manual", "running"}:
            raise InvalidTaskState(
                f"parent {previous_task_id} is not executable "
                f"(status: {parent['status']})"
            )
        new_revision = current.revision + 1
        rebound = conn.execute(
            "UPDATE execution_bindings SET task_id = ?, revision = ?, updated_at = ? "
            "WHERE profile = ? AND root_session_id = ? "
            "AND task_id = ? AND revision = ?",
            (
                previous_task_id,
                new_revision,
                now,
                key.profile,
                key.root_session_id,
                current.task_id,
                current.revision,
            ),
        ).rowcount
        if rebound != 1:
            raise BindingRevisionConflict("execution binding changed during closure")

        # `repeat` hand-off (Evan, 2026-09-15): a corrective child carries
        # REPEAT-OF:<parent>:<step>.  Its completion summary becomes the parent
        # step's summary — the reason the step was re-opened is now on record,
        # and the parent resumes from the step it was on.
        repeat = _repeat_target(conn, current.task_id)
        if repeat is not None and outcome == "done":
            repeat_parent, repeat_step = repeat
            if repeat_parent == previous_task_id:
                _set_step_summary(
                    conn,
                    repeat_parent,
                    repeat_step,
                    f"Re-opened and corrected via {current.task_id}: "
                    f"{status_note or step_title}",
                    actor=actor,
                    now=now,
                )

        _append_event(
            conn,
            previous_task_id,
            "execution-restored",
            payload={
                **_binding_payload(key, new_revision),
                "closed_task_id": current.task_id,
                "outcome": outcome,
            },
            now=now,
        )
        return PlanStepResult(
            task_id=current.task_id,
            step_no=step_no,
            next_step=None,
            closed=True,
            restored_task_id=previous_task_id,
            binding_revision=new_revision,
        )

    deleted = conn.execute(
        "DELETE FROM execution_bindings "
        "WHERE profile = ? AND root_session_id = ? "
        "AND task_id = ? AND revision = ?",
        (
            key.profile,
            key.root_session_id,
            current.task_id,
            current.revision,
        ),
    ).rowcount
    if deleted != 1:
        raise BindingRevisionConflict("execution binding changed during closure")
    # Delegated-plan completion (Evan, 2026-09-22): the origin routing on a
    # plan task points at the DISPATCHER's session, so a plan close must
    # fire the same kanban status notification a worker 'done' transition
    # does — otherwise the dispatcher never learns the delegated work
    # finished.  Best-effort: never fail the close over delivery.
    try:
        from hermes_cli.kanban import _notify_kanban_status_change

        closed_row = _task_row(conn, current.task_id)
        _notify_kanban_status_change(
            current.task_id,
            terminal_status,
            summary=status_note or note,
            title=closed_row["title"] if closed_row else None,
        )
    except Exception:  # noqa: BLE001 — notification must never block closure
        pass
    return PlanStepResult(
        task_id=current.task_id,
        step_no=step_no,
        next_step=None,
        closed=True,
        restored_task_id=None,
        binding_revision=None,
    )


def _clear_step_summary(
    conn: sqlite3.Connection, task_id: str, step_no: int
) -> None:
    """Drop the stored summary for one Plan step.

    Used by `repeat`: the claim the parent held for that step was wrong, so the
    step stands open until the corrective child's completion summary replaces it.
    """
    marker = f"[plan-step-summary:{step_no}] "
    conn.execute(
        "DELETE FROM task_comments WHERE task_id = ? AND body LIKE ?",
        (task_id, f"{marker}%"),
    )


def _repeat_target(conn: sqlite3.Connection, task_id: str) -> Optional[tuple[str, int]]:
    """Return ``(parent_task_id, step_no)`` when this Plan is a `repeat` child.

    Written at creation as ``REPEAT-OF:<parent>:<step>``; the child's
    completion summary is handed back to that parent step.
    """
    row = conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ? AND body LIKE 'REPEAT-OF:%' "
        "ORDER BY id LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    body = row["body"] if isinstance(row, sqlite3.Row) else row[0]
    parts = str(body).split(":")
    if len(parts) < 3:
        return None
    try:
        return parts[1], int(parts[2])
    except ValueError:
        return None


def _step_receipts(conn: sqlite3.Connection, task_id: str) -> set[int]:
    """Return the step numbers that carry a completion receipt.

    A receipt is an `advance` that supplied ``proof`` (a commit, a test
    result, a file path — anything checkable).  A summary alone is a CLAIM;
    this set is what separates a claim from evidence.
    """
    rows = conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ? AND body LIKE 'RECEIPT:%'",
        (task_id,),
    ).fetchall()
    steps: set[int] = set()
    for row in rows:
        body = row["body"] if isinstance(row, sqlite3.Row) else row[0]
        try:
            steps.add(int(str(body).split(":", 2)[1]))
        except (IndexError, ValueError):
            continue
    return steps


def advance_plan(
    conn: sqlite3.Connection,
    key: ExecutionKey,
    *,
    expected_task_id: str,
    expected_revision: int,
    summary: str,
    actor: str,
    proof: Optional[str] = None,
    step: Optional[int] = None,
) -> PlanStepResult:
    """Complete one step, atomically advancing or closing the active Plan.

    Two-phase completion (Evan, 2026-09-15): a plan step may not advance on a
    bare claim.  ``advance`` without ``proof`` for a step that has no receipt
    RECORDS the claim and returns the SAME step with an instruction to verify
    against ground truth and resubmit.  Supplying ``proof`` (or re-claiming an
    already-recorded claim) completes the step.  This catches the failure that
    actually happened — Ornith advanced four steps in four minutes, then said
    "I did not actually fix Step 1."

    ``step`` is the caller stating which step it believes it is completing.
    A mismatch is refused instead of advancing: an agent that has lost its
    place must re-read ``remind``, not silently move the plan forward.
    """
    _validate_key(key)
    with write_txn(conn):
        current = _assert_expected_binding(
            conn, key, expected_task_id, expected_revision
        )
        task = _task_row(conn, current.task_id)
        if task["status"] != "manual":
            raise InvalidTaskState(
                f"plan {current.task_id} is not active (status: {task['status']})"
            )
        steps, step_no = _steps_for_task(task)
        if step is not None and step != step_no:
            raise InvalidTaskState(
                f"step {step} is not the active step of plan {current.task_id} "
                f"(active: {step_no}) — call `remind` before advancing"
            )
        now = int(time.time())

        # Two-phase completion (Evan, 2026-09-15).  A bare claim never moves the
        # step: it is recorded as a summary and the SAME step comes back with an
        # instruction to verify and resubmit WITH `proof`.  Only `proof` — or a
        # receipt already on file — advances.  Repeating the claim no longer
        # sneaks past the gate; the explicit escape hatch is `repeat`, which
        # opens a corrective child plan.
        if not proof:
            if step_no not in _step_receipts(conn, current.task_id):
                _set_step_summary(
                    conn, current.task_id, step_no, summary, actor=actor, now=now
                )
                return PlanStepResult(
                    task_id=current.task_id,
                    step_no=step_no,
                    next_step=steps[step_no - 1],
                    closed=False,
                    restored_task_id=None,
                    binding_revision=current.revision,
                )

        if step_no == len(steps):
            closed = _close_plan_in_txn(
                conn,
                key,
                current,
                outcome="done",
                reason=None,
                actor=actor,
                status_note=summary,
            )
            if proof:
                conn.execute(
                    "INSERT INTO task_comments (task_id, author, body, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (current.task_id, actor, f"RECEIPT:{step_no}:{proof}", now),
                )
            return closed

        if proof:
            conn.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at) "
                "VALUES (?, ?, ?, ?)",
                (current.task_id, actor, f"RECEIPT:{step_no}:{proof}", now),
            )
        _set_step_summary(
            conn,
            current.task_id,
            step_no,
            summary,
            actor=actor,
            now=now,
        )
        changed = conn.execute(
            "UPDATE tasks SET task_stepno = ? "
            "WHERE id = ? AND status = 'manual' AND task_stepno = ?",
            (step_no + 1, current.task_id, step_no),
        ).rowcount
        if changed != 1:
            raise InvalidTaskState(f"plan {current.task_id} changed during advance")
        new_revision = current.revision + 1
        rebound = conn.execute(
            "UPDATE execution_bindings SET revision = ?, updated_at = ? "
            "WHERE profile = ? AND root_session_id = ? "
            "AND task_id = ? AND revision = ?",
            (
                new_revision,
                now,
                key.profile,
                key.root_session_id,
                current.task_id,
                current.revision,
            ),
        ).rowcount
        if rebound != 1:
            raise BindingRevisionConflict("execution binding changed during advance")
        _append_event(
            conn,
            current.task_id,
            "plan-step-completed",
            payload={
                **_binding_payload(key, new_revision),
                "completed_step": step_no,
                "next_step": step_no + 1,
            },
            now=now,
        )
        return PlanStepResult(
            task_id=current.task_id,
            step_no=step_no + 1,
            next_step=steps[step_no],
            closed=False,
            restored_task_id=None,
            binding_revision=new_revision,
        )


def handoff_plan(
    conn: sqlite3.Connection,
    key: ExecutionKey,
    *,
    expected_task_id: str,
    expected_revision: int,
    summary: str,
    actor: str,
) -> PlanStepResult:
    """Replace the scratch summary for the active step without advancing it."""
    _validate_key(key)
    with write_txn(conn):
        current = _assert_expected_binding(
            conn, key, expected_task_id, expected_revision
        )
        task = _task_row(conn, current.task_id)
        if task["status"] != "manual":
            raise InvalidTaskState(
                f"plan {current.task_id} is not active (status: {task['status']})"
            )
        steps, step_no = _steps_for_task(task)
        now = int(time.time())
        _set_step_summary(
            conn,
            current.task_id,
            step_no,
            summary,
            actor=actor,
            now=now,
        )
        return PlanStepResult(
            task_id=current.task_id,
            step_no=step_no,
            next_step=steps[step_no - 1],
            closed=False,
            restored_task_id=None,
            binding_revision=current.revision,
        )


def continue_plan(
    conn: sqlite3.Connection,
    key: ExecutionKey,
    plan_id: str,
    *,
    actor: str,
    session_id: str,
) -> ExecutionBinding:
    """Transfer an active Plan to the caller's agent and current session."""
    _validate_key(key)
    if not plan_id:
        raise ValueError("plan_id must not be empty")
    if not session_id:
        raise PlanStateUnavailable("agent session is unavailable")
    now = int(time.time())
    with write_txn(conn):
        task = _task_row(conn, plan_id)
        if task["status"] not in ("manual", "archived", "blocked"):
            raise InvalidTaskState(
                f"plan {plan_id} cannot continue from status {task['status']}"
            )
        from_status: str = task["status"]
        _steps_for_task(task)
        prior = conn.execute(
            "SELECT revision FROM execution_bindings "
            "WHERE task_id = ? OR (profile = ? AND root_session_id = ?)",
            (plan_id, key.profile, key.root_session_id),
        ).fetchall()
        revision = max((int(row["revision"]) for row in prior), default=0) + 1
        conn.execute(
            "DELETE FROM execution_bindings WHERE task_id = ? "
            "OR (profile = ? AND root_session_id = ?)",
            (plan_id, key.profile, key.root_session_id),
        )
        conn.execute(
            "INSERT INTO execution_bindings "
            "(profile, root_session_id, task_id, revision, bound_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (key.profile, key.root_session_id, plan_id, revision, now, now),
        )
        changed = conn.execute(
            "UPDATE tasks SET assignee = ?, session_id = ?, status = 'manual' "
            "WHERE id = ? AND status IN ('manual', 'archived', 'blocked')",
            (actor, session_id, plan_id),
        ).rowcount
        if changed != 1:
            raise InvalidTaskState(f"plan {plan_id} changed during continuation")
        if from_status != "manual":
            _append_event(
                conn,
                plan_id,
                "plan-continued",
                payload={"actor": actor, "from_status": from_status, "session_id": session_id},
                now=now,
            )
        stored = _get_binding_row(conn, key)
        if stored is None:
            raise ExecutionBindingError("plan binding disappeared during continuation")
        return _binding_from_row(stored)


def close_plan(
    conn: sqlite3.Connection,
    key: ExecutionKey,
    *,
    expected_task_id: str,
    expected_revision: int,
    outcome: PlanOutcome,
    reason: Optional[str],
    actor: str,
) -> PlanStepResult:
    """Close the active Plan and restore its explicit parent or unbind root."""
    _validate_key(key)
    if outcome not in {"done", "failed", "test-complete"}:
        raise ValueError("outcome must be done, failed, or test-complete")
    with write_txn(conn):
        current = _assert_expected_binding(
            conn, key, expected_task_id, expected_revision
        )
        return _close_plan_in_txn(
            conn,
            key,
            current,
            outcome=outcome,
            reason=reason,
            actor=actor,
        )
