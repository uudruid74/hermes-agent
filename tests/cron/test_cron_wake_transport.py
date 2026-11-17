"""Bridge framing and copy caps for t_94b593b6 (cron wake spam / silent no-injection).

Three controlled defects and their guards, all covered here:

1. ``gateway/mcp_bridge`` read commands with ``StreamReader.readuntil`` under
   the stdlib 64 KiB line limit — ``LimitOverrunError`` killed a 106K-char
   wake payload and the connection died before answering (silent no-injection).
2. Cron wakes carried the assembled brief with the full skill inlined.
3. The ``-t`` wake's mandatory visible copy posted that same payload as 29
   chunked messages.  It must stay a short notice.

Module guard (t_94b593b6): no test here may reach a real `hermes send` — the
subprocess is held hostage so a forgotten fake fails loud instead of
waking a live agent.
"""
from __future__ import annotations

import asyncio
import json
import subprocess as _real_subprocess

import pytest


@pytest.fixture(autouse=True)
def _no_live_wake(monkeypatch):
    def blocked(*_a, **_kw):
        raise AssertionError(
            "wake test tried to run a real subprocess — fake "
            "cron.scheduler.subprocess.run before invoking a wake path"
        )

    monkeypatch.setattr(_real_subprocess, "run", blocked)


# ---------------------------------------------------------------------------
# 1. Bridge reader limit — payload framing
# ---------------------------------------------------------------------------

async def _read_from_reader(payload: bytes, *, cap: int | None = None):
    """Feed ``payload`` into a real StreamReader and run the bridge reader.

    Returns ``(old_readuntil_result, new_reader_result)``:

    * ``old_readuntil_result`` is None when the stdlib ``readuntil`` under the
      64 KiB line limit raised ``LimitOverrunError`` (the pre-fix behavior),
      else the raw line.
    * ``new_reader_result`` is what ``_read_command_line`` returns now.
    """
    from gateway.mcp_bridge import _read_command_line

    reader = asyncio.StreamReader()
    reader.feed_data(payload)
    reader.feed_eof()

    # Demonstrate the old failure mode on the same payload/stream.
    old_readuntil = "unavailable"
    try:
        old_readuntil = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=5.0)
    except asyncio.LimitOverrunError:
        old_readuntil = None
    except (asyncio.IncompleteReadError, asyncio.TimeoutError):
        old_readuntil = "no-newline"
    except Exception:
        old_readuntil = "unreadable"

    reader2 = asyncio.StreamReader()
    reader2.feed_data(payload)
    reader2.feed_eof()
    if cap is not None:
        new_result = await _read_command_line(reader2, timeout=5.0, max_bytes=cap)
    else:
        new_result = await _read_command_line(reader2, timeout=5.0)
    return old_readuntil, new_result


def test_bridge_reads_payloads_over_64k():
    """Payloads larger than the old 64 KiB reader limit survive framing."""
    payload = json.dumps({"action": "send", "text": "X" * (128 * 1024)}).encode() + b"\n"
    old, new = asyncio.run(_read_from_reader(payload))
    assert old is None  # the old readuntil ceiling is the documented defect
    assert new == payload
    assert len(new) > 64 * 1024


def test_bridge_reader_accepts_106k_wake_brief():
    """The exact defect: a 106K-char cron journal brief must frame cleanly."""
    payload = json.dumps({"action": "send", "text": "B" * (106 * 1024)}).encode() + b"\n"
    old, new = asyncio.run(_read_from_reader(payload))
    assert old is None
    assert new == payload


def test_bridge_reader_bounded_by_max_bytes():
    """A peer pushing past the cap is refused, not buffered forever."""
    payload = json.dumps({"action": "x", "text": "Y" * 300_000}).encode() + b"\n"
    _, new = asyncio.run(_read_from_reader(payload, cap=100))
    assert new is None


def test_bridge_reader_handles_eof_and_timeout():
    from gateway.mcp_bridge import _read_command_line

    class EmptyReader:
        async def read(self, n=-1):
            return b""

    assert asyncio.run(_read_command_line(EmptyReader(), timeout=0.2)) is None
    assert asyncio.run(_read_command_line(EmptyReader(), timeout=0.2, max_bytes=10)) is None


def test_bridge_default_cap_is_sane():
    from gateway.mcp_bridge import BRIDGE_MAX_PAYLOAD_BYTES

    assert 64 * 1024 < BRIDGE_MAX_PAYLOAD_BYTES <= 1024 * 1024


# ---------------------------------------------------------------------------
# 2/3. Payload discipline + visible-copy caps
# ---------------------------------------------------------------------------

def test_visible_copy_keeps_full_payload_verbatim():
    """Evan 2026-11-11: the -u copy IS the full payload (cap removed).

    The visible copy must never hide what travels through the system — a
    long wake arrives in full (chunked by the platform), never truncated to
    a short notice (t_94b593b6's cap was removed by design).
    """
    import hermes_cli.send_cmd as send_cmd

    assert callable(send_cmd._wake_visible_copy_message)

    long_payload = "[cron-wake] Journals\njob_id: jw\n" + ("This is a very long brief " * 4000)
    visible = send_cmd._wake_visible_copy_message("neo", long_payload)
    assert visible == long_payload.strip()
    assert "very long brief" in visible  # full payload, never truncated


