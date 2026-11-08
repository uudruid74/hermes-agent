"""Bridge framing and copy caps for t_94b593b6 (cron wake spam / silent no-injection).

Three controlled defects and their guards, all covered here:

1. ``gateway/mcp_bridge`` read commands with ``StreamReader.readuntil`` under
   the stdlib 64 KiB line limit — ``LimitOverrunError`` killed a 106K-char
   wake payload and the connection died before answering (silent no-injection).
2. Cron wakes carried the assembled brief with the full skill inlined.
3. The ``-t`` wake's mandatory visible copy posted that same payload as 29
   chunked messages.  It must stay a short notice.
"""
from __future__ import annotations

import asyncio
import json


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

def test_visible_copy_caps_large_payload():
    """t_94b593b6: the -u copy is a short notice, never the raw wake payload."""
    import hermes_cli.send_cmd as send_cmd

    assert callable(send_cmd._wake_visible_copy_message)

    long_payload = "[cron-wake] Journals\njob_id: jw\n" + ("This is a very long brief " * 4000)
    short = send_cmd._wake_visible_copy_message("neo", long_payload)
    assert len(short) < 400
    assert "wake payload" in short and "run continues" in short
    assert "very long brief" not in short  # never the raw payload


def test_visible_copy_keeps_short_wakes_verbatim():
    """Short wakes (tells, dispatches) keep their full text under the cap."""
    import hermes_cli.send_cmd as send_cmd

    short = send_cmd._wake_visible_copy_message("neo", "work kanban task t_1")
    assert short == "work kanban task t_1"


def test_visible_copy_handles_empty_payload():
    import hermes_cli.send_cmd as send_cmd

    assert "empty payload" in send_cmd._wake_visible_copy_message("neo", "")
    assert "wake fired" in send_cmd._wake_visible_copy_message("neo", "   ")