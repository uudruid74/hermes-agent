"""Codex HTTP throttle: shared token pacing before OpenAI rejects a request."""

import json
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

import agent.openai_rate_limit_throttle as throttle_module
from agent.openai_rate_limit_throttle import (
    DEFAULT_OPENAI_CODEX_RATE_LIMIT_POLICY,
    OpenAIRateLimitThrottle,
    OpenAIRateLimitPolicy,
    install_openai_rate_limit_throttle,
)


class _Clock:
    def __init__(self, now=1_000.0):
        self.now = now
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _response(status, headers=None):
    return SimpleNamespace(status_code=status, headers=headers or {})


def _request(byte_count):
    return SimpleNamespace(
        method="POST",
        url="https://chatgpt.com/backend-api/codex/responses",
        content=b"x" * byte_count,
    )


@pytest.fixture()
def throttle(tmp_path, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(
        throttle_module, "jittered_backoff", lambda *a, **kw: kw["base_delay"] + 1.0
    )
    return OpenAIRateLimitThrottle(
        state_path=tmp_path / "openai.json",
        clock=clock.time,
        sleep=clock.sleep,
    ), clock


def test_429_uses_reset_tokens_as_floor_and_backs_down_ramp(throttle):
    limiter, clock = throttle
    limiter._save_state({
        "ramp_tpm": 4_000_000.0,
        "ramp_updated_at": clock.time(),
        "last_request_at": clock.time(),
        "reservations": [],
    })

    classification = limiter.after_response(
        _response(
            429,
            {
                "Retry-After": "30",
                "x-ratelimit-reset-tokens": "6m0s",
            },
        )
    )

    state = json.loads(limiter.state_path.read_text())
    assert classification == "slow_down"
    assert 360.0 <= state["blocked_until"] - clock.time() <= 396.0
    assert state["ramp_tpm"] == pytest.approx(2_000_000.0)


def test_429_backs_down_initial_one_million_tpm_ramp(throttle):
    limiter, _clock = throttle

    limiter.after_response(_response(429))

    state = json.loads(limiter.state_path.read_text())
    assert state["ramp_tpm"] == pytest.approx(500_000.0)


@pytest.mark.parametrize(
    ("status", "classification"),
    [(503, "server_is_overloaded"), (402, "billing_or_quota")],
)
def test_503_and_402_do_not_create_rate_limit_cooldown(
    throttle, status, classification
):
    limiter, _clock = throttle

    assert (
        limiter.after_response(_response(status, {"Retry-After": "90"}))
        == classification
    )

    assert not limiter.state_path.exists()


def test_next_request_waits_for_token_reset_when_remaining_is_insufficient(throttle):
    limiter, clock = throttle
    limiter.after_response(
        _response(
            200,
            {
                "x-ratelimit-limit-tokens": "1000",
                "x-ratelimit-remaining-tokens": "100",
                "x-ratelimit-reset-tokens": "6m0s",
            },
        )
    )

    limiter.before_request(_request(800))  # ~200 tokens, greater than 100 remaining

    assert clock.sleeps
    assert sum(clock.sleeps) >= 360.0


def test_ramp_grows_by_at_most_fifty_percent_after_fifteen_active_minutes(throttle):
    limiter, clock = throttle
    limiter._save_state({
        "ramp_tpm": 1_000_000.0,
        "ramp_updated_at": clock.time() - 901.0,
        "last_request_at": clock.time() - 10.0,
        "reservations": [],
    })

    limiter.reserve(1)

    state = json.loads(limiter.state_path.read_text())
    assert state["ramp_tpm"] == pytest.approx(1_500_000.0)


def test_idle_traffic_resets_ramp_to_one_million_tpm(throttle):
    limiter, clock = throttle
    limiter._save_state({
        "ramp_tpm": 4_000_000.0,
        "ramp_updated_at": clock.time() - 901.0,
        "last_request_at": clock.time() - 61.0,
        "reservations": [],
    })

    limiter.reserve(1)

    state = json.loads(limiter.state_path.read_text())
    assert state["ramp_tpm"] == pytest.approx(1_000_000.0)


def test_default_config_exposes_current_codex_throttle_policy():
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["rate_limits"]["openai_codex"] == {
        "base_tpm": 1_000_000,
        "backdown_factor": 0.5,
        "reservation_window_seconds": 60,
        "idle_reset_seconds": 60,
        "ramp_interval_seconds": 900,
        "ramp_multiplier": 1.5,
        "retry_after_cap_seconds": 600,
    }


def test_policy_is_immutable_and_rejects_invalid_values():
    with pytest.raises(FrozenInstanceError):
        DEFAULT_OPENAI_CODEX_RATE_LIMIT_POLICY.base_tpm = 1

    with pytest.raises(ValueError, match="backdown_factor"):
        OpenAIRateLimitPolicy.from_config({"backdown_factor": 0})
    with pytest.raises(ValueError, match="unknown"):
        OpenAIRateLimitPolicy.from_config({"unknown": 1})


def test_custom_policy_controls_pacing_backdown_and_retry_cap(tmp_path, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(
        throttle_module, "jittered_backoff", lambda *a, **kw: kw["base_delay"] + 1.0
    )
    policy = OpenAIRateLimitPolicy.from_config(
        {
            "base_tpm": 100,
            "backdown_factor": 0.25,
            "reservation_window_seconds": 10,
            "idle_reset_seconds": 300,
            "ramp_interval_seconds": 20,
            "ramp_multiplier": 1.25,
            "retry_after_cap_seconds": 7,
        }
    )
    limiter = OpenAIRateLimitThrottle(
        policy=policy,
        state_path=tmp_path / "custom-openai.json",
        clock=clock.time,
        sleep=clock.sleep,
    )

    assert limiter.reserve(80) == 0
    assert limiter.reserve(30) == pytest.approx(10)

    limiter.after_response(_response(429, {"Retry-After": "999"}))

    state = json.loads(limiter.state_path.read_text())
    assert state["ramp_tpm"] == pytest.approx(25)
    assert 7 <= state["blocked_until"] - clock.time() <= 7.7


def test_install_loads_one_policy_from_config(monkeypatch):
    import hermes_cli.config

    custom = {
        "rate_limits": {
            "openai_codex": {
                "base_tpm": 600_000,
                "backdown_factor": 0.35,
                "idle_reset_seconds": 300,
                "ramp_multiplier": 1.25,
            }
        }
    }
    monkeypatch.setattr(hermes_cli.config, "load_config_readonly", lambda: custom)
    client = SimpleNamespace(event_hooks={"request": [], "response": []})
    agent = SimpleNamespace(
        _interrupt_requested=False, _emit_status=lambda _message: None
    )

    limiter = install_openai_rate_limit_throttle(client, agent)

    assert limiter.policy == OpenAIRateLimitPolicy(
        base_tpm=600_000,
        backdown_factor=0.35,
        reservation_window_seconds=60,
        idle_reset_seconds=300,
        ramp_interval_seconds=900,
        ramp_multiplier=1.25,
        retry_after_cap_seconds=600,
    )
    assert agent._openai_codex_rate_limit_policy is limiter.policy


def test_install_is_idempotent():
    client = SimpleNamespace(event_hooks={"request": [], "response": []})
    agent = SimpleNamespace(
        _interrupt_requested=False, _emit_status=lambda _message: None
    )

    first = install_openai_rate_limit_throttle(client, agent)
    second = install_openai_rate_limit_throttle(client, agent)

    assert first is second
    assert len(client.event_hooks["request"]) == 1
    assert len(client.event_hooks["response"]) == 1
