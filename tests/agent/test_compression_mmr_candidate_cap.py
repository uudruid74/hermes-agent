"""Candidate-cap boundary for MMR reranking (fix-compressor, t_fc6b69d2).

``_mmr_order`` is O(n^2) on its INPUT. Before this fix the
``[:_TIER_CANDIDATE_UNITS]`` slice ran AFTER MMR, so the full compressible
window fed pairwise Jaccard (4.5M+ pairs) and deterministic compression
stalled 100-330s per attempt before the idle watchdog cancelled it. These
tests pin the cap at the MMR boundary at both ranked call sites so it cannot
silently migrate again, keep Evan's <5s bar honest on the 460K-token repro
fixture, and check the composition change preserves the protected path's
top-12 selection.
"""

from __future__ import annotations

import random
import time

import agent.internal_compression_fallback as icf
from agent.internal_compression_fallback import build_internal_fallback

_PROSE = [
    "agent", "compress", "context", "window", "session", "message",
    "summary", "payload", "buffer", "stream", "protocol", "gateway",
    "channel", "timeout", "retry", "backoff", "cancel", "fence",
    "commit", "rollback", "snapshot", "checkpoint", "recovery", "probe",
    "budget", "ceiling", "reserve", "marker", "spine", "ledger",
    "schema", "column", "query", "index", "cursor", "transaction",
    "replica", "primary", "shard", "lease", "token", "kernel",
    "scheduler", "queue", "worker", "thread", "pool", "socket",
    "packet", "header", "telemetry", "metric", "sample", "profile",
    "trace", "span", "event", "journal", "archive", "restore", "verify",
]


def _mixed_corpus(n_messages: int, words_each: int) -> list[dict[str, str]]:
    """Repro-shape corpus with short artifact-bearing (inviolable) units.

    Every 10th message is a short path line so the artifact lock has
    something to lock; the rest are prose so the bulk of candidates come
    from ordinary scoring.
    """
    random.seed(11)
    messages: list[dict[str, str]] = []
    for index in range(n_messages):
        if index % 10 == 0:
            content = f"patched agent/module_{index:04d}.py"
        else:
            content = " ".join(
                random.choice(_PROSE) for _ in range(words_each)
            )
        messages.append(
            {
                "role": "user" if index % 2 == 0 else "assistant",
                "content": content,
            }
        )
    return messages


def _repro_corpus() -> list[dict[str, str]]:
    """The bug file's 680-msg / ~460K-token benchmark fixture."""
    random.seed(11)
    return [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": " ".join(random.choice(_PROSE) for _ in range(620)),
        }
        for index in range(680)
    ]


def _mmr_reference_naive(ranked, lam):
    """The pre-fix _mmr_order semantics verbatim: max re-scanned per step.

    Spec anchor for test_incremental_mmr_is_identical_to_naive_reference.
    Slow by construction (O(n^3)); keep the fixture small.
    """
    remaining = list(ranked)
    token_sets = {
        id(unit): icf._content_tokens(unit.text) for unit, _score in remaining
    }
    chosen = []
    picked_tokens = []
    while remaining:
        best_index = 0
        best_value = float("-inf")
        for index, (unit, score) in enumerate(remaining):
            tokens = token_sets[id(unit)]
            overlap = 0.0
            if tokens and picked_tokens:
                for previous in picked_tokens:
                    union = len(tokens | previous)
                    if union:
                        similarity = len(tokens & previous) / union
                        if similarity > overlap:
                            overlap = similarity
            value = score - lam * overlap
            if value > best_value:
                best_value = value
                best_index = index
        unit, score = remaining.pop(best_index)
        chosen.append((unit, score))
        picked_tokens.append(token_sets[id(unit)])
    return chosen


