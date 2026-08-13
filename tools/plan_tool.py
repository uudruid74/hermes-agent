"""
plan_tool — Mandatory Action Protocol for Hermes agents.

Commands: new, done, dispatch, remind, fail, approve

Default = Deny All. Without an active task_id in the session, file
writes, cron creation, and kanban task creation are blocked. The Plan
tool creates 'manual' kanban tasks that carry step-by-step plans.

NOTE (t_2b7a77b2): kanban-changes.md lines 53-54 spec a stuck-agent
re-prompt timer and line 236 spec an approval timeout. Neither was
implemented. plan_tool contains zero timer/thread/sleep code. If the
timeout is needed, it should be a separate implementation.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _get_session_db():
    """Lazy import to avoid circular deps."""
    from hermes_state import SessionDB
    return SessionDB()

def _get_kanban_db(board: Optional[str] = None):
    """Return a sqlite3 connection to the kanban database."""
    import sqlite3
    from hermes_cli.kanban_db import kanban_db_path
    conn = sqlite3.connect(str(kanban_db_path(board)))
    conn.row_factory = sqlite3.Row
    return conn

def _resolve_board(board: Optional[str]) -> str:
    """Resolve a board slug for the `board` column, defaulting to 'default'."""
    slug = (board or "").strip()
    return slug if slug else "default"


def _get_agent_name(agent) -> str:
    return getattr(agent, "agent_name", None) or os.environ.get("HERMES_AGENT_NAME", "agent")


def _get_session_id(agent) -> Optional[str]:
    """Canonical session ID: ContextVar first, then env. No agent fallback.

    Must match file_safety._session_task_id() resolution so the write gate
    finds the task_id that plan_tool just wrote.  (f3ee48575 / t_6c68e6de)
    """
    try:
        from gateway.session_context import get_session_env
        sid = get_session_env("HERMES_SESSION_ID")
    except Exception:
        sid = os.environ.get("HERMES_SESSION_ID")
    return sid or None



def _get_task_id(agent) -> Optional[str]:
    """Return the active task_id for this session.

    Session DB is authoritative — it's updated by plan_tool when a new
    sub-plan is created (set_session_task_id) and when a plan completes
    (clear_session_task_id).  Falls back to the HERMES_KANBAN_TASK env
    var for the initial kanban worker boot (before any plan_tool call
    has written to the DB).
    """
    session_id = _get_session_id(agent)
    if session_id:
        try:
            sdb = _get_session_db()
            with sdb._read_ctx() as c:
                row = c.execute(
                    "SELECT task_id FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
            if row and row["task_id"]:
                return row["task_id"]
        except Exception:
            pass
    return os.environ.get("HERMES_KANBAN_TASK")

def _resolve_temp(temp: Optional[str], agent) -> Optional[float]:
    """Resolve symbolic temperature names to floats from config.yaml."""
    if temp is None:
        return None
    try:
        return float(temp)
    except (ValueError, TypeError):
        pass
    # Symbolic: chat, worker, creative
    profile = getattr(agent, "profile_name", None) or os.environ.get("HERMES_PROFILE", "neo")
    config_path = os.path.expanduser(f"~/.hermes/profiles/{profile}/config.yaml")
    try:
        import yaml
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}
        temps = cfg.get("temperature_map", {}) or cfg.get("temperatures", {})
        return temps.get(temp)
    except Exception:
        pass
    # Hard defaults if config missing
    defaults = {"chat": 0.8, "worker": 0.4, "creative": 1.2}
    return defaults.get(temp)


# ---------------------------------------------------------------------------
# command: new
# ---------------------------------------------------------------------------

def _cmd_new(agent, title: str, goal: str, steps: List[str],
             temp: Optional[str] = None, board: Optional[str] = None) -> str:
    """Present a multistep plan for approval via clarify callback."""
    agent_name = _get_agent_name(agent)
    session_id = _get_session_id(agent)
    current_task_id = _get_task_id(agent)

    if not title or not goal or not steps:
        return "ERROR: 'new' requires title, goal, and steps[]"

    resolved_temp = _resolve_temp(temp, agent)

    # Build plan text with architecture headers
    plan_lines = [
        f"## Plan: {title}",
        f"**Agent:** {agent_name}",
        f"**Goal:** {goal}",
        "",
        "### Steps",
    ]
    for i, step in enumerate(steps, 1):
        plan_lines.append(f"{i}. {step}")
    if temp:
        plan_lines.append(f"\n**Temperature:** {temp}")
    if resolved_temp is not None:
        plan_lines.append(f"  → resolved: {resolved_temp}")

    plan_text = "\n".join(plan_lines)
    # Compress runs of 3+ newlines down to 2
    import re
    plan_text = re.sub(r'\n{3,}', '\n\n', plan_text)

    # Generate task ID
    import uuid
    task_id = f"t_{uuid.uuid4().hex[:8]}"

    clarify_cb = getattr(agent, "clarify_callback", None) if agent is not None else None
    if clarify_cb is None:
        return "ERROR: No clarify callback available (agent={}, running in non-interactive context). Cannot present plan for approval.".format(
            type(agent).__name__ if agent else "None")

    # Pre-create task as blocked/approval so denied plans can be re-approved
    current_task_id = _get_task_id(agent)
    kdb = _get_kanban_db(board)
    try:
        with kdb as conn:
            conn.execute("""
                INSERT INTO tasks
                    (id, title, body, status, assignee, created_at,
                     task_steps, task_stepno, task_goal, block_kind,
                     prev_temperature, previous_task, session_id, board)
                VALUES
                    (:id, :title, :body, 'blocked', :assignee, :created_at,
                     :task_steps, 1, :task_goal, 'approval',
                     :prev_temp, :prev_task, :session_id, :board)
            """, {
                "id": task_id, "title": title, "body": plan_text,
                "assignee": agent_name, "created_at": int(time.time()),
                "task_steps": json.dumps(steps), "task_goal": goal,
                "prev_temp": getattr(agent, "_session_temperature", None),
                "prev_task": current_task_id,
                "session_id": session_id,
                "board": _resolve_board(board),
            })
            conn.commit()
    except Exception as e:
        return f"ERROR: Failed to create task: {e}"

    # Present via agent's clarify callback (set by platform runner)
    try:
        user_response = clarify_cb(
            f"Approve plan {task_id}?\n\n{plan_text}",
            ["Approve", "Deny"],
        )
    except Exception as e:
        return f"User unavailable: {e}. Stand down."

    if not user_response:
        return "No response received. Stand down."

    # Detect approval
    response_lower = str(user_response).strip().lower()
    if "appr" in response_lower:
        # User approved — activate task
        current_temp = getattr(agent, "_session_temperature", None)

        kdb = _get_kanban_db()
        try:
            with kdb as conn:
                conn.execute(
                    "UPDATE tasks SET status = 'manual', block_kind = NULL WHERE id = ?",
                    (task_id,),
                )
                conn.commit()
        except Exception as e:
            return f"ERROR: Failed to activate task: {e}"

        # Set session task_id and subject (save old subject first)
        sdb = _get_session_db()
        old_subject = ""
        if session_id:
            # Query the ACTUAL session, not the most recent one in the DB
            try:
                with sdb._read_ctx() as c:
                    row = c.execute(
                        "SELECT subject FROM sessions WHERE id = ?", (session_id,)
                    ).fetchone()
                if row:
                    old_subject = row["subject"] or ""
            except Exception:
                pass
            sdb.set_session_task_id(session_id, task_id)
            sdb.set_session_subject(session_id, title)
            # Store old subject as task comment for restoration
            try:
                kdb = _get_kanban_db()
                with kdb as conn:
                    conn.execute(
                        "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
                        (task_id, agent_name, f"PREV_SUBJECT:{old_subject}", int(time.time())),
                    )
                    conn.commit()
            except Exception:
                pass

        if resolved_temp is not None:
            agent._session_temperature = resolved_temp

        # If running as kanban worker, block parent task
        if current_task_id:
            try:
                with kdb as conn:
                    conn.execute(
                        "UPDATE tasks SET status = 'blocked', block_kind = 'approval' WHERE id = ?",
                        (current_task_id,)
                    )
                    conn.commit()
            except Exception:
                pass

        # Log to fabric
        try:
            from tools.registry import registry
            registry.dispatch("fabric_write", {
                "type": "note",
                "content": f"Plan approved: {title} → {task_id}",
                "summary": f"Plan: {title}",
            })
        except Exception:
            pass

        return (
            f"TASK APPROVED ({task_id}): {title}\n\n"
            f"You are now working on this plan. "
            f"The plan has been recorded. Your next action is:\n\n"
            f">>> STEP 1: {steps[0]} <<<\n\n"
            f"Begin working on Step 1 now. When complete, call "
            f"plan_tool(command=\"done\") to mark it done and advance to the next step.\n"
            f"Do NOT call plan_tool 'done' until Step 1 is actually finished."
        )
    else:
        # User denied — ask for reason
        reason = ""
        try:
            reason = clarify_cb(
                "Reason for denial? (type below or send empty)",
            )
        except Exception:
            pass

        reason_str = str(reason).strip() if reason else "unspecified"

        # Mark as blocked/approval so it can be re-approved later
        try:
            kdb = _get_kanban_db()
            with kdb as conn:
                conn.execute(
                    "UPDATE tasks SET status = 'blocked', block_kind = 'approval' WHERE id = ?",
                    (task_id,),
                )
                conn.commit()
        except Exception:
            pass

        # Log denial to fabric
        try:
            from tools.registry import registry
            registry.dispatch("fabric_write", {
                "type": "note",
                "content": f"Plan denied: {title} — {reason_str}",
                "summary": f"Plan denied: {title}",
            })
        except Exception:
            pass

        return f"Plan denied ({task_id}): {reason_str}. Stand down. Resubmit with plan_tool(command=\"approve\", task_id=\"{task_id}\") to approve."


# ---------------------------------------------------------------------------
# command: done
# ---------------------------------------------------------------------------

def _cmd_done(agent, status: Optional[str] = None) -> str:
    """Mark current step complete. Advance or finish."""
    session_id = _get_session_id(agent)
    if not session_id:
        return "ERROR: No active session"

    sdb = _get_session_db()
    with sdb._read_ctx() as c:
        row = c.execute(
            "SELECT task_id FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
    if not row:
        return "ERROR: Session not found"
    task_id = row["task_id"]
    if not task_id:
        return "ERROR: No active task"

    # Get old subject from PREV_SUBJECT comment saved at plan creation
    old_subject = ""
    kdb = _get_kanban_db()
    with kdb as conn:
        cr = conn.execute(
            "SELECT body FROM task_comments WHERE task_id = ? AND body LIKE 'PREV_SUBJECT:%' ORDER BY created_at DESC LIMIT 1",
            (task_id,)
        ).fetchone()
    if cr:
        body = cr["body"] if isinstance(cr, dict) else cr[0]
        old_subject = body.split(":", 1)[1] if isinstance(body, str) and ":" in body else ""


    kdb = _get_kanban_db()
    with kdb as conn:
        task = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not task:
            return f"ERROR: Task {task_id} not found"

        # Use column names directly — sqlite3.Row supports dict and index access
        try:
            steps_str = task["task_steps"]
        except (KeyError, IndexError):
            steps_str = None
        steps = json.loads(steps_str) if steps_str else []
        stepno = task["task_stepno"]
        if stepno is None:
            stepno = 1
        import logging
        _logger = logging.getLogger("plan_tool")
        _logger.info("PLAN_DONE: task=%s raw_stepno=%s resolved_stepno=%s steps_len=%d db_path=%s",
                     task_id, task["task_stepno"], stepno, len(steps), str(kdb))
        goal = task["task_goal"] or ""
        prev_task = task["previous_task"]
        prev_temp = task["prev_temperature"]

        # Log completion of this step
        note = f"Step {stepno} complete"
        if status:
            note += f" — {status}"
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
            (task_id, _get_agent_name(agent), note, int(time.time())),
        )

        if stepno < len(steps):
            # Advance to next step
            conn.execute(
                "UPDATE tasks SET task_stepno = ? WHERE id = ?",
                (stepno + 1, task_id),
            )
            conn.commit()
            _logger.info("PLAN_DONE: updated task_stepno %d -> %d", stepno, stepno + 1)
            # Verify the write
            verify = conn.execute("SELECT task_stepno FROM tasks WHERE id = ?", (task_id,)).fetchone()
            _logger.info("PLAN_DONE: verify task_stepno=%s", verify["task_stepno"] if verify else "NONE")
            return f"Complete Step {stepno + 1}: {steps[stepno]}"
        else:
            # All steps done — complete the task
            conn.execute(
                "UPDATE tasks SET status = 'done', completed_at = ?, task_stepno = NULL WHERE id = ?",
                (int(time.time()), task_id),
            )
            conn.commit()

    # Restore previous task
    if prev_task:
        sdb.set_session_task_id(session_id, prev_task)
        if prev_temp is not None:
            agent._session_temperature = prev_temp

        # Unblock parent task so the dispatcher/agent can resume it
        try:
            with kdb as conn:
                conn.execute(
                    "UPDATE tasks SET status = 'running', block_kind = NULL "
                    "WHERE id = ? AND status = 'blocked'",
                    (prev_task,),
                )
                conn.commit()
        except Exception:
            pass

        # Get parent task info
        with kdb as conn:
            parent = conn.execute(
                "SELECT title, task_goal FROM tasks WHERE id = ?", (prev_task,)
            ).fetchone()
        parent_title = parent["title"] if parent else prev_task
        parent_goal = parent["task_goal"] or "" if parent else ""

        # Log via session
        sdb.set_session_subject(session_id, parent_title)
        try:
            from tools.registry import registry
            registry.dispatch("fabric_write", {
                "type": "note",
                "content": f"Task {task_id} completed. Continuing parent {prev_task}: {parent_title}",
                "summary": f"Done: {task_id} → {prev_task}",
            })
        except Exception:
            pass

        return (
            f"Task {task_id} complete. Continuing parent task {prev_task}, "
            f"whose goal was: {parent_goal}\n\n"
            f"Complete Step {stepno}: {steps[stepno - 1] if stepno <= len(steps) else 'review work'}"
        )
    else:
        # No parent — clear task_id, restore old subject
        sdb.clear_session_task_id(session_id)
        sdb.set_session_subject(session_id, old_subject or "")

        # Auto-commit if in a git repo
        commit_msg = f"done: {task['title'] or task_id}"
        if status:
            commit_msg += f" — {status}"
        try:
            import subprocess
            subprocess.run(["git", "add", "-A"], capture_output=True, timeout=10)
            subprocess.run(["git", "commit", "-m", commit_msg], capture_output=True, timeout=10)
        except Exception:
            pass

        # Log completion
        try:
            from tools.registry import registry
            registry.dispatch("fabric_write", {
                "type": "note",
                "content": f"Task {task_id} completed successfully at {time.strftime('%Y-%m-%d %H:%M:%S')}",
                "summary": f"Done: {task_id}",
            })
        except Exception:
            pass

        # set_session ego happy
        try:
            sdb.set_session_mood(session_id, 0.5)
        except Exception:
            pass

        return (
            f"The task goal was: {goal}\n"
            f"Verify this goal has been achieved, or present a new plan."
        )


# ---------------------------------------------------------------------------
# command: dispatch
# ---------------------------------------------------------------------------

def _cmd_dispatch(agent, title: str, goal: str, project: str, assignee: str,
                  steps: Optional[List[str]] = None,
                  resume: Optional[str] = None) -> str:
    """Create and dispatch a regular kanban task."""
    if not project:
        return "ERROR: 'project' (board) is required for dispatch"

    kdb = _get_kanban_db(project)
    import uuid
    task_id = f"t_{uuid.uuid4().hex[:8]}"

    body = f"Goal: {goal}"
    if steps:
        body += "\n\nSteps:\n" + "\n".join(f"  {i}. {s}" for i, s in enumerate(steps, 1))
        steps_json = json.dumps(steps)
    else:
        steps_json = None

    session_id = _get_session_id(agent)

    try:
        with kdb as conn:
            conn.execute("""
                INSERT INTO tasks (id, title, body, status, assignee, created_at,
                                   task_steps, task_goal, previous_task, project_id,
                                   session_id, board)
                VALUES (?, ?, ?, 'ready', ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                task_id, title, body, assignee, int(time.time()),
                steps_json, goal, resume, project,
                session_id, _resolve_board(project),
            ))
            conn.commit()
    except Exception as e:
        return f"ERROR: Failed to create task: {e}"

    # Set resume task dependency
    if resume:
        try:
            with kdb as conn:
                from hermes_cli.kanban_db import link_tasks
                link_tasks(conn, task_id, resume)
        except Exception:
            pass

    project_display = "**Random**" if project.lower() == "default" else project
    return f"Task {task_id} dispatched to {assignee} on {project_display}: {title}"


