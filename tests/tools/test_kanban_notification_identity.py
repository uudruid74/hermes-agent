from types import SimpleNamespace

from tools import kanban_tools


def test_worker_only_event_uses_shared_kanban_notifier(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "hermes_cli.kanban._notify_kanban_status_change",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    kanban_tools._notify_kanban_event(
        "t_buzz",
        "commented",
        "new context",
        SimpleNamespace(title="Buzz task", assignee="neo"),
    )

    assert calls == [
        (
            ("t_buzz", "commented"),
            {
                "summary": "new context",
                "title": "Buzz task",
                "assignee": "neo",
            },
        )
    ]
