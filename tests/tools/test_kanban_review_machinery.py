"""Upstream review-machinery port tests (t_6a55f4d6, build step 4).

kanban_request_review / kanban_request_changes (worker tools) and the
`kanban reopen-review` CLI verb all inherit ONE base:
kanban_db.send_dispatcher_message — durable event first, then the
best-effort `hermes send -t` wake. No status/step mutation.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hermes_cli import kanban_db as kb  # noqa: E402
from hermes_cli.kanban_db import SCHEMA_SQL  # noqa: E402
from tools import kanban_tools  # noqa: E402


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def _task(conn, *, created_by="gopher", assignee="neo", status="running"):
    conn.execute(
        "INSERT INTO tasks(id,title,status,assignee,created_at,created_by) "
        "VALUES(?,?,?,?,1,?)",
        ("t_xyz", "T", status, assignee, created_by),
    )
    conn.commit()
    return "t_xyz"


class _Ctx:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *exc):
        return False


class _NoCloseConn:
    """Proxy that swallows close() so handlers' finally blocks keep the
    in-memory test DB usable after the call."""

    def __init__(self, conn):
        self._conn = conn

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_send_dispatcher_message_event_first_then_ping(monkeypatch):
    conn = _conn()
    tid = _task(conn)
    sent: list[list[str]] = []
    from hermes_cli import _subprocess_compat

    monkeypatch.setattr(
        _subprocess_compat,
        "spawn_detached",
        lambda argv, **kw: sent.append(argv) or SimpleNamespace(),
    )
    ok, detail = kb.send_dispatcher_message(
        conn,
        tid,
        direction="to-dispatcher",
        kind="review-requested",
        text="please review",
        actor="worker",
    )
    assert ok
    assert (
        conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0]
        == "running"
    )
    event = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (tid,),
    ).fetchone()
    assert event["kind"] == "review-review-requested"
    assert json.loads(event["payload"])["text"] == "please review"
    # ping went to the AI creator (gopher != assignee neo)
    assert sent and sent[0][3] == "gopher"


def test_send_dispatcher_message_self_dispatched_has_no_target(monkeypatch):
    conn = _conn()
    tid = _task(conn, created_by="neo", assignee="neo")
    sent: list[list[str]] = []
    from hermes_cli import _subprocess_compat

    monkeypatch.setattr(
        _subprocess_compat,
        "spawn_detached",
        lambda argv, **kw: sent.append(argv) or SimpleNamespace(),
    )
    ok, detail = kb.send_dispatcher_message(
        conn,
        tid,
        direction="to-dispatcher",
        kind="review-requested",
        text="please review",
        actor="worker",
    )
    # The durable ask IS recorded; no AI target, human fallback noted.
    assert ok and "no AI recipient" in detail and not sent


def test_kanban_request_review_tool(monkeypatch):
    conn = _conn()
    tid = _task(conn)
    from hermes_cli import _subprocess_compat

    monkeypatch.setattr(
        _subprocess_compat, "spawn_detached", lambda argv, **kw: SimpleNamespace()
    )
    monkeypatch.setattr(kanban_tools, "_connect", lambda board=None: (kb, _NoCloseConn(conn)))
    out = json.loads(
        kanban_tools._handle_request_review(
            {"task_id": tid, "summary": "did it, tested"}
        )
    )
    assert out["ok"] is True
    event = conn.execute(
        "SELECT kind FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (tid,),
    ).fetchone()
    assert event["kind"] == "review-review-requested"


def test_kanban_request_changes_tool(monkeypatch):
    conn = _conn()
    tid = _task(conn)
    from hermes_cli import _subprocess_compat

    monkeypatch.setattr(
        _subprocess_compat, "spawn_detached", lambda argv, **kw: SimpleNamespace()
    )
    monkeypatch.setattr(kanban_tools, "_connect", lambda board=None: (kb, _NoCloseConn(conn)))
    out = json.loads(
        kanban_tools._handle_request_changes({"task_id": tid, "reason": "add tests"})
    )
    assert out["ok"] is True
    event = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (tid,),
    ).fetchone()
    assert event["kind"] == "review-changes-requested"
    assert "add tests" in json.loads(event["payload"])["text"]
    # status untouched
    assert (
        conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0]
        == "running"
    )


def test_kanban_request_review_requires_summary(monkeypatch):
    conn = _conn()
    tid = _task(conn)
    monkeypatch.setattr(kanban_tools, "_connect", lambda board=None: (kb, _NoCloseConn(conn)))
    out = kanban_tools._handle_request_review({"task_id": tid})
    assert "summary is required" in out


def test_reopen_review_cli_verb(monkeypatch):
    import argparse

    conn = _conn()
    tid = _task(conn)
    from hermes_cli import _subprocess_compat

    monkeypatch.setattr(
        _subprocess_compat, "spawn_detached", lambda argv, **kw: SimpleNamespace()
    )
    from hermes_cli import kanban as kanban_cli

    monkeypatch.setattr(kanban_cli.kb, "connect_closing", lambda: _Ctx(conn))
    args = argparse.Namespace(task_id=tid, reason=["checker", "misled"])
    rc = kanban_cli._cmd_reopen_review(args)
    assert rc == 0
    event = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (tid,),
    ).fetchone()
    assert event["kind"] == "review-review-reopened"
    assert "checker misled" in json.loads(event["payload"])["text"]