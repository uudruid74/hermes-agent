"""Per-assignee one-at-a-time gate + FIFO drain (Evan 2026-11-17).

The checkbox flow never fans out WITHIN an assignee: at most one live bug
task per worker, so the same assignee drains checked bugs one at a time in
FIFO order (the sweep scans oldest-first) while different assignees run in
parallel. The gate DEFERS (never drops) — the bug file stays pending+checked
so the next pass picks it up when the slot frees. ``force=True`` (bugtool
redispatch) bypasses.
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


def checked_bug_text(assignee="neo"):
    return (
        "---\n"
        'title: "queued bug"\n'
        'type: "bug"\n'
        'status: "pending"\n'
        f'assignee: "{assignee}"\n'
        "---\n\n"
        "# Queued bug\n\n"
        "## Description\n\nWork\n\n"
        "## How To Reproduce\n\nRun\n\n"
        "## Actual Behavior\n\nBroke\n\n"
        f"## Assignee\n\n{assignee}\n\n"
        "## Approved to run\n\n- [x] approved by Evan\n\n"
        "## Kanban tasks\n\n\n"
        "## Failure Reports\n\n"
    )


def write_pending_bug(projects_root, slug, assignee="neo", date="2026-11-17"):
    path = projects_root / "Hermes-Agent" / "bugs" / "pending" / f"{date}-{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(checked_bug_text(assignee), encoding="utf-8")
    return path


def mock_kanban(monkeypatch, bugtool, board=(), fail_list=False):
    """Mock `hermes kanban`. `board` is the live task list; creates append to it
    so the gate sees the dispatch it just made (FIFO drain simulation)."""
    created = []
    served = [dict(record) for record in board]

    def run(args, **_kwargs):
        if args[:3] == ["hermes", "kanban", "create"]:
            task_id = f"t_created{len(created) + 1}"
            created.append(task_id)
            assignee = args[args.index("--assignee") + 1] if "--assignee" in args else ""
            served.append(
                {"id": task_id, "title": "BUG: (mock)", "status": "ready", "assignee": assignee}
            )
            return subprocess.CompletedProcess(args, 0, json.dumps({"id": task_id}), "")
        if args[:3] == ["hermes", "kanban", "list"]:
            if fail_list:
                return subprocess.CompletedProcess(args, 1, "", "board query down")
            return subprocess.CompletedProcess(args, 0, json.dumps(served), "")
        if args[:3] == ["hermes", "kanban", "show"]:
            return subprocess.CompletedProcess(args, 0, "Status: running\n", "")
        if args[:3] == ["hermes", "kanban", "comment"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected subprocess: {args}")

    monkeypatch.setattr(bugtool.subprocess, "run", run)
    return created


def bug_task(task_id, title, status, assignee="neo"):
    return {"id": task_id, "title": title, "status": status, "assignee": assignee}


def test_same_assignee_defers_while_bug_task_live(monkeypatch, tmp_path):
    bugtool, projects_root, _ = load_bugtool(monkeypatch, tmp_path)
    created = mock_kanban(
        monkeypatch, bugtool,
        board=[bug_task("t_live1", "BUG: already-running", "running", "neo")],
    )
    path = write_pending_bug(projects_root, "second")
    text = path.read_text(encoding="utf-8")
    assert bugtool.maybe_dispatch_locked(path, text) is None
    assert created == []
    # defer, never drop: the file stays pending + checked
    after = path.read_text(encoding="utf-8")
    assert 'status: "pending"' in after
    assert "- [x] approved by Evan" in after


def test_different_assignee_runs_in_parallel(monkeypatch, tmp_path):
    """Evan 2026-11-17: the gate is per assignee — zephyr's free slot must not
    be held hostage by neo's live bug task."""
    bugtool, projects_root, _ = load_bugtool(monkeypatch, tmp_path)
    created = mock_kanban(
        monkeypatch, bugtool,
        board=[bug_task("t_live1", "BUG: neo-busy", "running", "neo")],
    )
    path = write_pending_bug(projects_root, "parallel", assignee="zephyr")
    assert bugtool.maybe_dispatch_locked(path, path.read_text(encoding="utf-8")) == "t_created1"
    assert created == ["t_created1"]


