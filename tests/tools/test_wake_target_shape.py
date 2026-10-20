"""RC6 — dispatcher wake shape: session origins must wake the CHAT.

t_ce9ae675 (LIVE): a session-origin card's wake was addressed
``gopher:cli:<sid>`` — which does NOT inject into the named session; the copy
posts to the profile's HOME session (send_cmd._wake_copy_target). Gopher's
home session (2d4caf) was stale; he worked in 450e6ab9 — the wake was
invisible and he hand-dug the ask id from the DB. The proven shape is the
CHAT target (t_de2a9b4a row 505784: a user turn in the gateway's ACTIVE
session). These tests pin the shape selection in ``wake_target_for``.
"""

import sqlite3

import pytest

from hermes_cli import kanban_db


@pytest.fixture()
def profile_home(tmp_path, monkeypatch):
    """A temp profile home whose state.db carries the session rows."""
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE sessions ("
        "id TEXT PRIMARY KEY, session_key TEXT, chat_id TEXT,"
        "thread_id TEXT, ended_at REAL, started_at REAL)"
    )
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?)",
        ("sess-live", "agent:gopher:telegram:dm:8900123006", "8900123006", "", None, 2.0),
    )
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?)",
        ("sess-dead", "agent:gopher:telegram:dm:8900123006", "8900123006", "", 1.0, 1.0),
    )
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?)",
        ("sess-chatless", "gopher", None, "", None, 1.5),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda _p: tmp_path)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    return tmp_path


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE task_comments (task_id TEXT, body TEXT)")
    return conn


def _origin(monkeypatch, *, platform, chat_id, profile="gopher"):
    monkeypatch.setattr(
        kanban_db,
        "get_origin_routing",
        lambda _c, _t: {
            "platform": platform,
            "chat_id": chat_id,
            "thread_id": "",
            "chat_type": "",
            "profile": profile,
        },
    )


def test_session_origin_with_chat_wakes_the_chat(profile_home, monkeypatch):
    """The t_ce9ae675 regression: no ``:cli:`` shape for a chat-backed session."""
    _origin(monkeypatch, platform="session", chat_id="sess-live")
    target = kanban_db.wake_target_for(_conn(), "t_ce9ae675", "gopher")
    assert target == "gopher:telegram:8900123006"
    assert ":cli:" not in target


def test_session_origin_chatless_keeps_cli_shape(profile_home, monkeypatch):
    _origin(monkeypatch, platform="session", chat_id="sess-chatless")
    target = kanban_db.wake_target_for(_conn(), "t_x", "gopher")
    assert target == "gopher:cli:sess-chatless"


def test_session_origin_self_echo_falls_back_to_bare(profile_home, monkeypatch):
    """Waking the acting session is a self-echo — bare profile fallback."""
    monkeypatch.setenv("HERMES_SESSION_ID", "sess-live")
    _origin(monkeypatch, platform="session", chat_id="sess-live")
    target = kanban_db.wake_target_for(_conn(), "t_x", "gopher")
    assert target == "gopher"


def test_telegram_origin_shape_unchanged(profile_home, monkeypatch):
    """The proven t_de2a9b4a shape (row 505784) stays exactly as-is."""
    _origin(monkeypatch, platform="telegram", chat_id="8900123006")
    target = kanban_db.wake_target_for(_conn(), "t_de2a9b4a", "gopher")
    assert target == "gopher:telegram:8900123006"


def test_dead_session_rolls_forward_to_live_chat(profile_home, monkeypatch):
    """A stale captured id resolves to its chat's live session, then the chat."""
    _origin(monkeypatch, platform="session", chat_id="sess-dead")
    target = kanban_db.wake_target_for(_conn(), "t_x", "gopher")
    assert target == "gopher:telegram:8900123006"
