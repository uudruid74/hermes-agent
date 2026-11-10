"""compression.max_attempts — config-driven compression retry cap.

The conversation loop's compression retry cap was hardcoded to 3, stranding
sessions that legitimately need more rounds — e.g. a restart history reload
whose incompressible tool schemas keep the request estimate above the
threshold while the messages themselves compress fine (the #62605 failure
class).  The cap is now parsed from ``compression.max_attempts`` in
``agent_init`` and read by the loop via
``getattr(agent, "max_compression_attempts", 3)``.

These tests pin the parse/validate/attach seam: default preserved, custom
value honored, floor and ceiling enforced, garbage tolerated.
"""

from __future__ import annotations

import contextlib
import io
from pathlib import Path

from hermes_state import SessionDB
from run_agent import AIAgent


_UNSET = object()


def _config(max_attempts=None, internal_only=_UNSET) -> dict:
    compression = {
        "enabled": True,
        "threshold": 0.50,
        "target_ratio": 0.20,
        "protect_first_n": 3,
        "protect_last_n": 20,
    }
    if max_attempts is not None:
        compression["max_attempts"] = max_attempts
    if internal_only is not _UNSET:
        compression["internal_only"] = internal_only
    return {
        "compression": compression,
        "prompt_caching": {"cache_ttl": "5m"},
        "sessions": {},
        "bedrock": {},
    }


def _make_agent(monkeypatch, tmp_path: Path, *, max_attempts=None, internal_only=_UNSET):
    from hermes_cli import config as config_mod

    monkeypatch.setattr(
        config_mod,
        "load_config",
        lambda: _config(max_attempts=max_attempts, internal_only=internal_only),
    )
    monkeypatch.setattr(
        config_mod,
        "load_config_readonly",
        lambda: _config(max_attempts=max_attempts, internal_only=internal_only),
    )
    db = SessionDB(db_path=tmp_path / "state.db")
    with contextlib.redirect_stdout(io.StringIO()):
        agent = AIAgent(
            base_url="https://chatgpt.com/backend-api/codex",
            api_key="test-key",
            provider="openai-codex",
            model="gpt-5.5",
            enabled_toolsets=[],
            disabled_toolsets=[],
            quiet_mode=True,
            skip_memory=True,
            session_db=db,
            session_id="max-attempts-test",
        )
    return agent


class TestCompressionMaxAttemptsConfig:
    def test_default_is_three_when_unset(self, monkeypatch, tmp_path):
        agent = _make_agent(monkeypatch, tmp_path)
        assert agent.max_compression_attempts == 3

    def test_custom_value_is_honored(self, monkeypatch, tmp_path):
        agent = _make_agent(monkeypatch, tmp_path, max_attempts=6)
        assert agent.max_compression_attempts == 6

    def test_zero_turns_off_the_limit(self, monkeypatch, tmp_path):
        """Evan 2026-11-10: ``compression.max_attempts: 0`` means UNLIMITED —
        "a value of 0 turns off the limit. The if < 1 you found should be a
        bypass not an error." The resolved value is +inf so every
        ``attempts < max`` gate stays open and every ``attempts > max``
        terminal stays closed with no per-site branching."""
        agent = _make_agent(monkeypatch, tmp_path, max_attempts=0)
        assert agent.max_compression_attempts == float("inf")

    def test_negative_also_bypasses_the_limit(self, monkeypatch, tmp_path):
        """The whole < 1 half-line is the bypass — not just literal 0."""
        agent = _make_agent(monkeypatch, tmp_path, max_attempts=-3)
        assert agent.max_compression_attempts == float("inf")

    def test_ceiling_still_caps_finite_values(self, monkeypatch, tmp_path):
        """The 10-cap must not fight the bypass: finite values still cap."""
        agent = _make_agent(monkeypatch, tmp_path, max_attempts=25)
        assert agent.max_compression_attempts == 10

    def test_zero_float_and_numeric_string_bypass_too(self, monkeypatch, tmp_path):
        agent = _make_agent(monkeypatch, tmp_path, max_attempts=0.0)
        assert agent.max_compression_attempts == float("inf")
        agent = _make_agent(monkeypatch, tmp_path, max_attempts="0")
        assert agent.max_compression_attempts == float("inf")


class TestInternalOnlyForcesSingleAttempt:
    """Evan 2026-11-10: when compression.internal_only is true,
    compression.max_attempts is forced to 1, ignoring config/default —
    retrying a deterministic compressor is pointless (same input, same
    output); the retry budget only makes sense for LLM summarizers."""

    def test_forces_one_when_max_attempts_unset(self, monkeypatch, tmp_path):
        agent = _make_agent(monkeypatch, tmp_path, internal_only=True)
        assert agent.max_compression_attempts == 1

    def test_forces_one_even_when_zero_unlimited(self, monkeypatch, tmp_path):
        agent = _make_agent(
            monkeypatch, tmp_path, max_attempts=0, internal_only=True
        )
        assert agent.max_compression_attempts == 1

    def test_forces_one_overriding_configured_five(self, monkeypatch, tmp_path):
        agent = _make_agent(
            monkeypatch, tmp_path, max_attempts=5, internal_only=True
        )
        assert agent.max_compression_attempts == 1

    def test_false_keeps_existing_rules(self, monkeypatch, tmp_path):
        agent = _make_agent(
            monkeypatch, tmp_path, max_attempts=0, internal_only=False
        )
        assert agent.max_compression_attempts == float("inf")
        agent = _make_agent(monkeypatch, tmp_path, internal_only=False)
        assert agent.max_compression_attempts == 3

    def test_unset_keeps_existing_rules(self, monkeypatch, tmp_path):
        agent = _make_agent(monkeypatch, tmp_path, max_attempts=5)
        assert agent.max_compression_attempts == 5

    def test_config_docs_shout_the_override(self):
        import inspect

        import hermes_cli.config_defaults as cfg

        source = " ".join(inspect.getsource(cfg).replace("#", " ").split())
        idx = source.find('"internal_only"')
        assert idx != -1
        window = source[idx : idx + 500]
        assert "max_attempts" in window and "1" in window







    def test_loop_pickup_degrades_to_default_when_attribute_missing(
        self, monkeypatch, tmp_path
    ):
        # The loop reads getattr(agent, "max_compression_attempts", 3): a
        # configured agent exposes its value, and an object without the
        # attribute (older pickle / minimal stub) degrades to the prior
        # hardcoded behavior.
        agent = _make_agent(monkeypatch, tmp_path, max_attempts=7)
        assert getattr(agent, "max_compression_attempts", 3) == 7
        assert getattr(object(), "max_compression_attempts", 3) == 3
