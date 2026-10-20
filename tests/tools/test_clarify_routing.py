"""Dispatcher routing for worker clarify (t_e4812748 root cause, 2026-10-20).

Raw kanban cards had NO clarify routing: the worker's ask went straight to
the interactive callback, the clarify_queue row was never written (the
runtime call passed neither ``agent`` nor ``task_id``), and in a headless
worker the 120s stand-down fallback masqueraded as a user answer — the
t_e4812748 worker reported "dispatcher answered" on exactly that string.

Contract now (Evan's fix target):
1. the clarify_queue row IS written (durable ask),
2. the question reaches the task's creator/dispatcher (F11 base: durable
   ``review-clarify`` event first, then the use-time wake),
3. the dispatcher's answer returns to the parked worker verbatim,
4. a timeout is an explicit NON-answer — never a fallback that reads like one.
"""

import argparse
import json
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from tools.clarify_tool import (
    _route_clarify_to_dispatcher,
    clarify_tool,
)


@pytest.fixture()
def kb_env(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    conn = sqlite3.connect(str(db_path))
    from hermes_cli.kanban_db import SCHEMA_SQL

    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT INTO tasks(id, title, status, created_by, assignee, board, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (
            "t_e4812748",
            "clarify routing test",
            "running",
            "gopher",
            "ornith",
            "default",
            int(time.time()),
        ),
    )
    conn.commit()
    conn.close()

    import hermes_cli.kanban_db as kb

    monkeypatch.setattr(kb, "kanban_db_path", lambda board=None: db_path)
    monkeypatch.setattr(
        "hermes_cli.profiles.profile_exists", lambda name: name == "gopher"
    )

    wakes = []

    def fake_spawn(cmd, **kw):
        wakes.append(list(cmd))
        return SimpleNamespace(pid=1234)

    monkeypatch.setattr("hermes_cli._subprocess_compat.spawn_detached", fake_spawn)
    return SimpleNamespace(db_path=str(db_path), wakes=wakes)


def _pending_ask_id(db_path, task_id="t_e4812748"):
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT id FROM clarify_queue WHERE task_id=? AND status='pending' "
            "ORDER BY created_at DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def test_worker_clarify_queues_row_and_wakes_dispatcher(kb_env):
    agent = SimpleNamespace(
        agent_name="ornith", session_id="sess_w1", clarify_callback=None
    )
    result = {}

    def run():
        result["r"] = clarify_tool(
            question="TEST-CLARIFY — which three tools live on Gopher's belt?",
            # The interactive fallback would "answer" — it must never be
            # consulted while a dispatcher owns the ask.
            callback=lambda q, c, multi_select=False: "IMPROVISED",
            agent=agent,
            task_id="t_e4812748",
        )

    t = threading.Thread(target=run, daemon=True)
    t.start()
    ask_id = None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not ask_id:
        ask_id = _pending_ask_id(kb_env.db_path)
        if not ask_id:
            time.sleep(0.1)
    assert ask_id, "the durable clarify_queue row was never written"

    # The wake carried the question and the answer instruction.
    assert kb_env.wakes, "the dispatcher was never woken"
    wake_text = " ".join(kb_env.wakes[0])
    assert "which three tools" in wake_text
    assert f"hermes kanban answer {ask_id}" in wake_text

    # The dispatcher answers out-of-band (the `kanban answer` verb).
    conn = sqlite3.connect(kb_env.db_path)
    conn.execute(
        "UPDATE clarify_queue SET status='answered', answer=?, answered_at=? "
        "WHERE id=?",
        ("Belt, watch, glasses", int(time.time()), ask_id),
    )
    conn.commit()
    conn.close()

    t.join(timeout=15)
    out = json.loads(result["r"])
    assert out["user_response"] == "Belt, watch, glasses"
    assert out["question"].startswith("TEST-CLARIFY")


def test_dispatcher_timeout_is_an_explicit_non_answer(kb_env, monkeypatch):
    monkeypatch.setattr("tools.clarify_tool.DISPATCHER_ANSWER_TIMEOUT_S", 2)
    agent = SimpleNamespace(
        agent_name="ornith", session_id="sess_w2", clarify_callback=None
    )
    out = json.loads(
        clarify_tool(
            question="Q?",
            callback=lambda q, c, multi_select=False: "IMPROVISED",
            agent=agent,
            task_id="t_e4812748",
        )
    )
    assert out["user_response"] == ""
    assert "NOT a user answer" in out["error"]
    assert "do not" in out["error"].lower()


def test_self_dispatched_clarify_keeps_interactive_path(kb_env):
    # creator == assignee → the human is the dispatcher (legacy path).
    conn = sqlite3.connect(kb_env.db_path)
    conn.execute("UPDATE tasks SET created_by='ornith' WHERE id='t_e4812748'")
    conn.commit()
    conn.close()
    agent = SimpleNamespace(
        agent_name="ornith", session_id="sess_w3", clarify_callback=None
    )
    out = json.loads(
        clarify_tool(
            question="Q?",
            callback=lambda q, c, multi_select=False: "my answer",
            agent=agent,
            task_id="t_e4812748",
        )
    )
    assert out["user_response"] == "my answer"
    assert not kb_env.wakes


def test_human_creator_stays_interactive(kb_env):
    # 'ekl' is not a profile — no wake, the human at the terminal answers.
    conn = sqlite3.connect(kb_env.db_path)
    conn.execute("UPDATE tasks SET created_by='ekl' WHERE id='t_e4812748'")
    conn.commit()
    conn.close()
    assert (
        _route_clarify_to_dispatcher("t_e4812748", None, "ask99", "Q?", "ornith")
        is None
    )
    assert not kb_env.wakes


def test_no_task_means_no_dispatcher(kb_env):
    assert (
        _route_clarify_to_dispatcher("t_nonexistent", None, "ask98", "Q?", "ornith")
        is None
    )


def test_kanban_answer_verb_records_the_answer(kb_env):
    conn = sqlite3.connect(kb_env.db_path)
    conn.execute(
        "INSERT INTO clarify_queue(id,session_id,task_id,agent_name,question,"
        "status,created_at) VALUES(?,?,?,?,?,?,?)",
        ("ask1", "s", "t_e4812748", "ornith", "Q?", "pending", int(time.time())),
    )
    conn.commit()
    conn.close()
    from hermes_cli import kanban as kb_cli

    rc = kb_cli._cmd_answer(
        argparse.Namespace(target="t_e4812748", text="Belt, watch, glasses")
    )
    assert rc == 0
    conn = sqlite3.connect(kb_env.db_path)
    try:
        row = conn.execute(
            "SELECT status, answer FROM clarify_queue WHERE id='ask1'"
        ).fetchone()
    finally:
        conn.close()
    assert row == ("answered", "Belt, watch, glasses")


def test_kanban_answer_verb_rejects_unknown_target(kb_env):
    from hermes_cli import kanban as kb_cli

    rc = kb_cli._cmd_answer(argparse.Namespace(target="t_nope", text="hi"))
    assert rc == 1


# ---------------------------------------------------------------------------
# `plan clarify` from a raw kanban card (t_31094219 re-test, 2026-10-20).
# Evan: "It is supposed to work from a kanban task." The plan command must
# resolve the card's dispatcher WITHOUT an enclosing plan binding.
# ---------------------------------------------------------------------------


def _open_conn(db_path):
    import sqlite3 as _sq

    return _sq.connect(db_path)


def _stub_worker_session(monkeypatch, task_id):
    import contextlib

    import hermes_state

    fake_sdb = SimpleNamespace(
        _read_ctx=lambda: contextlib.nullcontext(
            SimpleNamespace(
                execute=lambda *a, **k: SimpleNamespace(
                    fetchone=lambda: (task_id,) if task_id else None
                )
            )
        ),
        close=lambda: None,
        find_task_session_id=lambda tid: None,
    )
    monkeypatch.setattr(hermes_state, "SessionDB", lambda *a, **k: fake_sdb)


def test_plan_clarify_from_raw_card_routes_to_dispatcher(kb_env, monkeypatch):
    import tools.plan_binding_adapter as pba

    # The exact failure shape from the t_31094219 run: no plan binding.
    monkeypatch.setattr(
        pba, "_current", lambda conn, agent: (None, None, "ERROR: No active task")
    )
    monkeypatch.setattr(
        pba,
        "_legacy",
        lambda: SimpleNamespace(_get_kanban_db=lambda: _open_conn(kb_env.db_path)),
    )
    _stub_worker_session(monkeypatch, "t_e4812748")

    agent = SimpleNamespace(
        agent_name="ornith", session_id="sess_p1", clarify_callback=None
    )
    result = {}

    def run():
        result["r"] = pba.cmd_clarify(agent, "PLAN-CLARIFY — which three tools?")

    t = threading.Thread(target=run, daemon=True)
    t.start()
    ask_id = None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not ask_id:
        ask_id = _pending_ask_id(kb_env.db_path)
        if not ask_id:
            time.sleep(0.1)
    assert ask_id, "plan clarify on a raw card never wrote the ask"
    wake_text = " ".join(kb_env.wakes[0])
    assert "PLAN-CLARIFY" in wake_text
    assert f"hermes kanban answer {ask_id}" in wake_text

    conn = sqlite3.connect(kb_env.db_path)
    conn.execute(
        "UPDATE clarify_queue SET status='answered', answer=?, answered_at=? "
        "WHERE id=?",
        ("Belt, watch, glasses", int(time.time()), ask_id),
    )
    conn.commit()
    conn.close()

    t.join(timeout=15)
    # cmd_clarify returns the dispatcher's answer text — not the JSON envelope.
    assert result["r"] == "Belt, watch, glasses"


def test_plan_clarify_without_card_still_errors(kb_env, monkeypatch):
    import tools.plan_binding_adapter as pba

    monkeypatch.setattr(
        pba, "_current", lambda conn, agent: (None, None, "ERROR: No active task")
    )
    monkeypatch.setattr(
        pba,
        "_legacy",
        lambda: SimpleNamespace(_get_kanban_db=lambda: _open_conn(kb_env.db_path)),
    )
    _stub_worker_session(monkeypatch, None)
    agent = SimpleNamespace(
        agent_name="ornith", session_id="sess_p2", clarify_callback=None
    )
    out = pba.cmd_clarify(agent, "Q?")
    assert out == "ERROR: No active task"
    assert not kb_env.wakes


def test_plan_clarify_raw_card_refuses_double_ask(kb_env, monkeypatch):
    import tools.plan_binding_adapter as pba

    monkeypatch.setattr(
        pba, "_current", lambda conn, agent: (None, None, "ERROR: No active task")
    )
    monkeypatch.setattr(
        pba,
        "_legacy",
        lambda: SimpleNamespace(_get_kanban_db=lambda: _open_conn(kb_env.db_path)),
    )
    _stub_worker_session(monkeypatch, "t_e4812748")
    conn = sqlite3.connect(kb_env.db_path)
    conn.execute(
        "INSERT INTO clarify_queue(id,session_id,task_id,agent_name,question,"
        "status,created_at) VALUES(?,?,?,?,?,?,?)",
        ("pending1", "s", "t_e4812748", "ornith", "Q?", "pending", int(time.time())),
    )
    conn.commit()
    conn.close()
    agent = SimpleNamespace(
        agent_name="ornith", session_id="sess_p3", clarify_callback=None
    )
    out = pba.cmd_clarify(agent, "Q2?")
    assert "pending clarify ask" in out
