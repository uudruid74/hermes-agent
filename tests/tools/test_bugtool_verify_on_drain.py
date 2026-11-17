"""Queue-drain verification (`bugtool verify`, Evan 2026-11-17).

Fast lane keeps the queue moving (graph + review + targeted tests); the slow
lane validates a night of changes ONCE when the hermes-agent bug queue drains.
RED auto-files a suite-red bug for Neo through the NORMAL gates — one-at-a-time
still applies, the file records Evan's standing order as approval provenance.
Artifact law: over-budget tmp growth is RED with offenders named ("We should
not be chewing 1.7G of data, ever").
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
BUGTOOLD_PATH = Path(__file__).parents[2] / "scripts" / "bugtoold.py"


def load_module(path, monkeypatch, tmp_path):
    monkeypatch.setenv("BUGTOOL_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("BUGTOOL_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("BUGTOOL_VERIFY_TMPDIR", str(tmp_path / "verify-tmp"))
    spec = importlib.util.spec_from_file_location(
        f"bugtool_verify_{threading.get_ident()}_{time.monotonic_ns()}", path
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def checked_bug_text(slug="preexisting"):
    return (
        "---\n"
        f'title: "{slug}"\n'
        'type: "bug"\n'
        'status: "pending"\n'
        'assignee: "neo"\n'
        "---\n\n"
        f"# {slug}\n\n"
        "## Description\n\nWork\n\n"
        "## How To Reproduce\n\nRun\n\n"
        "## Actual Behavior\n\nBroken\n\n"
        "## Assignee\n\nneo\n\n"
        "## Approved to run\n\n- [x] approved by Evan\n\n"
        "## Kanban tasks\n\n\n"
        "## Failure Reports\n\n"
    )


def setup_project(bugtool, projects_root, slug=None, checked=True):
    pending = projects_root / "hermes-agent" / "bugs" / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    if slug:
        text = checked_bug_text(slug)
        if not checked:
            text = text.replace("- [x] approved by Evan", "- [ ] approved by Evan")
        (pending / f"2026-11-17-{slug}.md").write_text(text, encoding="utf-8")


def fake_subprocess(monkeypatch, module, board=()):
    """Tolerate git/rm plumbing; mock `hermes kanban`."""
    created = []

    def run(args, **_kwargs):
        if args[0] in ("git", "rm"):
            return subprocess.CompletedProcess(args, 0, "", "")
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

    monkeypatch.setattr(module.subprocess, "run", run)
    return created


def red_result(**over):
    result = {
        "ok": False, "returncode": 1, "summary": "2 failed, 10 passed",
        "failures": ["FAILED tests/test_alpha.py::test_one", "FAILED tests/test_beta.py::test_two"],
        "budget_mb": 12, "budget_cap_mb": 512, "offenders": [], "duration_s": 3.0,
    }
    result.update(over)
    return result


def test_queue_drain_semantics(monkeypatch, tmp_path):
    bugtool = load_module(BUGTOOL_PATH, monkeypatch, tmp_path)
    projects_root = tmp_path / "projects"
    # Empty pending dir: drained
    setup_project(bugtool, projects_root)
    assert bugtool.queue_is_drained("hermes-agent") is True
    # A checked, gate-complete bug waiting to go out: NOT drained
    setup_project(bugtool, projects_root, slug="waiting")
    assert bugtool.queue_is_drained("hermes-agent") is False
    # Unchecked (waiting on Evan's tick) does NOT hold the queue
    waiting = projects_root / "hermes-agent" / "bugs" / "pending" / "2026-11-17-waiting.md"
    waiting.write_text(checked_bug_text("waiting").replace("[x]", "[ ]"), encoding="utf-8")
    assert bugtool.queue_is_drained("hermes-agent") is True


def test_verify_green_no_card(monkeypatch, tmp_path):
    bugtool = load_module(BUGTOOL_PATH, monkeypatch, tmp_path)
    projects_root = tmp_path / "projects"
    setup_project(bugtool, projects_root)
    fake_subprocess(monkeypatch, bugtool)
    monkeypatch.setattr(bugtool, "_verify_git_sha", lambda: "abc12345")
    monkeypatch.setattr(
        bugtool, "run_full_suite",
        lambda project, tmpdir: {"ok": True, "returncode": 0, "summary": "10 passed",
                                 "failures": [], "budget_mb": 5, "budget_cap_mb": 512,
                                 "offenders": [], "duration_s": 1.0},
    )
    bugtool.cmd_verify(argparse.Namespace(project="hermes-agent", on_drain=True))
    pending = projects_root / "hermes-agent" / "bugs" / "pending"
    assert not list(pending.glob("*suite-red*")), "green run must not file a card"
    assert bugtool.verify_marker()["result"] == "green"


def test_verify_red_files_checked_neo_card(monkeypatch, tmp_path):
    bugtool = load_module(BUGTOOL_PATH, monkeypatch, tmp_path)
    projects_root = tmp_path / "projects"
    setup_project(bugtool, projects_root)
    created = fake_subprocess(monkeypatch, bugtool)
    monkeypatch.setattr(bugtool, "_verify_git_sha", lambda: "abc12345")
    monkeypatch.setattr(bugtool, "run_full_suite", lambda project, tmpdir: red_result())
    bugtool.cmd_verify(argparse.Namespace(project="hermes-agent", on_drain=True))
    cards = list((projects_root / "hermes-agent" / "bugs" / "pending").glob("*suite-red-abc12345*"))
    assert len(cards) == 1
    text = cards[0].read_text(encoding="utf-8")
    assert 'assignee: "neo"' in text
    # Approval carries Evan's standing order as provenance, box CHECKED
    assert "approved by Evan (auto-filed by `bugtool verify`" in text
    assert "- [x] approved by Evan" in text
    assert "FAILED tests/test_alpha.py::test_one" in text
    # Dispatched through the normal gates
    assert created and created[0].startswith("t_")
    # Idempotent: a second red at the same sha files no duplicate
    monkeypatch.setattr(bugtool, "run_full_suite", lambda project, tmpdir: red_result())
    bugtool.cmd_verify(argparse.Namespace(project="hermes-agent", on_drain=True))
    cards2 = list((projects_root / "hermes-agent" / "bugs" / "pending").glob("*suite-red-abc12345*"))
    assert len(cards2) == 1


def test_over_budget_is_red_with_offenders(monkeypatch, tmp_path):
    bugtool = load_module(BUGTOOL_PATH, monkeypatch, tmp_path)
    projects_root = tmp_path / "projects"
    setup_project(bugtool, projects_root)
    fake_subprocess(monkeypatch, bugtool)
    monkeypatch.setattr(bugtool, "_verify_git_sha", lambda: "def56789")
    monkeypatch.setattr(
        bugtool, "run_full_suite",
        lambda project, tmpdir: red_result(
            ok=False, summary="10 passed", failures=[], returncode=0,
            budget_mb=1799, budget_cap_mb=512,
            offenders=["1500 MB pytest-0/test_compressor_family0"],
        ),
    )
    bugtool.cmd_verify(argparse.Namespace(project="hermes-agent", on_drain=True))
    cards = list((projects_root / "hermes-agent" / "bugs" / "pending").glob("*suite-red-def56789*"))
    assert len(cards) == 1
    text = cards[0].read_text(encoding="utf-8")
    assert "OVER BUDGET" in text
    assert "1500 MB pytest-0/test_compressor_family0" in text


def test_daemon_spawn_gates(monkeypatch, tmp_path):
    bugtoold = load_module(BUGTOOLD_PATH, monkeypatch, tmp_path)
    bugtool = load_module(BUGTOOL_PATH, monkeypatch, tmp_path)
    projects_root = tmp_path / "projects"
    setup_project(bugtool, projects_root)

    class Decisions(list):
        def append(self, action, **fields):  # daemon's log shape
            super().append((action, fields))

    spawned = []
    monkeypatch.setattr(
        bugtoold.subprocess, "Popen",
        lambda args, **kw: spawned.append(args) or subprocess.CompletedProcess(args, 0),
    )
    # Drained + idle: spawns the detached verify
    bugtoold.maybe_spawn_verify(bugtool, projects_root, Decisions(), dry_run=False)
    assert spawned and spawned[0][-2:] == ["verify", "--on-drain"]
    # Not drained: no spawn
    spawned.clear()
    setup_project(bugtool, projects_root, slug="waiting")
    bugtoold.maybe_spawn_verify(bugtool, projects_root, Decisions(), dry_run=False)
    assert not spawned
    # Dry-run: logged, never spawned
    waiting = projects_root / "hermes-agent" / "bugs" / "pending" / "2026-11-17-waiting.md"
    waiting.unlink()
    decisions = Decisions()
    bugtoold.maybe_spawn_verify(bugtool, projects_root, decisions, dry_run=True)
    assert not spawned
    assert any(a == "verify" for a, _ in decisions)
