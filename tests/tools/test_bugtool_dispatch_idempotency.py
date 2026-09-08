import argparse
import importlib.util
import json
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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


def complete_bug_text(approved=True):
    checkbox = "- [x] approved by Evan" if approved else "- [ ] approved by Evan"
    return (
        "# Repeated dispatch\n\n"
        "## Symptom\n\nDuplicates\n\n"
        "## Repro\n\nRun reconciliation\n\n"
        "## Suspected cause\n\nMissing durable state\n\n"
        "## Assignee\n\nneo\n\n"
        f"## Approved to run\n\n{checkbox}\n\n"
        "## Kanban tasks\n\n\n"
        "## Failure Reports\n\n"
    )


def write_pending_bug(projects_root, text=None):
    path = projects_root / "Hermes-Agent" / "bugs" / "pending" / "2026-09-07-loop.md"
    path.parent.mkdir(parents=True)
    path.write_text(text or complete_bug_text(), encoding="utf-8")
    return path


def mock_kanban(monkeypatch, bugtool, create_delay=0.0):
    created = []

    def run(args, **_kwargs):
        if args[:3] == ["hermes", "kanban", "create"]:
            if create_delay:
                time.sleep(create_delay)
            task_id = f"t_created{len(created) + 1}"
            created.append(task_id)
            return subprocess.CompletedProcess(args, 0, json.dumps({"id": task_id}), "")
        if args[:3] == ["hermes", "kanban", "show"]:
            return subprocess.CompletedProcess(args, 0, "Status: ready\n", "")
        raise AssertionError(f"unexpected subprocess: {args}")

    monkeypatch.setattr(bugtool.subprocess, "run", run)
    return created


def test_writeback_failure_records_task_and_prevents_duplicate(monkeypatch, tmp_path):
    bugtool, projects_root, state_root = load_bugtool(monkeypatch, tmp_path)
    path = write_pending_bug(projects_root)
    created = mock_kanban(monkeypatch, bugtool)
    real_replace = bugtool.os.replace

    def fail_bug_replace(source, destination):
        if Path(destination) == path:
            raise OSError("simulated markdown write failure")
        return real_replace(source, destination)

    monkeypatch.setattr(bugtool.os, "replace", fail_bug_replace)

    with pytest.raises(OSError, match="simulated markdown write failure"):
        with bugtool.locked_root():
            bugtool.maybe_dispatch_locked(path, path.read_text(encoding="utf-8"))

    with bugtool.locked_root():
        assert bugtool.maybe_dispatch_locked(path, path.read_text(encoding="utf-8")) is None

    assert created == ["t_created1"]
    records = [
        json.loads(line)
        for line in (state_root / "bugs-dispatched.log").read_text(encoding="utf-8").splitlines()
    ]
    assert records[-1]["status"] == "created"
    assert records[-1]["task_id"] == "t_created1"
    assert records[-1]["timestamp"].endswith("Z")


def test_ambiguous_create_failure_is_not_retried(monkeypatch, tmp_path):
    bugtool, projects_root, state_root = load_bugtool(monkeypatch, tmp_path)
    path = write_pending_bug(projects_root)
    create_calls = []

    def run(args, **_kwargs):
        if args[:3] == ["hermes", "kanban", "create"]:
            create_calls.append(args)
            return subprocess.CompletedProcess(args, 1, "", "connection lost after request")
        raise AssertionError(f"unexpected subprocess: {args}")

    monkeypatch.setattr(bugtool.subprocess, "run", run)

    for _ in range(2):
        with bugtool.locked_root():
            assert bugtool.maybe_dispatch_locked(path, path.read_text(encoding="utf-8")) is None

    assert len(create_calls) == 1
    records = [
        json.loads(line)
        for line in (state_root / "bugs-dispatched.log").read_text(encoding="utf-8").splitlines()
    ]
    assert [record["status"] for record in records] == ["reserved", "failed"]


def test_create_task_uses_json_id_instead_of_body_task_id(monkeypatch, tmp_path):
    bugtool, projects_root, state_root = load_bugtool(monkeypatch, tmp_path)
    path = write_pending_bug(projects_root)

    def run(args, **_kwargs):
        if args[:3] == ["hermes", "kanban", "create"]:
            stdout = json.dumps({"body": "previous task t_old", "id": "t_created1"})
            return subprocess.CompletedProcess(args, 0, stdout, "")
        raise AssertionError(f"unexpected subprocess: {args}")

    monkeypatch.setattr(bugtool.subprocess, "run", run)

    with bugtool.locked_root():
        created = bugtool.maybe_dispatch_locked(path, path.read_text(encoding="utf-8"))

    assert created == "t_created1"
    assert bugtool.task_ids(path.read_text(encoding="utf-8")) == ["t_created1"]
    records = [
        json.loads(line)
        for line in (state_root / "bugs-dispatched.log").read_text(encoding="utf-8").splitlines()
    ]
    assert records[-1]["task_id"] == "t_created1"


def test_concurrent_check_calls_create_one_task(monkeypatch, tmp_path):
    bugtool, projects_root, _state_root = load_bugtool(monkeypatch, tmp_path)
    write_pending_bug(projects_root)
    created = mock_kanban(monkeypatch, bugtool, create_delay=0.05)
    start = threading.Barrier(2)

    def check_once():
        start.wait()
        bugtool.cmd_check(argparse.Namespace())

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(check_once) for _ in range(2)]
        for future in futures:
            future.result(timeout=2)

    assert created == ["t_created1"]


def test_markdown_revert_uses_dispatch_log_before_markdown_state(monkeypatch, tmp_path):
    bugtool, projects_root, state_root = load_bugtool(monkeypatch, tmp_path)
    original = complete_bug_text()
    path = write_pending_bug(projects_root, original)
    created = mock_kanban(monkeypatch, bugtool)

    bugtool.cmd_check(argparse.Namespace())
    assert created == ["t_created1"]
    path.write_text(original, encoding="utf-8")

    def markdown_live_state_must_not_be_consulted(_text):
        raise AssertionError("dispatch log must be consulted before markdown task state")

    monkeypatch.setattr(bugtool, "live_task_ids", markdown_live_state_must_not_be_consulted)
    bugtool.cmd_check(argparse.Namespace())

    assert created == ["t_created1"]
    assert (state_root / "bugs-dispatched.log").is_file()
