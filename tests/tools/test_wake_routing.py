"""Use-time wake routing — the clarification/notify prompt must land in the
recipient's CURRENT live session, never the home channel by default, never a
fabricated/stale session id (Evan's routing fix, 2026-10-19).

The bug this pins (verified on t_b0821a8e / t_a427292d, 2026-10-19):
  - ``hermes send -t <profile>`` expanded to the profile's HOME CHANNEL
    (gopher's home = the #general group) instead of the live dispatch
    session the plan was created in;
  - waking a captured compression-root label (e.g. ``20260906_...``) that
    names NO session row fabricated a fresh imaginary session.

Assertion shape from the brief: "the clarification prompt lands in the live
session 20261014_023141_da82753a, not 20260906_194042_f0b3c774".
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hermes_cli import kanban_db as kb  # noqa: E402
from tools import plan_binding_adapter  # noqa: E402

DEAD_SESSION = "20260906_194042_f0b3c774"  # names no row at all (compression root)
OLD_SESSION = "20261001_000000_deadbeef"  # real row, ended
LIVE_SESSION = "20261014_023141_da82753a"  # real row, current live
DM_KEY = "agent:main:telegram:dm:8900123006"


@pytest.fixture(autouse=True)
def _no_real_wake(monkeypatch):
    """Hostage guard (agent-wake-routing): fails loud on a real wake send."""

    def _guard(*_args, **_kwargs):
        raise AssertionError(
            "real 'hermes send' attempted — fake subprocess.run in the test"
        )

    monkeypatch.setattr("subprocess.run", _guard)


def _profiles(tmp_path, monkeypatch):
    """Fake profile homes; get_profile_dir resolves inside tmp_path only."""
    root = tmp_path / "profiles"
    root.mkdir()

    def fake_get_profile_dir(name):
        d = root / name
        d.mkdir(exist_ok=True)
        return d

    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir", fake_get_profile_dir
    )
    return root


def _state_db(root, profile, rows):
    """Fresh state.db for *profile* with the given session rows.

    Uses the real SessionDB schema — ``SessionDB._init_schema`` runs the
    full DDL on open (indexes reference columns like ``source``), so a
    minimal table breaks it.
    """
    from hermes_state_schema import SCHEMA_SQL

    (root / profile).mkdir(exist_ok=True)
    db_path = root / profile / "state.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(SCHEMA_SQL)
        conn.executemany(
            "INSERT INTO sessions (id, session_key, ended_at, started_at, task_id, "
            "source) VALUES (?, ?, ?, ?, ?, 'cli')",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _kanban_conn(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(kb.SCHEMA_SQL)
    return conn


def _task(conn, *, assignee="zephyr", created_by="gopher"):
    tid = kb.create_task(conn, title="wake routing", assignee=assignee)
    if created_by:
        conn.execute("UPDATE tasks SET created_by=? WHERE id=?", (created_by, tid))
    return tid


# ---------------------------------------------------------------------------
# resolve_live_session_id
# ---------------------------------------------------------------------------


def test_live_captured_id_resolves_to_itself(tmp_path, monkeypatch):
    root = _profiles(tmp_path, monkeypatch)
    _state_db(root, "gopher", [(LIVE_SESSION, DM_KEY, None, 100.0, None)])
    assert kb.resolve_live_session_id("gopher", LIVE_SESSION) == LIVE_SESSION


def test_dead_captured_id_rolls_forward_to_live_successor(tmp_path, monkeypatch):
    """The captured id is dead; its chat's CURRENT live session wins."""
    root = _profiles(tmp_path, monkeypatch)
    _state_db(root, "gopher", [
        (OLD_SESSION, DM_KEY, 900.0, 100.0, None),   # ended
        (LIVE_SESSION, DM_KEY, None, 950.0, None),   # current
    ])
    assert kb.resolve_live_session_id("gopher", OLD_SESSION) == LIVE_SESSION


def test_fabricated_root_id_resolves_to_none(tmp_path, monkeypatch):
    """A compression-root label names no session row — must NOT be woken."""
    root = _profiles(tmp_path, monkeypatch)
    _state_db(root, "gopher", [(LIVE_SESSION, DM_KEY, None, 950.0, None)])
    assert kb.resolve_live_session_id("gopher", DEAD_SESSION) is None


def test_dead_chat_with_no_live_successor_resolves_to_none(tmp_path, monkeypatch):
    root = _profiles(tmp_path, monkeypatch)
    _state_db(root, "gopher", [(OLD_SESSION, DM_KEY, 900.0, 100.0, None)])
    assert kb.resolve_live_session_id("gopher", OLD_SESSION) is None


# ---------------------------------------------------------------------------
# wake_target_for
# ---------------------------------------------------------------------------


def test_session_origin_wakes_live_session_not_home(tmp_path, monkeypatch):
    """The brief's assertion: the prompt lands in the live session id."""
    root = _profiles(tmp_path, monkeypatch)
    _state_db(root, "gopher", [(LIVE_SESSION, DM_KEY, None, 950.0, None)])
    conn = _kanban_conn(tmp_path, monkeypatch)
    tid = _task(conn)
    kb.store_origin_routing(
        conn, tid, platform="session", chat_id=LIVE_SESSION, profile="gopher",
        overwrite=True,
    )
    target = kb.wake_target_for(conn, tid, "gopher")
    # t_de2a9b4a contract: a chat-backed session rolls up to its CHAT —
    # the :cli: shape does NOT inject into the named session (the copy
    # posts to the profile's home session), so it cannot satisfy "lands
    # in the live session". See test_wake_target_shape.py.
    assert target == "gopher:telegram:8900123006"
    assert ":cli:" not in target


