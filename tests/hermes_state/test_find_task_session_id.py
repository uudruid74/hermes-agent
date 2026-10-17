"""Tests for SessionDB.find_task_session_id — the kanban worker-session
binding lookup that lets notifications wake a worker's real session
(cli:<session_id>) instead of a chat DM."""

import pytest

from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    d = SessionDB(db_path=tmp_path / "state.db")
    yield d
    d.close()


def test_returns_bound_live_session(db):
    db.create_session("worker-1", source="kanban")
    db.set_session_task_id("worker-1", "t_live")
    assert db.find_task_session_id("t_live") == "worker-1"


def test_prefers_most_recent_live_session(db):
    db.create_session("worker-old", source="kanban")
    db.set_session_task_id("worker-old", "t_retried")
    db.create_session("worker-new", source="kanban")
    db.set_session_task_id("worker-new", "t_retried")
    assert db.find_task_session_id("t_retried") == "worker-new"


def test_ignores_ended_sessions(db):
    db.create_session("worker-done", source="kanban")
    db.set_session_task_id("worker-done", "t_done")
    db.end_session("worker-done", "completed")
    assert db.find_task_session_id("t_done") is None


def test_unbound_task_returns_none(db):
    db.create_session("worker-1", source="kanban")
    assert db.find_task_session_id("t_unbound") is None


def test_empty_task_id_returns_none(db):
    db.create_session("worker-1", source="kanban")
    db.set_session_task_id("worker-1", "")
    assert db.find_task_session_id("") is None
