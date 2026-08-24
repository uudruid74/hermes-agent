"""Regression tests for no-plan terminal Bubblewrap policy."""

from types import SimpleNamespace

import agent.file_safety as file_safety


def _outside_hermes_roots(monkeypatch, tmp_path):
    hermes_root = "/opt/hermes-test-root"
    hermes_home = "/opt/hermes-test-root/profiles/neo"
    monkeypatch.setattr(file_safety, "_hermes_root_path", lambda: hermes_root)
    monkeypatch.setattr(file_safety, "_hermes_home_path", lambda: hermes_home)


def test_terminal_bubblewrap_root_allows_tmp_but_rejects_hermes_state_parent(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(file_safety, "_session_task_id", lambda: None)
    _outside_hermes_roots(monkeypatch, tmp_path)

    assert file_safety.terminal_bubblewrap_root(str(tmp_path)) == str(tmp_path)

    hermes_home = tmp_path / "state-inside-tmp"
    monkeypatch.setattr(file_safety, "_hermes_home_path", lambda: str(hermes_home))
    assert file_safety.terminal_bubblewrap_root(str(tmp_path)) is None


def test_terminal_bubblewrap_root_allows_only_this_agents_journal(monkeypatch, tmp_path):
    monkeypatch.setattr(file_safety, "_session_task_id", lambda: None)
    monkeypatch.setattr(file_safety, "_is_temp_path", lambda path: False)
    _outside_hermes_roots(monkeypatch, tmp_path)
    vault = tmp_path / "vault"
    journal_child = vault / "Neo" / "daily"
    journal_child.mkdir(parents=True)
    monkeypatch.setenv("VAULT_ROOT", str(vault))
    monkeypatch.setenv("HERMES_AGENT_NAME", "Neo")

    assert file_safety.terminal_bubblewrap_root(str(journal_child)) == str(journal_child)
    assert file_safety.terminal_bubblewrap_root(str(vault)) is None


def test_active_plan_keeps_terminal_unwrapped(monkeypatch, tmp_path):
    _outside_hermes_roots(monkeypatch, tmp_path)
    monkeypatch.setattr(file_safety, "_session_task_id", lambda: "t_active")

    assert file_safety.terminal_bubblewrap_root(str(tmp_path)) is None


def test_timeout_forces_project_root_into_bubblewrap(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(file_safety, "_session_task_id", lambda: None)
    _outside_hermes_roots(monkeypatch, tmp_path)
    monkeypatch.setattr(file_safety, "_plan_approval_timed_out", lambda: True)
    monkeypatch.setattr("agent.runtime_cwd.resolve_agent_cwd", lambda: str(project))

    assert file_safety.terminal_bubblewrap_root(None) == str(project)


def test_timeout_marker_reads_the_live_agent(monkeypatch):
    import agent.agent_runtime_helpers as runtime_helpers

    monkeypatch.setattr(
        runtime_helpers,
        "_current_agent",
        SimpleNamespace(_plan_approval_timed_out="t_timeout"),
    )

    assert file_safety._plan_approval_timed_out() is True
