"""Self-wake suppression for kanban notifications (t_0bae723b).

A kanban status change is delivered twice: a plain human notice (outbound,
starts no turn) and a JSON wake injected through the origin profile's bridge
as a synthetic inbound user message — which STARTS AN AGENT TURN in the
target session. When the action was authored by the very session being
woken (its own claim/comment/completion on a task it created or owns),
the wake is a self-echo and must be suppressed. The human notice stays.

Actor identity follows the same leak-guard rule as
``tools/environments/local.py::_inject_session_context_env``: ContextVars
are authoritative; when the session-context machinery is engaged but a var
is ``_UNSET`` (dispatcher tick, concurrent-host worker), the stale
``os.environ`` mirror is NOT trusted. Unknown actor suppresses nothing
(fail open) so dispatcher-spawned workers still wake their creators.
"""

import time as _time

import pytest

from pathlib import Path
from unittest.mock import patch

from hermes_cli import kanban_db as kb
from hermes_cli.kanban import (
    _notify_kanban_status_change,
    _wake_targets_acting_session,
    _acting_session_identity,
)

ORIGIN_SESSION = "20261004_004715_780dc3"
OTHER_SESSION = "20260901_000000_abcdef"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def clean_session_context(monkeypatch):
    """Reset session ContextVars and the engaged flag before/after each test.

    The engaged flag is process-global: another test file may have already
    engaged the session-context machinery in this pytest process, which
    would silently disable the os.environ CLI fallback paths. Reset both
    the ContextVars (to _UNSET) and the flag, and restore afterwards.
    """
    import gateway.session_context as sc

    monkeypatch.setattr(sc, "_session_context_engaged", False)
    sc.reset_session_vars()
    for name in (
        "HERMES_SESSION_ID",
        "HERMES_SESSION_PLATFORM",
        "HERMES_SESSION_CHAT_ID",
        "HERMES_SESSION_CHAT_TYPE",
        "HERMES_SESSION_THREAD_ID",
        "HERMES_SESSION_PROFILE",
        "HERMES_SESSION_USER_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    yield sc
    sc.reset_session_vars()


def _make_task(session_id=None, assignee="neo"):
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="self-wake suppression test",
            assignee=assignee,
            created_by="user",
            session_id=session_id,
        )
    return tid


def _store_session_origin(tid, session_id=ORIGIN_SESSION, profile="neo"):
    with kb.connect_closing() as conn:
        kb.store_origin_routing(
            conn, tid,
            platform="session", chat_id=session_id,
            profile=profile,
        )


def _store_telegram_origin(tid, chat_id="chat1", profile="neo", thread_id=""):
    # add_notify_sub-style INSERT via kb's subscription row is not what the
    # notifier reads; the origin comment must be a telegram origin. The
    # session-validated store_origin_routing() only accepts platform='session',
    # so write the telegram marker comment directly (same encoding).
    import json as _json

    payload = _json.dumps({
        "platform": "telegram",
        "chat_id": chat_id,
        "thread_id": thread_id or "",
        "chat_type": "dm",
        "profile": profile or "",
    })
    body = f"__kanban_origin__{payload}"
    now = int(_time.time())
    with kb.connect_closing() as conn:
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at)"
            " VALUES (?, 'system', ?, ?)",
            (tid, body, now),
        )
        conn.commit()


def _make_profile_state_db(kanban_home, profile, session_id, chat_id):
    """Create the origin profile's state.db with one live session row.

    ``_resolve_session_chat`` looks up ``sessions`` in the origin profile's
    state.db and parses ``session_key`` (agent:<agent>:<platform>:<chat_type>:<chat>)
    to turn a session origin into a deliverable chat. Without this row the
    notifier falls back to the session-notice path before ever reaching the
    wake — so the self-wake suppression branch is only exercised when the
    origin session resolves, which is exactly the production situation.
    """
    profiles_root = Path(kanban_home) / "profiles" / profile
    profiles_root.mkdir(parents=True, exist_ok=True)
    db_path = profiles_root / "state.db"
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sessions ("
        " id TEXT PRIMARY KEY, session_key TEXT, chat_id TEXT)"
    )
    conn.execute(
        "INSERT OR REPLACE INTO sessions (id, session_key, chat_id)"
        " VALUES (?, ?, ?)",
        (
            session_id,
            f"agent:main:telegram:dm:{chat_id}",
            chat_id,
        ),
    )
    conn.commit()
    conn.close()
    return db_path


