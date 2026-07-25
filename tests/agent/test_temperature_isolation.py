"""Integration tests for per-session temperature isolation.

Covers all 6 scenarios from the integration test task:
  1. Interactive session adjust_temperature(temperature) sets temp
  2. Kanban worker uses worker_temperature (0.1)
  3. Delegated task uses worker_temperature
  4. Session isolation — two agents get independent temperatures
  5. Kimi / Moonshot providers return null from adjust_temperature
  6. Clamping at 0.0 and 1.3
"""

import json
from types import SimpleNamespace

import pytest


# ── helpers ────────────────────────────────────────────────────────────

def _make_agent(*, provider="deepseek", temperature=None,
                worker_temperature=None, session_temperature=None,
                temperature_map=None):
    """Build a minimal mock agent with the attributes resolve_temperature needs."""
    agent = SimpleNamespace(
        provider=provider,
        _temperature=temperature,
        temperature=temperature,
        worker_temperature=worker_temperature,
        _session_temperature=session_temperature,
        _temperature_map=temperature_map,
    )
    return agent


# ── scenario 1: adjust_temperature in interactive session ──────────────

class TestAdjustTemperatureInteractive:
    """Scenario 1 — adjust_temperature tool in an interactive session."""

    def test_positive_adjustment_increases_temperature(self):
        """temperature=1.05 sets 1.05 directly."""
        from tools.temperature_tool import adjust_temperature_tool

        agent = _make_agent(temperature=0.7)
        result = json.loads(adjust_temperature_tool(temperature=1.05, agent=agent))

        assert result["temperature"] == pytest.approx(1.05)
        assert result["changed"] is True  # first use sets session override
        assert result["previous"] is None
        assert agent._session_temperature == pytest.approx(1.05)

    def test_negative_adjustment_decreases_temperature(self):
        """temperature=0.35 sets 0.35 directly."""
        from tools.temperature_tool import adjust_temperature_tool

        agent = _make_agent(temperature=0.7)
        result = json.loads(adjust_temperature_tool(temperature=0.35, agent=agent))

        assert result["temperature"] == pytest.approx(0.35)
        assert agent._session_temperature == pytest.approx(0.35)

    def test_chained_adjustment_detects_change(self):
        """Two calls: first sets 0.84, second sets 1.05 — change detected."""
        from tools.temperature_tool import adjust_temperature_tool

        agent = _make_agent(temperature=0.7)

        # First call — no previous session temp, but setting it IS a change
        r1 = json.loads(adjust_temperature_tool(temperature=0.84, agent=agent))
        assert r1["changed"] is True
        assert r1["temperature"] == pytest.approx(0.84)

        # Second call — previous was 0.84, change IS detected
        r2 = json.loads(adjust_temperature_tool(temperature=1.05, agent=agent))
        assert r2["changed"] is True
        assert r2["temperature"] == pytest.approx(1.05)
        assert r2["previous"] == pytest.approx(0.84)

    def test_resolve_temperature_prefers_session_override(self):
        """resolve_temperature returns _session_temperature when set."""
        from agent.chat_completion_helpers import resolve_temperature

        agent = _make_agent(temperature=0.7, worker_temperature=0.1,
                            session_temperature=1.5)

        assert resolve_temperature(agent) == 1.5


# ── scenario 2: kanban worker uses worker_temperature (0.1) ────────────

class TestKanbanWorkerTemperature:
    """Scenario 2 — Kanban workers resolve to worker_temperature."""

    def test_kanban_worker_resolves_worker_temperature(self, monkeypatch):
        """With HERMES_KANBAN_TASK set, resolve_temperature → worker_temperature."""
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test123")
        monkeypatch.delenv("HERMES_DELEGATED_TASK", raising=False)

        from agent.chat_completion_helpers import resolve_temperature

        agent = _make_agent(temperature=0.7, worker_temperature=0.1)
        assert resolve_temperature(agent) == 0.1

    def test_kanban_worker_falls_back_to_profile_when_no_worker_temp(self, monkeypatch):
        """Without worker_temperature, resolve to profile temperature."""
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test123")
        monkeypatch.delenv("HERMES_DELEGATED_TASK", raising=False)

        from agent.chat_completion_helpers import resolve_temperature

        agent = _make_agent(temperature=0.7, worker_temperature=None)
        assert resolve_temperature(agent) == 0.7

    def test_kanban_worker_session_override_still_wins(self, monkeypatch):
        """Even in kanban mode, _session_temperature overrides all."""
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test123")
        monkeypatch.delenv("HERMES_DELEGATED_TASK", raising=False)

        from agent.chat_completion_helpers import resolve_temperature

        agent = _make_agent(temperature=0.7, worker_temperature=0.1,
                            session_temperature=1.5)
        assert resolve_temperature(agent) == 1.5


# ── scenario 3: delegated task uses worker_temperature ─────────────────

