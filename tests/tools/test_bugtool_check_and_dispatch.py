"""check-and-dispatch: Evan-only approval, scoped to ONE bug, no sweep.

SUPERSEDES the multi-bug `bugtool check` sweep (removed 2026-09-16).

The old `check` command did two things in one invocation:
  1. with a filename -- stamped the `## Approved to run` checkbox for Evan and
     dispatched that bug (the part worth keeping, now `check-and-dispatch`);
  2. bare, or as a trailing pass -- swept EVERY pending bug file and dispatched
     each gate-complete one, so a single typo could spawn several paid workers.

Evan's ruling (2026-09-16): "Perhaps a better way is to make this only dispatch
the bug being checked, but add a 'related bugs' metadata that has the agent work
the others as part of the same kanban task. 1 task, many bugs." Approval is his
action; agents must not be able to perform it by reflex, and grouping is data,
not a multi-spawn.
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
        f"bugtool_cad_test_{threading.get_ident()}_{time.monotonic_ns()}", BUGTOOL_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, projects_root, state_root


def complete_bug_text(approved=False, related=None):
    checkbox = "- [x] approved by Evan" if approved else "- [ ] approved by Evan"
    text = (
        "---\n"
        'title: "test bug"\n'
        'type: "bug"\n'
        'status: "pending"\n'
        'date: "2026-09-16"\n'
        'filed_by: "Gopher"\n'
        'assignee: "neo"\n'
        "---\n\n"
        "# Bug\n\n"
        "## Description\n\nSomething is wrong\n\n"
        "## How To Reproduce\n\nRun it\n\n"
        "## Actual Behavior\n\nIt breaks\n\n"
        "## Assignee\n\nneo\n\n"
        f"## Approved to run\n\n{checkbox}\n\n"
    )
    if related is not None:
        text += f"## Related bugs\n\n{related}\n\n"
    text += "## Kanban tasks\n\n\n## Failure Reports\n\n"
    return text


def write_pending_bug(projects_root, name="2026-09-16-target.md", text=None):
    path = projects_root / "Hermes-Agent" / "bugs" / "pending" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text or complete_bug_text(), encoding="utf-8")
    return path


def mock_kanban(monkeypatch, bugtool, record_bodies=False):
    """Intercept hermes subprocess calls; collect created task ids (+ bodies)."""
    created = []
    bodies = {}

    def run(args, **_kwargs):
        if args[:3] == ["hermes", "kanban", "create"]:
            task_id = f"t_created{len(created) + 1}"
            created.append(task_id)
            if record_bodies and "--body" in args:
                bodies[task_id] = args[args.index("--body") + 1]
            return subprocess.CompletedProcess(args, 0, json.dumps({"id": task_id}), "")
        if args[:3] == ["hermes", "kanban", "show"]:
            return subprocess.CompletedProcess(args, 0, "status: ready\n", "")
        if args[:3] == ["hermes", "kanban", "comment"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected subprocess: {args}")

    monkeypatch.setattr(bugtool.subprocess, "run", run)
    return (created, bodies) if record_bodies else created


def cad_args(path=None, i_am_evan=True):
    return argparse.Namespace(
        file=str(path) if path else "",
        i_am_evan=i_am_evan,
    )


# --- the guard: no approval without Evan's explicit flag ----------------------


def test_refuses_without_i_am_evan_flag(monkeypatch, tmp_path, capsys):
    """An agent running the command by reflex must not approve anything."""
    bugtool, projects_root, _ = load_bugtool(monkeypatch, tmp_path)
    path = write_pending_bug(projects_root)
    created = mock_kanban(monkeypatch, bugtool)

    with pytest.raises(SystemExit) as excinfo:
        bugtool.cmd_check_and_dispatch(cad_args(path, i_am_evan=False))

    assert "i-am-evan" in str(excinfo.value)
    # Nothing was dispatched AND the approval box is untouched.
    assert created == []
    assert not bugtool.approved_to_run(path.read_text(encoding="utf-8"))


def test_flag_is_required_even_with_a_valid_file(monkeypatch, tmp_path):
    bugtool, projects_root, _ = load_bugtool(monkeypatch, tmp_path)
    path = write_pending_bug(projects_root)
    created = mock_kanban(monkeypatch, bugtool)

    with pytest.raises(SystemExit):
        bugtool.cmd_check_and_dispatch(cad_args(path, i_am_evan=False))

    assert created == []
    assert bugtool.bug_status(path.read_text(encoding="utf-8")) == "pending"


def test_refuses_with_no_file(monkeypatch, tmp_path):
    """Bare invocation cannot be a sweep -- it must error, not dispatch."""
    bugtool, projects_root, _ = load_bugtool(monkeypatch, tmp_path)
    write_pending_bug(projects_root, "2026-09-16-a.md")
    write_pending_bug(projects_root, "2026-09-16-b.md")
    created = mock_kanban(monkeypatch, bugtool)

    with pytest.raises(SystemExit) as excinfo:
        bugtool.cmd_check_and_dispatch(cad_args(None, i_am_evan=True))

    assert "requires a bug file" in str(excinfo.value)
    assert created == []


# --- the happy path: approve + dispatch ONE bug -------------------------------


def test_approves_and_dispatches_with_flag(monkeypatch, tmp_path):
    bugtool, projects_root, _ = load_bugtool(monkeypatch, tmp_path)
    path = write_pending_bug(projects_root)
    created = mock_kanban(monkeypatch, bugtool)

    bugtool.cmd_check_and_dispatch(cad_args(path, i_am_evan=True))

    assert created == ["t_created1"]
    text = path.read_text(encoding="utf-8")
    assert bugtool.approved_to_run(text)          # checkbox stamped
    assert bugtool.bug_status(text) == "dispatched"
    assert bugtool.task_ids(text) == ["t_created1"]


# --- no sweep: other pending bugs are untouched -------------------------------


def test_does_not_dispatch_other_pending_bugs(monkeypatch, tmp_path):
    """The removed sweep's whole hazard: one command, many paid workers."""
    bugtool, projects_root, state_root = load_bugtool(monkeypatch, tmp_path)
    target = write_pending_bug(projects_root, "2026-09-16-target.md")
    other = write_pending_bug(
        projects_root, "2026-09-16-other.md", complete_bug_text(approved=True)
    )
    bystander = write_pending_bug(
        projects_root, "2026-09-16-bystander.md", complete_bug_text(approved=True)
    )
    created = mock_kanban(monkeypatch, bugtool)

    bugtool.cmd_check_and_dispatch(cad_args(target, i_am_evan=True))

    assert created == ["t_created1"], "exactly one task, for the named bug only"
    for p in (other, bystander):
        text = p.read_text(encoding="utf-8")
        assert bugtool.bug_status(text) == "pending", f"{p.name} must be untouched"
        assert bugtool.task_ids(text) == [], f"{p.name} must own no task"
    # Their approvals were pre-existing in this fixture; the point is no NEW task.
    records = [
        json.loads(line)
        for line in (state_root / "bugs-dispatched.log").read_text(encoding="utf-8").splitlines()
    ]
    assert [r["task_id"] for r in records if r["status"] == "created"] == ["t_created1"]


