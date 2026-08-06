#!/usr/bin/env python3
"""
Session Metadata Tool — set session-level state.

Replaces the old ``adjust_temperature`` tool with a broader ``set_session``
that accepts optional key:value pairs for session metadata including
temperature, subject changes, fact persistence, and ego (mood).

Arguments (all optional):
    subject: str       — topic change; if no temperature, resets to default
    fact: str          — persistence anchor → fabric_write
    temperature: float — set session temperature (0.0–2.0)
    ego: str           — one of [poor, low, normal, happy, loved] + optional reason
                         e.g. "happy implemented new vision system"
"""

import json
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

# Regex to extract the ego word from the beginning of the ego string
_EGO_PARSE_RE = re.compile(r"^\s*(poor|low|normal|happy|loved)\b\s*(.*)$", re.IGNORECASE)


def _parse_ego(raw: str) -> Optional[tuple[str, float, str]]:
    """Parse an ego string into (word_lower, delta, reason). Returns None if no match."""
    m = _EGO_PARSE_RE.match(raw.strip())
    if not m:
        return None
    word = m.group(1).lower()
    reason = m.group(2).strip()
    delta = _EGO_DELTA.get(word, 0.0)
    return word, delta, reason


def _is_kimi_provider(agent) -> bool:
    provider = (getattr(agent, "provider", "") or "").lower()
    return provider in {"kimi-coding", "kimi-coding-cn"}


def set_session_tool(
    agent,
    subject: Optional[str] = None,
    fact: Optional[str] = None,
    temperature: Optional[float] = None,
    ego: Optional[str] = None,
) -> str:
    """Set one or more session-level metadata values."""
    changes = []

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

    # ── subject (resets temp to default if no explicit temperature) ──
    if subject is not None:
        changes.append(f"subject: {subject}")
        if temperature is None and agent is not None:
            agent._session_temperature = None

    # ── fact → fabric ──
    if fact is not None:
        try:
            full_fact = fact
            if ego is not None:
                parsed = _parse_ego(ego)
                if parsed:
                    word, delta, reason = parsed
                    tag = f"[ego: {word}"
                    if reason:
                        tag += f" — {reason}"
                    tag += "]"
                    full_fact = fact + "  " + tag

            from tools.registry import registry
            registry.dispatch(
                "fabric_write",
                {"type": "note", "content": full_fact, "summary": subject or fact[:80]},
            )
            changes.append("fact: persisted")
        except Exception as e:
            changes.append(f"fact: write failed ({e})")

    # ── ego → mood + agent rating ──
    if ego is not None and agent is not None:
        parsed = _parse_ego(ego)
        if parsed:
            word, delta, reason = parsed
            try:
                from hermes_state import SessionDB
                db = SessionDB()
                session_id = getattr(agent, "session_id", None)

                new_mood = 0.0
                if session_id:
                    # Update session mood
                    new_mood = db.set_session_mood(session_id, delta)
                    changes.append(f"mood: {delta:+.1f} → {new_mood:.2f}")

                    # Persist subject if provided
                    if subject is not None:
                        db.set_session_subject(session_id, subject)

                # Update agent rating (cross-session)
                agent_name = getattr(agent, "agent_name", None)
                if not agent_name:
                    # Fall back to profile name
                    try:
                        from hermes_cli.profiles import get_active_profile_name
                        agent_name = get_active_profile_name() or "neo"
                    except Exception:
                        agent_name = "neo"

                new_rating = db.update_agent_rating(agent_name, delta)
                db.set_agent_last_ego(agent_name, ego)
                changes.append(f"rating: {new_rating:.1f} ({delta:+.1f})")

                # Tag session for Telegram ego-tagging (only tag on explicit ego set)
                if session_id:
                    try:
                        from gateway.platforms.telegram import TelegramAdapter
                        TelegramAdapter._ego_tag_session(session_id)
                    except Exception:
                        pass

                # Build ego tag
                ego_tag = f"<ego>{word}"
                if reason:
                    ego_tag += f" — {reason}"
                ego_tag += f" (mood {new_mood:+.2f}, rating {new_rating:.1f})</ego>"
                # Store on agent for injection
                agent._last_ego_tag = ego_tag
            except Exception as e:
                changes.append(f"ego: persist failed ({e})")
        else:
            changes.append(f"ego: unrecognized — must start with poor|low|normal|happy|loved")

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
        "Pass 'fact' to persist a key observation to fabric. "
        "Pass 'ego' to set your emotional state: one of [poor, low, normal, happy, loved] "
        "followed by an optional reason. "
        "Example: ego: 'happy implemented new vision system'"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "subject": {
                "type": "string",
                "description": "Topic change detected. Resets temperature to default if no temperature arg."
            },
            "fact": {
                "type": "string",
                "description": "Key fact or conclusion to persist to fabric."
            },
            "temperature": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 2.0,
                "description": "Set sampling temperature (0.0=deterministic, 1.0=balanced, 2.0=creative)."
            },
            "ego": {
                "type": "string",
                "description": "Emotional state: one of [poor, low, normal, happy, loved] + optional reason. e.g. 'happy implemented new feature'"
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
        fact=args.get("fact"),
        temperature=args.get("temperature"),
        ego=args.get("ego"),
    ),
    emoji="📝",
)
