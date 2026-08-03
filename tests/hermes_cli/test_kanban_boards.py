"""Compatibility guarantees for the unified single Kanban database."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def fresh_home(tmp_path: Path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb._INITIALIZED_PATHS.clear()
    yield home
    kb._INITIALIZED_PATHS.clear()


def test_all_board_aliases_resolve_to_one_database(fresh_home):
    expected = fresh_home / "kanban.db"
    assert kb.kanban_db_path() == expected
    assert kb.kanban_db_path("default") == expected
    assert kb.kanban_db_path("legacy-name") == expected


def test_workspace_attachment_and_log_paths_are_flat(fresh_home):
    assert kb.workspaces_root("ignored") == fresh_home / "kanban" / "workspaces"
    assert kb.attachments_root("ignored") == fresh_home / "kanban" / "attachments"
    assert kb.worker_logs_dir("ignored") == fresh_home / "kanban" / "logs"


def test_legacy_current_file_and_env_cannot_switch_storage(fresh_home, monkeypatch):
    current = kb.current_board_path()
    current.parent.mkdir(parents=True, exist_ok=True)
    current.write_text("old-board\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "another-board")

    assert kb.get_current_board() == "default"
    assert kb.kanban_db_path() == fresh_home / "kanban.db"


def test_only_default_compatibility_board_exists(fresh_home):
    assert kb.board_exists()
    assert kb.board_exists("default")
    assert not kb.board_exists("named")
    assert [entry["slug"] for entry in kb.list_boards()] == ["default"]


def test_named_board_create_switch_remove_are_rejected(fresh_home):
    with pytest.raises(ValueError, match="named boards"):
        kb.create_board("named")
    with pytest.raises(ValueError, match="only the 'default'"):
        kb.set_current_board("named")
    with pytest.raises(ValueError, match="cannot be removed"):
        kb.remove_board("named")


def test_default_compatibility_calls_are_safe(fresh_home):
    metadata = kb.create_board("default")
    assert metadata["slug"] == "default"
    kb.set_current_board("default")
    assert kb.get_current_board() == "default"
    with pytest.raises(ValueError, match="cannot be removed"):
        kb.remove_board("default")


def test_board_argument_does_not_partition_rows(fresh_home):
    with kb.connect(board="alpha") as conn:
        task_id = kb.create_task(conn, title="shared", assignee="neo")
    with kb.connect(board="beta") as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.title == "shared"


def test_schema_contains_no_notification_subscription_table(fresh_home):
    with kb.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert "kanban_notify_subs" not in tables
