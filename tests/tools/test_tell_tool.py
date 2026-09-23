"""Tests for the agent-to-agent tell tool."""

import json
from types import SimpleNamespace

import pytest

from tools import tell_tool


def _spawned():
    """A truthy stand-in for a live Popen handle (spawn succeeded)."""
    return SimpleNamespace()


def test_tell_wraps_message_and_targets_agent_profile(monkeypatch):
    monkeypatch.setenv("USERNAME", "zephyr")
    monkeypatch.setenv("HERMES_AGENT_NAME", "WrongAgent")
    monkeypatch.setenv("HERMES_PROFILE", "ignored-profile")
    calls = []
    echoes = []

    def fake_spawn(command, **_kwargs):
        calls.append(command)
        return _spawned()

    monkeypatch.setattr(tell_tool, "spawn_detached", fake_spawn)

    result = json.loads(
        tell_tool.tell_tool(
            agent="gopher",
            message="Check the relay.",
            echo_callback=echoes.append,
        )
    )

    assert calls == [
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
        ]
    ]
    assert echoes == ["gopher: Check the relay."]
    assert result == {"dispatched": True, "note": "notified gopher"}


def test_tell_routes_to_explicit_session_and_displays_reply_session(monkeypatch):
    monkeypatch.setenv("USERNAME", "zephyr")
    calls = []

    def fake_spawn(command, **_kwargs):
        calls.append(command)
        return _spawned()

    monkeypatch.setattr(tell_tool, "spawn_detached", fake_spawn)

    tell_tool.tell_tool(
        agent="neo",
        message="Cron reply.",
        session_id="cron_daily_20260922_120000",
        origin_session_id="20260922_121500_receiver",
        echo_callback=lambda _message: None,
    )

    command = calls[0]
    assert command[:4] == [
        "hermes",
        "send",
        "-u",
        "neo:cli:cron_daily_20260922_120000",
    ]
    assert "agent='zephyr'" in command[-1]
    assert "session_id='20260922_121500_receiver'" in command[-1]


def test_tell_reply_reaches_explicit_session_via_send_cli(monkeypatch, tmp_path):
    import argparse

    from hermes_cli import profiles, send_cmd
    from hermes_state import SessionDB

    target_home = tmp_path / "neo"
    target_home.mkdir()
    target_session = "cron_daily_20260922_120000"
    session_db = SessionDB(db_path=target_home / "state.db")
    session_db.create_session(session_id=target_session, source="cron")
    session_db.close()

    monkeypatch.setenv("USERNAME", "zephyr")
    monkeypatch.setattr(profiles, "profile_exists", lambda name: name == "neo")
    monkeypatch.setattr(profiles, "get_profile_dir", lambda _name: target_home)
    monkeypatch.setattr(send_cmd, "_load_hermes_env", lambda: None)

    def run_send_cli(command, **_kwargs):
        args = argparse.Namespace(
            to=None,
            user=command[3],
            message=command[4],
            file=None,
            subject=None,
            list_targets=False,
            json=False,
            quiet=True,
        )
        with pytest.raises(SystemExit) as exc:
            send_cmd.cmd_send(args)
        assert exc.value.code == 0
        return _spawned()

    monkeypatch.setattr(tell_tool, "spawn_detached", run_send_cli)

    result = json.loads(
        tell_tool.tell_tool(
            agent="neo",
            message="Round trip reply.",
            session_id=target_session,
            origin_session_id="20260922_121500_receiver",
            echo_callback=lambda _message: None,
        )
    )

    session_db = SessionDB(db_path=target_home / "state.db")
    notices = session_db.drain_session_notices(target_session)
    session_db.close()

    assert result == {"dispatched": True, "note": "notified neo"}
    assert len(notices) == 1
    assert "Round trip reply." in notices[0]["text"]
    assert "session_id='20260922_121500_receiver'" in notices[0]["text"]


def test_tell_does_not_fall_back_to_superseded_identity_vars(monkeypatch):
    monkeypatch.delenv("USERNAME", raising=False)
    monkeypatch.setenv("HERMES_AGENT_NAME", "WrongAgent")
    monkeypatch.setenv("HERMES_PROFILE", "wrong-profile")
    calls = []

    def fake_spawn(command, **_kwargs):
        calls.append(command)
        return _spawned()

    monkeypatch.setattr(tell_tool, "spawn_detached", fake_spawn)

    result = json.loads(
        tell_tool.tell_tool(
            agent="zephyr",
            message="Reply test.",
            echo_callback=lambda _message: None,
        )
    )

    assert result["dispatched"] is True
    assert "Incoming message from agent follows:" in calls[0][-1]
    assert "WrongAgent" not in calls[0][-1]
    assert "wrong-profile" not in calls[0][-1]


