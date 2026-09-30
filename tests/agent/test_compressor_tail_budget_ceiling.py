"""Regression tests for task t_06894b19 — the tail cut must honour
``tail_token_budget`` for ordinary small-message tails instead of saturating
at the former 1.5x soft ceiling, while still keeping a single oversized
boundary message intact (the #40803 no-progress guard remains in force).
"""

from unittest.mock import patch

import pytest

from agent.context_compressor import ContextCompressor, _estimate_msg_budget_tokens


@pytest.fixture()
def compressor():
    with patch("agent.context_compressor.get_model_context_length", return_value=200_000):
        return ContextCompressor(
            model="test/model",
            threshold_percent=0.50,
            protect_first_n=1,
            protect_last_n=8,
            quiet_mode=True,
        )


class TestTailCutHonorsBudget:
    def test_many_small_messages_stop_at_budget_across_transcript_sizes(self, compressor):
        """Tail tokens stay under the configured budget and stop growing
        transcript-invariantly near it; they must never sit at 1.4x budget."""
        budget = 9_369
        results = []
        for count in (240, 300, 420, 900):
            messages = [{"role": "system", "content": "sys"}]
            messages.extend(
                {
                    "role": "user" if i % 2 == 0 else "assistant",
                    "content": f"{i:04d} " + ("x" * 196),
                }
                for i in range(count)
            )
            cut = compressor._find_tail_cut_by_tokens(
                messages, head_end=1, token_budget=budget
            )
            tail_tokens = sum(
                _estimate_msg_budget_tokens(m) for m in messages[cut:]
            )
            results.append((count, len(messages) - cut, tail_tokens))
            assert tail_tokens <= budget, (count, tail_tokens)

        # Saturation: once the transcript exceeds the budget, the selected
        # tail stops growing with transcript size (and stays under budget).
        assert len({(n, t) for _, n, t in results}) == 1, results

    def test_oversized_single_message_is_kept_intact(self, compressor):
        """A message larger than the budget is retained whole, never split
        (the case the old 1.5x ceiling existed to protect)."""
        compressor.protect_last_n = 3
        budget = 100
        oversized = {"role": "assistant", "content": "tool dump " + ("x" * 800)}
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "older reply"},
            {"role": "user", "content": "recent ask"},
            oversized,
            {"role": "user", "content": "latest ask"},
        ]
        cut = compressor._find_tail_cut_by_tokens(messages, head_end=1, token_budget=budget)
        tail = messages[cut:]
        assert oversized in tail
        assert cut <= len(messages) - 1  # compression still has something to claim