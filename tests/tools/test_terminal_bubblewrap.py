"""Regression coverage for terminal no-plan Bubblewrap dispatch."""

import json
from unittest.mock import MagicMock, patch

import tools.terminal_tool as terminal_tool


def _recording_registry(captured):
    registry = MagicMock()

    def dispatch(name, args, **kwargs):
        captured["name"] = name
        captured["args"] = args
        captured["kwargs"] = kwargs
        return json.dumps({"result": "ok"})

    registry.dispatch.side_effect = dispatch
    return registry


def test_eligible_terminal_cwd_dispatches_with_internal_bubblewrap_root(monkeypatch):
    captured = {}
    import agent.file_safety as file_safety
    from model_tools import handle_function_call

    monkeypatch.setattr(
        file_safety, "terminal_bubblewrap_root", lambda cwd: "/tmp/terminal-root"
    )
    monkeypatch.setattr(file_safety, "is_write_denied_by_task_gate", lambda: True)
    with patch("model_tools.registry", _recording_registry(captured)):
        result = handle_function_call(
            "terminal",
            {"command": "true", "cwd": "/tmp/terminal-root"},
            skip_pre_tool_call_hook=True,
        )

    assert json.loads(result) == {"result": "ok"}
    assert captured["name"] == "terminal"
    assert captured["args"]["_bubblewrap_root"] == "/tmp/terminal-root"


def test_bubblewrap_command_binds_only_workspace_writable(monkeypatch):
    monkeypatch.setattr(terminal_tool.shutil, "which", lambda name: "/usr/bin/bwrap")
    monkeypatch.setattr(terminal_tool.os.path, "exists", lambda path: path in {"/usr", "/etc"})

    command = terminal_tool._bubblewrap_command("touch created", "/tmp/sandbox")

    assert command.startswith("/usr/bin/bwrap --die-with-parent")
    assert "--ro-bind /usr /usr" in command
    assert "--ro-bind /etc /etc" in command
    assert "--tmpfs /tmp" in command
    assert "--bind /tmp/sandbox /workspace" in command
    assert "--chdir /workspace" in command
    assert command.endswith("/bin/sh -lc 'touch created'")


def test_terminal_uses_bubblewrap_root_as_effective_workdir(monkeypatch):
    calls = []

    class FakeEnv:
        env = {}

        def execute(self, command, **kwargs):
            calls.append((command, kwargs))
            return {"output": "ok", "returncode": 0}

    task_id = "sandboxed-session"
    monkeypatch.setattr(terminal_tool, "_active_environments", {task_id: FakeEnv()})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(terminal_tool, "_task_env_overrides", {})
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {"env_type": "local", "cwd": "/default", "timeout": 60, "lifetime_seconds": 3600},
    )
    monkeypatch.setattr(
        terminal_tool,
        "_check_all_guards",
        lambda command, env_type, **kwargs: {"approved": True},
    )
    monkeypatch.setattr(terminal_tool, "_bubblewrap_command", lambda command, root: f"bwrap {root} -- {command}")

    result = json.loads(
        terminal_tool.terminal_tool(
            command="pwd",
            task_id=task_id,
            requested_cwd="/tmp/sandbox",
            _bubblewrap_root="/tmp/sandbox",
        )
    )

    assert result["exit_code"] == 0
    assert calls == [
        ("bwrap /tmp/sandbox -- pwd", {"timeout": 60, "cwd": "/tmp/sandbox", "bounded_capture": True})
    ]
