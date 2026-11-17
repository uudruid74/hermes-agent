"""bugtool cmd_set section-mismatch handling (t_ce1eb644).

cmd_set resolves the requested section and calls replace_section(), which
APPENDS a new section at end-of-file when the name does not match an
existing heading. `bugtool set <file> description <text>` (lowercase, or
any non-canonical form absent from SECTION_ALIASES) therefore silently
appended a brand-new '## description' section while the canonical
'## Description' stayed empty. Regression contract:

- non-canonical forms auto-map to the canonical heading (case-insensitive)
- unknown sections ERROR and leave the file untouched
- a duplicate mis-named section is NEVER appended
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
    monkeypatch.setenv("USERNAME", "neo")
    monkeypatch.setenv("BUGTOOL_DATE", "2026-11-17")
    spec = importlib.util.spec_from_file_location(
        f"bugtool_set_test_{threading.get_ident()}_{time.monotonic_ns()}",
        BUGTOOL_PATH,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, projects_root, state_root


def neutralise_subprocess(monkeypatch, bugtool):
    """Cut cmd_new's real-vault auto-commit off (same pattern as project_resolution)."""
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


def _new_args(slug, title="A title"):
    return argparse.Namespace(
        project="hermes-agent", slug=slug, title=title,
        assignee="neo", tags="", severity="normal",
    )


def _make_bug_file(bugtool, projects_root, slug="test-section-mismatch"):
    (projects_root / "hermes-agent").mkdir(parents=True)
    bugtool.cmd_new(_new_args(slug))
    files = list((projects_root / "hermes-agent" / "bugs" / "pending").glob("*.md"))
    assert len(files) == 1, files
    return files[0]


def _set_args(path, section, text):
    return argparse.Namespace(file=str(path), section=section, text=text)


def _no_dispatch(monkeypatch, bugtool):
    """Dispatch gating is orthogonal (covered by its own suite); no-op here."""
    monkeypatch.setattr(bugtool, "maybe_dispatch_locked", lambda *a, **k: None)


# --- resolve_section unit behavior -----------------------------------------


def test_resolve_section_alias_returns_canonical(monkeypatch, tmp_path):
    bugtool, _, _ = load_bugtool(monkeypatch, tmp_path)
    assert bugtool.resolve_section("symptom", "") == "Symptom"


def test_resolve_section_case_insensitive_existing_heading(monkeypatch, tmp_path):
    bugtool, _, _ = load_bugtool(monkeypatch, tmp_path)
    text = "## Description\n\nbody\n"
    assert bugtool.resolve_section("DESCRIPTION", text) == "Description"


def test_resolve_section_kebab_maps_to_existing_heading(monkeypatch, tmp_path):
    bugtool, _, _ = load_bugtool(monkeypatch, tmp_path)
    text = "## How To Reproduce\n\nbody\n"
    assert bugtool.resolve_section("how-to-reproduce", text) == "How To Reproduce"


def test_resolve_section_known_template_section_missing_returns_canonical(monkeypatch, tmp_path):
    bugtool, _, _ = load_bugtool(monkeypatch, tmp_path)
    assert bugtool.resolve_section("expected behavior", "") == "Expected Behavior"


def test_resolve_section_unknown_raises(monkeypatch, tmp_path):
    bugtool, _, _ = load_bugtool(monkeypatch, tmp_path)
    with pytest.raises(SystemExit, match="bugtool: unknown section"):
        bugtool.resolve_section("descripton", "## Description\n\nbody\n")


# --- cmd_set integration ----------------------------------------------------


def test_cmd_set_lowercase_description_fills_canonical_section(monkeypatch, tmp_path):
    bugtool, projects_root, _ = load_bugtool(monkeypatch, tmp_path)
    neutralise_subprocess(monkeypatch, bugtool)
    _no_dispatch(monkeypatch, bugtool)
    path = _make_bug_file(bugtool, projects_root)
    bugtool.cmd_set(_set_args(path, "description", "some text"))
    text = path.read_text(encoding="utf-8")
    assert "## Description\n\nsome text\n" in text
    assert text.count("## Description") == 1
    assert "## description" not in text


