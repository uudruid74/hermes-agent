"""Close-notification contract for the single Kanban database."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_notify


def test_origin_route_invokes_one_internal_send(monkeypatch):
    calls = []
    monkeypatch.delenv("HERMES_KANBAN_NOTIFY_PROFILE", raising=False)

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr(kanban_notify.subprocess, "run", fake_run)

    assert kanban_notify.notify_task_closed(
        "t_12345678",
        "done",
        title="Ship it",
        summary="first line\nnot sent",
        origin={
            "platform": "telegram",
            "chat_id": "-10042",
            "thread_id": "77",
            "chat_type": "group",
        },
    )

    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[:5] == [
        "hermes", "send", "-u", "telegram:-10042:77", command[4]
    ]
    payload = json.loads(command[4])
    assert payload == {
        "source": "kanban",
        "type": "done",
        "task_id": "t_12345678",
        "title": "Ship it",
        "summary": "first line",
    }
    assert kwargs["env"]["HERMES_NOTIFY_CHAT_TYPE"] == "group"


def test_no_origin_routes_to_telegram_home(monkeypatch):
    calls = []
    monkeypatch.delenv("HERMES_KANBAN_NOTIFY_PROFILE", raising=False)

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setenv("HERMES_NOTIFY_CHAT_TYPE", "stale")
    monkeypatch.setattr(kanban_notify.subprocess, "run", fake_run)

    assert kanban_notify.notify_task_closed("t_cli0001", "blocked", summary="needs input")

    command, kwargs = calls[0]
    assert command[:4] == ["hermes", "send", "-u", "telegram"]
    assert "HERMES_NOTIFY_CHAT_TYPE" not in kwargs["env"]


def test_notify_uses_dispatching_gateway_profile(monkeypatch):
    calls = []
    monkeypatch.setenv("HERMES_KANBAN_NOTIFY_PROFILE", "gopher")
    monkeypatch.setattr(
        kanban_notify.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs))
        or subprocess.CompletedProcess(command, 0),
    )

    assert kanban_notify.notify_task_closed("t_profile", "done")
    assert calls[0][0][:6] == [
        "hermes", "-p", "gopher", "send", "-u", "telegram"
    ]


def test_nonterminal_and_failed_delivery_are_best_effort(monkeypatch):
    monkeypatch.setattr(
        kanban_notify.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 7, "", "offline"),
    )

    assert not kanban_notify.notify_task_closed("t_1", "running")
    assert not kanban_notify.notify_task_closed("t_1", "done")


def test_complete_notifies_once_after_commit(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    delivered = []

    def fake_notify(task_id, status, **kwargs):
        with kb.connect() as observer:
            assert kb.get_task(observer, task_id).status == "done"
        delivered.append((task_id, status, kwargs))
        return True

    monkeypatch.setattr(kb, "_notify_closed_after_commit", fake_notify)

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="Durable close", assignee="neo")
        kb.store_origin_routing(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="home-42",
            thread_id="9",
            chat_type="group",
        )
        assert kb.complete_task(conn, task_id, summary="verified")

    assert len(delivered) == 1
    assert delivered[0][0:2] == (task_id, "done")
    assert delivered[0][2]["title"] == "Durable close"
    assert delivered[0][2]["origin"]["thread_id"] == "9"


def test_block_notifies_only_when_status_lands_blocked(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    delivered = []
    monkeypatch.setattr(
        kb,
        "_notify_closed_after_commit",
        lambda task_id, status, **kwargs: delivered.append((task_id, status)) or True,
    )

    with kb.connect() as conn:
        blocked_id = kb.create_task(conn, title="Human gate", assignee="neo")
        assert kb.claim_task(conn, blocked_id)
        blocked_run = kb.get_task(conn, blocked_id).current_run_id
        assert kb.block_task(
            conn,
            blocked_id,
            reason="review-required: inspect",
            expected_run_id=blocked_run,
        )

        dependency_id = kb.create_task(conn, title="Wait", assignee="neo")
        assert kb.claim_task(conn, dependency_id)
        dependency_run = kb.get_task(conn, dependency_id).current_run_id
        assert kb.block_task(
            conn,
            dependency_id,
            reason="waiting for parent",
            kind="dependency",
            expected_run_id=dependency_run,
        )
        assert kb.get_task(conn, dependency_id).status == "todo"

    assert delivered == [(blocked_id, "blocked")]


def test_board_aliases_share_one_db_and_schema_has_no_subscription_table(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))

    assert kb.kanban_db_path("alpha") == db_path
    assert kb.kanban_db_path("beta") == db_path
    with kb.connect(board="alpha") as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert "kanban_notify_subs" not in tables


def test_internal_bare_platform_resolves_home_channel(monkeypatch):
    from gateway.config import HomeChannel, Platform
    from tools import send_message_tool as send_tool

    home = HomeChannel(
        platform=Platform.TELEGRAM,
        chat_id="home-42",
        name="Home",
        thread_id="77",
        chat_type="group",
    )
    pconfig = SimpleNamespace(enabled=True, home_channel=home)
    config = SimpleNamespace(
        platforms={Platform.TELEGRAM: pconfig},
        get_home_channel=lambda platform: home if platform == Platform.TELEGRAM else None,
    )
    recorded = {}

    async def fake_send(platform, platform_config, chat_id, message, **kwargs):
        recorded.update(
            platform=platform,
            platform_config=platform_config,
            chat_id=chat_id,
            message=message,
            kwargs=kwargs,
        )
        return {"success": True, "queued": True}

    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: config)
    monkeypatch.setattr(send_tool, "_send_via_adapter", fake_send)
    monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False)

    result = json.loads(
        send_tool.send_message_tool(
            {
                "action": "send",
                "target": "telegram",
                "message": "wake",
                "internal": True,
            }
        )
    )

    assert result == {"success": True, "queued": True}
    assert recorded["platform"] == Platform.TELEGRAM
    assert recorded["chat_id"] == "home-42"
    assert recorded["kwargs"]["thread_id"] == "77"
    assert recorded["kwargs"]["user_context"]["chat_type"] == "group"
