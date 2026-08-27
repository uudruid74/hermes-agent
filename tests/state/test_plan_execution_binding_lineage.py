"""RED contract tests for SessionDB root session lineage API."""

from __future__ import annotations

import sqlite3
import pytest

from hermes_state import SessionDB


def _connect(tmp_path):
    db = tmp_path / "state.db"
    return SessionDB(db_path=db)


def test_get_root_session_id_returns_self_when_no_parent(tmp_path):
    db = _connect(tmp_path)
    db.create_session("root", "cli")
    assert db.get_root_session_id("root") == "root"


def test_get_root_session_id_walks_compression_chain(tmp_path):
    db = _connect(tmp_path)
    db.create_session("root", "cli")
    db.append_message("root", "user", "first")
    db.try_acquire_compression_lock("root", "holder", ttl_seconds=60)
    db.publish_compression_child(
        parent_session_id="root",
        child_session_id="child",
        source="cli",
        messages=[{"role": "user", "content": "compressed"}],
        compression_lock_holder="holder",
    )
    assert db.get_root_session_id("child") == "root"
    assert db.get_root_session_id("root") == "root"


def test_get_root_session_id_multi_generation(tmp_path):
    db = _connect(tmp_path)
    db.create_session("gen0", "cli")
    db.append_message("gen0", "user", "first")
    db.try_acquire_compression_lock("gen0", "holder", ttl_seconds=60)
    db.publish_compression_child(
        parent_session_id="gen0",
        child_session_id="gen1",
        source="cli",
        messages=[{"role": "user", "content": "gen1"}],
        compression_lock_holder="holder",
    )
    db.append_message("gen1", "user", "second")
    db.try_acquire_compression_lock("gen1", "holder", ttl_seconds=60)
    db.publish_compression_child(
        parent_session_id="gen1",
        child_session_id="gen2",
        source="cli",
        messages=[{"role": "user", "content": "gen2"}],
        compression_lock_holder="holder",
    )
    assert db.get_root_session_id("gen2") == "gen0"
    assert db.get_root_session_id("gen1") == "gen0"
    assert db.get_root_session_id("gen0") == "gen0"


def test_get_root_session_id_rejects_branch_children(tmp_path):
    db = _connect(tmp_path)
    db.create_session("root", "cli")
    db.append_message("root", "user", "first")
    db.try_acquire_compression_lock("root", "holder", ttl_seconds=60)
    db.publish_compression_child(
        parent_session_id="root",
        child_session_id="child",
        source="cli",
        messages=[{"role": "user", "content": "compressed"}],
        compression_lock_holder="holder",
    )
    # Manually mark child as branched to test branch rejection
    with db._lock:
        db._conn.execute(
            "UPDATE sessions SET model_config = json_set(COALESCE(model_config, '{}'), '$._branched_from', 'true') WHERE id = ?",
            ("child",),
        )
    assert db.get_root_session_id("child") == "child"


def test_get_root_session_id_rejects_missing_session(tmp_path):
    db = _connect(tmp_path)
    with pytest.raises(RuntimeError, match="session not found"):
        db.get_root_session_id("nonexistent")


def test_get_root_session_id_rejects_cycle(tmp_path):
    db = _connect(tmp_path)
    db.create_session("a", "cli")
    db.create_session("b", "cli", parent_session_id="a")
    # Create cycle: a -> b -> a
    with db._lock:
        db._conn.execute(
            "UPDATE sessions SET parent_session_id = 'b' WHERE id = 'a'"
        )
    with pytest.raises(RuntimeError, match="compression lineage cycle"):
        db.get_root_session_id("b")


def test_get_root_session_id_rejects_empty_string(tmp_path):
    db = _connect(tmp_path)
    with pytest.raises(ValueError, match="session_id must not be empty"):
        db.get_root_session_id("")


def test_get_root_session_id_rejects_missing_parent(tmp_path, monkeypatch):
    db = _connect(tmp_path)
    db.create_session("parent", "cli")
    db.create_session("child", "cli", parent_session_id="parent")
    get_session = db.get_session
    monkeypatch.setattr(
        db,
        "get_session",
        lambda session_id: None if session_id == "parent" else get_session(session_id),
    )
    with pytest.raises(RuntimeError, match="parent session not found"):
        db.get_root_session_id("child")


def test_get_root_session_id_identity_only_no_task_state(tmp_path):
    db = _connect(tmp_path)
    db.create_session("root", "cli")
    # Even if session row had task_id, it must not be read here
    with db._lock:
        db._conn.execute(
            "UPDATE sessions SET task_id = 't_dummy' WHERE id = ?",
            ("root",),
        )
    assert db.get_root_session_id("root") == "root"