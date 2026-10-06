"""bugtool project-name resolution — deterministic slug (t_011fbd08).

Evan's ruling (2026-10-04 Special Notes): the wiki project dirs are renamed
to match the kanban slugs (Hermes-Agent -> hermes-agent, Eddon -> eddon, ...).
`normalize_project` therefore does NO case resolution — the passed slug IS the
canonical path, and it MUST already exist under PROJECTS_ROOT. A missing
project dies loudly (naming the available ones) instead of silently returning
an empty list or creating a shadow directory.

Mutation-verify: tests 2 and 3 FAIL against the committed code (which returned
the caller's casing verbatim and mkdir'd a shadow dir).
"""
import argparse
import importlib.util
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
    monkeypatch.setenv("USERNAME", "gopher")
    spec = importlib.util.spec_from_file_location(
        f"bugtool_proj_test_{threading.get_ident()}_{time.monotonic_ns()}", BUGTOOL_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, projects_root, state_root


def neutralise_subprocess(monkeypatch, bugtool):
    """Replace bugtool's whole subprocess module with a no-op recorder.

    cmd_new auto-commits to the REAL vault (`git -C /home/ekl/vault ...`),
    so every test that reaches the write path must cut subprocess off
    completely — patching only `.run` is insufficient.
    """
    calls = []

    class FakePopen:
        def __init__(self, *a, **k):
            self.args = a[0] if a else []
            calls.append(self.args)
        def communicate(self, *a, **k):
            return ("", "")

    class FakeSubprocess:
        CompletedProcess = subprocess.CompletedProcess

        @staticmethod
        def run(*a, **k):
            args = a[0] if a else []
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")

        Popen = FakePopen

    monkeypatch.setattr(bugtool, "subprocess", FakeSubprocess)
    return calls


def test_normalize_project_exact_slug_returns_unchanged(monkeypatch, tmp_path):
    bugtool, projects_root, _ = load_bugtool(monkeypatch, tmp_path)
    (projects_root / "hermes-agent").mkdir(parents=True)
    assert bugtool.normalize_project("hermes-agent") == "hermes-agent"


def test_normalize_project_wrong_case_unknown_dies(monkeypatch, tmp_path):
    bugtool, projects_root, _ = load_bugtool(monkeypatch, tmp_path)
    (projects_root / "hermes-agent").mkdir(parents=True)
    with pytest.raises(SystemExit) as exc:
        bugtool.normalize_project("Hermes-Agent")
    assert "does not exist" in str(exc.value)
    assert "hermes-agent" in str(exc.value)  # names the known project


def test_cmd_new_unknown_project_refuses_and_creates_nothing(monkeypatch, tmp_path):
    bugtool, projects_root, _ = load_bugtool(monkeypatch, tmp_path)
    (projects_root / "hermes-agent").mkdir(parents=True)
    args = argparse.Namespace(
        project="Ragamuffin", slug="some-slug", title="A title",
        assignee="neo", tags="", severity="normal",
    )
    with pytest.raises(SystemExit) as exc:
        bugtool.cmd_new(args)
    assert "does not exist" in str(exc.value)
    # No shadow directory was created.
    assert not (projects_root / "Ragamuffin").exists()


def test_cmd_new_canonical_slug_writes_into_existing_dir(monkeypatch, tmp_path):
    bugtool, projects_root, _ = load_bugtool(monkeypatch, tmp_path)
    (projects_root / "hermes-agent").mkdir(parents=True)
    # cmd_new auto-commits to the real vault via subprocess; cut git off
    # completely so this test can never touch /home/ekl/vault.
    calls = neutralise_subprocess(monkeypatch, bugtool)
    args = argparse.Namespace(
        project="hermes-agent", slug="some-slug", title="A title",
        assignee="neo", tags="", severity="normal",
    )
    bugtool.cmd_new(args)
    # cmd_new attempts its vault auto-commit via git; with the whole
    # subprocess module stubbed, those git invocations are RECORDED but never
    # executed — proving real git could not have touched /home/ekl/vault.
    assert any(
        isinstance(c, list) and c and c[0] == "git"
        for c in calls
    )
    written = list((projects_root / "hermes-agent" / "bugs" / "pending").glob("*.md"))
    assert len(written) == 1
    assert written[0].name.endswith("some-slug.md")


def test_all_bug_files_wrong_case_dies_not_silent_empty(monkeypatch, tmp_path):
    bugtool, projects_root, _ = load_bugtool(monkeypatch, tmp_path)
    (projects_root / "hermes-agent" / "bugs" / "pending").mkdir(parents=True)
    # Existing slug: returns the (empty) list, no exception.
    assert bugtool.all_bug_files("hermes-agent") == []
    # Wrong case / unknown slug: dies loudly instead of a silent empty list.
    with pytest.raises(SystemExit):
        bugtool.all_bug_files("Hermes-Agent")
