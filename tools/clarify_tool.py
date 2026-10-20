#!/usr/bin/env python3
"""
Clarify Tool Module - Interactive Clarifying Questions

Allows the agent to present structured multiple-choice questions or open-ended
prompts to the user. In CLI mode, choices are navigable with arrow keys. On
messaging platforms, choices are rendered as a numbered list.

Supports both single-select (radio) and multi-select (checkbox) modes via the
``multi_select`` parameter.

The actual user-interaction logic lives in the platform layer (cli.py for CLI,
gateway/run.py for messaging). This module defines the schema, validation, and
a thin dispatcher that delegates to a platform-provided callback.
"""

import json
from typing import List, Optional, Callable


# Maximum number of predefined choices the agent can offer.
# A 5th "Other (type your answer)" option is always appended by the UI.
MAX_CHOICES = 4

# How long a dispatcher-routed clarify waits for the answer recorded in
# clarify_queue (``hermes kanban answer`` / the dashboard). Bounded so a
# vanished dispatcher can't hang a worker forever — on expiry the outcome is
# reported as an explicit NON-answer, never as a user response.
DISPATCHER_ANSWER_TIMEOUT_S = 900


def _flatten_choice(c) -> str:
    """Coerce a single choice into its user-facing display string.

    The schema declares choices as bare strings, but LLMs sometimes emit
    dict-shaped choices like ``[{"description": "..."}]``. A naive ``str(c)``
    turns the whole dict into its Python repr — ``{'description': '...'}`` —
    which then leaks onto every surface that renders the choice (CLI panel,
    Discord buttons, Telegram numbered list) AND is returned verbatim as the
    user's answer. Normalising here, at the one platform-agnostic entry point,
    fixes the whole class in one place instead of per-adapter.

    Dict unwrap order is the canonical LLM tool-call user-facing keys:
    ``label`` → ``description`` → ``text`` → ``title``. ``name`` and ``value``
    are deliberately excluded — they're component-shaped fields that could
    carry raw enum values or short identifiers, not human-readable labels. A
    dict with none of the canonical keys is dropped (returns ""), since a
    garbage label is worse than no choice at all.
    """
    if c is None:
        return ""
    if isinstance(c, str):
        return c.strip()
    if isinstance(c, dict):
        for key in ("label", "description", "text", "title"):
            v = c.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""
    if isinstance(c, (list, tuple)):
        return " ".join(_flatten_choice(x) for x in c).strip()
    return str(c).strip()


def _invoke_callback(callback, question, choices, multi_select):
    """Invoke the platform callback, passing multi_select if supported.

    Uses signature inspection (not a ``TypeError`` retry) to decide whether
    the callback accepts the ``multi_select`` keyword — a retry-on-TypeError
    approach would re-invoke a *compatible* callback that raised TypeError
    internally, potentially prompting the user twice.
    """
    import inspect

    accepts_multi = False
    try:
        sig = inspect.signature(callback)
        params = sig.parameters
        accepts_multi = "multi_select" in params or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
    except (TypeError, ValueError):
        # Builtins / C callables without introspectable signatures:
        # be conservative and use the legacy 2-arg form.
        accepts_multi = False

    if accepts_multi:
        return callback(question, choices, multi_select=multi_select)
    return callback(question, choices)


def _parse_multi_select_response(raw_response) -> List[str]:
    """Parse a multi-select response into a list of cleaned choice strings.

    Handles three forms:
      - Already a list  →  stringify + strip each element
      - JSON array      →  parse and strip
      - Comma-separated →  split, strip, drop empties
    """
    if isinstance(raw_response, list):
        return [str(r).strip() for r in raw_response if str(r).strip()]

    raw = str(raw_response).strip()

    # Try JSON array
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(p).strip() for p in parsed if str(p).strip()]
        except json.JSONDecodeError:
            pass

    # Fall back to comma-separated
    return [s.strip() for s in raw.split(",") if s.strip()]


