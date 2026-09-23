"""Unit coverage for the background-review aux-model selector + pruned digest.

Covers the two behaviors this change adds:
  • _resolve_review_runtime — auto/same-model → not routed (main model);
    a configured different model → routed with resolved credentials.
  • _digest_history — compact size-bounded replay on EVERY review path: recent
    tail pruned (tool outputs truncated + duplicated outputs collapsed), older
    turns digested into one synthetic user-role message, hard char budget with
    oldest-first eviction.
  • _digest_exceeds_loaded_ctx — LM Studio loaded-context pre-flight skip.

Pure-function / config-driven; no live model calls.
"""
from typing import Any
from unittest.mock import patch

import json

from agent import background_review as br


def _msg(role, content, tool_calls=None):
    m = {"role": role, "content": content}
    if tool_calls:
        m["tool_calls"] = tool_calls
    return m


# ---------------------------------------------------------------------------
# _resolve_review_runtime — the aux-model selector
# ---------------------------------------------------------------------------

class _FakeAgent:
    def __init__(self, provider="openai-codex", model="gpt-5.5"):
        self.provider = provider
        self.model = model
        self._credential_pool: Any = None
        self.request_overrides = {}
        self.max_tokens: int | None = None

    def _current_main_runtime(self):
        return {
            "api_key": "parent-key",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_mode": "codex_app_server",
        }


def test_routing_auto_inherits_parent_and_downgrades_codex_app_server():
    agent = _FakeAgent()
    cfg = {"auxiliary": {"background_review": {"provider": "auto", "model": ""}}}
    with patch("hermes_cli.config.load_config", return_value=cfg), patch("hermes_cli.config.load_config_readonly", return_value=cfg):
        rt = br._resolve_review_runtime(agent)
    assert rt["routed"] is False
    assert rt["provider"] == "openai-codex"
    assert rt["model"] == "gpt-5.5"
    assert rt["api_mode"] == "codex_responses"  # downgraded so agent-loop tools dispatch


def test_routing_to_different_model_marks_routed_and_resolves_credentials():
    agent = _FakeAgent()
    cfg = {"auxiliary": {"background_review": {
        "provider": "openrouter", "model": "google/gemini-3-flash-preview",
    }}}
    fake_rp = {
        "provider": "openrouter", "api_key": "or-key",
        "base_url": "https://openrouter.ai/api/v1", "api_mode": "chat_completions",
        "credential_pool": "routed-pool",
        "request_overrides": {"extra_body": {"store": False}},
        "max_output_tokens": 2048,
    }
    with patch("hermes_cli.config.load_config", return_value=cfg), patch("hermes_cli.config.load_config_readonly", return_value=cfg), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=fake_rp):
        rt = br._resolve_review_runtime(agent)
    assert rt["routed"] is True
    assert rt["provider"] == "openrouter"
    assert rt["model"] == "google/gemini-3-flash-preview"
    assert rt["api_key"] == "or-key"
    assert rt["credential_pool"] == "routed-pool"
    assert rt["request_overrides"] == {"extra_body": {"store": False}}
    assert rt["max_tokens"] == 2048


def test_unrouted_runtime_keeps_parent_pool_and_overrides():
    agent = _FakeAgent()
    agent._credential_pool = "parent-pool"
    agent.request_overrides = {"service_tier": "priority"}
    agent.max_tokens = 4096
    with patch("hermes_cli.config.load_config", return_value={}), patch("hermes_cli.config.load_config_readonly", return_value={}):
        rt = br._resolve_review_runtime(agent)
    assert rt["credential_pool"] == "parent-pool"
    assert rt["request_overrides"] == {"service_tier": "priority"}
    assert rt["max_tokens"] == 4096


def test_routing_same_model_as_parent_is_not_routed():
    agent = _FakeAgent(provider="openrouter", model="anthropic/claude-opus-4.8")
    cfg = {"auxiliary": {"background_review": {
        "provider": "openrouter", "model": "anthropic/claude-opus-4.8",
    }}}
    with patch("hermes_cli.config.load_config", return_value=cfg), patch("hermes_cli.config.load_config_readonly", return_value=cfg):
        rt = br._resolve_review_runtime(agent)
    assert rt["routed"] is False  # same model/provider → keep full-replay path


def test_routing_resolution_failure_falls_back_to_parent():
    agent = _FakeAgent()
    cfg = {"auxiliary": {"background_review": {
        "provider": "openrouter", "model": "google/gemini-3-flash-preview",
    }}}
    with patch("hermes_cli.config.load_config", return_value=cfg), patch("hermes_cli.config.load_config_readonly", return_value=cfg), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider",
               side_effect=RuntimeError("boom")):
        rt = br._resolve_review_runtime(agent)
    assert rt["routed"] is False
    assert rt["provider"] == "openai-codex"


# ---------------------------------------------------------------------------
# _digest_history — routed-path compact replay
# ---------------------------------------------------------------------------

def test_digest_under_tail_returns_full():
    msgs = [_msg("user", "hi"), _msg("assistant", "hello")]
    assert br._digest_history(msgs, tail=24) == msgs


def test_digest_collapses_old_keeps_tail_verbatim():
    msgs = []
    for i in range(60):
        msgs.append(_msg("user", f"u{i} " + "x" * 50))
        msgs.append(_msg("assistant", f"a{i} " + "y" * 50))
    out = br._digest_history(msgs, tail=10)
    # First message is the synthetic digest (user role → alternation preserved).
    assert out[0]["role"] == "user"
    assert out[0]["content"].startswith("[Earlier conversation digest")
    # Recent tail preserved verbatim (pruning only touches tool outputs).
    assert out[-1] == msgs[-1]
    assert len(out) == 11  # 1 digest + 10 tail


