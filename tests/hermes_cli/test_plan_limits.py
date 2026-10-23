"""Tests for Plan submission limits — the per-field cap split (t_d3583771).

The Plan is the Protected region of the context window, so its size is a
permanent per-turn cost.  These tests pin the settled per-field caps
(Gopher tested, Evan approved 2026-10-23): goal/global 2K chars, step title
128 UTF-8 bytes, expansion 2K chars (verbatim), summary 240 chars
(middle-cut), denial exempt (see execution_bindings).
"""

from __future__ import annotations
import sys

sys.path.insert(0, ".")

from hermes_cli.plan_limits import (  # noqa: E402
    PLAN_EXPANSION_MAX_CHARS,
    PLAN_GOAL_MAX_CHARS,
    PLAN_MAX_STEPS,
    PLAN_STEP_TITLE_MAX_BYTES,
    PLAN_TEXT_MAX_CHARS,
    cap_expansion,
    cap_goal,
    cap_step_title,
    cap_steps,
    cap_summary,
    cap_text,
    compress_whitespace,
    expansion_text_error,
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
    text = "GOAL" + "x" * 2500 + "TRAILING"
    capped = cap_goal(text)
    assert len(capped) == PLAN_GOAL_MAX_CHARS
    assert capped.startswith("GOAL")
    assert not capped.endswith("TRAILING")


def test_summary_cut_keeps_HEAD_and_TAIL():
    """B1 trim direction (council 2026-10-14): never cut heads — cut middle.

    The old tail-only cap stored summaries starting mid-word ("Summary: ork
    in this rerun"). Now the lead and the conclusion both survive and the
    middle is elided with an explicit marker.
    """
    text = "LEADING" + "x" * 400 + "CONCLUSION"
    capped = cap_summary(text)
    assert len(capped) <= PLAN_TEXT_MAX_CHARS
    assert capped.startswith("LEADING")
    assert capped.endswith("CONCLUSION")
    assert "chars omitted" in capped


def test_short_text_is_untouched_apart_from_whitespace():
    assert cap_text("fix the bug") == "fix the bug"
    assert cap_summary("fix the bug") == "fix the bug"
    assert cap_text("  fix   the  bug ") == "fix the bug"


def test_step_title_cap_is_128_UTF8_BYTES():
    """The split (t_d3583771): step titles are labels, capped at 128 bytes."""
    capped = cap_step_title("t" * 500)
    assert len(capped.encode("utf-8")) == PLAN_STEP_TITLE_MAX_BYTES
    # Multi-byte chars must not split a code point at the cut.
    capped = cap_step_title("é" * 200)
    assert len(capped.encode("utf-8")) <= PLAN_STEP_TITLE_MAX_BYTES
    assert capped and all(ch == "é" for ch in capped)


def test_step_title_keeps_the_HEAD():
    assert cap_step_title("TITLE " + "x" * 500).startswith("TITLE")


def test_expansion_is_verbatim_and_only_end_stripped():
    """Expansions are spec ground truth: no whitespace compression, no cut."""
    text = "\n\n  spec line 1\n\n  spec line 2  \n\n"
    assert cap_expansion(text) == "spec line 1\n\n  spec line 2"
    long = "x" * (PLAN_EXPANSION_MAX_CHARS + 100)
    assert cap_expansion(long) == long, "expansions are NEVER truncated"


def test_caps_are_idempotent():
    """Applied at both the adapter and the kernel — must not double-truncate."""
    text = "word " * 200
    assert cap_text(cap_text(text)) == cap_text(text)
    assert cap_summary(cap_summary(text)) == cap_summary(text)
    assert cap_step_title(cap_step_title(text)) == cap_step_title(text)
    goal = "word " * 500  # 2500 chars > PLAN_GOAL_MAX_CHARS
    assert cap_goal(cap_goal(goal)) == cap_goal(goal)


def test_cap_steps_applies_to_every_step():
    steps = ["s" * 500, "short", "  spaced   step  "]
    capped = cap_steps(steps)
    assert [len(item.encode("utf-8")) for item in capped] == [
        PLAN_STEP_TITLE_MAX_BYTES,
        5,
        11,
    ]
    assert capped[2] == "spaced step"


def test_goal_text_error_refuses_over_the_2K_cap():
    """Truncation is silent data loss — submission must REFUSE (Evan, 2026-09-18)."""
    assert goal_text_error("G" * PLAN_GOAL_MAX_CHARS) is None
    error = goal_text_error("G" * (PLAN_GOAL_MAX_CHARS + 1))
    assert error is not None
    assert str(PLAN_GOAL_MAX_CHARS + 1) in error and str(PLAN_GOAL_MAX_CHARS) in error
    assert "FILE" in error


def test_goal_text_error_measures_after_whitespace_compression():
    """Whitespace collapses first, so a padded-but-short goal still passes."""
    padded = "  G  " * 50 + "\n\n"  # 300 raw chars -> 100 compressed
    assert len(padded) > PLAN_TEXT_MAX_CHARS
    assert goal_text_error(padded) is None


def test_steps_text_error_names_the_offending_step_and_counts_bytes():
    assert steps_text_error(["fine", "also fine"]) is None
    assert steps_text_error(None) is None
    error = steps_text_error(["fine", "s" * 400])
    assert error is not None
    assert "step 2" in error, "must name WHICH step is over the cap"
    assert str(PLAN_STEP_TITLE_MAX_BYTES) in error
    # Byte-based: 128 multi-byte chars exceed 128 bytes.
    error = steps_text_error(["é" * PLAN_STEP_TITLE_MAX_BYTES])
    assert error is not None


def test_expansion_text_error_names_the_offending_step():
    assert expansion_text_error(["ok", ""]) is None
    assert expansion_text_error(None) is None
    error = expansion_text_error(["ok", "x" * (PLAN_EXPANSION_MAX_CHARS + 1)])
    assert error is not None
    assert "step 2" in error
    assert str(PLAN_EXPANSION_MAX_CHARS) in error


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
