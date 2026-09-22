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


def test_kanban_identity_uses_username_only(monkeypatch):
    monkeypatch.setenv("USERNAME", "neo")
    monkeypatch.setenv("HERMES_PROFILE", "wrong-route")
    monkeypatch.setenv("USER", "wrong-login")

    assert kanban._profile_author() == "neo"


def test_cli_origin_routing_uses_username_only(monkeypatch):
    captured = {}
    monkeypatch.setenv("USERNAME", "neo")
    monkeypatch.setenv("HERMES_PROFILE", "wrong-route")
    monkeypatch.setenv("HERMES_SESSION_PROFILE", "wrong-session-route")
    monkeypatch.setattr(
        kanban.kb,
        "store_origin_routing",
        lambda _conn, _task_id, **kwargs: captured.update(kwargs),
    )

    kanban._store_cli_origin_routing(object(), "t_test", "telegram:123")

    assert captured["profile"] == "neo"
    assert captured["allow_non_session"] is True


def test_cli_implicit_origin_rejects_channel_fallback_without_session(
    monkeypatch, capsys
):
    calls = []
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "8900123006")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(
        kanban.kb,
        "store_origin_routing",
        lambda *_args, **kwargs: calls.append(kwargs),
    )

    kanban._store_cli_implicit_origin(object(), "t_test")

    assert calls == []
    assert "refusing implicit channel origin" in capsys.readouterr().err


def test_cli_implicit_origin_uses_durable_session_id(monkeypatch):
    captured = {}
    monkeypatch.setenv("USERNAME", "neo")
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "8900123006")
    monkeypatch.setenv("HERMES_SESSION_ID", "20260921_010203_abcdef")
    monkeypatch.setattr(
        kanban.kb,
        "store_origin_routing",
        lambda _conn, _task_id, **kwargs: captured.update(kwargs),
    )

    kanban._store_cli_implicit_origin(object(), "t_test")

    assert captured == {
        "platform": "session",
        "chat_id": "20260921_010203_abcdef",
        "profile": "neo",
    }


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


def test_origin_profile_env_overrides_inherited_telegram_identity(
    tmp_path, monkeypatch
):
    profile_home = tmp_path / "profiles" / "zephyr"
    profile_home.mkdir(parents=True)
    (profile_home / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=zephyr-token\n"
        "TELEGRAM_HOME_CHANNEL=123\n"
        "TELEGRAM_ALLOWED_USERS=456\n",
        encoding="utf-8",
    )
    import hermes_cli.profiles

    monkeypatch.setattr(
        hermes_cli.profiles, "get_profile_dir", lambda _profile: profile_home
    )
    env = {
        "HERMES_HOME": "/profiles/neo",
        "HERMES_PROFILE": "neo",
        "TELEGRAM_BOT_TOKEN": "neo-token",
        "TELEGRAM_HOME_CHANNEL": "999",
        "TELEGRAM_ALLOWED_USERS": "888",
    }

    kanban._load_user_profile_env(env, "zephyr")

    assert env["HERMES_HOME"] == str(profile_home)
    assert env["HERMES_PROFILE"] == "zephyr"
    assert env["TELEGRAM_BOT_TOKEN"] == "zephyr-token"
    assert env["TELEGRAM_HOME_CHANNEL"] == "123"
    assert env["TELEGRAM_ALLOWED_USERS"] == "456"


def test_kanban_wake_uses_origin_profile_bridge_and_identity(monkeypatch):
    import gateway.mcp_bridge
    import model_tools
    import tools.send_message_tool

    calls = []
    monkeypatch.setattr(
        gateway.mcp_bridge,
        "bridge_socket_path",
        lambda hermes_home=None: f"{hermes_home}/mcp.sock",
    )

    def send_via_bridge(platform, chat_id, payload, **kwargs):
        calls.append((platform.value, chat_id, payload, kwargs))
        return {"success": True, "queued": True}

    monkeypatch.setattr(tools.send_message_tool, "_send_via_bridge", send_via_bridge)
    monkeypatch.setattr(model_tools, "_run_async", lambda value: value)

    adapter, result = kanban._send_kanban_wake(
        target="telegram:123:456",
        platform="telegram",
        chat_id="123",
        thread_id="456",
        chat_type="forum",
        payload='{"source":"kanban"}',
        notify_env={
            "HERMES_HOME": "/profiles/zephyr",
            "HERMES_SESSION_USER_ID": "evan",
            "HERMES_SESSION_TELEGRAM_ID": "789",
            "HERMES_SESSION_USER_NAME": "Evan",
        },
    )

    assert adapter == "mcp_bridge:/profiles/zephyr/mcp.sock"
    assert result == {"success": True, "queued": True}
    assert calls == [
        (
            "telegram",
            "123",
            '{"source":"kanban"}',
            {
                "thread_id": "456",
                "user_context": {
                    "user_id": "evan",
                    "platform_user_id": "789",
                    "sender_name": "Evan",
                    "chat_type": "forum",
                },
                "bridge_path": "/profiles/zephyr/mcp.sock",
            },
        )
    ]


