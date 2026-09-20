from __future__ import annotations

from agent.llmslim_selector import ScoredUnit, SelectionUnit, select_budgeted_units


def _scored(
    text: str,
    order: int,
    *,
    chunk: int,
    score: float,
) -> ScoredUnit:
    unit = SelectionUnit(
        text=text,
        order=(order,),
        token_count=1,
        role="assistant",
    )
    return ScoredUnit(
        unit=unit,
        chunk_index=chunk,
        score=score,
        centrality=score,
        relevance=0.0,
        instruction=0.0,
        entity=0.0,
        recency=0.0,
    )


def _count_fits(limit: int):
    return lambda units: len(units) <= limit


def test_pass_one_represents_distinct_semantic_chunks() -> None:
    scored = [
        _scored("dominant-a", 0, chunk=0, score=1.0),
        _scored("dominant-b", 1, chunk=0, score=0.9),
        _scored("thin", 2, chunk=1, score=0.2),
    ]

    result = select_budgeted_units(scored, fits=_count_fits(2))

    assert result.state == "selected"
    assert [unit.text for unit in result.units] == ["dominant-a", "thin"]


def test_pass_two_reoffers_deferred_units_until_assembled_candidate_is_full() -> None:
    scored = [
        _scored("first", 0, chunk=0, score=1.0),
        _scored("second", 1, chunk=0, score=0.8),
        _scored("third", 2, chunk=1, score=0.7),
    ]

    result = select_budgeted_units(scored, fits=_count_fits(3))

    assert result.state == "unchanged"
    assert [unit.text for unit in result.units] == ["first", "second", "third"]


def test_emission_is_chronological_not_score_order() -> None:
    scored = [
        _scored("old", 0, chunk=0, score=0.1),
        _scored("new", 2, chunk=1, score=1.0),
        _scored("middle", 1, chunk=2, score=0.8),
    ]

    result = select_budgeted_units(scored, fits=_count_fits(3))

    assert [unit.text for unit in result.units] == ["old", "middle", "new"]


def test_fit_callback_measures_the_exact_chronological_candidate() -> None:
    observed: list[tuple[str, ...]] = []
    scored = [
        _scored("alpha", 0, chunk=0, score=1.0),
        _scored("beta", 1, chunk=1, score=0.9),
    ]

    def assembled_message_fits(units: tuple[SelectionUnit, ...]) -> bool:
        observed.append(tuple(unit.text for unit in units))
        assembled = "[CONTEXT WINDOW COMPRESSED]\n" + "\n".join(
            unit.text for unit in units
        )
        return len(assembled) <= len("[CONTEXT WINDOW COMPRESSED]\nalpha")

    result = select_budgeted_units(scored, fits=assembled_message_fits)

    assert result.state == "selected"
    assert [unit.text for unit in result.units] == ["alpha"]
    assert ("alpha", "beta") in observed


def test_no_fitting_candidate_reports_no_op_explicitly() -> None:
    result = select_budgeted_units(
        [_scored("oversized", 0, chunk=0, score=1.0)],
        fits=lambda _units: False,
    )

    assert result.state == "no-op"
    assert result.units == ()


def test_empty_selection_reports_no_op_without_calling_fit() -> None:
    result = select_budgeted_units(
        [],
        fits=lambda _units: (_ for _ in ()).throw(AssertionError("fit called")),
    )

    assert result.state == "no-op"
    assert result.units == ()
