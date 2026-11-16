"""Tests for boundary-compaction triggers (Evan, 2026-11-16).

Plan step-changes, plan-continue, and session subject changes are hard
semantic cuts: compaction fires FRESH there instead of waiting for the size
threshold.  These tests cover the trigger gate on ContextCompressor:

- step-change marker trigger fires below threshold;
- the no-double-fire guard (re-shown marker for the same step does not
  re-fire; only the step NUMBER advancing does);
- the subject-change trigger fires once per flip, never on confirmatory
  re-sets;
- the explicit plan-continue bump fires even when marker/subject are
  unchanged;
- pending triggers survive until a compaction actually completes (commit),
  so lock-skipped attempts re-fire;
- planless sessions are untouched (threshold-only).
"""

import pytest
from unittest.mock import patch

from agent.context_compressor import ContextCompressor


@pytest.fixture()
def compressor():
    """A ContextCompressor with mocked dependencies, threshold resolved."""
    with patch("agent.context_compressor.get_model_context_length", return_value=100000):
        c = ContextCompressor(
            model="test/model",
            threshold_percent=0.85,
            protect_first_n=2,
            protect_last_n=2,
            quiet_mode=True,
        )
        _ = c.context_length
        return c


def _msg(content, role="assistant"):
    return {"role": role, "content": content}


STEP3 = [_msg("Complete Step 3: Write the integration tests")]
STEP3_REMIND = [_msg("Active Step 3 of 9: Write the integration tests")]
STEP4 = [_msg("Complete Step 4: Harden the loader")]
NO_MARKERS = [_msg("plain prose, no plan markers here")]


class TestStepChangeTrigger:
    def test_marker_fires_below_threshold(self, compressor):
        # 50K << 85K threshold — threshold-only would never fire.
        ok, reason = compressor.should_compress_info(
            50_000, messages=STEP3
        )
        assert ok is True
        assert reason == "boundary_step_change"

    def test_commit_consumes_trigger_remind_reshow_does_not_refire(self, compressor):
        assert compressor.should_compress(50_000, messages=STEP3) is True
        compressor._commit_boundary_snapshot()
        # Remind re-shows the SAME step (3) — must NOT re-fire.
        assert compressor.should_compress(50_000, messages=STEP3_REMIND) is False
        assert compressor.should_compress(50_000, messages=STEP3) is False

    def test_step_number_advance_fires_again(self, compressor):
        assert compressor.should_compress(50_000, messages=STEP3) is True
        compressor._commit_boundary_snapshot()
        assert compressor.should_compress(50_000, messages=STEP4) is True
        compressor._commit_boundary_snapshot()
        assert compressor.should_compress(50_000, messages=STEP4) is False

    def test_marker_triggers_with_any_marker_type(self, compressor):
        # Distinct step numbers: each marker type must fire independently.
        for marker in (
            "Complete Step 2: next",
            ">>> STEP 3: next <<<",
            "Active Step 4 of 5: next",
        ):
            ok, reason = compressor.should_compress_info(50_000, messages=[_msg(marker)])
            assert ok is True
            assert reason == "boundary_step_change"
            compressor._commit_boundary_snapshot()

    def test_planless_session_untouched_below_threshold(self, compressor):
        assert compressor.should_compress(50_000, messages=NO_MARKERS) is False
        assert compressor.should_compress(50_000) is False

    def test_threshold_still_fires_without_messages(self, compressor):
        assert compressor.should_compress(90_000) is True

    def test_non_dict_message_rows_skipped(self, compressor):
        messy = [{"role": "user", "content": "Complete Step 4: x"}, "not-a-dict", None]
        ok, reason = compressor.should_compress_info(50_000, messages=messy)
        assert ok is True
        assert reason == "boundary_step_change"


class TestSubjectTrigger:
    def test_subject_flip_fires_once_confirmatory_re_set_does_not(self, compressor, monkeypatch):
        current = {"subject": "planning"}
        monkeypatch.setattr(compressor, "_session_subject", lambda: current["subject"])
        # First observation baselines the pre-existing subject — no fire.
        assert compressor.should_compress(50_000, messages=NO_MARKERS) is False
        # Subject flip -> fire.
        current["subject"] = "debugging"
        ok, reason = compressor.should_compress_info(50_000, messages=NO_MARKERS)
        assert ok is True
        assert reason == "boundary_subject_change"
        compressor._commit_boundary_snapshot()
        # Confirmatory re-set of the same subject -> no fire.
        current["subject"] = "debugging"
        assert compressor.should_compress(50_000, messages=NO_MARKERS) is False

    def test_subject_change_does_not_fire_when_unavailable(self, compressor, monkeypatch):
        monkeypatch.setattr(compressor, "_session_subject", lambda: "")
        assert compressor.should_compress(50_000, messages=NO_MARKERS) is False
        # Even after commit, still no subject -> no fire.
        compressor._commit_boundary_snapshot()
        assert compressor.should_compress(50_000, messages=NO_MARKERS) is False


class TestPlanContinueTrigger:
    def test_explicit_bump_fires_even_when_marker_subject_unchanged(self, compressor):
        # Baseline: step 3 committed.
        assert compressor.should_compress(50_000, messages=STEP3) is True
        compressor._commit_boundary_snapshot()
        compressor.request_boundary_compaction("plan_continue")
        # Same transcript, same subject — only the explicit bump can fire.
        ok, reason = compressor.should_compress_info(50_000, messages=STEP3)
        assert ok is True
        assert reason == "plan_continue"

    def test_bump_fires_without_messages_argument(self, compressor):
        compressor.request_boundary_compaction("plan_continue")
        assert compressor.should_compress(50_000) is True

    def test_pending_survives_until_commit(self, compressor):
        compressor.request_boundary_compaction("plan_continue")
        assert compressor.should_compress(50_000) is True
        # Lock-skip / no-progress: trigger is NOT consumed.
        assert compressor.should_compress(50_000) is True
        compressor._commit_boundary_snapshot()
        assert compressor.should_compress(50_000) is False

    def test_commit_without_pending_snapshot_clears_stale_reason(self, compressor):
        compressor.request_boundary_compaction("plan_continue")
        compressor._commit_boundary_snapshot()
        assert compressor.should_compress(50_000) is False


class TestBlockInteraction:
    def test_boundary_trigger_still_respects_automatic_block(self, compressor):
        # Arm the anti-thrash breaker (>= 2 fallback streak).
        compressor._fallback_compression_streak = 2
        ok, reason = compressor.should_compress_info(50_000, messages=STEP3)
        assert ok is False
        assert reason == "ineffective"