def test_resolved_file_is_refused(monkeypatch, tmp_path):
    bugtool, projects_root, _ = load_bugtool(monkeypatch, tmp_path)
    path = projects_root / "Hermes-Agent" / "bugs" / "resolved" / "2026-09-16-done.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(complete_bug_text(), encoding="utf-8")
    created = mock_kanban(monkeypatch, bugtool)

    with pytest.raises(SystemExit) as excinfo:
        bugtool.cmd_check_and_dispatch(cad_args(path, i_am_evan=True))

    assert excinfo.value.code == 0
    assert created == []


# --- 1 task, many bugs: related-bugs travels as data --------------------------


def test_related_bugs_are_folded_into_one_task_body(monkeypatch, tmp_path):
    bugtool, projects_root, _ = load_bugtool(monkeypatch, tmp_path)
    related = "- 2026-09-16-companion.md — same root cause\n- 2026-09-16-third.md — shares the repro"
    path = write_pending_bug(projects_root, text=complete_bug_text(related=related))
    created, bodies = mock_kanban(monkeypatch, bugtool, record_bodies=True)

    bugtool.cmd_check_and_dispatch(cad_args(path, i_am_evan=True))

    assert created == ["t_created1"], "one task for the primary bug"
    body = bodies["t_created1"]
    assert "## Related bugs" in body
    assert "2026-09-16-companion.md" in body
    assert "2026-09-16-third.md" in body


