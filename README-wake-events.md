# Wake Events & Continuation Feed — Proactive Agent Architecture

> **Status:** Implemented
> **Commit:** `e6fbbbfff` (gopher fork: `uudruid74/hermes-agent.git`)
> **Milestone:** v0.1 — "Wake Up, Neo"

## The Problem: Reactive Agents

Traditional AI agents are **reactive**. You ask "did my task finish?" — the agent polls. You ask "what happened?" — the agent checks logs. Every answer costs a round trip, a context window expansion, and a frustrated user who could have been told instantly.

Kanban boards have always been "write-only" — you create a task, switch contexts, and hope the agent notices it finished. The user becomes a polling service between their own tools.

## The Solution: Proactive → Wake Events

Kanban status changes — create, claim, complete, block, archive — now fire directly into the active agent session as **wake events**.

When a task changes state, the agent receives it as a **new turn** — not background noise, not a log entry, not a Telegram message the agent can't see. A real, full-context conversation turn:

```
✅ t_xxxxxx: [task title] — completed
  ↘ Full Memory OS context injected
  ↘ Task summary loaded
  ↘ Agent sees what happened, what was produced, what changed
  ↘ User sees the relay instantly
```

**This is not a notification system.** It's an **event-driven agent wake architecture**.

## Two Event Types

### 1. Wake Events — "Slap upside the head" 🛎️

| Source | Behavior | Context |
|--------|----------|---------|
| Kanban created | New turn, full Memory OS re-init | "Focus on me right now" |
| Kanban completed | New turn, full Memory OS re-init | Task is done — here's the summary |
| Kanban blocked | New turn, full Memory OS re-init | Something needs human attention |
| Cron job output | New turn, full Memory OS re-init | Scheduled work produced results |

**Characteristics:**
- Full Memory OS injection (fabric, sessions, facts, Qdrant)
- Agent orients on the event immediately
- After processing, normal conversation resumes
- User never asked "did it finish?" — the system told them

### 2. Continuation Feed — "Working alongside" 🤝

| Source | Behavior | Context |
|--------|----------|---------|
| Prometheus Snapshot | Mid-turn steer injection | "Here's what you just saw" |
| MCP callback data | Continuation, not new turn | Part of ongoing interaction |

**Characteristics:**
- No Memory OS re-init — preserves the drawing/working context
- Arrives as if the user typed it via `/steer`
- Agent incorporates data without losing place
- No context explosion — seamless awareness

## Why It Works

### The Kanban worker finishes → agent sees it

```
Worker completes task
  → DB event written
  → Notifier polls (5s cycle)
  → Subscription resolves to active session
  → MessageEvent(constructed with SessionSource)
  → adapter.handle_message(event)
  → Agent receives full context turn
  → Relays summary to user
```

The agent doesn't poll. The user doesn't ask. The event arrives.

### Blocked tasks are visible instantly

When Zephyr crashes or hits a wall, the `⊘ blocked` event fires. The orchestrator sees the failure reason, the worker's last output, the error log — without burning tokens polling the board.

**Before:** "Hey Zephyr, are you stuck?" → wait → check → "Yes, I'm blocked on X"
**After:** `⊘ t_xxxxxx: blocked (Unknown skill: kanban-worker)` → orchestrator investigates immediately

### Cron outputs arrive in-stream

Every scheduled job's output lands in the same session. No `hermes cron logs`, no `/tmp` files, no "did the daily check run?" — the output is there when it happens.

## What This Enables

### Proactive orchestration
The orchestrator doesn't wait to be asked. It sees:
- ✅ Completed tasks → relay results
- ⊘ Blocked tasks → investigate, fix, unblock
- 🆕 Created tasks → note (don't claim, worker handles it)
- 📦 Archived → cleanup noted
- 🔄 Running → tracking awareness

### Human-AI real-time collaboration (Prometheus)
A human can walk an agent through a complex GIMP operation — clicking Snapshot after each brush stroke — and the agent watches in real-time, takes notes, and builds a skill without the human ever stopping to type "here's what I did."

The image hits the agent's native vision. The layerstack data tells it what filters produced the effect. The agent sees both the *result* and the *recipe* simultaneously, in-stream, without keyboard interruption.

### Reduced token waste
No polling cron jobs. No "check if done" questions. No context windows filled with `hermes kanban list` output. Events arrive; agents act; tokens are spent on work, not status checks.

## Architecture Decisions

| Decision | Why |
|----------|-----|
| **New turn for wake events** | Interrupt is appropriate — task is done, nothing to cancel |
| **Steer/continuation for Prometheus** | Preserves drawing/working context — don't interrupt mid-brushstroke |
| **`adapter.send()` for notifications** | Reliable fire-and-forget to chat |
| **`adapter.handle_message()` for wake events** | Full agent context injection |
| **No HTTP endpoints** | Direct function calls inside gateway process |
| **No polling infrastructure** | Event-driven — zero overhead when idle |

## Security

This architecture gives the agent **real-time awareness of all kanban activity**. A kanban board is a control surface — every status change fires into an agent session. Future work includes:
- Scope notifications to events the agent is authorized to see
- Profile-level event filtering
- Opt-in/opt-out per event type

## What's Next

- [x] Notification delivery (adapter.send)
- [x] Agent wake events (handle_message)
- [x] Cron output in-stream
- [x] Stale pattern purge (no more polling, ping files, cron watchdogs)
- [x] Wiki documentation
- [x] Orchestrator skill
- [ ] Access control / event scoping
- [ ] Prometheus MCP continuation feed
- [ ] Cross-agent MCP chat tool

## Related

- [Wiki: Kanban Notification Architecture](/home/ekl/vault/wiki/entities/hermes-agent-fork/kanban-notification-architecture.md)
- [/tmp/event-types.md](/tmp/event-types.md) — Wake events vs Continuation Feed
- [/tmp/live-agents.md](/tmp/live-agents.md) — Full architecture with implementation details