def test_same_assignee_fifo_drains_one_per_slot(monkeypatch, tmp_path):
    """When a slot is full, the next checked bug waits; when it frees, the
    oldest checked bug goes (FIFO = the sweep's oldest-first scan order)."""
    bugtool, projects_root, _ = load_bugtool(monkeypatch, tmp_path)
    created = mock_kanban(monkeypatch, bugtool, board=[])
    old = write_pending_bug(projects_root, "fifo-old", date="2026-11-01")
    new = write_pending_bug(projects_root, "fifo-new", date="2026-11-17")
    # sweep order = oldest first (all_bug_files is date-sorted)
    assert bugtool.maybe_dispatch_locked(old, old.read_text(encoding="utf-8")) == "t_created1"
    # slot now full (the mock list reflects the create) -> newer one defers
    assert bugtool.maybe_dispatch_locked(new, new.read_text(encoding="utf-8")) is None
    assert created == ["t_created1"]


@pytest.mark.parametrize("status", ["todo", "ready", "running", "scheduled"])
def test_cap_count_statuses_hold_the_slot(monkeypatch, tmp_path, status):
    bugtool, projects_root, _ = load_bugtool(monkeypatch, tmp_path)
    created = mock_kanban(
        monkeypatch, bugtool,
        board=[bug_task("t_live1", "BUG: occupied", status, "neo")],
    )
    path = write_pending_bug(projects_root, f"hold-{status}")
    assert bugtool.maybe_dispatch_locked(path, path.read_text(encoding="utf-8")) is None
    assert created == []


@pytest.mark.parametrize("status", ["blocked", "done", "archived"])
def test_blocked_and_finished_dont_hold_the_slot(monkeypatch, tmp_path, status):
    bugtool, projects_root, _ = load_bugtool(monkeypatch, tmp_path)
    created = mock_kanban(
        monkeypatch, bugtool,
        board=[bug_task("t_live1", "BUG: occupied", status, "neo")],
    )
    path = write_pending_bug(projects_root, f"free-{status}")
    assert bugtool.maybe_dispatch_locked(path, path.read_text(encoding="utf-8")) == "t_created1"
    assert created == ["t_created1"]


def test_non_bug_tasks_dont_hold_the_slot(monkeypatch, tmp_path):
    """Unrelated board work (suite cards, project work) is not the bug queue."""
    bugtool, projects_root, _ = load_bugtool(monkeypatch, tmp_path)
    created = mock_kanban(
        monkeypatch, bugtool,
        board=[
            bug_task("t_other1", "DISPATCH SUITE: cells 9-L1", "running", "neo"),
            bug_task("t_other2", "Eddon Alpha-1 · Stage 12", "ready", "neo"),
        ],
    )
    path = write_pending_bug(projects_root, "unrelated")
    assert bugtool.maybe_dispatch_locked(path, path.read_text(encoding="utf-8")) == "t_created1"
    assert created == ["t_created1"]


def test_force_bypasses_the_gate(monkeypatch, tmp_path):
    bugtool, projects_root, _ = load_bugtool(monkeypatch, tmp_path)
    created = mock_kanban(
        monkeypatch, bugtool,
        board=[bug_task("t_live1", "BUG: already-running", "running", "neo")],
    )
    path = write_pending_bug(projects_root, "forced")
    result = bugtool.maybe_dispatch_locked(
        path, path.read_text(encoding="utf-8"), force=True
    )
    assert result == "t_created1"
    assert created == ["t_created1"]


def test_board_query_failure_fails_open_loudly(monkeypatch, tmp_path, capsys):
    bugtool, projects_root, _ = load_bugtool(monkeypatch, tmp_path)
    created = mock_kanban(monkeypatch, bugtool, fail_list=True)
    path = write_pending_bug(projects_root, "query-down")
    assert bugtool.maybe_dispatch_locked(path, path.read_text(encoding="utf-8")) == "t_created1"
    assert created == ["t_created1"]
    err = capsys.readouterr().err
    assert "one-at-a-time gate unavailable" in err
