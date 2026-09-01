from types import SimpleNamespace

from tools import kanban_tools


def test_worker_kanban_user_notification_uses_default_profile(monkeypatch):
    class FakeConnection:
        def close(self):
            pass

    calls = []
    monkeypatch.setattr(kanban_tools, "_load_gateway_profile_env", lambda env: env.update({"HERMES_PROFILE": "gopher"}))
    monkeypatch.setattr(kanban_tools, "_load_user_profile_env", lambda env: env.update({"HERMES_PROFILE": "default"}))
    monkeypatch.setattr("hermes_cli.kanban_db.connect", lambda: FakeConnection())
    monkeypatch.setattr(
        "hermes_cli.kanban_db.get_origin_routing",
        lambda *_args: {"platform": "buzz", "chat_id": "dm-id", "chat_type": "dm"},
    )
    monkeypatch.setattr("subprocess.run", lambda args, **kwargs: calls.append((args, kwargs)))

    kanban_tools._notify_kanban_event(
        "t_buzz", "done", "verified", SimpleNamespace(title="Buzz task", assignee="neo")
    )

    assert calls[0][0][:4] == ["hermes", "send", "-t", "buzz:dm-id"]
    assert calls[0][1]["env"]["HERMES_PROFILE"] == "gopher"
    assert calls[1][0][:4] == ["hermes", "send", "-u", "buzz:dm-id"]
    assert calls[1][1]["env"]["HERMES_PROFILE"] == "default"