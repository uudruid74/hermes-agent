"""plan dispatch must kick event-time dispatch (Evan's agreed design 2026-10-19).

Every mutating verb/tool fires ``kick_dispatch()`` -> one ``dispatch_once``
tick at the moment of the event. Zephyr's t_084728ad (2026-11-02) sat in
``ready`` 1.4h+ because ``_cmd_dispatch`` — the canonical cross-agent
dispatch path — inserted the task and returned without kicking. This pins
the kick at that call site.
"""

from __future__ import annotations

import sqlite3

from hermes_cli.kanban_db import SCHEMA_SQL
from tools import plan

from tests.tools.test_plan_binding_adapter import _Agent, _FlexibleSessionDB


class _DispatchAgent(_Agent):
    session_id = "dispatch-kick"
    _session_db = _FlexibleSessionDB()


def test_dispatch_fires_kick_dispatch(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    monkeypatch.setattr(plan, "_get_kanban_db", lambda board=None: conn)

    kicks: list[dict] = []

    import hermes_cli.kanban as kanban

    monkeypatch.setattr(
        kanban, "kick_dispatch", lambda board=None: kicks.append({"board": board})
    )

    agent = _DispatchAgent()
    result = plan.plan(
        agent,
        "dispatch",
        title="Kick test",
        goal="The create event must kick dispatch.",
        project="hermes-agent",
        assignee="gopher",
    )

    assert "ERROR" not in str(result)
    assert kicks == [{"board": plan._resolve_board("hermes-agent")}]