def test_kanban_human_notice_uses_origin_profile_bridge(monkeypatch):
    import gateway.mcp_bridge
    import model_tools
    import tools.send_message_tool

    calls = []
    monkeypatch.setattr(
        gateway.mcp_bridge,
        "bridge_socket_path",
        lambda hermes_home=None: f"{hermes_home}/mcp.sock",
    )

    def send_delivery(platform, chat_id, message, **kwargs):
        calls.append((platform.value, chat_id, message, kwargs))
        return {"success": True, "queued": True}

    monkeypatch.setattr(
        tools.send_message_tool,
        "_send_delivery_via_bridge",
        send_delivery,
    )
    monkeypatch.setattr(model_tools, "_run_async", lambda value: value)

    adapter, result = kanban._send_kanban_human_notification(
        platform="telegram",
        chat_id="123",
        thread_id="456",
        message="task done",
        notify_env={"HERMES_HOME": "/profiles/zephyr"},
    )

    assert adapter == "mcp_bridge:/profiles/zephyr/mcp.sock"
    assert result == {"success": True, "queued": True}
    assert calls == [
        (
            "telegram",
            "123",
            "task done",
            {
                "thread_id": "456",
                "bridge_path": "/profiles/zephyr/mcp.sock",
            },
        )
    ]


def test_kanban_gateway_origin_shares_profile_env_and_injects_wake(
    monkeypatch, caplog
):
    class FakeConnection:
        def close(self):
            pass

    human_calls = []
    wake_calls = []
    loader_calls = []

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

    def load_origin(env, profile):
        loader_calls.append(profile)
        env.update({
            "HERMES_HOME": f"{profile}-home",
            "HERMES_PROFILE": profile,
            "TELEGRAM_BOT_TOKEN": f"{profile}-token",
        })

    def send_wake(**kwargs):
        wake_calls.append(kwargs)
        return "mcp_bridge:/tmp/hermes/mcp_bridge.zephyr.sock", {
            "success": True,
            "queued": True,
        }

    def send_human(**kwargs):
        human_calls.append(kwargs)
        return "mcp_bridge:/tmp/hermes/mcp_bridge.zephyr.sock", {
            "success": True,
            "queued": True,
        }

    monkeypatch.setattr(kanban, "_load_user_profile_env", load_origin)
    monkeypatch.setattr(kanban, "_send_kanban_human_notification", send_human)
    monkeypatch.setattr(kanban, "_send_kanban_wake", send_wake)
    monkeypatch.setattr(
        "subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("kanban notification must not spawn subprocesses")
        ),
    )
    caplog.set_level("INFO", logger="hermes_cli.kanban")

    kanban._notify_kanban_status_change("t_gateway", "done", title="Gateway task")

    assert loader_calls == ["zephyr"]
    assert len(human_calls) == 1
    assert human_calls[0]["platform"] == "telegram"
    assert human_calls[0]["chat_id"] == "123"
    assert human_calls[0]["message"] == "✅ Gateway task → done"
    human_env = human_calls[0]["notify_env"]
    assert human_env["HERMES_PROFILE"] == "zephyr"
    assert human_env["TELEGRAM_BOT_TOKEN"] == "zephyr-token"

    assert len(wake_calls) == 1
    assert wake_calls[0]["target"] == "telegram:123"
    assert wake_calls[0]["notify_env"] is human_env
    assert "target=telegram:123" in caplog.text
    assert "adapter=mcp_bridge:/tmp/hermes/mcp_bridge.zephyr.sock" in caplog.text
    assert "result=success" in caplog.text


def test_kanban_failed_internal_wake_queues_session_notice(
    tmp_path, monkeypatch
):
    profile_home = tmp_path / "profiles" / "zephyr"
    profile_home.mkdir(parents=True)
    db = SessionDB(db_path=profile_home / "state.db")
    db.create_session("creator-session", "telegram")
    db.close()

    class FakeConnection:
        def close(self):
            pass

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
    monkeypatch.setattr(
        kanban.kb,
        "get_task",
        lambda *_args: SimpleNamespace(
            session_id="creator-session", created_by="zephyr"
        ),
    )
    monkeypatch.setattr(
        kanban,
        "_load_user_profile_env",
        lambda env, profile: env.update({
            "HERMES_HOME": str(profile_home),
            "HERMES_PROFILE": profile,
        }),
    )
    monkeypatch.setattr(
        kanban,
        "_send_kanban_human_notification",
        lambda **_kwargs: ("mcp_bridge", {"success": True, "queued": True}),
    )
    monkeypatch.setattr(
        kanban,
        "_send_kanban_wake",
        lambda **_kwargs: ("mcp_bridge", {"error": "gateway unavailable"}),
    )
    monkeypatch.setattr(
        "subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("kanban notification must not spawn subprocesses")
        ),
    )
    import hermes_cli.profiles

    monkeypatch.setattr(
        hermes_cli.profiles, "get_profile_dir", lambda _profile: profile_home
    )

    kanban._notify_kanban_status_change(
        "t_gateway", "done", title="Gateway task", summary="verified"
    )

    reopened = SessionDB(db_path=profile_home / "state.db")
    assert reopened.drain_session_notices("creator-session") == [
        {"text": "✅ Gateway task → done — verified", "level": "info"}
    ]
    reopened.close()