def test_tell_echoes_exact_message_to_origin_before_send(monkeypatch):
    events = []
    echoes = []

    def fake_spawn(command, **_kwargs):
        events.append("send")
        return _spawned()

    def capture_echo(payload):
        events.append("echo")
        echoes.append(payload)

    monkeypatch.setattr(tell_tool, "spawn_detached", fake_spawn)

    tell_tool.tell_tool(
        agent="gopher",
        message="line one\n[REDACTED must remain literal]\nline three",
        echo_callback=capture_echo,
    )

    assert events == ["echo", "send"]
    assert echoes == ["gopher: line one\n[REDACTED must remain literal]\nline three"]


def test_tell_requires_echo_callback_and_does_not_send_without_one(monkeypatch):
    calls = []
    monkeypatch.setattr(tell_tool, "spawn_detached", lambda *_a, **_kw: calls.append(True))

    with pytest.raises(TypeError):
        tell_tool.tell_tool(agent="gopher", message="This must not be secret.")

    assert calls == []


def test_tell_does_not_send_when_origin_echo_delivery_fails(monkeypatch):
    calls = []
    monkeypatch.setattr(tell_tool, "spawn_detached", lambda *_a, **_kw: calls.append(True))

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
    monkeypatch.setattr(tell_tool, "spawn_detached", lambda *_args, **_kwargs: _spawned())
    logs = []
    monkeypatch.setattr(tell_tool.logger, "info", lambda *args, **_kwargs: logs.append(args))

    tell_tool.tell_tool(
        agent="gopher",
        message="Check the relay.",
        echo_callback=lambda _message: None,
    )

    assert logs == [("%s", "gopher: Check the relay.")]


def test_tell_reports_spawn_failure_and_echoes_first(monkeypatch):
    echoes = []

    def fake_spawn(command, **_kwargs):
        return None  # spawn failure (binary missing, permission, ...)

    monkeypatch.setattr(tell_tool, "spawn_detached", fake_spawn)

    result = json.loads(
        tell_tool.tell_tool(
            agent="zephyr",
            message="Are you there?",
            echo_callback=echoes.append,
        )
    )

    assert echoes == ["zephyr: Are you there?"]
    assert result == {"error": "failed to launch 'hermes send' for zephyr"}


def test_agent_runtime_routes_tell_to_mandatory_origin_callback(monkeypatch):
    from agent.agent_runtime_helpers import invoke_tool

    monkeypatch.setenv("USERNAME", "neo")
    sent = []
    echoes = []

    def fake_spawn(command, **_kwargs):
        sent.append(command[-1])
        return _spawned()

    monkeypatch.setattr(tell_tool, "spawn_detached", fake_spawn)
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
    assert result["dispatched"] is True


def test_agent_runtime_exposes_cron_session_id_for_a_reply(monkeypatch):
    from agent.agent_runtime_helpers import invoke_tool

    monkeypatch.setenv("USERNAME", "neo")
    sent = []

    def fake_spawn(command, **_kwargs):
        sent.append(command)
        return _spawned()

    monkeypatch.setattr(tell_tool, "spawn_detached", fake_spawn)
    agent = SimpleNamespace(
        _memory_manager=None,
        tell_echo_callback=lambda _message: None,
        session_id="cron_journal_20260922_120000",
    )

    invoke_tool(
        agent,
        "tell",
        {
            "agent": "zephyr",
            "message": "Talk with me.",
            "session_id": "20260922_121500_receiver",
        },
        "",
        pre_tool_block_checked=True,
        skip_tool_request_middleware=True,
        skip_tool_execution_middleware=True,
    )

    assert sent[0][:4] == [
        "hermes",
        "send",
        "-u",
        "zephyr:cli:20260922_121500_receiver",
    ]
    assert "agent='neo'" in sent[0][-1]
    assert "session_id='cron_journal_20260922_120000'" in sent[0][-1]


def test_agent_runtime_refuses_tell_without_origin_callback(monkeypatch):
    from agent.agent_runtime_helpers import invoke_tool

    calls = []
    monkeypatch.setattr(tell_tool, "spawn_detached", lambda *_a, **_kw: calls.append(True))
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


def test_tell_schema_requires_agent_and_message_and_accepts_session_id():
    assert tell_tool.TELL_SCHEMA["parameters"]["required"] == ["agent", "message"]
    assert "session_id" in tell_tool.TELL_SCHEMA["parameters"]["properties"]
    assert tell_tool.TELL_SCHEMA["name"] == "tell"


def test_tell_is_registered_in_session_toolset():
    entry = tell_tool.registry.get_entry("tell")

    assert entry is not None
    assert entry.toolset == "session"


def test_tell_is_in_hermes_cli_toolset_used_by_agent_profiles():
    from toolsets import TOOLSETS

    assert "tell" in TOOLSETS["hermes-cli"]["tools"]
    assert "tell" in TOOLSETS["session"]["tools"]
