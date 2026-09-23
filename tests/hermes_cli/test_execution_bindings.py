"""Contract tests for the authoritative Plan execution-binding kernel."""

from __future__ import annotations

import json
import sqlite3

import pytest

from hermes_cli import execution_bindings as bindings
from hermes_cli import kanban_db as kb


def _insert_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    status: str = "manual",
    previous_task: str | None = None,
    steps: tuple[str, ...] = ("first",),
    step_no: int | None = 1,
    pre_approved: bool = False,
    plan_kind: str = "normal",
) -> None:
    conn.execute(
        """
        INSERT INTO tasks (
            id, title, status, assignee, created_at, task_steps, task_stepno,
            task_goal, previous_task, plan_kind, pre_approved, board
        ) VALUES (?, ?, ?, 'tester', 1, ?, ?, 'goal', ?, ?, ?, 'default')
        """,
        (
            task_id,
            task_id,
            status,
            json.dumps(list(steps)),
            step_no,
            previous_task,
            plan_kind,
            int(pre_approved),
        ),
    )
    conn.commit()


def _approve(conn: sqlite3.Connection, plan_id: str) -> None:
    conn.execute(
        """
        INSERT INTO plan_authorizations (
            plan_id, board, kind, state, plan_digest, execution_task_id,
            requested_at, approved_at, revision
        ) VALUES (?, 'default', 'manual', 'approved', ?, ?, 1, 2, 1)
        """,
        (plan_id, "a" * 64, plan_id),
    )
    conn.commit()


def _key() -> bindings.ExecutionKey:
    return bindings.ExecutionKey(profile="wintermute", root_session_id="root-session")


def test_fresh_schema_has_execution_binding_table_and_index(tmp_path):
    with kb.connect(tmp_path / "kanban.db") as conn:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        indexes = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }

    assert "execution_bindings" in tables
    assert "idx_execution_bindings_task" in indexes


def test_worker_bootstrap_is_idempotent_but_conflicts_fail_closed(tmp_path):
    with kb.connect(tmp_path / "kanban.db") as conn:
        _insert_task(conn, "t_worker", status="running")
        _insert_task(conn, "t_other", status="running")

        first = bindings.bootstrap_worker_binding(conn, _key(), "t_worker")
        second = bindings.bootstrap_worker_binding(conn, _key(), "t_worker")

        assert first == second
        assert first.task_id == "t_worker"
        assert first.revision == 1

        with pytest.raises(bindings.ActiveTaskConflict):
            bindings.bootstrap_worker_binding(conn, _key(), "t_other")

        assert bindings.require_binding(conn, _key()).task_id == "t_worker"


def test_bootstrap_rejects_missing_or_closed_task(tmp_path):
    with kb.connect(tmp_path / "kanban.db") as conn:
        with pytest.raises(bindings.InvalidTaskState):
            bindings.bootstrap_worker_binding(conn, _key(), "t_missing")

        _insert_task(conn, "t_done", status="done", step_no=None)
        with pytest.raises(bindings.InvalidTaskState):
            bindings.bootstrap_worker_binding(conn, _key(), "t_done")


def test_plan_activation_requires_durable_approval(tmp_path):
    with kb.connect(tmp_path / "kanban.db") as conn:
        _insert_task(conn, "t_plan", status="blocked")

        with pytest.raises(bindings.PlanAuthorizationRequired):
            bindings.activate_plan(
                conn,
                _key(),
                "t_plan",
                expected_parent_task_id=None,
                expected_revision=None,
            )

        _approve(conn, "t_plan")
        active = bindings.activate_plan(
            conn,
            _key(),
            "t_plan",
            expected_parent_task_id=None,
            expected_revision=None,
        )

        task = conn.execute(
            "SELECT status, block_kind, previous_task FROM tasks WHERE id='t_plan'"
        ).fetchone()
        assert active.task_id == "t_plan"
        assert active.revision == 1
        assert dict(task) == {
            "status": "manual",
            "block_kind": None,
            "previous_task": None,
        }


def test_explicit_nested_activation_requires_exact_parent_and_revision(tmp_path):
    with kb.connect(tmp_path / "kanban.db") as conn:
        _insert_task(conn, "t_parent")
        _insert_task(conn, "t_child", status="blocked")
        _approve(conn, "t_child")
        parent = bindings.bootstrap_worker_binding(conn, _key(), "t_parent")

        with pytest.raises(bindings.ActiveTaskConflict):
            bindings.activate_plan(
                conn,
                _key(),
                "t_child",
                expected_parent_task_id=None,
                expected_revision=parent.revision,
            )
        with pytest.raises(bindings.BindingRevisionConflict):
            bindings.activate_plan(
                conn,
                _key(),
                "t_child",
                expected_parent_task_id="t_parent",
                expected_revision=parent.revision + 1,
            )

        child = bindings.activate_plan(
            conn,
            _key(),
            "t_child",
            expected_parent_task_id="t_parent",
            expected_revision=parent.revision,
        )
        row = conn.execute(
            "SELECT previous_task, status FROM tasks WHERE id='t_child'"
        ).fetchone()

        assert child.task_id == "t_child"
        assert child.revision == parent.revision + 1
        assert dict(row) == {"previous_task": "t_parent", "status": "manual"}


