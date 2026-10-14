"""B3 payload true-event-time — merge-into-tail payload rows (t_de77589d).

Council 2026-10-14 ground truth: 8 Run-B payload rows shared ONE frozen
timestamp (1791928751.2707717) despite spanning ~2h. The merge-into-tail
carrier kept whatever stale stamp it had inherited down the fold chain, or
fell to the compaction clock. Fix: the merged row IS a payload row and
carries the snapshot's true event time — the newest summarized turn's clock
(the same rule the BUG E carry applies to standalone summaries), so
inherited carrier stamps can never chain forward.
"""

from unittest.mock import MagicMock, patch

import pytest

from agent.context_compressor import (
    ContextCompressor,
    _MERGED_PRIOR_CONTEXT_HEADER,
)


@pytest.fixture()
def compressor():
    with patch(
        "agent.context_compressor.get_model_context_length",
        return_value=100000,
    ):
        c = ContextCompressor(
            model="test/model",
            threshold_percent=0.85,
            protect_first_n=2,
            protect_last_n=2,
            quiet_mode=True,
        )
        _ = c.context_length
    return c


class TestMergedPayloadTimestamp:
    def test_merged_payload_row_carries_snapshot_time(self, compressor):
        """The merge-into-tail path stamps its payload row with the
        snapshot's true event time (300.0 = newest summarized turn), never
        the carrier's stale inherited stamp (999.0) and never a compaction
        clock. One code rule covers every carrier shape (fold rows, live
        rows, unwrapped fold priors) — the frozen clock chain dies at the
        merge."""
        c = compressor
        c._generate_summary = MagicMock(return_value="TEST SUMMARY BODY")
        c.tail_token_budget = 5  # force a real middle on this tiny window
        # Head ends assistant (the dynamic head covers 3 rows here), tail
        # starts user — the alternation collision that routes the summary
        # through the merge-into-tail path instead of a standalone row.
        messages = [
            {"role": "system", "content": "sys prompt", "timestamp": 1.0},
            {"role": "user", "content": "head user prose " + "h" * 200,
             "timestamp": 40.0},
            {"role": "assistant", "content": "head assistant prose " + "h" * 200,
             "timestamp": 50.0},
            {"role": "user", "content": "middle question 0 " + "m" * 400,
             "timestamp": 200.0},
            {"role": "assistant", "content": "middle answer 0 " + "m" * 400,
             "timestamp": 210.0},
            {"role": "user", "content": "middle question 1 " + "m" * 400,
             "timestamp": 250.0},
            {"role": "assistant", "content": "middle answer 1 " + "m" * 400,
             "timestamp": 300.0},  # newest summarized turn = snapshot time
            {
                "role": "user",
                "content": "[CONTEXT WINDOW COMPRESSED] " + "f" * 200,
                "timestamp": 999.0,  # stale inherited clock (frozen chain)
            },
            {"role": "user", "content": "live question " + "l" * 100,
             "timestamp": 400.0},
        ]
        out = c.compress(messages, current_tokens=90_000, force=True)
        merged = [
            m
            for m in out
            if isinstance(m.get("content"), str)
            and _MERGED_PRIOR_CONTEXT_HEADER in m["content"]
        ]
        assert merged, "merge-into-tail path must have fired"
        assert merged[0].get("timestamp") == pytest.approx(300.0), (
            "the merged payload row must carry the snapshot's true event "
            "time (300.0) instead of the carrier's stale inherited clock, "
            f"got {merged[0].get('timestamp')}"
        )

    def test_standalone_summary_keeps_bug_e_carry(self, compressor):
        """The standalone summary row (BUG E path) carries the same
        snapshot time when no merge collision exists."""
        c = compressor
        c._generate_summary = MagicMock(return_value="TEST SUMMARY BODY")
        c.tail_token_budget = 5
        messages = [
            {"role": "system", "content": "sys prompt", "timestamp": 1.0},
            {"role": "user", "content": "head user prose " + "h" * 200,
             "timestamp": 40.0},
            {"role": "assistant", "content": "head assistant prose " + "h" * 200,
             "timestamp": 50.0},
            {"role": "user", "content": "middle question 0 " + "m" * 400,
             "timestamp": 200.0},
            {"role": "assistant", "content": "middle answer 0 " + "m" * 400,
             "timestamp": 210.0},
            {"role": "user", "content": "middle question 1 " + "m" * 400,
             "timestamp": 250.0},
            {"role": "assistant", "content": "middle answer 1 " + "m" * 400,
             "timestamp": 300.0},
            {"role": "user", "content": "live question " + "l" * 100,
             "timestamp": 400.0},
            {"role": "assistant", "content": "tail answer " + "t" * 100,
             "timestamp": 410.0},
        ]
        out = c.compress(messages, current_tokens=90_000, force=True)
        from agent.context_compressor import COMPRESSED_SUMMARY_METADATA_KEY

        summaries = [
            m for m in out if m.get(COMPRESSED_SUMMARY_METADATA_KEY)
        ]
        assert summaries, "a summary row must have been emitted"
        clocks = sorted(
            round(m.get("timestamp"), 3) for m in summaries
        )
        assert all(200.0 <= t <= 300.0 for t in clocks), (
            "summary rows must carry a real summarized-turn clock "
            f"(200..300), got {clocks}"
        )