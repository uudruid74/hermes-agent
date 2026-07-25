"""Tests for kanban origin routing.

The kanban_notify_subs subscription system has been removed. Origin routing
is now stored as a system comment on the task via store_origin_routing() and
retrieved via get_origin_routing(). The gateway watcher polls active tasks
with origin routing comments instead of maintaining a subscription table.

Tests in this file cover the origin routing mechanism.
"""
