from types import SimpleNamespace

from tools import kanban_tools


def test_worker_kanban_user_notification_uses_origin_profile(monkeypatch):
    class FakeConnection:
        def close(self):
            pass

    calls = []
    monkeypatch.setattr(kanban_tools, "_load_gateway_profile_env", lambda env: env.update({"HERMES_PROFILE": "gopher"}))
    monkeypatch.setattr(
        kanban_tools,
        "_load_user_profile_env",
        lambda env, profile: env.update({"HERMES_PROFILE": profile}),
    )
    monkeypatch.setattr("hermes_cli.kanban_db.connect", lambda: FakeConnection())
    monkeypatch.setattr(
        "hermes_cli.kanban_db.get_origin_routing",
        lambda *_args: {
            "platform": "buzz",
            "chat_id": "dm-id",
            "chat_type": "dm",
            "profile": "zephyr",
        },
    )
    monkeypatch.setattr("subprocess.run", lambda args, **kwargs: calls.append((args, kwargs)))

    kanban_tools._notify_kanban_event(
        "t_buzz", "done", "verified", SimpleNamespace(title="Buzz task", assignee="neo")
    )

    assert calls[0][0] == ["/home/ekl/bin/bugtool", "check"]
    assert calls[0][1]["timeout"] == 5
    assert calls[1][0][:4] == ["hermes", "send", "-t", "buzz:dm-id"]
    assert calls[1][1]["env"]["HERMES_PROFILE"] == "gopher"
    assert calls[2][0][:4] == ["hermes", "send", "-u", "buzz:dm-id"]
    assert calls[2][1]["env"]["HERMES_PROFILE"] == "zephyr"


def test_worker_legacy_origin_keeps_inherited_profile_env():
    env = {
        "HERMES_HOME": "/home/user/.hermes/profiles/neo",
        "HERMES_PROFILE": "neo",
    }

    kanban_tools._load_user_profile_env(env, None)

    assert env == {
        "HERMES_HOME": "/home/user/.hermes/profiles/neo",
        "HERMES_PROFILE": "neo",
    }


def test_worker_kanban_notification_ignores_bugtool_oserror(monkeypatch):
    class FakeConnection:
        def close(self):
            pass

    calls = []

    def run(args, **kwargs):
        if args == ["/home/ekl/bin/bugtool", "check"]:
            raise OSError("bugtool unavailable")
        calls.append((args, kwargs))

    monkeypatch.setattr("hermes_cli.kanban_db.connect", lambda: FakeConnection())
    monkeypatch.setattr(
        "hermes_cli.kanban_db.get_origin_routing",
        lambda *_args: {"platform": "buzz", "chat_id": "dm-id", "chat_type": "dm"},
    )
    monkeypatch.setattr("subprocess.run", run)

    kanban_tools._notify_kanban_event(
        "t_buzz", "done", None, SimpleNamespace(title="Buzz task", assignee="neo")
    )

    assert [call[0][2] for call in calls] == ["-t", "-u"]
