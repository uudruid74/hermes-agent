"""Opt-in LIVE wake smoke (CONTRACT law 4). Skipped unless RUN_LIVE=1.

Run against a DESIGNATED TEST session only:
  RUN_LIVE=1 LIVE_AGENT=gopher python -m pytest tests/dispatch/test_live_wake_smoke.py -v

This is the pytest face of tests/dispatch/live_wake_smoke.py — the authoritative
'row lands, no turn' lockout. See tests/dispatch/CONTRACT.md.
"""

import os
import sys

import pytest

from tests.dispatch.live_wake_smoke import main as smoke_main

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE") != "1",
    reason="live wake smoke: opt-in only (RUN_LIVE=1), targets designated test sessions",
)


def test_live_tell_wake_spawns_turn():
    agent = os.environ.get("LIVE_AGENT")
    assert agent, "LIVE_AGENT=<test profile> required for the live smoke"
    argv = ["--agent", agent, "--timeout", os.environ.get("LIVE_TIMEOUT", "30")]
    session = os.environ.get("LIVE_SESSION")
    if session:
        argv += ["--session", session]
    rc = smoke_main(argv)
    assert rc == 0, (
        "wake smoke failed with exit %d — exit 3 is the 'row lands, no turn' class "
        "(2026-11-16-tell-wake-delivered-no-turn); exit 5 = loud-drop contract trip"
        % rc
    )