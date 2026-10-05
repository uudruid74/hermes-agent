"""Phase B tests (t_cd44bbde): the per-agent live-task cap.

The cap DEFERS a second checked bug for a busy agent — the file stays pending
and checked (never dropped, never mutated) so the next pass picks it up.
Design call (stated in bugtool.py): busy = ANY live task for that agent on the
board, not only daemon-originated ones.
"""
import importlib.util
import json
import subprocess
import threading
import time
from pathlib import Path

import pytest


BUGTOOL_PATH = Path(__file__).parents[2] / "scripts" / "bugtool.py"


def load_bugtool(monkeypatch, tmp_path):
    projects_root = tmp_path / "projects"
    state_root = tmp_path / "state"
    monkeypatch.setenv("BUGTOOL_PROJECTS_ROOT", str(projects_root))
    monkeypatch.setenv("BUGTOOL_STATE_ROOT", str(state_root))
    spec = importlib.util.spec_from_file_location(
        f"bugtool_test_{threading.get_ident()}_{time.monotonic_ns()}", BUGTOOL_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, projects_root, state_root


def checked_bug_text():
    return (
        "---\n"
        'title: "capped bug"\n'
        'type: "bug"\n'
        'status: "pending"\n'
        'assignee: "neo"\n'
        "---\n\n"
        "# Capped bug\n\n"
        "## Description\n\nWork\n\n"
        "## How To Reproduce\n\nRun\n\n"
        "## Actual Behavior\n\nBlocked\n\n"
        "## Assignee\n\nneo\n\n"
        "## Approved to run\n\n- [x] approved by Evan\n\n"
        "## Kanban tasks\n\n\n"
        "## Failure Reports\n\n"
    )


def write_pending_bug(projects_root, slug):
    path = projects_root / "Hermes-Agent" / "bugs" / "pending" / f"2026-10-05-{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(checked_bug_text(), encoding="utf-8")
    return path


def mock_kanban(monkeypatch, bugtool, board=()):
    """Mock `hermes kanban`. `board` is what `kanban list --assignee` reports."""
    created = []

    def run(args, **_kwargs):
        if args[:3] == ["hermes", "kanban", "create"]:
            task_id = f"t_created{len(created) + 1}"
            created.append(task_id)
            return subprocess.CompletedProcess(args, 0, json.dumps({"id": task_id}), "")
        if args[:3] == ["hermes", "kanban", "list"]:
            return subprocess.CompletedProcess(args, 0, json.dumps(list(board)), "")
        if args[:3] == ["hermes", "kanban", "show"]:
            return subprocess.CompletedProcess(args, 0, "Status: running\n", "")
        if args[:3] == ["hermes", "kanban", "comment"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected subprocess: {args}")

    monkeypatch.setattr(bugtool.subprocess, "run", run)
    return created


def test_two_bugs_one_busy_agent_creates_exactly_zero_new_tasks(monkeypatch, tmp_path):
    """Two checked bugs, agent already holds one live task -> exactly ONE task
    exists for the agent (the pre-existing live one); neither bug dispatches."""
    bugtool, projects_root, _state_root = load_bugtool(monkeypatch, tmp_path)
    bug1 = write_pending_bug(projects_root, "capped-one")
    bug2 = write_pending_bug(projects_root, "capped-two")
    live_board = [{"id": "t_live1", "status": "running", "assignee": "neo"}]
    created = mock_kanban(monkeypatch, bugtool, board=live_board)

    with bugtool.locked_root():
        assert bugtool.maybe_dispatch_locked(bug1, bug1.read_text(encoding="utf-8")) is None
        assert bugtool.maybe_dispatch_locked(bug2, bug2.read_text(encoding="utf-8")) is None

    assert created == []  # no NEW task: the agent's one live task is the only one


def test_deferred_bug_stays_pending_and_checked(monkeypatch, tmp_path):
    """The defer path must never mutate the file: still pending, still checked."""
    bugtool, projects_root, _state_root = load_bugtool(monkeypatch, tmp_path)
    bug1 = write_pending_bug(projects_root, "capped-one")
    before = bug1.read_text(encoding="utf-8")
    mock_kanban(monkeypatch, bugtool, board=[{"id": "t_live1", "status": "running"}])

    with bugtool.locked_root():
        assert bugtool.maybe_dispatch_locked(bug1, before) is None

    after = bug1.read_text(encoding="utf-8")
    assert after == before
    assert bugtool.bug_status(after) == "pending"
    assert bugtool.approved_to_run(after)


def test_free_agent_dispatches_proceeds(monkeypatch, tmp_path):
    """A done task is not live: an agent holding only done tasks is not busy."""
    bugtool, projects_root, _state_root = load_bugtool(monkeypatch, tmp_path)
    bug1 = write_pending_bug(projects_root, "capped-one")
    created = mock_kanban(
        monkeypatch, bugtool, board=[{"id": "t_old", "status": "done"}]
    )

    with bugtool.locked_root():
        result = bugtool.maybe_dispatch_locked(bug1, bug1.read_text(encoding="utf-8"))

    assert result == "t_created1"
    assert created == ["t_created1"]


def test_cap_bypassed_on_force_redispatch(monkeypatch, tmp_path):
    """force=True (bugtool redispatch) is the deliberate human path: no cap."""
    bugtool, projects_root, _state_root = load_bugtool(monkeypatch, tmp_path)
    bug1 = write_pending_bug(projects_root, "capped-one")
    created = mock_kanban(monkeypatch, bugtool, board=[{"id": "t_live1", "status": "running"}])

    with bugtool.locked_root():
        result = bugtool.maybe_dispatch_locked(
            bug1, bug1.read_text(encoding="utf-8"), directive="retry", force=True
        )

    assert result == "t_created1"
    assert created == ["t_created1"]


def test_cap_fails_open_when_board_query_fails(monkeypatch, tmp_path):
    """A dead board query logs a loud warning and does NOT wedge dispatch:
    the cap is overload protection, not a correctness gate."""
    bugtool, projects_root, _state_root = load_bugtool(monkeypatch, tmp_path)
    bug1 = write_pending_bug(projects_root, "capped-one")
    created = []

    def run(args, **_kwargs):
        nonlocal created
        if args[:3] == ["hermes", "kanban", "create"]:
            created.append("t_created1")
            return subprocess.CompletedProcess(args, 0, json.dumps({"id": "t_created1"}), "")
        if args[:3] == ["hermes", "kanban", "list"]:
            return subprocess.CompletedProcess(args, 1, "", "board unavailable")
        raise AssertionError(f"unexpected subprocess: {args}")

    monkeypatch.setattr(bugtool.subprocess, "run", run)

    with bugtool.locked_root():
        result = bugtool.maybe_dispatch_locked(bug1, bug1.read_text(encoding="utf-8"))

    assert result == "t_created1"
    assert created == ["t_created1"]


def test_live_task_count_counts_only_live_statuses(monkeypatch, tmp_path):
    """Direct helper check: todo/running/blocked count; done/archived do not."""
    bugtool, _projects_root, _state_root = load_bugtool(monkeypatch, tmp_path)
    board = [
        {"id": "t_a", "status": "todo"},
        {"id": "t_b", "status": "blocked"},
        {"id": "t_c", "status": "done"},
        {"id": "t_d", "status": "manual"},
    ]
    monkeypatch.setattr(
        bugtool.subprocess, "run",
        lambda args, **_kw: subprocess.CompletedProcess(args, 0, json.dumps(board), ""),
    )
    assert bugtool.live_task_count_by_agent("neo") == 2
