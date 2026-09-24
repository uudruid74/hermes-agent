"""Regression test for BUG E: compaction re-stamps every compacted turn with
the compaction clock instead of the time it was spoken.

The summary message is assembled fresh in ``compress()`` — unlike the head and
tail copies, which keep their own ``timestamp`` via ``_fresh_compaction_message_copy``
(a shallow ``.copy()``), the summary dict had no timestamp at all, so
``_insert_message_rows`` stamped it with ``time.time()`` (the compaction clock).
Every turn the summary replaces then carries the compaction's timestamp in the
DB instead of its real one.

The fix carries the newest summarized turn's timestamp onto the summary
message, so the compacted transcript keeps the time things were spoken.
"""
from unittest.mock import MagicMock, patch

import pytest

from agent.context_compressor import (
    COMPRESSED_SUMMARY_METADATA_KEY,
    ContextCompressor,
)


def _make_compressor():
    with patch(
        "agent.context_compressor.get_model_context_length", return_value=8000
    ):
        return ContextCompressor(
            model="test-model", quiet_mode=True, config_context_length=8000
        )


def _make_messages(n_turns=30):
    """Alternating turns, each carrying a distinct, monotonic timestamp."""
    msgs = [{"role": "system", "content": "sys", "timestamp": 1_000.0}]
    base = 10_000.0
    for i in range(n_turns):
        msgs.append(
            {"role": "user", "content": f"question {i} " + "x" * 400, "timestamp": base + i}
        )
        msgs.append(
            {
                "role": "assistant",
                "content": f"answer {i} " + "y" * 400,
                "timestamp": base + i + 0.5,
            }
        )
    return msgs


def _compress(cc, msgs):
    resp = MagicMock()
    resp.choices[0].message.content = "## Active Task\nstuff"
    with patch("agent.context_compressor.call_llm", return_value=resp):
        return cc.compress(msgs, current_tokens=100_000, force=True)


def _summary_message(out):
    flagged = [
        m for m in out
        if isinstance(m, dict) and m.get(COMPRESSED_SUMMARY_METADATA_KEY)
    ]
    assert len(flagged) == 1
    return flagged[0]


class TestCompactionCarriesTimestamp:
    def test_summary_carries_newest_summarized_turn_timestamp(self):
        """The standalone summary keeps the timestamp of the newest turn it
        replaced, not the compaction clock."""
        cc = _make_compressor()
        out = _compress(cc, _make_messages())
        summary = _summary_message(out)
        assert summary.get("timestamp") is not None
        # The summary replaces the middle turns; its timestamp must be one of
        # the input turn timestamps (real spoken time), never the compaction
        # clock (which would be ~time.time(), far larger than our fixtures).
        assert summary["timestamp"] <= 10_000.0 + 30
        assert summary["timestamp"] >= 10_000.0

    def test_summary_timestamp_is_not_compaction_clock(self):
        """Guard against the exact defect: the summary must not carry a
        freshly-generated time.time() value."""
        cc = _make_compressor()
        out = _compress(cc, _make_messages())
        summary = _summary_message(out)
        import time

        # The compaction clock is ~time.time() (billions); our carried
        # timestamp is ~10_000. If the bug returns, this assertion fails.
        assert summary["timestamp"] < time.time() - 1_000_000

    def test_head_and_tail_keep_their_own_timestamps(self):
        """Head/tail copies already preserved their timestamps via the shallow
        copy; assert the invariant still holds after the summary change."""
        cc = _make_compressor()
        msgs = _make_messages()
        out = _compress(cc, msgs)
        # System prompt (head) keeps its timestamp.
        assert out[0].get("timestamp") == 1_000.0
        # At least the newest tail message keeps a real (small) timestamp.
        tail_timestamps = [
            m.get("timestamp") for m in out if m.get("timestamp") is not None
        ]
        assert all(ts <= 10_000.0 + 30 for ts in tail_timestamps)

    def test_summary_timestamp_none_when_turns_undated(self):
        """If the summarized turns carry no timestamp, the summary carries
        none either — it is never fabricated."""
        cc = _make_compressor()
        msgs = _make_messages()
        for m in msgs:
            m.pop("timestamp", None)
        out = _compress(cc, msgs)
        summary = _summary_message(out)
        assert summary.get("timestamp") is None
