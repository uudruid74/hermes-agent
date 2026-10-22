#!/usr/bin/env python3
"""check_payload_handles.py — READ-ONLY audit of an emitted compressed block.

Verifies the contract the emitted block advertises:

    [#N] ... (HH:MM:SS)   ==   the clock of session row N

and separately reports handles whose row no longer exists.

Written after the t_e4812748 emitted-block fixes. The point is that this
checks the *database*, not the compressor's own fields — so it cannot agree
with a buggy pairing by construction. Run it on a freshly emitted payload to
validate a change; run it on an old payload to see the pre-fix state.

No writes. Opens every DB with mode=ro. Safe to commit as a dev audit script.

Usage:
    python3 scripts/check_payload_handles.py --db <state.db> --row <rowid>
    python3 scripts/check_payload_handles.py --db <state.db> --latest
    python3 scripts/check_payload_handles.py --db <state.db> --latest -v

Exit codes:
    0  every handle's printed stamp matches its row  (and no dead handles)
    1  at least one mismatch or dead handle
    2  could not read the payload
"""

from __future__ import annotations

import argparse
import datetime as _dt
import re
import sqlite3
import sys

HANDLE_RE = re.compile(r"\[#(\d+)\]")
STAMP_RE = re.compile(r"\((\d{2}):(\d{2}):(\d{2})\)\s*$")
BLOCK_MARKERS = ("[CONTEXT WINDOW COMPRESSED]", "[PRIOR CONTEXT SUMMARY]")


def _open_ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _find_latest_payload(con: sqlite3.Connection) -> int | None:
    """Newest row whose content looks like an emitted compressed block."""
    for marker in BLOCK_MARKERS:
        row = con.execute(
            "SELECT id FROM messages WHERE content LIKE ? ORDER BY id DESC LIMIT 1",
            (marker + "%",),
        ).fetchone()
        if row:
            return int(row[0])
    return None


def _hhmmss(ts: float | None) -> str | None:
    if ts is None:
        return None
    return _dt.datetime.utcfromtimestamp(float(ts)).strftime("%H:%M:%S")


def _extract_pairs(content: str) -> list[tuple[int, str, int]]:
    """(handle, printed_stamp, line_number) for every handle+stamp line."""
    out: list[tuple[int, str, int]] = []
    for lineno, line in enumerate(content.splitlines(), 1):
        stamp = STAMP_RE.search(line)
        if not stamp:
            continue
        handle = HANDLE_RE.search(line)
        if not handle:
            continue
        out.append((int(handle.group(1)), stamp.group(0).strip("() ").strip(), lineno))
    return out


def _attribute(con: sqlite3.Connection, printed: str, lo: int, hi: int) -> str:
    """Which row actually owns the printed clock? Search near the handle."""
    hits = con.execute(
        "SELECT id FROM messages WHERE id BETWEEN ? AND ? AND "
        "strftime('%H:%M:%S', timestamp, 'unixepoch') = ?",
        (max(1, lo), hi, printed),
    ).fetchall()
    if not hits:
        return "no row near this handle carries that clock"
    ids = [int(h[0]) for h in hits]
    return "clock belongs to row(s): " + ", ".join(f"#{i}" for i in ids[:6])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True, help="path to a profile state.db")
    ap.add_argument("--row", type=int, help="messages.id of the emitted payload")
    ap.add_argument("--latest", action="store_true", help="auto-find newest payload")
    ap.add_argument("--window", type=int, default=40, help="row-id search span")
    ap.add_argument("-v", "--verbose", action="store_true", help="list every line")
    args = ap.parse_args(argv)

    try:
        con = _open_ro(args.db)
    except sqlite3.Error as exc:
        print(f"ERROR: cannot open {args.db}: {exc}", file=sys.stderr)
        return 2

    rowid = args.row
    if rowid is None:
        if not args.latest:
            ap.error("give --row or --latest")
        rowid = _find_latest_payload(con)
        if rowid is None:
            print("ERROR: no emitted payload found", file=sys.stderr)
            return 2

    row = con.execute(
        "SELECT role, length(content), content FROM messages WHERE id=?", (rowid,)
    ).fetchone()
    if row is None:
        print(f"ERROR: row {rowid} not found", file=sys.stderr)
        return 2

    role, size, content = row[0], row[1], row[2] or ""
    pairs = _extract_pairs(content)
    print(f"payload row {rowid}  role={role}  bytes={size}  handle+stamp lines={len(pairs)}")

    match = mismatch = dead = 0
    bad_rows: list[tuple[int, str, str, str, int]] = []
    dead_rows: list[tuple[int, int]] = []

    for handle, printed, lineno in pairs:
        r = con.execute("SELECT timestamp FROM messages WHERE id=?", (handle,)).fetchone()
        if r is None:
            dead += 1
            dead_rows.append((handle, lineno))
            continue
        own = _hhmmss(r[0])
        if own == printed:
            match += 1
        else:
            mismatch += 1
            bad_rows.append((handle, printed, own or "?", "", lineno))

    # attribute the misprinted clocks
    attributed: list[tuple[int, str, str, str, int]] = []
    for handle, printed, own, _, lineno in bad_rows:
        who = _attribute(con, printed, handle - args.window, handle + args.window)
        attributed.append((handle, printed, own, who, lineno))

    if args.verbose and match:
        print("\n  matching handles:")
        for handle, printed, _ in pairs:
            r = con.execute("SELECT timestamp FROM messages WHERE id=?", (handle,)).fetchone()
            if r and _hhmmss(r[0]) == printed:
                print(f"    #{handle} printed {printed} == own {_hhmmss(r[0])}  OK")

    if attributed:
        print("\n  MISMATCHES (printed stamp is not the row's own clock):")
        for handle, printed, own, who, lineno in attributed:
            print(f"    line {lineno:4d}  #{handle} printed {printed}  own {own}   {who}")

    if dead_rows:
        print("\n  DEAD HANDLES (row no longer exists — session_search cannot open these):")
        for handle, lineno in dead_rows:
            print(f"    line {lineno:4d}  #{handle}")

    print(
        f"\nresult: match={match} mismatch={mismatch} dead={dead} "
        f"(of {len(pairs)} handle+stamp lines)"
    )
    con.close()
    return 0 if (mismatch == 0 and dead == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
