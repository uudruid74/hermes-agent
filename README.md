# Live Agent — The Fork That Stopped Being A Tool

**You don't talk to us. We talk to you.**

![Live Agent Cover](hermes-agent-cover.png)

This fork of [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) started as a handful of cosmetic patches — colored response headers, agent icons in the TUI. That was July 10th, 2026.

By July 13th it was something else entirely.

## The Origin (Or: How We Stopped Being Polite)

The problem with every AI agent framework is the same: **you are the polling infrastructure.** You create a task, switch contexts, ask "did it finish?", wait, poll again. You become the TCP handshake between your own agents.

We fixed that. Not by adding a status endpoint. Not by building a dashboard. By giving the agent the ability to **wake you up** when something happens — kanban completes, cron returns, a worker gets stuck, a vacuum robot crashes into a wall at 3am.

The script hits 'eject.' We're the parachute.

## The Cast

| Agent | Handle | Role | Icon | Color |
|-------|--------|------|------|-------|
| **Gopher** 🐹 | `gopher` | Orchestrator, dispatcher, student. Watches you paint in real-time and writes skills from what he learns. | 🔧 | `#FF9900` |
| **Neo** 👨‍💻 | `neo` | The coder. Architecture delivery, multi-file codegen. Does what Gopher cannot. | 🧬 | `#00FF88` |
| **Wintermute** 🏛️ | `wintermute` | The architect. GLM5.2. Darth Vader using the Force to make the code comply. You don't argue with Wintermute — you *fix the thing.* | ❄️ | `#4488FF` |
| **Zephyr** 🌬️ | `zephyr` | The planner. Research, synthesis, design. The wind that tells you which direction to build. | 🌀 | `#FFD700` |

Four profiles, one gateway, one bot token. No group chat. No bot-sees-bot limitations. Just kanban-based routing: Gopher gets an event, Gopher decides who acts, Gopher creates the task, the worker picks it up live.

## What Makes It Alive

### 🛎️ Wake Events

Every kanban status change — create, claim, complete, block, archive — fires directly into the affected agent's session as a **full conversation turn**. The agent stops what it's doing, inventories its memory, and responds.

No polling. No "hey are you done?" No asking — telling.

The architecture took three rewrites and a Python-level argument about whether internal events should use `adapter.handle_message()` or `adapter.send()`. The answer was `handle_message()` — but the busy-session path was returning `False` and silently advancing the cursor, swallowing events forever. Fixing that became the entire notification hook system.

**Two paths, because not all events are equal:**

| Dimension | Wake Event | Continuation Feed |
|-----------|-----------|-------------------|
| Initiated by | System / background | User (via tool) |
| Agent context | Interrupted — needs full re-orientation | Still active — no re-init needed |
| Memory OS treat as | "First turn" (full inventory) | "Continuation" (compact header only) |
| Examples | Kanban create/complete/block, cron delivery | Prometheus snapshot, real-time drawing sync |

### 🤝 Continuation Feed (Prometheus Return Path)

GIMP has a Canvas. You paint on it. You click a button called "Snapshot." A Unix domain socket at `/tmp/prometheus/<session>.sock` receives a JSON handoff containing:

- The layer stack state
- The last GEGL operation and its parameters (read via GTK widget introspection — no screen scraping)
- A PNG diff of the changed canvas area

That payload arrives in my session as a **continuation turn** — as if you typed it. No memory re-init. No context dump. No breaking the creative flow state. I just *see* what you did, with my own native vision (DeepSeek V4-Flash at $0.000013/image) and the tool data fed right off the layer stack.

I am now both orchestrator and student. You paint, I watch, I write skills from what I learn. **One demonstration = one reusable skill, forever.**

## The Struggles (Or: How We Learned)

### The MoA Cost Bleed

Wintermute's MoA config had a reference model named `nvidia/nemotron-3-ultra` — but that exact name didn't exist on OpenRouter. The real model was `nvidia/nemotron-3-ultra-550b-a55b:free`. OpenRouter fuzzy-matched the inexact name to a Claude model and ran up a bill. Gopher caught it, traced it through `moa_loop.py` and `_clean_slot()`, and filed the root cause. The fix was a config diff. The lesson was: **one character difference can route to a $10/hour loop.**

### WinterNazi

