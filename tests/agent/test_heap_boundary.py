"""Heap boundary + re-ingested payload pruning (Evan, 2026-09-16).

Every test here is written BEFORE the behaviour it checks is trusted, and the
live-session numbers are read from the real transcript, not fixtures.  Evan's
rule: a unit test on mocks is not enough — verify against real data.
"""

from __future__ import annotations

import sqlite3
import sys

import pytest

sys.path.insert(0, ".")

from agent.internal_compression_fallback import (  # noqa: E402
    _PAYLOAD_MARKER,
    _plan_step_change_index,
    _prune_reingested_payload,
)

ORNYTH_DB = "/home/ekl/.hermes/profiles/ornith/state.db"


# ---------------------------------------------------------------------------
# The step-change boundary
# ---------------------------------------------------------------------------


def test_newest_marker_wins_not_the_first():
    """Evan: rank from the LAST step change forward, not the first."""
    messages = [{"role": "user", "content": f"filler {i}"} for i in range(12)]
    messages[3]["content"] = "Complete Step 1: do the thing"
    messages[9]["content"] = "Complete Step 4: now this"
    assert _plan_step_change_index(messages, 0, 12) == 9


def test_all_three_marker_forms_are_recognised():
    for text in (
        "Complete Step 2: Inspect current code",
        ">>> STEP 3: Read the canonical file <<<",
        "Active Step 1 of 9: Delete the deprecated implementation",
    ):
        assert _plan_step_change_index([{"role": "tool", "content": text}], 0, 1) == 0, text


def test_no_marker_leaves_the_region_unbounded():
    """Absent a Plan, ranking must behave exactly as it did before."""
    plain = [{"role": "user", "content": "ordinary content"} for _ in range(5)]
    assert _plan_step_change_index(plain, 0, 5) == 0


def test_boundary_ignores_markers_before_start():
    messages = [{"role": "user", "content": "Complete Step 1: old"}] + [
        {"role": "user", "content": "filler"} for _ in range(4)
    ]
    assert _plan_step_change_index(messages, 1, 5) == 1


def test_none_and_missing_content_do_not_crash():
    messages = [
        {"content": None},
        {"role": "tool"},
        {"content": "Active Step 2 of 7: read the file"},
    ]
    assert _plan_step_change_index(messages, 0, 3) == 2


# ---------------------------------------------------------------------------
# Re-ingested payload pruning
# ---------------------------------------------------------------------------


def _payload(body: str = "old context") -> str:
    return f"{_PAYLOAD_MARKER}  ## Relevant Earlier Context\n{body}"


def test_reingested_payload_is_pruned_to_nothing():
    assert _prune_reingested_payload(_payload()) == ""


def test_message_units_prunes_every_payload_including_a_trailing_one():
    """No positional exemption: a payload is machinery wherever it sits.

    An earlier version exempted the newest message as "the payload under
    construction".  That payload is appended AFTER compression, so it is never
    inside the ranked region — and the exemption was slice-relative, wrongly
    sparing a payload whenever it landed last in the slice.  Verified on the
    live session: ``_message_units([row 30934])`` produced 133 rankable units.
    """
    from agent.internal_compression_fallback import _message_units

    payload = "[CONTEXT WINDOW COMPRESSED]  ## Relevant Earlier Context\n[USER]: old stuff"
    units = _message_units([{"role": "user", "content": payload}])
    assert units == []


def test_the_payload_being_assembled_survives_when_flagged():
    """The escape hatch exists for callers that know the text is live."""
    assert _prune_reingested_payload(_payload(), is_newest=True) == _payload()


def test_ordinary_content_is_untouched():
    """A session_search result that merely quotes a payload must still rank."""
    text = '{"success": true, "results": [{"snippet": "[CONTEXT WINDOW COMPRESSED] ..."}]}'
    assert _prune_reingested_payload(text) == text


def test_strip_boilerplate_still_owns_the_wrapper():
    """Pruning is separate from wrapper stripping; both must still work."""
    from agent.internal_compression_fallback import _strip_boilerplate

    cleaned = _strip_boilerplate(_payload("real content here"))
    assert _PAYLOAD_MARKER not in cleaned
    assert "real content here" in cleaned


# ---------------------------------------------------------------------------
# Live session — the numbers these rules were derived from
# ---------------------------------------------------------------------------


def _load_active_messages() -> list[dict]:
    conn = sqlite3.connect(ORNYTH_DB)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE active = 1 ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return [{"role": r["role"], "content": r["content"]} for r in rows]


@pytest.mark.skipif(
    not __import__("os").path.exists(ORNYTH_DB), reason="live Ornith DB not present"
)
def test_live_session_boundary_excludes_completed_steps():
    messages = _load_active_messages()
    if len(messages) < 10:
        pytest.skip("live transcript too short")
    boundary = _plan_step_change_index(messages, 1, len(messages))
    # The transcript carries Plan markers; the boundary must land on one of
    # them rather than at the start.
    assert boundary > 1, "expected a step-change marker in the live transcript"


@pytest.mark.skipif(
    not __import__("os").path.exists(ORNYTH_DB), reason="live Ornith DB not present"
)
def test_live_payload_rows_are_all_pruned():
    conn = sqlite3.connect(ORNYTH_DB)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, content FROM messages WHERE content LIKE ? ORDER BY id",
            (f"%{_PAYLOAD_MARKER}%",),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        pytest.skip("no payload rows in the live transcript")

    # Rows whose marker is not at position 0 are QUOTED payloads (assistant
    # "prior context" blocks and session_search snippets) — they are content
    # and must survive.  Genuine payloads carry the marker at position 0.
    genuine = [r for r in rows if (r["content"] or "").lstrip().startswith(_PAYLOAD_MARKER)]
    assert genuine, "expected at least one genuine payload row"
    for row in genuine:
        assert _prune_reingested_payload(row["content"]) == "", (
            f"payload row {row['id']} was not pruned"
        )
