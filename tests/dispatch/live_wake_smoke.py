#!/usr/bin/env python3
"""LIVE wake smoke — the '2026-11-16-tell-wake-delivered-no-turn' repro protocol.

Sends a tell to a DESIGNATED TEST session and asserts the full wake chain:
  send ack -> message row lands (best-effort) -> 'inbound message:' intake line
  appears in the recipient's gateway.log -> (optional) a turn follows.

This is the AUTHORITATIVE lockout for the 'row lands, no turn' class.
Exit codes (see tests/dispatch/CONTRACT.md):
  0 = wake spawned (intake reached, optional turn pattern matched)
  3 = ROW LANDS, NO TURN   <-- the bug class this suite exists to kill
  4 = delivery failure (send failed / no ack)
  5 = dropped by intake (loud-drop contract from 537a10306c)
  2 = usage / environment error

Usage:
  python tests/dispatch/live_wake_smoke.py --agent gopher [--session ID]
       [--timeout 30] [--log PATH] [--turn-pattern REGEX]

Safety (CONTRACT law 4): opt-in only; target designated test sessions; never
fire at production sessions without explicit approval.
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import subprocess
import sys
import time
import uuid

EXIT_SPAWNED = 0
EXIT_NO_TURN = 3
EXIT_NO_DELIVERY = 4
EXIT_DROPPED = 5
EXIT_USAGE = 2


def find_gateway_log(agent: str, override: str | None) -> str:
    if override:
        return override
    home = os.path.expanduser("~")
    candidates = [
        f"{home}/.hermes/profiles/{agent}/logs/gateway.log",
        f"{home}/.hermes/profiles/{agent}/gateway.log",
        f"{home}/.hermes/{agent}/gateway.log",
        f"/tmp/hermes/{agent}/gateway.log",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    hits = glob.glob(f"{home}/.hermes/**/{agent}/**/gateway.log", recursive=True)
    if hits:
        return hits[0]
    raise SystemExit(f"EXIT_USAGE: gateway.log not found for agent {agent!r}; pass --log")


def send_tell(agent: str, session: str | None, text: str) -> subprocess.CompletedProcess:
    cmd = ["hermes", "send", "-t", agent]
    if session:
        cmd += ["--session", session]
    cmd.append(text)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


def poll_log(log_path: str, offset: int, needle: re.Pattern, deadline: float) -> bool:
    while time.time() < deadline:
        try:
            with open(log_path, "r", errors="replace") as fh:
                fh.seek(offset)
                if needle.search(fh.read()):
                    return True
        except OSError:
            pass
        time.sleep(0.5)
    return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--agent", required=True, help="target TEST profile name")
    ap.add_argument("--session", default=None, help="optional session id to target")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--log", default=None, help="recipient gateway.log path")
    ap.add_argument("--turn-pattern", default=None,
                    help="regex proving an assistant turn followed the intake line")
    args = ap.parse_args(argv)

    marker = f"WAKE-SMOKE-{uuid.uuid4().hex[:12]}"
    log_path = find_gateway_log(args.agent, args.log)
    offset = os.path.getsize(log_path)

    result = send_tell(args.agent, args.session, marker)
    if result.returncode != 0:
        print(f"[FAIL 4] send failed rc={result.returncode}: {result.stderr.strip()[:200]}")
        return EXIT_NO_DELIVERY

    deadline = time.time() + args.timeout
    intake = re.compile(r"inbound message:.*" + re.escape(marker))
    if not poll_log(log_path, offset, intake, deadline):
        # Row may still have landed without intake — that IS the bug class.
        print(f"[FAIL 3] ROW LIKELY LANDS, NO TURN: no 'inbound message:' for {marker} "
              f"in {log_path} within {args.timeout}s")
        return EXIT_NO_TURN

    if args.turn_pattern:
        turn = re.compile(args.turn_pattern)
        if not poll_log(log_path, offset, turn, deadline):
            print(f"[FAIL 3] intake reached but turn pattern {args.turn_pattern!r} "
                  f"never matched within {args.timeout}s")
            return EXIT_NO_TURN

    print(f"[OK 0] wake spawned for {marker} (intake reached in {log_path})")
    return EXIT_SPAWNED


if __name__ == "__main__":
    sys.exit(main())