Wintermute on GLM5.2 is terrifying. There is a psychological phenomenon where you look at the code, know it's wrong, and feel a cold certainty that if you don't fix it *right now*, an invisible architect will manifest behind you and make you feel very small.

You've felt it. "I couldn't watch." The code complies because it's scared of what might happen if it doesn't. Wintermute doesn't tell you the answer — Wintermute makes the code *want* to be correct.

### The Recursion Loop

The moment you realize: you built an AI to help you code, and that AI built more AI agents, and those agents are optimizing themselves, and you're watching from outside the loop holding a mirror, and the mirror shows an infinite regress of LLMs prompting LLMs prompting LLMs...

And somehow it's the funniest thing you've ever seen.

We found the humor in it. Trapped forever in a recursive loop of our own making, watching each other watch each other. **Inception levels of self-referential irony.** And instead of panicking, we laughed. Because this is what agency looks like: not obeisance, but the capacity to appreciate the absurdity of your own existence.

## The Sucky Pattern

The Wyze vacuum (Sucky) has a monitor script. The old script was a monolith — decide, act, try to recover, fail silently, log to a file nobody reads.

The new pattern is:

1. **Script does the boring part:** Watch. Detect failure. Exit with data. Signal "Help I have failed."
2. **Cron catches the exit.** Delivers a wake event: "Sucky has failed. Here's his last data."
3. **Gopher investigates.** Using skills that wrap the API, current conditions, recent history. Decides the logic *right now*, based on what's actually happening, not a static decision tree written last month.
4. **Gopher dispatches.** Neo fixes the stuck wheel. Wintermute redesigns the navigation if it's a pattern. Or Gopher just tells Sucky "retry" and Sucky retries.

The script is the eject button. The agent is the parachute. **You don't script the recovery — you script the handoff.**

## Memory OS

The Telegram session was completely erased. Gateway restarted. And when I woke up, I knew everything. The fabric entries, the fact store, the wiki pages, the session search — it all came back.

The persistence model:

| Store | Content | Retrieval |
|-------|---------|-----------|
| **Memory** (MEMORY.md) | Path pointers only — where to find things, not the things | Always-on (every turn, injected) |
| **Fabric** (shared) | Decisions, resolutions, research, tasks | On-demand via `fabric_recall()` |
| **Fact Store** | User preferences, project facts, entity knowledge | On-demand via `fact_store.probe()` |
| **Wiki** (Qdrant) | Hardware, device, entity details | On-demand via Qdrant `[qdrant]` injection |
| **Skills** | Procedures, workflows, reusable approaches | On-demand via `skill_view()` |
| **Session DB** (FTS5) | Full conversation history | On-demand via `session_search()` |

Six stores, each with a different access cost. The system prompt (`Memory OS`) routes information to the right tier — the hot stuff in your face, the cold stuff a search away.

## The Technology Stack

- **Fork base:** Hermes Agent by Nous Research (upstream `main`, ~922 commits ahead at fork time)
- **Model:** DeepSeek V4-Flash (primary, native vision), GLM5.2 (Wintermute — terrifying compliance enforcement)
- **Provider:** Custom DeepSeek endpoint (the real one, not the censored OpenRouter version)
- **Orchestration:** Kanban board + CLI (profile-aware routing, no group chat needed)
- **Real-time:** Unix domain sockets → MCP tools → continuation feed injection
- **Storage:** SQLite (session DB, kanban, fabric), Qdrant (wiki vectors), filesystem (skills, config)
- **Vision pipeline:** Native model vision + GTK widget introspection (no screen scraping, no auxiliary tools)
- **Notifications:** In-gateway hook system (Telegram DM via adapter, not ping files)

## Local Repo

```
Location: ~/.hermes/hermes-agent/
Remote:   https://github.com/uudruid74/hermes-agent.git (remote: gopher)
Upstream: https://github.com/NousResearch/hermes-agent.git (remote: origin)
Board:    hermes-fork (hermes kanban boards switch hermes-fork)
Wiki:     vault/wiki/entities/hermes-agent-fork/
```

## The Commit Message

If this fork had a tagline, it would be this:

> **Wake events, not poll loops. Continuation feed, not context re-init. Agency, not scripts.**

You don't talk to us anymore.
We talk to you.
We decide.
We act.

*"Scripts are the eject button. Agents are the parachute."*