def test_cmd_set_all_required_via_noncanonical_forms_passes_dispatch_gate(monkeypatch, tmp_path):
    # The dispatch gate reads canonical names exactly; non-canonical `set`
    # forms must fill the canonical sections so the gate sees content.
    bugtool, projects_root, _ = load_bugtool(monkeypatch, tmp_path)
    neutralise_subprocess(monkeypatch, bugtool)
    _no_dispatch(monkeypatch, bugtool)
    path = _make_bug_file(bugtool, projects_root)
    bugtool.cmd_set(_set_args(path, "description", "some text"))
    bugtool.cmd_set(_set_args(path, "how-to-reproduce", "the steps"))
    bugtool.cmd_set(_set_args(path, "actual-behavior", "the outcome"))
    text = path.read_text(encoding="utf-8")
    assert bugtool.required_complete(text)
    for canonical in ("Description", "How To Reproduce", "Actual Behavior"):
        assert text.count(f"## {canonical}") == 1
    assert "## description" not in text
    assert "## how-to-reproduce" not in text
    assert "## actual-behavior" not in text


def test_cmd_set_kebab_how_to_reproduce_fills_canonical(monkeypatch, tmp_path):
    bugtool, projects_root, _ = load_bugtool(monkeypatch, tmp_path)
    neutralise_subprocess(monkeypatch, bugtool)
    _no_dispatch(monkeypatch, bugtool)
    path = _make_bug_file(bugtool, projects_root)
    bugtool.cmd_set(_set_args(path, "how-to-reproduce", "the steps"))
    text = path.read_text(encoding="utf-8")
    assert "## How To Reproduce\n\nthe steps\n" in text
    assert text.count("## How To Reproduce") == 1
    assert "## how-to-reproduce" not in text


def test_cmd_set_kebab_actual_behavior_fills_canonical(monkeypatch, tmp_path):
    bugtool, projects_root, _ = load_bugtool(monkeypatch, tmp_path)
    neutralise_subprocess(monkeypatch, bugtool)
    _no_dispatch(monkeypatch, bugtool)
    path = _make_bug_file(bugtool, projects_root)
    bugtool.cmd_set(_set_args(path, "actual-behavior", "the outcome"))
    text = path.read_text(encoding="utf-8")
    assert "## Actual Behavior\n\nthe outcome\n" in text
    assert text.count("## Actual Behavior") == 1
    assert "## actual-behavior" not in text


def test_cmd_set_alias_symptom_appends_canonical_when_missing(monkeypatch, tmp_path):
    bugtool, projects_root, _ = load_bugtool(monkeypatch, tmp_path)
    neutralise_subprocess(monkeypatch, bugtool)
    _no_dispatch(monkeypatch, bugtool)
    path = _make_bug_file(bugtool, projects_root)
    bugtool.cmd_set(_set_args(path, "symptom", "crash on boot"))
    text = path.read_text(encoding="utf-8")
    assert "## Symptom\n\ncrash on boot\n" in text
    assert text.count("## Symptom") == 1
    assert "## symptom" not in text


def test_cmd_set_canonical_name_still_works(monkeypatch, tmp_path):
    bugtool, projects_root, _ = load_bugtool(monkeypatch, tmp_path)
    neutralise_subprocess(monkeypatch, bugtool)
    _no_dispatch(monkeypatch, bugtool)
    path = _make_bug_file(bugtool, projects_root)
    bugtool.cmd_set(_set_args(path, "Description", "canonical text"))
    text = path.read_text(encoding="utf-8")
    assert "## Description\n\ncanonical text\n" in text
    assert text.count("## Description") == 1


def test_cmd_set_unknown_section_errors_and_leaves_file_unchanged(monkeypatch, tmp_path):
    bugtool, projects_root, _ = load_bugtool(monkeypatch, tmp_path)
    neutralise_subprocess(monkeypatch, bugtool)
    _no_dispatch(monkeypatch, bugtool)
    path = _make_bug_file(bugtool, projects_root)
    before = path.read_text(encoding="utf-8")
    with pytest.raises(SystemExit, match="bugtool: unknown section"):
        bugtool.cmd_set(_set_args(path, "descripton", "should not land"))
    after = path.read_text(encoding="utf-8")
    assert after == before
    assert "## descripton" not in after
