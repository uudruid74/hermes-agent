"""Kanban board watcher methods for GatewayRunner.

Extracted verbatim from ``gateway/run.py`` (god-file decomposition Phase 3).
These are the background-loop methods that subscribe to kanban boards, deliver
notifications/artifacts, and drive the multi-agent dispatcher. They use only
``self`` state, so they live on a mixin that ``GatewayRunner`` inherits — the
``self._kanban_*`` call sites resolve identically via the MRO, making this a
behavior-neutral move that lifts ~1,000 LOC out of run.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from agent.i18n import t

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource

# Match the logger run.py uses (logging.getLogger(__name__) where __name__ ==
# "gateway.run") so extracted log records keep their original logger name.
logger = logging.getLogger("gateway.run")


def _resolve_auto_decompose_settings(
    load_config: Callable[[], Any],
) -> "tuple[bool, int]":
    """Resolve the live (enabled, per_tick) auto-decompose settings.

    Read fresh from config on every dispatcher tick (#49638) so that flipping
    ``kanban.auto_decompose: false`` to STOP runaway fan-out takes effect on the
    next tick instead of requiring a gateway restart. Auto-decompose is a
    safety toggle — a user who sees it create and launch tasks they didn't
    intend reaches for this flag to halt it, and a stale boot-captured value
    silently ignoring that change is the bug reported in #49638.

    Fails **safe**: if the config read raises, return ``(False, 3)`` — a
    transient read error must never re-enable a feature the user turned off,
    nor fall back to the burst-prone default-on behaviour. ``per_tick`` is
    clamped to ``>= 1``.
    """
    try:
        cfg = load_config()
    except Exception:
        return False, 3
    kcfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    enabled = bool(kcfg.get("auto_decompose", True))
    try:
        per_tick = int(kcfg.get("auto_decompose_per_tick", 3) or 3)
    except (TypeError, ValueError):
        per_tick = 3
    if per_tick < 1:
        per_tick = 1
    return enabled, per_tick


def _acquire_singleton_lock(lock_path) -> "tuple[Optional[object], str]":
    """Take an exclusive, non-blocking advisory lock for the sole dispatcher.

    Only one gateway process machine-wide may run the embedded kanban
    dispatcher: concurrent dispatchers double the reclaim frequency (each
    runs its own ``release_stale_claims`` → promote → dispatch loop), double
    claim-attempt events in the event log, and — with ``wal_autocheckpoint=0`` —
    concurrent manual WAL checkpoints can corrupt index pages. The
    ``dispatch_in_gateway`` config flag is the primary control; this lock is the
    backstop that survives config drift and same-profile restart races.

    Delegates to :func:`gateway.status._try_acquire_file_lock` (``fcntl`` on
    POSIX, ``msvcrt`` on Windows) so the guard is cross-platform.

    Returns ``(handle, "held")`` on success — the caller keeps the file handle
    for the process lifetime and **must** release it via
    :func:`_release_singleton_lock` when done. ``(None, "contended")`` when
    another process holds the lock (caller must NOT dispatch). ``(None,
    "unavailable")`` when locking cannot be performed (non-POSIX filesystem
    without flock, or the status.py helpers are unimportable) — caller falls
    back to config-only control.
    """
    try:
        from gateway.status import _try_acquire_file_lock  # deferred; same package
    except ImportError:
        return None, "unavailable"
    try:
        Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
        handle = open(str(lock_path), "a+", encoding="utf-8")
    except OSError:
        return None, "unavailable"
    if not _try_acquire_file_lock(handle):
        handle.close()
        return None, "contended"
    return handle, "held"


def _release_singleton_lock(handle) -> None:
    """Release a dispatcher singleton lock acquired via :func:`_acquire_singleton_lock`."""
    if handle is None:
        return
    try:
        from gateway.status import _release_file_lock
        _release_file_lock(handle)
    except Exception:
        pass
    try:
        handle.close()
    except Exception:
        pass


class GatewayKanbanWatchersMixin:
    """Kanban watcher / notifier / dispatcher loops for GatewayRunner."""

    async def _kanban_notifier_watcher(self, interval: float = 5.0) -> None:
        """Poll active tasks with origin routing and deliver events.

        For each active task with a ``__kanban_origin__`` system comment,
        fetches ``task_events`` newer than the in-memory cursor and delivers
        them to the origin channel. Cursors are tracked in memory; on gateway
        restart, we start from the current max event id so nothing replays.

        Runs in the gateway event loop; all SQLite work is pushed to a
        thread via ``asyncio.to_thread`` so the loop never blocks on the
        WAL lock. Failures in one tick don't stop subsequent ticks.
        """
        # Gate: only the dispatch-owning gateway opens kanban DBs for notifier polling.
        # Non-dispatch gateways have no subscriptions to deliver — all kanban state lives
        # in the dispatch owner's per-board DBs. This prevents N-gateway -shm contention.
        # TODO: gate per-board when per-board dispatcher_owner tracking lands.
        try:
            from hermes_cli.config import load_config as _load_config
        except Exception:
            logger.warning("kanban notifier: config loader unavailable; disabled")
            return
        env_override = os.environ.get("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "").strip().lower()
        if env_override in {"0", "false", "no", "off"}:
            logger.info("kanban notifier: disabled via HERMES_KANBAN_DISPATCH_IN_GATEWAY env")
            return
        try:
            cfg = _load_config()
        except Exception as exc:
            logger.warning("kanban notifier: cannot load config (%s); disabled", exc)
            return
        kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
        if not kanban_cfg.get("dispatch_in_gateway", True):
            logger.info(
                "kanban notifier: disabled via config kanban.dispatch_in_gateway=false"
            )
            return
        _wake_home_session = bool(kanban_cfg.get("wake_home_session", False))
        from gateway.config import Platform as _Platform
        try:
            from hermes_cli import kanban_db as _kb
        except Exception:
            logger.warning("kanban notifier: kanban_db not importable; notifier disabled")
            return

        # Kinds that produce no user-facing notification.
        SILENT_KINDS = frozenset({
            "heartbeat", "archived", "unblocked", "ready",
            "assigned", "specified", "linked", "unlinked",
            "claim_rejected", "suspected_hallucinated_references",
        })
        # Subscriptions are removed only when the task reaches a truly final
        # status (done / archived). We used to also unsub on any terminal
        # event kind (gave_up / crashed / timed_out / blocked), but that
        # silently dropped the user out of the loop whenever the dispatcher
        # respawned the task: a worker that crashes, gets reclaimed, runs
        # again, and crashes a second time would only notify on the first
        # crash because the subscription was deleted after the first event.
        # Same shape as the reblock-after-unblock cycle that PR #22941
        # fixed for `blocked`. Keeping the subscription alive until the
        # task is genuinely done lets the cursor (advanced atomically by
        # claim_unseen_events_for_sub) handle dedup, and any retry-loop
        # event reaches the user.
        # Per-subscription send-failure counter. Adapter.send raising
        # means the chat is dead (deleted, bot kicked, etc.) — after N
        # consecutive send failures the sub is dropped so we don't spin
        # against a dead chat every 5 seconds forever.
        MAX_SEND_FAILURES = 3
        sub_fail_counts: dict[tuple, int] = getattr(
            self, "_kanban_sub_fail_counts", {}
        )
        self._kanban_sub_fail_counts = sub_fail_counts
        notifier_profile = getattr(self, "_kanban_notifier_profile", None)
        if not notifier_profile:
            notifier_profile = self._active_profile_name()
            self._kanban_notifier_profile = notifier_profile

        # Initial delay so the gateway can finish wiring adapters.
        await asyncio.sleep(5)

        logger.info("kanban notifier: starting poll loop")

        while self._running:
            try:
                def _collect():
                    deliveries: list[dict] = []
                    active_platforms = {
                        getattr(platform, "value", str(platform)).lower()
                        for platform in self.adapters.keys()
                    }
                    if not active_platforms:
                        logger.debug("kanban notifier: no connected adapters; skipping tick")
                        return deliveries

                    # Single flat kanban.db — open once per tick.
                    try:
                        conn = _kb.connect()
                    except Exception as exc:
                        logger.debug("kanban notifier: cannot open kanban DB: %s", exc)
                        return deliveries
                    try:
                        # In-memory per-task event cursor. Keys are task_id.
                        # On gateway restart we start from the current max event
                        # id so nothing gets replayed.
                        _task_cursors: dict[str, int] = getattr(
                            self, "_kanban_task_cursors", {}
                        )
                        self._kanban_task_cursors = _task_cursors

                        # Find all active tasks that have origin routing comments.
                        _active_rows = conn.execute(
                            "SELECT id FROM tasks WHERE status NOT IN ('done','archived')"
                        ).fetchall()
                        for (_tid,) in _active_rows:
                            _origin = _kb.get_origin_routing(conn, _tid)
                            if not _origin:
                                continue
                            platform = (_origin.get("platform") or "").lower()
                            if platform not in active_platforms:
                                continue
                            chat_id = _origin.get("chat_id") or ""
                            thread_id = _origin.get("thread_id") or ""

                            # Age gate: skip tasks created less than 15s ago
                            # to avoid racing with slash_commands.py's
                            # store_origin_routing.
                            _created_row = conn.execute(
                                "SELECT created_at FROM tasks WHERE id = ?", (_tid,)
                            ).fetchone()
                            if _created_row and _created_row[0]:
                                _age = int(time.time()) - _created_row[0]
                                if _age < 15:
                                    continue

                            cursor = _task_cursors.get(_tid, 0)
                            # Fetch events newer than cursor.
                            rows = conn.execute(
                                "SELECT * FROM task_events WHERE task_id = ? AND id > ?"
                                " ORDER BY id ASC",
                                (_tid, cursor),
                            ).fetchall()
                            if not rows:
                                continue

                            events: list = []
                            max_id = cursor
                            for r in rows:
                                try:
                                    payload = json.loads(r["payload"]) if r["payload"] else None
                                except Exception:
                                    payload = None
                                from hermes_cli.kanban_db import Event as _Event
                                events.append(_Event(
                                    id=r["id"], task_id=r["task_id"], kind=r["kind"],
                                    payload=payload, created_at=r["created_at"],
                                    run_id=(int(r["run_id"]) if r["run_id"] is not None else None),
                                ))
                                max_id = max(max_id, int(r["id"]))

                            task = _kb.get_task(conn, _tid)
                            # Build synthetic sub dict for the delivery section.
                            sub = {
                                "task_id": _tid,
                                "platform": platform,
                                "chat_id": chat_id,
                                "thread_id": thread_id,
                                "notifier_profile": notifier_profile,
                            }
                            deliveries.append({
                                "sub": sub,
                                "old_cursor": cursor,
                                "cursor": max_id,
                                "events": events,
                                "task": task,
                                "board": None,  # flat DB, no boards
                            })
                            logger.info(
                                "kanban notifier: claimed %d event(s) for %s cursor %s→%s",
                                len(events), _tid, cursor, max_id,
                            )
                    finally:
                        conn.close()
                    return deliveries

                deliveries = await asyncio.to_thread(_collect)
                for d in deliveries:
                    sub = d["sub"]
                    task = d["task"]
                    board_slug = d.get("board")
                    event_kinds = [e.kind for e in d.get("events", [])]
                    logger.info(
                        "kanban notifier: TRACE → delivery for %s on board %s: %d event(s) %s",
                        sub["task_id"], board_slug or "?", len(event_kinds), event_kinds,
                    )
                    platform_str = (sub["platform"] or "").lower()
                    try:
                        plat = _Platform(platform_str)
                    except ValueError:
                        await asyncio.to_thread(
                            self._kanban_advance, sub, d["cursor"], board_slug,
                        )
                        continue
                    sub_profile = sub.get("notifier_profile") or ""
                    _adapter_profile = sub_profile or None
                    if _adapter_profile and _adapter_profile == notifier_profile:
                        _adapter_profile = None
                    adapter = self._authorization_adapter(plat, _adapter_profile)
                    if adapter is None:
                        logger.debug(
                            "kanban notifier: adapter %s disconnected before delivery for %s; rewinding claim",
                            platform_str, sub["task_id"],
                        )
                        await asyncio.to_thread(
                            self._kanban_rewind,
                            sub,
                            d["cursor"],
                            d.get("old_cursor", 0),
                            board_slug,
                        )
                        continue
                    # Typing indicator for kanban worker events removed.
                    # Keep _is_terminal for unsub logic below.
                    _task_status = getattr(task, "status", None)
                    _is_terminal = _task_status in ("done", "archived")
                    title = (task.title if task else sub["task_id"])[:120]
                    board_tag = f"[{board_slug}] " if board_slug else ""
                    # Resolve delivery coordinates with origin routing for BOTH
                    # the direct notification and the wakeup notification below.
                    _delivery_chat_id = sub["chat_id"]
                    _delivery_thread_id = sub.get("thread_id") or ""
                    for ev in d["events"]:
                        kind = ev.kind
                        # Silent kinds: claim advances the cursor but no
                        # notification is sent. This restores the pre-regression
                        # behavior where archived/unblocked were silently
                        # consumed, and extends it to informational kinds.
                        if kind in SILENT_KINDS:
                            logger.info(
                                "kanban notifier: SILENT skip %s/%s for %s (cursor %s)",
                                kind, board_tag, sub["task_id"], d["cursor"],
                            )
                            continue
                        # DB-hook-style format: emoji + id: title — status (summary)
                        if kind == "status" and ev.payload and ev.payload.get("status"):
                            status = str(ev.payload["status"])
                        else:
                            status = kind
                        _EMOJI = {
                            "todo": "⬜", "ready": "▶️", "running": "🔄",
                            "triage": "🔍", "scheduled": "⏳", "blocked": "🔴",
                            "done": "✅", "completed": "✅", "archived": "📦",
                            "gave_up": "✖", "crashed": "💥", "timed_out": "⏱",
                            "unblocked": "🔓", "created": "🆕", "claimed": "🔄",
                            "spawned": "🔄", "heartbeat": "", "status": "🔄",
                            "dependency_wait": "🔗", "block_loop_detected": "🔁",
                            "reclaimed": "🔂", "promoted": "⬆️",
                            "promoted_manual": "⬆️", "protocol_violation": "⚠️",
                            "commented": "💬",
                        }
                        emoji = _EMOJI.get(status, "❓")
                        parts = [f"[Hermes] A kanban task you created ({sub['task_id']}), assigned to {getattr(task, 'assignee', None) or 'unassigned'}, has changed status to {status} - {title}"]
                        summary = ""
                        if kind == "completed" and ev.payload and ev.payload.get("summary"):
                            summary = str(ev.payload["summary"])
                        elif task and task.result and kind == "completed":
                            summary = task.result
                        elif kind in ("blocked", "dependency_wait", "block_loop_detected"):
                            if ev.payload and ev.payload.get("reason"):
                                summary = str(ev.payload["reason"])
                        elif kind == "commented" and ev.payload and ev.payload.get("body"):
                            summary = str(ev.payload["body"])
                        elif kind == "protocol_violation" and ev.payload and ev.payload.get("error"):
                            summary = str(ev.payload["error"])
                        elif kind == "crashed" and ev.payload and ev.payload.get("error"):
                            summary = str(ev.payload["error"])
                        elif kind == "gave_up" and ev.payload and ev.payload.get("error"):
                            summary = str(ev.payload["error"])
                        elif kind == "timed_out" and ev.payload and ev.payload.get("limit_seconds"):
                            summary = f"max_runtime={ev.payload['limit_seconds']}s"
                        if summary:
                            first_line = summary.splitlines()[0][:300]
                            parts.append(f" ({first_line})")
                        msg = "".join(parts)
                        metadata: dict[str, Any] = {}
                        if sub.get("thread_id"):
                            metadata["thread_id"] = sub["thread_id"]
                        sub_key = (
                            sub["task_id"], sub["platform"],
                            sub["chat_id"], sub.get("thread_id") or "",
                        )
                        try:
                            # Route ALL kanban status changes through
                            # handle_message() so the agent wakes up and
                            # processes them as user input — enabling
                            # cross-agent communication and context-aware
                            # reactions. Falls back to adapter.send() on
                            # failure as a safety net.
                            try:
                                # Resolve delivery coordinates. Origin routing
                                # (stored as a system comment by
                                # slash_commands.py on create) is the ground
                                # truth -- it overrides whatever the
                                # subscription says so that tasks created
                                # from a topic always route back to that
                                # topic, regardless of subscription state.
                                _origin = None  # pre-bind for type checker; set inside try
                                try:
                                    from hermes_cli import kanban_db as _kb2
                                    _origin_conn = _kb2.connect(board=board_slug)
                                    try:
                                        _origin = _kb2.get_origin_routing(_origin_conn, sub["task_id"])
                                        if _origin and _origin.get("platform") == platform_str:
                                            _delivery_chat_id = _origin["chat_id"] or _delivery_chat_id
                                            _delivery_thread_id = _origin.get("thread_id") or _delivery_thread_id
                                            logger.debug(
                                                "kanban notifier: using origin routing for %s -> %s/%s/%s",
                                                sub["task_id"], platform_str,
                                                _delivery_chat_id, _delivery_thread_id,
                                            )
                                    finally:
                                        _origin_conn.close()
                                except Exception:
                                    pass  # origin routing is best-effort
                                # Infer chat_type. The origin routing comment
                                # (stored by slash_commands.py on /kanban
                                # create) carries the true chat_type from the
                                # message source and is the ground truth.
                                #
                                # Without origin routing, we must infer:
                                # - Telegram DM topics: chat_type="dm" with
                                #   thread_id set (NOT "group"). A "group"
                                #   inference here breaks DM-topic session
                                #   keys and routes notifications to the
                                #   wrong session.
                                # - Forum/supergroup topics: chat_type="group"
                                #   or "forum" with thread_id.
                                # - Non-threaded deliveries: resolve from
                                #   home channel config, fall back "group".
                                _origin_chat_type = (_origin.get("chat_type") or "").strip() if _origin else ""
                                if _origin_chat_type:
                                    _sub_chat_type = _origin_chat_type
                                    logger.info(
                                        "kanban notifier: chat_type from origin routing for %s: %s",
                                        sub["task_id"], _sub_chat_type,
                                    )
                                elif _delivery_thread_id.strip():
                                    # Threaded delivery, unknown chat_type.
                                    # For Telegram, check whether this is a
                                    # DM topic (chat_type="dm") rather than
                                    # a forum/supergroup topic.
                                    _sub_chat_type = "group"
                                    if platform_str == "telegram":
                                        try:
                                            # Check adapter for DM topic info
                                            _tg_adapter = getattr(self, "_authorization_adapter", None)
                                            if _tg_adapter is not None:
                                                _tg_adapter = _tg_adapter(plat)
                                            if _tg_adapter and hasattr(type(_tg_adapter), "_get_dm_topic_info"):
                                                _topic_info = _tg_adapter._get_dm_topic_info(
                                                    _delivery_chat_id, _delivery_thread_id
                                                )
                                                if isinstance(_topic_info, dict):
                                                    _sub_chat_type = "dm"
                                                    logger.info(
                                                        "kanban notifier: DM topic detected via adapter for %s: %s/%s",
                                                        sub["task_id"], _delivery_chat_id, _delivery_thread_id,
                                                    )
                                        except Exception as _dm_exc:
                                            logger.debug(
                                                "kanban notifier: DM topic check failed for %s: %s",
                                                sub["task_id"], _dm_exc,
                                            )
                                        # Also check via is_telegram_dm_topic_target (handles
                                        # operator-declared topics that are not in the
                                        # adapter cache yet)
                                        if _sub_chat_type != "dm":
                                            try:
                                                _check = getattr(self, "_is_telegram_dm_topic_target", None)
                                                if _check and _check(
                                                    plat, _delivery_chat_id, _delivery_thread_id,
                                                    chat_type="dm",
                                                ):
                                                    _sub_chat_type = "dm"
                                                    logger.info(
                                                        "kanban notifier: DM topic via _is_telegram_dm_topic_target for %s",
                                                        sub["task_id"],
                                                    )
                                            except Exception:
                                                pass
                                    logger.info(
                                        "kanban notifier: inferred chat_type for threaded %s: %s "
                                        "(platform=%s thread=%s)",
                                        sub["task_id"], _sub_chat_type, platform_str,
                                        _delivery_thread_id,
                                    )
                                else:
                                    _home_ch = self.config.get_home_channel(plat) if hasattr(self, "config") and self.config else None
                                    _sub_chat_type = (_home_ch.chat_type) if _home_ch and getattr(_home_ch, "chat_type", None) else "group"
                                    logger.info(
                                        "kanban notifier: chat_type from home channel for %s: %s "
                                        "(home=%s)",
                                        sub["task_id"], _sub_chat_type,
                                        _home_ch.chat_type if _home_ch else "none",
                                    )
                                session_source = SessionSource(
                                    platform=Platform(sub["platform"]),
                                    chat_id=_delivery_chat_id,
                                    thread_id=_delivery_thread_id or None,
                                    chat_type=_sub_chat_type,
                                    user_id=sub.get("user_id"),
                                    profile=sub.get("notifier_profile"),
                                )
                                notify_event = MessageEvent(
                                    text=msg,
                                    source=session_source,
                                    message_type=MessageType.TEXT,
                                    internal=False,
                                    timestamp=datetime.now(),
                                )
                                logger.info(
                                    "kanban notifier: TRACE → handle_message for %s: [%s] → %s/%s",
                                    sub["task_id"], msg[:80], platform_str, sub["chat_id"],
                                )
                                await adapter.handle_message(notify_event)
                                logger.info(
                                    "kanban notifier: pushed %s event for %s to agent via handle_message "
                                    "(%s/%s) on board %s",
                                    kind, sub["task_id"], platform_str,
                                    sub["chat_id"], board_slug,
                                )
                            except Exception as _exc:
                                logger.warning(
                                    "kanban notifier: handle_message failed for %s (%s); "
                                    "falling back to adapter.send",
                                    sub["task_id"], _exc,
                                )
                                logger.info(
                                    "kanban notifier: TRACE → adapter.send fallback for %s: [%s] → %s/%s",
                                    sub["task_id"], msg[:80], platform_str, sub["chat_id"],
                                )
                                await adapter.send(
                                    sub["chat_id"], msg,
                                    metadata=metadata,
                                )
                                logger.info(
                                    "kanban notifier: TRACE → adapter.send completed for %s",
                                    sub["task_id"],
                                )
                            logger.debug(
                                "kanban notifier: delivered %s event for %s to %s/%s on board %s",
                                kind, sub["task_id"], platform_str, sub["chat_id"], board_slug,
                            )
                            # After delivering the text notification, surface
                            # any artifact paths the worker referenced in
                            # ``kanban_complete(summary=..., artifacts=[...])``
                            # (or the legacy ``result`` field) as native
                            # uploads. ``extract_local_files`` finds bare
                            # absolute paths in the summary;
                            # ``send_document`` / ``send_image_file`` uploads
                            # them. Only fires on the ``completed`` event so
                            # we never spam attachments on retries.
                            if kind == "completed":
                                try:
                                    await self._deliver_kanban_artifacts(
                                        adapter=adapter,
                                        chat_id=sub["chat_id"],
                                        metadata=metadata,
                                        event_payload=getattr(ev, "payload", None),
                                        task=task,
                                    )
                                except Exception as art_exc:
                                    logger.debug(
                                        "kanban notifier: artifact delivery for %s failed: %s",
                                        sub["task_id"], art_exc,
                                    )
                            # Reset the failure counter on success.
                            sub_fail_counts.pop(sub_key, None)
                        except Exception as exc:
                            fails = sub_fail_counts.get(sub_key, 0) + 1
                            sub_fail_counts[sub_key] = fails
                            logger.warning(
                                "kanban notifier: send failed for %s on %s "
                                "(attempt %d/%d): %s",
                                sub["task_id"], platform_str, fails,
                                MAX_SEND_FAILURES, exc,
                            )
                            # Rewind the cursor so events are retried next
                            # poll instead of being permanently dropped.
                            await asyncio.to_thread(
                                self._kanban_rewind,
                                sub,
                                d["cursor"],
                                d.get("old_cursor", 0),
                                board_slug,
                            )
                            if fails >= MAX_SEND_FAILURES:
                                logger.warning(
                                    "kanban notifier: dropping subscription "
                                    "%s on %s after %d consecutive send failures",
                                    sub["task_id"], platform_str, fails,
                                )
                                await asyncio.to_thread(self._kanban_unsub, sub, board_slug)
                                sub_fail_counts.pop(sub_key, None)
                            else:
                                await asyncio.to_thread(
                                    self._kanban_rewind,
                                    sub,
                                    d["cursor"],
                                    d.get("old_cursor", 0),
                                    board_slug,
                                )
                            # Rewind the pre-send claim on transient failure so
                            # a later tick can retry. After too many failures,
                            # dropping the subscription is the terminal action.
                            break
                    else:
                        # All events delivered; advance cursor. The cursor
                        # is the dedup mechanism — it prevents re-delivery
                        # of the same event on subsequent ticks.
                        await asyncio.to_thread(
                            self._kanban_advance, sub, d["cursor"], board_slug,
                        )
                        # Unsubscribe only when the task has reached a truly
                        # final status (done / archived). For blocked /
                        # gave_up / crashed / timed_out the subscription is
                        # kept alive so the user gets notified again if the
                        # dispatcher respawns the task and it cycles into the
                        # same state. See the longer comment on TERMINAL_KINDS
                        # above for the failure mode this prevents.
                        task_terminal = task and task.status in {"done", "archived"}
                        _WAKE_KINDS = ("completed", "gave_up", "crashed", "timed_out", "blocked",
                                      "commented", "protocol_violation", "claimed")
                        _wake_kinds = {ev.kind for ev in d["events"] if ev.kind in _WAKE_KINDS}
                        if _wake_kinds:
                            try:
                                _session_key = getattr(task, "session_id", None) or ""
                                if _session_key:
                                    _title = (task.title if task else sub["task_id"])[:120]
                                    _assignee = task.assignee if task else ""
                                    _parts = []
                                    if "completed" in _wake_kinds: _parts.append(t("gateway.kanban.wake.completed"))
                                    if "gave_up" in _wake_kinds: _parts.append(t("gateway.kanban.wake.gave_up"))
                                    if "crashed" in _wake_kinds: _parts.append(t("gateway.kanban.wake.crashed"))
                                    if "timed_out" in _wake_kinds: _parts.append(t("gateway.kanban.wake.timed_out"))
                                    if "blocked" in _wake_kinds: _parts.append(t("gateway.kanban.wake.blocked"))
                                    if "claimed" in _wake_kinds: _parts.append(t("gateway.kanban.wake.claimed"))
                                    _status = t("gateway.kanban.wake.status_joiner").join(_parts) or t("gateway.kanban.wake.status_default")
                                    _synth = t(
                                        "gateway.kanban.wake.message",
                                        task_id=sub["task_id"],
                                        status=_status,
                                        title=_title,
                                        assignee=_assignee,
                                        board=board_slug,
                                    )
                                    # Resolve chat_type from the home channel config
                                    # so the session key matches the user's actual
                                    # session.  Falls back to "group" when the home
                                    # channel isn't configured.
                                    _wake_chat_type = _sub_chat_type  # reuse resolution from primary notification
                                    _source = SessionSource(
                                        platform=plat,
                                        chat_id=_delivery_chat_id,
                                        chat_type=_wake_chat_type,
                                        thread_id=_delivery_thread_id or None,
                                        user_id=sub.get("user_id"),
                                        profile=sub_profile or None,
                                    )
                                    _synth_event = MessageEvent(
                                        text=_synth,
                                        message_type=MessageType.TEXT,
                                        source=_source,
                                        internal=True,
                                    )
                                    await adapter.handle_message(_synth_event)
                                    # HOME session wake: when
                                    # kanban.wake_home_session is true, also
                                    # inject a wake event into the creator's
                                    # HOME session so they are woken in their
                                    # primary conversation channel. Best-effort
                                    # — a missing or unreachable HOME channel
                                    # is logged at debug and skipped; the
                                    # subscription channel wake above is
                                    # unaffected.
                                    if _wake_home_session:
                                        try:
                                            _home_ch = self.config.get_home_channel(plat)
                                        except Exception:
                                            _home_ch = None
                                        if _home_ch and _home_ch.chat_id:
                                            try:
                                                _home_source = SessionSource(
                                                    platform=plat,
                                                    chat_id=_home_ch.chat_id,
                                                    chat_type=getattr(_home_ch, "chat_type", None) or "group",
                                                    thread_id=_home_ch.thread_id,
                                                    user_id=sub.get("user_id"),
                                                    profile=sub_profile or None,
                                                )
                                                _home_event = MessageEvent(
                                                    text=_synth,
                                                    message_type=MessageType.TEXT,
                                                    source=_home_source,
                                                    internal=True,
                                                )
                                                await adapter.handle_message(_home_event)
                                                logger.info(
                                                    "kanban notifier: woke HOME session for %s on %s/%s profile=%s events=%s",
                                                    sub["task_id"], platform_str, _home_ch.chat_id, sub_profile or "default", _wake_kinds,
                                                )
                                            except Exception as _home_err:
                                                logger.debug(
                                                    "kanban notifier: HOME session wake skipped for %s: %s",
                                                    sub["task_id"], _home_err,
                                                )
                                    logger.info(
                                        "kanban notifier: woke agent for %s on %s/%s profile=%s events=%s",
                                        sub["task_id"], platform_str, sub["chat_id"], sub_profile or "default", _wake_kinds,
                                    )
                            except Exception as _wk_err:
                                # Best-effort: the notification itself already
                                # delivered and the cursor has advanced, so a
                                # broken wake path must not wedge the tick — but
                                # log at WARNING with a traceback rather than
                                # DEBUG so a persistently-failing wake is visible
                                # in normal logs instead of silently no-op'ing.
                                logger.warning(
                                    "kanban notifier: wakeup injection failed for %s: %s",
                                    sub["task_id"], _wk_err, exc_info=True,
                                )
                        if task_terminal:
                            await asyncio.to_thread(
                                self._kanban_unsub, sub, board_slug,
                            )
            except Exception as exc:
                logger.warning("kanban notifier tick failed: %s", exc)
            # Sleep with cancellation checks.
            for _ in range(int(max(1, interval))):
                if not self._running:
                    return
                await asyncio.sleep(1)

    def _kanban_advance(
        self, sub: dict, cursor: int, board: Optional[str] = None,
    ) -> None:
        """Sync helper: advance in-memory cursor for a task's origin routing.

        With the flat kanban.db + origin routing, cursors are tracked
        in memory (``_kanban_task_cursors``). On gateway restart, we
        start from the current max event id so nothing replays.
        """
        _task_cursors: dict[str, int] = getattr(
            self, "_kanban_task_cursors", {}
        )
        self._kanban_task_cursors = _task_cursors
        _task_cursors[sub["task_id"]] = cursor

    def _kanban_unsub(self, sub: dict, board: Optional[str] = None) -> None:
        """No-op: origin routing is stored as a system comment on the task.

        There are no subscription rows to remove. The cursor is left in
        ``_kanban_task_cursors`` so completed/archived tasks don't replay
        events, but they are naturally excluded from the poll because
        ``_collect`` only queries active tasks.
        """

    def _kanban_rewind(
        self,
        sub: dict,
        claimed_cursor: int,
        old_cursor: int,
        board: Optional[str] = None,
    ) -> None:
        """Sync helper: reset in-memory cursor after send failure.

        When delivery fails (adapter disconnected, etc.), reset the cursor
        so the next tick retries the same events.
        """
        _task_cursors: dict[str, int] = getattr(
            self, "_kanban_task_cursors", {}
        )
        self._kanban_task_cursors = _task_cursors
        _task_cursors[sub["task_id"]] = old_cursor

    async def _deliver_kanban_artifacts(
        self,
        *,
        adapter,
        chat_id: str,
        metadata: dict,
        event_payload: Optional[dict],
        task,
    ) -> None:
        """Upload artifact files referenced by a completed kanban task.

        Workers passing ``kanban_complete(artifacts=[...])`` ship absolute
        file paths through the completion event so downstream humans get
        the deliverable as a native upload instead of a path printed in
        chat.

        Sources scanned, in priority order:
          1. ``event_payload['artifacts']`` (explicit list — preferred)
          2. ``event_payload['summary']`` (truncated first line)
          3. ``task.result`` (legacy fallback)

        Files are deduplicated, missing files are silently skipped (the
        path may have been mentioned for reference only), and delivery
        errors are logged but do not break the notifier loop.
        """
        from pathlib import Path as _Path

        candidates: list[str] = []
        seen: set[str] = set()

        def _add(path: str) -> None:
            if not path:
                return
            expanded = os.path.expanduser(path)
            if expanded in seen:
                return
            if not os.path.isfile(expanded):
                return
            seen.add(expanded)
            candidates.append(expanded)

        # 1. Explicit artifacts list in payload.
        if isinstance(event_payload, dict):
            raw = event_payload.get("artifacts")
            if isinstance(raw, (list, tuple)):
                for item in raw:
                    if isinstance(item, str):
                        _add(item)

            # 2. Paths embedded in the payload summary.
            summary = event_payload.get("summary")
            if isinstance(summary, str) and summary:
                paths, _ = adapter.extract_local_files(summary)
                for p in paths:
                    _add(p)

        # 3. Legacy: paths embedded in task.result.
        if task is not None and getattr(task, "result", None):
            result_text = str(task.result)
            paths, _ = adapter.extract_local_files(result_text)
            for p in paths:
                _add(p)

        if not candidates:
            return

        from gateway.platforms.base import BasePlatformAdapter
        candidates = BasePlatformAdapter.filter_local_delivery_paths(candidates)
        if not candidates:
            return

        _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
        _VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp"}

        from urllib.parse import quote as _quote

        # Partition images so they ride a single send_multiple_images call
        # on platforms that support batch image uploads (Signal/Slack RPCs).
        image_paths = [p for p in candidates if _Path(p).suffix.lower() in _IMAGE_EXTS]
        other_paths = [p for p in candidates if _Path(p).suffix.lower() not in _IMAGE_EXTS]

        if image_paths:
            try:
                batch = [(f"file://{_quote(p)}", "") for p in image_paths]
                await adapter.send_multiple_images(
                    chat_id=chat_id, images=batch, metadata=metadata,
                )
            except Exception as exc:
                logger.warning(
                    "kanban notifier: image batch upload failed: %s", exc,
                )

        for path in other_paths:
            ext = _Path(path).suffix.lower()
            try:
                if ext in _VIDEO_EXTS:
                    await adapter.send_video(
                        chat_id=chat_id, video_path=path, metadata=metadata,
                    )
                else:
                    await adapter.send_document(
                        chat_id=chat_id, file_path=path, metadata=metadata,
                    )
            except Exception as exc:
                logger.warning(
                    "kanban notifier: artifact upload (%s) failed: %s",
                    path, exc,
                )

    async def _kanban_dispatcher_watcher(self) -> None:
        """Embedded kanban dispatcher — one tick every `dispatch_interval_seconds`.

        Gated by `kanban.dispatch_in_gateway` in config.yaml (default True).
        When true, the gateway hosts the single dispatcher for this profile:
        no separate `hermes kanban daemon` process needed. When false, the
        loop exits immediately and an external daemon is expected.

        Each tick calls :func:`kanban_db.dispatch_once` inside
        ``asyncio.to_thread`` so the SQLite WAL lock never blocks the
        event loop. Failures in one tick don't stop subsequent ticks —
        same pattern as `_kanban_notifier_watcher`.

        Shutdown: the loop checks ``self._running`` between ticks; gateway
        stop() flips it to False and cancels pending tasks, and the
        in-flight ``to_thread`` returns on its own after the current
        ``dispatch_once`` call finishes (typically <1ms on an idle board).
        """
        # Read config once at boot. If the user flips the flag later, they
        # restart the gateway; same pattern as every other background
        # watcher here. Honours HERMES_KANBAN_DISPATCH_IN_GATEWAY env var
        # as an escape hatch (false-y value disables without editing YAML).
        try:
            from hermes_cli.config import load_config as _load_config
        except Exception:
            logger.warning("kanban dispatcher: config loader unavailable; disabled")
            return
        env_override = os.environ.get("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "").strip().lower()
        if env_override in {"0", "false", "no", "off"}:
            logger.info("kanban dispatcher: disabled via HERMES_KANBAN_DISPATCH_IN_GATEWAY env")
            return

        try:
            cfg = _load_config()
        except Exception as exc:
            logger.warning("kanban dispatcher: cannot load config (%s); disabled", exc)
            return
        kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
        if not kanban_cfg.get("dispatch_in_gateway", True):
            logger.info(
                "kanban dispatcher: disabled via config kanban.dispatch_in_gateway=false"
            )
            return

        try:
            from hermes_cli import kanban_db as _kb
        except Exception:
            logger.warning("kanban dispatcher: kanban_db not importable; dispatcher disabled")
            return

        # Single-dispatcher backstop. dispatch_in_gateway defaults to true, so a
        # new profile gateway (or a same-profile restart race) can silently
        # start a second dispatcher; concurrent dispatchers double reclaim
        # frequency, double claim-attempt events, and — with
        # wal_autocheckpoint=0 — concurrent manual WAL checkpoints can corrupt
        # index pages. The lock lives at the machine-global kanban root
        # (shared across profiles by design), so it serialises ALL gateways.
        self._kanban_dispatcher_lock_handle = None
        _lock_path = _kb.kanban_home() / "kanban" / ".dispatcher.lock"
        _lock_handle, _lock_state = _acquire_singleton_lock(_lock_path)
        if _lock_state == "contended":
            logger.info(
                "kanban dispatcher: another gateway already holds the dispatcher "
                "lock (%s); this gateway will NOT dispatch.", _lock_path,
            )
            return
        if _lock_state == "held":
            self._kanban_dispatcher_lock_handle = _lock_handle  # hold for process lifetime
            logger.info("kanban dispatcher: holding singleton dispatcher lock (%s)", _lock_path)
        else:
            logger.warning(
                "kanban dispatcher: advisory lock unavailable at %s; proceeding "
                "on config control alone.", _lock_path,
            )

        try:
            interval = float(kanban_cfg.get("dispatch_interval_seconds", 60) or 60)
        except (ValueError, TypeError):
            logger.warning(
                "kanban dispatcher: invalid dispatch_interval_seconds=%r, using default 60",
                kanban_cfg.get("dispatch_interval_seconds"),
            )
            interval = 60.0
        interval = max(interval, 1.0)  # sanity floor — tighter than this is a footgun

        # Read max_spawn config to limit concurrent kanban tasks.
        # When not configured, apply DEFAULT_MAX_SPAWN so a gateway
        # restart doesn't flood-spawn every ready task at once.
        max_spawn = kanban_cfg.get("max_spawn", None)
        if max_spawn is None:
            from hermes_cli.kanban_db import DEFAULT_MAX_SPAWN
            max_spawn = DEFAULT_MAX_SPAWN
            logger.info(
                "kanban dispatcher: max_spawn not configured, "
                "using default=%d", max_spawn,
            )
        else:
            logger.info(f"kanban dispatcher: max_spawn={max_spawn}")

        # Cap the number of simultaneously running tasks so slow workers
        # (local LLMs, resource-constrained hosts) don't pile up and time
        # out. When set, the dispatcher skips spawning when the board
        # already has this many tasks in 'running' status.
        raw_max_in_progress = kanban_cfg.get("max_in_progress", None)
        max_in_progress = None
        if raw_max_in_progress is not None:
            try:
                max_in_progress = int(raw_max_in_progress)
            except (TypeError, ValueError):
                logger.warning(
                    "kanban dispatcher: invalid kanban.max_in_progress=%r; ignoring",
                    raw_max_in_progress,
                )
                max_in_progress = None
            else:
                if max_in_progress < 1:
                    logger.warning(
                        "kanban dispatcher: kanban.max_in_progress=%r is below 1; ignoring",
                        raw_max_in_progress,
                    )
                    max_in_progress = None
                else:
                    logger.info(f"kanban dispatcher: max_in_progress={max_in_progress}")

        raw_failure_limit = kanban_cfg.get("failure_limit", _kb.DEFAULT_FAILURE_LIMIT)
        try:
            failure_limit = int(raw_failure_limit)
        except (TypeError, ValueError):
            logger.warning(
                "kanban dispatcher: invalid kanban.failure_limit=%r; using default %d",
                raw_failure_limit,
                _kb.DEFAULT_FAILURE_LIMIT,
            )
            failure_limit = _kb.DEFAULT_FAILURE_LIMIT
        if failure_limit < 1:
            logger.warning(
                "kanban dispatcher: kanban.failure_limit=%r is below 1; using default %d",
                raw_failure_limit,
                _kb.DEFAULT_FAILURE_LIMIT,
            )
            failure_limit = _kb.DEFAULT_FAILURE_LIMIT

        # Read stale_timeout_seconds — 0 disables stale detection.
        raw_stale = kanban_cfg.get("dispatch_stale_timeout_seconds", 0)
        try:
            stale_timeout_seconds = int(raw_stale or 0)
        except (TypeError, ValueError):
            logger.warning(
                "kanban dispatcher: invalid kanban.dispatch_stale_timeout_seconds=%r; "
                "disabling stale detection",
                raw_stale,
            )
            stale_timeout_seconds = 0

        # Read kanban.default_assignee — fallback profile for tasks
        # created without an explicit assignee (e.g. via the dashboard).
        # When set, the dispatcher applies it to unassigned ready tasks
        # instead of skipping them indefinitely (#27145). Empty string
        # (the schema default) means "no fallback, keep skipping" —
        # backward-compatible with existing installs.
        default_assignee = (kanban_cfg.get("default_assignee") or "").strip() or None
        if default_assignee:
            logger.info(
                "kanban dispatcher: default_assignee=%r (unassigned ready tasks "
                "will route to this profile)",
                default_assignee,
            )

        # Read kanban.max_in_progress_per_profile — per-profile concurrency
        # cap (#21582). When set, no single profile gets more than N
        # workers running at once, even if the global max_in_progress
        # would allow it. Prevents one profile's local model / API quota
        # / browser pool from being overwhelmed by a fan-out.
        raw_per_profile = kanban_cfg.get("max_in_progress_per_profile", None)
        max_in_progress_per_profile = None
        if raw_per_profile is not None:
            try:
                max_in_progress_per_profile = int(raw_per_profile)
            except (TypeError, ValueError):
                logger.warning(
                    "kanban dispatcher: invalid kanban.max_in_progress_per_profile=%r; ignoring",
                    raw_per_profile,
                )
                max_in_progress_per_profile = None
            else:
                if max_in_progress_per_profile < 1:
                    logger.warning(
                        "kanban dispatcher: kanban.max_in_progress_per_profile=%r is below 1; ignoring",
                        raw_per_profile,
                    )
                    max_in_progress_per_profile = None
                else:
                    logger.info(
                        "kanban dispatcher: max_in_progress_per_profile=%d",
                        max_in_progress_per_profile,
                    )

        # Initial delay so the gateway finishes wiring adapters before the
        # dispatcher spawns workers (those workers may hit gateway notify
        # subscriptions etc.). Matches the notifier watcher's delay.
        await asyncio.sleep(5)

        # Health telemetry mirrored from `_cmd_daemon`: warn when ready
        # queue is non-empty but spawns are 0 for N consecutive ticks —
        # usually means broken PATH, missing venv, or credential loss.
        HEALTH_WINDOW = 6
        bad_ticks = 0
        last_warn_at = 0
        # Avoid hot-looping corrupt-looking board DBs, but do not suppress
        # same-fingerprint retries forever: transient WAL/open races can
        # surface as "database disk image is malformed" for one tick.
        CORRUPT_BOARD_RETRY_AFTER_SECONDS = 300
        disabled_corrupt_boards: dict[
            str, tuple[tuple[str, int | None, int | None], float]
        ] = {}

        def _board_db_fingerprint(slug: str) -> tuple[str, int | None, int | None]:
            path = _kb.kanban_db_path(slug)
            try:
                resolved = str(path.expanduser().resolve())
            except Exception:
                resolved = str(path)
            try:
                stat = path.stat()
            except OSError:
                return (resolved, None, None)
            return (resolved, stat.st_mtime_ns, stat.st_size)

        def _is_corrupt_board_db_error(exc: Exception) -> bool:
            corrupt_guard_error = getattr(_kb, "KanbanDbCorruptError", None)
            if corrupt_guard_error is not None and isinstance(exc, corrupt_guard_error):
                return True
            if not isinstance(exc, sqlite3.DatabaseError):
                return False
            msg = str(exc).lower()
            return (
                "file is not a database" in msg
                or "database disk image is malformed" in msg
            )

        def _tick_once_for_board(slug: str) -> "Optional[object]":
            """Run one dispatch_once for a specific board.

            Runs in a worker thread via `asyncio.to_thread`. `board=slug`
            is passed through `dispatch_once` so `resolve_workspace` and
            `_default_spawn` see the right paths. The per-board DB is
            opened explicitly so concurrent boards never share a
            connection handle or accidentally claim across each other.
            """
            conn = None
            fingerprint = _board_db_fingerprint(slug)
            disabled_entry = disabled_corrupt_boards.get(slug)
            if disabled_entry is not None:
                disabled_fingerprint, disabled_at = disabled_entry
                age = time.monotonic() - disabled_at
                if (
                    disabled_fingerprint == fingerprint
                    and age < CORRUPT_BOARD_RETRY_AFTER_SECONDS
                ):
                    return None
                if disabled_fingerprint == fingerprint:
                    logger.info(
                        "kanban dispatcher: board %s database fingerprint unchanged "
                        "after %.0fs quarantine; retrying dispatch",
                        slug,
                        age,
                    )
                else:
                    logger.info(
                        "kanban dispatcher: board %s database changed; retrying dispatch",
                        slug,
                    )
                disabled_corrupt_boards.pop(slug, None)
            try:
                conn = _kb.connect(board=slug)
                # `connect()` runs the schema + idempotent migration on
                # first open per process; the previous explicit
                # `init_db()` call here busted the per-process cache and
                # re-ran the migration on a second connection, racing
                # the first. See the matching comment in
                # `_kanban_notifier_watcher` and issue #21378.
                return _kb.dispatch_once(
                    conn,
                    board=slug,
                    max_spawn=max_spawn,
                    max_in_progress=max_in_progress,
                    failure_limit=failure_limit,
                    stale_timeout_seconds=stale_timeout_seconds,
                    default_assignee=default_assignee,
                    max_in_progress_per_profile=max_in_progress_per_profile,
                )
            except sqlite3.DatabaseError as exc:
                if _is_corrupt_board_db_error(exc):
                    disabled_corrupt_boards[slug] = (fingerprint, time.monotonic())
                    logger.error(
                        "kanban dispatcher: board %s database %s is not a valid "
                        "SQLite database; pausing dispatch for this board until "
                        "the file changes, the gateway restarts, or the "
                        "quarantine timer expires. Move or restore the file, "
                        "then run `hermes kanban init` if you need a fresh board.",
                        slug,
                        fingerprint[0],
                    )
                    return None
                logger.exception("kanban dispatcher: tick failed on board %s", slug)
                return None
            except Exception as exc:
                if _is_corrupt_board_db_error(exc):
                    disabled_corrupt_boards[slug] = (fingerprint, time.monotonic())
                    logger.error(
                        "kanban dispatcher: board %s database %s is not a valid "
                        "SQLite database; pausing dispatch for this board until "
                        "the file changes, the gateway restarts, or the "
                        "quarantine timer expires. Move or restore the file, "
                        "then run `hermes kanban init` if you need a fresh board.",
                        slug,
                        fingerprint[0],
                    )
                    return None
                logger.exception("kanban dispatcher: tick failed on board %s", slug)
                return None
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

        def _tick_once() -> "list[tuple[str, Optional[object]]]":
            """Run one dispatch_once per board. Returns (slug, result) pairs.

            Enumerating boards on every tick keeps the dispatcher honest
            when users create a new board mid-run: no restart required,
            the next tick picks it up automatically.
            """
            try:
                boards = _kb.list_boards(include_archived=False)
            except Exception:
                boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
            out: list[tuple[str, "Optional[object]"]] = []
            for b in boards:
                slug = b.get("slug") or _kb.DEFAULT_BOARD
                out.append((slug, _tick_once_for_board(slug)))
            return out

        def _ready_nonempty() -> bool:
            """Cheap probe: is there at least one ready+assigned+unclaimed
            task on ANY board whose assignee maps to a real Hermes profile
            (i.e. one the dispatcher would actually spawn for)?

            Tasks assigned to control-plane lanes (e.g. ``orion-cc``,
            ``orion-research``) are pulled by terminals via
            ``claim_task`` directly and never spawnable, so a queue full
            of those is "correctly idle", not "stuck". Filtering them out
            here keeps the stuck-warn fire only on real failures (broken
            PATH, missing venv, credential loss for a real Hermes profile).
            """
            try:
                boards = _kb.list_boards(include_archived=False)
            except Exception:
                boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
            for b in boards:
                slug = b.get("slug") or _kb.DEFAULT_BOARD
                conn = None
                try:
                    conn = _kb.connect(board=slug)
                    if _kb.has_spawnable_ready(conn):
                        return True
                    if _kb.has_spawnable_review(conn):
                        return True
                except Exception:
                    continue
                finally:
                    if conn is not None:
                        try:
                            conn.close()
                        except Exception:
                            pass
            return False

        # Auto-decompose: turn fresh triage tasks into ready workgraphs
        # before the dispatcher fans out workers. Gated by
        # ``kanban.auto_decompose`` (default True). Capped by
        # ``kanban.auto_decompose_per_tick`` (default 3) so a bulk-load
        # of triage tasks doesn't burst-spend the aux LLM in one tick;
        # remainder defers to subsequent ticks.
        #
        # The flag is re-read from config EVERY tick (#49638) rather than
        # captured once at boot. Auto-decompose is a safety toggle: a user who
        # sees it fan out and run tasks they didn't intend reaches for
        # ``kanban.auto_decompose: false`` to STOP it — and that must take
        # effect on the next tick, not require a gateway restart. (Reported:
        # auto-decompose created and launched destructive tasks while the user
        # was still typing the task description, and the flag "couldn't be
        # disabled" because the gateway had captured its boot-time value.)
        def _read_auto_decompose_settings() -> tuple[bool, int]:
            """Re-resolve (enabled, per_tick) from current config each tick."""
            return _resolve_auto_decompose_settings(_load_config)

        def _auto_decompose_tick(auto_decompose_per_tick: int) -> int:
            """Run the auto-decomposer for up to N triage tasks across all
            boards. Returns the number of triage tasks that were
            successfully decomposed or specified this tick.
            """
            try:
                from hermes_cli import kanban_decompose as _decomp
            except Exception as exc:  # pragma: no cover
                logger.warning(
                    "kanban auto-decompose: import failed (%s); skipping", exc,
                )
                return 0
            try:
                boards = _kb.list_boards(include_archived=False)
            except Exception:
                boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
            attempted = 0
            successes = 0
            for b in boards:
                slug = b.get("slug") or _kb.DEFAULT_BOARD
                if attempted >= auto_decompose_per_tick:
                    break
                # Pin this board for the duration of the call — same
                # pattern as the dashboard specify endpoint. The
                # decomposer module connects with no board kwarg and
                # relies on the env var.
                prev_env = os.environ.get("HERMES_KANBAN_BOARD")
                try:
                    os.environ["HERMES_KANBAN_BOARD"] = slug
                    try:
                        triage_ids = _decomp.list_triage_ids()
                    except Exception as exc:
                        logger.debug(
                            "kanban auto-decompose: list_triage_ids failed on board %s (%s)",
                            slug, exc,
                        )
                        triage_ids = []
                    for tid in triage_ids:
                        if attempted >= auto_decompose_per_tick:
                            break
                        attempted += 1
                        try:
                            outcome = _decomp.decompose_task(
                                tid, author="auto-decomposer",
                            )
                        except Exception:
                            logger.exception(
                                "kanban auto-decompose: decompose_task crashed on %s",
                                tid,
                            )
                            continue
                        if outcome.ok:
                            successes += 1
                            if outcome.fanout and outcome.child_ids:
                                logger.info(
                                    "kanban auto-decompose [%s]: %s → %d children",
                                    slug, tid, len(outcome.child_ids),
                                )
                            else:
                                logger.info(
                                    "kanban auto-decompose [%s]: %s → single task (no fanout)",
                                    slug, tid,
                                )
                        else:
                            # Common no-op reasons (no aux client configured) shouldn't
                            # spam logs every tick. Log at debug.
                            logger.debug(
                                "kanban auto-decompose [%s]: %s skipped: %s",
                                slug, tid, outcome.reason,
                            )
                finally:
                    if prev_env is None:
                        os.environ.pop("HERMES_KANBAN_BOARD", None)
                    else:
                        os.environ["HERMES_KANBAN_BOARD"] = prev_env
            return successes

        logger.info(
            "kanban dispatcher: embedded in gateway (interval=%.1fs)", interval
        )
        while self._running:
            try:
                # Reap zombie children before per-board work so a board DB
                # failure cannot block cleanup of unrelated workers.
                pids = await asyncio.to_thread(_kb.reap_worker_zombies)
                if pids:
                    logger.info(
                        "kanban dispatcher: reaped %d zombie worker(s), pids=%s",
                        len(pids),
                        pids,
                    )
            except Exception:
                logger.exception("kanban dispatcher: zombie reaper failed")

            try:
                # Re-read the auto-decompose toggle live each tick so a user
                # flipping kanban.auto_decompose=false to STOP runaway fan-out
                # takes effect on the next tick, not on gateway restart (#49638).
                _ad_enabled, _ad_per_tick = _read_auto_decompose_settings()
                if _ad_enabled:
                    await asyncio.to_thread(_auto_decompose_tick, _ad_per_tick)
                results = await asyncio.to_thread(_tick_once)
                any_spawned = False
                for slug, res in (results or []):
                    if res is not None and getattr(res, "spawned", None):
                        any_spawned = True
                        # Quiet by default — only log when something actually
                        # happened, so an idle gateway stays silent.
                        logger.info(
                            "kanban dispatcher [%s]: spawned=%d reclaimed=%d "
                            "crashed=%d timed_out=%d promoted=%d auto_blocked=%d",
                            slug,
                            len(res.spawned),
                            res.reclaimed,
                            len(res.crashed) if hasattr(res.crashed, "__len__") else 0,
                            len(res.timed_out) if hasattr(res.timed_out, "__len__") else 0,
                            res.promoted,
                            len(res.auto_blocked) if hasattr(res.auto_blocked, "__len__") else 0,
                        )
                # Health telemetry (aggregate across boards)
                ready_pending = await asyncio.to_thread(_ready_nonempty)
                if ready_pending and not any_spawned:
                    bad_ticks += 1
                else:
                    bad_ticks = 0
                if bad_ticks >= HEALTH_WINDOW:
                    now = int(time.time())
                    if now - last_warn_at >= 300:
                        logger.warning(
                            "kanban dispatcher stuck: ready queue non-empty for "
                            "%d consecutive ticks but 0 workers spawned. Check "
                            "profile health (venv, PATH, credentials) and "
                            "`hermes kanban list --status ready`.",
                            bad_ticks,
                        )
                        last_warn_at = now
            except asyncio.CancelledError:
                logger.debug("kanban dispatcher: cancelled")
                _release_singleton_lock(self._kanban_dispatcher_lock_handle)
                self._kanban_dispatcher_lock_handle = None
                raise
            except Exception:
                logger.exception("kanban dispatcher: unexpected watcher error")

            # Sleep in 1s slices so shutdown is snappy — otherwise a stop()
            # waits up to `interval` seconds for the current sleep to finish.
            slept = 0.0
            while slept < interval and self._running:
                await asyncio.sleep(min(1.0, interval - slept))
                slept += 1.0

        _release_singleton_lock(self._kanban_dispatcher_lock_handle)
        self._kanban_dispatcher_lock_handle = None
