"""End-to-end: the artifact survives the REAL compressor under a tight budget.

The existing artifact-lock tests exercise ``_lock_first`` / ``_rank_units``
directly.  That is precisely the shape of test that let the ``end()``
false-deletion bug survive: it asserted the helper's contract while the
production entry point could still be wrong.  These tests drive
``build_internal_fallback`` — the function ``context_compressor.py:6525``
actually calls — so the wiring, the budget packer, and the lock are all in
the path under test.

The load-bearing one is ``test_lock_off_drops_the_artifact``: it runs the
same payload with the lock monkeypatched off and asserts the artifact is
gone.  If it passes while the lock-on test also passes, the fixture is
vacuous — see the two traps documented below, both of which a first draft of
this file fell into.

TWO FIXTURE TRAPS (both hit on 2026-09-19, both silent):

1. **The artifact must not sit in the protected tail.**  ``protect_last_n``
   emits the last N messages verbatim, so an artifact placed there survives
   with or without the lock and the mutation test passes for the wrong
   reason.  Same for the protected head (``protect_head_count``).

2. **The prose must survive canonical dedupe.**  ``_content_tokens`` drops
   digits and function words, so numbered variants of ONE sentence collapse
   into a single unit.  A fixture built that way ranks ~3 units instead of
   dozens, the budget never binds, and every unit is selected regardless of
   the lock.  Distinct *topics* are required, not distinct numbers.
"""

from __future__ import annotations

from agent import internal_compression_fallback as icf
from agent.internal_compression_fallback import build_internal_fallback

_ARTIFACT = "edited /home/ekl/.hermes/hermes-agent/agent/system_prompt.py"

# Distinct after _content_tokens(): no digits, no shared function words.
_TOPICS = [
    "centrality weighting over the similarity graph",
    "relevance scoring against the newest work",
    "recency decay applied to the ranked middle",
    "protected terms removed from the heap scoring pass",
    "heap boundary at the active plan step change",
    "verbatim tail preservation above the emitted block",
    "budget allocation split across source regions",
    "repeat gate coverage against the verbatim tail",
    "lexrank stationary distribution over the cosine graph",
    "tfidf weighting on the canonical token form",
]


def _payload() -> list[dict]:
    """Prose-heavy middle, artifact in the MIDDLE, short protected tail."""
    msgs: list[dict] = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "opening request about the compression work"},
    ]
    # The artifact sits in the middle so the lock is what keeps it.
    msgs.append({"role": "assistant", "content": _ARTIFACT})
    for index in range(30):
        topic = _TOPICS[index % len(_TOPICS)]
        msgs.append({
            "role": "assistant" if index % 2 else "user",
            "content": (
                f"The compression selector ranked the middle units using "
                f"{topic} so the emitted block carries {topic} forward "
                f"through the ranked middle of the window."
            ),
        })
    for index in range(3):
        msgs.append({"role": "assistant", "content": f"recent tail {index}"})
    msgs.append({"role": "user", "content": "carry on with the work please"})
    return msgs


_BINDING_BUDGET = 400


def _run(target_tokens: int):
    return build_internal_fallback(
        _payload(),
        protect_head_count=1,
        protect_last_n=3,
        target_tokens=target_tokens,
    )


def _fixture_is_sound() -> None:
    """Fail loudly if the fixture drifts into either trap."""
    msgs = _payload()
    units = icf._message_units(msgs)
    ranked = icf._rank_units(units, [], [])
    assert len(ranked) > 5, (
        f"only {len(ranked)} units ranked — the prose collapsed under canonical "
        f"dedupe, so the budget cannot bind and the lock is untested"
    )
    res = build_internal_fallback(
        msgs, protect_head_count=1, protect_last_n=3,
        target_tokens=_BINDING_BUDGET,
    )
    art_idx = [i for i, m in enumerate(msgs) if _ARTIFACT in str(m.get("content"))]
    assert art_idx and all(i < res.tail_start for i in art_idx), (
        f"the artifact (index {art_idx}) sits in the protected tail "
        f"(tail_start={res.tail_start}) — the lock never gets a say"
    )


def test_fixture_is_not_vacuous() -> None:
    _fixture_is_sound()


def test_artifact_reaches_the_real_payload() -> None:
    """The path the compressor calls must carry the artifact through."""
    res = _run(target_tokens=_BINDING_BUDGET)
    assert _ARTIFACT in res.summary, (
        "the artifact did not survive build_internal_fallback — the lock is "
        "either unwired or priced out of the budget"
    )


def test_lock_off_drops_the_artifact(monkeypatch) -> None:
    """THE MUTATION PROOF: with the lock disabled, the artifact is lost.

    If this fails while ``test_artifact_reaches_the_real_payload`` passes, the
    lock is load-bearing.  If BOTH pass, this budget does not bind and the
    assertion above proves nothing.
    """
    monkeypatch.setattr(icf, "_is_inviolable", lambda unit: False)
    res = _run(target_tokens=_BINDING_BUDGET)
    assert _ARTIFACT not in res.summary, (
        "the artifact survived with the lock disabled — this budget does not "
        "bind, so the lock-on test above is vacuous"
    )


def test_dropout_is_reported_when_a_locked_artifact_is_lost() -> None:
    """_inviolable_missing must fire on the real emitted text, not a mock."""
    locked = icf._lock_first(
        icf._rank_units(icf._message_units(_payload()), [], [])
    )
    locked_units = [u for u, _s in locked if icf._is_inviolable(u)]
    assert locked_units, "fixture produced no locked unit — test is vacuous"

    res = _run(target_tokens=_BINDING_BUDGET)
    missing = icf._inviolable_missing(res.summary, locked_units)
    assert missing == [], f"locked artifacts missing from the payload: {missing}"