def test_intermediate_advance_changes_step_and_binding_revision(tmp_path):
    with kb.connect(tmp_path / "kanban.db") as conn:
        _insert_task(conn, "t_plan", steps=("first", "second"))
        current = bindings.bootstrap_worker_binding(conn, _key(), "t_plan")

        result = bindings.advance_plan(
            conn,
            _key(),
            expected_task_id="t_plan",
            expected_revision=current.revision,
            summary="verified first",
            actor="Wintermute",
            proof="pytest: 3 passed",
        )

        assert result.closed is False
        assert result.step_no == 2
        assert result.next_step == "second"
        assert result.binding_revision == current.revision + 1
        assert conn.execute(
            "SELECT task_stepno FROM tasks WHERE id='t_plan'"
        ).fetchone()[0] == 2


@pytest.mark.parametrize(
    ("outcome", "expected_status", "event_kind"),
    [
        ("done", "done", "plan-completed"),
        ("failed", "archived", "plan-failed"),
        ("test-complete", "archived", "plan-test-complete"),
    ],
)
def test_every_terminal_outcome_restores_the_same_parent(
    tmp_path, outcome, expected_status, event_kind
):
    with kb.connect(tmp_path / f"{outcome}.db") as conn:
        _insert_task(conn, "t_parent")
        _insert_task(conn, "t_child", previous_task="t_parent")
        parent = bindings.bootstrap_worker_binding(conn, _key(), "t_parent")
        conn.execute(
            "UPDATE execution_bindings SET task_id='t_child', revision=? "
            "WHERE profile=? AND root_session_id=?",
            (parent.revision + 1, _key().profile, _key().root_session_id),
        )
        conn.commit()
        child = bindings.require_binding(conn, _key())

        result = bindings.close_plan(
            conn,
            _key(),
            expected_task_id="t_child",
            expected_revision=child.revision,
            outcome=outcome,
            reason="controlled" if outcome == "failed" else None,
            actor="Wintermute",
        )

        assert result.closed is True
        assert result.restored_task_id == "t_parent"
        restored = bindings.require_binding(conn, _key())
        assert restored.task_id == "t_parent"
        assert restored.revision == child.revision + 1
        assert conn.execute(
            "SELECT status FROM tasks WHERE id='t_child'"
        ).fetchone()[0] == expected_status
        assert conn.execute(
            "SELECT 1 FROM task_events WHERE task_id='t_child' AND kind=?",
            (event_kind,),
        ).fetchone()


def test_final_advance_closes_root_and_removes_binding(tmp_path, monkeypatch):
    with kb.connect(tmp_path / "kanban.db") as conn:
        _insert_task(conn, "t_plan")
        current = bindings.bootstrap_worker_binding(conn, _key(), "t_plan")
        transaction_states: list[bool] = []
        monkeypatch.setattr(
            "hermes_cli.kanban._notify_kanban_status_change",
            lambda *_args, **_kwargs: transaction_states.append(conn.in_transaction),
        )

        result = bindings.advance_plan(
            conn,
            _key(),
            expected_task_id="t_plan",
            expected_revision=current.revision,
            summary="verified final step",
            actor="Wintermute",
            proof="commit deadbeef",
        )

        assert result.closed is True
        assert result.restored_task_id is None
        assert bindings.get_binding(conn, _key()) is None
        row = conn.execute(
            "SELECT status, task_stepno FROM tasks WHERE id='t_plan'"
        ).fetchone()
        assert row["status"] == "done"
        assert row["task_stepno"] is None
        assert transaction_states == [False]


def test_root_close_notifies_only_after_transaction_commits(tmp_path, monkeypatch):
    """Notification must never run while close_plan owns the Kanban write lock."""
    with kb.connect(tmp_path / "kanban.db") as conn:
        _insert_task(conn, "t_root")
        current = bindings.bootstrap_worker_binding(conn, _key(), "t_root")
        transaction_states: list[bool] = []
        monkeypatch.setattr(
            "hermes_cli.kanban._notify_kanban_status_change",
            lambda *_args, **_kwargs: transaction_states.append(conn.in_transaction),
        )

        result = bindings.close_plan(
            conn,
            _key(),
            expected_task_id="t_root",
            expected_revision=current.revision,
            outcome="failed",
            reason="controlled failure",
            actor="Wintermute",
        )

        assert result.closed is True
        assert result.restored_task_id is None
        assert transaction_states == [False]


