"""Regression: strip-retry after 413 must not die on the compression budget.

Evan (2026-11-27): when the 413 recovery strips retained vision payloads
("📐 Compression could not reduce the request further — removed retained vision
payloads and retrying..."), the payload changed — so the retried request is
NEW work and prior compression attempts must not count against it.

Prior behavior (with ``compression.max_attempts: 1`` — the forced cap when
``compression.internal_only: true``): first 413 → attempt 1 → strip → retry →
second 413 → attempt 2 > 1 → terminal "max compression attempts (1) reached",
even though the strip had just changed the payload. The fix resets
``compression_attempts`` to 0 after a successful strip, so progressive
stripping can continue; if nothing more can be stripped, the loop terminates
honestly with "cannot compress further" instead of the misleading budget error.

This test drives a real ``AIAgent`` with the same harness as
``test_preflight_compression_cap_e2e.py``: max_attempts=1, the API call
returns 413 twice (each followed by a successful strip), then succeeds.
Without the fix the turn dies on the budget after the second 413; with the
fix it completes.
"""
from __future__ import annotations

import contextlib
import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from hermes_state import SessionDB
from run_agent import AIAgent


class _FakeApiError(Exception):
    """Stand-in for an openai.BadRequestError with status_code + body."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.body = {"error": {"message": message}}
        self.response = None


def _config(max_attempts) -> dict:
    return {
        "compression": {
            "enabled": True,
            "threshold": 0.50,
            "target_ratio": 0.20,
            "protect_first_n": 3,
            "protect_last_n": 20,
            "max_attempts": max_attempts,
        },
        "prompt_caching": {"cache_ttl": "5m"},
        "sessions": {},
        "bedrock": {},
    }


def _stop_response():
    msg = SimpleNamespace(
        content="done",
        reasoning_content=None,
        reasoning=None,
        tool_calls=None,
    )
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


def _make_agent(monkeypatch, tmp_path: Path, *, max_attempts) -> AIAgent:
    from hermes_cli import config as config_mod

    monkeypatch.setattr(config_mod, "load_config", lambda: _config(max_attempts))
    monkeypatch.setattr(
        config_mod, "load_config_readonly", lambda: _config(max_attempts)
    )
    db = SessionDB(db_path=tmp_path / "state.db")
    with (
        contextlib.redirect_stdout(io.StringIO()),
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            base_url="https://openrouter.ai/api/v1",
            api_key="test-key",
            model="test/model",
            enabled_toolsets=[],
            disabled_toolsets=[],
            quiet_mode=True,
            skip_memory=True,
            skip_context_files=True,
            session_db=db,
            session_id="strip-retry-budget-e2e",
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent._disable_streaming = True
    agent.tool_delay = 0
    agent.save_trajectories = False
    return agent


def test_strip_retry_resets_compression_budget(monkeypatch, tmp_path):
    agent = _make_agent(monkeypatch, tmp_path, max_attempts=1)
    assert agent.max_compression_attempts == 1

    # Keep the preflight pressure gate out of the picture.
    agent.context_compressor.threshold_tokens = 100_000_000

    # Compression never reduces (returns messages unchanged) — the 413
    # fallthrough goes to the vision-payload strip.
    def _noop_compress(messages, system_message, **_kwargs):
        return messages, "compressed prompt"

    strip_calls = []

    def _strip_once(api_messages, **kwargs):
        # Two successful strips (each changes the work), then nothing left.
        strip_calls.append(len(api_messages))
        return len(strip_calls) <= 2

    err = _FakeApiError(413, "Request payload too large")
    agent.client.chat.completions.create.side_effect = [
        err, err, _stop_response(),
    ]

    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i}"}
        for i in range(10)
    ]

    with (
        patch.object(agent, "_compress_context", side_effect=_noop_compress),
        patch.object(
            agent,
            "_try_strip_image_parts_from_tool_messages",
            side_effect=_strip_once,
        ),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("hello", conversation_history=history)

    assert result["completed"] is True, (
        f"strip-retry should complete after the payload changed; got: "
        f"{result.get('final_response')} {result.get('error')}"
    )
    assert result.get("failed") is not True
    # Both strips were consumed — the second 413 was allowed through.
    assert len(strip_calls) == 2