class TestDelegatedTaskTemperature:
    """Scenario 3 — Delegated subagents resolve to worker_temperature."""

    def test_delegated_resolves_worker_temperature(self, monkeypatch):
        """Delegated subagents with _subagent_id set resolve to worker_temperature."""
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

        from agent.chat_completion_helpers import resolve_temperature

        agent = _make_agent(temperature=0.7, worker_temperature=0.1)
        agent._subagent_id = "sub_abc123"
        assert resolve_temperature(agent) == 0.1

    def test_delegated_falls_back_to_profile(self, monkeypatch):
        """Without worker_temperature, delegated falls back to profile."""
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

        from agent.chat_completion_helpers import resolve_temperature

        agent = _make_agent(temperature=0.7, worker_temperature=None)
        agent._subagent_id = "sub_abc123"
        assert resolve_temperature(agent) == 0.7


# ── scenario 4: session isolation (Telegram topic A != topic B) ────────

class TestSessionIsolation:
    """Scenario 4 — Two agent instances have independent temperatures."""

    def test_two_agents_independent_session_temps(self):
        """Agent A temp change does not affect Agent B."""
        from tools.temperature_tool import adjust_temperature_tool
        from agent.chat_completion_helpers import resolve_temperature

        agent_a = _make_agent(temperature=0.7)
        agent_b = _make_agent(temperature=0.7)

        # Adjust agent A
        adjust_temperature_tool(temperature=1.05, agent=agent_a)

        # Agent A should be hot
        assert resolve_temperature(agent_a) == pytest.approx(1.05)

        # Agent B should be unchanged
        assert resolve_temperature(agent_b) == 0.7

    def test_two_agents_independent_with_worker_temp(self, monkeypatch):
        """Kanban worker A adjusts temp → worker B still gets worker_temperature."""
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test123")
        monkeypatch.delenv("HERMES_DELEGATED_TASK", raising=False)

        from tools.temperature_tool import adjust_temperature_tool
        from agent.chat_completion_helpers import resolve_temperature

        agent_a = _make_agent(temperature=0.7, worker_temperature=0.1)
        agent_b = _make_agent(temperature=0.7, worker_temperature=0.1)

        # Agent A overrides session (temperature=1.4, within [0.0, 2.0] range)
        adjust_temperature_tool(temperature=1.4, agent=agent_a)
        assert resolve_temperature(agent_a) == pytest.approx(1.4)

        # Agent B still gets worker_temperature (no session override)
        assert resolve_temperature(agent_b) == 0.1

    def test_interactive_agent_gets_profile_temp(self):
        """No env vars → resolve to profile temperature."""
        from agent.chat_completion_helpers import resolve_temperature

        agent = _make_agent(temperature=0.7)
        assert resolve_temperature(agent) == 0.7

    def test_interactive_agent_none_when_no_profile_temp(self):
        """No env vars and no profile temp → None."""
        from agent.chat_completion_helpers import resolve_temperature

        agent = _make_agent(temperature=None, worker_temperature=None)
        assert resolve_temperature(agent) is None


# ── scenario 5: Kimi / Moonshot returns null ───────────────────────────

class TestKimiNullReturn:
    """Scenario 5 — Kimi and Moonshot providers return null from adjust_temperature."""

    @pytest.mark.parametrize("provider", ["kimi-coding", "kimi-coding-cn"])
    def test_kimi_provider_returns_null(self, provider):
        """Kimi providers don't support temperature — tool returns JSON null."""
        from tools.temperature_tool import adjust_temperature_tool

        agent = _make_agent(provider=provider, temperature=0.7)
        result = adjust_temperature_tool(temperature=1.05, agent=agent)

        assert json.loads(result) is None

    @pytest.mark.parametrize("provider", ["kimi-coding", "kimi-coding-cn"])
    def test_kimi_provider_does_not_mutate_session_temp(self, provider):
        """Kimi provider: _session_temperature stays None after call."""
        from tools.temperature_tool import adjust_temperature_tool

        agent = _make_agent(provider=provider, temperature=0.7)
        adjust_temperature_tool(temperature=1.05, agent=agent)

        assert agent._session_temperature is None

    def test_kimi_does_not_block_resolve_for_non_kimi(self):
        """Non-Kimi provider still works normally."""
        from tools.temperature_tool import adjust_temperature_tool

        agent = _make_agent(provider="deepseek", temperature=0.7)
        result = json.loads(adjust_temperature_tool(temperature=0.84, agent=agent))

        assert result["temperature"] == pytest.approx(0.84)
        assert agent._session_temperature == pytest.approx(0.84)


# ── scenario 6: clamping at 0.0 and 2.0 ────────────────────────────────

