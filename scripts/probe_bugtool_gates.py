#!/usr/bin/env python3
"""READ-ONLY probe across the REAL pending bug set.

Answers: "if the dispatcher ran right now, would it create ANY task?"

This replaces scripts/probe_bugtool_gates.py, which lived only in a kanban
workspace copy and was lost when the ghosts were cleared. It belongs in the
live tree (scripts/) because it is a DEPLOYMENT check, not a repo check —
see gopher-kanban-flow references/bugtool-six-copies-2026-09-15.md.

Safety: stubs subprocess.run so that reaching a task-creation call RAISES
instead of spawning a real worker. Nothing here mutates the board.

Usage:
    ./venv/bin/python scripts/probe_bugtool_gates.py            # all pending
    ./venv/bin/python scripts/probe_bugtool_gates.py -v         # per-file detail
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

BUGTOOL_PATHS = [
    Path.home() / ".local" / "bin" / "bugtool",
    Path(__file__).resolve().parent / "bugtool.py",
]


def load_bugtool(path: Path):
    """Import bugtool.py as a module without executing its CLI."""
    resolved = path.resolve()
    spec = importlib.util.spec_from_file_location("bugtool_probe_target", resolved)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load bugtool from {resolved}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class CreationReached(RuntimeError):
    """Raised if any code path tries to run a real subprocess."""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print per-file gate result")
    ap.add_argument("--bugtool", default=None,
                    help="path to the bugtool to probe (default: the deployed one)")
    args = ap.parse_args()

    target = Path(args.bugtool) if args.bugtool else next(
        (p for p in BUGTOOL_PATHS if p.exists()), None
    )
    if target is None:
        raise SystemExit("no bugtool found to probe")

    print(f"probing deployed path: {target}")
    if target.is_symlink():
        print(f"  -> resolves to: {target.resolve()}")
    mod = load_bugtool(target)

    # Locate the real bug trees.
    roots = sorted(
        d for d in (Path.home() / "vault").rglob("*")
        if d.is_dir() and d.name == "pending" and d.parent.name == "bugs"
    )
    if not roots:
        print("no pending/ bug directories found under ~/vault")
        return 0

    # Stub every subprocess entry point -> any real dispatch attempt raises.
    real_run = subprocess.run
    created_attempts: list[list] = []

    def guarded_run(cmd, *a, **kw):
        created_attempts.append(cmd if isinstance(cmd, list) else [str(cmd)])
        raise CreationReached(f"subprocess reached: {cmd!r}")

    subprocess.run = guarded_run
    try:
        total = would_dispatch = errored = skipped = 0
        for root in roots:
            for path in sorted(root.glob("*.md")):
                total += 1
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError as exc:
                    errored += 1
                    if args.verbose:
                        print(f"  ERR  {path.name}: {exc}")
                    continue
                # maybe_dispatch_locked is the single gate everything funnels
                # through (cmd_check and cmd_dispatch both call it).
                before = len(created_attempts)
                buf = io.StringIO()
                try:
                    with redirect_stdout(buf):
                        result = mod.maybe_dispatch_locked(path, text)
                except CreationReached:
                    would_dispatch += 1
                    print(f"  *** WOULD CREATE *** {path.name}")
                    continue
                except Exception as exc:  # gate raised: report, never hide
                    errored += 1
                    if args.verbose:
                        print(f"  ERR  {path.name}: {type(exc).__name__}: {exc}")
                    continue
                if result:
                    would_dispatch += 1
                    print(f"  *** WOULD CREATE *** {path.name} -> {result}")
                else:
                    skipped += 1
                    if args.verbose:
                        note = buf.getvalue().strip().splitlines()
                        print(f"  ok   {path.name}: {note[-1] if note else 'blocked'}")
                if len(created_attempts) != before:
                    print(f"  *** SUBPROCESS REACHED on {path.name} ***")
    finally:
        subprocess.run = real_run

    print()
    print(f"files scanned   : {total}")
    print(f"would dispatch  : {would_dispatch}   <- MUST be 0")
    print(f"blocked (good)  : {skipped}")
    print(f"errors          : {errored}")
    print(f"subprocess hits : {len(created_attempts)}   <- MUST be 0")
    print()
    print("RESULT:", "PASS" if (would_dispatch == 0 and not created_attempts) else "FAIL")
    return 0 if (would_dispatch == 0 and not created_attempts) else 1


if __name__ == "__main__":
    sys.exit(main())
