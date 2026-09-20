"""Tests for the agent-to-agent tell tool."""

import json
from types import SimpleNamespace

import pytest

from tools import tell_tool


def _completed(*, returncode=0, stdout="sent\n", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_tell_wraps_message_and_targets_agent_profile(monkeypatch):
    monkeypatch.setenv("USERNAME", "zephyr")
    monkeypatch.setenv("HERMES_AGENT_NAME", "WrongAgent")
    monkeypatch.setenv("HERMES_PROFILE", "ignored-profile")
    calls = []
    echoes = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _completed()

    monkeypatch.setattr(tell_tool.subprocess, "run", fake_run)

    result = json.loads(
        tell_tool.tell_tool(
            agent="gopher",
            message="Check the relay.",
            echo_callback=echoes.append,
        )
    )

    assert calls == [
        (
            [
                "hermes",
                "send",
                "-u",
                "gopher",
                "Incoming message from zephyr follows:\n"
                "---\n"
                "Check the relay.\n"
                "---\n"
                "If a reply is required, use the 'tell' command to reply.",
            ],
            {"capture_output": True, "text": True, "timeout": 15},
        )
    ]
    assert echoes == ["gopher: Check the relay."]
    assert result == {"returncode": 0, "stdout": "sent\n", "stderr": ""}


def test_tell_does_not_fall_back_to_superseded_identity_vars(monkeypatch):
    monkeypatch.delenv("USERNAME", raising=False)
    monkeypatch.setenv("HERMES_AGENT_NAME", "WrongAgent")
    monkeypatch.setenv("HERMES_PROFILE", "wrong-profile")
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return _completed()

    monkeypatch.setattr(tell_tool.subprocess, "run", fake_run)

    result = json.loads(
        tell_tool.tell_tool(
            agent="zephyr",
            message="Reply test.",
            echo_callback=lambda _message: None,
        )
    )

    assert result["returncode"] == 0
    assert "Incoming message from agent follows:" in calls[0][-1]
    assert "WrongAgent" not in calls[0][-1]
    assert "wrong-profile" not in calls[0][-1]


def test_tell_echoes_exact_message_to_origin_before_send(monkeypatch):
    events = []
    echoes = []

    def fake_run(command, **kwargs):
        events.append("send")
        return _completed()

    def capture_echo(payload):
        events.append("echo")
        echoes.append(payload)

    monkeypatch.setattr(tell_tool.subprocess, "run", fake_run)

    tell_tool.tell_tool(
        agent="gopher",
        message="line one\n[REDACTED must remain literal]\nline three",
        echo_callback=capture_echo,
    )

    assert events == ["echo", "send"]
    assert echoes == ["gopher: line one\n[REDACTED must remain literal]\nline three"]


def test_tell_requires_echo_callback_and_does_not_send_without_one(monkeypatch):
    calls = []
    monkeypatch.setattr(tell_tool.subprocess, "run", lambda *_a, **_kw: calls.append(True))

    with pytest.raises(TypeError):
        tell_tool.tell_tool(agent="gopher", message="This must not be secret.")

    assert calls == []


def test_tell_does_not_send_when_origin_echo_delivery_fails(monkeypatch):
    calls = []
    monkeypatch.setattr(tell_tool.subprocess, "run", lambda *_a, **_kw: calls.append(True))

    def fail_echo(_message):
        raise RuntimeError("origin unavailable")

    with pytest.raises(RuntimeError, match="origin unavailable"):
        tell_tool.tell_tool(
            agent="gopher",
            message="This must not be secret.",
            echo_callback=fail_echo,
        )

    assert calls == []


def test_tell_logs_exact_echo(monkeypatch):
    monkeypatch.setattr(tell_tool.subprocess, "run", lambda *_args, **_kwargs: _completed())
    logs = []
    monkeypatch.setattr(tell_tool.logger, "info", lambda *args, **_kwargs: logs.append(args))

    tell_tool.tell_tool(
        agent="gopher",
        message="Check the relay.",
        echo_callback=lambda _message: None,
    )

    assert logs == [("%s", "gopher: Check the relay.")]


def test_tell_preserves_send_failure_result_and_echoes_first(monkeypatch):
    echoes = []

    def fake_run(command, **kwargs):
        return _completed(
            returncode=1,
            stdout="",
            stderr="hermes send: bridge socket missing\n",
        )

    monkeypatch.setattr(tell_tool.subprocess, "run", fake_run)

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


def test_agent_runtime_routes_tell_to_mandatory_origin_callback(monkeypatch):
    from agent.agent_runtime_helpers import invoke_tool

    monkeypatch.setenv("USERNAME", "neo")
    sent = []
    echoes = []

    def fake_run(command, **kwargs):
        sent.append(command[-1])
        return _completed()

    monkeypatch.setattr(tell_tool.subprocess, "run", fake_run)
    agent = SimpleNamespace(
        _memory_manager=None,
        tell_echo_callback=echoes.append,
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
    assert sent and "Trace this." in sent[0]
    assert result["returncode"] == 0


def test_agent_runtime_refuses_tell_without_origin_callback(monkeypatch):
    from agent.agent_runtime_helpers import invoke_tool

    calls = []
    monkeypatch.setattr(tell_tool.subprocess, "run", lambda *_a, **_kw: calls.append(True))
    agent = SimpleNamespace(_memory_manager=None, session_id="session-1")

    with pytest.raises(RuntimeError, match="origin echo callback"):
        invoke_tool(
            agent,
            "tell",
            {"agent": "wintermute", "message": "Do not send secretly."},
            "",
            pre_tool_block_checked=True,
            skip_tool_request_middleware=True,
            skip_tool_execution_middleware=True,
        )

    assert calls == []


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
