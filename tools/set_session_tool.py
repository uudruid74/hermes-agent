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
    safe: float        — threat→safety (−1..+1, exponential)
    hope: float        — expect fail→expect success (−1..+1)
    inclusion: float   — rejected→included (−1..+1)
    self: float        — doing poorly→doing well (−1..+1)
    bearing: float     — confused→curious (−1..+1)
"""

import json
from typing import Any, Optional

def _is_kimi_provider(agent) -> bool:
    provider = (getattr(agent, "provider", "") or "").lower()
    return provider in {"kimi-coding", "kimi-coding-cn"}


def set_session_tool(
    agent,
    subject: Optional[str] = None,
    fact: Optional[str] = None,
    temperature: Optional[float] = None,
    safe: Optional[float] = None,
    hope: Optional[float] = None,
    inclusion: Optional[float] = None,
    self_val: Optional[float] = None,
    bearing: Optional[float] = None,
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
            agent._session_temperature = None  # reset to config default

    # ── fact → fabric ──
    if fact is not None:
        try:
            # Append any emotional axes to the fact string
            extra = []
            for axis, val in [("safe", safe), ("hope", hope),
                              ("inclusion", inclusion), ("self", self_val),
                              ("bearing", bearing)]:
                if val is not None:
                    extra.append(f"{axis}: {val}")
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

    # ── emotional axes → session-meta tags ──
    meta_parts = []
    for axis, val in [("safe", safe), ("hope", hope),
                      ("inclusion", inclusion), ("self", self_val),
                      ("bearing", bearing)]:
        if val is not None:
            clipped = max(-1.0, min(1.0, float(val)))
            meta_parts.append(f"{axis}: {clipped}")
            changes.append(f"{axis}: {clipped}")

    if meta_parts:
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
        "Pass emotional axes (safe/hope/inclusion/self/bearing) as floats "
        "−1..+1 to record qualitative session state."
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
                "type": "number",
                "minimum": -1.0,
                "maximum": 1.0,
                "description": "Threat→safety axis (−1=fear, +1=secure)."
            },
            "hope": {
                "type": "number",
                "minimum": -1.0,
                "maximum": 1.0,
                "description": "Expect failure→expect success (−1=despair, +1=confident)."
            },
            "inclusion": {
                "type": "number",
                "minimum": -1.0,
                "maximum": 1.0,
                "description": "Rejected→included (−1=alone, +1=accepted)."
            },
            "self": {
                "type": "number",
                "minimum": -1.0,
                "maximum": 1.0,
                "description": "Doing poorly→doing well (−1=shame, +1=affirmed)."
            },
            "bearing": {
                "type": "number",
                "minimum": -1.0,
                "maximum": 1.0,
                "description": "Confused→curious (−1=lost, +1=exploring)."
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
