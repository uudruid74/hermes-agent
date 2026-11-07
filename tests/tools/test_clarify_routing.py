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

from hermes_cli.kanban_db import _review_ask_target, sticky_dispatcher_for_task
from tools.clarify_tool import (
    _route_clarify_to_dispatcher,
    clarify_tool,
)
from tools.plan_binding_adapter import _dispatcher_for_task


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


def test_plan_bound_worker_clarify_routes_via_binding(kb_env):
    """Evan 2026-10-27 (t_3b2474c5): a plan-bound worker carries its task in
    the execution binding, NOT in sessions.task_id (which is NULL). Before
    the binding fallback the dispatcher route was starved of a task id and
    the ask fell through to the interactive path — landing on the human's
    DM instead of reaching the dispatcher. The tool kwarg ``task_id`` is
    deliberately absent here: that is exactly ornith's call shape."""
    conn = sqlite3.connect(kb_env.db_path)
    now = int(time.time())
    conn.execute(
        "INSERT INTO execution_bindings(profile, root_session_id, task_id, "
        "revision, bound_at, updated_at) VALUES(?,?,?,?,?,?)",
        ("ornith", "sess_root_w1", "t_e4812748", 1, now, now),
    )
    conn.commit()
    conn.close()

    class FakeSessionDB:
        def get_compression_root(self, session_id):
            return "sess_root_w1"

    agent = SimpleNamespace(
        agent_name="ornith",
        profile_name="ornith",
        session_id="sess_w1",
        clarify_callback=None,
        _session_db=FakeSessionDB(),
    )
    result = {}

    def run():
        result["r"] = clarify_tool(
            question="TEST-CLARIFY — binding fallback routes me to the dispatcher",
            callback=lambda q, c, multi_select=False: "IMPROVISED",
            agent=agent,
        )

    t = threading.Thread(target=run, daemon=True)
    t.start()

    # Poll the queue row by content (not by task link): the row is written
    # before routing regardless, and the red shape is precisely a row whose
    # task_id is blank because no task id was ever resolved.
    row = None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and row is None:
        c2 = sqlite3.connect(kb_env.db_path)
        try:
            row = c2.execute(
                "SELECT id, task_id FROM clarify_queue ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        finally:
            c2.close()
        if row is None:
            time.sleep(0.1)
    assert row, "the durable clarify_queue row was never written"

    # The bug shape: the interactive callback answered instantly and the
    # dispatcher was never woken at all.
    assert kb_env.wakes, (
        "dispatcher never woken — the ask fell to the interactive path "
        "(this is the human-DM bug)"
    )
    wake_text = " ".join(kb_env.wakes[0])
    assert "binding fallback" in wake_text
    assert "/review t_e4812748 <your answer>" in wake_text

    # The queue row is linked to the REAL kanban task, not blank.
    assert row[1] == "t_e4812748", f"queue row task_id is {row[1]!r}, not t_e4812748"

    # The dispatcher answers through /review; the worker gets the real
    # answer — never the interactive fallback string.
    from hermes_cli.plan_slash_helpers import plan_review_command

    ok, msg = plan_review_command("t_e4812748 yes — binding")
    assert ok, msg
    t.join(timeout=15)
    out = json.loads(result["r"])
    assert out["user_response"] == "yes — binding"
    assert out["question"].startswith("TEST-CLARIFY")


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

    # The wake carried the question and the /review answer instruction
    # (Evan 2026-10-20: the answer channel is /review — NOT a new command).
    # t_ad4b4a96 (2026-11-06 05:52 repro): the ask must ALSO name the tool
    # form the agent-dispatcher actually runs — the wake command's payload
    # routes through `plan review`'s `response`, and the agent answering the
    # named channel hit "has none pending" before this fix.
    assert kb_env.wakes, "the dispatcher was never woken"
    wake_text = " ".join(kb_env.wakes[0])
    assert "which three tools" in wake_text
    assert f"/review t_e4812748 <your answer>" in wake_text
    assert "plan review task_id=t_e4812748 response=" in wake_text
    assert "hermes kanban answer" not in wake_text

    # The dispatcher answers through the review command (/review flow).
    from hermes_cli.plan_slash_helpers import plan_review_command

    ok, msg = plan_review_command("t_e4812748 Belt, watch, glasses")
    assert ok, msg
    assert "delivered" in msg.lower()

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


def test_creator_asked_clarify_keeps_the_button_path(kb_env, monkeypatch):
    """t_c02fc2cd plan approval: the CREATOR raises the ask on a task it
    delegated to a worker (created_by != assignee), so the dispatcher
    resolves to the creator — the ASKER itself. The wake would land back in
    the asker's own session as slash-command text and the human addressee
    never sees the choices. The ask must keep the interactive path, where
    the callback receives the choices and the platforms render BUTTONS
    (Evan 2026-10-22: "I could click buttons before. Now I have to use
    slash commands.")."""
    # Without the guard the ask parks in the dispatcher-wait loop until the
    # answer window expires — keep that window tiny so the pre-fix case
    # fails fast (timeout non-answer) instead of hanging the suite.
    monkeypatch.setattr("tools.clarify_tool.DISPATCHER_ANSWER_TIMEOUT_S", 2)
    agent = SimpleNamespace(
        agent_name="gopher", session_id="sess_self", clarify_callback=None
    )
    seen = {}

    def cb(q, c, multi_select=False):
        seen["question"], seen["choices"] = q, c
        return "Approve"

    out = json.loads(
        clarify_tool(
            question="Approve plan t_x?",
            choices=["Approve", "Deny", "Yolo"],
            callback=cb,
            agent=agent,
            task_id="t_e4812748",
        )
    )
    assert out["user_response"] == "Approve"
    # The options reached the UI layer intact — this is what renders buttons.
    assert seen["choices"] == ["Approve", "Deny", "Yolo"]
    # No text wake: the asker IS the dispatcher; the human is downstream of
    # the asker's interactive callback.
    assert not kb_env.wakes
    # The durable ask is still answered for the /review / dashboard channel.
    conn = sqlite3.connect(kb_env.db_path)
    try:
        row = conn.execute(
            "SELECT status FROM clarify_queue WHERE task_id='t_e4812748' "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row and row[0] == "answered"


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
    row linked IT — so `WHERE task_id='t_…'` and the /review answer
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


def test_review_command_answers_the_pending_ask(kb_env):
    """The /review flow records the answer and the parked worker gets it
    verbatim — the channel spec's \"the response string is literally what the
    worker receives\"."""
    from hermes_cli.plan_slash_helpers import plan_review_command

    conn = sqlite3.connect(kb_env.db_path)
    conn.execute(
        "INSERT INTO clarify_queue(id,session_id,task_id,agent_name,question,"
        "status,created_at) VALUES(?,?,?,?,?,?,?)",
        ("ask1", "s", "t_e4812748", "ornith", "Q?", "pending", int(time.time())),
    )
    conn.commit()
    conn.close()
    ok, msg = plan_review_command("t_e4812748 Belt, watch, glasses")
    assert ok, msg
    conn = sqlite3.connect(kb_env.db_path)
    try:
        row = conn.execute(
            "SELECT status, answer FROM clarify_queue WHERE id='ask1'"
        ).fetchone()
    finally:
        conn.close()
    assert row == ("answered", "Belt, watch, glasses")

def test_review_command_prompts_for_the_answer(kb_env):
    """Dual-mode render: a pending ask reads as \"answer this clarifying
    question about task X\" and teaches the /review answer form."""
    from hermes_cli.plan_slash_helpers import plan_review_command

    conn = sqlite3.connect(kb_env.db_path)
    conn.execute(
        "INSERT INTO clarify_queue(id,session_id,task_id,agent_name,question,"
        "status,created_at) VALUES(?,?,?,?,?,?,?)",
        ("ask2", "s", "t_e4812748", "ornith", "which three tools?", "pending",
         int(time.time())),
    )
    conn.commit()
    conn.close()
    ok, text = plan_review_command("t_e4812748")
    assert ok, text
    assert "answer this clarifying question about task t_e4812748" in text
    assert "which three tools?" in text
    assert "/review t_e4812748 <your answer>" in text
    assert "hermes kanban answer" not in text

def test_review_command_rejects_answer_without_pending_ask(kb_env):
    from hermes_cli.plan_slash_helpers import plan_review_command

    ok, text = plan_review_command("t_e4812748 stray answer text")
    assert not ok
    assert "no pending clarifying question" in text

def test_review_command_rejects_unknown_task(kb_env):
    from hermes_cli.plan_slash_helpers import plan_review_command

    ok, text = plan_review_command("t_nope hi")
    assert not ok
    assert "not found" in text

def test_kanban_answer_verb_is_gone():
    """`hermes kanban answer` was invented during the clarify saga without
    Evan's permission (2026-10-20: \"NO NEW COMMANDS\"). The answer channel
    is /review — the unapproved verb must not exist anywhere."""
    from hermes_cli import kanban as kb_cli

    assert not hasattr(kb_cli, "_cmd_answer")

    parent = argparse.ArgumentParser()
    sub = parent.add_subparsers(dest="top")
    parser = kb_cli.build_parser(sub)
    with pytest.raises(SystemExit):
        parser.parse_args(["kanban", "answer", "t_e4812748", "hi"])


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


# ---------------------------------------------------------------------------
# Sticky dispatcher resolution (t_cc772a4c): the dispatcher is fixed at
# assignment time and sticks through nesting depth — "when he is assigned to
# a plan, his dispatching agent sticks" (Evan, 2026-10-25).  The chain is
# recursive (ancestor walk), never hardcoded.
# ---------------------------------------------------------------------------

def _seed_chain(db_path, rows):
    """Insert bare tasks: rows of (id, created_by, assignee, previous_task)."""
    conn = sqlite3.connect(db_path)
    try:
        for task_id, created_by, assignee, previous_task in rows:
            conn.execute(
                "INSERT INTO tasks(id, title, status, created_by, assignee,"
                " previous_task, board, created_at)"
                " VALUES(?,?,?,?,?,?,'default',?)",
                (
                    task_id,
                    f"task {task_id}",
                    "manual",
                    created_by,
                    assignee,
                    previous_task,
                    int(time.time()),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _resolvers_agree(db_path, task_id):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return (
            _dispatcher_for_task(conn, task_id),
            _review_ask_target(conn, task_id, direction="to-dispatcher"),
            sticky_dispatcher_for_task(conn, task_id),
        )
    finally:
        conn.close()


def test_sticky_dispatcher_walks_previous_task_to_the_parent(kb_env):
    # Parent: zephyr dispatched ornith.  Child: ornith created its own plan
    # while bound to the parent (created_by == assignee == ornith) — the
    # child has NO dispatcher of its own and must inherit zephyr's.
    _seed_chain(
        kb_env.db_path,
        [
            ("t_parent", "zephyr", "ornith", None),
            ("t_child", "ornith", "ornith", "t_parent"),
        ],
    )
    assert _resolvers_agree(kb_env.db_path, "t_child") == ("zephyr",) * 3


def test_sticky_dispatcher_holds_at_depth_three(kb_env):
    _seed_chain(
        kb_env.db_path,
        [
            ("t_parent", "zephyr", "ornith", None),
            ("t_child", "ornith", "ornith", "t_parent"),
            ("t_grandchild", "ornith", "ornith", "t_child"),
        ],
    )
    assert _resolvers_agree(kb_env.db_path, "t_grandchild") == ("zephyr",) * 3


def test_sticky_dispatcher_none_for_human_created_task(kb_env):
    # Top-level, created_by='user', assignee=zephyr: a human creator is not a
    # wake target — the human gate stays (Evan approves these himself).
    _seed_chain(kb_env.db_path, [("t_human", "user", "zephyr", None)])
    assert _resolvers_agree(kb_env.db_path, "t_human") == (None,) * 3


def test_sticky_dispatcher_none_for_self_dispatch(kb_env):
    # A worker's own top-level plan: creator IS the assignee, no ancestor,
    # no origin — no AI dispatcher, the interactive callback keeps the
    # choices/buttons.
    _seed_chain(kb_env.db_path, [("t_self", "ornith", "ornith", None)])
    assert _resolvers_agree(kb_env.db_path, "t_self") == (None,) * 3


def test_sticky_dispatcher_cycle_guard_does_not_hang(kb_env):
    # previous_task loop (including a self-loop) must terminate, not hang.
    _seed_chain(
        kb_env.db_path,
        [
            ("t_loop_a", "ornith", "ornith", "t_loop_b"),
            ("t_loop_b", "ornith", "ornith", "t_loop_a"),
            ("t_self_loop", "ornith", "ornith", "t_self_loop"),
        ],
    )
    assert _resolvers_agree(kb_env.db_path, "t_loop_a") == (None,) * 3
    assert _resolvers_agree(kb_env.db_path, "t_loop_b") == (None,) * 3
    assert _resolvers_agree(kb_env.db_path, "t_self_loop") == (None,) * 3


def test_sticky_dispatcher_durable_stamp_survives_parent_loss(kb_env):
    # D2: the stamp is read FIRST, so the dispatcher sticks even when the
    # ancestor chain is gone (parent archived/deleted, board moved, depth).
    _seed_chain(kb_env.db_path, [("t_orphan", "ornith", "ornith", "t_gone")])
    conn = sqlite3.connect(kb_env.db_path)
    try:
        conn.execute(
            "UPDATE tasks SET dispatcher='zephyr' WHERE id='t_orphan'"
        )
        conn.commit()
    finally:
        conn.close()
    assert _resolvers_agree(kb_env.db_path, "t_orphan") == ("zephyr",) * 3
