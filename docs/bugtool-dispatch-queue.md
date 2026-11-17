# Bugtool dispatch queue

`bugtool` is the wiki-backed bug tracker for the Hermes fleet (`scripts/bugtool.py`).
Bug reports live as dated, front-mattered markdown under `wiki/Projects/<project>/bugs/`;
`bugtoold` (`scripts/bugtoold.py`) watches them, commits watched changes, and dispatches
checked bugs to the kanban board through the normal dispatch gates.

This page documents the dispatch queue and the queue-drain verification that sits on top
of it.

## Two-lane model (Evan)

The bug pipeline runs on two lanes:

- **Fast lane** — every change gets code-review-graph blast-radius analysis + agent
  review + targeted tests for that change. This keeps the queue moving.
- **Slow lane** — the big suite runs **once**, after a night of changes, not after every
  job. When the hermes-agent bug queue drains, `bugtoold` runs the named suite a single
  time; a RED run dispatches a fix job to Neo through the normal gates.

Evan's standing order (verbatim, recorded in `VERIFY_APPROVAL_NOTE` in the source):

> "let's change this massive test suite to only run once after a night of changes.
> Instead of running it after every job, we let bugtool run it when the queue empties,
> and if it fails, it dispatches another job to Neo to fix the failures."

## Queue-drain verify

`bugtool verify [--on-drain]` runs the named big suite once and acts on the result.

- **Trigger** — when the hermes-agent bug queue drains (no live bug tasks, no checked,
  gate-complete bug waiting to dispatch), `bugtoold` spawns a **detached**
  `bugtool verify --on-drain` so the watch loop never blocks on the long run.
- **Scope** — only `hermes-agent` has a named suite today (the `VERIFY_SUITES`
  registry); other boards get named suites later. `bugtool verify` on a project not in
  the registry dies loudly.
- **`--on-drain`** — only starts when the queue is drained AND this git sha hasn't been
  verified yet (tracked in the verify marker). Without `--on-drain` it runs
  unconditionally (manual invocation).
- **RED** — auto-files `suite-red-<sha>` assigned to **Neo**, carrying the failure list,
  and dispatches it through the **normal** dispatch gates. Approval provenance on the
  auto-filed card is Evan's standing order, recorded verbatim. Idempotent: one card per
  red sha.
- **GREEN** — writes the result to the verify marker; no card.

## One-at-a-time per assignee

`MAX_LIVE_BUG_TASKS = 1` per assignee (Evan 2026-11-17): at most one live bug task per
assignee before further dispatches for that agent are **deferred** — never dropped.

- Different assignees run in parallel; the same assignee drains **newest-first**
  (Evan 2026-11-17: "NEWEST first. Those are the important ones" — the sweep
  scans date-sorted descending, so the freshest checked bug goes when the slot
  frees; the stale old tail waits for triage).
- **DEFER, never drop** — a deferred file stays `pending` + checked and the next pass
  picks it up when the slot frees.
- `bugtool redispatch --directive` (force) bypasses the gate — the human asked for it.
- **Non-bug tasks don't hold the bug queue.** `live_bug_task_count` counts only tasks
  titled `BUG: <slug>` (the tool's own convention).
- **Blocked tasks don't count toward the cap** (Evan 2026-10-14) — a blocked bug task is
  not consuming attention, so it does not hold the slot.
- A separate, broader cap, `MAX_LIVE_TASKS_PER_AGENT = 5`, applies to all live tasks per
  agent (not just bugs), also defer-never-drop and force-bypassed by redispatch.

## Completion triggers

A kanban task completion fires `kick_dispatch` (`hermes_cli/kanban.py`) — every mutation
that can enable dispatch work, including `complete`, kicks one dispatch tick. So each
completion is a dispatch event: the queue self-drains and the next checked bug goes out
automatically. When the queue empties, the verify fires; if it's red the Neo fix card
enters the same queue, closing the loop:

```
fix -> drain -> verify -> green
```

## Artifact law

"We should not be chewing 1.7G of data, ever" (Evan 2026-11-17). The verify run measures
tmp growth against a 512 MB budget (`BUGTOOL_VERIFY_BUDGET_MB`). An over-budget run is
RED even if tests pass, and the top per-test-dir offenders are named in the card so the
bloat is never anonymous.

## Per-test ceiling

`pyproject.toml` sets a global `timeout = 120` (pytest-timeout): no single test may
outlast real usage. Offenders fail loud as Timeout instead of silently grinding.

## State / logs

Everything lives under `BUGTOOL_STATE_ROOT` (default `~/.local/state/bugtool`):

- `bugs-dispatched.log` — dispatch records (one JSON line per dispatch attempt).
- `suite-verify.json` — the verify marker (pid, sha, result).
- `suite-verify.log` — the verify run's output.