def test_incremental_mmr_is_identical_to_naive_reference():
    """Incremental max-similarity must reproduce the naive semantics exactly.

    Same picks, same order, same tie-breaks: only the work spent computing
    max similarity may differ.
    """
    messages = _mixed_corpus(48, 25)
    messages.append({"role": "assistant", "content": "ok"})
    messages.append({"role": "user", "content": "ok"})
    units = icf._message_units(messages)
    pairs = [(unit, float(97 - index)) for index, unit in enumerate(units)]
    # Force a score tie so the strict-greater first-index tie-break is exercised
    # on both sides.
    pairs[5] = (pairs[5][0], 42.0)
    pairs[6] = (pairs[6][0], 42.0)

    fast = icf._mmr_order(pairs)
    naive = _mmr_reference_naive(pairs, icf._MMR_LAMBDA)

    assert [(unit.text, score) for unit, score in fast] == [
        (unit.text, score) for unit, score in naive
    ]


def test_mmr_order_input_never_exceeds_candidate_cap(monkeypatch):
    """The cap bounds _mmr_order's INPUT at every ranked call site."""
    observed: list[int] = []
    original = icf._mmr_order

    def spy(ranked, *, lam=icf._MMR_LAMBDA):
        observed.append(len(ranked))
        return original(ranked, lam=lam)

    monkeypatch.setattr(icf, "_mmr_order", spy)

    messages = _mixed_corpus(600, 30)
    units = icf._message_units(messages)
    assert len(units) > icf._TIER_CANDIDATE_UNITS

    plain = icf._rank_units(units, [], units)
    assert observed, "plain ranking path never invoked _mmr_order"
    assert max(observed) <= icf._TIER_CANDIDATE_UNITS
    assert len(plain) <= icf._TIER_CANDIDATE_UNITS

    build_internal_fallback(
        messages,
        protect_head_count=4,
        protect_last_n=21,
        target_tokens=40000,
    )
    assert max(observed) <= icf._TIER_CANDIDATE_UNITS


def test_semantic_rank_output_respects_candidate_cap(monkeypatch):
    """Site 2 (semantic path) keeps its own [:cap] boundary after promotion."""
    messages = _mixed_corpus(600, 30)
    units = icf._message_units(messages)

    ranked = icf._rank_units(units, [], units, semantic=True)
    assert ranked
    assert len(ranked) <= icf._TIER_CANDIDATE_UNITS


def test_candidate_cap_preserves_protected_top12_selection(monkeypatch):
    """Cap-before-MMR must not change WHICH units the protected path picks.

    Reproduces the pre-fix composition on the same ``ordered`` list the new
    composition consumed and asserts the protected path's top-12 (the
    ``_persistent_protected`` top_k window) is identical before/after.
    """
    captured: list[list] = []
    original_promote = icf._tier_promote

    def spy(ordered):
        captured.append(ordered)
        return original_promote(ordered)

    monkeypatch.setattr(icf, "_tier_promote", spy)

    messages = _mixed_corpus(300, 40)
    units = icf._message_units(messages)
    ranked = icf._rank_units(units, [], units)
    assert captured

    # The pre-fix call-site composition, verbatim.
    pre_fix = icf._lock_first(
        icf._mmr_order(icf._tier_promote(captured[0]))
    )[: icf._TIER_CANDIDATE_UNITS]

    assert [unit.text for unit, _ in ranked[:12]] == [
        unit.text for unit, _ in pre_fix[:12]
    ]


def test_full_build_460k_tokens_stays_under_five_seconds():
    """Evan's bar: deterministic selection on 680 msgs / ~460K tokens < 5s."""
    messages = _repro_corpus()

    start = time.monotonic()
    result = build_internal_fallback(
        messages,
        protect_head_count=4,
        protect_last_n=21,
        target_tokens=40000,
    )
    elapsed = time.monotonic() - start

    assert result.summary
    assert elapsed < 5.0, (
        f"build_internal_fallback took {elapsed:.2f}s on the 680-msg "
        f"/ ~460K-token fixture (bar: 5s)"
    )