def test_visible_copy_keeps_short_wakes_verbatim():
    """Short wakes (tells, dispatches) keep their full text under the cap."""
    import hermes_cli.send_cmd as send_cmd

    short = send_cmd._wake_visible_copy_message("neo", "work kanban task t_1")
    assert short == "work kanban task t_1"


def test_visible_copy_handles_empty_payload():
    import hermes_cli.send_cmd as send_cmd

    assert "empty payload" in send_cmd._wake_visible_copy_message("neo", "")
    assert "wake fired" in send_cmd._wake_visible_copy_message("neo", "   ")

# ---------------------------------------------------------------------------
# 4. In-process fallback intake truth (cell 9-L1, t_1afbfd87)
# ---------------------------------------------------------------------------
#
# The fallback is the LAST-RESORT path in fire_cron_wake. It used to treat
# "adapter.handle_message returned without raising" as delivery — a wake an
# adapter swallows (no handler attached, event dropped) reported success and
# the job recorded "wake fired" for a turn that never ran. It now reports the
# TRUE intake outcome by observing the session guard around the call, exactly
# like gateway.mcp_bridge._handle_inject:
#
#   busy guard already installed -> queued (the turn runs when the active
#                                    one finishes; a delivery)
#   guard installed after         -> spawned (a delivery)
#   neither                       -> dropped (NOT a delivery: hard failure)


def _adapter_loop():
    """A running loop in a background thread the fallback can schedule onto."""
    import asyncio
    import threading

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=lambda: (asyncio.set_event_loop(loop), loop.run_forever()), daemon=True)
    thread.start()
    return loop


def _source_for(target="telegram:8900123006"):
    from gateway.config import Platform
    from gateway.session import SessionSource

    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="8900123006",
        chat_type="dm",
        user_id="cli",
        user_name="CLI User",
    )


def _key_for(source):
    from gateway.session import build_session_key

    return build_session_key(
        source, group_sessions_per_user=True, thread_sessions_per_user=False
    )


class _RealisticAdapter:
    """Models BasePlatformAdapter.handle_message: install the session guard,
    then spawn the turn. ``guard=False`` models an adapter that swallows the
    event (no message handler attached) — the dropped case."""

    def __init__(self, *, guard=True):
        from types import SimpleNamespace

        self.config = SimpleNamespace(extra={})
        self._active_sessions = {}
        self.guard = guard
        self.events = []

    async def handle_message(self, event):
        self.events.append(event)
        if not self.guard:
            return
        self._active_sessions[_key_for(event.source)] = __import__("asyncio").Event()


def test_in_process_wake_spawned_is_delivery():
    from cron.scheduler import _in_process_wake

    adapter = _RealisticAdapter()
    loop = _adapter_loop()
    try:
        assert (
            _in_process_wake(
                "telegram:8900123006",
                "the brief",
                adapters={"telegram": adapter},
                loop=loop,
            )
            is True
        )
        assert len(adapter.events) == 1
        assert adapter.events[0].text.startswith("Incoming user wake (cron):")
    finally:
        loop.call_soon_threadsafe(loop.stop)


def test_in_process_wake_queued_is_delivery():
    """A wake queued behind a busy session WILL run — a delivery, not a drop."""
    from cron.scheduler import _in_process_wake

    adapter = _RealisticAdapter()
    source = _source_for()
    adapter._active_sessions[_key_for(source)] = __import__("asyncio").Event()
    loop = _adapter_loop()
    try:
        assert (
            _in_process_wake(
                "telegram:8900123006",
                "the brief",
                adapters={"telegram": adapter},
                loop=loop,
            )
            is True
        )
    finally:
        loop.call_soon_threadsafe(loop.stop)


def test_in_process_wake_dropped_is_hard_failure():
    """THE cell-9-L1 fallback bug: an adapter that accepts the event and
    starts no turn is a DROPPED wake. Reporting success there is the exact
    'wake fired for a turn that never spawned' false green."""
    from cron.scheduler import _in_process_wake

    adapter = _RealisticAdapter(guard=False)
    loop = _adapter_loop()
    try:
        assert (
            _in_process_wake(
                "telegram:8900123006",
                "the brief",
                adapters={"telegram": adapter},
                loop=loop,
            )
            is False
        ), "a wake with no turn must never report delivery"
        assert len(adapter.events) == 1, "the event was offered; it just dropped"
    finally:
        loop.call_soon_threadsafe(loop.stop)


def test_in_process_wake_adapter_raising_is_failure():
    from cron.scheduler import _in_process_wake

    class Rejecting(_RealisticAdapter):
        async def handle_message(self, event):
            raise RuntimeError("wedged")

    loop = _adapter_loop()
    try:
        assert (
            _in_process_wake(
                "telegram:8900123006",
                "the brief",
                adapters={"telegram": Rejecting()},
                loop=loop,
            )
            is False
        )
    finally:
        loop.call_soon_threadsafe(loop.stop)


def test_fire_cron_wake_never_launders_a_dropped_fallback(monkeypatch):
    """fire_cron_wake composes CLI + fallback: a confirmed-failed CLI wake
    followed by a DROPPED in-process fallback is an honest False."""
    import subprocess

    monkeypatch.setattr(
        "cron.scheduler.subprocess.run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom"),
    )
    adapter = _RealisticAdapter(guard=False)
    loop = _adapter_loop()
    try:
        from cron.scheduler import fire_cron_wake

        assert fire_cron_wake("neo", "the brief", adapters={"telegram": adapter}, loop=loop) is False
    finally:
        loop.call_soon_threadsafe(loop.stop)
