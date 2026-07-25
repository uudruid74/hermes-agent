#!/usr/bin/env python3
"""
Temperature Tool Module — Session-scoped temperature adjustment.

Lets the agent set its own sampling temperature mid-session via
``adjust_temperature(temperature)`` (absolute value). The value is stored
on the agent as ``_session_temperature`` and takes priority in
``resolve_temperature()`` over profile defaults and worker temperature.

Kimi / Moonshot providers do not support temperature — the tool returns
``null`` (no-op) for those providers.

Values outside [0.0, 2.0] are rejected (no change, current value returned).
"""

import json
from typing import Any, Dict, Optional


def _is_kimi_provider(agent) -> bool:
    """Return True when the provider is a Kimi / Moonshot variant."""
    provider = (getattr(agent, "provider", "") or "").lower()
    return provider in {"kimi-coding", "kimi-coding-cn"}


def adjust_temperature_tool(temperature: float, agent) -> str:
    """Set session temperature to an absolute value.

    Args:
        temperature: Absolute temperature value.  Must be in [0.0, 2.0];
                     values outside this range are rejected (no change).
                     0.0 = deterministic (coding/math),
                     1.0 = balanced (data analysis),
                     2.0 = creative (conversation/translation).
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

    # Value in [0.0, 2.0] → set it. Outside → no change, return current.
    if 0.0 <= temperature <= 2.0:
        agent._session_temperature = temperature
        changed = previous is None or abs(temperature - previous) > 0.001
        return json.dumps({
            "temperature": temperature,
            "changed": changed,
            "previous": previous,
        })

    # Out of range → no change. Get the actual running temperature.
    current = _get_current_temperature(agent)
    return json.dumps({
        "temperature": current,
        "changed": False,
        "previous": current,
    })


def _get_current_temperature(agent) -> Optional[float]:
    """Resolve the agent's actual current temperature."""
    # Session override (set by this tool)
    session = getattr(agent, "_session_temperature", None)
    if session is not None:
        return session
    # Profile default
    profile = getattr(agent, "_temperature", None)
    if profile is not None:
        return profile
    # Worker temperature (kanban/delegated)
    worker = getattr(agent, "worker_temperature", None)
    if worker is not None:
        return worker
    return None


# --- Schema ---

ADJUST_TEMPERATURE_SCHEMA = {
    "name": "adjust_temperature",
    "description": (
        "Set the agent's sampling temperature to an absolute value for "
        "the remainder of the session. Values outside [0.0, 2.0] are "
        "rejected (no change). "
        "0.0 = deterministic (coding/math), 1.0 = balanced (data analysis), "
        "2.0 = creative (conversation/translation). "
        "Kimi/Moonshot providers return null (unsupported)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "temperature": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 2.0,
                "description": (
                    "Absolute temperature value. 0.0 = deterministic (coding/math), "
                    "1.0 = balanced (data analysis), 2.0 = creative conversation/translation. "
                    "values outside this range are rejected (no change)."
                ),
            },
        },
        "required": ["temperature"],
    },
}


# --- Registry ---

from tools.registry import registry

registry.register(
    name="adjust_temperature",
    toolset="temperature",
    schema=ADJUST_TEMPERATURE_SCHEMA,
    handler=lambda args, **kw: adjust_temperature_tool(
        temperature=args.get("temperature", 0.7),
        agent=kw.get("agent", None),  # injected at agent-level dispatch; None = registry fallback
    ),
    emoji="🌡️",
)
