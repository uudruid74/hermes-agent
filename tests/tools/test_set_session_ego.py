"""Tests for the set_session ego argument (t_9b241a99 follow-up).

Pins the two defects:
  1. A numeric `ego` delta must parse (was: silently ignored).  The task's
     fix added this — but never ran, because a stray `\"\"\"` made the module
     a SyntaxError.
  2. `set_agent_last_ego` must receive the ego WORD, not the raw argument.
     `ego=-1` was being persisted literally as the string "-1", which
     turn_context.py injects verbatim into the context block.
"""
from __future__ import annotations

import tools.set_session_tool as T


def test_numeric_ego_deltas_parse_to_words() -> None:
    assert T._parse_ego(-1) == ("poor", -1.0)
    assert T._parse_ego(-0.5) == ("low", -0.5)
    assert T._parse_ego(0) == ("normal", 0.0)
    assert T._parse_ego(0.5) == ("happy", 0.5)
    assert T._parse_ego(1) == ("loved", 1.0)


def test_numeric_ego_clamps_and_rounds() -> None:
    assert T._parse_ego(1.2) == ("loved", 1.0)
    assert T._parse_ego(-5) == ("poor", -1.0)
    assert T._parse_ego(5) == ("loved", 1.0)


def test_string_ego_words_still_work() -> None:
    assert T._parse_ego("happy") == ("happy", 0.5)
    assert T._parse_ego("  LOVED  ") == ("loved", 1.0)


def test_unknown_string_ego_is_rejected() -> None:
    assert T._parse_ego("bogus") is None
    assert T._parse_ego("") is None


def test_schema_accepts_both_string_and_number() -> None:
    ego = T.SET_SESSION_SCHEMA["parameters"]["properties"]["ego"]
    assert {"type": "string"} in ego["oneOf"]
    assert {"type": "number"} in ego["oneOf"]


def test_last_ego_persists_the_word_not_the_raw_argument() -> None:
    """Regression: ego=-1 must not be stored/injected as the literal '-1'."""
    import inspect

    src = inspect.getsource(T.set_session_tool)
    assert "set_agent_last_ego(agent_name_rating, word)" in src
    assert "set_agent_last_ego(agent_name_rating, ego)" not in src


def test_module_imports_and_is_callable() -> None:
    # The whole bug was a module-level SyntaxError; importing is the guard.
    assert callable(T.set_session_tool)
