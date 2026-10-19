"""Tests for GatewayKanbanStartupSyncMixin (gateway/kanban_startup_sync.py).

The polling watcher loops this module once held are DELETED (Evan,
2026-10-19 — "we don't use polling loops or watchers"). What remains is a
one-shot startup reality check; dispatch and notification delivery are
event-time (hermes_cli/kanban.py::kick_dispatch / _notify_kanban_status_change
→ hermes send -t). These tests pin the surface so the loops cannot come
back unnoticed.
"""

from __future__ import annotations

from gateway.kanban_startup_sync import GatewayKanbanStartupSyncMixin

KANBAN_METHODS = [
    "_kanban_startup_sync",
]

DELETED_METHODS = [
    "_kanban_notifier_watcher",
    "_kanban_dispatcher_watcher",
    "_kanban_advance",
    "_kanban_unsub",
    "_kanban_rewind",
    "_deliver_kanban_artifacts",
    "_owns_kanban_dispatcher_lock",
    "_release_kanban_dispatcher_lock",
]


def test_mixin_defines_startup_sync():
    for m in KANBAN_METHODS:
        assert hasattr(GatewayKanbanStartupSyncMixin, m), f"mixin missing {m}"


def test_polling_loops_are_gone():
    """No polling loops or watchers may return (Evan, 2026-10-19). Dispatch is
    event-time (kick_dispatch) plus ONE startup reality check — that's all."""
    for m in DELETED_METHODS:
        assert not hasattr(GatewayKanbanStartupSyncMixin, m), (
            f"{m} was reintroduced — dispatch/notification are event-time "
            "(_notify_kanban_status_change / kick_dispatch → hermes send -t); "
            "only the one-shot _kanban_startup_sync may exist"
        )