def test_stale_origin_rolls_forward_instead_of_waking_dead_session(
    tmp_path, monkeypatch
):
    """t_a427292d regression: never wake the dead captured id."""
    root = _profiles(tmp_path, monkeypatch)
    _state_db(root, "gopher", [
        (OLD_SESSION, DM_KEY, 900.0, 100.0, None),
        (LIVE_SESSION, DM_KEY, None, 950.0, None),
    ])
    conn = _kanban_conn(tmp_path, monkeypatch)
    tid = _task(conn)
    kb.store_origin_routing(
        conn, tid, platform="session", chat_id=OLD_SESSION, profile="gopher",
        overwrite=True,
    )
    target = kb.wake_target_for(conn, tid, "gopher")
    # t_de2a9b4a contract: rolls forward to the live session's CHAT shape.
    assert target == "gopher:telegram:8900123006"
    assert OLD_SESSION not in target


def test_fabricated_origin_id_falls_back_to_profile_home(tmp_path, monkeypatch):
    """Unresolvable captured id → home wake is the only sane fallback."""
    root = _profiles(tmp_path, monkeypatch)
    _state_db(root, "gopher", [(LIVE_SESSION, DM_KEY, None, 950.0, None)])
    conn = _kanban_conn(tmp_path, monkeypatch)
    tid = _task(conn)
    kb.store_origin_routing(
        conn, tid, platform="session", chat_id=DEAD_SESSION, profile="gopher",
        overwrite=True,
    )
    assert kb.wake_target_for(conn, tid, "gopher") == "gopher"


def test_chat_origin_wakes_that_exact_chat(tmp_path, monkeypatch):
    root = _profiles(tmp_path, monkeypatch)
    conn = _kanban_conn(tmp_path, monkeypatch)
    tid = _task(conn)
    kb.store_origin_routing(
        conn, tid,
        platform="telegram", chat_id="-1004406953125", thread_id="1",
        chat_type="group", profile="gopher",
        allow_non_session=True, overwrite=True,
    )
    target = kb.wake_target_for(conn, tid, "gopher")
    assert target == "gopher:telegram:-1004406953125:1"


def test_worker_direction_wakes_live_bound_session(tmp_path, monkeypatch):
    """Clarify-response → worker: the worker's live bound session (sessions.task_id
    binding), not the dispatcher's origin and not the home channel."""
    root = _profiles(tmp_path, monkeypatch)
    conn = _kanban_conn(tmp_path, monkeypatch)
    tid = _task(conn, assignee="zephyr", created_by="gopher")
    _state_db(root, "zephyr", [("20261015_111111_cafebabe", "k2", None, 1.0, tid)])
    kb.store_origin_routing(
        conn, tid, platform="session", chat_id=LIVE_SESSION, profile="gopher",
        overwrite=True,
    )
    target = kb.wake_target_for(conn, tid, "zephyr")
    assert target == "zephyr:cli:20261015_111111_cafebabe"


def test_nothing_resolvable_falls_back_to_profile_home(tmp_path, monkeypatch):
    root = _profiles(tmp_path, monkeypatch)
    _state_db(root, "zephyr", [])
    conn = _kanban_conn(tmp_path, monkeypatch)
    tid = _task(conn)
    assert kb.wake_target_for(conn, tid, "zephyr") == "zephyr"


def test_self_wake_guard_falls_back_to_home(tmp_path, monkeypatch):
    """t_0bae723b: a wake to the acting session is an echo — use home instead."""
    root = _profiles(tmp_path, monkeypatch)
    _state_db(root, "gopher", [(LIVE_SESSION, DM_KEY, None, 950.0, None)])
    conn = _kanban_conn(tmp_path, monkeypatch)
    tid = _task(conn)
    kb.store_origin_routing(
        conn, tid, platform="session", chat_id=LIVE_SESSION, profile="gopher",
        overwrite=True,
    )
    monkeypatch.setenv("HERMES_SESSION_ID", LIVE_SESSION)
    assert kb.wake_target_for(conn, tid, "gopher") == "gopher"


# ---------------------------------------------------------------------------
# _send_user delivers the resolved target to `hermes send -t`
# ---------------------------------------------------------------------------


def test_send_user_passes_live_session_target_to_send_t(tmp_path, monkeypatch):
    root = _profiles(tmp_path, monkeypatch)
    _state_db(root, "gopher", [(LIVE_SESSION, DM_KEY, None, 950.0, None)])
    conn = _kanban_conn(tmp_path, monkeypatch)
    tid = _task(conn)
    kb.store_origin_routing(
        conn, tid, platform="session", chat_id=LIVE_SESSION, profile="gopher",
        overwrite=True,
    )

    sent: list[list[str]] = []

    class _SendProc:
        returncode = 0
        stdout = json.dumps({"delivered": True})
        stderr = ""

    def fake_run(argv, **_kw):
        sent.append(list(argv))
        return _SendProc()

    monkeypatch.setattr(subprocess, "run", fake_run)
    error = plan_binding_adapter._send_user("gopher", "ping", conn=conn, task_id=tid)
    assert error is None
    # t_de2a9b4a contract: the resolved target is the CHAT shape.
    assert sent == [["hermes", "send", "-t", "gopher:telegram:8900123006", "ping"]]
