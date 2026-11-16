"""Board→topic wake-routing map + worker identity binding (t_20c014fc).

Phase 1 — the board→topic resolver lives in the project DB and is additive:
a board with no mapping never opts in (legacy spawn); an empty topic opts in
with the single DM fallback; a topic opts in to that one topic (Evan's ruling
#4: "NO fallback chains — each board maps to ONE topic; Default -> null;
null/empty -> agent's DM. That is the ONLY fallback rule").

Phase 2 — the ownership gate scopes a worker to its one task via env (CLI
spawn) OR the session binding (``-t`` wake), and accepts an explicit
``task_id`` (Evan's ruling #1: "the gate must accept explicit task_id (env
stays for CLI spawn)").
"""
from __future__ import annotations

import json

import pytest


# ---------------------------------------------------------------------------
# Phase 1 — board→topic resolver
# ---------------------------------------------------------------------------

@pytest.fixture
def pdb_conn(tmp_path, monkeypatch):
    """Isolated projects DB connection."""
    from pathlib import Path

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from hermes_cli import projects_db as pdb

    with pdb.connect_closing() as conn:
        yield conn


def test_no_mapping_falls_back_to_spawn(pdb_conn):
    """A board that never opted in keeps the legacy spawn path (no rewire)."""
    from hermes_cli import projects_db as pdb

    route = pdb.resolve_board_wake_route(pdb_conn, "unmapped-board")
    assert route.mode == "spawn"
    assert route.is_wake is False
    assert route.topic is None


def test_empty_topic_opts_in_with_dm_fallback(pdb_conn):
    """Empty topic = opted in, wake to the agent's DM (the only fallback)."""
    from hermes_cli import projects_db as pdb

    pdb.set_board_topic(pdb_conn, "default", "")
    route = pdb.resolve_board_wake_route(pdb_conn, "default")
    assert route.mode == "wake"
    assert route.is_wake is True
    assert route.topic is None  # None → caller targets the agent DM


def test_topic_opts_in_to_one_topic(pdb_conn):
    """A mapped topic opts in to exactly that one topic (no fallback chain)."""
    from hermes_cli import projects_db as pdb

    pdb.set_board_topic(pdb_conn, "eng", "telegram:-100123:42")
    route = pdb.resolve_board_wake_route(pdb_conn, "eng")
    assert route.mode == "wake"
    assert route.topic == "telegram:-100123:42"


def test_clear_board_topic_returns_to_spawn(pdb_conn):
    """Opting back out restores the legacy spawn path."""
    from hermes_cli import projects_db as pdb

    pdb.set_board_topic(pdb_conn, "eng", "telegram:-100123:42")
    pdb.clear_board_topic(pdb_conn, "eng")
    route = pdb.resolve_board_wake_route(pdb_conn, "eng")
    assert route.mode == "spawn"
    assert pdb.get_board_topic(pdb_conn, "eng") is None


def test_set_board_topic_rejects_empty_board(pdb_conn):
    from hermes_cli import projects_db as pdb

    with pytest.raises(ValueError):
        pdb.set_board_topic(pdb_conn, "  ", "telegram:1:2")


# ---------------------------------------------------------------------------
# Phase 2 — worker identity / ownership gate
# ---------------------------------------------------------------------------

@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    """Simulate a CLI-spawned worker: HERMES_HOME isolated, env task set."""
    from pathlib import Path

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("USERNAME", "test-worker")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)

    from hermes_cli import kanban_db as kb

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="worker-test", assignee="test-worker")
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    return tid


def test_env_identity_scopes_cli_worker(worker_env):
    """CLI spawn path: HERMES_KANBAN_TASK is the worker's scope."""
    from tools import kanban_tools as kt

    assert kt._worker_task_scope() == worker_env
    assert kt._default_task_id(None) == worker_env
    assert kt._enforce_worker_task_ownership(worker_env) is None


def test_env_worker_rejects_foreign_task(worker_env):
    from tools import kanban_tools as kt

    assert kt._enforce_worker_task_ownership("t_other") is not None


def test_explicit_arg_wins_over_env(worker_env):
    """Ruling #1: the gate accepts an explicit task_id (it always wins)."""
    from tools import kanban_tools as kt

    assert kt._default_task_id("t_explicit") == "t_explicit"


def test_session_binding_scopes_woken_worker(monkeypatch, worker_env):
    """``-t`` wake path: no env, but the session binding carries the task."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(
        "tools.kanban_tools._session_bound_task_id", lambda: worker_env
    )
    from tools import kanban_tools as kt

    assert kt._worker_task_scope() == worker_env
    assert kt._default_task_id(None) == worker_env
    # Scoped to its own task only.
    assert kt._enforce_worker_task_ownership(worker_env) is None
    assert kt._enforce_worker_task_ownership("t_other") is not None


def test_unbound_context_is_unrestricted_orchestrator(monkeypatch, worker_env):
    """No env + no binding = orchestrator: routing is unrestricted."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr("tools.kanban_tools._session_bound_task_id", lambda: None)
    from tools import kanban_tools as kt

    assert kt._worker_task_scope() is None
    # Orchestrator may close out child tasks (foreign ids allowed).
    assert kt._enforce_worker_task_ownership("t_any_child") is None
    # ...but a bare kanban_complete still needs an explicit id (no default).
    assert kt._default_task_id(None) is None


