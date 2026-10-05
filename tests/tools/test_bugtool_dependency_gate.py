"""Phase A3 tests (t_cd44bbde): the `## Depends on` dispatch gate.

Four required cases plus wiring coverage:
  1. unresolved dependency (still in pending/) blocks dispatch
  2. resolved dependency (moved to resolved/) allows dispatch
  3. dependency slug matching no bug file = hard refusal (SystemExit), distinct message
  4. dependency cycle A->B->A dies loudly (SystemExit), never loops
  5. maybe_dispatch_locked wiring: prints "blocked by <slug>" and returns None

Every test creates the dependency file via write_pending_bug() so fixtures
match real bug-filename layout (YYYY-MM-DD-<slug>.md under bugs/pending/).
"""
import argparse
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


def complete_bug_text(approved=True, depends=None):
    checkbox = "- [x] approved by Evan" if approved else "- [ ] approved by Evan"
    depends_section = f"## Depends on\n\n{depends}\n\n" if depends else ""
    return (
        "# Dependent bug\n\n"
        "## Description\n\nBlocked work\n\n"
        "## How To Reproduce\n\nRun reconciliation\n\n"
        "## Actual Behavior\n\nBlocked\n\n"
        "## Assignee\n\nneo\n\n"
        f"## Approved to run\n\n{checkbox}\n\n"
        f"{depends_section}"
        "## Kanban tasks\n\n\n"
        "## Failure Reports\n\n"
    )


def write_pending_bug(projects_root, slug, text=None):
    path = projects_root / "Hermes-Agent" / "bugs" / "pending" / f"2026-10-04-{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text or complete_bug_text(), encoding="utf-8")
    return path


