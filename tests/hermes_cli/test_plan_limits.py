"""Tests for Plan submission limits (Evan, 2026-09-16).

The Plan is the Protected region of the context window, so its size is a
permanent per-turn cost.  These tests pin the two truncation DIRECTIONS
(head for goal/step, tail for summary/proof) and the 12-step maximum.
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")

from hermes_cli.plan_limits import (  # noqa: E402
    PLAN_MAX_STEPS,
    PLAN_TEXT_MAX_CHARS,
    cap_steps,
    cap_summary,
    cap_text,
    compress_whitespace,
    goal_text_error,
    step_count_error,
    steps_text_error,
)


def test_compress_whitespace_collapses_every_run():
    assert compress_whitespace("fix\n\nthe   bug\t\n") == "fix the bug"
    assert compress_whitespace("") == ""
    assert compress_whitespace(None) == ""  # type: ignore[arg-type]


def test_goal_keeps_the_HEAD_of_the_text():
    """A goal states its objective up front — tail-capping would lose it."""
    text = "GOAL" + "x" * 400 + "TRAILING"
    capped = cap_text(text)
    assert len(capped) == PLAN_TEXT_MAX_CHARS
    assert capped.startswith("GOAL")
    assert not capped.endswith("TRAILING")


def test_summary_keeps_the_LAST_240_chars():
    """Evan's explicit rule: a summary's conclusion is at the end."""
    text = "LEADING" + "x" * 400 + "CONCLUSION"
    capped = cap_summary(text)
    assert len(capped) == PLAN_TEXT_MAX_CHARS
    assert capped.endswith("CONCLUSION")
    assert not capped.startswith("LEADING")


def test_short_text_is_untouched_apart_from_whitespace():
    assert cap_text("fix the bug") == "fix the bug"
    assert cap_summary("fix the bug") == "fix the bug"
    assert cap_text("  fix   the  bug ") == "fix the bug"


def test_caps_are_idempotent():
    """Applied at both the adapter and the kernel — must not double-truncate."""
    text = "word " * 200
    assert cap_text(cap_text(text)) == cap_text(text)
    assert cap_summary(cap_summary(text)) == cap_summary(text)


def test_cap_steps_applies_to_every_step():
    steps = ["s" * 500, "short", "  spaced   step  "]
    capped = cap_steps(steps)
    assert [len(item) for item in capped] == [PLAN_TEXT_MAX_CHARS, 5, 11]
    assert capped[2] == "spaced step"


def test_goal_text_error_refuses_over_the_cap():
    """Truncation is silent data loss — submission must REFUSE (Evan, 2026-09-18)."""
    assert goal_text_error("G" * PLAN_TEXT_MAX_CHARS) is None
    error = goal_text_error("G" * (PLAN_TEXT_MAX_CHARS + 1))
    assert error is not None
    assert "241" in error and str(PLAN_TEXT_MAX_CHARS) in error
    assert "FILE" in error


def test_goal_text_error_measures_after_whitespace_compression():
    """Whitespace collapses first, so a padded-but-short goal still passes."""
    padded = "  G  " * 50 + "\n\n"  # 300 raw chars -> 100 compressed
    assert len(padded) > PLAN_TEXT_MAX_CHARS
    assert goal_text_error(padded) is None


def test_steps_text_error_names_the_offending_step():
    assert steps_text_error(["fine", "also fine"]) is None
    assert steps_text_error(None) is None
    error = steps_text_error(["fine", "s" * 400])
    assert error is not None
    assert "step 2" in error, "must name WHICH step is over the cap"


def test_cap_steps_handles_none_and_empty():
    assert cap_steps(None) == []
    assert cap_steps([]) == []


def test_twelve_steps_allowed():
    assert step_count_error(["s"] * PLAN_MAX_STEPS) is None


def test_thirteen_steps_refused_with_subplan_guidance():
    error = step_count_error(["s"] * (PLAN_MAX_STEPS + 1))
    assert error is not None
    assert "at most 12 steps" in error
    assert "SUBPLANS" in error


def test_step_count_error_on_none():
    assert step_count_error(None) is None
