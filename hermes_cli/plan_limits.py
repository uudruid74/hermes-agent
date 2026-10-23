"""Submission limits for Plan text — goal, step titles, expansions, summaries.

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
submission makes the Plan's worst case a known constant against a known
window, instead of a number that drifts upward with every cycle.

Per-field caps (settled split, Gopher tested / Evan approved 2026-10-23 —
plan-import-export-flat-file-spec): the single 240 cap is split per field,
because the fields carry different kinds of text:

* ``goal`` / global description → 2K chars (it is run-wide context now).
* ``step title`` → 128 bytes (a title; the detail lives in the expansion).
* ``step expansion`` → 2K chars, EXEMPT from the prose cap — it is spec
  ground truth and is stored verbatim (import/export feature).
* ``step summary`` → 240 chars (prose; the claim's one-line receipt).
* ``denial`` → EXEMPT (see ``execution_bindings._set_step_denial``): reviewer
  verdicts are not prose and must survive verbatim.

Truncation directions, deliberately different once; a third rule now
governs summaries (council, 2026-10-14 — nemotron preservation forensics):

* ``cap_head`` for goals and step titles — the lead states the objective.
  Keeping the LAST chars of a task goal would discard the task. Heads are
  never cut here either, so this already complies with the no-head-cut rule.
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

#: Maximum characters for a step summary or a proof claim (prose).
PLAN_TEXT_MAX_CHARS = 240

#: Maximum characters for a goal / global description (whitespace-compressed).
PLAN_GOAL_MAX_CHARS = 2048

#: Maximum UTF-8 bytes for a step title.
PLAN_STEP_TITLE_MAX_BYTES = 128

#: Maximum characters for a step expansion (spec ground truth, verbatim).
PLAN_EXPANSION_MAX_CHARS = 2048

#: Maximum number of steps in one Plan. More than this should use subplans.
PLAN_MAX_STEPS = 12

_WHITESPACE_RE = re.compile(r"\s+")


def compress_whitespace(text: str) -> str:
    """Collapse every whitespace run to a single space and strip the ends.

    Applies to prose Plan text (goals, titles, summaries): a summary padded
    with newlines spends Protected-region characters on layout. Expansions
    are spec ground truth and are NOT compressed (stored verbatim).
    """
    if not text:
        return ""
    return _WHITESPACE_RE.sub(" ", text).strip()


def _elide(text: str, *, keep_tail: bool, limit: int = PLAN_TEXT_MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    # Strip after cutting: a head cut can land on a space (leaving a trailing
    # one) and a tail cut can land after one (leaving a leading one).  Without
    # this, capping already-capped text would shave another character and the
    # function would not be idempotent — which matters because the cap is
    # applied at both the adapter and the kernel.
    if keep_tail:
        return text[-limit:].strip()
    return text[:limit].strip()


def _elide_middle(text: str, limit: int = PLAN_TEXT_MAX_CHARS) -> str:
    """Cut the MIDDLE of oversized Plan text; never cut the head (B1).

    Keeps the lead (the council's "never cut heads" rule — the mid-word
    starts like "Summary: ork in this rerun" are the bug being fixed) AND
    the conclusion (the original reason summaries kept the tail). The
    omitted span is marked so the reader knows the text was deliberately
    compressed. Output stays ``<= limit``, so re-capping is a
    no-op and the function is idempotent like its siblings.
    """
    if len(text) <= limit:
        return text

    def _split(omitted: int) -> tuple[str, int, int]:
        marker = f" …[{omitted:,} chars omitted]… "
        room = max(limit - len(marker), 0)
        head = (room + 1) // 2
        return marker, head, room - head

    marker, head, tail = _split(len(text))
    # The omitted count's digit width can change once the true span is known
    # (same single-recompute pattern as _bound_summary_input).
    marker, head, tail = _split(len(text) - head - tail)
    if tail <= 0:
        tail = 1
        head = max(limit - len(marker) - tail, 0)
    out = text[:head].rstrip() + marker + text[-tail:].lstrip()
    # Final guard: a marker-width edge must never push the result past the cap.
    if len(out) > limit:
        over = len(out) - limit
        head = max(head - (over + 1) // 2, 0)
        tail = max(tail - (over // 2), 1)
        out = text[:head] + marker + text[-tail:]
    return out.strip()


def _cap_bytes(text: str, max_bytes: int) -> str:
    """Head-cap UTF-8 text to ``max_bytes``, cutting on a char boundary."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore").strip()