def _route_clarify_to_dispatcher(
    task_id,
    session_task_id,
    clarify_id: str,
    question: str,
    agent_name: str,
) -> Optional[str]:
    """Route a worker's clarify ask to the task's dispatcher (F11 base).

    Raw kanban cards never had this path — the ask went straight to the
    interactive callback, which in a headless worker times out and its
    stand-down fallback reads like a real user answer (t_e4812748, where the
    worker claimed "dispatcher answered" on the fallback string). The ask now
    goes through ``send_dispatcher_message`` — the durable ``review-clarify``
    event FIRST, then the use-time wake-ping to the creator/origin — matching
    the plan-card F2 channel.

    Returns the dispatcher's wake recipient, or None when no dispatcher
    resolves (self-dispatch, human creator, no task) — the caller then keeps
    the legacy interactive path.
    """
    import sqlite3
    import sys

    from hermes_cli.kanban_db import (
        _review_ask_target,
        kanban_db_path,
        send_dispatcher_message,
    )

    try:
        conn = sqlite3.connect(str(kanban_db_path()), timeout=5)
    except sqlite3.Error as exc:
        print(f"CLARIFY_DBG: dispatcher routing db open failed: {exc}", file=sys.stderr)
        return None
    try:
        conn.row_factory = sqlite3.Row
        tid = None
        for cand in (task_id, session_task_id):
            cand = str(cand or "").strip()
            if cand and conn.execute(
                "SELECT 1 FROM tasks WHERE id=?", (cand,)
            ).fetchone():
                tid = cand
                break
        if not tid:
            return None
        recipient = _review_ask_target(conn, tid, direction="to-dispatcher")
        if not recipient:
            return None
        # Only an agent profile can be woken; a human creator stays on the
        # interactive path (the human IS the dispatcher there).
        from hermes_cli.profiles import profile_exists

        if not profile_exists(recipient):
            return None
        ask = (
            f"{tid} → your worker ({agent_name}) is asking for clarification.\n\n"
            f"Question: {question}\n\n"
            f"Answer it with:\n  hermes kanban answer {clarify_id} \"<your answer>\"\n"
            "(or the dashboard). The worker is parked until the answer arrives."
        )
        ok, detail = send_dispatcher_message(
            conn,
            tid,
            direction="to-dispatcher",
            kind="clarify",
            text=ask,
            actor=agent_name or "worker",
        )
        if not ok:
            print(f"CLARIFY_DBG: dispatcher ask failed: {detail}", file=sys.stderr)
            return None
        print(f"CLARIFY_DBG: {detail}", file=sys.stderr)
        return recipient
    except sqlite3.Error as exc:
        print(f"CLARIFY_DBG: dispatcher routing failed: {exc}", file=sys.stderr)
        return None
    finally:
        conn.close()


def _wait_for_dispatcher_answer(
    clarify_id: str, db_path: str, timeout_s: Optional[float] = None
) -> Optional[str]:
    """Poll clarify_queue for the dispatcher's recorded answer."""
    import sqlite3
    import time

    if timeout_s is None:
        timeout_s = DISPATCHER_ANSWER_TIMEOUT_S
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        row = None
        try:
            chk = sqlite3.connect(db_path, timeout=2)
            try:
                row = chk.execute(
                    "SELECT answer FROM clarify_queue WHERE id=? AND status='answered'",
                    (clarify_id,),
                ).fetchone()
            finally:
                chk.close()
        except sqlite3.Error:
            row = None
        if row and row[0] is not None:
            return str(row[0])
        time.sleep(1.0)
    return None


