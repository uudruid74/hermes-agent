"""Durable immutable-digest plan authorization kernel.

Only this module mutates plan authorization records.  Tool, cron, and
Dashboard surfaces use these direct functions rather than writing the tables.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import hashlib
import json
import sqlite3
import time
from typing import Any, Mapping, Optional

from hermes_cli.kanban_db import write_txn


_IMMUTABLE_PAYLOAD_KEYS = (
    "title",
    "goal",
    "steps",
    "kind",
    "assign",
    "board",
    "root",
    "cron",
    "resume",
)


@dataclass(frozen=True)
class PlanAuthorization:
    plan_id: str
    board: str
    kind: str
    state: str
    plan_digest: str
    execution_task_id: Optional[str]
    execution_session_id: Optional[str]
    parent_task_id: Optional[str]
    origin_session_id: Optional[str]
    origin_platform: Optional[str]
    origin_chat_id: Optional[str]
    origin_thread_id: str
    requested_at: int
    presented_at: Optional[int]
    approved_at: Optional[int]
    approved_by_session_id: Optional[str]
    approved_by_actor: Optional[str]
    approved_via: Optional[str]
    denied_at: Optional[int]
    denied_by_session_id: Optional[str]
    denial_reason: Optional[str]
    revoked_at: Optional[int]
    revision: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "PlanAuthorization":
        return cls(**{field.name: row[field.name] for field in fields(cls)})


@dataclass(frozen=True)
class ApprovalActor:
    session_id: Optional[str]
    actor: Optional[str]
    via: str


@dataclass(frozen=True)
class Presentation:
    authorization: PlanAuthorization


@dataclass(frozen=True)
class ReleaseResult:
    authorization: PlanAuthorization
    idempotent: bool = False
    released: bool = False


@dataclass(frozen=True)
class PlanRequest:
    plan_id: str
    payload: Mapping[str, Any]
    execution_task_id: Optional[str] = None
    execution_session_id: Optional[str] = None
    parent_task_id: Optional[str] = None
    origin_session_id: Optional[str] = None
    origin_platform: Optional[str] = None
    origin_chat_id: Optional[str] = None
    origin_thread_id: str = ""


def _normalized_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Select precisely the immutable authorization boundary."""
    assign = payload.get("assign")
    if assign is None:
        assign = payload.get("assignee")
    return {
        "title": payload.get("title"),
        "goal": payload.get("goal"),
        "steps": list(payload.get("steps") or []),
        "kind": payload.get("kind"),
        "assign": assign,
        "board": payload.get("board"),
        "root": payload.get("root"),
        "cron": payload.get("cron"),
        "resume": payload.get("resume"),
    }


