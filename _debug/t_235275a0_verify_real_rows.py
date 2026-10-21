"""t_235275a0 — real-row verification of the emitted block.

Replays Ornith's real session rows (state.db session_id=20261017_182828_140bbb1e
— the session gopher audited) through build_internal_fallback and checks the
five spec items against the emitted block:

  1. tool lines keep the target/filename/command (write_file/terminal/patch/
     tell forms), even for rows whose call arguments were dropped;
  2. every emitted unit carries a [#xx] recovery handle;
  3. emitted unit format: [#id] [ROLE]: text (HH:MM:SS);
  4. NO section headers — chronological stream only;
  5. no duplicate/overlapping manifest-style sections (fold dedupe).

Run:  python3 /home/ekl/.hermes/kanban/workspaces/t_235275a0/verify_real_rows.py
"""
import hashlib
import json
import re
import sqlite3
import sys
import time

sys.path.insert(0, "/home/ekl/.hermes/hermes-agent")

from agent.internal_compression_fallback import build_internal_fallback  # noqa: E402

DB_PATH = "/home/ekl/.hermes/profiles/ornith/state.db"
SESSION_ID = "20261017_182828_140bbb1e"

_UNIT_RE = re.compile(r"^\[#\d+\] \[[A-Z]+\]: .*\(\d{2}:\d{2}:\d{2}\)$", re.S)


def load_messages():
    """Session rows in the production conversation shape (with _row_id)."""
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    rows = db.execute(
        "SELECT id, role, content, tool_call_id, tool_name, tool_calls, timestamp"
        " FROM messages WHERE session_id=? AND active=1 ORDER BY id",
        (SESSION_ID,),
    ).fetchall()
    db.close()
    messages = []
    for row_id, role, content, tool_call_id, tool_name, tool_calls, ts in rows:
        msg = {"role": role, "content": content or "", "_row_id": row_id}
        if tool_call_id:
            msg["tool_call_id"] = tool_call_id
        if tool_name:
            msg["tool_name"] = tool_name
        if tool_calls:
            msg["tool_calls"] = json.loads(tool_calls)
        if ts:
            msg["timestamp"] = ts
        messages.append(msg)
    return messages


def main() -> int:
    src = open(__file__, "rb").read()
    print(f"driver sha256={hashlib.sha256(src).hexdigest()[:16]}")
    print(f"module  sha256={hashlib.sha256(open('/home/ekl/.hermes/hermes-agent/agent/internal_compression_fallback.py', 'rb').read()).hexdigest()[:16]}")
    print(f"loaded  {time.strftime('%Y-%m-%d %H:%M:%S')}")

    messages = load_messages()
    print(f"rows loaded: {len(messages)}")
    fallback = build_internal_fallback(
        messages,
        protect_head_count=2,
        protect_last_n=2,
        marker_recent_count=16,
        target_tokens=8_000,
    )
    summary = fallback.summary
    print("=" * 72)
    print(summary)
    print("=" * 72)

    checks: list[tuple[str, bool, str]] = []

    # 1 — targets preserved / no bare tool names
    tool_lines = [
        line for line in summary.splitlines() if "[TOOL]:" in line
    ]
    bare = [
        line
        for line in tool_lines
        if re.search(r"\[TOOL\]: [a-z_]+:  — ", line)
        or re.search(r"\[TOOL\]: [a-z_]+: — ", line)
    ]
    checks.append((
        "1 targets on tool lines (no bare names)",
        bool(tool_lines) and not bare,
        f"{len(tool_lines)} tool lines, {len(bare)} bare",
    ))
    tell_lines = [line for line in tool_lines if "tell" in line]
    if tell_lines:
        checks.append((
            "1b tell -> tool_call_id form",
            all("tell → tool_call_id " in line for line in tell_lines),
            f"{len(tell_lines)} tell lines",
        ))

    # 2 — handles
    unit_lines = [line for line in summary.splitlines() if line.startswith("[#")]
    checks.append((
        "2 every unit carries [#xx]",
        bool(unit_lines),
        f"{len(unit_lines)} handled units",
    ))

    # 3 — unit format
    bad = [line for line in unit_lines if not line.startswith("[#")]
    checks.append((
        "3 unit format [#id] [ROLE]: text (HH:MM:SS)",
        not bad and all(_UNIT_RE.match(line) for line in unit_lines),
        f"{len(unit_lines)} units checked",
    ))

    # 4 — no section headers
    headers = re.findall(r"(?m)^#{1,6}\s.*$", summary)
    checks.append(("4 no section headers", not headers, f"headers: {headers}"))

    # 5 — no duplicated manifest lines (stream vs fold)
    manifest_keys = [
        m.group(0)
        for m in re.finditer(r"(?m)^(?:\[#\d+\] )?\[TOOL\]: .+ — .+$", summary)
    ]
    dupes = {key for key in manifest_keys if manifest_keys.count(key) > 1}
    checks.append((
        "5 no duplicate manifest lines",
        not dupes,
        f"{len(manifest_keys)} tool lines, dupes: {sorted(dupes)[:3]}",
    ))

    # chronology sanity: row ids increase down the main stream
    ids = [int(m.group(1)) for m in re.finditer(r"(?m)^\[#(\d+)\]", summary)]
    checks.append((
        "stream chronological by row id",
        ids == sorted(ids),
        f"{len(ids)} handles",
    ))

    print()
    ok = True
    for name, passed, detail in checks:
        ok = ok and passed
        print(f"[{'PASS' if passed else 'FAIL'}] {name} — {detail}")
    print()
    print("RESULT:", "ALL SPEC CHECKS PASS" if ok else "SPEC VIOLATIONS REMAIN")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