def test_absent_related_section_leaves_body_shape_unchanged(monkeypatch, tmp_path):
    """Pre-existing bugs must produce byte-identical bodies to before."""
    bugtool, projects_root, _ = load_bugtool(monkeypatch, tmp_path)
    path = write_pending_bug(projects_root)
    created, bodies = mock_kanban(monkeypatch, bugtool, record_bodies=True)

    bugtool.cmd_check_and_dispatch(cad_args(path, i_am_evan=True))

    assert "## Related bugs" not in bodies["t_created1"]


def test_related_bugs_is_optional_not_required(monkeypatch, tmp_path):
    bugtool, projects_root, _ = load_bugtool(monkeypatch, tmp_path)
    path = write_pending_bug(projects_root, text=complete_bug_text(approved=True))

    text = path.read_text(encoding="utf-8")
    assert "Related bugs" not in bugtool.REQUIRED_SECTIONS
    # With every required field satisfied and NO Related bugs section, the
    # dispatch gate is clear -- the section never blocks a bug.
    assert bugtool.missing_dispatch_fields(text) == []


def test_section_alias_resolves_related_bugs(monkeypatch, tmp_path):
    bugtool, _projects_root, _ = load_bugtool(monkeypatch, tmp_path)
    assert bugtool.section_name("related-bugs") == "Related bugs"
    assert bugtool.section_name("Related bugs") == "Related bugs"


# --- the old entry point is gone ---------------------------------------------


def test_cmd_check_no_longer_exists(monkeypatch, tmp_path):
    """A stale caller must fail loudly instead of silently dispatching.

    This is the regression guard for the 2026-09-16 incident: I ran `bugtool
    check` believing it was a status query, and it wrote Evan's approval and
    dispatched a paid worker. There is deliberately NO `cmd_check` alias -- an
    AttributeError is a loud failure, a working shim would be a silent one.
    """
    bugtool, _projects_root, _ = load_bugtool(monkeypatch, tmp_path)
    assert not hasattr(bugtool, "cmd_check")


def test_parser_has_no_bare_check_subcommand(monkeypatch, tmp_path):
    bugtool, _projects_root, _ = load_bugtool(monkeypatch, tmp_path)
    parsed = bugtool.parser().parse_args(["check-and-dispatch", "somefile.md", "--i-am-evan"])
    assert parsed.i_am_evan is True
    with pytest.raises(SystemExit):
        bugtool.parser().parse_args(["check", "somefile.md"])


def test_parser_flag_defaults_to_false(monkeypatch, tmp_path):
    bugtool, _projects_root, _ = load_bugtool(monkeypatch, tmp_path)
    parsed = bugtool.parser().parse_args(["check-and-dispatch", "somefile.md"])
    assert parsed.i_am_evan is False


def test_dispatch_subcommand_still_works_without_the_flag(monkeypatch, tmp_path):
    """`dispatch` stays the agent-safe path: it reads approval, never writes it."""
    bugtool, projects_root, _ = load_bugtool(monkeypatch, tmp_path)
    path = write_pending_bug(projects_root, text=complete_bug_text(approved=True))
    created = mock_kanban(monkeypatch, bugtool)

    bugtool.cmd_dispatch(argparse.Namespace(file=str(path)))

    assert created == ["t_created1"]
    assert bugtool.approved_to_run(path.read_text(encoding="utf-8"))
