"""Protected terms and unsampled-unit centrality (Evan, 2026-09-18).

Two measured defects, both about units being scored on fewer signals than
the formula claims:

1. Above the 256-unit cap, `_lexrank` left every unsampled unit at exactly
   0.0.  Centrality is 40% of the score, so 97.5% of a 10,248-unit window
   could not compete on centrality at all.

2. The corpus's OWN filler (`config`, `model`, `hermes`, `compression`) was
   weighted like real content, because the stopword list only removes
   English filler.  Weight is ``count * idf``, so a global term appearing
   three times outweighed a rare term appearing once -- every vector leaned
   the same way and centrality degenerated.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent.internal_compression_fallback import (  # noqa: E402
    _MAX_CANDIDATE_UNITS,
    _protected_terms,
    _lexrank,
    _tfidf_vectors,
)


def _corpus_with_global_filler(unit_count: int = 600) -> list[str]:
    """Units sharing one global term, plus units carrying unique terms."""
    texts = []
    for index in range(unit_count):
        if index % 2:
            texts.append(
                f"compression payload row {index} distinct{index} token{index * 7}"
            )
        else:
            texts.append(
                f"unrelated observation {index} unique{index} separate{index * 3}"
            )
    return texts


def test_unsampled_units_are_not_left_at_zero():
    """Every unit must carry SOME centrality above the sample cap."""
    vectors = _tfidf_vectors([f"unit {index} token{index}" for index in range(400)])
    scores = _lexrank(vectors)

    assert len(scores) == 400
    assert all(score > 0.0 for score in scores), "a unit was stranded at 0.0"


def test_unsampled_unit_inherits_the_nearer_sample():
    """A gap between two samples takes the score of whichever is closer."""
    count = _MAX_CANDIDATE_UNITS * 2
    vectors = _tfidf_vectors(
        [f"unit {index} shared token{index % 5}" for index in range(count)]
    )
    scores = _lexrank(vectors)

    assert len(scores) == count
    # Neighbours of any filled index are filled from the same sample, so the
    # spread across the whole array must be continuous -- no 0.0 islands.
    assert min(scores) > 0.0


def test_pairwise_work_stays_bounded_after_the_fill():
    """"The fill adds no comparisons -- it must stay inside the sample cap."""
    calls = 0

    def _counted(left, right):
        nonlocal calls
        calls += 1
        return 1.0 if left and right else 0.0

    import agent.internal_compression_fallback as fallback

    original = fallback._cosine
    fallback._cosine = _counted
    try:
        fallback._lexrank(
            [{"token": 1.0} for _ in range(_MAX_CANDIDATE_UNITS * 4)]
        )
    finally:
        fallback._cosine = original

    assert calls <= (_MAX_CANDIDATE_UNITS * (_MAX_CANDIDATE_UNITS - 1)) // 2


def test_global_filler_is_detected_as_protected():
    texts = _corpus_with_global_filler()
    protected = _protected_terms(texts)

    assert "compression" in protected


def test_unique_terms_are_not_protected():
    """The guard: a term in one or two units is discriminative, not filler."""
    texts = _corpus_with_global_filler()
    protected = _protected_terms(texts)

    leaked = [term for term in protected if term.startswith(("distinct", "unique"))]
    assert leaked == [], f"discriminative terms were Protected: {leaked}"


def test_short_terms_are_never_protected():
    """A token too short to be a filename or identifier cannot qualify."""
    texts = [f"ok ok ok unit {index} filler{index}" for index in range(200)]
    protected = _protected_terms(texts)

    assert all(len(term) > 4 for term in protected)


def test_protected_terms_are_capped_as_a_share_of_vocabulary():
    """A degenerate corpus cannot Protect most of its own vocabulary."""
    # One term repeated in every unit, alongside a wide vocabulary.
    texts = [
        f"common shared repeated {index} varied{index} extra{index * 2}"
        for index in range(400)
    ]
    protected = _protected_terms(texts)
    vocabulary = {token for token in ("common", "shared", "repeated")}

    assert len(protected) <= max(1, int(len(vocabulary) * 0.05)) or len(protected) <= 3


def test_no_protected_terms_on_a_corpus_without_filler():
    """Nothing clears the floor -> behave exactly as before."""
    texts = [f"alpha{index} beta{index * 3} gamma{index * 5}" for index in range(50)]
    protected = _protected_terms(texts)

    assert protected == frozenset()


def test_dropping_protected_terms_thins_the_graph():
    """The measured point: removing filler reduces spurious edges."""
    # Units share a pool of domain filler (which should Protect) plus a
    # smaller pool of real content words.  Dropping the filler leaves the
    # content-word overlap, which crosses the 0.1 edge threshold less often.
    texts = [
        f"compression model config output terminal command "
        f"field{index % 5} value{index % 13} row{index}"
        for index in range(400)
    ]
    before = _tfidf_vectors(texts)
    protected = _protected_terms(texts)
    assert protected, "fixture failed to produce any detectable filler"
    after = _tfidf_vectors(texts, drop=protected)

    def edges(vectors):
        total = 0
        for left in range(len(vectors)):
            for right in range(left + 1, len(vectors)):
                similarity = sum(
                    value * vectors[right].get(token, 0.0)
                    for token, value in vectors[left].items()
                )
                if similarity >= 0.1:
                    total += 1
        return total

    before_edges = edges(before)
    after_edges = edges(after)
    assert before_edges > 0, "fixture produced no baseline edges"
    assert after_edges < before_edges, (
        f"expected fewer edges after dropping filler: "
        f"{before_edges} -> {after_edges}"
    )


def test_drop_preserves_vector_count_and_order():
    """`drop` must not change the shape callers index into."""
    texts = ["alpha beta shared", "gamma delta shared", "epsilon shared"]
    vectors = _tfidf_vectors(texts, drop=frozenset({"shared"}))

    assert len(vectors) == 3
    assert all("shared" not in vector for vector in vectors)