def _patched_delivery():
    """Patch both delivery senders, returning (wake_mock, human_mock)."""
    wake = patch(
        "hermes_cli.kanban._send_kanban_wake",
        return_value=("adapter", {"success": True}),
    )
    human = patch(
        "hermes_cli.kanban._send_kanban_human_notification",
        return_value=("adapter", {"success": True}),
    )
    return wake, human


# ---------------------------------------------------------------------------
# _acting_session_identity: leak-guard semantics
# ---------------------------------------------------------------------------


def test_actor_identity_cli_fallback_when_not_engaged(
    clean_session_context, monkeypatch
):
    """Plain CLI process (not engaged): identity comes from os.environ."""
    monkeypatch.setenv("HERMES_SESSION_ID", ORIGIN_SESSION)
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "chat1")
    identity = _acting_session_identity()
    assert identity["HERMES_SESSION_ID"] == ORIGIN_SESSION
    assert identity["HERMES_SESSION_PLATFORM"] == "telegram"
    assert identity["HERMES_SESSION_CHAT_ID"] == "chat1"


def test_actor_identity_ignores_stale_env_when_engaged_unbound(
    clean_session_context,
):
    """Engaged but unbound (dispatcher tick): stale os.environ mirror must
    NOT be trusted — identity is unknown (empty strings)."""
    sc = clean_session_context
    import os

    sc.set_session_vars(
        platform="telegram", chat_id="some-other-chat", session_id=OTHER_SESSION,
    )
    # Simulate the per-message handler entry reset: vars go _UNSET while the
    # machinery stays engaged, and the process-global mirror keeps a stale id.
    sc.reset_session_vars()
    os.environ["HERMES_SESSION_ID"] = ORIGIN_SESSION
    identity = _acting_session_identity()
    assert identity["HERMES_SESSION_ID"] == ""
    assert identity["HERMES_SESSION_CHAT_ID"] == ""


def test_actor_identity_binds_authoritative_contextvars(
    clean_session_context,
):
    """Bound ContextVars win regardless of any os.environ mirror."""
    sc = clean_session_context
    sc.set_session_vars(
        platform="telegram", chat_id="chat1", session_id=ORIGIN_SESSION,
        thread_id="t1", profile="neo",
    )
    identity = _acting_session_identity()
    assert identity["HERMES_SESSION_ID"] == ORIGIN_SESSION
    assert identity["HERMES_SESSION_CHAT_ID"] == "chat1"
    assert identity["HERMES_SESSION_THREAD_ID"] == "t1"
    assert identity["HERMES_SESSION_PROFILE"] == "neo"


# ---------------------------------------------------------------------------
# _wake_targets_acting_session: predicate rules
# ---------------------------------------------------------------------------


def test_session_origin_matching_actor_suppressed(
    clean_session_context, monkeypatch
):
    """Worker-created task (session origin == actor session id)."""
    monkeypatch.setenv("HERMES_SESSION_ID", ORIGIN_SESSION)
    origin = {"platform": "session", "chat_id": ORIGIN_SESSION, "profile": "neo"}
    assert _wake_targets_acting_session(origin, "cli", "x", "", None) is True


def test_session_origin_other_actor_not_suppressed(
    clean_session_context, monkeypatch
):
    """Actor session differs from origin session → wake proceeds."""
    monkeypatch.setenv("HERMES_SESSION_ID", OTHER_SESSION)
    origin = {"platform": "session", "chat_id": ORIGIN_SESSION, "profile": "neo"}
    assert _wake_targets_acting_session(origin, "cli", "x", "", None) is False


def test_worker_no_session_context_not_suppressed(
    clean_session_context, monkeypatch
):
    """Dispatcher-spawned worker: no session env at all, never suppress."""
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    origin = {"platform": "session", "chat_id": ORIGIN_SESSION, "profile": "neo"}
    assert _wake_targets_acting_session(origin, "cli", "x", "", None) is False


