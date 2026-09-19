"""Boilerplate must never be ranked as context (Evan, 2026-09-15).

LexRank's centrality term rewards any text that recurs across the corpus.
A previous compaction payload's wrapper — ``[CONTEXT WINDOW COMPRESSED]``,
the section headers, the ``--- END OF CONTEXT SUMMARY ---`` rule, and the
``[USER]: [USER]:`` prefixes that stack as payloads are re-ingested — recurs
in EVERY earlier payload.  Measured on a live 4,795-row session, the top two
ranked "units" were pure wrapper.

These tests pin the properties that matter:

1. Wrapper noise is stripped before chunking, and units that are *only*
   wrapper are dropped from the ranking entirely.
2. Near-duplicate units collapse on their content-word canonical form, and
   units too short to carry context never reach the ranking (Evan,
   2026-09-15 — the emitted area was 59% fragments under 10 content tokens).
3. Real content survives: a unit that clears the floor is left exactly as it
   was, and a transcript made only of short lines still ranks something
   rather than emptying the block.
"""

from __future__ import annotations

from typing import Any

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
    middle = [
        _unit(dup, 0),
        _unit(dup, 1),
        _unit("[ASSISTANT]: unrelated note about the sessions index", 2),
    ]
    ranked = _rank_units(middle, [], [])
    texts = [unit.text for unit, _ in ranked]
    assert sum(1 for t in texts if "Rewrite the tell path" in t) == 1
    assert any("unrelated note" in t for t in texts)


def test_repeated_real_lines_are_deduped() -> None:
    # Evan, 2026-09-15: dedupe is NOT scoped to wrapper text.  Near-duplicate
    # real lines were filling the emitted area — the top of the ranking was a
    # clique of step echoes that reinforced each other's centrality — so the
    # canonical form (content words, function words dropped) collapses them.
    line = "[USER]: Retrying the same failing migration command now"
    middle = [_unit(line, 0), _unit(line, 1)]
    ranked = _rank_units(middle, [], [])
    assert len([u for u, _ in ranked if "Retrying" in u.text]) == 1


def test_canonical_form_ignores_word_order_and_function_words() -> None:
    # Two re-quoted wordings of one line carry the same information, so the
    # canonical form compares content words as a set.
    first = "[USER]: The delete of create/join must not happen in production"
    second = "[USER]: in production must not happen the delete of create/join"
    ranked = _rank_units([_unit(first, 0), _unit(second, 1)], [], [])
    assert len([u for u, _ in ranked if "delete" in u.text]) == 1


def test_short_stub_units_are_not_ranked() -> None:
    # Measured: 59% of the emitted area was under 10 content tokens, and
    # "Step 5/9" (1 content token) was occupying the top of the ranking.
    middle = [
        _unit("[USER]: Step 5/9", 0),
        _unit("[USER]: pente_games has exactly eleven columns defined", 1),
    ]
    ranked = _rank_units(middle, [], [])
    texts = [unit.text for unit, _ in ranked]
    assert any("eleven columns" in t for t in texts)
    assert not any("Step 5/9" in t for t in texts)


def test_all_short_transcript_still_ranks_something() -> None:
    # A floor must not be able to empty the area: an empty block is worse than
    # a weak one, since the caller has no other middle context to fall back on.
    middle = [_unit("[USER]: step one", 0), _unit("[USER]: step two", 1)]
    ranked = _rank_units(middle, [], [])
    assert len(ranked) == 2


def test_agent_narration_is_dropped() -> None:
    # Evan, 2026-09-15: narration chatter was 59.7% of the emitted area in
    # 17/17 payloads.  Throw it out rather than rank it.
    middle = [
        _unit("[ASSISTANT]: Let me check the real current state of the code:", 0),
        _unit("[ASSISTANT]: Now let me see the `create()` tell branch", 1),
        _unit("[ASSISTANT]: The DB schema has 11 columns in pente_games", 2),
    ]
    ranked = _rank_units(middle, [], [])
    texts = [unit.text for unit, _ in ranked]
    assert not any("Let me check" in t for t in texts)
    assert not any("let me see" in t.lower() for t in texts)
    assert any("11 columns" in t for t in texts)


def test_user_requests_are_never_treated_as_narration() -> None:
    # "Let me know when it's done" is the user asking for something; only the
    # assistant's own "let me do X" lines are chatter.  Role decides it.
    middle = [
        _unit("[USER]: Let me know when the migration is done and verified", 0),
        _unit("[USER]: Implement the fixes, in order, then work on step 6", 1),
    ]
    ranked = _rank_units(middle, [], [])
    texts = [unit.text for unit, _ in ranked]
    assert any("Let me know" in t for t in texts)
    assert any("Implement the fixes" in t for t in texts)


def test_findings_are_not_mistaken_for_narration() -> None:
    # A finding that happens to start with a narration-ish word must survive.
    middle = [
        _unit("[ASSISTANT]: Looking at the traceback, line 42 raises KeyError", 0),
        _unit("[ASSISTANT]: Here is what I found: the sessions schema is wrong", 1),
        _unit("[ASSISTANT]: The leaderboard table stores agents, wins and losses", 2),
    ]
    ranked = _rank_units(middle, [], [])
    texts = [unit.text for unit, _ in ranked]
    assert any("Looking at the traceback" in t for t in texts)
    assert any("what I found" in t for t in texts)


