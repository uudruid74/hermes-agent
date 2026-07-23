#!/usr/bin/env python3
"""
Temperature Tool Module — Session-scoped temperature adjustment.

Lets the agent adjust its own sampling temperature mid-session via
``adjust_temperature(percentage)``. The value is stored on the agent
as ``_session_temperature`` and takes priority in ``resolve_temperature()``
over profile defaults and worker temperature.

Kimi / Moonshot providers do not support temperature — the tool returns
``null`` (no-op) for those providers.
"""

import json
from typing import Any, Dict


def _is_kimi_provider(agent) -> bool:
    """Return True when the provider is a Kimi / Moonshot variant."""
    provider = (getattr(agent, "provider", "") or "").lower()
    return provider in {"kimi-coding", "kimi-coding-cn"}


def adjust_temperature_tool(percentage: float, agent) -> str:
    """Adjust session temperature by a percentage offset.

    Args:
        percentage: Signed percentage change (e.g. +20 = 20% hotter,
                    -10 = 10% colder).
        agent: The AIAgent instance (injected at call site).

    Returns:
        JSON string with {temperature, changed, previous}.
        For Kimi providers, returns JSON null.
    """
    # Kimi / Moonshot doesn't support temperature — return null
    if _is_kimi_provider(agent):
        return json.dumps(None)

    if agent is None:
        return json.dumps({"error": "adjust_temperature requires agent dispatch; registry fallback failed"})

    previous = getattr(agent, "_session_temperature", None)
    # Use the profile temperature as baseline if no session override exists
    baseline = previous if previous is not None else (getattr(agent, "_temperature", None) or 0.7)

    # Calculate new temperature: baseline * (1 + percentage/100), clamped [0.0, 2.0]
    new_temp = baseline * (1.0 + percentage / 100.0)
    new_temp = max(0.0, min(2.0, new_temp))

    agent._session_temperature = new_temp
    changed = previous is None or abs(new_temp - previous) > 0.001

    return json.dumps({
        "temperature": new_temp,
        "changed": changed,
        "previous": previous,
    })


# --- Schema ---

ADJUST_TEMPERATURE_SCHEMA = {
    "name": "adjust_temperature",
    "description": (
        "Adjust the agent's sampling temperature for the remainder of the session. "
        "Positive percentage = more creative; negative = more deterministic. "
        "Temperature is clamped to [0.0, 2.0] and applies multiplicatively "
        "to the current value. Kimi/Moonshot providers return null (unsupported)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "percentage": {
                "type": "number",
                "description": (
                    "Signed percentage change. +20 makes temperature 20% higher "
                    "(more creative). -20 makes it 20% lower (more deterministic). "
                    "e.g. current=0.7, +20 → 0.84; current=0.7, -50 → 0.35"
                ),
            },
        },
        "required": ["percentage"],
    },
}


# --- Registry ---

from tools.registry import registry

registry.register(
    name="adjust_temperature",
    toolset="temperature",
    schema=ADJUST_TEMPERATURE_SCHEMA,
    handler=lambda args, **kw: adjust_temperature_tool(
        percentage=args.get("percentage", 0.0),
        agent=kw.get("agent", None),  # injected at agent-level dispatch; None = registry fallback
    ),
    emoji="🌡️",
)