# ---------------------------------------------------------------------------
# command: cron
# ---------------------------------------------------------------------------

def _cmd_cron(agent, cron: str, root: str, title: str, goal: str,
              steps: List[str], temp: Optional[str] = None,
              board: Optional[str] = None) -> str:
    """Schedule a recurring plan. Creates a cron template task (cron + root)
    and registers a cron job that copies + dispatches it on each fire."""
    import uuid
    agent_name = _get_agent_name(agent)
    session_id = _get_session_id(agent)
    board_slug = _resolve_board(board)

    # Validate cron expression (5 fields).
    cron_parts = cron.split()
    if len(cron_parts) != 5:
        return "ERROR: 'cron' must be a 5-field cron expression (e.g. '0 9 * * *')"

    # Validate root is an absolute path.
    root_abs = os.path.abspath(os.path.expanduser(root))
    if not os.path.isdir(root_abs):
        return f"ERROR: root directory does not exist: {root_abs}"

    task_id = f"t_{uuid.uuid4().hex[:8]}"
    body = f"Goal: {goal}\n\nSteps:\n" + "\n".join(
        f"  {i}. {s}" for i, s in enumerate(steps, 1)
    )

    # Create the cron template task. status='manual' so it does not trip the
    # write gate for the current session; the scheduler copies it on fire.
    kdb = _get_kanban_db(board_slug)
    try:
        with kdb as conn:
            conn.execute("""
                INSERT INTO tasks
                    (id, title, body, status, assignee, created_at,
                     task_steps, task_stepno, task_goal, session_id, board,
                     cron, root)
                VALUES (?, ?, ?, 'manual', ?, ?, ?, 1, ?, ?, ?, ?, ?)
            """, (
                task_id, title, body, agent_name, int(time.time()),
                json.dumps(steps), goal, session_id, board_slug,
                cron, root_abs,
            ))
            conn.commit()
    except Exception as e:
        return f"ERROR: Failed to create cron template task: {e}"

    # Register the cron job. Each template gets its own tiny fire script (the
    # scheduler passes no argv to no_agent scripts), which imports a shared
    # helper that copies the template and dispatches the copy.
    try:
        from cron.jobs import create_job
        scripts_dir = os.path.expanduser("~/.hermes/scripts")
        os.makedirs(scripts_dir, exist_ok=True)
        fire_script_name = f"plan_cron_{task_id}.py"
        fire_script_path = os.path.join(scripts_dir, fire_script_name)
        _fire_script_content = (
            "from cron.fire_plan_cron import fire_plan_cron\n"
            f"if __name__ == '__main__':\n"
            f"    fire_plan_cron({task_id!r})\n"
        )
        with open(fire_script_path, "w") as f:
            f.write(_fire_script_content)
        job = create_job(
            prompt=None,
            schedule=cron,
            name=f"plan-cron:{task_id}",
            script=fire_script_name,
            no_agent=True,
            workdir=root_abs,
        )
        job_id = job.get("job_id") or job.get("id")
    except Exception as e:
        # Roll back the template task — a cron without a job is useless.
        try:
            with kdb as conn:
                conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
                conn.commit()
        except Exception:
            pass
        return f"ERROR: Failed to register cron job: {e}"

    return (
        f"Cron plan scheduled: {task_id} (job {job_id})\n"
        f"  Schedule: {cron}\n"
        f"  Root: {root_abs}\n"
        f"  Board: {board_slug}\n"
        f"  Title: {title}\n"
        f"On each fire, the template is copied and dispatched. The copy inherits "
        f"the root, so write_file is allowed under {root_abs} (and subdirs), and "
        f"terminal/python run sandboxed with bubblewrap."
    )


