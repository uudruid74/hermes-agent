"""Kanban dispatch startup sync for GatewayRunner.

Named for what it does, not for what the deleted code was. The notification
polling loop and the dispatch interval loop that used to live here are GONE
(Evan, 2026-10-19: "we don't use polling loops or watchers"). Notifications
and dispatch are event-time now — every state change runs
``hermes_cli/kanban.py::kick_dispatch`` (one dispatch tick, serialized by
``dispatch_once``'s per-board tick lock) and the notification hook delivers
via ``hermes send -t``, which injects the prompt as a user turn.

What remains is the one thing events cannot cover: a restart gap. While the
gateway was down the world moved on — tasks changed state, workers died —
and nobody was listening. So startup runs ONE reality-check pass (the
auto-decompose backlog + a dispatch tick per board) and returns. Sync to
reality once, then get out of the way. No polling.
"""

from __future__ import annotations

import asyncio
import logging
import os

# Match the logger run.py uses (logging.getLogger(__name__) where __name__ ==
# "gateway.run") so extracted log records keep their original logger name.
logger = logging.getLogger("gateway.run")


# Single implementation lives in hermes_cli.kanban (shared with the
# event-driven triage spawn); re-exported here for existing imports.
from hermes_cli.kanban import _resolve_auto_decompose_settings  # noqa: F401


def _auto_decompose_pass(per_pass: int) -> int:
    """Decompose up to N triage tasks across all boards. Returns successes.

    Same sweep the old per-tick loop ran, but exactly once per wake:
    the gateway startup reality check (backlog catch-up) and the detached
    per-task ``hermes kanban decompose <id>`` spawn for fresh triage tasks.
    """
    from hermes_cli import kanban_db as _kb

    try:
        from hermes_cli import kanban_decompose as _decomp
    except Exception as exc:  # pragma: no cover
        logger.warning("kanban auto-decompose: import failed (%s); skipping", exc)
        return 0
    try:
        boards = _kb.list_boards(include_archived=False)
    except Exception:
        boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
    attempted = 0
    successes = 0
    for b in boards:
        slug = b.get("slug") or _kb.DEFAULT_BOARD
        if attempted >= per_pass:
            break
        # Pin this board for the duration of the call — the decomposer
        # module connects with no board kwarg and relies on the env var.
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
                if attempted >= per_pass:
                    break
                attempted += 1
                try:
                    outcome = _decomp.decompose_task(tid, author="auto-decomposer")
                except Exception:
                    logger.exception(
                        "kanban auto-decompose: decompose_task crashed on %s", tid
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
                    # spam logs. Log at debug.
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


class GatewayKanbanStartupSyncMixin:
    """One-shot kanban reality check at gateway startup."""

    async def _kanban_startup_sync(self) -> None:
        """Sync dispatch to reality once, then return — NOT a loop.

        Catches the restart gap: reclaim stale/crashed runs, promote
        dependents, claim+spawn ready work, and drain (capped) the
        auto-decompose backlog. After this pass, state changes drive
        dispatch through ``hermes_cli/kanban.py::kick_dispatch``; there is
        no interval loop and no dispatcher singleton lock — every tick is
        serialized per board by ``dispatch_once``'s tick lock (issue
        #35240), which also covers a coexisting ``hermes kanban daemon``.
        """
        from hermes_cli import kanban_db as _kb
        from hermes_cli import kanban as _kanban

        # Honour the historical in-gateway dispatch opt-out: when false the
        # user runs `hermes kanban daemon` externally (or doesn't use kanban)
        # and the gateway must not dispatch. Event kicks in emitter processes
        # are a separate concern gated by HERMES_KANBAN_DISPATCH_KICKS.
        env_override = os.environ.get("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "").strip().lower()
        if env_override in {"0", "false", "no", "off"}:
            logger.info("kanban startup sync: disabled via HERMES_KANBAN_DISPATCH_IN_GATEWAY")
            return
        try:
            from hermes_cli.config import load_config as _load_config

            cfg = _load_config()
        except Exception as exc:
            logger.warning("kanban startup sync: cannot load config (%s); disabled", exc)
            return
        kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
        if not kanban_cfg.get("dispatch_in_gateway", True):
            logger.info(
                "kanban startup sync: disabled via config kanban.dispatch_in_gateway=false"
            )
            return

        logger.info("kanban startup sync: reality-check pass begins")

        # 1. Auto-decompose backlog (capped — the burst-spend guard). The
        #    toggle is re-read from config here so a flipped
        #    ``kanban.auto_decompose: false`` is honoured at restart without
        #    further ceremony (#49638).
        ad_enabled, ad_per_pass = _resolve_auto_decompose_settings(_load_config)
        if ad_enabled:
            try:
                decomposed = await asyncio.to_thread(_auto_decompose_pass, ad_per_pass)
                if decomposed:
                    logger.info(
                        "kanban startup sync: decomposed %d triage task(s)", decomposed
                    )
            except Exception:
                logger.exception("kanban startup sync: auto-decompose pass failed")

        # 2. One dispatch tick per board. Enumerating boards here keeps new
        #    boards honest without any watcher: a board created while the
        #    gateway was down is synced on this pass.
        try:
            boards = _kb.list_boards(include_archived=False)
        except Exception:
            boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
        for b in boards:
            slug = b.get("slug") or _kb.DEFAULT_BOARD
            try:
                await asyncio.to_thread(_kanban.kick_dispatch, slug)
            except Exception:
                logger.exception("kanban startup sync: dispatch tick failed on %s", slug)

        logger.info("kanban startup sync: reality-check pass done")