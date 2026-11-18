"""Regression tests for the run-level duplicate backstop (Evan 2026-11-18).

The dispatcher must refuse a second live task per assignee: at claim time,
while another run of the same profile is ``running``, the new task is
deferred — commented + HOME / Wintermute notified on first deferral — and
left ``ready`` for a later tick. An explicit ``max_in_progress_per_profile``
cap opts into fan-out and replaces this single-flight default.

Incident: on 2026-11-18 two Neo workers ran simultaneously on the same
hermes-agent tree (t_c7840ef7 + t_e10339f1) — colliding edits, 1h25m run
with zero commits. Detection happened only because a human noticed.
"""
from __future__ import annotations

import os
import sys
import tempfile

from tests.hermes_cli.conftest import (
    _purge_hermes_modules,
    _restore_hermes_modules,
)

import pytest


@pytest.fixture()
def isolated_kanban_home_with_profiles(monkeypatch):
    """Spin up a fresh HERMES_HOME with kanban DB + alpha/beta profiles."""
    test_home = tempfile.mkdtemp(prefix="kanban_defer_same_assignee_test_")
    for prof in ("alpha", "beta", "default"):
        os.makedirs(os.path.join(test_home, "profiles", prof), exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", test_home)
    saved_modules = _purge_hermes_modules(
        "hermes_cli", "hermes_state", "hermes_constants"
    )
    try:
        from hermes_cli import kanban_db

        yield kanban_db
    finally:
        _restore_hermes_modules(
            saved_modules, "hermes_cli", "hermes_state", "hermes_constants"
        )


def _fake_spawn(*args, **kwargs):
    return 12345


@pytest.fixture()
def notify_recorder(monkeypatch, isolated_kanban_home_with_profiles):
    """Replace the standing-notify flow with a recorder for assertions."""
    kb = isolated_kanban_home_with_profiles
    calls = []

    def _record(task_id, assignee, blocker_id):
        calls.append((task_id, assignee, blocker_id))

    monkeypatch.setattr(kb, "_notify_deferred_same_assignee", _record)
    return calls


def _review_status(conn, task_id):
    with conn:
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,))
        conn.execute("UPDATE tasks SET claim_lock = NULL WHERE id = ?", (task_id,))


def _complete(conn, task_id):
    with conn:
        conn.execute(
            "UPDATE tasks SET status = 'done', claim_lock = NULL WHERE id = ?",
            (task_id,),
        )


def test_refuses_second_live_run_per_assignee(
    isolated_kanban_home_with_profiles, notify_recorder
):
    """Two ready tasks for the same profile must not both spawn: only the
    first is claimed; the rest are deferred with comment + event + notify."""
    kb = isolated_kanban_home_with_profiles
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        alpha_ids = [kb.create_task(conn, title=f"a{i}", assignee="alpha") for i in range(3)]
        beta_ids = [kb.create_task(conn, title=f"b{i}", assignee="beta") for i in range(2)]

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)

    # One alpha + one beta spawned; the other ready tasks deferred: two
    # alpha (blocker = spawned alpha) and one beta (blocker = spawned beta).
    assert len(res.spawned) == 2
    spawn_assignees = [s[1] for s in res.spawned]
    assert spawn_assignees.count("alpha") == 1
    assert spawn_assignees.count("beta") == 1

    deferred = res.skipped_same_assignee
    assert len(deferred) == 3
    spawned_alpha = [s[0] for s in res.spawned if s[1] == "alpha"][0]
    spawned_beta = [s[0] for s in res.spawned if s[1] == "beta"][0]
    alpha_deferred = [d for d in deferred if d[1] == "alpha"]
    beta_deferred = [d for d in deferred if d[1] == "beta"]
    assert len(alpha_deferred) == 2
    assert len(beta_deferred) == 1
    assert all(d[0] in alpha_ids for d in alpha_deferred)
    assert all(d[2] == spawned_alpha for d in alpha_deferred)
    assert beta_deferred[0][0] in beta_ids
    assert beta_deferred[0][2] == spawned_beta

    # Standing flow: comment on each deferred task + event recorded.
    with kb.connect_closing() as conn:
        for task_id, assignee, blocker in deferred:
            comments = kb.list_comments(conn, task_id)
            assert any("Deferred at claim time" in c.body for c in comments)
            ev = conn.execute(
                "SELECT kind, payload FROM task_events "
                "WHERE task_id = ? AND kind = 'deferred_same_assignee'",
                (task_id,),
            ).fetchone()
            assert ev is not None
            assert blocker in ev["payload"]

    # Notify fired once per deferred task (first episode).
    assert len(notify_recorder) == 3
    assert all(c[1] in ("alpha", "beta") for c in notify_recorder)
    assert all(c[2] in (spawned_alpha, spawned_beta) for c in notify_recorder)


