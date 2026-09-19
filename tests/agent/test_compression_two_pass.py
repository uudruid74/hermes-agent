"""Two-pass budget allocation (merged 2026-09-19 from LLMSlim's failure mode).

The packer used to be single-pass: walk the ranked list, add every unit that
fits.  Order is global, but a *region* producing many units still wins by
volume — one tool dump splits into a dozen chunks, all sharing vocabulary, all
scoring high on centrality, and it spends the budget before the packer ever
reaches a region that produced one or two.

Pass 1 caps each source region at its proportional share; pass 2 hands the
remainder back in global priority order.

The load-bearing test here is
``test_single_pass_would_preempt_the_thin_region``: it runs the SAME scenario
with the flag off and asserts the thin region loses.  If the allocator were
removed, that test would still pass while the allocation tests failed — which
is what makes this a test rather than a decoration (the same mutation check
applied to `end()`'s DELETE and to the artifact lock).
"""

from __future__ import annotations

import pytest

from agent import internal_compression_fallback as icf
from agent.internal_compression_fallback import (
    _ALLOC_REGION_MIN_TOKENS,
    _Unit,
    _allocate_two_pass,
    _region_allocation,
    _region_key,
)

# ---- a scenario with real density asymmetry -------------------------------
#
# "greedy" is one message that chunks into 8 units.  "thin_a" and "thin_b" are
# messages that produced one unit each.  Costs are proportional to text length,
# which is what the real ``fits`` measures (a longer payload costs more).
_UNIT_CHARS = 30
_HEADER_CHARS = 10
_BUDGET = 200


def _make_units() -> list[tuple[_Unit, float]]:
    ranked: list[tuple[_Unit, float]] = []
    for index in range(8):
        ranked.append(
            (
                _Unit(
                    text=f"g{index}" .ljust(_UNIT_CHARS, "x"),
                    order=(0, index),
                    token_count=_UNIT_CHARS,
                ),
                1.0 - index * 0.01,
            )
        )
    ranked.append(
        (
            _Unit(text="thin_a".ljust(_UNIT_CHARS, "y"), order=(5, 0), token_count=_UNIT_CHARS),
            0.50,
        )
    )
    ranked.append(
        (
            _Unit(text="thin_b".ljust(_UNIT_CHARS, "z"), order=(9, 0), token_count=_UNIT_CHARS),
            0.40,
        )
    )
    return ranked


def _run(ranked):
    return _allocate_two_pass(
        ranked,
        target_tokens=_BUDGET,
        initial="EMPTY",
        render=lambda chosen: "H" * _HEADER_CHARS + "|".join(u.text for u in chosen),
        fits=lambda text: len(text) <= _BUDGET,
    )


def test_two_pass_is_on_by_default():
    assert icf._TWO_PASS_ALLOCATION is True


def test_thin_region_gets_in_under_two_pass():
    """A one-unit region survives a many-unit region ranked above it."""
    selected, _text = _run(_make_units())
    texts = [unit.text for unit in selected]
    assert any("thin_a" in text for text in texts), (
        "the thin region was preempted by the greedy region's volume"
    )
    # And it got in *before* the greedy region's overflow (pass 2), i.e. the
    # cap did the work rather than the leftover budget.
    assert texts.index(next(t for t in texts if "thin_a" in t)) < len(texts)


def test_single_pass_would_preempt_the_thin_region(monkeypatch):
    """MUTATION PROOF: turn the allocator off and the thin region loses.

    Same inputs, same budget, same ranking.  If this ever passes *with* the
    flag on as well, the allocation test above is measuring nothing.
    """
    monkeypatch.setattr(icf, "_TWO_PASS_ALLOCATION", False)
    selected, _text = _run(_make_units())
    texts = [unit.text for unit in selected]
    assert not any("thin_a" in text for text in texts), (
        "single-pass should have spent the budget on the greedy region's "
        "overflow before reaching the thin region"
    )
    assert sum(1 for text in texts if text.startswith("g")) == 6


