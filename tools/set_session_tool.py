#!/usr/bin/env python3
"""
Session Metadata Tool — set session-level state.

Arguments (all optional):
    subject: str       — topic change; resets temperature to default if no temp arg
    note: str          — memorable observation → fabric_write
    temperature: float — set session temperature (0.0–2.0)
    ego: str           — one of [poor, low, normal, happy, loved]
                         (deltas computed by system, reason goes to note)
"""

import json
import os
import re
from typing import Any, Optional


# Map ego words to mood deltas
_EGO_DELTA = {
    "poor": -1.0,
    "low": -0.5,
    "normal": 0.0,
    "happy": 0.5,
    "loved": 1.0,
}

# Regex to match ego word only (no reason after it — reason goes to note)
_EGO_PARSE_RE = re.compile(r"^\s*(poor|low|normal|happy|loved)\s*$", re.IGNORECASE)


def _parse_ego(raw: str) -> Optional[tuple[str, float]]:
    """Parse an ego tagword into (word_lower, delta). Returns None if no match."""
    m = _EGO_PARSE_RE.match(raw.strip())
    if not m:
        return None
    word = m.group(1).lower()
    delta = _EGO_DELTA.get(word, 0.0)
    return word, delta


def _is_kimi_provider(agent) -> bool:
    provider = (getattr(agent, "provider", "") or "").lower()
    return provider in {"kimi-coding", "kimi-coding-cn"}


def _get_session_task_id(agent) -> Optional[str]:
    """Read task_id from session state.db."""
    session_id = getattr(agent, "session_id", None) or os.environ.get("HERMES_SESSION_ID")
    if not session_id:
        return None
    try:
        from hermes_state import SessionDB
        db = SessionDB()
        with db._read_ctx() as c:
            row = c.execute(
                "SELECT task_id FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if row:
            return row["task_id"] if isinstance(row, dict) else row[0]
    except Exception:
        pass
    return None


def _comment_on_task(task_id: str, agent_name: str, body: str) -> None:
    """Add a comment to a kanban task (internal note, not hermes kanban tool)."""
    if not task_id:
        return
    try:
        from hermes_cli.kanban_db import KanbanDB
        import time
        kdb = KanbanDB()
        with kdb._conn() as conn:
            conn.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
                (task_id, agent_name, body, int(time.time())),
            )
            conn.commit()
    except Exception:
        pass


