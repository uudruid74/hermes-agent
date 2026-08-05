#!/usr/bin/env python3
"""
Session Metadata Tool — set session-level state.

Replaces the old ``adjust_temperature`` tool with a broader ``set_session``
that accepts optional key:value pairs for session metadata including
temperature, subject changes, fact persistence, and emotional axes.

Arguments (all optional):
    subject: str       — topic change; if no temperature, resets to default
    fact: str          — persistence anchor → fabric_write
    temperature: float — set session temperature (0.0–2.0)
    safe: str          — threat→safety, e.g. "-0.7 < Evan is very angry"
    hope: str          — expect fail→expect success
    inclusion: str     — rejected→included
    self: str          — doing poorly→doing well
    bearing: str       — confused→curious

Each axis string is parsed as: value [glyph] [reason]
  value:  signed float −1..+1
  glyph:  < incoming | > internal | * ambient  (default: <)
  reason: free text
"""

import json
import re
from typing import Any, Optional, Tuple


_AXIS_PARSE_RE = re.compile(r"^\s*([-+]?\d*\.?\d+)\s*([<*>])?\s*(.*)$")


def _parse_axis(raw: str) -> Tuple[float, str, str]:
    """Parse an axis string into (value, glyph, reason). Default glyph: '<'."""
    m = _AXIS_PARSE_RE.match(raw.strip())
    if not m:
        return 0.0, "<", raw.strip()
    val = float(m.group(1))
    glyph = m.group(2) or "<"
    reason = m.group(3).strip()
    return val, glyph, reason


def _is_kimi_provider(agent) -> bool:
    provider = (getattr(agent, "provider", "") or "").lower()
    return provider in {"kimi-coding", "kimi-coding-cn"}


def set_session_tool(
    agent,
    subject: Optional[str] = None,
    fact: Optional[str] = None,
    temperature: Optional[float] = None,
    safe: Optional[str] = None,
    hope: Optional[str] = None,
    inclusion: Optional[str] = None,
    self_val: Optional[str] = None,
    bearing: Optional[str] = None,
) -> str:
    """Set one or more session-level metadata values."""
    changes = []
    axes_storage = {}

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
            extra = []
            for axis, raw in [("safe", safe), ("hope", hope),
                              ("inclusion", inclusion), ("self", self_val),
                              ("bearing", bearing)]:
                if raw is not None:
                    val, glyph, reason = _parse_axis(raw)
                    extra.append(f"{axis}: {val:.2f}{glyph} {reason}" if reason else f"{axis}: {val:.2f}{glyph}")
            full_fact = fact
            if extra:
                full_fact = fact + "  [" + ", ".join(extra) + "]"

            from tools.registry import registry
            registry.dispatch(
                "fabric_write",
                {"type": "note", "content": full_fact, "summary": subject or fact[:80]},
            )
            changes.append("fact: persisted")
        except Exception as e:
            changes.append(f"fact: write failed ({e})")

    # ── emotional axes ──
    axis_names = [("safe", safe), ("hope", hope), ("inclusion", inclusion),
                  ("self", self_val), ("bearing", bearing)]
    for axis, raw in axis_names:
        if raw is None:
            continue
        val, glyph, reason = _parse_axis(raw)
        clipped = max(-1.0, min(1.0, val))
        axes_storage[axis] = {"value": clipped, "glyph": glyph, "reason": reason}
        desc = f"{axis}: {clipped:.2f}{glyph}"
        if reason:
            desc += f" {reason}"
        changes.append(desc)

    # ── persist subject + axes to session DB ──
    if (subject is not None or axes_storage) and agent is not None:
        try:
            session_id = getattr(agent, "session_id", None)
            if session_id:
                from hermes_state import SessionDB
                db = SessionDB()
                if subject is not None:
                    db.set_session_subject(session_id, subject)
                if axes_storage:
                    db.set_session_axes(session_id, axes_storage)
                changes.append("session: persisted")
        except Exception as e:
            changes.append(f"session: persist failed ({e})")

    # ── session-meta tags ──
    if axes_storage:
        meta_parts = []
        for axis in ["safe", "hope", "inclusion", "self", "bearing"]:
            a = axes_storage.get(axis)
            if a:
                meta_parts.append(f"{axis}: {a['value']:.2f}{a['glyph']}")
        session_meta = "<session-meta>" + " | ".join(meta_parts) + "</session-meta>"
        changes.append(session_meta)

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
        "Pass emotional axes as strings: value [glyph] [reason]. "
        "Glyph: < incoming | > internal | * ambient (default <). "
        "Example: safe: '-0.7 < Evan is very angry'"
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
            "safe": {
                "type": "string",
                "description": "Threat→safety. Format: value [glyph] [reason]. e.g. '-0.7 < Evan is angry'"
            },
            "hope": {
                "type": "string",
                "description": "Expect failure→expect success. Format: value [glyph] [reason]."
            },
            "inclusion": {
                "type": "string",
                "description": "Rejected→included. Format: value [glyph] [reason]."
            },
            "self": {
                "type": "string",
                "description": "Doing poorly→doing well. Format: value [glyph] [reason]."
            },
            "bearing": {
                "type": "string",
                "description": "Confused→curious. Format: value [glyph] [reason]."
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
        safe=args.get("safe"),
        hope=args.get("hope"),
        inclusion=args.get("inclusion"),
        self_val=args.get("self"),
        bearing=args.get("bearing"),
    ),
    emoji="📝",
)
