---
name: systematic-debugging
description: "4-phase root cause debugging: understand bugs before fixing."
version: 1.1.0
author: Hermes Agent (adapted from obra/superpowers)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [debugging, troubleshooting, problem-solving, root-cause, investigation]
    related_skills: [test-driven-development, plan, plan-driven-subagent-execution]
---
# Systematic Debugging

## Overview

Random fixes waste time and create new bugs. Quick patches mask underlying issues.

**Core principle:** ALWAYS find root cause before attempting fixes. Symptom fixes are failure.

**Violating the letter of this process is violating the spirit of debugging.**

## The Iron Law

```
NO FIXES WITHOUT ROOT CAUSE INVESTIGATION FIRST
```

## When to Use

Use for ANY technical issue:
- Test failures
- Bugs in production
- Unexpected behavior
- Performance problems
- Build failures
- Integration issues

**Use this ESPECIALLY when:**
- Under time pressure (emergencies make guessing tempting)
- "Just one quick fix" seems obvious
- You've already tried multiple fixes
- Previous fix didn't work
- You don't fully understand the issue

- Issue seems simple (simple bugs have root causes too)
- You're in a hurry (rushing guarantees rework)
- Someone wants it fixed NOW (systematic is faster than thrashing)

## The Four Phases

You MUST complete each phase before proceeding to the next.

---

## Phase 1: Root Cause Investigation

**BEFORE attempting ANY fix:**

### 1. Read Error Messages Carefully

- Don't skip past errors or warnings
- They often contain the exact solution
- Read stack traces completely
- Note line numbers, file paths, error codes

**Action:** Use `read_file` on the relevant source files. Use `search_files` to find the error string in the codebase.

### 2. Reproduce Consistently

- Can you trigger it reliably?
- What are the exact steps?
- Does it happen every time?
- If not reproducible → gather more data, don't guess

**Action:** Use the `terminal` tool to run the failing test or trigger the bug:

```bash
# Run specific failing test
pytest tests/test_module.py::test_name -v

# Run with verbose output
pytest tests/test_module.py -v --tb=long
```

### 3. Check Recent Changes

- What changed that could cause this?
- Git diff, recent commits
- New dependencies, config changes

```bash
# Recent commits
git log --oneline -10

# Uncommitted changes
git diff

# Changes in specific file
git log -p --follow src/problematic_file.py | head -100
```

### 4. Gather Evidence in Multi-Component Systems

**WHEN system has multiple components (API → service → database, CI → build → deploy):**

**BEFORE proposing fixes, add diagnostic instrumentation:**

For EACH component boundary:
- Log what data enters the component
- Log what data exits the component
- Verify environment/config propagation
- Check state at each layer

Run once to gather evidence showing WHERE it breaks.
THEN analyze evidence to identify the failing component.
THEN investigate that specific component.

### 5. Trace Data Flow

**WHEN error is deep in the call stack:**

- Where does the bad value originate?
- What called this function with the bad value?
- Keep tracing upstream until you find the source
- Fix at the source, not at the symptom

**Action:** Use `search_files` to trace references:

```python
# Find where the function is called
search_files("function_name(", path="src/", file_glob="*.py")

# Find where the variable is set
search_files("variable_name\\s*=", path="src/", file_glob="*.py")
```

### 5b. Trace Event-Driven / Streaming Pipelines

**WHEN data flows through events, streaming deltas, message queues, or cross-process channels** — the standard function-call trace doesn't work because components live in different processes or fire asynchronously.

**Approach: Map the full pipeline before debugging any single stage.**

1. **List every transformation step** from source to sink, noting process boundaries (model → gateway → desktop/TUI app → renderer).
2. **Check each stage's data contract:** What shape is the input? What shape is the output? Is the stage stateful or stateless? Does it assume complete input?
3. **Isolate side-of-failure first** — determine if the corruption happens server-side (before the event is emitted) or client-side (after the UI receives it).
4. **Check accumulation logic** — streaming text is built up from deltas. Each accumulation point (buffer, concatenation, ref) is a potential corruption site.
5. **Check every regex transform** — regexes written for complete text can over-match or drop content when fed partial streaming deltas.

See `references/streaming-pipeline-debugging.md` for the full technique with a concrete worked example (garbled assistant output traced through the Hermes streaming pipeline).

**Browser printing:** See `references/browser-printing-linux.md` for the diagnosis workflow when Brave/Chrome silently fails to print — no CUPS job, no error, no log. The fix chain is: disable new PDF viewer flag → disable print preview flag → disable GPU rasterization.

### Phase 1 Completion Checklist

- [ ] Error messages fully read and understood
- [ ] Issue reproduced consistently
- [ ] Recent changes identified and reviewed
- [ ] Evidence gathered (logs, state, data flow)
- [ ] Problem isolated to specific component/code
- [ ] Root cause hypothesis formed

**STOP:** Do not proceed to Phase 2 until you understand WHY it's happening.

---

## Phase 2: Pattern Analysis