def test_digest_does_not_open_tail_on_a_tool_message():
    msgs = []
    for i in range(40):
        msgs.append(_msg("user", "u" + "x" * 50))
        msgs.append(_msg("assistant", "", tool_calls=[
            {"function": {"name": "terminal", "arguments": "{}"}}]))
        msgs.append({"role": "tool", "content": "result " + "w" * 50})
    out = br._digest_history(msgs, tail=2)
    # The pruned tail (after the digest) must not begin on a bare tool message.
    assert out[1]["role"] != "tool"


def test_digest_records_tool_names_in_arc():
    old = [
        _msg("user", "do the thing"),
        _msg("assistant", "", tool_calls=[
            {"function": {"name": "skill_view", "arguments": "{}"}},
            {"function": {"name": "patch", "arguments": "{}"}}]),
    ]
    msgs = old + [_msg("user", f"tail{i}") for i in range(30)]
    out = br._digest_history(msgs, tail=10)
    digest = out[0]["content"]
    assert "USER: do the thing" in digest
    assert "tools: skill_view, patch" in digest


# ---------------------------------------------------------------------------
# _digest_history — pruning (tool-output truncation + repeat collapse)
# ---------------------------------------------------------------------------

def _tool_msg(content_text, tool_call_id="call_1"):
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": [{"type": "tool_result", "tool_call_id": tool_call_id, "content": content_text}],
    }


def test_prune_truncates_massive_tool_outputs():
    big = "D" * 5000  # 5KB tool output — the overflow class from the bug
    msgs = [_msg("user", "go"), _msg("assistant", "", tool_calls=[
        {"function": {"name": "terminal", "arguments": "{}"}}]), _tool_msg(big)]
    out = br._digest_history(msgs, tail=24)
    block = out[-1]["content"][0]
    assert block["type"] == "tool_result"
    text = block["content"]
    assert len(text) <= br._TOOL_OUTPUT_MAX + 100  # ~500 payload + marker overhead
    assert text[: br._TOOL_OUTPUT_TAIL] == "D" * br._TOOL_OUTPUT_TAIL
    assert text[-br._TOOL_OUTPUT_TAIL:] == "D" * br._TOOL_OUTPUT_TAIL
    assert "truncated" in text


def test_prune_collapses_identical_tool_result_blocks_in_one_turn():
    # One assistant turn fires 6 parallel tool calls; the tool message then
    # carries 6 identical tool_result blocks. They collapse to one line.
    same = "same result " * 20
    tool_msg = {
        "role": "tool",
        "content": [
            {"type": "tool_result", "tool_call_id": f"call_{i}", "content": same}
            for i in range(6)
        ],
    }
    msgs = [_msg("user", "go"), _msg("assistant", "", tool_calls=[
        {"function": {"name": "terminal", "arguments": "{}"}} for _ in range(6)]), tool_msg]
    out = br._digest_history(msgs, tail=24)
    blocks = [b for b in out[-1]["content"] if isinstance(b, dict)]
    tool_results = [b for b in blocks if b.get("type") == "tool_result"]
    assert len(tool_results) == 1  # 6 identical outputs → one line
    assert "[6× identical]" in tool_results[0]["content"]


def test_prune_leaves_small_tool_outputs_untouched():
    small = "tiny output"
    msgs = [_msg("user", "go"), _msg("assistant", "", tool_calls=[
        {"function": {"name": "terminal", "arguments": "{}"}}]), _tool_msg(small)]
    out = br._digest_history(msgs, tail=24)
    block = out[-1]["content"][0]
    assert block["content"] == small


def test_digest_budget_evicts_oldest_lines_first():
    # Many older turns with long text → budget trims the OLDEST lines.
    msgs = []
    for i in range(200):
        msgs.append(_msg("user", f"u{i} " + "x" * 400))
        msgs.append(_msg("assistant", f"a{i} " + "y" * 400))
    out = br._digest_history(msgs, tail=10)
    digest = out[0]["content"]
    assert len(digest) <= br._DIGEST_BUDGET + 1000  # digest content + intro len
    # Newest old turn survives the budget…
    assert "USER: u189" in digest
    # …but an old enough one got evicted (oldest-first).
    assert "USER: u0" not in digest


# ---------------------------------------------------------------------------
# _digest_exceeds_loaded_ctx — LM Studio pre-flight skip
# ---------------------------------------------------------------------------

def test_loaded_ctx_skip_fires_for_lmstudio_overflow():
    fake_models = {"data": [{"id": "m", "max_context_length": 262144, "loaded_context_length": 78643}]}
    with patch("urllib.request.urlopen") as mock_open:
        cm = mock_open.return_value.__enter__.return_value
        cm.read.return_value = json.dumps(fake_models).encode("utf-8")
        assert br._digest_exceeds_loaded_ctx(
            [_msg("user", "x" * 60000)], {"provider": "lmstudio", "base_url": "http://127.0.0.1:1234/v1"}
        ) is True


def test_loaded_ctx_skip_never_fires_for_api_providers():
    assert br._digest_exceeds_loaded_ctx(
        [_msg("user", "x" * 60000)], {"provider": "openrouter", "base_url": "https://openrouter.ai/api/v1"}
    ) is False


def test_loaded_ctx_skip_survives_probe_failure():
    with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
        assert br._digest_exceeds_loaded_ctx(
            [_msg("user", "x" * 1000)], {"provider": "lmstudio", "base_url": "http://127.0.0.1:1234/v1"}
        ) is False  # flaky probe must not silently kill the review