def test_dedup_no_repeat_notify_while_blocker_persists(
    isolated_kanban_home_with_profiles, notify_recorder
):
    """While the blocker keeps running, later ticks stay deferred but must NOT
    re-comment / re-notify every tick. Once the blocker completes, the
    remaining task is dispatched and no further notify fires."""
    kb = isolated_kanban_home_with_profiles
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        kb.create_task(conn, title="a0", assignee="alpha")
        second = kb.create_task(conn, title="a1", assignee="alpha")

    with kb.connect_closing() as conn:
        res1 = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    assert len(res1.spawned) == 1
    assert len(res1.skipped_same_assignee) == 1
    blocker = res1.spawned[0][0]
    assert len(notify_recorder) == 1

    # Second tick while the blocker is still running: deferred again, but no
    # new comment / notify (same episode).
    with kb.connect_closing() as conn:
        res2 = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    assert len(res2.spawned) == 0
    assert len(res2.skipped_same_assignee) == 1
    assert len(notify_recorder) == 1
    with kb.connect_closing() as conn:
        comments = kb.list_comments(conn, second)
    assert len(comments) == 1  # only the first-episode comment

    # Blocker completes → next tick dispatches the deferred task; no notify
    # for it (the deferral episode ended without a new one).
    with kb.connect_closing() as conn:
        _complete(conn, blocker)
    with kb.connect_closing() as conn:
        res3 = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    assert len(res3.spawned) == 1
    assert res3.spawned[0][0] == second
    assert len(notify_recorder) == 1


def test_explicit_cap_preserves_fanout(isolated_kanban_home_with_profiles, notify_recorder):
    """An explicit max_in_progress_per_profile opts into fan-out: the guard
    is bypassed and the existing cap bucket governs (no regressions for
    #21582)."""
    kb = isolated_kanban_home_with_profiles
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        for i in range(3):
            kb.create_task(conn, title=f"a{i}", assignee="alpha")

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn,
            spawn_fn=_fake_spawn,
            dry_run=False,
            max_in_progress_per_profile=2,
        )
    assert len(res.spawned) == 2
    assert len(res.skipped_per_profile_capped) == 1
    assert len(res.skipped_same_assignee) == 0
    assert notify_recorder == []


def test_dry_run_reports_deferral_without_mutations(
    isolated_kanban_home_with_profiles, notify_recorder
):
    """dry_run surfaces the would-be deferral but writes nothing and spawns
    nothing."""
    kb = isolated_kanban_home_with_profiles
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        alpha_ids = [kb.create_task(conn, title=f"a{i}", assignee="alpha") for i in range(2)]

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=True)

    assert len(res.skipped_same_assignee) == 1
    assert res.skipped_same_assignee[0][0] in alpha_ids
    assert notify_recorder == []
    with kb.connect_closing() as conn:
        assert kb.list_comments(conn, alpha_ids[1]) == []
        ev = conn.execute(
            "SELECT 1 FROM task_events WHERE kind = 'deferred_same_assignee'"
        ).fetchone()
        assert ev is None
        # All tasks still ready — nothing claimed in dry run.
        ready = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = 'ready'"
        ).fetchone()[0]
        assert ready == 2


def test_review_column_deferred_too(isolated_kanban_home_with_profiles, notify_recorder):
    """A review agent is also a live run of the assignee: it is refused while
    another run of the same profile is in flight."""
    kb = isolated_kanban_home_with_profiles
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        kb.create_task(conn, title="running", assignee="alpha")
        review_id = kb.create_task(conn, title="to-review", assignee="alpha")
        _review_status(conn, review_id)

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)

    assert len(res.spawned) == 1  # only the running-column task
    assert any(d[0] == review_id for d in res.skipped_same_assignee)
    assert len(notify_recorder) == 1
    # The review task is untouched — still in review, unclaimed.
    with kb.connect_closing() as conn:
        row = conn.execute(
            "SELECT status, claim_lock FROM tasks WHERE id = ?", (review_id,)
        ).fetchone()
        assert row["status"] == "review"
        assert row["claim_lock"] is None
