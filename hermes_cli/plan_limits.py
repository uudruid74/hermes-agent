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

Two truncation directions, deliberately different once; a third rule now
governs summaries (council, 2026-10-14 — nemotron preservation forensics):

* ``cap_head`` for goals and steps — the lead states the objective. Keeping
  the LAST 240 chars of a task goal would discard the task. Heads are never
  cut here either, so this already complies with the no-head-cut rule.
* ``cap_middle`` for step summaries and proof claims — the conclusion lives
  at the end AND the council found stored summaries head-cut mid-word
  (payloads showed "Summary: ork in this rerun" — B1: head-truncation), so
  the old tail-only cut is a bug. Shrinking now cuts the MIDDLE (head+tail
  kept, omission marked): "Never cut heads; budget evicts whole units; if
  shrinking, cut middle (head+tail)" (Evan, 2026-10-14).

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


def _elide_middle(text: str) -> str:
    """Cut the MIDDLE of oversized Plan text; never cut the head (B1).

    Keeps the lead (the council's "never cut heads" rule — the mid-word
    starts like "Summary: ork in this rerun" are the bug being fixed) AND
    the conclusion (the original reason summaries kept the tail). The
    omitted span is marked so the reader knows the text was deliberately
    compressed. Output stays ``<= PLAN_TEXT_MAX_CHARS``, so re-capping is a
    no-op and the function is idempotent like its siblings.
    """
    if len(text) <= PLAN_TEXT_MAX_CHARS:
        return text

    def _split(omitted: int) -> tuple[str, int, int]:
        marker = f" …[{omitted:,} chars omitted]… "
        room = max(PLAN_TEXT_MAX_CHARS - len(marker), 0)
        head = (room + 1) // 2
        return marker, head, room - head

    marker, head, tail = _split(len(text))
    # The omitted count's digit width can change once the true span is known
    # (same single-recompute pattern as _bound_summary_input).
    marker, head, tail = _split(len(text) - head - tail)
    if tail <= 0:
        tail = 1
        head = max(PLAN_TEXT_MAX_CHARS - len(marker) - tail, 0)
    out = text[:head].rstrip() + marker + text[-tail:].lstrip()
    # Final guard: a marker-width edge must never push the result past the cap.
    if len(out) > PLAN_TEXT_MAX_CHARS:
        over = len(out) - PLAN_TEXT_MAX_CHARS
        head = max(head - (over + 1) // 2, 0)
        tail = max(tail - (over // 2), 1)
        out = text[:head] + marker + text[-tail:]
    return out.strip()


def cap_text(text: Optional[str]) -> str:
    """Whitespace-compress a goal or step, then keep its first 240 chars."""
    return _elide(compress_whitespace(text or ""), keep_tail=False)


def cap_summary(text: Optional[str]) -> str:
    """Whitespace-compress a step summary or proof; shrink by cutting the middle.

    Head and tail survive (B1 trim direction, council 2026-10-14): the lead
    names the subject and the conclusion lives at the end — both are kept and
    the middle is elided with an explicit marker. The old tail-only cut left
    stored summaries starting mid-word ("Summary: ork in this rerun").
    """
    return _elide_middle(compress_whitespace(text or ""))


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
        f"the detail (plan 'new' while a Plan is active creates a child "
        f"automatically)."
    )
