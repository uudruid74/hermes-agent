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