def cap_text(text: Optional[str], limit: int = PLAN_TEXT_MAX_CHARS) -> str:
    """Whitespace-compress prose, then keep its first ``limit`` chars."""
    return _elide(compress_whitespace(text or ""), keep_tail=False, limit=limit)


def cap_goal(text: Optional[str]) -> str:
    """Whitespace-compress a goal / global description (2K head-cap)."""
    return cap_text(text, PLAN_GOAL_MAX_CHARS)


def cap_step_title(text: Optional[str]) -> str:
    """Whitespace-compress a step title, head-capped at 128 UTF-8 bytes."""
    return _cap_bytes(compress_whitespace(text or ""), PLAN_STEP_TITLE_MAX_BYTES)


def cap_summary(text: Optional[str]) -> str:
    """Whitespace-compress a step summary or proof; shrink by cutting the middle.

    Head and tail survive (B1 trim direction, council 2026-10-14): the lead
    names the subject and the conclusion lives at the end — both are kept and
    the middle is elided with an explicit marker. The old tail-only cut left
    stored summaries starting mid-word ("Summary: ork in this rerun").
    """
    return _elide_middle(compress_whitespace(text or ""))


def cap_expansion(text: Optional[str]) -> str:
    """Strip the ends of a step expansion and bound it to 2K chars.

    Spec ground truth: no whitespace compression, no prose cap — the text is
    stored as written (minus outer blank lines) so the run's rules survive
    verbatim (``plan import`` refuses oversized expansions at submission).
    """
    return (text or "").strip()


def cap_steps(steps: Optional[Iterable[str]]) -> list[str]:
    """Whitespace-compress and head-cap every step title in a Plan."""
    return [cap_step_title(step) for step in (steps or [])]


def cap_expansions(expansions: Optional[Iterable[str]]) -> list[str]:
    """Bound every step expansion in a Plan (verbatim otherwise)."""
    return [cap_expansion(expansion) for expansion in (expansions or [])]


def text_limit_error(
    field: str, text: Optional[str], limit: int = PLAN_TEXT_MAX_CHARS
) -> Optional[str]:
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
    if len(compact) <= limit:
        return None
    return (
        f"ERROR: {field} is {len(compact)} chars; the limit is "
        f"{limit} (whitespace-compressed). Nothing was created — "
        f"shorten it and retry. For detailed instructions use `plan import` "
        f"(a flat markdown spec carries a 2K expansion per step), or put the "
        f"brief in a FILE and reference its path from a short goal. More "
        f"steps should use SUBPLANS."
    )


def goal_text_error(goal: Optional[str]) -> Optional[str]:
    """Refuse a goal over the 2K cap (see ``text_limit_error``)."""
    return text_limit_error("'goal'", goal, PLAN_GOAL_MAX_CHARS)


def steps_text_error(steps: Optional[Iterable[str]]) -> Optional[str]:
    """Refuse the FIRST step title over the 128-byte cap, naming its position.

    Titles are measured in UTF-8 BYTES (the settled limit), not chars: a
    step title is a label, and the detail belongs in the step expansion.
    """
    for index, step in enumerate(steps or [], 1):
        encoded = (step or "").encode("utf-8")
        if len(encoded) > PLAN_STEP_TITLE_MAX_BYTES:
            return (
                f"ERROR: step {index} title is {len(encoded)} bytes; the limit "
                f"is {PLAN_STEP_TITLE_MAX_BYTES} (whitespace-compressed UTF-8). "
                f"Nothing was created — shorten it and retry. For detailed "
                f"instructions use `plan import`: each step carries a short "
                f"title plus a {PLAN_EXPANSION_MAX_CHARS}-char expansion."
            )
    return None


def expansion_text_error(expansions: Optional[Iterable[str]]) -> Optional[str]:
    """Refuse the FIRST step expansion over the 2K cap, naming its position.

    Expansions are stored verbatim (no whitespace compression), so the check
    measures the raw stripped text.
    """
    for index, expansion in enumerate(expansions or [], 1):
        size = len((expansion or "").strip())
        if size > PLAN_EXPANSION_MAX_CHARS:
            return (
                f"ERROR: step {index} expansion is {size} chars; the limit is "
                f"{PLAN_EXPANSION_MAX_CHARS}. Nothing was created — shorten it "
                f"and retry. Expansions are spec ground truth and are never "
                f"truncated."
            )
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
        f"Plan automatically)."
    )
