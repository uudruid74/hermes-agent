"""Regression: detect compression progress by tokens, not just rows.

Issue #39548: preflight compression in the turn prologue was checking
``len(messages) >= _orig_len`` to decide "Cannot compress further". This
false-positives when a pass summarises message contents — reducing the
estimated request token count without removing any rows — and surfaces a
spurious ``Context length exceeded`` failure followed by an auto-reset of
an otherwise healthy session.

These tests pin the contract of ``_compression_made_progress``: a
row-count reduction OR a *material* (>5%) token-count reduction counts as
progress.
"""

from __future__ import annotations

from agent.turn_context import (
    _compression_made_progress,
    _compression_warrants_another_preflight_pass,
)


class TestCompressionMadeProgress:
    def test_rows_reduced_counts_as_progress(self):
        """Removing message rows is the obvious progress signal."""
        assert _compression_made_progress(
            orig_len=10, new_len=5, orig_tokens=1000, new_tokens=1000
        ) is True



    def test_neither_moved_means_no_progress(self):
        """The genuine "stuck" case — same rows, same tokens, give up."""
        assert _compression_made_progress(
            orig_len=10, new_len=10, orig_tokens=1000, new_tokens=1000
        ) is False




    def test_sub_5pct_token_drop_is_not_progress(self):
        """A token reduction below the 5% material floor does NOT count as
        progress — matching the overflow-handler retry path (#39550) so a
        marginal wobble can't keep the multi-pass loop spinning."""
        # 1000 -> 970 is a 3% drop, below the 5% floor.
        assert _compression_made_progress(
            orig_len=10, new_len=10, orig_tokens=1000, new_tokens=970
        ) is False
        # 1000 -> 940 is a 6% drop, above the floor.
        assert _compression_made_progress(
            orig_len=10, new_len=10, orig_tokens=1000, new_tokens=940
        ) is True



class TestCompressionWarrantsAnotherPreflightPass:
    def test_material_reduction_above_threshold_allows_another_pass(self):
        assert _compression_warrants_another_preflight_pass(
            orig_tokens=400_000,
            new_tokens=350_000,
            threshold_tokens=272_000,
        ) is True

    def test_marginal_reduction_above_threshold_stops(self):
        assert _compression_warrants_another_preflight_pass(
            orig_tokens=350_000,
            new_tokens=345_000,
            threshold_tokens=272_000,
        ) is False



class TestRetryAfterCompression:
    """A request that FITS must retry, even when compaction removed nothing.

    Issue: ornith 2026-09-15. A 35,031-token request on a 78,080 window —
    comfortably under the 64,000 compression threshold — aborted the turn with
    "Context length exceeded: 9,177 tokens. Cannot compress further."

    Two defects in one branch:

    1. It decided on the MESSAGE-ONLY estimate (9,177) while the request that
       actually failed was 35,031 (messages + 8,688 system prompt + 18,893 tool
       schemas). The reported number could not explain the failure.
    2. It had no arm for "the request now fits". When a session's compressible
       region is already minimal, every pass returns the same transcript, so
       the row-count and >5%-token arms can never fire — and a healthy session
       was declared unsalvageable.
    """

    def _f(self, **kw):
        from agent.turn_context import _retry_after_compression

        base = dict(
            request_tokens=35031, context_length=78080,
            orig_len=96, new_len=96,
            orig_tokens=35031, new_tokens=35031,
        )
        base.update(kw)
        return _retry_after_compression(**base)

    def test_request_under_window_retries_even_with_no_progress(self):
        """THE regression: 35K request on a 78K window, compaction did nothing."""
        assert self._f() is True

    def test_genuine_overflow_still_fails(self):
        """Guard: a request genuinely larger than the window must NOT retry."""
        assert self._f(request_tokens=90000, orig_tokens=90000, new_tokens=90000) is False

    def test_zero_or_negative_request_tokens_is_not_a_pass(self):
        """A degenerate estimate must not be read as 'it fits'."""
        assert self._f(request_tokens=0, orig_tokens=90000, new_tokens=90000) is False

    def test_rows_dropped_still_counts(self):
        assert self._f(request_tokens=90000, orig_tokens=90000, new_tokens=90000,
                       new_len=40) is True

    def test_material_token_cut_still_counts(self):
        assert self._f(request_tokens=90000, orig_tokens=90000, new_tokens=70000) is True

    def test_provider_lowered_context_limit_counts(self):
        assert self._f(request_tokens=90000, orig_tokens=90000, new_tokens=90000,
                       context_limit_shrank=True) is True

    def test_sub_5pct_token_wobble_alone_does_not_count(self):
        """Existing contract preserved: a <5% wobble is not progress."""
        assert self._f(request_tokens=90000, orig_tokens=90000, new_tokens=88000) is False