# ---------------------------------------------------------------------------
# Phase 3 — per-task wake routing (2026-11-16 ripout regression)
# ---------------------------------------------------------------------------

def _make_task(task_id: str, assignee: str, board: str):
    """Build a minimal Task without touching a DB."""
    from hermes_cli.kanban_db import Task

    return Task(
        id=task_id,
        title="wake-route test",
        body=None,
        assignee=assignee,
        status="ready",
        priority=0,
        created_by="test",
        created_at=0,
        started_at=None,
        completed_at=None,
        workspace_kind="scratch",
        workspace_path="/tmp/wsx",
        claim_lock=None,
        claim_expires=None,
        tenant=None,
        board=board,
    )


class TestPerTaskWakeRouting:
    """The wake target comes from the task's OWN board column.

    Regression: an eddon-board task woke into the hermes-agent topic
    because the dispatcher tick resolved the wake route from an ambient
    "current board" pointer (2026-11-16, Evan: "We need the wake to go to
    you when you dispatch it" + "Rip that out now!").
    """

    @pytest.fixture
    def routed_env(self, monkeypatch, tmp_path):
        """Isolated kanban DB + projects DB with two opted-in boards."""
        from pathlib import Path

        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        # No ambient pin — the whole point.
        monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)

        from hermes_cli import kanban_db as kb
        from hermes_cli import projects_db as pdb

        kb._INITIALIZED_PATHS.clear()
        kb.init_db()
        with kb.connect() as conn:
            kb.create_board("eddon")
            kb.create_board("hermes-agent")
            kb.create_task(
                conn, title="eddon-task", assignee="neo", board="eddon"
            )
        with pdb.connect_closing() as pconn:
            pdb.set_board_topic(pconn, "eddon", "telegram:-100:4")
            pdb.set_board_topic(pconn, "hermes-agent", "telegram:-100:6")
        return kb

    def _capture_wake(self, kb, monkeypatch):
        """Return a (spawn_dispatch, wake_dispatch) capture pair."""
        import types

        calls = {"spawn": [], "wake": []}

        def fake_spawn(task, workspace, *, board=None):
            calls["spawn"].append((task.id, board))
            return None

        def fake_resolver(conn, board):
            from hermes_cli import projects_db as pdb

            with pdb.connect_closing() as pconn:
                return pdb.resolve_board_wake_route(pconn, board)

        # A real connection: the dispatch records a wake_fired event.
        conn = kb.connect()
        dispatch = kb._make_wake_dispatch(
            conn, fake_resolver, fallback_spawn=fake_spawn,
        )

        def fake_origin(conn, task_id):
            return {"chat_id": "-100"}

        def fake_send(argv, **kwargs):
            calls["wake"].append(argv)
            proc = types.SimpleNamespace(pid=4242)
            return proc

        monkeypatch.setattr(kb, "get_origin_routing", fake_origin)
        import hermes_cli._subprocess_compat as sc
        monkeypatch.setattr(sc, "spawn_detached", fake_send)
        return dispatch, calls

    def test_task_board_decides_wake_topic(self, routed_env, monkeypatch):
        """eddon task -> eddon topic, even with NO ambient board set."""
        kb = routed_env
        dispatch, calls = self._capture_wake(kb, monkeypatch)

        task = _make_task("t_eddon", "neo", "eddon")
        pid = dispatch(task, "/tmp/wsx")
        assert pid is None
        assert len(calls["wake"]) == 1
        argv = calls["wake"][0]
        # Topic /4 — NOT /6.
        assert "neo:telegram:-100:4" in argv

    def test_unmapped_board_falls_back_to_spawn(self, routed_env, monkeypatch):
        """A board with no topic mapping uses the classic spawn path."""
        kb = routed_env
        dispatch, calls = self._capture_wake(kb, monkeypatch)

        task = _make_task("t_plain", "neo", "default")
        dispatch(task, "/tmp/wsx")
        assert calls["wake"] == []
        # board=None here: the per-task dispatch was called without a
        # tick-level board, and the task's board never opted in — the
        # spawn falls through exactly as the classic dispatcher would.
        assert calls["spawn"] == [("t_plain", None)]

    def test_two_boards_route_independently(self, routed_env, monkeypatch):
        """Two tasks on different boards in one tick go to their own topics."""
        kb = routed_env
        dispatch, calls = self._capture_wake(kb, monkeypatch)

        dispatch(_make_task("t_e", "neo", "eddon"), "/tmp/wsx1")
        dispatch(_make_task("t_h", "neo", "hermes-agent"), "/tmp/wsx2")
        targets = [a[a.index("-t") + 1] for a in calls["wake"]]
        assert targets == ["neo:telegram:-100:4", "neo:telegram:-100:6"]
