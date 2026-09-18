"""End-to-end check of the Protected area + subject gate (2026-09-18).

Not a unit test of a piece — Evan: "these changes are designed to work
together and you won't get valid tests one at a time."  This drives the real
`build_internal_fallback` across successive compactions with a realistic
window and asserts the properties the design actually claims.
"""
import sys

sys.path.insert(0, "/home/ekl/.hermes/hermes-agent")
from agent.internal_compression_fallback import (  # noqa: E402
    ProtectedMemory,
    build_internal_fallback,
)

# A window big enough for Protected to engage (> _MAX_CANDIDATE_UNITS units).
# Topic A dominates the early bulk and recurs; topic B's lines are unique.
messages = [{"role": "system", "content": "system prompt"}]
messages.append({"role": "user", "content": "opening request"})
for index in range(150):
    # long-lived recurring thread -- should end up Protected
    messages.append(
        {
            "role": "assistant",
            "content": (
                f"Dax compression architecture: the Protected region holds "
                f"globally central material and it rides the prompt cache. "
                f"Rotation {index} restates the same durable principle."
            ),
        }
    )
for index in range(60):
    # one-off detail -- should NOT be Protected
    messages.append(
        {
            "role": "assistant",
            "content": f"Transient detail number {index}: unrelated passing value {index * 17}.",
        }
    )
messages.append({"role": "user", "content": "current live request"})
messages.append({"role": "assistant", "content": "current live answer"})

KW = dict(protect_head_count=2, protect_last_n=2, target_tokens=6000)

print("=" * 92)
print("  1. FIRST COMPACTION (no history -> bootstrap, area populated)")
print("=" * 92)
f1 = build_internal_fallback(messages, **KW)
m1 = f1.protected_memory
print(f"  mode={f1.mode}  protected block present: {'## Protected Context' in f1.summary}")
print(f"  memory runs recorded: {len(m1.recent)}   top-K: {len(m1.recent[0]) if m1.recent else 0}")
print(f"  recurring thread present: {'Dax compression architecture' in f1.summary}")
print(f"  transient detail present: {'Transient detail number 0:' in f1.summary}")

print()
print("=" * 92)
print("  2. SECOND + THIRD COMPACTION (2-of-3 persistence accumulates)")
print("=" * 92)
f2 = build_internal_fallback(messages, **KW, protected_memory=m1, session_subject="Dax")
f3 = build_internal_fallback(
    messages, **KW, protected_memory=f2.protected_memory, session_subject="Dax"
)
print(f"  after run 2: history {len(f2.protected_memory.recent)} runs")
print(f"  after run 3: history {len(f3.protected_memory.recent)} runs (capped at 3)")
print(f"  area still populated: {'## Protected Context' in f3.summary}")

print()
print("=" * 92)
print("  3. HEAP COMPACTION — same subject, Protected should be STABLE (cacheable)")
print("=" * 92)
before = f3.summary.split("## Verbatim")[0]
f4 = build_internal_fallback(
    messages, **KW, protected_memory=f3.protected_memory, session_subject="Dax"
)
after = f4.summary.split("## Verbatim")[0]
print(f"  prefix identical across the two compactions: {before == after}")
if before != after:
    import difflib

    for line in list(difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm=""))[:12]:
        print("   ", line)

print()
print("=" * 92)
print("  4. FULL COMPACTION — subject CHANGED, history discarded and rebuilt")
print("=" * 92)
f5 = build_internal_fallback(
    messages, **KW, protected_memory=f4.protected_memory, session_subject="Something Else"
)
print(f"  history discarded (bootstrap again): {len(f5.protected_memory.recent)} run")
print(f"  subject stamped: {f5.protected_memory.subject!r}")
print(f"  area rebuilt and populated: {'## Protected Context' in f5.summary}")

print()
print("=" * 92)
print("  5. NO SUBJECT AT ALL — must behave exactly as before (safe fallback)")
print("=" * 92)
f6 = build_internal_fallback(messages, **KW)
print(f"  produced a payload: {bool(f6.summary)}   mode={f6.mode}")
print(f"  did not raise: True")

print()
print("=" * 92)
print("  6. SMALL WINDOW — Protected must stay inert (the regression I caught)")
print("=" * 92)
small = [
    {"role": "system", "content": "system prompt"},
    {"role": "user", "content": "opening"},
    {"role": "assistant", "content": "Unrelated old weather discussion."},
    {"role": "user", "content": "Continue the SQLite schema migration."},
    {"role": "assistant", "content": "Verify the SQLite migration after changing schema."},
]
fs = build_internal_fallback(
    small,
    protect_head_count=2,
    protect_last_n=2,
    target_tokens=160,
    memory_context="SQLite session note: preserve the schema rollback command.",
)
print(f"  Protected engaged on 5-message window: {'## Protected Context' in fs.summary} (want False)")
print(f"  Session Notes still emitted: {'## Session Notes' in fs.summary} (want True)")
