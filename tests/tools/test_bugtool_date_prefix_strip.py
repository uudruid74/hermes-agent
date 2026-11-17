"""bugtool cmd_new slug date-prefix stripping (t_e88dad90).

cmd_new prepends today's date to the generated filename. When a caller
passes a slug that ALREADY carries a YYYY-MM-DD prefix, the old code produced
double-dated filenames ('2026-11-16-2026-11-16-foo.md'). A leading genuine
date is now stripped so both 'bugtool new P foo' and
'bugtool new P 2026-11-16-foo' yield '2026-11-16-foo.md'.
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
    # Freeze "today" so filename expectations are deterministic.
    monkeypatch.setenv("BUGTOOL_DATE", "2026-11-17")
    spec = importlib.util.spec_from_file_location(
        f"bugtool_date_test_{threading.get_ident()}_{time.monotonic_ns()}",
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


def _written_pending(projects_root):
    return list((projects_root / "hermes-agent" / "bugs" / "pending").glob("*.md"))


# --- strip_date_prefix unit behavior ---------------------------------------


def test_strip_date_prefix_removes_genuine_date(monkeypatch, tmp_path):
    bugtool, _, _ = load_bugtool(monkeypatch, tmp_path)
    assert bugtool.strip_date_prefix("2026-11-16-some-slug") == "some-slug"


def test_strip_date_prefix_unchanged_without_date(monkeypatch, tmp_path):
    bugtool, _, _ = load_bugtool(monkeypatch, tmp_path)
    assert bugtool.strip_date_prefix("some-slug") == "some-slug"


def test_strip_date_prefix_keeps_invalid_calendar_date(monkeypatch, tmp_path):
    # 2026-02-30 is not a real calendar date -> must NOT be stripped.
    bugtool, _, _ = load_bugtool(monkeypatch, tmp_path)
    assert bugtool.strip_date_prefix("2026-02-30-some-slug") == "2026-02-30-some-slug"


def test_strip_date_prefix_only_strips_at_start(monkeypatch, tmp_path):
    bugtool, _, _ = load_bugtool(monkeypatch, tmp_path)
    assert bugtool.strip_date_prefix("foo-2026-11-16-bar") == "foo-2026-11-16-bar"


def test_strip_date_prefix_date_alone_kept(monkeypatch, tmp_path):
    # No trailing content after the dash: nothing to strip.
    bugtool, _, _ = load_bugtool(monkeypatch, tmp_path)
    assert bugtool.strip_date_prefix("2026-11-16") == "2026-11-16"


# --- cmd_new integration ----------------------------------------------------


def test_cmd_new_dated_slug_writes_single_dated_file(monkeypatch, tmp_path):
    bugtool, projects_root, _ = load_bugtool(monkeypatch, tmp_path)
    (projects_root / "hermes-agent").mkdir(parents=True)
    neutralise_subprocess(monkeypatch, bugtool)
    bugtool.cmd_new(_new_args("2026-11-16-some-slug"))
    written = _written_pending(projects_root)
    assert len(written) == 1
    assert written[0].name == "2026-11-17-some-slug.md"


def test_cmd_new_undated_slug_yields_same_filename(monkeypatch, tmp_path):
    bugtool, projects_root, _ = load_bugtool(monkeypatch, tmp_path)
    (projects_root / "hermes-agent").mkdir(parents=True)
    neutralise_subprocess(monkeypatch, bugtool)
    bugtool.cmd_new(_new_args("some-slug"))
    written = _written_pending(projects_root)
    assert len(written) == 1
    assert written[0].name == "2026-11-17-some-slug.md"


def test_cmd_new_dated_slug_frontmatter_date_is_canonical_today(monkeypatch, tmp_path):
    bugtool, projects_root, _ = load_bugtool(monkeypatch, tmp_path)
    (projects_root / "hermes-agent").mkdir(parents=True)
    neutralise_subprocess(monkeypatch, bugtool)
    bugtool.cmd_new(_new_args("2026-11-16-some-slug"))
    text = _written_pending(projects_root)[0].read_text(encoding="utf-8")
    assert 'date: "2026-11-17"' in text
    assert "2026-11-16" not in text.splitlines()[0:7]  # no doubled date anywhere in frontmatter
