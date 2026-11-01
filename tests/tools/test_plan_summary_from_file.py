"""advance/handoff accept the summary from file= (short-args emission path).

2026-11-01 wire capture proved the provider stream carries `advance` calls
with the composed `summary` key stripped (context-pressure emission failure).
Writing the summary via write_file and passing file=<path> keeps every
emitted argument short; the file content is used as the summary.
"""

from __future__ import annotations

import sqlite3

from hermes_cli.kanban_db import SCHEMA_SQL
from tools import plan

from tests.tools.test_plan_binding_adapter import _Agent, _FlexibleSessionDB


class _FileSummaryAgent(_Agent):
    session_id = "file-summary"
    _session_db = _FlexibleSessionDB()


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def _new_plan(monkeypatch):
    conn = _db()
    monkeypatch.setattr(plan, "_get_kanban_db", lambda board=None: conn)
    monkeypatch.setattr(
        plan, "clarify_tool", lambda *_a, **_k: '{"user_response":"Approve"}'
    )
    agent = _FileSummaryAgent()
    plan.plan(agent, "new", title="File summary", goal="g", steps=["s1", "s2"])
    return conn, agent


def test_advance_reads_summary_from_file(monkeypatch, tmp_path):
    conn, agent = _new_plan(monkeypatch)
    f = tmp_path / "summary.md"
    f.write_text("Did the thing: files changed, outcome stated.")
    result = plan.plan(agent, "advance", file=str(f))
    assert "ERROR" not in str(result)
    row = conn.execute(
        "SELECT task_stepno FROM tasks WHERE status='manual'"
    ).fetchone()
    assert row[0] == 2


def test_advance_without_summary_or_file_still_errors(monkeypatch):
    _conn, agent = _new_plan(monkeypatch)
    result = plan.plan(agent, "advance")
    assert "requires summary" in str(result)


def test_advance_unreadable_summary_file_errors_clearly(monkeypatch, tmp_path):
    _conn, agent = _new_plan(monkeypatch)
    result = plan.plan(agent, "advance", file=str(tmp_path / "nope.md"))
    assert "cannot read summary file" in str(result)


def test_inline_summary_still_wins_over_file(monkeypatch, tmp_path):
    conn, agent = _new_plan(monkeypatch)
    f = tmp_path / "summary.md"
    f.write_text("file text")
    result = plan.plan(agent, "advance", summary="inline text", file=str(f))
    assert "ERROR" not in str(result)
    row = conn.execute(
        "SELECT task_stepno FROM tasks WHERE status='manual'"
    ).fetchone()
    assert row[0] == 2
