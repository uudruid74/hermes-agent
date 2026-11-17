"""Regression tests: compression rotation must preserve the session's task binding.

Bug: 2026-11-17-plan-gate-relocks-on-compaction (kanban t_237562d7).

``publish_compression_child`` closes the parent and publishes the compacted
continuation atomically — inheriting the parent's cwd/git/profile and the
gateway origin columns (#59527) — but it historically did NOT carry
``sessions.task_id``.  The compression child was born unbound, so the live
session lost the plan/task binding the moment the context compacted:

* ``find_task_session_id`` (the wake target for approval/review/notify
  delivery) only matches LIVE rows — the bound parent is ended and the live
  child is unbound, so no wake could reach the worker ("approval that never
  comes").
* ``_worker_task_scope`` / ``_session_bound_task_id`` (kanban worker
  ownership gate) resolves through ``HERMES_SESSION_ID``, which the rotation
  re-points at the child — scope lost, worker degraded to orchestrator.

The recovery path (``_adopt_live_compression_child``) already transferred the
binding (t_2b7a77b2) but the PRIMARY publication path never did.
"""

from __future__ import annotations

import pytest

from hermes_state import SessionDB


def _connect(tmp_path):
    db = tmp_path / "state.db"
    return SessionDB(db_path=db)


def _rotate(db, parent_id, child_id, *, messages=None):
    """One compression rotation: lock, publish child, done."""
    assert db.try_acquire_compression_lock(parent_id, "holder", ttl_seconds=60)
    db.publish_compression_child(
        parent_session_id=parent_id,
        child_session_id=child_id,
        source="cli",
        messages=messages or [{"role": "user", "content": "compressed"}],
        compression_lock_holder="holder",
    )


def test_publish_compression_child_inherits_task_id(tmp_path):
    db = _connect(tmp_path)
    db.create_session("root", "cli")
    db.append_message("root", "user", "first")
    db.set_session_task_id("root", "t_plan42")

    _rotate(db, "root", "child")

    assert db.get_session_task_id("child") == "t_plan42"


def test_publish_compression_child_unbound_parent_stays_unbound(tmp_path):
    db = _connect(tmp_path)
    db.create_session("root", "cli")
    db.append_message("root", "user", "first")

    _rotate(db, "root", "child")

    assert db.get_session_task_id("child") is None


def test_find_task_session_id_returns_live_child_after_rotation(tmp_path):
    """The wake target must be the LIVE continuation, not the ended parent.

    ``find_task_session_id`` is how kanban notifications resolve the
    worker's real session (``_resolve_worker_session``,
    ``resolve_worker_live_session_id``).  After a rotation only the child is
    live; without the inherited binding no live session matches and
    approval/review wakes have nowhere to land.
    """
    db = _connect(tmp_path)
    db.create_session("root", "cli")
    db.append_message("root", "user", "first")
    db.set_session_task_id("root", "t_plan42")

    _rotate(db, "root", "child")

    assert db.find_task_session_id("t_plan42") == "child"


def test_multi_generation_rotation_keeps_task_id(tmp_path):
    db = _connect(tmp_path)
    db.create_session("gen0", "cli")
    db.append_message("gen0", "user", "first")
    db.set_session_task_id("gen0", "t_deep")

    _rotate(db, "gen0", "gen1")
    db.append_message("gen1", "user", "second")
    _rotate(db, "gen1", "gen2")

    assert db.get_session_task_id("gen1") == "t_deep"
    assert db.get_session_task_id("gen2") == "t_deep"
    assert db.find_task_session_id("t_deep") == "gen2"


def test_clear_session_task_id_still_clears_binding(tmp_path):
    """Inheritance must not change clear semantics (task complete/failed)."""
    db = _connect(tmp_path)
    db.create_session("root", "cli")
    db.append_message("root", "user", "first")
    db.set_session_task_id("root", "t_plan42")

    _rotate(db, "root", "child")
    assert db.get_session_task_id("child") == "t_plan42"

    db.clear_session_task_id("child")
    assert db.get_session_task_id("child") is None
    assert db.find_task_session_id("t_plan42") is None


def test_worker_task_scope_survives_rotation(tmp_path, monkeypatch):
    """Kanban worker identity (``-t`` wake path) must survive compaction.

    ``_session_bound_task_id`` reads ``get_session_task_id(HERMES_SESSION_ID)``
    and the rotation re-points ``HERMES_SESSION_ID`` at the child.  With the
    binding dropped, ``_worker_task_scope`` returned None and the worker
    degraded into an unrestricted orchestrator.
    """
    db = _connect(tmp_path)
    db.create_session("root", "cli")
    db.append_message("root", "user", "first")
    db.set_session_task_id("root", "t_wake7")

    _rotate(db, "root", "child")

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_SESSION_ID", "child")
    monkeypatch.setattr("hermes_state.SessionDB", lambda *args, **kwargs: db)

    from tools import kanban_tools as kt

    assert kt._session_bound_task_id() == "t_wake7"
    assert kt._worker_task_scope() == "t_wake7"
