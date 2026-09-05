"""Regression coverage for CLI-targeted out-of-band session notices."""

from __future__ import annotations

import argparse
from types import SimpleNamespace

import pytest

from hermes_cli import kanban, send_cmd
from hermes_state import SessionDB


def _send_args(target: str, message: str) -> argparse.Namespace:
    return argparse.Namespace(
        list_targets=False,
        to=None,
        user=target,
        message=message,
        file=None,
        subject=None,
        json=False,
        quiet=False,
    )


def test_session_notice_round_trip_and_cli_drain(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("cli-session", "cli")
    assert db.enqueue_session_notice("cli-session", "✅ task done", level="success")

    import cli

    cli_instance = object.__new__(cli.HermesCLI)
    cli_instance._session_db = db
    cli_instance.session_id = "cli-session"
    notices = []

    def receive_notice(notice):
        notices.append(notice)

    cli_instance._on_notice = receive_notice
    cli_instance._flush_credit_notices = lambda: None

    cli.HermesCLI._drain_session_notices(cli_instance)

    assert [(notice.text, notice.level) for notice in notices] == [
        ("✅ task done", "success")
    ]
    assert db.drain_session_notices("cli-session") == []
    db.close()


def test_send_cli_target_queues_notice_in_current_profile(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("cli-session", "cli")
    db.close()

    import hermes_constants

    monkeypatch.setattr(send_cmd, "_load_hermes_env", lambda: None)
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)

    with pytest.raises(SystemExit) as exc:
        send_cmd.cmd_send(_send_args("cli:cli-session", "kanban complete"))

    assert exc.value.code == 0
    reopened = SessionDB(db_path=tmp_path / "state.db")
    assert reopened.drain_session_notices("cli-session") == [
        {"text": "kanban complete", "level": "info"}
    ]
    reopened.close()


def test_kanban_cli_origin_queues_session_notice(tmp_path, monkeypatch):
    profile_home = tmp_path / "profiles" / "neo"
    profile_home.mkdir(parents=True)
    db = SessionDB(db_path=profile_home / "state.db")
    db.create_session("cli-session", "cli")
    db.close()

    class FakeConnection:
        def close(self):
            pass

    monkeypatch.setattr(kanban.kb, "connect", lambda: FakeConnection())
    monkeypatch.setattr(kanban.kb, "get_origin_routing", lambda *_args: None)
    monkeypatch.setattr(
        kanban.kb,
        "get_task",
        lambda *_args: SimpleNamespace(session_id="cli-session", created_by="neo"),
    )
    import hermes_cli.profiles

    monkeypatch.setattr(hermes_cli.profiles, "get_profile_dir", lambda _profile: profile_home)

    kanban._notify_kanban_status_change(
        "t_cli", "done", title="CLI task", summary="verified"
    )

    reopened = SessionDB(db_path=profile_home / "state.db")
    notices = reopened.drain_session_notices("cli-session")
    assert notices == [{"text": "✅ CLI task → done — verified", "level": "info"}]
    reopened.close()


def test_kanban_gateway_origin_keeps_platform_subprocess_path(monkeypatch):
    class FakeConnection:
        def close(self):
            pass

    calls = []
    monkeypatch.setattr(kanban.kb, "connect", lambda: FakeConnection())
    monkeypatch.setattr(
        kanban.kb,
        "get_origin_routing",
        lambda *_args: {
            "platform": "telegram",
            "chat_id": "123",
            "chat_type": "dm",
            "profile": "zephyr",
        },
    )
    monkeypatch.setattr(kanban.kb, "get_task", lambda *_args: None)
    monkeypatch.setattr(
        kanban,
        "_load_gateway_profile_env",
        lambda env: env.update({"HERMES_HOME": "gopher-home", "HERMES_PROFILE": "gopher"}),
    )
    monkeypatch.setattr(
        kanban,
        "_load_user_profile_env",
        lambda env, profile: env.update({
            "HERMES_HOME": f"{profile}-home",
            "HERMES_PROFILE": profile,
        }),
    )
    monkeypatch.setattr(
        "subprocess.run",
        lambda args, **kwargs: calls.append((args, kwargs)),
    )

    kanban._notify_kanban_status_change("t_gateway", "done", title="Gateway task")

    assert calls[0][0] == ["/home/ekl/bin/bugtool", "check"]
    assert calls[0][1]["timeout"] == 5
    assert calls[1][0][:4] == ["hermes", "send", "-t", "telegram:123"]
    assert calls[1][1]["env"]["HERMES_PROFILE"] == "gopher"
    assert calls[2][0][:4] == ["hermes", "send", "-u", "telegram:123"]
    assert calls[2][1]["env"]["HERMES_PROFILE"] == "zephyr"


def test_kanban_cli_notification_ignores_bugtool_oserror(monkeypatch):
    class FakeConnection:
        def close(self):
            pass

    calls = []

    def run(args, **kwargs):
        if args == ["/home/ekl/bin/bugtool", "check"]:
            raise OSError("bugtool unavailable")
        calls.append((args, kwargs))

    monkeypatch.setattr(kanban.kb, "connect", lambda: FakeConnection())
    monkeypatch.setattr(
        kanban.kb,
        "get_origin_routing",
        lambda *_args: {"platform": "telegram", "chat_id": "123", "chat_type": "dm"},
    )
    monkeypatch.setattr(kanban.kb, "get_task", lambda *_args: None)
    monkeypatch.setattr("subprocess.run", run)

    kanban._notify_kanban_status_change("t_gateway", "done", title="Gateway task")

    assert [call[0][2] for call in calls] == ["-t", "-u"]