def test_same_chat_same_profile_suppressed(clean_session_context):
    """Gateway-session actor bound to the same chat the wake targets."""
    sc = clean_session_context
    sc.set_session_vars(
        platform="telegram", chat_id="chat1", thread_id="",
        profile="neo",
    )
    origin = {"platform": "telegram", "chat_id": "chat1", "profile": "neo"}
    assert (
        _wake_targets_acting_session(origin, "telegram", "chat1", "", None)
        is True
    )


def test_same_chat_different_profile_not_suppressed(clean_session_context):
    """Shared group chat, different profile acting: the bridge delivers on
    the ORIGIN profile's adapter, so this is NOT a self-wake."""
    sc = clean_session_context
    sc.set_session_vars(
        platform="telegram", chat_id="chat1", thread_id="",
        profile="gopher",
    )
    origin = {"platform": "telegram", "chat_id": "chat1", "profile": "neo"}
    assert (
        _wake_targets_acting_session(origin, "telegram", "chat1", "", None)
        is False
    )


def test_task_session_id_match_suppressed(clean_session_context, monkeypatch):
    """tasks.session_id creation stamp matches the acting session even when
    the origin comment was overwritten by re-dispatch."""
    monkeypatch.setenv("HERMES_SESSION_ID", OTHER_SESSION)
    from types import SimpleNamespace

    task = SimpleNamespace(session_id=OTHER_SESSION)
    origin = {"platform": "telegram", "chat_id": "chat1", "profile": "neo"}
    assert (
        _wake_targets_acting_session(origin, "telegram", "chat1", "", task)
        is True
    )


# ---------------------------------------------------------------------------
# End-to-end through _notify_kanban_status_change
# ---------------------------------------------------------------------------


def test_notify_self_wake_suppressed_human_notice_kept(
    kanban_home, clean_session_context, monkeypatch,
):
    """Worker-created task, acted on by its own creating session:
    the wake (turn-starting inject) is dropped; the human notice goes out."""
    tid = _make_task(session_id=ORIGIN_SESSION)
    _store_session_origin(tid, ORIGIN_SESSION, profile="neo")
    _make_profile_state_db(
        kanban_home, "neo", ORIGIN_SESSION, chat_id="chat1",
    )
    monkeypatch.setenv("HERMES_SESSION_ID", ORIGIN_SESSION)
    monkeypatch.setenv("USERNAME", "neo")

    # _load_user_profile_env reads the origin profile dir from
    # get_profile_dir("neo") — created above with the state.db.
    wake, human = _patched_delivery()
    with wake as wake_mock, human as human_mock:
        _notify_kanban_status_change(tid, "done", summary="all good", title="T")
    wake_mock.assert_not_called()
    human_mock.assert_called_once()


def test_notify_creator_wake_still_fires_for_worker(
    kanban_home, clean_session_context, monkeypatch,
):
    """Worker (unknown actor) completing a creator-created task: the wake
    must still fire — this is the intended worker→creator path."""
    tid = _make_task(session_id=ORIGIN_SESSION)
    _store_session_origin(tid, ORIGIN_SESSION, profile="neo")
    _make_profile_state_db(
        kanban_home, "neo", ORIGIN_SESSION, chat_id="chat1",
    )
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setenv("USERNAME", "neo")

    wake, human = _patched_delivery()
    with wake as wake_mock, human as human_mock:
        _notify_kanban_status_change(tid, "done", summary="all good", title="T")
    wake_mock.assert_called_once()
    human_mock.assert_called_once()


def test_notify_telegram_self_chat_suppressed(
    kanban_home, clean_session_context, monkeypatch,
):
    """Gateway-session agent commenting on a task created from its own
    telegram chat: wake suppressed, human notice delivered."""
    tid = _make_task(session_id=None)
    _store_telegram_origin(tid, chat_id="8900123006", profile="neo")
    sc = clean_session_context
    sc.set_session_vars(
        platform="telegram", chat_id="8900123006", thread_id="",
        profile="neo", session_id=OTHER_SESSION,
    )
    monkeypatch.setenv("USERNAME", "neo")
    (Path(kanban_home) / "profiles").mkdir(exist_ok=True)

    wake, human = _patched_delivery()
    with wake as wake_mock, human as human_mock:
        _notify_kanban_status_change(tid, "commented", summary="note")
    wake_mock.assert_not_called()
    human_mock.assert_called_once()