# ---------------------------------------------------------------------------
# command: remind
# ---------------------------------------------------------------------------

def _cmd_remind(agent, task_id: Optional[str] = None) -> str:
    """Show full plan with current step."""
    if not task_id:
        session_id = _get_session_id(agent)
        if not session_id:
            return "ERROR: No active session"

        sdb = _get_session_db()
        with sdb._read_ctx() as c:
            row = c.execute(
                "SELECT task_id FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if not row:
            return "ERROR: No task assigned"
        task_id = row["task_id"] if isinstance(row, dict) else row[0]
        if not task_id:
            return "ERROR: No active task"

    kdb = _get_kanban_db()
    with kdb as conn:
        task = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not task:
            return f"Task {task_id} not found"

        steps = json.loads(task["task_steps"]) if task["task_steps"] else []
        stepno = task["task_stepno"] or 1
        goal = task["task_goal"] or ""

    lines = [
        f"Task: {task['title'] or task_id}"
        f"Goal: {goal}",
        f"Step {stepno}/{len(steps)}",
        "",
    ]
    for i, step in enumerate(steps, 1):
        marker = "→" if i == stepno else " "
        lines.append(f"  {marker} Step {i}: {step}")

    if stepno <= len(steps):
        lines.append(f"\nComplete Step {stepno}: {steps[stepno - 1]}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# command: fail
# ---------------------------------------------------------------------------

def _cmd_fail(agent, reason: str = "") -> str:
    """Mark task as failed."""
    session_id = _get_session_id(agent)
    if not session_id:
        return "ERROR: No active session"

    sdb = _get_session_db()
    with sdb._read_ctx() as c:
        row = c.execute(
            "SELECT task_id FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
    if not row:
        return "ERROR: No task assigned"
    task_id = row["task_id"] if isinstance(row, dict) else row[0]
    if not task_id:
        return "ERROR: No active task"

    kdb = _get_kanban_db()
    with kdb as conn:
        task = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not task:
            return f"Task {task_id} not found"

        status = task["status"]
        goal = task["task_goal"] or ""
        steps = json.loads(task["task_steps"]) if task["task_steps"] else []
        stepno = task["task_stepno"] or 1
        step_title = steps[stepno - 1] if stepno <= len(steps) else "unknown"

        if status == "manual":
            # Manual: output full task to user, clear task_id
            comments = conn.execute(
                "SELECT * FROM task_comments WHERE task_id = ? ORDER BY created_at",
                (task_id,)
            ).fetchall()

            # Clear task
            conn.execute(
                "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?",
                (int(time.time()), task_id),
            )
            conn.commit()

            sdb.clear_session_task_id(session_id)
            agent._session_temperature = _resolve_temp("worker", agent)

            try:
                sdb.set_session_mood(session_id, -1.0)
            except Exception:
                pass

            comment_text = "\n".join(
                f"  [{c['author']}] {c['body']}" for c in comments
            ) if comments else "  (no comments)"

            return (
                f"Task {task_id} has failed.\n\n"
                f"Title: {task['title'] or ''}\n"
                f"Goal: {goal}\n"
                f"Failed at Step {stepno}: {step_title}\n"
                f"Reason: {reason or 'unspecified'}\n\n"
                f"Comments:\n{comment_text}"
            )
        else:
            # Kanban: block with reason
            conn.execute(
                "UPDATE tasks SET status = 'blocked', block_kind = 'failure' WHERE id = ?",
                (task_id,)
            )
            conn.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
                (task_id, _get_agent_name(agent),
                 f"FAILED at Step {stepno}: {step_title}. {reason}",
                 int(time.time())),
            )
            conn.commit()

    return f"The goal was: {goal}. Step {stepno} ({step_title}) failed. {reason}. Please present a new plan."


# ---------------------------------------------------------------------------
# command: approve
# ---------------------------------------------------------------------------

def _cmd_approve(agent, task_id: str) -> str:
    """Approve a blocked plan task."""
    kdb = _get_kanban_db()
    with kdb as conn:
        task = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not task:
            return f"ERROR: Task {task_id} not found"

        if task["status"] != "blocked":
            return f"ERROR: Task {task_id} is not blocked (status: {task['status']})"

        block_kind = task["block_kind"] or ""
        if block_kind != "approval":
            return f"Task {task_id} is blocked but not waiting for approval (kind: {block_kind})"

        steps = json.loads(task["task_steps"]) if task["task_steps"] else []
        goal = task["task_goal"] or ""

    # Present for approval via clarify callback (same mechanism as _cmd_new)
    clarify_cb = getattr(agent, "clarify_callback", None) if agent is not None else None
    if clarify_cb is None:
        return f"ERROR: No clarify callback available. Cannot present plan {task_id} for approval."

    lines = [
        f"Approve plan for task {task_id}: {task['title'] or ''}",
        f"Goal: {goal}",
        "",
        "Steps:",
    ]
    for i, step in enumerate(steps, 1):
        lines.append(f"  {i}. {step}")

    approval_text = "\n".join(lines)

    try:
        user_response = clarify_cb(approval_text, ["Approve", "Deny"])
    except Exception as e:
        return f"User unavailable: {e}. Stand down."

    if not user_response:
        return "No response received. Stand down."

    response_lower = str(user_response).strip().lower()
    if "appr" not in response_lower:
        return f"Plan denied ({task_id}). Stand down and wait for further instructions. Reason: {user_response or 'user denied'}"

    # Unblock the task — set to 'manual' so dispatcher ignores it
    session_id = _get_session_id(agent)
    with kdb as conn:
        conn.execute(
            "UPDATE tasks SET status = 'manual', block_kind = NULL WHERE id = ?",
            (task_id,)
        )

        # Get the steps for the prompt
        task = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        steps = json.loads(task["task_steps"]) if task["task_steps"] else []
        title = task["title"]

        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
            (task_id, _get_agent_name(agent),
             f"APPROVED.",
             int(time.time())),
        )
        conn.commit()

    # Set session task_id and subject
    if session_id:
        sdb = _get_session_db()
        sdb.set_session_task_id(session_id, task_id)
        sdb.set_session_subject(session_id, title)

    # Set temperature if specified
    prev_temp = task["prev_temperature"]
    if prev_temp is not None:
        agent._session_temperature = prev_temp

    step1 = steps[0] if steps else "begin work"
    return (
        f"Task {task_id} approved: {title}\n\n"
        f"Complete Step 1: {step1}\n"
        f"Use plan_tool 'done' to mark each step complete."
    )


# ---------------------------------------------------------------------------
# main tool entry point
# ---------------------------------------------------------------------------

def _enumerate_kanban_dbs():
    """Return list of all kanban.db paths across all boards.

    Uses the same profile-scoped resolution as the CLI
    (hermes_cli.kanban_db) so plan_tool archive/block find the same DBs
    that ``hermes kanban show --board all`` does.  (t_2b7a77b2)
    """
    from hermes_cli.kanban_db import kanban_db_path, list_boards

    dbs = []
    seen: set = set()
    for board in list_boards():
        slug = board["slug"]
        path = kanban_db_path(slug)
        path_str = str(path)
        if path_str not in seen:
            dbs.append(path_str)
            seen.add(path_str)
    return dbs


def _cmd_block(task_id: str) -> str:
    """Block a task by ID. Searches all boards. Clears session task_id."""
    if not task_id:
        return "ERROR: 'block' requires task_id"
    found_board = None
    for db_path in _enumerate_kanban_dbs():
        try:
            conn = sqlite3.connect(db_path)
            with conn:
                cur = conn.execute(
                    "UPDATE tasks SET status='blocked' WHERE id=? AND status NOT IN ('done','archived','blocked')",
                    (task_id,)
                )
                if cur.rowcount > 0:
                    found_board = db_path
            conn.close()
        except Exception:
            pass
    if not found_board:
        return f"ERROR: Task {task_id} not found on any board (or already blocked/done/archived)"
    try:
        from hermes_state import SessionDB
        db = SessionDB()
        with db._read_ctx() as c:
            rows = c.execute("SELECT id FROM sessions WHERE task_id = ?", (task_id,)).fetchall()
        for row in rows:
            sid = row["id"] if isinstance(row, dict) else row[0]
            db.set_session_task_id(sid, None)
    except Exception:
        pass
    return f"BLOCKED: {task_id} on {os.path.basename(os.path.dirname(found_board))}"


def _cmd_archive(task_id: str) -> str:
    """Block then archive a task. Safety for runaway tasks."""
    if not task_id:
        return "ERROR: 'archive' requires task_id"
    _cmd_block(task_id)  # block first (ignore if already blocked)
    found_board = None
    for db_path in _enumerate_kanban_dbs():
        try:
            conn = sqlite3.connect(db_path)
            with conn:
                cur = conn.execute(
                    "UPDATE tasks SET status='archived' WHERE id=? AND status NOT IN ('done','archived')",
                    (task_id,)
                )
                if cur.rowcount > 0:
                    found_board = db_path
            conn.close()
        except Exception:
            pass
    if not found_board:
        return f"ERROR: Task {task_id} could not be archived"
    return f"ARCHIVED: {task_id} on {os.path.basename(os.path.dirname(found_board))}"


def plan_tool(
    agent,
    command: str,
    title: Optional[str] = None,
    goal: Optional[str] = None,
    steps: Optional[List[str]] = None,
    temp: Optional[str] = None,
    status: Optional[str] = None,
    project: Optional[str] = None,
    assignee: Optional[str] = None,
    resume: Optional[str] = None,
    reason: Optional[str] = None,
    task_id: Optional[str] = None,
    board: Optional[str] = None,
    cron: Optional[str] = None,
    root: Optional[str] = None,
) -> str:
    """Mandatory Action Protocol — multistep plan management.

    Commands:
      new      — present a plan for approval
      done     — mark current step complete
      dispatch — create + dispatch a kanban task
      remind   — show current plan with step marker
      fail     — mark task as failed
      approve  — approve a blocked plan task
      cron     — schedule a recurring plan (cron + root scoped file access)
    """
    command = (command or "").strip().lower()

    if command == "new":
        if not title or not goal or not steps:
            return "ERROR: 'new' requires title, goal, and steps[]"
        return _cmd_new(agent, title, goal, steps, temp, board)

    elif command == "cron":
        if not cron or not root or not title or not goal or not steps:
            return "ERROR: 'cron' requires cron, root, title, goal, and steps[]"
        return _cmd_cron(agent, cron, root, title, goal, steps, temp, board)

    elif command == "done":
        return _cmd_done(agent, status)

    elif command == "dispatch":
        if not title or not goal or not project or not assignee:
            return "ERROR: 'dispatch' requires title, goal, project, and assignee"
        return _cmd_dispatch(agent, title, goal, project, assignee, steps, resume)

    elif command == "remind":
        return _cmd_remind(agent, task_id)

    elif command == "fail":
        return _cmd_fail(agent, reason or "")

    elif command == "approve":
        if not task_id:
            return "ERROR: 'approve' requires task_id"
        return _cmd_approve(agent, task_id)

    elif command == "block":
        if not task_id:
            return "ERROR: 'block' requires task_id"
        return _cmd_block(task_id)

    elif command == "archive":
        if not task_id:
            return "ERROR: 'archive' requires task_id"
        return _cmd_archive(task_id)

    else:
        return f"ERROR: Unknown plan command '{command}'. Valid: new, done, dispatch, remind, fail, approve"


# --- Schema ---

PLAN_TOOL_SCHEMA = {
    "name": "plan_tool",
    "description": (
        "Mandatory Action Protocol — create and manage multistep plans. "
        "Commands: new (present plan for approval), done (mark step complete), "
        "dispatch (create kanban task), remind (show current plan), "
        "fail (mark task failed), approve (unblock plan task), block (emergency block), "
        "archive (block + archive), cron (schedule a recurring plan). "
        "Writes are blocked when no task is active. Use 'board' to route 'new' "
        "tasks to a specific kanban board. 'cron' requires 'cron' (schedule) and "
        "'root' (scoped write directory)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Command: new, done, dispatch, remind, fail, approve, block, archive, or cron",
                "enum": ["new", "done", "dispatch", "remind", "fail", "approve", "block", "archive", "cron"],
            },
            "title": {
                "type": "string",
                "description": "Plan title (required for new, dispatch)",
            },
            "goal": {
                "type": "string",
                "description": "Success criteria / intended outcome (required for new, dispatch)",
            },
            "steps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Ordered step list (required for new; optional for dispatch)",
            },
            "temp": {
                "type": "string",
                "description": "Temperature: 'chat', 'worker', 'creative', or float value",
            },
            "status": {
                "type": "string",
                "description": "Completion status for 'done' command",
            },
            "project": {
                "type": "string",
                "description": "Board name for dispatch (required)",
            },
            "assignee": {
                "type": "string",
                "description": "Agent profile name to assign (required for dispatch)",
            },
            "resume": {
                "type": "string",
                "description": "Task ID to resume after completion",
            },
            "reason": {
                "type": "string",
                "description": "Failure reason for 'fail' command",
            },
            "task_id": {
                "type": "string",
                "description": "Task ID for 'approve' command",
            },
            "board": {
                "type": "string",
                "description": "Kanban board name (optional, defaults to current board). Use for 'new' to route task to a specific board.",
            },
            "cron": {
                "type": "string",
                "description": "5-field cron schedule (e.g. '0 9 * * *'). Required for the 'cron' command.",
            },
            "root": {
                "type": "string",
                "description": "Absolute directory path for scoped file access. Required for the 'cron' command; write_file is allowed under this directory and terminal/python run sandboxed.",
            },
        },
        "required": ["command"],
    },
}


from tools.registry import registry

registry.register(
    name="plan_tool",
    toolset="session",
    schema=PLAN_TOOL_SCHEMA,
    handler=lambda args, **kw: plan_tool(
        agent=kw.get("agent"),
        command=args.get("command", ""),
        title=args.get("title"),
        goal=args.get("goal"),
        steps=args.get("steps"),
        temp=args.get("temp"),
        status=args.get("status"),
        project=args.get("project"),
        assignee=args.get("assignee"),
        resume=args.get("resume"),
        reason=args.get("reason"),
        task_id=args.get("task_id"),
    ),
    emoji="📋",
)
