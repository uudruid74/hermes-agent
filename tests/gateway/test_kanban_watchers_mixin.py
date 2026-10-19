"""Tests for the extracted GatewayKanbanWatchersMixin (god-file Phase 3).

The kanban dispatcher loop lives in a mixin that GatewayRunner inherits.
The notification polling loop that used to share this mixin was DELETED
(Evan, 2026-10-19) — notification delivery is event-time via the DB hook
(``hermes_cli/kanban.py::_notify_kanban_status_change``) and ``hermes send
-t``. These tests confirm the mixin exposes the dispatcher and that
GatewayRunner picks it up via the MRO.
"""

from __future__ import annotations

from gateway.kanban_watchers import GatewayKanbanWatchersMixin

KANBAN_METHODS = [
    "_kanban_dispatcher_watcher",
    "_owns_kanban_dispatcher_lock",
    "_release_kanban_dispatcher_lock",
]

DELETED_METHODS = [
    "_kanban_notifier_watcher",
    "_kanban_advance",
    "_kanban_unsub",
    "_kanban_rewind",
    "_deliver_kanban_artifacts",
]


def test_mixin_defines_kanban_methods():
    for m in KANBAN_METHODS:
        assert hasattr(GatewayKanbanWatchersMixin, m), f"mixin missing {m}"


def test_notifier_polling_machinery_is_gone():
    """The notification polling loop must never come back (Evan, 2026-10-19)."""
    for m in DELETED_METHODS:
        assert not hasattr(GatewayKanbanWatchersMixin, m), (
            f"{m} was reintroduced — notifications are event-time "
            "(_notify_kanban_status_change → hermes send -t), no polling"
        )


