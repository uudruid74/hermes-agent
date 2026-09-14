"""Tests for the agent-to-agent tell tool."""

import json
from types import SimpleNamespace

from tools import tell_tool


def _completed(*, returncode=0, stdout="sent\n", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_tell_wraps_message_and_targets_agent_profile(monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_NAME", "Zephyr")
    monkeypatch.setenv("HERMES_PROFILE", "ignored-profile")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _completed()

    monkeypatch.setattr(tell_tool.subprocess, "run", fake_run)

    result = json.loads(tell_tool.tell_tool(agent="gopher", message="Check the relay."))

    assert calls == [
        (
            [
                "hermes",
                "send",
                "-u",
                "gopher",
                "Incoming message from Zephyr follows:\n"
                "---\n"
                "Check the relay.\n"
                "---\n"
                "If a reply is required, use the 'tell' command to reply.",
            ],
            {"capture_output": True, "text": True, "timeout": 15},
        )
    ]
    assert result == {"returncode": 0, "stdout": "sent\n", "stderr": ""}


def test_tell_uses_profile_when_agent_name_is_absent(monkeypatch):
    monkeypatch.delenv("HERMES_AGENT_NAME", raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "neo")
    monkeypatch.setattr(
        tell_tool.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed(),
    )

    result = json.loads(tell_tool.tell_tool(agent="zephyr", message="Reply test."))

    assert result["returncode"] == 0


def test_tell_echoes_target_and_message_to_origin(monkeypatch):
    monkeypatch.setattr(tell_tool.subprocess, "run", lambda *_args, **_kwargs: _completed())
    echoes = []

    tell_tool.tell_tool(
        agent="gopher",
        message="Check the relay.",
        echo_callback=echoes.append,
    )

    assert echoes == ["gopher: Check the relay."]


def test_tell_preserves_send_failure_result_and_echoes_attempt(monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_NAME", "Gopher")
    monkeypatch.setattr(
        tell_tool.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed(
            returncode=1,
            stdout="",
            stderr="hermes send: bridge socket missing\n",
        ),
    )
    echoes = []

    result = json.loads(
        tell_tool.tell_tool(
            agent="zephyr",
            message="Are you there?",
            echo_callback=echoes.append,
        )
    )

    assert echoes == ["zephyr: Are you there?"]
    assert result == {
        "returncode": 1,
        "stdout": "",
        "stderr": "hermes send: bridge socket missing\n",
    }


def test_agent_runtime_routes_tell_to_origin_callback(monkeypatch):
    from agent.agent_runtime_helpers import invoke_tool

    monkeypatch.setattr(tell_tool.subprocess, "run", lambda *_args, **_kwargs: _completed())
    echoes = []
    agent = SimpleNamespace(
        _memory_manager=None,
        interim_assistant_callback=echoes.append,
        session_id="session-1",
    )

    result = json.loads(
        invoke_tool(
            agent,
            "tell",
            {"agent": "wintermute", "message": "Trace this."},
            "",
            pre_tool_block_checked=True,
            skip_tool_request_middleware=True,
            skip_tool_execution_middleware=True,
        )
    )

    assert echoes == ["wintermute: Trace this."]
    assert result["returncode"] == 0


def test_tell_is_owned_by_agent_runtime():
    from agent.agent_runtime_helpers import AGENT_RUNTIME_POST_HOOK_TOOL_NAMES
    from model_tools import _AGENT_LOOP_TOOLS

    assert "tell" in AGENT_RUNTIME_POST_HOOK_TOOL_NAMES
    assert "tell" in _AGENT_LOOP_TOOLS


def test_tell_schema_requires_agent_and_message():
    assert tell_tool.TELL_SCHEMA["parameters"]["required"] == ["agent", "message"]
    assert tell_tool.TELL_SCHEMA["name"] == "tell"


def test_tell_is_registered_in_session_toolset():
    entry = tell_tool.registry.get_entry("tell")

    assert entry is not None
    assert entry.toolset == "session"


def test_tell_is_in_hermes_cli_toolset_used_by_agent_profiles():
    from toolsets import TOOLSETS

    assert "tell" in TOOLSETS["hermes-cli"]["tools"]
    assert "tell" in TOOLSETS["session"]["tools"]
