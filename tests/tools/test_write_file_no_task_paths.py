"""Regression coverage for scoped no-task ``write_file`` dispatch."""

import json
from unittest.mock import MagicMock, patch

import agent.file_safety as file_safety


def _recording_registry(captured):
    registry = MagicMock()

    def dispatch(name, args, **kwargs):
        captured["name"] = name
        captured["args"] = args
        return json.dumps({"result": "ok"})

    registry.dispatch.side_effect = dispatch
    return registry


def _dispatch_write_file(captured, path):
    from model_tools import handle_function_call

    with patch("model_tools.registry", _recording_registry(captured)):
        return handle_function_call(
            "write_file",
            {"path": path, "content": "contents"},
            skip_pre_tool_call_hook=True,
        )


def test_no_task_write_file_dispatches_for_temp_path(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(file_safety, "is_write_denied_by_task_gate", lambda: True)

    result = _dispatch_write_file(captured, str(tmp_path / "scratch.txt"))

    assert json.loads(result) == {"result": "ok"}
    assert captured["name"] == "write_file"
    assert captured["args"]["path"] == str(tmp_path / "scratch.txt")


def test_no_task_write_file_dispatches_for_current_agent_journal(monkeypatch, tmp_path):
    captured = {}
    journal = tmp_path / "vault" / "Neo"
    monkeypatch.setattr(file_safety, "is_write_denied_by_task_gate", lambda: True)
    monkeypatch.setattr(file_safety, "_is_temp_path", lambda path: False)
    monkeypatch.setenv("VAULT_ROOT", str(journal.parent))
    monkeypatch.setenv("HERMES_AGENT_NAME", "Neo")

    result = _dispatch_write_file(captured, str(journal / "today.md"))

    assert json.loads(result) == {"result": "ok"}
    assert captured["name"] == "write_file"


def test_no_task_write_file_path_exemption_preserves_protected_paths(monkeypatch, tmp_path):
    protected = tmp_path / "state.db"
    monkeypatch.setattr(file_safety, "_hermes_home_path", lambda: tmp_path)
    monkeypatch.setattr(file_safety, "_hermes_root_path", lambda: tmp_path)

    assert file_safety.is_write_file_path_allowed_without_task(str(protected)) is False


def test_no_task_write_file_path_exemption_rejects_unrelated_path(monkeypatch, tmp_path):
    monkeypatch.setattr(file_safety, "_is_temp_path", lambda path: False)

    assert file_safety.is_write_file_path_allowed_without_task(
        str(tmp_path / "unrelated.txt")
    ) is False


def test_no_task_patch_stays_denied_even_for_eligible_write_file_path(monkeypatch):
    captured = {}
    from model_tools import handle_function_call

    monkeypatch.setattr(file_safety, "is_write_denied_by_task_gate", lambda: True)
    monkeypatch.setattr(
        file_safety, "is_write_file_path_allowed_without_task", lambda path: True
    )
    with patch("model_tools.registry", _recording_registry(captured)):
        result = handle_function_call(
            "patch",
            {"mode": "replace", "path": "/tmp/scratch.txt", "old_string": "a", "new_string": "b"},
            skip_pre_tool_call_hook=True,
        )

    assert captured == {}
    assert json.loads(result)["error"].startswith("Write denied: No active task.")
