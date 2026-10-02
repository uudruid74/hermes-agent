from __future__ import annotations

from unittest.mock import patch

from agent.internal_compression_fallback import (
    ProtectedMemory,
    _Unit,
    _canonical_form,
    _persistent_protected,
)


def _unit(text: str, order: int) -> _Unit:
    return _Unit(text=text, order=(order,), token_count=10)


def test_protected_selection_dedups_canonical_forms_before_budget_cap() -> None:
    duplicate_a = _unit(
        "context_compressor.py handles the tool pair sanitizer boundary", 0
    )
    duplicate_b = _unit(
        "the boundary in tool pair sanitizer context_compressor.py handles", 1
    )
    distinct = _unit("rollback command preserves the migration database", 2)
    units = [duplicate_a, duplicate_b, distinct] + [distinct] * 254
    ranked = [(duplicate_a, 3.0), (duplicate_b, 2.0), (distinct, 1.0)]

    assert duplicate_a.text != duplicate_b.text
    assert _canonical_form(duplicate_a.text) == _canonical_form(duplicate_b.text)

    with patch(
        "agent.internal_compression_fallback._rank_units", return_value=ranked
    ):
        chosen, _memory = _persistent_protected(
            units,
            ProtectedMemory(),
            top_k=3,
            area_budget=2,
        )

    assert chosen == [duplicate_a, distinct]
    assert len({_canonical_form(unit.text) for unit in chosen}) == len(chosen)