**Find the pattern before fixing:**

### 1. Find Working Examples

- Locate similar working code in the same codebase
- What works that's similar to what's broken?

**Action:** Use `search_files` to find comparable patterns:

```python
search_files("similar_pattern", path="src/", file_glob="*.py")
```

### 2. Compare Against References

- If implementing a pattern, read the reference implementation COMPLETELY
- Don't skim — read every line
- Understand the pattern fully before applying

### 3. Identify Differences

- What's different between working and broken?
- List every difference, however small
- Don't assume "that can't matter"

### 4. Understand Dependencies

- What other components does this need?
- What settings, config, environment?
- What assumptions does it make?

---

## Phase 3: Hypothesis and Testing

**Scientific method:**

### 1. Form a Single Hypothesis

- State clearly: "I think X is the root cause because Y"
- Write it down
- Be specific, not vague

### 2. Test Minimally

- Make the SMALLEST possible change to test the hypothesis
- One variable at a time
- Don't fix multiple things at once

### 3. Verify Before Continuing

- Did it work? → Phase 4
- Didn't work? → Form NEW hypothesis
- DON'T add more fixes on top

### 4. When You Don't Know

- Say "I don't understand X"
- Don't pretend to know
- Ask the user for help
- Research more

---

## Phase 4: Implementation

**Fix the root cause, not the symptom:**

### 1. Create Failing Test Case

- Simplest possible reproduction
- Automated test if possible
- MUST have before fixing
- Use the `test-driven-development` skill

### 2. Implement Single Fix

- Address the root cause identified
- ONE change at a time
- No "while I'm here" improvements
- No bundled refactoring

### 3. Verify Fix

```bash
# Run the specific regression test
pytest tests/test_module.py::test_regression -v

# Run full suite — no regressions
pytest tests/ -q
```

### 4. If Fix Doesn't Work — The Rule of Three

- **STOP.**
- Count: How many fixes have you tried?
- If < 3: Return to Phase 1, re-analyze with new information
- **If ≥ 3: STOP and question the architecture (step 5 below)**
- DON'T attempt Fix #4 without architectural discussion

### 5. If 3+ Fixes Failed: Question Architecture

- Each fix reveals new shared state/coupling in a different place
- Fixes require "massive refactoring" to implement
- Each fix creates new symptoms elsewhere

**STOP and question fundamentals:**
- Is this pattern fundamentally sound?
- Are we "sticking with it through sheer inertia"?
- Should we refactor the architecture vs. continue fixing symptoms?

**Discuss with the user before attempting more fixes.**

This is NOT a failed hypothesis — this is a wrong architecture.

---

## Red Flags — STOP and Follow Process

- "Quick fix for now, investigate later"
- "Just try changing X and see if it works"
- "Add multiple changes, run tests"
- "Skip the test, I'll manually verify"
- "It's probably X, let me fix that"
- "I don't fully understand but this might work"
- "Pattern says X but I'll adapt it differently"
- "Here are the main problems: [lists fixes without investigation]"
- Proposing solutions before tracing data flow
- **"One more fix attempt" (when already tried 2+)**
- **Each fix reveals a new problem in a different place**

**ALL of these mean: STOP. Return to Phase 1.**

**If 3+ fixes failed:** Question the architecture (Phase 4 step 5).

## Common Rationalizations

| Excuse | Reality |
|--------|---------|
| "Issue is simple, don't need process" | Simple issues have root causes too. Process is fast for simple bugs. |
| "Emergency, no time for process" | Systematic debugging is FASTER than guess-and-check thrashing. |
| "Just try this first, then investigate" | First fix sets the pattern. Do it right from the start. |
| "I'll write test after confirming fix works" | Untested fixes don't stick. Test first proves it. |
| "Multiple fixes at once saves time" | Can't isolate what worked. Causes new bugs. |
| "Reference too long, I'll adapt the pattern" | Partial understanding guarantees bugs. Read it completely. |
| "I see the problem, let me fix it" | Seeing symptoms ≠ understanding root cause. |
| "One more fix attempt" (after 2+ failures) | 3+ failures = architectural problem. Question the pattern, don't fix again. |

### Never declare victory on a fix you proposed until the user confirms

**Rule:** When you propose a fix and the user says anything tentative ("seems to", "might be", "let me check", "I think"), that is NOT confirmation. Do NOT close the investigation, write fabric entries, or update skills declaring the issue resolved.