def compute_plan_digest(payload: Mapping[str, Any]) -> str:
    """Return SHA-256 for normalized immutable plan content only."""
    canonical = json.dumps(
        _normalized_payload(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _append_event(
    conn: sqlite3.Connection,
    plan_id: str,
    revision: int,
    kind: str,
    *,
    actor: Optional[str] = None,
    actor_session_id: Optional[str] = None,
    via: Optional[str] = None,
    payload: Optional[Mapping[str, Any]] = None,
) -> None:
    conn.execute(
        """
        INSERT INTO plan_authorization_events
            (plan_id, revision, kind, actor, actor_session_id, via, payload, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            plan_id,
            revision,
            kind,
            actor,
            actor_session_id,
            via,
            json.dumps(payload, sort_keys=True, separators=(",", ":"))
            if payload is not None
            else None,
            int(time.time()),
        ),
    )


def create_pending_plan(
    conn: sqlite3.Connection, request: PlanRequest
) -> PlanAuthorization:
    """Create one pending durable authorization and its requested audit event."""
    payload = _normalized_payload(request.payload)
    if payload["kind"] not in {"manual", "kanban", "cron"}:
        raise ValueError("plan kind must be manual, kanban, or cron")
    digest = compute_plan_digest(payload)
    now = int(time.time())

    with write_txn(conn):
        existing = conn.execute(
            "SELECT * FROM plan_authorizations WHERE plan_id = ?", (request.plan_id,)
        ).fetchone()
        if existing is not None:
            record = PlanAuthorization.from_row(existing)
            if record.plan_digest != digest:
                raise ValueError("plan id already has a different immutable digest")
            return record
        conn.execute(
            """
            INSERT INTO plan_authorizations (
                plan_id, board, kind, state, plan_digest, execution_task_id,
                execution_session_id, parent_task_id, origin_session_id,
                origin_platform, origin_chat_id, origin_thread_id, requested_at
            ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request.plan_id,
                payload["board"] or "default",
                payload["kind"],
                digest,
                request.execution_task_id,
                request.execution_session_id,
                request.parent_task_id,
                request.origin_session_id,
                request.origin_platform,
                request.origin_chat_id,
                request.origin_thread_id,
                now,
            ),
        )
        _append_event(
            conn,
            request.plan_id,
            1,
            "requested",
            actor_session_id=request.origin_session_id,
            payload={"plan_digest": digest},
        )
        row = conn.execute(
            "SELECT * FROM plan_authorizations WHERE plan_id = ?", (request.plan_id,)
        ).fetchone()
    return PlanAuthorization.from_row(row)


def present_plan(
    conn: sqlite3.Connection, plan_id: str, presenter: ApprovalActor
) -> Presentation:
    """Record a presentation of a still-pending authorization."""
    now = int(time.time())
    with write_txn(conn):
        row = conn.execute(
            "SELECT * FROM plan_authorizations WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"plan authorization {plan_id} not found")
        record = PlanAuthorization.from_row(row)
        if record.state != "pending":
            raise ValueError(f"plan authorization {plan_id} is not pending")
        conn.execute(
            "UPDATE plan_authorizations SET presented_at = ? WHERE plan_id = ?",
            (now, plan_id),
        )
        _append_event(
            conn,
            plan_id,
            record.revision,
            "presented",
            actor=presenter.actor,
            actor_session_id=presenter.session_id,
            via=presenter.via,
        )
        updated = conn.execute(
            "SELECT * FROM plan_authorizations WHERE plan_id = ?", (plan_id,)
        ).fetchone()
    return Presentation(PlanAuthorization.from_row(updated))


def record_presentation_timeout(
    conn: sqlite3.Connection, plan_id: str, presenter: ApprovalActor
) -> None:
    """Audit an unanswered presentation without converting it into a denial."""
    with write_txn(conn):
        row = conn.execute(
            "SELECT state, revision FROM plan_authorizations WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"plan authorization {plan_id} not found")
        if row["state"] != "pending":
            raise ValueError(f"plan authorization {plan_id} is not pending")
        _append_event(
            conn,
            plan_id,
            row["revision"],
            "timed_out",
            actor=presenter.actor,
            actor_session_id=presenter.session_id,
            via=presenter.via,
        )


def resolve_plan(
    conn: sqlite3.Connection,
    plan_id: str,
    decision: str,
    actor: ApprovalActor,
    *,
    reason: Optional[str] = None,
) -> ReleaseResult:
    """Resolve exactly one pending authorization; repeat approval is read-only."""
    if decision not in {"approved", "denied"}:
        raise ValueError("decision must be approved or denied")
    now = int(time.time())
    with write_txn(conn):
        row = conn.execute(
            "SELECT * FROM plan_authorizations WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"plan authorization {plan_id} not found")
        record = PlanAuthorization.from_row(row)
        if record.state == "approved" and decision == "approved":
            return ReleaseResult(record, idempotent=True)
        if record.state != "pending":
            raise ValueError(f"plan authorization {plan_id} is not pending")
        if decision == "approved":
            conn.execute(
                """
                UPDATE plan_authorizations
                   SET state = 'approved', approved_at = ?,
                       approved_by_session_id = ?, approved_by_actor = ?, approved_via = ?
                 WHERE plan_id = ? AND state = 'pending'
                """,
                (now, actor.session_id, actor.actor, actor.via, plan_id),
            )
        else:
            conn.execute(
                """
                UPDATE plan_authorizations
                   SET state = 'denied', denied_at = ?, denied_by_session_id = ?,
                       denial_reason = ?
                 WHERE plan_id = ? AND state = 'pending'
                """,
                (now, actor.session_id, reason, plan_id),
            )
        _append_event(
            conn,
            plan_id,
            record.revision,
            decision,
            actor=actor.actor,
            actor_session_id=actor.session_id,
            via=actor.via,
            payload={"reason": reason} if decision == "denied" and reason else None,
        )
        updated = conn.execute(
            "SELECT * FROM plan_authorizations WHERE plan_id = ?", (plan_id,)
        ).fetchone()
    return ReleaseResult(PlanAuthorization.from_row(updated))


def resolve_plan_in_txn(
    conn: sqlite3.Connection,
    plan_id: str,
    decision: str,
    actor: ApprovalActor,
    *,
    reason: Optional[str] = None,
) -> ReleaseResult:
    """Resolve an authorization inside a caller-owned transaction.

    This is the Plan activation primitive: callers can combine approval,
    task activation, binding installation, and audit events under one commit.
    """
    if decision not in {"approved", "denied"}:
        raise ValueError("decision must be approved or denied")
    now = int(time.time())
    row = conn.execute(
        "SELECT * FROM plan_authorizations WHERE plan_id = ?", (plan_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"plan authorization {plan_id} not found")
    record = PlanAuthorization.from_row(row)
    if record.state == "approved" and decision == "approved":
        return ReleaseResult(record, idempotent=True)
    if record.state != "pending":
        raise ValueError(f"plan authorization {plan_id} is not pending")
    if decision == "approved":
        conn.execute(
            """
            UPDATE plan_authorizations
               SET state = 'approved', approved_at = ?,
                   approved_by_session_id = ?, approved_by_actor = ?, approved_via = ?
             WHERE plan_id = ? AND state = 'pending'
            """,
            (now, actor.session_id, actor.actor, actor.via, plan_id),
        )
    else:
        conn.execute(
            """
            UPDATE plan_authorizations
               SET state = 'denied', denied_at = ?, denied_by_session_id = ?,
                   denial_reason = ?
             WHERE plan_id = ? AND state = 'pending'
            """,
            (now, actor.session_id, reason, plan_id),
        )
    _append_event(
        conn,
        plan_id,
        record.revision,
        decision,
        actor=actor.actor,
        actor_session_id=actor.session_id,
        via=actor.via,
        payload={"reason": reason} if decision == "denied" and reason else None,
    )
    updated = conn.execute(
        "SELECT * FROM plan_authorizations WHERE plan_id = ?", (plan_id,)
    ).fetchone()
    return ReleaseResult(PlanAuthorization.from_row(updated))


def get_plan_authorization(
    conn: sqlite3.Connection, plan_id: str
) -> Optional[PlanAuthorization]:
    row = conn.execute(
        "SELECT * FROM plan_authorizations WHERE plan_id = ?", (plan_id,)
    ).fetchone()
    return PlanAuthorization.from_row(row) if row is not None else None