def clarify_tool(
    question: str,
    choices: Optional[List[str]] = None,
    multi_select: bool = False,
    callback: Optional[Callable] = None,
    agent=None,
    task_id: Optional[str] = None,
) -> str:
    """
    Ask the user a question, optionally with multiple-choice options.

    Args:
        question:     The question text to present.
        choices:      Up to 4 predefined answer choices. When omitted the
                      question is purely open-ended.
        multi_select: When True, the user can select multiple choices
                      (checkboxes).  The ``user_response`` in the output JSON
                      will be a list of strings instead of a single string.
                      Has no effect when ``choices`` is omitted.
        callback:     Platform-provided function that handles the actual UI
                      interaction.  Signature:
                      ``callback(question, choices, multi_select=False) -> str``.
                      The optional ``multi_select`` keyword is passed so the
                      platform can render checkboxes instead of radio buttons.
                      Injected by the agent runner (cli.py / gateway).
        task_id:      Internal override for the task linked to the queue row.
                      Plan approval uses this before the new task is bound to
                      the session.

    Returns:
        JSON string with the user's response.
    """
    import time
    if not question or not question.strip():
        return tool_error("Question text is required.")

    question = question.strip()

    # Validate and trim choices
    if choices is not None:
        if not isinstance(choices, list):
            return tool_error("choices must be a list of strings.")
        # LLMs sometimes emit dict-shaped choices (e.g. [{"description": "..."}])
        # instead of bare strings. _flatten_choice unwraps them to their
        # user-facing text here — the single platform-agnostic entry point —
        # so the CLI panel, Discord buttons, and Telegram list all render clean
        # text and the resolved answer is never a raw Python dict repr.
        choices = [s for s in (_flatten_choice(c) for c in choices) if s]
        if len(choices) > MAX_CHOICES:
            choices = choices[:MAX_CHOICES]
        if not choices:
            choices = None  # empty list → open-ended

    # NOTE: the "callback required" gate runs AFTER the dispatcher routing
    # attempt below — a dispatcher-routed ask never needs the interactive
    # callback (headless workers call clarify with no callback context; the
    # t_31094219 re-test hit exactly that).

    # Write to clarify_queue so dashboard can pick it up
    clarify_id = None
    session_task_id = None
    if agent is not None:
        import uuid, time, os
        from hermes_cli.kanban_db import kanban_db_path
        import sqlite3
        db = sqlite3.connect(str(kanban_db_path()))
        db.execute("PRAGMA journal_mode=WAL")
        clarify_id = uuid.uuid4().hex[:12]
        session_id = getattr(agent, "session_id", None) or os.environ.get("HERMES_SESSION_ID")
        from hermes_state import SessionDB
        sdb = SessionDB()
        row = None
        read_ctx = sdb._read_ctx()
        if read_ctx is not None:
            with read_ctx as ctx:
                row = ctx.execute(  # type: ignore[union-attr]
                    "SELECT task_id FROM sessions WHERE id=?", (session_id,)
                ).fetchone()
        if row:
            session_task_id = row["task_id"] if isinstance(row, dict) else row[0]
        queue_task_id = task_id or session_task_id
        # Link the ask to the REAL kanban task when one exists: the tool
        # kwarg may carry a session/isolation id (t_936962bd retest4 stored
        # the session id in task_id — `hermes kanban answer t_…` and the
        # dashboard then found nothing). Prefer a candidate naming a tasks
        # row.
        try:
            for cand in (task_id, session_task_id):
                c = str(cand or "").strip()
                if c and db.execute("SELECT 1 FROM tasks WHERE id=?", (c,)).fetchone():
                    queue_task_id = c
                    break
        except sqlite3.Error as exc:
            import sys

            print(f"CLARIFY_DBG: task link resolution failed: {exc}", file=sys.stderr)
        agent_name = getattr(agent, "agent_name", "unknown")
        db.execute(
            "INSERT INTO clarify_queue(id,session_id,task_id,agent_name,question,choices,multi_select,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (clarify_id, session_id, queue_task_id, agent_name, question,
             json.dumps(choices) if choices else None,
             1 if multi_select else 0, "pending", int(time.time())))
        db.commit()
        db.close()
        import sys
        print(f"CLARIFY_DBG: inserted {clarify_id} for agent={agent_name}, session={session_id}", file=sys.stderr)

    # Raw-card dispatcher routing (t_e4812748 root cause, 2026-10-20): a
    # worker's clarify on a kanban task must reach the task's creator. The
    # F11 base writes the durable ask FIRST, then wake-pings the dispatcher.
    # With a dispatcher in play the interactive callback is not the addressee
    # and must not decide the answer.
    dispatcher = None
    if clarify_id:
        dispatcher = _route_clarify_to_dispatcher(
            task_id,
            session_task_id,
            clarify_id,
            question,
            getattr(agent, "agent_name", "unknown") if agent is not None else "unknown",
        )

    # The callback is the HUMAN answer channel. A dispatcher-routed ask
    # doesn't need one; refuse (and drop the just-written ask) only when
    # neither answer channel exists.
    if callback is None and not dispatcher:
        if clarify_id:
            import sqlite3 as _sq

            from hermes_cli.kanban_db import kanban_db_path

            try:
                drop = _sq.connect(str(kanban_db_path()))
                try:
                    drop.execute("DELETE FROM clarify_queue WHERE id=?", (clarify_id,))
                    drop.commit()
                finally:
                    drop.close()
            except _sq.Error as exc:
                import sys

                print(f"CLARIFY_DBG: stale-ask cleanup failed: {exc}", file=sys.stderr)
        return tool_error("Clarify tool is not available in this execution context.")

    # Race: callback (WebSocket/CLI) vs DB poll (dashboard).
    # The dashboard writes answers to clarify_queue; the callback blocks on
    # a WebSocket event.  Whichever provides an answer first wins.
    raw_response = None
    if dispatcher and clarify_id:
        from hermes_cli.kanban_db import kanban_db_path

        # Dispatcher-routed ask: the answer arrives out-of-band in the queue
        # (`hermes kanban answer` / the dashboard). On expiry the outcome is
        # an explicit NON-answer — a fallback must never masquerade as one
        # (the t_e4812748 worker claimed success on the stand-down string).
        raw_response = _wait_for_dispatcher_answer(clarify_id, str(kanban_db_path()))
        if raw_response is None:
            return json.dumps({
                "question": question,
                "choices_offered": choices,
                "user_response": "",
                "error": (
                    f"Clarify ask routed to dispatcher {dispatcher}, but no "
                    "answer arrived within the wait window. This is a TIMEOUT, "
                    "NOT a user answer — do not treat it as one and do not "
                    "improvise an answer. Stand down and report the unanswered "
                    "question verbatim."
                ),
            }, ensure_ascii=False)
    elif clarify_id:
        import threading
        _cb_result = [None]
        _cb_done = threading.Event()
        def _run_callback():
            try:
                _cb_result[0] = _invoke_callback(callback, question, choices, multi_select)
            except Exception:
                pass
            _cb_done.set()
        t = threading.Thread(target=_run_callback, daemon=True)
        t.start()
        # Poll DB for dashboard answer while callback runs
        db_path = str(kanban_db_path())
        while not _cb_done.is_set():
            _cb_done.wait(timeout=1.0)
            if _cb_done.is_set():
                break
            # Check if dashboard wrote an answer
            try:
                chk = sqlite3.connect(db_path, timeout=2)
                chk.execute("PRAGMA journal_mode=WAL")
                row = chk.execute(
                    "SELECT answer FROM clarify_queue WHERE id=? AND status='answered'",
                    (clarify_id,)).fetchone()
                chk.close()
                if row and row[0] is not None:
                    raw_response = row[0]
                    import sys
                    print(f"CLARIFY_DBG: dashboard answered {clarify_id}: {raw_response}", file=sys.stderr)
                    break
            except Exception:
                pass
        if raw_response is None:
            raw_response = _cb_result[0]
        # If callback is still blocking (dashboard answered via DB), the
        # thread will eventually time out or get cleaned up — that's fine.
    else:
        raw_response = _invoke_callback(callback, question, choices, multi_select)
    if raw_response is None:
        raw_response = ""

    if clarify_id and raw_response:
        db3 = sqlite3.connect(str(kanban_db_path()))
        db3.execute("UPDATE clarify_queue SET status='answered', answered_at=? WHERE id=?",
                    (int(time.time()), clarify_id))
        db3.commit(); db3.close()
        import sys
        print(f"CLARIFY_DBG: marked {clarify_id} as answered", file=sys.stderr)

    if multi_select and choices is not None:
        user_response = _parse_multi_select_response(raw_response)
    else:
        user_response = str(raw_response).strip()

    return json.dumps({
        "question": question,
        "choices_offered": choices,
        "user_response": user_response,
    }, ensure_ascii=False)


