"""Fire a plan-cron template: copy it to a new task and dispatch it.

Invoked by per-template scripts under ~/.hermes/scripts/ (one per cron plan).
Each runs with no_agent=True, so this helper is the entire job — no LLM.

On fire:
  1. Copy the template task to a fresh task id (status='ready').
  2. The copy inherits title, body, steps, goal, assignee, board, root.
  3. The dispatcher claims the 'ready' copy like any normal task.

The copy carries `root`, so the file_safety write gate allows write_file under
that directory (and subdirs), and terminal/python execution is sandboxed with
bubblewrap.
"""
from __future__ import annotations

import os
import sqlite3
import time
import uuid


def _kanban_db_path():
    override = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if override:
        return override
    root = os.environ.get("HERMES_KANBAN_HOME", "").strip()
    if not root:
        root = os.path.join(os.path.expanduser("~"), ".hermes")
    return os.path.join(root, "kanban", "kanban.db")


def fire_plan_cron(task_id: str) -> None:
    db_path = _kanban_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        template = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if template is None:
            print(f"fire_plan_cron: template {task_id} not found")
            return

        # Skip if the template was deleted/archived since scheduling.
        if template["status"] in ("archived", "done"):
            print(f"fire_plan_cron: template {task_id} is {template['status']}, skipping")
            return

        new_id = f"t_{uuid.uuid4().hex[:8]}"
        now = int(time.time())
        conn.execute(
            """
            INSERT INTO tasks
                (id, title, body, status, assignee, created_at,
                 task_steps, task_stepno, task_goal, session_id, board,
                 root, previous_task)
            VALUES (?, ?, ?, 'ready', ?, ?, ?, 1, ?, NULL, ?, ?, ?)
            """,
            (
                new_id,
                template["title"],
                template["body"],
                template["assignee"],
                now,
                template["task_steps"],
                template["task_goal"],
                template["board"],
                template["root"],
                task_id,  # previous_task links back to the template
            ),
        )
        conn.commit()
        print(f"fire_plan_cron: dispatched {new_id} (from template {task_id})")
    finally:
        conn.close()
