"""Read-only migration audit for legacy SessionDB task identities.

The runtime authority is ``execution_bindings``.  This module deliberately
inspects the one-release legacy ``sessions.task_id`` data without applying it:
operators receive a deterministic manifest and must review/perform any
production mutation separately.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class LegacyBindingAudit:
    session_id: str
    task_id: str
    task_status: str | None
    authorization_state: str | None
    disposition: str
    reason: str


def audit_legacy_session_task_ids(state_conn: Any, kanban_conn: Any) -> list[LegacyBindingAudit]:
    """Return a sorted, mutation-free audit of non-empty legacy task IDs.

    A legacy row becomes a *candidate* only when it names an existing active
    Plan task.  The audit intentionally does not infer approval, completion,
    profile, or compression root; all other rows are conflicts for an
    operator to resolve.
    """
    session_rows = state_conn.execute(
        "SELECT id, task_id FROM sessions WHERE task_id IS NOT NULL AND task_id != ''"
    ).fetchall()
    records: list[LegacyBindingAudit] = []
    for session_id, task_id in sorted((str(row[0]), str(row[1])) for row in session_rows):
        task = kanban_conn.execute(
            "SELECT status FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if task is None:
            records.append(LegacyBindingAudit(
                session_id, task_id, None, None, "conflict", "task-missing"
            ))
            continue
        authorization = kanban_conn.execute(
            "SELECT state FROM plan_authorizations WHERE plan_id=?", (task_id,)
        ).fetchone()
        auth_state = str(authorization[0]) if authorization is not None else None
        status = str(task[0])
        if status != "manual":
            records.append(LegacyBindingAudit(
                session_id, task_id, status, auth_state, "conflict", "task-not-active-plan"
            ))
        elif auth_state != "approved":
            records.append(LegacyBindingAudit(
                session_id, task_id, status, auth_state, "conflict", "authorization-not-approved"
            ))
        else:
            records.append(LegacyBindingAudit(
                session_id, task_id, status, auth_state, "candidate", "operator-review-required"
            ))
    return records


def migration_manifest(state_conn: Any, kanban_conn: Any) -> dict[str, Any]:
    """Build a stable read-only operator manifest from legacy identity rows."""
    records = audit_legacy_session_task_ids(state_conn, kanban_conn)
    return {
        "format": "execution-binding-migration-audit-v1",
        "mutates_production": False,
        "records": [asdict(record) for record in records],
        "candidate_count": sum(record.disposition == "candidate" for record in records),
        "conflict_count": sum(record.disposition == "conflict" for record in records),
    }
