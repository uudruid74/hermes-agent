"""Regression guard for the too-many-images 400 recovery.

Upstream per-request image-count caps (e.g. the cheaperinference route's
"Too many images in request: 31 > 30" 400) previously fell through to a
generic ``format_error`` classification → non-retryable abort of the turn.

The fix:

  1. ``agent/error_classifier.py`` recognizes the wording in
     ``_classify_400`` as ``FailoverReason.too_many_images`` with
     ``retryable=True`` + ``should_compress=True``.
  2. ``agent/conversation_loop.py`` gains a recovery branch (mirroring the
     existing ``long_context_tier`` branch) that compacts the window via the
     existing ``agent._compress_context`` machinery and retries — instead of
     blind retries or the abort path.

These tests mirror the ``is_client_error`` predicate shape from
``agent/conversation_loop.py`` (same lock-step convention as
``test_31273_402_not_retried.py``) so the too-many-images reason can never
silently fall into the terminal abort path.
"""
from __future__ import annotations

from agent.error_classifier import FailoverReason, classify_api_error


class _FakeApiError(Exception):
    """Stand-in for an openai.BadRequestError with status_code + body."""

    def __init__(self, status_code: int, message: str, body: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body or {"error": {"message": message}}
        self.response = None


def _mirror_is_client_error(
    *,
    classified_retryable: bool,
    classified_reason,
    classified_should_compress: bool = False,
    is_local_validation_error: bool = False,
    is_context_length_error: bool = False,
) -> bool:
    """Exact shape of conversation_loop.py's is_client_error check.

    Kept in lock-step with the source.  If you change one, change
    both — or, better, refactor the predicate into a shared helper
    and have both sites import it.
    """
    return (
        is_local_validation_error
        or (
            not classified_retryable
            and not classified_should_compress
            and classified_reason not in {
                FailoverReason.rate_limit,
                FailoverReason.overloaded,
                FailoverReason.context_overflow,
                FailoverReason.payload_too_large,
                FailoverReason.long_context_tier,
                FailoverReason.thinking_signature,
            }
        )
    ) and not is_context_length_error


class TestTooManyImagesIsNotClientError:
    """The recovery path must never be pre-empted by the terminal abort."""

    def test_too_many_images_not_client_error(self):
        result = _mirror_is_client_error(
            classified_retryable=True,
            classified_reason=FailoverReason.too_many_images,
            classified_should_compress=True,
        )
        assert result is False

    def test_classified_retryable_and_compressible(self):
        err = _FakeApiError(400, "Too many images in request: 31 > 30")
        result = classify_api_error(err, provider="openrouter")
        assert result.reason == FailoverReason.too_many_images
        assert result.retryable is True
        assert result.should_compress is True

    def test_compression_branch_signal_present_in_loop_source(self):
        """The recovery branch must exist in conversation_loop.py.

        Guard against the branch being dropped in a future refactor — the
        enum + classifier alone would silently strand the error in the
        generic retry path without compaction.
        """
        import pathlib

        src = pathlib.Path("agent/conversation_loop.py").read_text()
        assert "FailoverReason.too_many_images" in src
        assert "restart_with_compressed_messages" in src
        # Must be the compaction branch, not the strip branch.
        assert "_compress_context(" in src
