"""bugtoold daemon tests (t_cd44bbde, Phases C + C2) — fully offline.

No live dispatch ever: `bugtool dispatch` child processes are mocked, the
watch loop is never started (we test cycle/scan/debounce units directly), and
fixture vaults live in tmp_path. The critical safety assertions:

  - commits are PATHSPEC-LIMITED (never `git add -A`);
  - the kill switch produces zero subprocess calls;
  - the hourly cap skips dispatches;
  - the decision log records dispatches AND skips with reasons;
  - dry-run suppresses commit + injector + dispatch side effects.
"""
import importlib.util
import json
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


BUGTOOLD_PATH = Path(__file__).parents[2] / "scripts" / "bugtoold.py"
BUGTOOL_PATH = Path(__file__).parents[2] / "scripts" / "bugtool.py"


def load_bugtoold(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location(
        f"bugtoold_test_{threading.get_ident()}_{time.monotonic_ns()}", BUGTOOLD_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setenv("BUGTOOL_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("BUGTOOL_STATE_ROOT", str(tmp_path / "state"))
    spec.loader.exec_module(module)
    return module


def make_vault(tmp_path):
    vault = tmp_path / "vault"
    (vault / "wiki" / "Projects" / "Hermes-Agent" / "bugs" / "pending").mkdir(parents=True)
    (vault / "wiki" / "Projects" / "Hermes-Agent" / "bugs" / "resolved").mkdir(parents=True)
    return vault


def checked_bug_text():
    return (
        "---\n"
        'title: "daemon bug"\n'
        'type: "bug"\n'
        'status: "pending"\n'
        'assignee: "neo"\n'
        "---\n\n"
        "# Daemon bug\n\n"
        "## Description\n\nWork\n\n"
        "## How To Reproduce\n\nRun\n\n"
        "## Actual Behavior\n\nBlocked\n\n"
        "## Assignee\n\nneo\n\n"
        "## Approved to run\n\n- [x] approved by Evan\n\n"
        "## Kanban tasks\n\n\n"
        "## Failure Reports\n\n"
    )


# ---------------------------------------------------------------- Debouncer


def test_debouncer_ready_only_after_quiescence(monkeypatch, tmp_path):
    bugtoold = load_bugtoold(monkeypatch, tmp_path)
    debouncer = bugtoold.Debouncer(60.0)
    assert not debouncer.ready(now=0.0)
    debouncer.mark(now=100.0)
    assert not debouncer.ready(now=130.0)   # 30s of quiet < 60s
    assert debouncer.ready(now=160.5)       # 60.5s of quiet
    debouncer.reset()
    assert not debouncer.ready(now=1000.0)  # silence before any change means no


# ------------------------------------------------------------- poll_changes


def test_poll_changes_detects_edit_and_resolution(monkeypatch, tmp_path):
    bugtoold = load_bugtoold(monkeypatch, tmp_path)
    vault = make_vault(tmp_path)
    pending = vault / "wiki/Projects/Hermes-Agent/bugs/pending/2026-10-05-x.md"
    pending.write_text(checked_bug_text(), encoding="utf-8")

    first, sig1 = bugtoold.poll_changes(vault, {})
    assert first == {pending}  # initial snapshot sees the existing file

    second, sig2 = bugtoold.poll_changes(vault, sig1)
    assert second == set()

    time.sleep(0.01)
    pending.write_text(checked_bug_text() + "\n<!-- edited -->\n", encoding="utf-8")
    edited, _sig3 = bugtoold.poll_changes(vault, sig2)
    assert edited == {pending}

    # Resolution = the pending file disappears (moved to resolved/): a change.
    target = vault / "wiki/Projects/Hermes-Agent/bugs/resolved/2026-10-05-x.md"
    target.write_text(pending.read_text(encoding="utf-8"), encoding="utf-8")
    pending.unlink()
    resolved_sig = dict(sig2)
    resolved_sig.pop(pending)
    resolved_sig[target] = sig2[pending] if target in sig2 else (0, 0)
    changed, _sig4 = bugtoold.poll_changes(vault, sig2)
    assert pending in changed  # its disappearance is the event


# ---------------------------------------------------------- commit_watched


def test_commit_watched_is_pathspec_limited_never_add_all(monkeypatch, tmp_path):
    bugtoold = load_bugtoold(monkeypatch, tmp_path)
    vault = make_vault(tmp_path)
    calls = []

    def run(args, **_kw):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(bugtoold.subprocess, "run", run)
    dirty = [
        (vault / "wiki/Projects/Hermes-Agent/bugs/pending/a.md", False),
        (vault / "wiki/Projects/Hermes-Agent/bugs/pending/b.md", True),
    ]
    assert bugtoold.commit_watched(vault, dirty, dry_run=False) is True

    add_calls = [c for c in calls if c[3] == "add"]
    commit_calls = [c for c in calls if c[3] == "commit"]
    assert len(commit_calls) == 1
    # The ONLY add is the untracked watched file, by explicit path.
    assert add_calls == [["git", "-C", str(vault), "add", "--", str(dirty[1][0])]]
    for call in calls:
        assert "-A" not in call
        assert "--all" not in call
    # The commit carries the pathspec after `--`.
    commit = commit_calls[0]
    assert "--" in commit
    assert set(commit[commit.index("--") + 1:]) == {str(p) for p, _ in dirty}


def test_commit_watched_dry_run_touches_nothing(monkeypatch, tmp_path):
    bugtoold = load_bugtoold(monkeypatch, tmp_path)
    vault = make_vault(tmp_path)

    def fail(_args, **_kw):
        raise AssertionError("dry-run must not invoke git")

    monkeypatch.setattr(bugtoold.subprocess, "run", fail)
    dirty = [(vault / "wiki/Projects/Hermes-Agent/bugs/pending/a.md", False)]
    assert bugtoold.commit_watched(vault, dirty, dry_run=True) is True


# ------------------------------------------------------------- kill switch


def test_kill_switch_blocks_everything(monkeypatch, tmp_path):
    bugtoold = load_bugtoold(monkeypatch, tmp_path)
    vault = make_vault(tmp_path)
    state = tmp_path / "bugtoold-state"
    state.mkdir()
    (state / "KILLSWITCH").write_text("", encoding="utf-8")
    decisions = bugtoold.DecisionLog(state / "decision-log.jsonl", dry_run=False)

    def fail(_args, **_kw):
        raise AssertionError("kill switch must produce zero subprocess calls")

    monkeypatch.setattr(bugtoold.subprocess, "run", fail)
    monkeypatch.setattr(bugtoold, "load_bugtool_module", fail)
    bugtoold.cycle(
        vault, dict(bugtoold.DEFAULTS), decisions, state / "KILLSWITCH",
        dry_run=False, killswitch_logged=[False],
    )
    entries = [json.loads(line) for line in (state / "decision-log.jsonl").read_text().splitlines()]
    assert any(e["action"] == "skip" and "kill switch" in e.get("reason", "") for e in entries)


# ----------------------------------------------------- scan + decision log


def test_scan_dispatches_checked_bug_via_bugtool_dispatch(monkeypatch, tmp_path):
    bugtoold = load_bugtoold(monkeypatch, tmp_path)
    vault = make_vault(tmp_path)
    bug = vault / "wiki/Projects/Hermes-Agent/bugs/pending/2026-10-05-ready.md"
    bug.write_text(checked_bug_text(), encoding="utf-8")
    state = tmp_path / "bugtoold-state"
    state.mkdir(parents=True)
    decisions = bugtoold.DecisionLog(state / "decision-log.jsonl", dry_run=False)

    def run(args, **_kw):
        # The daemon's own child dispatch (sys.executable bugtool.py dispatch ...)
        if args[0] == sys.executable and args[2:4] == ["dispatch", str(bug)]:
            return subprocess.CompletedProcess(args, 0, f"dispatched t_foo1 -> {bug.name}\n", "")
        # Board queries from the loaded bugtool module (live_task_count_by_agent)
        if args[:2] == ["hermes", "kanban"]:
            return subprocess.CompletedProcess(args, 0, "[]", "")
        raise AssertionError(f"unexpected subprocess: {args}")

    monkeypatch.setattr(bugtoold.subprocess, "run", run)
    dispatched = bugtoold.scan_and_dispatch(
        vault, dict(bugtoold.DEFAULTS), decisions, dry_run=False
    )
    assert dispatched == 1
    entries = [json.loads(line) for line in (state / "decision-log.jsonl").read_text().splitlines()]
    dispatches = [e for e in entries if e["action"] == "dispatch"]
    assert len(dispatches) == 1
    assert dispatches[0]["bug"] == bug.name
    assert dispatches[0]["task"] == "t_foo1"


def test_scan_records_skip_reasons_for_every_block(monkeypatch, tmp_path):
    bugtoold = load_bugtoold(monkeypatch, tmp_path)
    vault = make_vault(tmp_path)
    unchecked = vault / "wiki/Projects/Hermes-Agent/bugs/pending/2026-10-05-unchecked.md"
    unchecked.write_text(checked_bug_text().replace("- [x]", "- [ ]"), encoding="utf-8")
    already = vault / "wiki/Projects/Hermes-Agent/bugs/pending/2026-10-05-already.md"
    already.write_text(
        checked_bug_text().replace('status: "pending"', 'status: "dispatched"'),
        encoding="utf-8",
    )
    state = tmp_path / "bugtoold-state"
    state.mkdir(parents=True)
    decisions = bugtoold.DecisionLog(state / "decision-log.jsonl", dry_run=False)

    def fail(_args, **_kw):
        raise AssertionError("no dispatch expected; every bug must skip")

    monkeypatch.setattr(bugtoold.subprocess, "run", fail)
    bugtoold.scan_and_dispatch(vault, dict(bugtoold.DEFAULTS), decisions, dry_run=False)
    entries = [json.loads(line) for line in (state / "decision-log.jsonl").read_text().splitlines()]
    reasons = {e["bug"]: e["reason"] for e in entries if e["action"] == "skip"}
    assert "approval checkbox unchecked" in reasons[unchecked.name]
    assert "already dispatched" in reasons[already.name]


def test_scan_dry_run_suppresses_dispatch_but_logs(monkeypatch, tmp_path, capsys):
    bugtoold = load_bugtoold(monkeypatch, tmp_path)
    vault = make_vault(tmp_path)
    bug = vault / "wiki/Projects/Hermes-Agent/bugs/pending/2026-10-05-ready.md"
    bug.write_text(checked_bug_text(), encoding="utf-8")
    state = tmp_path / "bugtoold-state"
    state.mkdir(parents=True)
    decisions = bugtoold.DecisionLog(state / "decision-log.jsonl", dry_run=True)

    def run(args, **_kw):
        # Board queries (per-agent cap) are legitimate read-only subprocesses;
        # anything else (a real dispatch, a commit) is forbidden in dry-run.
        if args[:2] == ["hermes", "kanban"]:
            return subprocess.CompletedProcess(args, 0, "[]", "")
        raise AssertionError(f"dry-run must not invoke: {args}")

    monkeypatch.setattr(bugtoold.subprocess, "run", run)
    assert bugtoold.scan_and_dispatch(vault, dict(bugtoold.DEFAULTS), decisions, dry_run=True) == 0
    # Dry-run touches nothing on disk: decisions go to the console log only.
    out = capsys.readouterr().out
    assert "dry-run: dispatch suppressed" in out
    assert not (state / "decision-log.jsonl").exists()


def test_hourly_cap_skips_dispatch(monkeypatch, tmp_path):
    bugtoold = load_bugtoold(monkeypatch, tmp_path)
    vault = make_vault(tmp_path)
    bug = vault / "wiki/Projects/Hermes-Agent/bugs/pending/2026-10-05-ready.md"
    bug.write_text(checked_bug_text(), encoding="utf-8")
    state = tmp_path / "bugtoold-state"
    state.mkdir(parents=True)
    decisions = bugtoold.DecisionLog(state / "decision-log.jsonl", dry_run=False)
    # Four dispatches in the last hour (the default cap).
    recent = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    with decisions.path.open("w", encoding="utf-8") as handle:
        for index in range(4):
            handle.write(json.dumps({"ts": recent, "action": "dispatch", "bug": f"old{index}"}) + "\n")

    def run(args, **_kw):
        # The per-agent cap's board query is a legitimate read-only call; the
        # actual dispatch must never happen once the hourly cap is reached.
        if args[:2] == ["hermes", "kanban"]:
            return subprocess.CompletedProcess(args, 0, "[]", "")
        raise AssertionError(f"hourly cap reached: no dispatch allowed, got {args}")

    monkeypatch.setattr(bugtoold.subprocess, "run", run)
    assert bugtoold.scan_and_dispatch(vault, dict(bugtoold.DEFAULTS), decisions, dry_run=False) == 0
    entries = [json.loads(line) for line in (state / "decision-log.jsonl").read_text().splitlines()]
    assert any("hourly cap reached" in e.get("reason", "") for e in entries)


def test_decision_log_counts_only_recent_dispatches(monkeypatch, tmp_path):
    bugtoold = load_bugtoold(monkeypatch, tmp_path)
    log_path = tmp_path / "decision-log.jsonl"
    decisions = bugtoold.DecisionLog(log_path, dry_run=False)
    decisions.append("dispatch", bug="recent.md")
    now = datetime.now(timezone.utc)
    stale = (now - timedelta(hours=2)).isoformat()
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ts": stale, "action": "dispatch", "bug": "stale.md"}) + "\n")
        handle.write(json.dumps({"ts": stale, "action": "skip", "reason": "x"}) + "\n")
    assert decisions.dispatches_last_hour() == 1


# ------------------------------------------------------------ config loading


def test_load_settings_defaults_without_config(monkeypatch, tmp_path):
    bugtoold = load_bugtoold(monkeypatch, tmp_path)
    settings = bugtoold.load_settings(tmp_path / "missing.yaml")
    assert settings == dict(bugtoold.DEFAULTS)


def test_load_settings_reads_config_yaml_section(monkeypatch, tmp_path):
    pytest.importorskip("yaml")
    bugtoold = load_bugtoold(monkeypatch, tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text(
        "bugtoold:\n  quiescence_seconds: 30\n  max_dispatches_per_hour: 7\n",
        encoding="utf-8",
    )
    settings = bugtoold.load_settings(config)
    assert settings["quiescence_seconds"] == 30
    assert settings["max_dispatches_per_hour"] == 7
    assert settings["poll_interval_seconds"] == bugtoold.DEFAULTS["poll_interval_seconds"]


def test_load_settings_invalid_value_keeps_default(monkeypatch, tmp_path):
    pytest.importorskip("yaml")
    bugtoold = load_bugtoold(monkeypatch, tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text("bugtoold:\n  max_dispatches_per_hour: -3\n", encoding="utf-8")
    settings = bugtoold.load_settings(config)
    assert settings["max_dispatches_per_hour"] == bugtoold.DEFAULTS["max_dispatches_per_hour"]
