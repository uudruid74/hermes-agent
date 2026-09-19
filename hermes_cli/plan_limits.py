"""Submission limits for Plan text — goal, steps, step summaries, proof claims.

Evan, 2026-09-16: *"Cap all goals, steps, summaries at 240 chars on
submission. Compress whitespace and truncate to the last 240 characters of
what the agent submits for a summary or verification claim. And no more than
12 steps (like a drunk). More steps should use subplans!"*

Why this exists: the active Plan is the **Protected** region of the context
window — it is injected at the front of the replaced region on every
compaction so it can ride the prompt cache. Nothing capped its size, so a
single step summary reached 2,285 chars on a live task and a task goal
reached 6,907. The Protected region has no budget of its own; the Heap
(sized by the low water mark) is what shrinks when the Plan grows. Capping at
submission makes the Plan's worst case a known constant —
``PLAN_MAX_STEPS * PLAN_TEXT_MAX_CHARS`` plus the goal — against a known
window, instead of a number that drifts upward with every cycle.

Two truncation directions, deliberately different:

* ``cap_head`` for goals and steps — the lead states the objective. Keeping
  the LAST 240 chars of a task goal would discard the task.
* ``cap_tail`` for step summaries and proof claims — the conclusion lives at
  the end. This is Evan's explicit rule for these two fields.

Every function is idempotent: capping already-capped text is a no-op. That is
what makes it safe to apply at both the adapter (the agent's submission path)
and the kernel (the durable write) without double-truncating.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

#: Maximum characters for a goal, a step, a step summary, or a proof claim.
PLAN_TEXT_MAX_CHARS = 240

#: Maximum number of steps in one Plan. More than this should use subplans.
PLAN_MAX_STEPS = 12

_WHITESPACE_RE = re.compile(r"\s+")


def compress_whitespace(text: str) -> str:
    """Collapse every whitespace run to a single space and strip the ends.

    Applies to all Plan text, not just the truncated fields: a summary padded
    with newlines spends Protected-region characters on layout.
    """
    if not text:
        return ""
    return _WHITESPACE_RE.sub(" ", text).strip()


def _elide(text: str, *, keep_tail: bool) -> str:
    if len(text) <= PLAN_TEXT_MAX_CHARS:
        return text
    # Strip after cutting: a head cut can land on a space (leaving a trailing
    # one) and a tail cut can land after one (leaving a leading one).  Without
    # this, capping already-capped text would shave another character and the
    # function would not be idempotent — which matters because the cap is
    # applied at both the adapter and the kernel.
    if keep_tail:
        return text[-PLAN_TEXT_MAX_CHARS:].strip()
    return text[:PLAN_TEXT_MAX_CHARS].strip()


def cap_text(text: Optional[str]) -> str:
    """Whitespace-compress a goal or step, then keep its first 240 chars."""
    return _elide(compress_whitespace(text or ""), keep_tail=False)


def cap_summary(text: Optional[str]) -> str:
    """Whitespace-compress a step summary or proof, keep its LAST 240 chars."""
    return _elide(compress_whitespace(text or ""), keep_tail=True)


def cap_steps(steps: Optional[Iterable[str]]) -> list[str]:
    """Whitespace-compress and head-cap every step in a Plan."""
    return [cap_text(step) for step in (steps or [])]


def text_limit_error(field: str, text: Optional[str]) -> Optional[str]:
    """Return an instructive error when ``text`` exceeds the character cap.

    A hard REFUSAL, not a silent truncation (Evan, 2026-09-18).  The cap was
    previously applied by truncating at submission, which produced a
    destructive failure that looked like amnesia: a 7,038-char dispatch brief
    was cut to 246 chars and stored that way, so the worker never received the
    instructions and spent 53 tool calls hunting for a body that did not exist.

    Truncation is silent data loss on the one field that carries the work.
    Refusing tells the caller immediately, while it still has the full text and
    can restructure — which is what actually happened: the brief was rewritten
    as a file the worker reads, and the goal fit under the cap.

    Whitespace is compressed before measuring, so the check matches exactly what
    ``cap_text``/``cap_summary`` would have stored.
    """
    compact = compress_whitespace(text or "")
    if len(compact) <= PLAN_TEXT_MAX_CHARS:
        return None
    return (
        f"ERROR: {field} is {len(compact)} chars; the limit is "
        f"{PLAN_TEXT_MAX_CHARS} (whitespace-compressed). Nothing was created — "
        f"shorten it and retry. Put the detailed brief in a FILE and reference "
        f"its path from a short goal; that is the supported way to carry long "
        f"instructions. More steps should use SUBPLANS."
    )


def goal_text_error(goal: Optional[str]) -> Optional[str]:
    """Refuse a goal over the cap (see ``text_limit_error``)."""
    return text_limit_error("'goal'", goal)


def steps_text_error(steps: Optional[Iterable[str]]) -> Optional[str]:
    """Refuse the FIRST step over the cap, naming its 1-based position."""
    for index, step in enumerate(steps or [], 1):
        error = text_limit_error(f"step {index}", step)
        if error:
            return error
    return None


def step_count_error(steps: Optional[Iterable[str]]) -> Optional[str]:
    """Return an instructive error when a Plan exceeds ``PLAN_MAX_STEPS``.

    A hard refusal, not a silent truncation: dropping steps would delete work
    the caller asked for. The message points at subplans, which is the
    supported way to go deeper.
    """
    count = len(list(steps or []))
    if count <= PLAN_MAX_STEPS:
        return None
    return (
        f"ERROR: a Plan may have at most {PLAN_MAX_STEPS} steps "
        f"(got {count}). More steps should use SUBPLANS — create this Plan "
        f"with {PLAN_MAX_STEPS} or fewer steps, then nest a child Plan for "
        f"the detail (plan_tool 'new' while a Plan is active creates a child "
        f"automatically)."
    )
