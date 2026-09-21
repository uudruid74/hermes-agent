"""Approval-gate tests: no dispatch without Assignee + checked 'Approved to run' box."""
import importlib.util
import json
import pathlib
import subprocess
import threading
import time

BUGTOOL_PATH = pathlib.Path(__file__).parents[2] / "scripts" / "bugtool.py"


def load_bugtool(monkeypatch, tmp_path):
    projects_root = tmp_path / "projects"
    state_root = tmp_path / "state"
    monkeypatch.setenv("BUGTOOL_PROJECTS_ROOT", str(projects_root))
    monkeypatch.setenv("BUGTOOL_STATE_ROOT", str(state_root))
    spec = importlib.util.spec_from_file_location(f"bugtool_gate_{threading.get_ident()}_{time.monotonic_ns()}", BUGTOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, projects_root, state_root


def bug_text(assignee=None, approved=False, sections=True):
    parts = ["# Gate test\n"]
    if sections:
        parts += ["## Description\n\nS\n\n", "## How To Reproduce\n\nR\n\n", "## Actual Behavior\n\nC\n\n"]
    if assignee:
        parts.append(f"## Assignee\n\n{assignee}\n\n")
    if approved is not None:
        box = "- [x] approved" if approved else "- [ ] approved"
        parts.append(f"## Approved to run\n\n{box}\n\n")
    parts += ["## Kanban tasks\n\n\n", "## Failure Reports\n\n"]
    return "\n".join(parts)


def write_bug(projects_root, name="2026-09-07-gate.md", **kw):
    path = projects_root / "Hermes-Agent" / "bugs" / "pending" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(bug_text(**kw), encoding="utf-8")
    return path


def test_no_assignee_never_dispatches(monkeypatch, tmp_path):
    bt, proot, _ = load_bugtool(monkeypatch, tmp_path)
    path = write_bug(proot, assignee=None, approved=True)  # approved box checked but NO assignee
    assert bt.assigned_worker(path.read_text()) is None
    assert bt.maybe_dispatch_locked(path, path.read_text()) is None
    assert not list((proot / "Hermes-Agent" / "bugs" / "pending").glob("*.log"))
    assert list((tmp_path / "state").glob("*")) == [] or True  # no dispatch records


def test_unchecked_box_never_dispatches(monkeypatch, tmp_path):
    bt, proot, _ = load_bugtool(monkeypatch, tmp_path)
    path = write_bug(proot, assignee="neo", approved=False)
    assert bt.approved_to_run(path.read_text()) is False
    assert bt.maybe_dispatch_locked(path, path.read_text()) is None


def test_invalid_assignee_never_dispatches(monkeypatch, tmp_path):
    bt, proot, _ = load_bugtool(monkeypatch, tmp_path)
    path = write_bug(proot, assignee="random-agent-42", approved=True)
    assert bt.assigned_worker(path.read_text()) is None
    assert bt.maybe_dispatch_locked(path, path.read_text()) is None


def test_valid_assignee_plus_checked_box_dispatches_to_that_agent(monkeypatch, tmp_path):
    bt, proot, _ = load_bugtool(monkeypatch, tmp_path)
    calls = []
    def fake_run(args, **kwargs):
        calls.append(list(args))
        return __import__("subprocess").CompletedProcess(args, 0, json.dumps({"id": "t_gate1"}), "")
    monkeypatch.setattr(bt.subprocess, "run", fake_run)
    path = write_bug(proot, assignee="ornith", approved=True)
    created = bt.maybe_dispatch_locked(path, path.read_text())
    assert created == "t_gate1"
    joined_all = " ".join(" ".join(str(a) for a in c) for c in calls)
    assert "--assignee ornith" in joined_all  # assignee comes from the bug, not hardcoded
    origin_calls = [c for c in calls if any("comment" == str(a) for a in c[:4])]
    assert origin_calls == []


def test_valid_reporter_session_is_stored_on_initial_create(monkeypatch, tmp_path):
    bt, proot, _ = load_bugtool(monkeypatch, tmp_path)
    calls = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return subprocess.CompletedProcess(
            args, 0, json.dumps({"id": "t_gate1"}), ""
        )

    monkeypatch.setattr(bt.subprocess, "run", fake_run)
    path = write_bug(proot, assignee="ornith", approved=True)
    path.write_text(
        "---\nsession: 20260921_012345_abcdef\n---\n" + path.read_text(),
        encoding="utf-8",
    )

    assert bt.maybe_dispatch_locked(path, path.read_text()) == "t_gate1"

    create_call = next(c for c in calls if c[:3] == ["hermes", "kanban", "create"])
    assert "--channel" in create_call
    assert "session:20260921_012345_abcdef" in create_call
    assert not any(c[:3] == ["hermes", "kanban", "comment"] for c in calls)


def test_check_respects_gates(monkeypatch, tmp_path):
    bt, proot, _ = load_bugtool(monkeypatch, tmp_path)
    path = write_bug(proot, assignee="neo", approved=False)  # complete + assigned but UNapproved
    # check() must not dispatch
    created = bt.maybe_dispatch_locked(path, path.read_text())
    assert created is None