def check_clarify_requirements() -> bool:
    """Clarify tool has no external requirements -- always available."""
    return True


# =============================================================================
# OpenAI Function-Calling Schema
# =============================================================================

CLARIFY_SCHEMA = {
    "name": "clarify",
    "description": (
        "Ask the user a question when you need clarification, feedback, or a "
        "decision before proceeding. Supports three modes:\n\n"
        "1. **Single-select multiple choice** — provide up to 4 choices. The user picks one "
        "or types their own answer via a 5th 'Other' option.\n"
        "2. **Multi-select multiple choice** — set multi_select=true. The user can select "
        "multiple options via checkboxes. user_response will be a list of selected choices.\n"
        "3. **Open-ended** — omit choices entirely. The user types a free-form "
        "response.\n\n"
        "CRITICAL: when you are offering options, put each option ONLY in the "
        "`choices` array — NEVER enumerate the options inside the `question` "
        "text. The UI renders `choices` as selectable rows; options written "
        "into the question string render as dead prose the user can't pick. "
        "Right: question='Which deployment target?', choices=['staging', "
        "'prod']. Wrong: question='Which target? 1) staging 2) prod', choices=[].\n\n"
        "Use this tool when:\n"
        "- The task is ambiguous and you need the user to choose an approach\n"
        "- You want post-task feedback ('How did that work out?')\n"
        "- You want to offer to save a skill or update memory\n"
        "- A decision has meaningful trade-offs the user should weigh in on\n\n"
        "Do NOT use this tool for simple yes/no confirmation of dangerous "
        "commands (the terminal tool handles that). Prefer making a reasonable "
        "default choice yourself when the decision is low-stakes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": (
                    "The question itself, and ONLY the question (e.g. 'Which "
                    "deployment target?'). Do NOT embed the answer options here "
                    "— pass them as separate elements in `choices`."
                ),
            },
            "choices": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": MAX_CHOICES,
                "description": (
                    "REQUIRED whenever you are presenting selectable options: "
                    "each distinct option is its own array element (up to 4). "
                    "The UI renders these as pickable rows and auto-appends an "
                    "'Other (type your answer)' option. Omit this parameter "
                    "entirely ONLY for a genuinely open-ended free-text question."
                ),
            },
            "multi_select": {
                "type": "boolean",
                "description": (
                    "When true, the user can select MULTIPLE options (like checkboxes). "
                    "The user_response will be a list of selected choices. "
                    "When false (default), single selection (radio). "
                    "Has no effect when choices is omitted (open-ended question)."
                ),
            },
        },
        "required": ["question"],
    },
}


# --- Registry ---
from tools.registry import registry, tool_error

registry.register(
    name="clarify",
    toolset="clarify",
    schema=CLARIFY_SCHEMA,
    handler=lambda args, **kw: clarify_tool(
        question=args.get("question", ""),
        choices=args.get("choices"),
        multi_select=args.get("multi_select", False),
        callback=kw.get("callback"),
        agent=kw.get("agent"),
        task_id=kw.get("task_id")),
    check_fn=check_clarify_requirements,
    emoji="❓",
)