def test_stale_compare_and_set_does_not_mutate_plan(tmp_path):
    with kb.connect(tmp_path / "kanban.db") as conn:
        _insert_task(conn, "t_plan", steps=("first", "second"))
        current = bindings.bootstrap_worker_binding(conn, _key(), "t_plan")

        with pytest.raises(bindings.BindingRevisionConflict):
            bindings.advance_plan(
                conn,
                _key(),
                expected_task_id="t_plan",
                expected_revision=current.revision + 1,
                summary="stale summary",
                actor="Wintermute",
            )

        assert conn.execute(
            "SELECT task_stepno FROM tasks WHERE id='t_plan'"
        ).fetchone()[0] == 1
        assert bindings.require_binding(conn, _key()) == current


def test_event_failure_rolls_back_task_binding_comment_and_event(tmp_path, monkeypatch):
    with kb.connect(tmp_path / "kanban.db") as conn:
        _insert_task(conn, "t_plan", steps=("first", "second"))
        current = bindings.bootstrap_worker_binding(conn, _key(), "t_plan")

        def fail_event(*_args, **_kwargs):
            raise RuntimeError("injected event failure")

        monkeypatch.setattr(bindings, "_append_event", fail_event)
        with pytest.raises(RuntimeError, match="injected event failure"):
            bindings.advance_plan(
                conn,
                _key(),
                expected_task_id="t_plan",
                expected_revision=current.revision,
                summary="must roll back",
                actor="Wintermute",
                proof="commit deadbeef",
            )

        assert conn.execute(
            "SELECT task_stepno FROM tasks WHERE id='t_plan'"
        ).fetchone()[0] == 1
        assert bindings.require_binding(conn, _key()) == current
        assert conn.execute(
            "SELECT COUNT(*) FROM task_comments WHERE task_id='t_plan'"
        ).fetchone()[0] == 0


def test_continue_archived_plan_reactivates_and_logs_event(tmp_path):
    """Evan 2026-09-13: `continue` must revive a fail/test-complete-archived
    plan into the caller's current session as status manual, and log a
    plan-continued event so the resurrection is visible in the audit trail."""
    with kb.connect(tmp_path / "kanban.db") as conn:
        _insert_task(conn, "t_archived", status="archived",
                     steps=("first", "second"), step_no=2)

        result = bindings.continue_plan(
            conn, _key(), "t_archived",
            actor="Ornith", session_id="session-2",
        )

        assert result.task_id == "t_archived"
        row = conn.execute(
            "SELECT status, assignee, session_id, task_stepno FROM tasks WHERE id='t_archived'"
        ).fetchone()
        assert row["status"] == "manual"
        assert row["assignee"] == "Ornith"
        assert row["session_id"] == "session-2"
        assert row["task_stepno"] == 2  # resumes at the failed step

        event = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id='t_archived' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert event["kind"] == "plan-continued"
        payload = json.loads(event["payload"])
        assert payload["from_status"] == "archived"
        assert payload["actor"] == "Ornith"
        assert payload["session_id"] == "session-2"


def test_continue_rejects_terminal_done_status(tmp_path):
    """Done plans stay closed — continue must not resurrect completed work."""
    with kb.connect(tmp_path / "kanban.db") as conn:
        _insert_task(conn, "t_done", status="done", step_no=None)

        with pytest.raises(bindings.InvalidTaskState, match="cannot continue"):
            bindings.continue_plan(
                conn, _key(), "t_done",
                actor="Ornith", session_id="session-2",
            )


def test_continue_blocked_plan_converts_to_manual_and_rebinds(tmp_path):
    """Evan 2026-09-21: `continue` must accept a blocked plan — convert it to
    manual and bind it to the caller's current session (live handoff from the
    kanban blocked pool to an active Telegram-watchable session)."""
    with kb.connect(tmp_path / "kanban.db") as conn:
        _insert_task(conn, "t_blocked", status="blocked",
                     steps=("first", "second"), step_no=1)

        result = bindings.continue_plan(
            conn, _key(), "t_blocked",
            actor="Neo", session_id="session-live",
        )

        assert result.task_id == "t_blocked"
        row = conn.execute(
            "SELECT status, assignee, session_id, task_stepno FROM tasks WHERE id='t_blocked'"
        ).fetchone()
        assert row["status"] == "manual"       # blocked → manual conversion
        assert row["assignee"] == "Neo"
        assert row["session_id"] == "session-live"
        assert row["task_stepno"] == 1

        event = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id='t_blocked' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert event["kind"] == "plan-continued"
        payload = json.loads(event["payload"])
        assert payload["from_status"] == "blocked"