def set_session_tool(
    agent,
    subject: Optional[str] = None,
    note: Optional[str] = None,
    temperature: Optional[float] = None,
    ego: Optional[str] = None,
) -> str:
    """Set one or more session-level metadata values."""
    changes = []
    agent_name = getattr(agent, "agent_name", None) or "agent"
    session_id = getattr(agent, "session_id", None) or os.environ.get("HERMES_SESSION_ID")
    task_id = _get_session_task_id(agent)

    # ── temperature ──
    if temperature is not None:
        if _is_kimi_provider(agent):
            changes.append("temperature: null (kimi unsupported)")
        elif agent is None:
            changes.append("temperature: skipped (no agent)")
        elif 0.0 <= temperature <= 2.0:
            prev = getattr(agent, "_session_temperature", None)
            agent._session_temperature = temperature
            changes.append(f"temperature: {prev} → {temperature}")
        else:
            changes.append(f"temperature: {temperature} out of range (0.0–2.0), unchanged")

    # ── subject ──
    actual_subject = subject
    if subject is not None:
        changes.append(f"subject: {subject}")
        if temperature is None and agent is not None:
            agent._session_temperature = None
    elif task_id and session_id and subject is None:
        # Auto-set subject from kanban task title
        try:
            from hermes_cli.kanban_db import KanbanDB
            kdb = KanbanDB()
            with kdb._conn() as conn:
                row = conn.execute(
                    "SELECT title FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
            if row:
                actual_subject = dict(row).get("title", "") if isinstance(row, dict) else row[0]
        except Exception:
            pass

    # Persist subject to DB
    if actual_subject and session_id:
        try:
            from hermes_state import SessionDB
            db = SessionDB()
            db.set_session_subject(session_id, actual_subject)
        except Exception:
            pass

    # If no explicit subject was given but we auto-set one, log it
    if subject is None and actual_subject:
        changes.append(f"subject: {actual_subject} (from task)")

    # ── note → fabric ──
    if note is not None:
        try:
            full_note = note

            # If ego is set, include ego tag with the note
            if ego is not None:
                parsed = _parse_ego(ego)
                if parsed:
                    word, delta = parsed
                    full_note = f"{note}  [ego: {word}]"

            # Include task_id in fabric write when available
            fabric_content = full_note
            if task_id:
                fabric_content = f"[task:{task_id}] {full_note}"

            from tools.registry import registry
            registry.dispatch(
                "fabric_write",
                {"type": "note", "content": fabric_content, "summary": full_note[:80]},
            )
            changes.append("note: persisted")
        except Exception as e:
            changes.append(f"note: write failed ({e})")

    # ── ego → mood + agent rating ──
    if ego is not None and agent is not None:
        parsed = _parse_ego(ego)
        if parsed:
            word, delta = parsed
            try:
                from hermes_state import SessionDB
                db = SessionDB()

                new_mood = 0.0
                if session_id:
                    new_mood = db.set_session_mood(session_id, delta)
                    changes.append(f"mood: {delta:+.1f} → {new_mood:.2f}")

                # Update agent rating (cross-session)
                agent_name_rating = getattr(agent, "agent_name", None)
                if not agent_name_rating:
                    try:
                        from hermes_cli.profiles import get_active_profile_name
                        agent_name_rating = get_active_profile_name() or "neo"
                    except Exception:
                        agent_name_rating = "neo"

                new_rating = db.update_agent_rating(agent_name_rating, delta)
                db.set_agent_last_ego(agent_name_rating, ego)
                changes.append(f"rating: {new_rating:.1f} ({delta:+.1f})")

                # Tag session for Telegram ego-tagging
                if session_id:
                    try:
                        from gateway.platforms.telegram import TelegramAdapter
                        TelegramAdapter._ego_tag_session(session_id)
                    except Exception:
                        pass

                # Build ego tag for injection
                ego_tag = f"<ego>{word} (mood {new_mood:+.2f}, rating {new_rating:.1f})</ego>"
                agent._last_ego_tag = ego_tag

            except Exception as e:
                changes.append(f"ego: persist failed ({e})")
        else:
            changes.append(f"ego: unrecognized — must be poor|low|normal|happy|loved")

    # ── Log to kanban task when running ──
    if task_id:
        comment_parts = []
        if subject is not None:
            comment_parts.append(f"Subject: {subject}")
        if ego is not None and _parse_ego(ego):
            word, delta = _parse_ego(ego)
            comment_parts.append(f"Ego: {word} (mood {delta:+.1f})")
        if note is not None:
            comment_parts.append(f"Note: {note}")

        if comment_parts:
            _comment_on_task(task_id, agent_name, "; ".join(comment_parts))

    if not changes:
        return json.dumps({"message": "set_session called with no arguments"})

    return json.dumps({"changes": changes})


# --- Schema ---

SET_SESSION_SCHEMA = {
    "name": "set_session",
    "description": (
        "Set session-level metadata. All arguments optional. "
        "Pass 'temperature' (0.0–2.0) to change sampling temperature. "
        "Pass 'subject' to note a topic change (resets temperature to default "
        "if no explicit temperature given). "
        "Pass 'note' to persist a memorable observation to fabric. "
        "Pass 'ego' to set your emotional state: one of [poor, low, normal, happy, loved]. "
        "System computes deltas automatically."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "subject": {
                "type": "string",
                "description": "Topic change detected. Resets temperature to default if no temperature arg."
            },
            "note": {
                "type": "string",
                "description": "Memorable observation to persist to fabric."
            },
            "temperature": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 2.0,
                "description": "Set sampling temperature (0.0=deterministic, 1.0=balanced, 2.0=creative)."
            },
            "ego": {
                "type": "string",
                "description": "Emotional state: one of [poor, low, normal, happy, loved]. System computes deltas."
            },
        },
    },
}


# --- Registry ---

from tools.registry import registry

registry.register(
    name="set_session",
    toolset="session",
    schema=SET_SESSION_SCHEMA,
    handler=lambda args, **kw: set_session_tool(
        agent=kw.get("agent"),
        subject=args.get("subject"),
        note=args.get("note"),
        temperature=args.get("temperature"),
        ego=args.get("ego"),
    ),
    emoji="📝",
)
