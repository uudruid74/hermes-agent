# Wake Events

This fork wakes you up when something happens instead of making you check.

## What's Different

Kanban boards have always been **write-only** — you create a task, switch contexts, hope the agent notices it finished, ask "did it finish?", wait, poll again. **You became the polling infrastructure between your own agents.**

We fixed that. Every kanban status change — create, claim, complete, block, archive — now fires directly into the active agent session as a full conversation turn. The agent sees it immediately. The user never asks "did it finish?" The system just tells you.

Two paths, because not all events are equal:

| Event Type | Behavior |
|------------|----------|
| 🛎️ **Wake Events** (kanban, cron) | New turn, full context — your agent is interrupted and told what happened |
| 🤝 **Continuation Feed** (Prometheus) | Steer injection — mid-turn data, no context loss |

Wake events for things that are *done*. Continuation for things still *in progress*.

## The Blocked Task Problem

Workers fail silently. When an agent hits a missing skill, runs out of budget, or finds a contradiction — **you don't know until you check**.

The orchestrator sees blocked events immediately. Failure reason, worker logs, everything. No polling. No "hey are you stuck?" The failure is visible the moment it happens.

## What's Included

- Notification delivery — reliable, fire-and-forget status updates
- Agent wake events — full context injection per status change
- Cron output delivered in-stream — no more checking logs
- Orchestrator response skill — templates for every event type
- All stale patterns removed — no ping files, no cron watchdogs, no `/tmp` exchange

## Tag

`v0.1-wake-events` — "Wake Up, Neo"