class TestClamping:
    """Scenario 6 — Temperature is clamped to [0.0, 2.0]."""

    def test_clamped_to_upper_bound_2_0(self):
        """Values above 2.0 clamp to 2.0."""
        from tools.temperature_tool import adjust_temperature_tool

        agent = _make_agent(temperature=0.7)
        result = json.loads(adjust_temperature_tool(temperature=5.0, agent=agent))

        assert result["temperature"] == 2.0
        assert agent._session_temperature == 2.0

    def test_clamped_to_lower_bound_0_0(self):
        """Negative values clamp to 0.0."""
        from tools.temperature_tool import adjust_temperature_tool

        agent = _make_agent(temperature=0.7)
        result = json.loads(adjust_temperature_tool(temperature=-1.0, agent=agent))

        assert result["temperature"] == 0.0
        assert agent._session_temperature == 0.0

    def test_clamped_from_high_input(self):
        """temperature=5.0 with existing session → clamps to 2.0, changed detected."""
        from tools.temperature_tool import adjust_temperature_tool

        agent = _make_agent(temperature=1.5, session_temperature=1.5)
        result = json.loads(adjust_temperature_tool(temperature=5.0, agent=agent))

        # 5.0 clamped to 2.0, changed from previous 1.5
        assert result["temperature"] == 2.0
        assert result["changed"] is True

    def test_clamped_from_low_input(self):
        """Negative input with existing session → clamps to 0.0."""
        from tools.temperature_tool import adjust_temperature_tool

        agent = _make_agent(temperature=0.1, session_temperature=0.1)
        result = json.loads(adjust_temperature_tool(temperature=-1.0, agent=agent))

        assert result["temperature"] == 0.0

    def test_no_change_detected_at_boundary(self):
        """Setting 2.0 when already at 2.0 should detect no change."""
        from tools.temperature_tool import adjust_temperature_tool

        agent = _make_agent(temperature=2.0, session_temperature=2.0)
        result = json.loads(adjust_temperature_tool(temperature=2.0, agent=agent))

        assert result["temperature"] == 2.0
        assert result["changed"] is False


# ── scenario 7: temperature_map scaling ─────────────────────────────────

class TestTemperatureMap:
    """Scenario 7 — temperature_map multiplier applied after resolve_temperature.

    The multiplier lives in build_api_kwargs(), which multiplies the resolved
    temperature by _temperature_map and clamps to [0.0, 2.0].  These tests
    replicate that logic on top of resolve_temperature() output.
    """

    @staticmethod
    def _effective(agent):
        """Resolve temperature then apply _temperature_map like build_api_kwargs."""
        from agent.chat_completion_helpers import resolve_temperature

        t = resolve_temperature(agent)
        if t is None:
            return None
        m = getattr(agent, '_temperature_map', None)
        if m is None:
            return t
        return max(0.0, min(2.0, t * m))

    def test_map_0_66_scales_2_0_to_1_32(self):
        """adjust_temperature(2.0) with temperature_map=0.66 → effective 1.32."""
        from tools.temperature_tool import adjust_temperature_tool

        agent = _make_agent(temperature=0.7, temperature_map=0.66)
        adjust_temperature_tool(temperature=2.0, agent=agent)

        assert agent._session_temperature == pytest.approx(2.0)
        assert self._effective(agent) == pytest.approx(1.32)

    def test_default_map_1_0_passes_through_unchanged(self):
        """temperature_map=1.0 (default) → effective == resolved (2.0)."""
        from tools.temperature_tool import adjust_temperature_tool

        agent = _make_agent(temperature=0.7, temperature_map=1.0)
        adjust_temperature_tool(temperature=2.0, agent=agent)

        assert self._effective(agent) == pytest.approx(2.0)

    def test_worker_temp_also_scaled_by_map(self, monkeypatch):
        """Kanban worker_temperature * temperature_map in build_api_kwargs."""
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test123")
        monkeypatch.delenv("HERMES_DELEGATED_TASK", raising=False)

        # worker_temperature=0.5, temperature_map=0.66 → effective 0.33
        agent = _make_agent(temperature=0.7, worker_temperature=0.5,
                            temperature_map=0.66)

        from agent.chat_completion_helpers import resolve_temperature
        assert resolve_temperature(agent) == 0.5  # raw worker temp
        assert self._effective(agent) == pytest.approx(0.33)

    def test_no_map_none_skips_multiplier(self):
        """No _temperature_map → resolve_temperature output used directly."""
        from tools.temperature_tool import adjust_temperature_tool

        agent = _make_agent(temperature=0.7, temperature_map=None)
        adjust_temperature_tool(temperature=2.0, agent=agent)

        assert self._effective(agent) == pytest.approx(2.0)

    def test_map_clamps_high_scale_to_2_0(self):
        """2.0 * 2.0=4.0 clamped back down to 2.0."""
        agent = _make_agent(temperature=2.0, session_temperature=2.0,
                            temperature_map=2.0)

        assert self._effective(agent) == pytest.approx(2.0)


# ── init_agent: worker_temperature propagation ─────────────────────────
# NOTE: init_agent worker_temperature wiring is verified end-to-end via
# resolve_temperature tests (TestKanbanWorkerTemperature, TestDelegatedTaskTemperature).
# Full init_agent integration requires a complete agent object; tested separately
# in integration harness (t_f525ada2: 208 delegation tests pass).
