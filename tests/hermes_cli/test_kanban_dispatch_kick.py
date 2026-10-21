"""Event-driven dispatch + artifact wake (Evan, 2026-10-19).

No polling loops: state changes kick one dispatch tick (kick_dispatch) and
completed-task artifacts ride the notification wake as MEDIA: uploads.
These tests pin the kick mechanics (env gate, re-entrancy, hook coverage,
settings plumbing) and the artifact wake payload/delivery.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hermes_cli import kanban_db as kb  # noqa: E402
from hermes_cli import kanban  # noqa: E402


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _capture_dispatch(monkeypatch):
    calls: list[dict] = []

    def fake_dispatch_once(conn, **kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(kb, "dispatch_once", fake_dispatch_once)
    return calls


# ---------------------------------------------------------------------------
# kick_dispatch
# ---------------------------------------------------------------------------


def test_kick_runs_one_tick_with_config_settings(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DISPATCH_KICKS", "1")
    calls = _capture_dispatch(monkeypatch)
    kanban.kick_dispatch()
    assert len(calls) == 1
    settings = calls[0]
    assert settings["board"] is None
    for key in (
        "max_spawn", "max_in_progress", "failure_limit",
        "stale_timeout_seconds", "default_assignee",
        "max_in_progress_per_profile",
    ):
        assert key in settings, f"dispatch settings missing {key}"


def test_kick_honours_env_gate(kanban_home, monkeypatch):
    """The suite (and operators) can turn event kicks off without code."""
    monkeypatch.setenv("HERMES_KANBAN_DISPATCH_KICKS", "0")
    calls = _capture_dispatch(monkeypatch)
    kanban.kick_dispatch()
    kanban.spawn_triage_decompose("t_x")
    assert calls == []


def test_kick_is_reentrant_safe(kanban_home, monkeypatch):
    """A dispatch tick's own claims fire the notify hook → must not re-kick."""
    monkeypatch.setenv("HERMES_KANBAN_DISPATCH_KICKS", "1")
    calls: list[dict] = []

    def nested_dispatch(conn, **kwargs):
        calls.append(kwargs)
        # Simulate the tick's claim firing the state-change hook mid-tick.
        kanban._notify_kanban_status_change("t_inner", "done", summary="s")

    monkeypatch.setattr(kb, "dispatch_once", nested_dispatch)
    kanban.kick_dispatch()
    assert len(calls) == 1, "the hook's kick must be suppressed inside a tick"


def test_state_change_hook_kicks_dispatch(kanban_home, monkeypatch):
    """THE state-change choke point drives dispatch — the CLI, tools,
    dispatcher and plan bindings all land here."""
    monkeypatch.setenv("HERMES_KANBAN_DISPATCH_KICKS", "1")
    calls = _capture_dispatch(monkeypatch)
    monkeypatch.setattr(kanban, "_send_kanban_human_notification", lambda **kw: ("x", {"success": True}))
    monkeypatch.setattr(kanban, "_send_kanban_wake", lambda **kw: ("x", {"success": True}))
    monkeypatch.setattr(kanban, "_wake_task_worker_session", lambda *a, **k: False)
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="kick me", assignee="zephyr")
    finally:
        conn.close()
    kanban._notify_kanban_status_change(tid, "completed", summary="done", title="kick me")
    assert len(calls) == 1, "every state change must kick one dispatch tick"


def test_notification_delivers_before_dispatch_kick(kanban_home, monkeypatch):
    """t_d979ec8d: deliver BEFORE kicking the dispatcher.

    A kick-first order ran a whole dispatch tick before this transition's
    own notification went out — and the tick's claims fire their own (newer)
    notifications mid-tick, so subscribers saw 'running' before 'ready' and
    carried the tick's latency on every message.
    """
    monkeypatch.setenv("HERMES_KANBAN_DISPATCH_KICKS", "1")
    order: list[str] = []
    monkeypatch.setattr(
        kanban, "_send_kanban_human_notification",
        lambda **kw: (order.append("deliver") or ("x", {"success": True})),
    )
    monkeypatch.setattr(kanban, "_send_kanban_wake", lambda **kw: ("x", {"success": True}))
    monkeypatch.setattr(kanban, "_wake_task_worker_session", lambda *a, **k: False)

    def fake_dispatch_once(conn, **kwargs):
        order.append("kick")

    monkeypatch.setattr(kb, "dispatch_once", fake_dispatch_once)
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="ordered", assignee="zephyr")
        kb.store_origin_routing(
            conn, tid,
            platform="telegram", chat_id="chat1", thread_id="",
            chat_type="dm", profile="neo",
            allow_non_session=True, overwrite=True,
        )
    finally:
        conn.close()
    kanban._notify_kanban_status_change(tid, "done", summary="s", title="ordered")
    assert order == ["deliver", "kick"]