def mock_kanban(monkeypatch, bugtool, live_task_ids=()):
    """Mock `hermes kanban` so create succeeds and listed tasks stay live."""
    created = []

    def run(args, **_kwargs):
        if args[:3] == ["hermes", "kanban", "create"]:
            task_id = f"t_created{len(created) + 1}"
            created.append(task_id)
            return subprocess.CompletedProcess(args, 0, json.dumps({"id": task_id}), "")
        if args[:3] == ["hermes", "kanban", "show"]:
            shown = next(iter(live_task_ids), "t_none")
            return subprocess.CompletedProcess(args, 0, f"Status: running\nTask: {shown}\n", "")
        if args[:3] == ["hermes", "kanban", "comment"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        # Per-agent cap board query (Phase B): empty board = agent not busy.
        if args[:3] == ["hermes", "kanban", "list"]:
            return subprocess.CompletedProcess(args, 0, "[]", "")
        raise AssertionError(f"unexpected subprocess: {args}")

    monkeypatch.setattr(bugtool.subprocess, "run", run)
    return created


def test_unresolved_dependency_blocks_dispatch(monkeypatch, tmp_path):
    """Dependency still in pending/ -> no dispatch, gate reports the slug."""
    bugtool, projects_root, _state_root = load_bugtool(monkeypatch, tmp_path)
    write_pending_bug(projects_root, "firstbug")
    dependent = write_pending_bug(
        projects_root, "secondbug", complete_bug_text(depends="firstbug")
    )
    created = mock_kanban(monkeypatch, bugtool)

    assert bugtool.dependency_gate(dependent.read_text(encoding="utf-8")) == ["firstbug"]

    with bugtool.locked_root():
        result = bugtool.maybe_dispatch_locked(
            dependent, dependent.read_text(encoding="utf-8")
        )
    assert result is None
    assert created == []  # no task was ever created


def test_resolved_dependency_allows_dispatch(monkeypatch, tmp_path):
    """Dependency moved to resolved/ -> gate passes, dispatch proceeds."""
    bugtool, projects_root, _state_root = load_bugtool(monkeypatch, tmp_path)
    resolved_path = (
        projects_root / "Hermes-Agent" / "bugs" / "resolved" / "2026-10-04-firstbug.md"
    )
    resolved_path.parent.mkdir(parents=True)
    resolved_path.write_text(complete_bug_text(), encoding="utf-8")
    dependent = write_pending_bug(
        projects_root, "secondbug", complete_bug_text(depends="firstbug")
    )
    created = mock_kanban(monkeypatch, bugtool)

    assert bugtool.dependency_gate(dependent.read_text(encoding="utf-8")) == []

    with bugtool.locked_root():
        result = bugtool.maybe_dispatch_locked(
            dependent, dependent.read_text(encoding="utf-8")
        )
    assert result == "t_created1"
    assert created == ["t_created1"]


def test_missing_dependency_slug_refuses(monkeypatch, tmp_path):
    """A slug matching NO bug file is a hard refusal with a distinct message."""
    bugtool, projects_root, _state_root = load_bugtool(monkeypatch, tmp_path)
    dependent = write_pending_bug(
        projects_root, "secondbug", complete_bug_text(depends="typo-slug")
    )
    mock_kanban(monkeypatch, bugtool)

    with pytest.raises(SystemExit, match="does not match any bug file"):
        bugtool.dependency_gate(dependent.read_text(encoding="utf-8"))

    with bugtool.locked_root(), pytest.raises(SystemExit, match="does not match any bug file"):
        bugtool.maybe_dispatch_locked(dependent, dependent.read_text(encoding="utf-8"))


def test_dependency_cycle_aborts_loudly(monkeypatch, tmp_path):
    """A->B->A cycle dies with SystemExit naming the cycle; never loops."""
    bugtool, projects_root, _state_root = load_bugtool(monkeypatch, tmp_path)
    bug_a = write_pending_bug(
        projects_root, "aaa", complete_bug_text(depends="bbb")
    )
    write_pending_bug(projects_root, "bbb", complete_bug_text(depends="aaa"))
    mock_kanban(monkeypatch, bugtool)

    with pytest.raises(SystemExit, match="dependency cycle detected: aaa -> bbb -> aaa"):
        bugtool.dependency_gate(bug_a.read_text(encoding="utf-8"))


def test_maybe_dispatch_blocked_message(monkeypatch, tmp_path, capsys):
    """Wiring: the blocked path prints the exact 'blocked by <slug>' line."""
    bugtool, projects_root, _state_root = load_bugtool(monkeypatch, tmp_path)
    write_pending_bug(projects_root, "firstbug")
    dependent = write_pending_bug(
        projects_root, "secondbug", complete_bug_text(depends="firstbug")
    )
    mock_kanban(monkeypatch, bugtool)

    with bugtool.locked_root():
        result = bugtool.maybe_dispatch_locked(
            dependent, dependent.read_text(encoding="utf-8")
        )
    out = capsys.readouterr().out
    assert result is None
    assert "nothing to dispatch for 2026-10-04-secondbug.md: blocked by firstbug" in out


def test_depends_section_alias_resolves(monkeypatch, tmp_path):
    """`bugtool set depends-on ...` normalizes to the canonical heading."""
    bugtool, _projects_root, _state_root = load_bugtool(monkeypatch, tmp_path)
    assert bugtool.section_name("depends-on") == bugtool.DEPENDS_SECTION
    assert bugtool.section_name("depends on") == bugtool.DEPENDS_SECTION


def test_date_prefixed_dependency_slug_is_normalized(monkeypatch, tmp_path):
    """A full filename in Depends on (2026-10-04-firstbug) works like the slug."""
    bugtool, projects_root, _state_root = load_bugtool(monkeypatch, tmp_path)
    write_pending_bug(projects_root, "firstbug")
    dependent = write_pending_bug(
        projects_root, "secondbug", complete_bug_text(depends="2026-10-04-firstbug")
    )
    mock_kanban(monkeypatch, bugtool)

    assert bugtool.dependency_gate(dependent.read_text(encoding="utf-8")) == ["firstbug"]
