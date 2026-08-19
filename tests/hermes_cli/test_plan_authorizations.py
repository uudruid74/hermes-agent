"""TDD coverage for durable plan authorization records."""

from __future__ import annotations

import sqlite3

from hermes_cli import kanban_db as kb
from hermes_cli import plan_authorizations as auth


def _payload(**overrides):
    payload = {
        "title": "Durable approval",
        "goal": "Authorize exactly one immutable plan",
        "steps": ["Create authorization", "Release after approval"],
        "kind": "manual",
        "assign": None,
        "board": "default",
        "root": None,
        "cron": None,
        "resume": None,
    }
    payload.update(overrides)
    return payload


def test_fresh_kanban_schema_has_plan_authorization_tables_and_indexes(tmp_path):
    db_path = tmp_path / "kanban.db"

    with kb.connect(db_path) as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        indexes = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }

    assert {"plan_authorizations", "plan_authorization_events"} <= tables
    assert {
        "idx_plan_auth_state",
        "idx_plan_auth_origin",
        "idx_plan_auth_execution",
        "idx_plan_auth_events",
    } <= indexes


def test_plan_authorization_round_trips_through_row_and_dict():
    record = auth.PlanAuthorization(
        plan_id="t_plan",
        board="default",
        kind="manual",
        state="pending",
        plan_digest="a" * 64,
        execution_task_id="t_plan",
        execution_session_id="session-a",
        parent_task_id=None,
        origin_session_id="session-a",
        origin_platform="cli",
        origin_chat_id="terminal",
        origin_thread_id="",
        requested_at=1,
        presented_at=None,
        approved_at=None,
        approved_by_session_id=None,
        approved_by_actor=None,
        approved_via=None,
        denied_at=None,
        denied_by_session_id=None,
        denial_reason=None,
        revoked_at=None,
        revision=1,
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT ? AS plan_id, ? AS board, ? AS kind, ? AS state, ? AS plan_digest, "
        "? AS execution_task_id, ? AS execution_session_id, ? AS parent_task_id, "
        "? AS origin_session_id, ? AS origin_platform, ? AS origin_chat_id, "
        "? AS origin_thread_id, ? AS requested_at, ? AS presented_at, ? AS approved_at, "
        "? AS approved_by_session_id, ? AS approved_by_actor, ? AS approved_via, "
        "? AS denied_at, ? AS denied_by_session_id, ? AS denial_reason, ? AS revoked_at, "
        "? AS revision",
        tuple(record.to_dict().values()),
    ).fetchone()

    assert auth.PlanAuthorization.from_row(row).to_dict() == record.to_dict()


def test_create_pending_plan_records_requested_event(tmp_path):
    db_path = tmp_path / "kanban.db"
    request = auth.PlanRequest(
        plan_id="t_plan",
        payload=_payload(),
        execution_task_id="t_plan",
        execution_session_id="session-a",
        origin_session_id="session-a",
        origin_platform="cli",
        origin_chat_id="terminal",
    )

    with kb.connect(db_path) as conn:
        record = auth.create_pending_plan(conn, request)
        event = conn.execute(
            "SELECT kind, revision, actor_session_id, payload "
            "FROM plan_authorization_events WHERE plan_id = ?",
            (request.plan_id,),
        ).fetchone()

    assert record.state == "pending"
    assert record.plan_digest == auth.compute_plan_digest(request.payload)
    assert event["kind"] == "requested"
    assert event["revision"] == 1
    assert event["actor_session_id"] == "session-a"


def _pending(conn, plan_id="t_pending"):
    return auth.create_pending_plan(
        conn,
        auth.PlanRequest(
            plan_id=plan_id,
            payload=_payload(),
            execution_task_id=plan_id,
            execution_session_id="session-owner",
            origin_session_id="session-origin",
        ),
    )


def test_timeout_keeps_authorization_pending_and_representable(tmp_path):
    with kb.connect(tmp_path / "kanban.db") as conn:
        _pending(conn)
        auth.record_presentation_timeout(
            conn, "t_pending", auth.ApprovalActor("session-a", "Evan", "clarify")
        )
        presentation = auth.present_plan(
            conn, "t_pending", auth.ApprovalActor("session-b", "Evan", "remote")
        )
        events = conn.execute(
            "SELECT kind FROM plan_authorization_events WHERE plan_id = ? ORDER BY id",
            ("t_pending",),
        ).fetchall()

    assert presentation.authorization.plan_id == "t_pending"
    assert presentation.authorization.state == "pending"
    assert presentation.authorization.revision == 1
    assert [event["kind"] for event in events] == [
        "requested", "timed_out", "presented"
    ]


def test_explicit_deny_records_actor_and_blocks_future_resolution(tmp_path):
    with kb.connect(tmp_path / "kanban.db") as conn:
        _pending(conn)
        denied = auth.resolve_plan(
            conn,
            "t_pending",
            "denied",
            auth.ApprovalActor("session-b", "Evan", "dashboard"),
            reason="Need a smaller scope",
        )
        event = conn.execute(
            "SELECT kind, actor, actor_session_id, via FROM plan_authorization_events "
            "WHERE plan_id = ? ORDER BY id DESC LIMIT 1",
            ("t_pending",),
        ).fetchone()

        import pytest

        with pytest.raises(ValueError, match="not pending"):
            auth.resolve_plan(
                conn,
                "t_pending",
                "approved",
                auth.ApprovalActor("session-b", "Evan", "dashboard"),
            )

    assert denied.authorization.state == "denied"
    assert denied.authorization.denial_reason == "Need a smaller scope"
    assert tuple(event) == ("denied", "Evan", "session-b", "dashboard")


def test_approval_is_idempotent_without_duplicate_decision_event(tmp_path):
    with kb.connect(tmp_path / "kanban.db") as conn:
        _pending(conn)
        actor = auth.ApprovalActor("session-b", "Evan", "clarify")
        first = auth.resolve_plan(conn, "t_pending", "approved", actor)
        second = auth.resolve_plan(conn, "t_pending", "approved", actor)
        approved_events = conn.execute(
            "SELECT COUNT(*) FROM plan_authorization_events "
            "WHERE plan_id = ? AND kind = 'approved'",
            ("t_pending",),
        ).fetchone()[0]

    assert first.authorization.state == "approved"
    assert second.idempotent is True
    assert approved_events == 1


def test_resolve_rejects_missing_authorization_and_invalid_decision(tmp_path):
    import pytest

    with kb.connect(tmp_path / "kanban.db") as conn:
        actor = auth.ApprovalActor("session-b", "Evan", "clarify")
        with pytest.raises(ValueError, match="not found"):
            auth.resolve_plan(conn, "missing", "approved", actor)
        with pytest.raises(ValueError, match="decision"):
            auth.resolve_plan(conn, "missing", "maybe", actor)


def test_compute_plan_digest_is_canonical_across_mapping_order():
    first = _payload()
    second = dict(reversed(list(first.items())))

    assert auth.compute_plan_digest(first) == auth.compute_plan_digest(second)


def test_compute_plan_digest_changes_when_an_immutable_payload_field_changes():
    assert auth.compute_plan_digest(_payload()) != auth.compute_plan_digest(
        _payload(goal="A different approved goal")
    )


def test_compute_plan_digest_excludes_mutable_execution_state():
    payload = _payload()
    with_execution_state = {
        **payload,
        "execution_task_id": "t_runtime",
        "execution_session_id": "session-runtime",
        "state": "approved",
    }

    assert auth.compute_plan_digest(payload) == auth.compute_plan_digest(
        with_execution_state
    )
