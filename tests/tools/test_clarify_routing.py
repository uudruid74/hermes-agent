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


def test_queue_row_links_the_real_task_id(kb_env, monkeypatch):
    """t_936962bd retest4: the tool kwarg carried a SESSION id and the ask
    row linked IT — so `WHERE task_id='t_…'` and `hermes kanban answer t_…`
    found nothing. The row must link the real kanban task."""
    # the worker session's task link names the kanban card
    import contextlib

    import hermes_state

    fake_sdb = SimpleNamespace(
        _read_ctx=lambda: contextlib.nullcontext(
            SimpleNamespace(
                execute=lambda *a, **k: SimpleNamespace(
                    fetchone=lambda: ("t_e4812748",)
                )
            )
        ),
        close=lambda: None,
        find_task_session_id=lambda tid: None,
    )
    monkeypatch.setattr(hermes_state, "SessionDB", lambda *a, **k: fake_sdb)
    monkeypatch.setattr("tools.clarify_tool.DISPATCHER_ANSWER_TIMEOUT_S", 2)
    agent = SimpleNamespace(
        agent_name="ornith", session_id="sess_link1", clarify_callback=None
    )
    result = {}

    def run():
        result["r"] = clarify_tool(
            question="link me",
            callback=lambda q, c, multi_select=False: "ans",
            agent=agent,
            task_id="20261020_104830_f21427",  # a session/isolation id
        )

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=15)
    conn = sqlite3.connect(kb_env.db_path)
    try:
        row = conn.execute(
            "SELECT task_id FROM clarify_queue WHERE question='link me'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "the ask row was never written"
    assert row[0] == "t_e4812748", f"ask linked to {row[0]!r}, not the kanban task"


def test_human_creator_falls_back_to_origin_dispatcher(kb_env):
    """t_936962bd retest4: CLI-created cards carry created_by='user' — not a
    profile — and the ask used to fall through to the interactive callback
    (headless stand-down) instead of reaching the card's origin session."""
    conn = sqlite3.connect(kb_env.db_path)
    conn.execute("UPDATE tasks SET created_by='user' WHERE id='t_e4812748'")
    conn.execute(
        "INSERT INTO task_comments(task_id, author, body, created_at) "
        "VALUES(?,?,?,?)",
        (
            "t_e4812748",
            "system",
            '__kanban_origin__{"platform": "session", "chat_id": '
            '"20261020_001937_450e6ab9", "thread_id": "", "chat_type": "", '
            '"profile": "gopher"}',
            int(time.time()),
        ),
    )
    conn.commit()
    conn.close()
    target = _route_clarify_to_dispatcher(
        "t_e4812748", None, "ask_origin", "Q?", "ornith"
    )
    assert target == "gopher"
    assert kb_env.wakes, "the origin dispatcher was never woken"
    assert "Q?" in " ".join(kb_env.wakes[0])


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


def test_every_clarify_dispatch_door_passes_agent_and_task_id():
    """RC5 (t_125c8b8d retest3): the SEQUENTIAL tool door called
    ``clarify_tool`` without ``agent``/``task_id`` while the concurrent
    ``invoke_tool`` door passed both — so a worker run degraded to the bare
    interactive callback (no queue row, no dispatcher routing) depending on
    which execution path ran. Both doors must carry the same contract; this
    test reads the call sites so the doors cannot drift apart again."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    for rel in ("agent/tool_executor.py", "agent/agent_runtime_helpers.py"):
        src = (root / rel).read_text(encoding="utf-8")
        for m in re.finditer(r"_clarify_tool\(", src):
            depth, end = 1, m.end()
            while depth and end < len(src):
                depth += {"(": 1, ")": -1}.get(src[end], 0)
                end += 1
            call = src[m.start():end]
            assert "agent=agent" in call, f"{rel}: clarify call missing agent="
            assert "task_id=" in call, f"{rel}: clarify call missing task_id="


# ---------------------------------------------------------------------------
# `plan clarify` is GONE (t_630b00b0, Evan 2026-10-20: "Get the god damn
# 'plan clarify' out of there"). There is ONE clarify surface -- the bare
# `clarify` tool tested above -- matching the standard Hermes tool call.
# ---------------------------------------------------------------------------


def test_plan_clarify_is_gone():
    import tools.plan_binding_adapter as pba
    from tools import plan

    assert not hasattr(pba, "cmd_clarify")
    assert not hasattr(pba, "_raw_card_clarify")
    # The plan schema no longer offers the command at all.
    enum = plan.PLAN_SCHEMA["parameters"]["properties"]["command"]["enum"]
    assert "clarify" not in enum

    class _Bare:
        clarify_callback = None

    result = plan.plan(_Bare(), "clarify")
    assert "Unknown plan command 'clarify'" in result