def test_cli_kick_actions_cover_mutations_not_reads():
    assert "create" in kanban._DISPATCH_KICK_ACTIONS
    assert "unblock" in kanban._DISPATCH_KICK_ACTIONS
    assert "promote" in kanban._DISPATCH_KICK_ACTIONS
    assert "complete" in kanban._DISPATCH_KICK_ACTIONS
    assert "decompose" in kanban._DISPATCH_KICK_ACTIONS
    assert "list" not in kanban._DISPATCH_KICK_ACTIONS
    assert "show" not in kanban._DISPATCH_KICK_ACTIONS
    assert "heartbeat" not in kanban._DISPATCH_KICK_ACTIONS


# ---------------------------------------------------------------------------
# auto-decompose wake (triage entry)
# ---------------------------------------------------------------------------


def test_triage_entry_wakes_decomposer(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DISPATCH_KICKS", "1")
    sent: list[list[str]] = []

    def fake_spawn(argv, **_kw):
        sent.append(list(argv))
        return object()

    monkeypatch.setattr("hermes_cli._subprocess_compat.spawn_detached", fake_spawn)
    kanban.spawn_triage_decompose("t_triage")
    assert sent == [["hermes", "kanban", "decompose", "t_triage"]]


def test_triage_wake_respects_auto_decompose_flag(kanban_home, monkeypatch):
    """#49638: flipping kanban.auto_decompose=false stops fan-out at once."""
    monkeypatch.setenv("HERMES_KANBAN_DISPATCH_KICKS", "1")
    sent: list[list[str]] = []
    monkeypatch.setattr(
        "hermes_cli._subprocess_compat.spawn_detached",
        lambda argv, **kw: sent.append(list(argv)) or object(),
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"auto_decompose": False}},
    )
    kanban.spawn_triage_decompose("t_triage")
    assert sent == []


# ---------------------------------------------------------------------------
# artifact wake (completed-task deliverables as MEDIA: uploads)
# ---------------------------------------------------------------------------


def test_completed_artifacts_ride_the_wake(kanban_home, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DISPATCH_KICKS", "0")
    real = tmp_path / "report.pdf"
    real.write_bytes(b"%PDF-fake")
    ghost = tmp_path / "definitely-does-not-exist.pdf"

    human: list[str] = []
    wakes: list[str] = []
    monkeypatch.setattr(
        kanban, "_send_kanban_human_notification",
        lambda **kw: (human.append(kw["message"]) or ("x", {"success": True})),
    )
    monkeypatch.setattr(
        kanban, "_send_kanban_wake",
        lambda **kw: (wakes.append(kw["payload"]) or ("x", {"success": True})),
    )
    monkeypatch.setattr(kanban, "_wake_task_worker_session", lambda *a, **k: False)

    sent: list[list[str]] = []
    monkeypatch.setattr(
        "hermes_cli._subprocess_compat.spawn_detached",
        lambda argv, **kw: sent.append(list(argv)) or object(),
    )

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="deliverable", assignee="zephyr")
        kb.store_origin_routing(
            conn, tid,
            platform="telegram", chat_id="chat1", thread_id="",
            chat_type="dm", profile="neo",
            allow_non_session=True, overwrite=True,
        )
    finally:
        conn.close()

    kanban._notify_kanban_status_change(
        tid, "completed", summary="done", title="deliverable",
        artifacts=[str(real), str(ghost)],
    )

    # The agent wake carries the existing deliverables (missing ones drop —
    # they may have been referenced by name only).
    payload = json.loads(wakes[-1])
    assert payload["artifacts"] == [str(real)]

    # Each existing file is delivered to the ORIGIN chat as a MEDIA: upload
    # via the media-aware send primitive. The upload in the chat IS the
    # artifact notification. Ghost paths never spawn.
    assert sent == [["hermes", "send", "-u", "telegram:chat1", f"MEDIA:{real}"]]