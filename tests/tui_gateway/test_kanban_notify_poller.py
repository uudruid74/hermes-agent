"""Tests for the TUI-side kanban notification drain (origin routing).

Kanban notifications are origin-routed at event time
(``hermes_cli/kanban.py::_deliver_kanban_notification``): chat-platform
origins get a gateway wake, and local surfaces (CLI workers, TUI/desktop
sessions) land in the origin profile's ``session_notices`` queue. The
CLI drains its notices via SIGUSR1 (``cli.py::_flush_credit_notices``);
the TUI's agent is an AIAgent with no signal path, so the TUI poller's
``_collect_kanban_notifications`` is the drain half — it atomically
claims the session's queued notices and hands them to the notification
loop (status.update + agent turn).

These tests cover that drain: session-id derivation (agent's durable id
first, session_key fallback), profile-aware DB selection, atomic claim
(a second drain gets nothing), and graceful behavior on missing DB.
"""

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tui_gateway.server import (
    _collect_kanban_notifications,
)

SESSION_KEY = "tui-session-key-1"


def _session(key: str = SESSION_KEY, agent_session_id=None, profile_home=None) -> dict:
    agent = None
    if agent_session_id is not None:
        agent = SimpleNamespace(session_id=agent_session_id)
    sess = {"session_key": key, "agent": agent}
    if profile_home is not None:
        sess["profile_home"] = str(profile_home)
    return sess


class _FakeSessionDB:
    """Minimal stand-in for hermes_state.SessionDB notice draining."""

    def __init__(self, notices):
        self._notices = list(notices)
        self.closed = False

    def drain_session_notices(self, session_id):
        notices, self._notices = self._notices, []
        return notices

    def close(self):
        self.closed = True


class TestCollectKanbanNotifications:
    def test_no_session_key_is_noop(self):
        assert _collect_kanban_notifications({}) == []
        assert _collect_kanban_notifications({"session_key": ""}) == []

    def test_finalized_session_is_noop(self):
        sess = {"session_key": SESSION_KEY, "_finalized": True}
        assert _collect_kanban_notifications(sess) == []

    def test_notices_are_drained_and_returned(self):
        sess = _session(agent_session_id="20261117_120000_abcdef01")
        db = _FakeSessionDB([
            {"text": "✔ Kanban t_x done — title", "level": "info"},
            {"text": "⏸ Kanban t_y blocked: needs input", "level": "info"},
        ])
        with patch("tui_gateway.server._get_db", return_value=db):
            texts = _collect_kanban_notifications(sess)
        assert texts == [
            "✔ Kanban t_x done — title",
            "⏸ Kanban t_y blocked: needs input",
        ]

    def test_drain_is_atomic_second_call_gets_nothing(self):
        sess = _session(agent_session_id="s1")
        db = _FakeSessionDB([{"text": "notice", "level": "info"}])
        with patch("tui_gateway.server._get_db", return_value=db):
            first = _collect_kanban_notifications(sess)
            second = _collect_kanban_notifications(sess)
        assert first == ["notice"]
        assert second == []

    def test_agent_session_id_preferred_over_session_key(self):
        # The task's origin routing records the agent's durable session id;
        # notices are keyed on it, so it must be the drain key.
        sess = _session(agent_session_id="durable-id", )
        seen = {}

        class _KeyedDB(_FakeSessionDB):
            def drain_session_notices(self, session_id):
                seen["key"] = session_id
                return super().drain_session_notices(session_id)

        db = _KeyedDB([{"text": "n", "level": "info"}])
        with patch("tui_gateway.server._get_db", return_value=db):
            _collect_kanban_notifications(sess)
        assert seen["key"] == "durable-id"

    def test_session_key_fallback_when_agent_has_no_id(self):
        sess = _session(agent_session_id=None)
        seen = {}

        class _KeyedDB(_FakeSessionDB):
            def drain_session_notices(self, session_id):
                seen["key"] = session_id
                return super().drain_session_notices(session_id)

        db = _KeyedDB([])
        with patch("tui_gateway.server._get_db", return_value=db):
            _collect_kanban_notifications(sess)
        assert seen["key"] == SESSION_KEY

    def test_blank_notices_are_skipped(self):
        sess = _session(agent_session_id="s1")
        db = _FakeSessionDB([
            {"text": "  ", "level": "info"},
            {"text": "real", "level": "info"},
        ])
        with patch("tui_gateway.server._get_db", return_value=db):
            texts = _collect_kanban_notifications(sess)
        assert texts == ["real"]

    def test_unavailable_db_returns_empty(self):
        sess = _session(agent_session_id="s1")
        with patch("tui_gateway.server._get_db", return_value=None):
            assert _collect_kanban_notifications(sess) == []

    def test_drain_failure_is_swallowed(self):
        sess = _session(agent_session_id="s1")

        class _BrokenDB:
            def drain_session_notices(self, session_id):
                raise sqlite3.OperationalError("locked")

            def close(self):
                pass

        with patch("tui_gateway.server._get_db", return_value=_BrokenDB()):
            assert _collect_kanban_notifications(sess) == []

    def test_profile_home_session_uses_its_own_db_and_closes_it(self):
        # A remote/profile session's notices live in ITS profile's
        # state.db — the drain must open that db and close the handle.
        sess = _session(agent_session_id="s1", profile_home="/tmp/fake-profile")

        def _fake_sessiondb(db_path):
            db = _FakeSessionDB([{"text": "profile notice", "level": "info"}])
            return db

        with patch("hermes_state.SessionDB", side_effect=_fake_sessiondb):
            texts = _collect_kanban_notifications(sess)
        assert texts == ["profile notice"]

    def test_profile_home_db_open_failure_returns_empty(self, tmp_path):
        sess = _session(
            agent_session_id="s1",
            profile_home=str(tmp_path / "nonexistent-profile"),
        )
        # SessionDB() on a nonexistent home raises inside the constructor
        # guarded block — the poller must not propagate the exception.
        with patch(
            "hermes_state.SessionDB",
            side_effect=OSError("cannot open"),
        ):
            assert _collect_kanban_notifications(sess) == []
