"""Kanban worker boot binds the session to the claimed task — t_6a22e29b.

The sessions.task_id binding is what lets kanban notifications wake the
worker's real session (cli:<session_id>) instead of a chat DM.
"""

from types import SimpleNamespace

from hermes_state import SessionDB
from run_agent import AIAgent


def _fake_agent(tmp_path):
    return SimpleNamespace(
        _persist_disabled=False,
        _session_db=SessionDB(db_path=tmp_path / "state.db"),
        _session_db_created=False,
        session_id="worker-sess-1",
        model="test-model",
        _session_init_model_config=None,
        _cached_system_prompt=None,
        _parent_session_id=None,
        platform=None,
    )


def test_kanban_worker_boot_binds_session_to_task(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_task")
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "kanban")
    agent = _fake_agent(tmp_path)

    AIAgent._ensure_db_session(agent)  # type: ignore[reportArgumentType]

    assert agent._session_db_created is True
    assert agent._session_db.find_task_session_id("t_task") == "worker-sess-1"


def test_plain_session_boot_has_no_task_binding(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_SESSION_SOURCE", raising=False)
    agent = _fake_agent(tmp_path)

    AIAgent._ensure_db_session(agent)  # type: ignore[reportArgumentType]

    assert agent._session_db_created is True
    assert agent._session_db.find_task_session_id("t_task") is None