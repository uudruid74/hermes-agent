"""The artifact lock must be load-bearing: dropping it must FAIL this test.

Companion to _is_inviolable / _lock_first in agent/internal_compression_fallback.

The failure this guards (2026-09-19): a coding session's file paths, pids and
error codes score near-zero on every signal the selector has — rare, short, no
shared vocabulary — so a budget squeeze drops exactly the details the session
cannot continue without.  LLMSlim's Tier 3 and TopoCompress's evidence argument
both hard-lock them by regex, with no model call.
"""
from __future__ import annotations

from agent.internal_compression_fallback import (
    _Unit,
    _carries_inviolable,
    _is_inviolable,
    _lock_first,
    _rank_units,
    _inviolable_missing,
    _MAX_INVIOLABLE_UNITS,
    _INVIOLABLE_MAX_TOKENS,
)
from agent.model_metadata import estimate_tokens_rough


def _u(text: str, order: int) -> _Unit:
    return _Unit(text=text, order=(order, 0),
                 token_count=estimate_tokens_rough(text))


def _long(text: str, order: int) -> _Unit:
    """A unit long enough to be over the lock's length gate."""
    filler = "the quick brown fox jumps over the lazy dog and keeps going " * 12
    return _u(f"{filler} see /home/ekl/x.py for details", order)


# --- detection ------------------------------------------------------------

def test_paths_are_detected():
    for text in [
        "edited /home/ekl/.hermes/hermes-agent/agent/system_prompt.py",
        "wrote ./tests/agent/test_prompt_caching.py",
        "touched context_compressor.py",
        "migrations/001_pente_schema.sql is the schema of record",
    ]:
        assert _carries_inviolable(text), text


def test_identifiers_are_detected():
    for text in [
        "killed pid 1476953",
        "exit_code 137",
        "ENOENT on the temp dir",
        "listening on 1234/tcp",
        "0xdeadbeef in the header",
        "_MAX_CANDIDATE_UNITS is 256",
    ]:
        assert _carries_inviolable(text), text


def test_plain_prose_is_not_detected():
    for text in [
        "the compression selector ranked the units by centrality",
        "I verified the shape of the dispatch and not its contents",
        "let me check the code",
    ]:
        assert not _carries_inviolable(text), text


def test_bare_numbers_are_deliberately_not_locked():
    """Numbers are the common case in tool output; locking them is noise."""
    assert not _carries_inviolable("37 rows, min=5 max=42")
    assert not _carries_inviolable("step 3 of 12")


# --- the length gate is the price of the lock ----------------------------

def test_short_artifact_unit_is_inviolable():
    assert _is_inviolable(_u("deleted /home/ekl/tmp/probe.py", 0))


def test_long_unit_is_not_locked_even_with_a_path():
    """One mention of a path must not protect three paragraphs of prose."""
    unit = _long("", 0)
    assert _carries_inviolable(unit.text)          # it does carry one
    assert not _is_inviolable(unit)                # but it is not locked


# --- the lock reorders selection -----------------------------------------

def test_lock_moves_artifacts_to_the_front():
    ranked = [
        (_u("central prose about the architecture", 0), 9.9),
        (_u("central prose about the selector", 1), 9.8),
        (_u("edited agent/system_prompt.py", 2), 0.1),      # ranked LAST
    ]
    locked = _lock_first(ranked)
    assert locked[0][0].text == "edited agent/system_prompt.py"
    # order WITHIN each group is preserved
    assert [u.text for u, _ in locked[1:]] == [
        "central prose about the architecture",
        "central prose about the selector",
    ]


def test_lock_respects_its_cap():
    ranked = [(_u(f"wrote file{i}/x.py", i), float(-i)) for i in range(40)]
    locked = _lock_first(ranked)
    promoted = [u for u, _ in locked[:_MAX_INVIOLABLE_UNITS] if _is_inviolable(u)]
    assert len(promoted) == _MAX_INVIOLABLE_UNITS
    # the promotion stopped there — index 12 is a normal unit
    assert all(_is_inviolable(u) for u, _ in locked[:_MAX_INVIOLABLE_UNITS])


def test_lock_is_a_noop_when_nothing_qualifies():
    ranked = [
        (_u("plain prose one", 0), 5.0),
        (_u("plain prose two", 1), 4.0),
    ]
    assert _lock_first(ranked) == ranked


# --- the lock must be verifiable, not merely attempted -------------------

def test_missing_reports_artifacts_that_did_not_survive():
    kept = _u("edited /home/ekl/kept.py", 0)
    dropped = _u("edited /home/ekl/dropped.py", 1)
    summary = f"## Protected\n{kept.text}"
    assert _inviolable_missing(summary, [kept, dropped]) == [dropped.text]
    assert _inviolable_missing(summary, [kept]) == []


# --- integration: the CALL SITE, not just the helper ----------------------
# A unit test of _lock_first passes even with the call removed from _rank_units.
# These drive the real ranking entry point so the wiring is what is tested.

def _prose(i: int, topic: str = "") -> _Unit:
    """Distinct prose units — the canonical form drops digits and function
    words, so numbered variants of ONE sentence collapse to a single unit and
    a fixture built that way exercises dedupe, not the lock."""
    topics = [
        "centrality weighting over the similarity graph",
        "relevance scoring against the newest work",
        "recency decay applied to the ranked middle",
        "protected terms removed from the heap scoring pass",
        "heap boundary at the active plan step change",
        "verbatim tail preservation above the emitted block",
    ]
    return _u(
        f"The compression selector ranked the middle units using "
        f"{topic or topics[i % len(topics)]} so the emitted block carries "
        f"{topic or topics[i % len(topics)]} forward",
        i,
    )


def test_rank_units_promotes_artifacts_above_higher_scoring_prose():
    middle = [_prose(i) for i in range(6)]
    middle.append(_u("edited agent/system_prompt.py", 6))       # ranked last
    middle.append(_u("killed pid 1476953", 7))
    ranked = _rank_units(middle, [], [])
    assert len(ranked) == len(middle), [u.text[:40] for u, _ in ranked]
    top = [u.text for u, _ in ranked[:2]]
    assert "edited agent/system_prompt.py" in top, top
    assert "killed pid 1476953" in top, top


def test_rank_units_keeps_order_when_no_artifacts_present():
    middle = [_prose(i) for i in range(5)]
    ranked = _rank_units(middle, [], [])
    assert len(ranked) == len(middle)
    assert not any(_is_inviolable(u) for u, _ in ranked)