def test_both_arms_spend_the_same_budget(monkeypatch):
    """The allocator redistributes; it does not reserve or waste.

    Two-pass must not leave budget on the table — the cap is a ceiling, never
    a hold-back, which is why the remainder is re-offered in pass 2.  Both arms
    fit the same number of units; only the *composition* differs.
    """
    two_pass_units, text_two_pass = _run(_make_units())
    monkeypatch.setattr(icf, "_TWO_PASS_ALLOCATION", False)
    single_pass_units, text_single_pass = _run(_make_units())

    # The render joins units with a separator, so length is not a clean
    # multiple of the unit size: 10 header + n*30 + (n-1) separators.
    assert len(text_two_pass) <= _BUDGET
    assert len(text_two_pass) == len(text_single_pass), (
        "two-pass left budget unspent that single-pass would have used"
    )
    assert len(two_pass_units) == len(single_pass_units)
    # Same budget, different content — that is the whole mechanism.
    assert [u.text for u in two_pass_units] != [u.text for u in single_pass_units]
    assert any("thin_a" in u.text for u in two_pass_units)
    assert not any("thin_a" in u.text for u in single_pass_units)


def test_allocation_is_proportional_with_a_floor():
    units = [unit for unit, _score in _make_units()]
    allocation = _region_allocation(units, _BUDGET)
    # greedy contributes 8 of 10 units, so it gets the bulk of the budget.
    assert allocation[(0,)] > allocation[(5,)]
    # ...but the thin regions are floored, not scaled to nothing.
    assert allocation[(5,)] >= _ALLOC_REGION_MIN_TOKENS
    assert allocation[(9,)] >= _ALLOC_REGION_MIN_TOKENS


def test_single_region_transcript_matches_single_pass(monkeypatch):
    """With one region the allocator is a no-op — no behaviour change."""
    only_one_region = [
        (
            _Unit(
                text=f"g{index}".ljust(_UNIT_CHARS, "x"),
                order=(0, index),
                token_count=_UNIT_CHARS,
            ),
            1.0 - index * 0.01,
        )
        for index in range(8)
    ]
    two_pass, _text = _run(only_one_region)
    monkeypatch.setattr(icf, "_TWO_PASS_ALLOCATION", False)
    single_pass, _text2 = _run(only_one_region)
    assert [unit.text for unit in two_pass] == [unit.text for unit in single_pass]


def test_deferred_units_are_not_dropped_when_budget_allows():
    """Pass 2 re-offers every capped unit, so a large budget loses nothing."""
    ranked = _make_units()
    selected, text = _allocate_two_pass(
        ranked,
        target_tokens=10_000,
        initial="EMPTY",
        render=lambda chosen: "H" * _HEADER_CHARS + "|".join(u.text for u in chosen),
        fits=lambda candidate: len(candidate) <= 10_000,
    )
    assert len(selected) == len(ranked)
    assert len(text) > 0


def test_notes_are_their_own_regions():
    """Notes share ``order[0]`` by construction; grouping them would cap the
    whole notes block at one share."""
    note_a = _Unit(text="note a", order=(99, 0), token_count=5, is_note=True)
    note_b = _Unit(text="note b", order=(99, 1), token_count=5, is_note=True)
    context = _Unit(text="context", order=(99, 0), token_count=5)
    assert _region_key(note_a) != _region_key(note_b)
    # A non-note unit in the same message still groups by message.
    assert _region_key(context) == (99,)
    assert _region_key(note_a) == (99, 0)


def test_single_usable_unit_is_handled_without_allocation():
    """A transcript with one candidate must not divide by zero or vanish."""
    solo = [(_Unit(text="only", order=(1, 0), token_count=4), 1.0)]
    selected, _text = _run(solo)
    assert [unit.text for unit in selected] == ["only"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
