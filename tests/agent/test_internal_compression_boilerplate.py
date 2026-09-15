"""Boilerplate must never be ranked as context (Evan, 2026-09-15).

LexRank's centrality term rewards any text that recurs across the corpus.
A previous compaction payload's wrapper — ``[CONTEXT WINDOW COMPRESSED]``,
the section headers, the ``--- END OF CONTEXT SUMMARY ---`` rule, and the
``[USER]: [USER]:`` prefixes that stack as payloads are re-ingested — recurs
in EVERY earlier payload.  Measured on a live 4,795-row session, the top two
ranked "units" were pure wrapper.

These tests pin the two properties that matter:

1. Wrapper noise is stripped before chunking, and units that are *only*
   wrapper are dropped from the ranking entirely.
2. Real content is never touched — the filter is narrow by construction,
   so ordinary text (including repeated-but-real lines and contentless
   filler that carries no wrapper) is left exactly as it was.
"""

from __future__ import annotations

import pytest

from agent import internal_compression_fallback as fallback
from agent.internal_compression_fallback import (
    _is_boilerplate_only,
    _is_fragment,
    _rank_units,
    _strip_boilerplate,
    _message_units,
)


# --------------------------------------------------------------------------
# Stripping
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "wrapper",
    [
        "[CONTEXT WINDOW COMPRESSED]",
        "--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---",
        "## Verbatim Recent Context",
        "## Relevant Earlier Context",
        "## Active Plan",
        "## Session Notes",
        "The messages after this summary marker are preserved verbatim.",
        "[This response was interrupted by a user correction.]",
        "The tool list for this conversation has been updated accordingly.",
        "[System note: recalled memory context, NOT new user input]",
    ],
)
def test_wrapper_is_removed_by_stripping(wrapper: str) -> None:
    assert _strip_boilerplate(wrapper).strip() == ""


def test_stacked_role_prefixes_are_collapsed() -> None:
    # An earlier compaction flattened newlines to spaces, so prefixes repeat
    # mid-string; a ``^``-anchored pattern would only catch the first run.
    text = "[USER]: [USER]: [ASSISTANT]: Use the plan_tool to create a plan"
    assert _strip_boilerplate(text) == "Use the plan_tool to create a plan"


def test_real_content_survives_stripping_untouched() -> None:
    text = "[ASSISTANT]: pente_games has exactly 11 columns — no status column"
    assert _strip_boilerplate(text).endswith(
        "pente_games has exactly 11 columns — no status column"
    )
    assert "11 columns" in _strip_boilerplate(text)


def test_wrapper_inline_with_content_keeps_the_content() -> None:
    text = "[USER]: [CONTEXT WINDOW COMPRESSED] ## Active Plan Task: fix pente"
    stripped = _strip_boilerplate(text)
    assert "Task: fix pente" in stripped
    assert "CONTEXT WINDOW COMPRESSED" not in stripped


# --------------------------------------------------------------------------
# The drop filters are narrow
# --------------------------------------------------------------------------


def test_pure_wrapper_is_boilerplate_only() -> None:
    assert _is_boilerplate_only("## Verbatim Recent Context")
    assert _is_boilerplate_only("[CONTEXT WINDOW COMPRESSED]")
    assert _is_boilerplate_only(
        "--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---"
    )


def test_wrapper_bearing_content_is_not_boilerplate_only() -> None:
    assert not _is_boilerplate_only("## Active Plan Task: fix pente defect 4")


def test_plain_text_is_never_boilerplate_only() -> None:
    # No wrapper marker: the filter must not claim it, however thin it is.
    for text in ["[USER]: ]", "[ASSISTANT]: 5.", "[USER]: Step 7: end deletes the row."]:
        assert not _is_boilerplate_only(text)


def test_filler_run_is_not_dropped() -> None:
    # A 1200-char filler chunk tokenizes to ~1 token, but carries no wrapper.
    # Dropping it would silently free budget and change real selection — the
    # exact regression that broke the plan-fallback budget test.
    assert not _is_boilerplate_only("[USER]: " + "x" * 1200)


def test_punctuation_stub_is_a_fragment() -> None:
    assert _is_fragment("[USER]: ]")
    assert _is_fragment("[ASSISTANT]: **2.")


def test_real_line_is_not_a_fragment() -> None:
    assert not _is_fragment("[USER]: Step 7: Step 7: `end` deletes the row.")


# --------------------------------------------------------------------------
# Ranking never surfaces wrapper noise
# --------------------------------------------------------------------------


def _unit(text: str, order: int = 0):
    return fallback._Unit(
        text=text,
        order=(order, 0),
        token_count=fallback.estimate_tokens_rough(text),
    )


def test_boilerplate_units_never_reach_the_ranking() -> None:
    middle = [
        _unit("[USER]: [CONTEXT WINDOW COMPRESSED]", 0),
        _unit("## Verbatim Recent Context", 1),
        _unit(
            "--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---",
            2,
        ),
        _unit("[ASSISTANT]: pente_games has exactly 11 columns", 3),
    ]
    ranked = _rank_units(middle, [], [])
    emitted = [unit.text for unit, _score in ranked]
    assert len(emitted) == 1
    assert "11 columns" in emitted[0]


def test_duplicate_wrapper_units_are_collapsed() -> None:
    # The same plan line arrives twice because two payloads were re-ingested.
    dup = "[USER]: ## Active Plan Step 3: Rewrite the tell path around play."
    middle = [_unit(dup, 0), _unit(dup, 1), _unit("[ASSISTANT]: unrelated note", 2)]
    ranked = _rank_units(middle, [], [])
    texts = [unit.text for unit, _ in ranked]
    assert sum(1 for t in texts if "Rewrite the tell path" in t) == 1
    assert any("unrelated note" in t for t in texts)


def test_repeated_real_lines_are_not_deduped() -> None:
    # Repeated-but-real lines carry no wrapper, so they must all survive:
    # dedupe is scoped to re-ingested wrapper text only.
    line = "[USER]: Retrying the same failing command"
    middle = [_unit(line, 0), _unit(line, 1)]
    ranked = _rank_units(middle, [], [])
    assert len([u for u, _ in ranked if "Retrying" in u.text]) == 2


def test_message_units_emits_stripped_text() -> None:
    messages = [
        {"role": "user", "content": "[CONTEXT WINDOW COMPRESSED] keep this line"},
        {"role": "assistant", "content": "## Verbatim Recent Context"},
    ]
    units = _message_units(messages)
    assert any("keep this line" in u.text for u in units)
    assert all("CONTEXT WINDOW COMPRESSED" not in u.text for u in units)