def test_message_units_prunes_a_real_payload_whole() -> None:
    """A payload whose marker is at position 0 is machinery, not content.

    SUPERSEDES the previous assertion that "[CONTEXT WINDOW COMPRESSED] keep
    this line" yields a rankable unit.  Evan, 2026-09-16: *"that is a hermes
    compression output format. That isn't supposed to be there"* … *"we need
    to regex prune those results when they hit mid-window."*  Every section of
    a payload is regenerated each cycle (Plan from the kanban DB, heap by
    LexRank, tail by protect_last_n), so ranking its body spends the budget on
    a copy of what the window already holds — measured at 98-99% self-copy
    across 14 consecutive payload pairs on the live Ornith session.
    """
    messages = [
        {"role": "user", "content": "[CONTEXT WINDOW COMPRESSED]  ## Relevant Earlier Context"},
        {"role": "assistant", "content": "## Verbatim Recent Context"},
    ]
    units = _message_units(messages)
    assert not any("CONTEXT WINDOW COMPRESSED" in u.text for u in units)
    # The non-payload second message still strips its wrapper heading.
    assert not any("Verbatim Recent Context" in u.text for u in units)


def test_message_units_still_emits_content_that_merely_quotes_a_marker() -> None:
    """The body of genuine content survives; only payload PREFIXES are pruned.

    A session_search result that quotes a payload carries the marker
    mid-string.  That is ordinary content and must still rank — this is the
    case a naive substring test got wrong.  Note the role: a ``tool`` message
    is already excluded as an observation, so the quoting case only matters
    for roles that DO reach the ranker.
    """
    messages = [
        {
            "role": "user",
            "content": 'Quoted from an earlier session: "[CONTEXT WINDOW COMPRESSED] ..." and keep this line',
        },
        {"role": "assistant", "content": "## Verbatim Recent Context"},
    ]
    units = _message_units(messages)
    assert any("keep this line" in u.text for u in units)
    assert not any("Verbatim Recent Context" in u.text for u in units)


# --- Repeat gate (Evan, 2026-09-15) --------------------------------------
# The heap must not spend budget re-stating text the window already holds.
# Scope: the VERBATIM TAIL is the repeat source.  The plan/summary text is
# deliberately NOT a tier — it stands in for the session-wide LexRank score
# (the Dax half, not yet built), so it is not a separate class of content.


def test_unit_repeated_from_the_tail_is_dropped() -> None:
    tail = fallback._content_tokens(
        "the parser raises on empty input and the cache key is stale"
    )
    assert fallback._heap_repeat_covered(
        "the parser raises on empty input", [tail]
    ) is True


def test_unit_carrying_new_information_is_kept() -> None:
    tail = fallback._content_tokens(
        "the parser raises on empty input and the cache key is stale"
    )
    assert fallback._heap_repeat_covered(
        "leaderboard reads through list and pente_leaderboard", [tail]
    ) is False


def test_empty_or_missing_tier_never_marks_a_repeat() -> None:
    assert fallback._heap_repeat_covered("anything at all", [frozenset()]) is False
    assert fallback._heap_repeat_covered("anything at all", []) is False


def test_empty_unit_is_never_a_repeat() -> None:
    tail = fallback._content_tokens("some tail text")
    assert fallback._heap_repeat_covered("", [tail]) is False


def test_plan_summary_is_not_treated_as_a_repeat_source() -> None:
    # The summary text must NOT be passed as a tier.  Guard the call sites.
    import inspect

    for fn in (fallback._add_plan_lexrank_area, fallback.build_internal_fallback):
        src = inspect.getsource(fn)
        assert "_content_tokens(summary)" not in src
        assert '_content_tokens("\\n".join(notes))' not in src


def test_notes_are_exempt_from_the_repeat_gate() -> None:
    """A note fully covered by the verbatim tail still reaches the payload.

    Behaviour, not source text.  This test used to grep the function source for
    ``"not unit.is_note"`` — which broke the moment the repeat gate moved into
    the two-pass packer's ``accept`` callable, even though the exemption was
    intact.  A source-text assertion cannot tell "the rule is gone" from "the
    rule moved", which is the same defect class as a test that asserts on a
    function's return value instead of the database it was supposed to write.

    The property under test: notes route separately from the heap's repeat
    gate, so a note whose content words are all present in the verbatim tail is
    still emitted as a note.
    """
    note = "Critical deployment note: never restart the database automatically."
    # Tail carries every content word of the note, so the repeat gate would
    # mark it a duplicate if it were applied to notes.
    msgs: list[dict[str, Any]] = [
        {"role": "system", "content": "sys"},
        {
            "role": "assistant",
            "content": (
                "Critical deployment note: never restart the database "
                "automatically. " + "padding prose about the ledger and the "
                "harbour and the almanac. "
            ),
        },
        {"role": "user", "content": "carry on with the work please"},
    ]
    res = fallback.build_internal_fallback(
        msgs,
        protect_head_count=1,
        protect_last_n=1,
        target_tokens=4000,
        memory_context=note,
    )
    assert "never restart the database automatically" in res.summary, (
        "the note was treated as a repeat of the verbatim tail"
    )
    # And it is emitted in the notes section, not mixed into the heap.
    assert "## Session Notes" in res.summary
