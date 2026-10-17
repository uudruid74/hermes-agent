"""Kanban worker-session wake binding — t_6a22e29b.

Notifications target the worker's real CLI session (cli:<session_id>) when
the task has a live bound session, instead of the chat-DM bridge wake. With
no bound session the configured notification routing is unchanged.
"""

import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban
from hermes_state import SessionDB


def _identity(session_id=""):
    """A full _acting_session_identity() result with the given session."""
    return {
        "HERMES_SESSION_ID": session_id,
        "HERMES_SESSION_PLATFORM": "",
        "HERMES_SESSION_CHAT_ID": "",
        "HERMES_SESSION_THREAD_ID": "",
        "HERMES_SESSION_PROFILE": "",
    }


@pytest.fixture()
def worker_home(tmp_path, monkeypatch):
    home = tmp_path / "neo"
    home.mkdir()

    import hermes_cli.profiles

    monkeypatch.setattr(hermes_cli.profiles, "get_profile_dir", lambda _name: home)
    monkeypatch.setattr(kanban, "_acting_session_identity", lambda: _identity(""))
    return home


def _bind_session(home, session_id, task_id):
    db = SessionDB(db_path=home / "state.db")
    db.create_session(session_id, source="kanban")
    db.set_session_task_id(session_id, task_id)
    db.close()


def _drain(home, session_id):
    db = SessionDB(db_path=home / "state.db")
    notices = db.drain_session_notices(session_id)
    db.close()
    return notices


def test_worker_wake_targets_bound_session(worker_home):
    _bind_session(worker_home, "worker-sess-1", "t_task")
    task = SimpleNamespace(id="t_task", assignee="neo")

    woken = kanban._wake_task_worker_session(
        task, json.dumps({"source": "kanban", "task_id": "t_task"}), "commented"
    )

    assert woken is True
    notices = _drain(worker_home, "worker-sess-1")
    assert len(notices) == 1
    assert "t_task" in notices[0]["text"]


def test_worker_wake_without_binding_falls_back(worker_home):
    task = SimpleNamespace(id="t_task", assignee="neo")
    assert kanban._wake_task_worker_session(task, "{}", "commented") is False


def test_worker_wake_suppresses_self_echo(worker_home, monkeypatch):
    _bind_session(worker_home, "worker-sess-1", "t_task")
    monkeypatch.setattr(
        kanban, "_acting_session_identity", lambda: _identity("worker-sess-1")
    )
    task = SimpleNamespace(id="t_task", assignee="neo")

    assert kanban._wake_task_worker_session(task, "{}", "commented") is False
    assert _drain(worker_home, "worker-sess-1") == []


def test_notify_wakes_worker_session_and_skips_chat_bridge_wake(
    worker_home, monkeypatch
):
    _bind_session(worker_home, "worker-sess-1", "t_task")
    task = SimpleNamespace(
        id="t_task",
        assignee="neo",
        created_by="gopher",
        session_id="creator-sess",
        title="Demo task",
    )
    conn = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(kanban.kb, "connect", lambda: conn)
    monkeypatch.setattr(
        kanban.kb,
        "get_origin_routing",
        lambda _c, _tid: {
            "platform": "telegram",
            "chat_id": "8900123006",
            "thread_id": "",
            "chat_type": "dm",
            "profile": "gopher",
        },
    )
    monkeypatch.setattr(kanban.kb, "get_task", lambda _c, _tid: task)
    monkeypatch.setattr(kanban, "_load_user_profile_env", lambda env, profile: None)

    human_calls = []
    wake_calls = []
    monkeypatch.setattr(
        kanban,
        "_send_kanban_human_notification",
        lambda **kw: human_calls.append(kw) or ("adapter", {"success": True}),
    )
    monkeypatch.setattr(
        kanban,
        "_send_kanban_wake",
        lambda **kw: wake_calls.append(kw) or ("adapter", {"success": True}),
    )

    kanban._notify_kanban_status_change(
        "t_task", "done", summary="shipped", title="Demo task", assignee="neo"
    )

    # Human chat visibility still fires; the LLM wake is the worker session.
    assert len(human_calls) == 1
    assert wake_calls == []
    notices = _drain(worker_home, "worker-sess-1")
    assert len(notices) == 1
    assert "t_task" in notices[0]["text"]


def test_notify_without_worker_session_keeps_existing_routing(
    worker_home, monkeypatch
):
    task = SimpleNamespace(
        id="t_task",
        assignee="neo",
        created_by="gopher",
        session_id="creator-sess",
        title="Demo task",
    )
    conn = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(kanban.kb, "connect", lambda: conn)
    monkeypatch.setattr(
        kanban.kb,
        "get_origin_routing",
        lambda _c, _tid: {
            "platform": "telegram",
            "chat_id": "8900123006",
            "thread_id": "",
            "chat_type": "dm",
            "profile": "gopher",
        },
    )
    monkeypatch.setattr(kanban.kb, "get_task", lambda _c, _tid: task)
    monkeypatch.setattr(kanban, "_load_user_profile_env", lambda env, profile: None)

    human_calls = []
    wake_calls = []
    monkeypatch.setattr(
        kanban,
        "_send_kanban_human_notification",
        lambda **kw: human_calls.append(kw) or ("adapter", {"success": True}),
    )
    monkeypatch.setattr(
        kanban,
        "_send_kanban_wake",
        lambda **kw: wake_calls.append(kw) or ("adapter", {"success": True}),
    )

    kanban._notify_kanban_status_change(
        "t_task", "done", summary="shipped", title="Demo task", assignee="neo"
    )

    assert len(human_calls) == 1
    assert len(wake_calls) == 1