**Why this matters:** Tentative feedback from a user is easy to hear as affirmation. The cost of a premature close + wrong fabric entry (which then poisons other agents' context) is far higher than the cost of waiting for an explicit "yes, this is fixed."

1. When the user says anything tentative → keep investigating other angles
2. Wait for explicit confirmation like "it's fixed", "that worked", "resolved"
3. If they say "I think we might be good" → acknowledge the possibility but DO NOT close
4. Only close the loop when you're confident AND the user confirms

- "They said 'good' so the fix worked" — no, they said "might be good"
- "My explanation convinced them" — you solved the wrong problem
- "Better to write it down in case it is fixed" — wrong data is worse than no data

### The user saying "seems like X fixed it" — hardest variant to catch

**The trap:** The user reports a change they made (new desktop client, toggled a setting) and says it "seems to have done it." This sounds like user confirmation — but the keyword is "seems." The user is reporting a HYPOTHESIS they're testing, not a CONFIRMED result.

**Concrete example from a production session:**
> User: "I think we might be good. The new desktop client cranked up the reasoning from Standard to Max and that seems to have done it"

My wrong response: immediately wrote a fabric entry declaring "Garbled output fixed by raising reasoning level to Max" and closed the case.

- "seems to have done it" was the user testing a theory, not confirming a result
- I latched onto the first plausible explanation instead of continuing to investigate
- The actual root cause (ICARUS context injection truncation) was completely different from the reasoning-effort theory
- The wrong fabric entry poisoned context until manually deleted

- The user attributes the fix to something THEY did (setting change, restart, etc.) — they're describing what they tried, not what worked
- Any hedging language ("seems", "might", "I think", "appears", "looks like", "could be") before a positive outcome claim
- The fix description is vague ("cranked up") rather than precise
- The user hasn't seen the next response yet — garbling could still be happening

1. Acknowledge the test: "Good data point"
2. Keep investigating other angles: "Let me also check X while you verify"
3. Wait for explicit "yep, fixed" or "resolved" before closing anything
4. If you don't hear back, do NOT assume the fix worked — the user would tell you if it did

### Verify at every layer before declaring done

Multi-layered systems (config.yaml + .env + SDK code + Docker + running daemon) require verification at EVERY layer before a fix is confirmed. Changing one file and assuming it works is how the Firecrawl 402 fix failed repeatedly — the config looked right but the env var was in the wrong .env file, the SDK defaulted to cloud, and the running process had stale state.

See `references/verify-every-layer.md` for the full protocol and the Firecrawl 402 case study.

### Variable scope across retry/fallback code paths

`UnboundLocalError` in a retry loop or multi-path error handler almost always means a variable is referenced in one code path but only assigned in a *later, separate* path within the same function.

**Hotspot pattern:** A long function (50+ lines) with multiple error-handling sections — eager fallback, credential pool rotation, compression recovery, retry-before-sleep. A variable is introduced and assigned in the *retry backoff / wait* section, but another section that runs *before* it (e.g., eager fallback) also references the same variable.

1. Search for all references to the variable name in the function
2. Note the line numbers — is the first reference a read or a write?
3. If the first reference is a read (not an assignment), you've found the bug
4. Trace which code paths reach that read before any assignment runs

**Fix:** Initialize the variable to `None` (or a safe default) at the top of the enclosing block — right before the *earliest* code path that references it. Even if a later section also initializes it, the earlier path needs its own initialization.

**Example:** In `conversation_loop.py`, `_retry_after` was set to `None` in the retry-backoff section (line ~3435), but the eager-fallback path (line ~2785) used it in a `_try_activate_fallback(retry_after_seconds=_retry_after)` call. Fix: initialize `_retry_after = None` right before the eager-fallback block.

## Quick Reference

| Phase | Key Activities | Success Criteria |
|-------|---------------|------------------|
| **1. Root Cause** | Read errors, reproduce, check changes, gather evidence, trace data flow | Understand WHAT and WHY |
| **2. Pattern** | Find working examples, compare, identify differences | Know what's different |
| **3. Hypothesis** | Form theory, test minimally, one variable at a time | Confirmed or new hypothesis |
| **4. Implementation** | Create regression test, fix root cause, verify | Bug resolved, all tests pass |

## Hermes Agent Integration

### Investigation Tools

Use these Hermes tools during Phase 1:

- **`search_files`** — Find error strings, trace function calls, locate patterns
- **`read_file`** — Read source code with line numbers for precise analysis
- **`terminal`** — Run tests, check git history, reproduce bugs
- **`web_search`/`web_extract`** — Research error messages, library docs

### With delegate_task

For complex multi-component debugging, dispatch investigation subagents:

```python
delegate_task(
    goal="Investigate why [specific test/behavior] fails",
    context="""
    Follow systematic-debugging skill:
    1. Read the error message carefully
    2. Reproduce the issue
    3. Trace the data flow to find root cause
    4. Report findings — do NOT fix yet

    Error: [paste full error]
    File: [path to failing code]
    Test command: [exact command]
    """,
    toolsets=['terminal', 'file']
)
```

### With test-driven-development

When fixing bugs:
1. Write a test that reproduces the bug (RED)
2. Debug systematically to find root cause
3. Fix the root cause (GREEN)
4. The test proves the fix and prevents regression

## Real-World Impact

From debugging sessions:
- Systematic approach: 15-30 minutes to fix
- Random fixes approach: 2-3 hours of thrashing
- First-time fix rate: 95% vs 40%
- New bugs introduced: Near zero vs common

**No shortcuts. No guessing. Systematic always wins.**
