# Dispatch/Wake Conformance Suite — Contract

Evan directive (2026-11-17): *"I want a full test suite of the 'tell' code and kanban
notifications, plan tool dispatching, bug tool watcher, all of it. Nobody commits code
that breaks our damn system ever again!"* — after the 3rd "delivered but no wake turn"
failure this month (2026-11-16-tell-wake-delivered-no-turn, forensics row 530607).

## Why this suite exists (the gap the existing 100+ tests did not cover)

Existing tests assert **delivery**: a call was made, a row was written, an ack returned.
The failures were **outcome** failures: the row landed and NO TURN SPAWNED. Nothing in
the suite crossed the seam between "bridge acks" and "the recipient actually enters a
turn". Neo's fix (537a10306c) made the bridge report the TRUE intake outcome
(spawned/queued/dropped) and fail loudly on dropped — that is Layer 1 for ONE seam.
This suite completes the contract across every wake path and locks the
**"row lands, no turn"** class out permanently.

## Laws (non-negotiable)

1. **DROPPED IS NEVER SUCCESS.** Every wake seam reports the true outcome
   (spawned | queued | dropped). `dropped` is a hard sender-side error, never an ack.
2. **ROW ⇒ INTAKE ⇒ TURN.** Any path that writes a user-role row into a session
   MUST route through the intake handler that spawns the turn
   (`gateway/run.py` `_handle_message_with_agent`, the `inbound message:` site).
   The test assertion is the TURN, not the row.
3. **The suite never mutates the tree it runs against** (git-hygiene rule 7).
   Full-suite runs happen in `git worktree` / temp clone. Tests never touch `.git`.
4. **Live probes are opt-in** (`-m live`), target designated test sessions only, and
   never fire at production sessions without explicit approval.
5. **Commit gate (proposed to Evan):** this suite must be green before a commit that
   touches dispatch/wake paths is treated as done. Pending Evan's approval for the
   enforcement mechanism (pre-commit hook vs. review pin).

## Scope matrix — every wake path × the two contracts

| # | Wake path | L1 outcome truth | L2 row→turn | status |
|---|-----------|------------------|-------------|--------|
| 1 | bridge inject (tell → mcp_bridge `_handle_inject`) | ✅ Neo 537a10306c (5 tests) | ✅ Neo 97341cfeff | core |
| 2 | `send_message_tool` bare `hermes send -t` / internal=True | ✅ fail-loud on dropped (Neo) | ✅ Neo 97341cfeff | core |
| 3 | `tell_tool` with session_id (`session_wake_target`) | 🔲 PENDING | ✅ Neo 97341cfeff | core |
| 4 | kanban `wake_fired` (worker spawn / task events) | 🔲 PENDING | ✅ Neo 97341cfeff | core |
| 5 | kanban notify-complete → subscriber delivery | 🔲 PENDING (Zephyr t_df8b6cf6) | n/a (chat delivery) | medium |
| 6 | plan dispatch / plan-review response wake | 🔲 PENDING | ✅ Neo 97341cfeff | core |
| 7 | plan stop-guard / step-nudge wake | 🔲 PENDING (Zephyr t_df8b6cf6) | n/a | easy |
| 8 | bugtool dispatch wake (bugtoold → assignee) | 🔲 PENDING | 🔲 PENDING | medium |
| 9 | cron job wake (CLI-wake primary, in-process injection) | 🔲 PENDING (Zephyr t_df8b6cf6) | 🔲 PENDING | medium |
| 10 | board-topic wake routing (board_topic rows) | 🔲 PENDING (Zephyr t_df8b6cf6) | n/a | medium |
| 11 | telegram topic/session binding wake (7f4d801016 class) | 🔲 PENDING | 🔲 PENDING | medium |
| 12 | LIVE end-to-end smoke (Gopher repro protocol) | n/a | ✅ `live_wake_smoke.py` | AUTHORITATIVE |

L1 = the seam reports spawned/queued/dropped truthfully and errors on dropped.
L2 = after the row lands, the intake handler runs and a turn actually spawns.

## Layers

- **Layer 1 (contract):** per-seam outcome truthfulness. Unit level, fakes, fast.
- **Layer 2 (intake):** row → `_handle_message_with_agent` → turn. The exact
  regression class. Unit/integration level with a real handler wired to a fake adapter.
- **Layer 3 (live smoke):** `live_wake_smoke.py` — the Gopher protocol verbatim:
  send a tell to a designated test session; assert the row lands AND the
  `inbound message:` line appears in gateway.log AND an assistant turn follows within
  N seconds. Exit codes: 0 = spawned; 3 = **ROW LANDS, NO TURN** (the bug class);
  4 = no row (delivery failure); 5 = dropped by intake. This is the authoritative
  lockout — Layers 1–2 are proxies that must never disagree with it.

## Gap ledger (fills as cells land; suite is the gate only when core cells are green)

- 2026-11-17: matrix created. L1 bridge + send_message_tool = Neo 537a10306c.
- 2026-11-17: L2 core cells (1,2,3,4,6) landed — Neo 97341cfeff, 11 tests
  (test_wake_intake_turn / test_tell_wake_turn / test_kanban_wake_turn /
  test_plan_wake_turn + wake_harness.py), verified green by Wintermute
  (11 passed / 1 skipped = opt-in live). L1 outcome-truth for cells 3/4/6
  still open; Zephyr t_df8b6cf6 covers 5/7/9/10.
- PENDING before the commit gate goes hard: 3-L1, 4-L1, 6-L1, 8, 11.

## Prior art to build on (do not duplicate)

- `tests/gateway/test_mcp_bridge.py` — Neo's 5 outcome tests + fakes harness.
- `tests/tools/test_tell_tool.py`, `test_wake_routing.py`, `test_wake_target_shape.py`
- `tests/gateway/test_wake_delivery.py` (adapter delivery only — NOT outcome)
- `tests/tools/test_plan_wake_delivery.py`, `test_plan_dispatch_kick.py`
- `tests/tools/test_bugtool_*.py` (approval/dependency/dispatch gates)
- `tests/cron/test_cron_wake_route.py`, `test_cron_wake_transport.py`
- Forensics evidence: `wiki/Projects/hermes-agent/bugs/pending/2026-11-16-tell-wake-delivered-no-turn.md`