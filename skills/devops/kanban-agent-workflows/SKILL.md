---
name: kanban-agent-workflows
description: Use when coordinating durable multi-agent execution through Hermes Kanban or adjacent implementation lanes — orchestrators, workers, Codex lanes, and plan-driven subagent execution.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [kanban, delegation, orchestration, subagents, codex, workflow, multi-agent]
    related_skills: [kanban-worker, kanban-orchestrator, codex, opencode, claude-code]
---

# Kanban Agent Workflows

## Overview
This umbrella covers durable multi-agent execution patterns in Hermes: Kanban orchestration, dispatched worker behavior, isolated coding lanes, and plan-driven subagent execution. It is the default dispatch skill for Kanban workers; the mandatory lifecycle still comes from `KANBAN_GUIDANCE` in the system prompt, while this skill supplies deeper coordination patterns and pitfalls.

## When to Use
- A task should be decomposed across Kanban cards.
- You are acting as a Kanban orchestrator or dispatcher-spawned worker.
- A worker needs a separate coding lane while Hermes retains lifecycle ownership.
- Work starts from a plan and should proceed task-by-task with review gates.

## Modes

### Kanban Orchestrator
Use for decomposition, dependency graphing, task creation/linking, recovery of stuck work, and user-facing status synthesis. Orchestrators route and reconcile; they should not quietly do all implementation themselves when durable coordination is the point.

### Kanban Worker
Use for claim → orient → execute → heartbeat → block/complete behavior on a single card. Workers should keep summaries tight, metadata structured, and boundaries explicit.

Good completion shape:

```python
kanban_complete(
    summary="implemented rate limiter; token bucket by user_id with IP fallback; 14 tests pass",
    metadata={
        "changed_files": ["rate_limiter.py", "tests/test_rate_limiter.py"],
        "tests_run": 14,
        "tests_passed": 14,
        "decisions": ["user_id primary, IP fallback for unauthenticated requests"],
    },
)
```

For code that needs human eyes, comment with structured handoff metadata, then block with a `review-required:` reason instead of marking the task terminally done.

### Coding Lane
Use a specialized coding backend only when a worker truly benefits from isolated implementation assistance. Hermes still owns the Kanban lifecycle, verification, logs, and final handoff.

### Plan-Driven Subagent Execution
Use when the work starts from a finite written plan and synchronous execution is acceptable. This is less durable than Kanban, but should still be explicitly staged with review gates.

## Decision Rules
- Use Kanban when work must survive longer, involve dependencies, or span profiles.
- Use plan-driven subagents when the scope is finite and synchronous execution is acceptable.
- Use isolated coding lanes for implementation help, not lifecycle ownership.
- If you are tempted to “just do it all yourself,” re-check whether that breaks the orchestration role.

## Common Pitfalls
1. Orchestrators doing implementation instead of routing.
2. Workers claiming or completing tasks without captured IDs and structured metadata.
3. Treating coding agents as lifecycle owners instead of execution lanes.
4. Running plan-driven subagents without explicit review gates.
5. Forgetting durable Kanban and synchronous subagents solve different coordination problems.
6. Assuming there is a built-in `hermes kanban status` command. The canonical built-ins are `stats`, `list`, `show`, and `log`; custom wrappers are convenience scripts.
7. Trusting a custom status wrapper without reading it. Direct-SQL helpers can hardcode board paths, miss statuses, and drift from Hermes semantics.
8. Treating `pid ... not alive` as the root cause. For Kanban worker crashes, read `hermes kanban log <task_id>` first; CLI startup errors such as `Unknown skill(s): ...` can kill the worker before the agent loop starts.
9. Relying on archived skills. Skill loading ignores `.archive/`; dispatch code must use the same exclusion rules before adding `--skills`.

## Verification Checklist
- [ ] Correct mode chosen: orchestrator / worker / coding lane / plan-driven subagents.
- [ ] Ownership boundaries are clear.
- [ ] Task IDs, links, and completion metadata are captured.
- [ ] Verification or review step performed before completion.
- [ ] Custom Kanban status tooling compared against built-in `hermes kanban` commands.
- [ ] User-facing reconciliation produced after parallel work.
