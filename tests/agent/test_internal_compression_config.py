from __future__ import annotations

import contextlib
import io

from hermes_state import SessionDB
from run_agent import AIAgent


def _make_agent(monkeypatch, tmp_path, internal_only=None, micro_compact=False):
    from hermes_cli import config as config_mod

    compression = {
        "enabled": True,
        "threshold": 0.5,
        "target_ratio": 0.2,
    }
    if internal_only is not None:
        compression["internal_only"] = internal_only
    if micro_compact:
        compression["micro_compact"] = True
    config = {
        "compression": compression,
        "prompt_caching": {"cache_ttl": "5m"},
        "sessions": {},
        "bedrock": {},
    }
    monkeypatch.setattr(config_mod, "load_config", lambda: config)
    monkeypatch.setattr(config_mod, "load_config_readonly", lambda: config)
    with contextlib.redirect_stdout(io.StringIO()):
        return AIAgent(
            base_url="https://chatgpt.com/backend-api/codex",
            api_key=str(tmp_path),
            provider="openai-codex",
            model="gpt-5.5",
            enabled_toolsets=[],
            disabled_toolsets=[],
            quiet_mode=True,
            skip_memory=True,
            session_db=SessionDB(db_path=tmp_path / "state.db"),
            session_id="internal-compression-config-test",
        )


def test_internal_only_defaults_off(monkeypatch, tmp_path):
    agent = _make_agent(monkeypatch, tmp_path)

    assert agent.context_compressor.internal_only is False


def test_internal_only_config_is_attached_to_compressor(monkeypatch, tmp_path):
    agent = _make_agent(monkeypatch, tmp_path, internal_only=True)

    assert agent.context_compressor.internal_only is True


def test_internal_only_disables_llm_micro_compaction(monkeypatch, tmp_path):
    agent = _make_agent(
        monkeypatch,
        tmp_path,
        internal_only=True,
        micro_compact=True,
    )

    assert agent.context_compressor._micro_compact_enabled is False
