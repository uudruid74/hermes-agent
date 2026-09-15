"""CPU-only emergency context selection when summary providers fail."""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from collections import Counter
from typing import Any

from agent.model_metadata import estimate_messages_tokens_rough, estimate_tokens_rough


INTERNAL_FALLBACK_PREFIX = (
    "[CONTEXT WINDOW COMPRESSED]"
)
VERBATIM_CONTEXT_MARKER = (
    "## Verbatim Recent Context\n"
    "The messages after this summary marker are preserved verbatim."
)
_TOKEN_RE = re.compile(r"[\w'-]{2,}", re.UNICODE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n{2,}")
_SESSION_NOTE_RE = re.compile(
    r"(?ims)^#{1,6}\s*session notes?\s*$\n(.*?)(?=^#{1,6}\s|\Z)"
)
_PRUNED_SKILL_RE = re.compile(r"\[SKILL_PRUNED:[^\]\n]+\]")
# Ranking weights (Evan, 2026-09-15): centrality + relevance + recency.
# Recency is an ADDITIVE third term, not a reweighting of relevance — a
# recency *multiplier* on relevance tested worse (66.6% vs 69.2% coverage)
# because it suppresses a genuinely central unit that merely sits further
# back, instead of letting position break ties between equally central ones.
_CENTRALITY_WEIGHT = 0.40
_RELEVANCE_WEIGHT = 0.40
_RECENCY_WEIGHT = 0.20
# Floor for a pruned-skill reload marker so it stays selectable when budget
# is free, without outranking real context (see the scoring loop).
_NOTE_SCORE_FLOOR = 1.25
_MAX_UNIT_CHARS = 1_200
_MAX_CANDIDATE_UNITS = 256
_ASSEMBLY_RESERVE_TOKENS = 24
# Minimum content tokens a unit must retain after boilerplate stripping to
# stay rankable.  A unit made only of wrapper text carries no context.
_MIN_UNIT_TOKENS = 2
# Boilerplate carries no context but repeats in EVERY previous payload, so
# LexRank's centrality term ranks it top — measured on a real 4,795-row
# session, the two highest-scoring "units" were the compaction wrapper and
# the verbatim marker (Evan, 2026-09-15).  Strip it before chunking.
_BOILERPLATE_RES = (
    re.compile(r"\[CONTEXT WINDOW COMPRESSED\]"),
    # ``\s`` not ``\n``: an earlier compaction already collapsed newlines to
    # spaces, so these wrappers arrive inline ("## Verbatim Recent Context
    # The messages after...") and line-anchored patterns silently miss them.
    re.compile(
        r"-{2,}\s*END OF CONTEXT SUMMARY\b[\s—–-]*"
        r"(?:respond to the message below, not the summary above)?[\s—–-]*",
        re.IGNORECASE,
    ),
    re.compile(
        r"#{1,6}\s*(?:Active Plan|Relevant Earlier Context|"
        r"Verbatim Recent Context|Session Notes|Context Summary)\b\s*:?",
        re.IGNORECASE,
    ),
    re.compile(
        r"The messages after this summary marker are preserved verbatim\.?",
        re.IGNORECASE,
    ),
    re.compile(r"\[This response was interrupted by a user correction\.\]"),
    re.compile(
        r"The tool list for this conversation has been updated accordingly\.?",
        re.IGNORECASE,
    ),
    re.compile(r"\[System note:[^\]]*\]", re.IGNORECASE),
    re.compile(r"\[OUT-OF-BAND USER MESSAGE[^\]]*\]", re.IGNORECASE),
    re.compile(r"\[/OUT-OF-BAND USER MESSAGE\]", re.IGNORECASE),
)
# ``[ROLE]: `` prefixes stack ("[USER]: [USER]: [ASSISTANT]: ") when payloads
# are re-ingested, and repeats can sit mid-text after newlines collapsed to
# spaces.  Only the leading run on a *fresh* message is ours — every other
# occurrence came from a previous payload and is wrapper noise.
_ROLE_PREFIX_RE = re.compile(r"(?:\[[A-Z_]{2,12}\]:[ \t]*)+")


@dataclass(frozen=True)
class InternalFallback:
    """Summary text and transcript boundaries selected by the fallback."""

    summary: str
    head_count: int
    tail_start: int
    mode: str


@dataclass(frozen=True)
class _Unit:
    text: str
    order: tuple[int, int]
    token_count: int
    is_note: bool = False


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return ""


def _template_visible_role(message: dict[str, Any]) -> str | None:
    role = message.get("role")
    if role == "tool" or (role == "assistant" and message.get("tool_calls")):
        return None
    return role if isinstance(role, str) else None


def _verbatim_tail_start(
    messages: list[dict[str, Any]], head_count: int, protect_last_n: int
) -> int:
    """Keep the configured number of template-visible messages verbatim."""

    visible_needed = max(protect_last_n, 0)
    tail_start = len(messages)
    visible_kept = 0
    while tail_start > head_count and visible_kept < visible_needed:
        tail_start -= 1
        if _template_visible_role(messages[tail_start]) is not None:
            visible_kept += 1
    if tail_start >= len(messages):
        return tail_start
    last_head_role = next(
        (
            role
            for role in (
                _template_visible_role(message)
                for message in reversed(messages[:head_count])
            )
            if role is not None
        ),
        None,
    )
    summary_role = "user" if last_head_role in {None, "assistant", "tool"} else "assistant"

    while tail_start > head_count:
        first_tail_role = next(
            (
                role
                for role in (
                    _template_visible_role(message) for message in messages[tail_start:]
                )
                if role is not None
            ),
            None,
        )
        if first_tail_role is None or first_tail_role != summary_role:
            break
        tail_start -= 1
    return tail_start


def _strip_boilerplate(text: str) -> str:
    """Remove compaction wrappers so they cannot be ranked as context.

    A previous payload's ``[CONTEXT WINDOW COMPRESSED]`` prefix, section
    headers and ``--- END OF CONTEXT SUMMARY`` rule recur in EVERY earlier
    payload, so they score as maximally "central" under LexRank while
    carrying no information at all.  Measured on a live session: 2 of the
    top-5 ranked units were pure wrapper.  ``_TOKEN_RE`` also counts
    underscore/bracket tokens like ``context_window_compressed`` as content
    words, so the junk survives vectorization unless it is removed first.
    """
    cleaned = text
    for pattern in _BOILERPLATE_RES:
        cleaned = pattern.sub(" ", cleaned)
    # Collapse the stacked ``[USER]: [USER]: [ASSISTANT]: `` prefixes that
    # build up as payloads are re-ingested.
    for _ in range(8):
        stripped = _ROLE_PREFIX_RE.sub("", cleaned)
        if stripped == cleaned:
            break
        cleaned = stripped
    return re.sub(r"[ \t]+", " ", cleaned).strip()


def _is_fragment(text: str) -> bool:
    """True for units with no content beyond a list marker or punctuation.

    Re-ingested payloads leave orphaned stubs behind once their wrapper and
    role prefixes are removed — ``]``, ``**2.``, ``5.`` — which carry no
    context but still consume budget and pollute the emitted block.
    """
    cleaned = _strip_boilerplate(text)
    if len(_TOKEN_RE.findall(cleaned.casefold())) >= _MIN_UNIT_TOKENS:
        return False
    return len(re.sub(r"[\W\d_]+", "", cleaned)) == 0


def _has_wrapper_marker(text: str) -> bool:
    """True when text carries an actual compaction wrapper.

    ``_message_units`` prefixes every unit with ``[ROLE]: ``, which the
    role-prefix normalizer inside ``_strip_boilerplate`` also removes — so
    comparing stripped-vs-original text cannot tell wrapper noise apart from
    ordinary content.  Test the wrapper patterns directly instead, and leave
    every unit that never matched one completely untouched.
    """
    return any(pattern.search(text) for pattern in _BOILERPLATE_RES)


def _is_boilerplate_only(text: str) -> bool:
    """True when a unit is *only* wrapper noise and nothing else.

    Deliberately narrow: the unit must carry a wrapper marker AND lose all
    content when it is removed.  Text that never matched a wrapper pattern
    (a filler run, a repeated log line) is never dropped — doing so would
    silently widen the budget and change which real units get selected.
    """
    if not _has_wrapper_marker(text):
        return False
    cleaned = _strip_boilerplate(text)
    return len(_TOKEN_RE.findall(cleaned.casefold())) < _MIN_UNIT_TOKENS


def _chunks(text: str) -> list[str]:
    text = _strip_boilerplate(text)
    chunks: list[str] = []
    for sentence in _SENTENCE_SPLIT_RE.split(text.strip()):
        sentence = re.sub(r"\s+", " ", sentence).strip()
        if not sentence:
            continue
        for start in range(0, len(sentence), _MAX_UNIT_CHARS):
            chunk = sentence[start : start + _MAX_UNIT_CHARS].strip()
            if chunk:
                chunks.append(chunk)
    return chunks


_MASKED_OBSERVATION = "[observation omitted — outside the last turn]"


def _is_observation(message: dict[str, Any]) -> bool:
    """True for environment observations (tool results).

    Observations are the verbose, disposable half of a turn.  They are kept
    verbatim inside the protected tail and masked everywhere else — they are
    never eligible for the lexrank middle and never feed the relevance
    centroid (Evan, 2026-09-14).
    """
    return message.get("role") == "tool"


def _message_units(
    messages: list[dict[str, Any]],
    start_index: int = 0,
    *,
    include_observations: bool = False,
) -> list[_Unit]:
    """Chunk messages into ranking units.

    ``order`` always carries the *true* message offset, so skipping messages
    cannot misalign the emitted summary against the transcript.  Observations
    are skipped by default: they must never be lexrank-injected into the
    compressed middle, nor bias the relevance centroid.
    """
    units: list[_Unit] = []
    for message_offset, message in enumerate(messages):
        if not include_observations and _is_observation(message):
            continue
        role = str(message.get("role") or "unknown").upper()
        for unit_offset, chunk in enumerate(_chunks(_content_text(message.get("content")))):
            text = f"[{role}]: {chunk}"
            units.append(
                _Unit(
                    text=text,
                    order=(start_index + message_offset, unit_offset),
                    token_count=estimate_tokens_rough(text),
                )
            )
    return units


def _mask_tail_observations(
    messages: list[dict[str, Any]], tail_start: int
) -> list[dict[str, Any]]:
    """Mask tail observations back to the last turn (the emergency rung).

    Keeps the observations of the newest assistant turn verbatim — the active
    work stays readable — and replaces every older observation inside the
    protected tail with a placeholder.  Reasoning, actions and user turns are
    never touched, so the tail stays contiguous and a later ``_fit_tail_start``
    can still re-measure it.

    Returns a new list; the caller's transcript is never mutated.
    """
    if tail_start >= len(messages):
        return messages

    tail = messages[tail_start:]
    last_turn_start: int | None = None
    for offset, message in enumerate(tail):
        if message.get("role") == "assistant":
            last_turn_start = offset

    if last_turn_start is None:
        keep_from = len(tail)
    else:
        keep_from = last_turn_start

    masked = list(messages[:tail_start])
    for offset, message in enumerate(tail):
        if offset < keep_from and _is_observation(message):
            replacement = dict(message)
            replacement["content"] = _MASKED_OBSERVATION
            masked.append(replacement)
        else:
            masked.append(message)
    return masked


def _session_notes(
    messages: list[dict[str, Any]],
    memory_context: str,
    previous_summary: str,
) -> list[str]:
    candidates: list[str] = []
    if memory_context and memory_context.strip():
        candidates.append(memory_context.strip())
    for source in [previous_summary, *[_content_text(msg.get("content")) for msg in messages]]:
        if not source:
            continue
        candidates.extend(_PRUNED_SKILL_RE.findall(source))
        candidates.extend(match.strip() for match in _SESSION_NOTE_RE.findall(source))

    notes: list[str] = []
    seen: set[str] = set()
    prioritized = sorted(
        candidates,
        key=lambda note: 0 if _PRUNED_SKILL_RE.fullmatch(note.strip()) else 1,
    )
    for candidate in prioritized:
        normalized = re.sub(r"\s+", " ", candidate).strip()
        if len(normalized) > _MAX_UNIT_CHARS:
            normalized = normalized[:_MAX_UNIT_CHARS].rstrip() + "…"
        if normalized and normalized not in seen:
            seen.add(normalized)
            notes.append(normalized)
            if len(notes) >= _MAX_CANDIDATE_UNITS:
                break
    return notes


def _tfidf_vectors(texts: list[str]) -> list[dict[str, float]]:
    token_counts = [Counter(_TOKEN_RE.findall(text.casefold())) for text in texts]
    document_frequency: Counter[str] = Counter()
    for counts in token_counts:
        document_frequency.update(counts.keys())

    document_count = len(texts)
    vectors: list[dict[str, float]] = []
    for counts in token_counts:
        vector = {
            token: count * (math.log((1 + document_count) / (1 + document_frequency[token])) + 1)
            for token, count in counts.items()
        }
        norm = math.sqrt(sum(value * value for value in vector.values()))
        vectors.append(
            {token: value / norm for token, value in vector.items()} if norm else {}
        )
    return vectors


def _cosine(left: dict[str, float], right: dict[str, float]) -> float:
    if len(left) > len(right):
        left, right = right, left
    return sum(value * right.get(token, 0.0) for token, value in left.items())


def _centroid(vectors: list[dict[str, float]]) -> dict[str, float]:
    if not vectors:
        return {}
    result: dict[str, float] = {}
    for vector in vectors:
        for token, value in vector.items():
            result[token] = result.get(token, 0.0) + value / len(vectors)
    norm = math.sqrt(sum(value * value for value in result.values()))
    return {token: value / norm for token, value in result.items()} if norm else {}


def _recency_scores(units: list[_Unit]) -> list[float]:
    """Position of each unit relative to the live work, normalized to 0..1.

    ``_Unit.order`` is ``(message_offset, chunk_offset)`` in true transcript
    coordinates, so the newest candidate sits one row behind the protected
    tail — i.e. immediately before the live turn.  1.0 means "adjacent to
    the current work", 0.0 means "oldest row in the compressible middle".

    Normalizing across the candidate span (rather than against an absolute
    row count) keeps the signal meaningful on both a 20-unit middle and a
    2,000-unit one.  Chunk offset is used only to order units within a
    single message; it never outranks a whole message of recency.
    """
    if not units:
        return []
    orders = [unit.order for unit in units]
    newest = max(orders)
    oldest = min(orders)
    if newest == oldest:
        return [1.0] * len(units)
    span = newest[0] - oldest[0]
    if span <= 0:
        # Every candidate is a chunk of one message: fall back to chunk order.
        chunk_span = max(1, newest[1] - oldest[1])
        return [(order[1] - oldest[1]) / chunk_span for order in orders]
    return [(order[0] - oldest[0]) / span for order in orders]


def _lexrank(vectors: list[dict[str, float]], threshold: float = 0.1) -> list[float]:
    count = len(vectors)
    if not count:
        return []
    if count > _MAX_CANDIDATE_UNITS:
        sampled_indices = [
            index * (count - 1) // (_MAX_CANDIDATE_UNITS - 1)
            for index in range(_MAX_CANDIDATE_UNITS)
        ]
        sampled_scores = _lexrank(
            [vectors[index] for index in sampled_indices],
            threshold=threshold,
        )
        scores = [0.0] * count
        for index, score in zip(sampled_indices, sampled_scores):
            scores[index] = score
        return scores

    graph: list[list[float]] = [[0.0] * count for _ in range(count)]
    for left in range(count):
        for right in range(left + 1, count):
            similarity = _cosine(vectors[left], vectors[right])
            if similarity >= threshold:
                graph[left][right] = similarity
                graph[right][left] = similarity

    scores = [1.0 / count] * count
    damping = 0.85
    for _ in range(30):
        updated = [(1.0 - damping) / count] * count
        for source, edges in enumerate(graph):
            edge_total = sum(edges)
            if edge_total:
                for target, weight in enumerate(edges):
                    if weight:
                        updated[target] += damping * scores[source] * weight / edge_total
            else:
                share = damping * scores[source] / count
                for target in range(count):
                    updated[target] += share
        if max(abs(updated[index] - scores[index]) for index in range(count)) < 1e-8:
            scores = updated
            break
        scores = updated

    maximum = max(scores)
    return [score / maximum for score in scores] if maximum else [0.0] * count


def _fits(
    messages: list[dict[str, Any]],
    head_count: int,
    tail_start: int,
    summary: str,
    target_tokens: int,
) -> bool:
    candidate = [
        *messages[:head_count],
        {"role": "assistant", "content": summary},
        *messages[tail_start:],
    ]
    return (
        estimate_messages_tokens_rough(candidate) + _ASSEMBLY_RESERVE_TOKENS
        <= target_tokens
    )


def _fit_tail_start(
    messages: list[dict[str, Any]],
    head_count: int,
    tail_start: int,
    summary: str,
    target_tokens: int,
) -> int:
    """Advance *tail_start* forward until the assembled payload fits.

    ``_verbatim_tail_start`` answers "which messages does the template
    display as the recent conversation" — it walks back until it has seen
    ``protect_last_n`` *visible* rows, and tool results / assistant
    tool-call rows are stepped over without being counted.  The index it
    returns therefore marks the start of a region that can be far larger
    than the rows the caller actually wanted to keep: on a tool-heavy
    session, 8 visible turns spanned 57 rows / 2.9x the fallback target.

    Protecting that whole scanned region verbatim is not required — the
    scaffolding in it is summarised by the lexrank pass like any other
    middle content.  So: take the visible-based start as the *widest*
    candidate and shrink it until ``_fits`` succeeds, keeping the region
    contiguous (the compressor emits ``messages[tail_start:]`` verbatim)
    and never leaving an orphaned tool result at the boundary.

    Always returns a usable start.  ``target_tokens`` is a TARGET, not a
    ceiling (Evan, 2026-09-15): landing above it is expected and is not a
    failure to compress, so when nothing fits this falls through to the
    tightest region it can produce rather than returning ``None`` and
    stranding the session.  Deterministic compression has no failure rung.
    """
    total = len(messages)
    start = max(0, min(tail_start, total))
    # ``head_count`` bounds the region we may consume: everything before it
    # is the protected head.
    limit = max(head_count, total - 1)
    while start <= limit:
        # Never begin the verbatim region on a bare tool result — its
        # assistant tool-call row would be dropped, orphaning the pair and
        # making the transcript invalid for the provider.
        while start < limit and messages[start].get("role") == "tool":
            start += 1
        if _fits(messages, head_count, start, summary, target_tokens):
            return start
        if start == limit:
            break
        start += 1
    # Nothing fit under the target.  Return the tightest region (the newest
    # message, or the whole head when everything is protected) instead of
    # aborting.
    return min(limit, total)


def _plan_summary(plan_context: str, notes: list[str]) -> str:
    parts = [INTERNAL_FALLBACK_PREFIX, "## Active Plan", plan_context.strip()]
    if notes:
        parts.extend(["## Session Notes", "\n".join(f"- {note}" for note in notes)])
    parts.append(VERBATIM_CONTEXT_MARKER)
    return "\n\n".join(parts)


def _minimal_plan_summary(minimal_plan_context: str, notes: list[str]) -> str:
    parts = [INTERNAL_FALLBACK_PREFIX, "## Active Plan", minimal_plan_context.strip()]
    if notes:
        parts.extend(["## Session Notes", "\n".join(f"- {note}" for note in notes)])
    parts.append(VERBATIM_CONTEXT_MARKER)
    return "\n\n".join(parts)


def _fitted_plan_summary(
    messages: list[dict[str, Any]],
    *,
    head_count: int,
    tail_start: int,
    plan_context: str,
    notes: list[str],
    target_tokens: int,
    minimal: bool = False,
) -> tuple[str, int]:
    """Keep each priority-ordered note that fits beside the required Plan.

    Returns ``(summary, fitted_tail_start)``.  The tail start is a
    *preference* from ``_verbatim_tail_start`` that is advanced forward only
    as far as the token target requires (see ``_fit_tail_start``), so the
    protected region is the largest that still fits.
    """

    builder = _minimal_plan_summary if minimal else _plan_summary
    selected_notes: list[str] = []
    summary = builder(plan_context, selected_notes)
    # Note selection runs against the FULL requested tail, exactly as
    # before: shrinking the tail early would free budget that lets
    # lower-priority notes in, which is not what "keep the notes that fit
    # beside the protected tail" means.
    for note in notes:
        candidate = builder(plan_context, [*selected_notes, note])
        if _fits(messages, head_count, tail_start, candidate, target_tokens):
            selected_notes.append(note)
            summary = candidate

    if _fits(messages, head_count, tail_start, summary, target_tokens):
        return summary, tail_start
    # Last resort: the requested tail cannot fit beside the plan at all.
    # Shrink the verbatim region (summarising its scaffolding) rather than
    # aborting — an abort here is terminal for the session.
    return summary, _fit_tail_start(
        messages, head_count, tail_start, summary, target_tokens
    )


def _rank_units(
    middle_units: list[_Unit],
    note_units: list[_Unit],
    recent_units: list[_Unit],
) -> list[tuple[_Unit, float]]:
    """Rank candidate units by centrality + relevance + recency, best first.

    Shared by both fallback paths so a session with an active Plan selects
    its middle with the same selector as one without (Evan, 2026-09-15:
    the Plan must not make the LexRank area unreachable).
    """
    candidates = middle_units + note_units
    if not candidates:
        return []

    # Drop wrapper-only units and collapse repeats.  Re-ingested payloads
    # carry the same plan/summary lines forward verbatim, so without this
    # the selector spends budget emitting the same line three times and
    # LexRank rewards the duplication as centrality (Evan, 2026-09-15).
    deduped: list[_Unit] = []
    seen_text: set[str] = set()
    for unit in candidates:
        if _is_boilerplate_only(unit.text) or _is_fragment(unit.text):
            continue
        if not _has_wrapper_marker(unit.text):
            # Not wrapper text: never dedupe it, or repeated-but-real lines
            # (progress logs, step echoes) would silently disappear.
            deduped.append(unit)
            continue
        key = re.sub(r"\W+", " ", _strip_boilerplate(unit.text).casefold()).strip()
        if not key or key in seen_text:
            continue
        seen_text.add(key)
        deduped.append(unit)
    candidates = deduped
    if not candidates:
        return []

    all_vectors = _tfidf_vectors(
        [unit.text for unit in candidates] + [unit.text for unit in recent_units]
    )
    candidate_vectors = all_vectors[: len(candidates)]
    recent_vectors = all_vectors[len(candidates) :]
    recent_centroid = _centroid(recent_vectors)
    relevance = [_cosine(vector, recent_centroid) for vector in candidate_vectors]
    centrality = _lexrank(candidate_vectors)
    recency = _recency_scores(candidates)
    scores: list[float] = []
    for unit, central, relevant, near in zip(candidates, centrality, relevance, recency):
        # Third signal (Evan, 2026-09-15): how close the unit sits to the
        # live work.  Lexrank centrality alone happily carries a *finished*
        # sub-thread forward forever, because it was highly connected when
        # it was active — position is what distinguishes "central then"
        # from "central and still current".  Measured over 37 real
        # compactions at equal token budget: 25 wins / 7 losses / 5 ties,
        # mean coverage 66.5% -> 69.2%.
        score = (
            _CENTRALITY_WEIGHT * central
            + _RELEVANCE_WEIGHT * relevant
            + _RECENCY_WEIGHT * near
        )
        if _PRUNED_SKILL_RE.search(unit.text):
            # Reload markers are an instruction to re-read a skill, not
            # context worth ranking: scoring them above every real unit
            # spent note budget on a placeholder (Evan, 2026-09-15).
            # They are still emitted — `_session_notes` keeps them — but
            # they no longer outrank the material they displaced.
            score = max(score, _NOTE_SCORE_FLOOR)
        elif unit.is_note:
            score = 1.25 + 0.4 * relevant
        scores.append(score)

    return sorted(
        zip(candidates, scores),
        key=lambda item: (-item[1], item[0].order),
    )[:_MAX_CANDIDATE_UNITS]


def _with_lexrank_section(summary: str, selected: list[_Unit]) -> str:
    """Insert a ``## Relevant Earlier Context`` block above the verbatim marker."""
    if not selected:
        return summary
    block = "\n\n".join(
        [
            "## Relevant Earlier Context",
            "\n".join(unit.text for unit in sorted(selected, key=lambda item: item.order)),
        ]
    )
    if VERBATIM_CONTEXT_MARKER in summary:
        head, _, rest = summary.partition(VERBATIM_CONTEXT_MARKER)
        return head.rstrip() + "\n\n" + block + "\n\n" + VERBATIM_CONTEXT_MARKER + rest
    return summary.rstrip() + "\n\n" + block


def _add_plan_lexrank_area(
    messages: list[dict[str, Any]],
    *,
    head_count: int,
    tail_start: int,
    summary: str,
    notes: list[str],
    target_tokens: int,
) -> tuple[str, int]:
    """Spend the leftover plan budget on the ranked middle (Evan, 2026-09-15).

    The Plan path used to return as soon as the plan + notes + verbatim
    tail fitted, which made the LexRank area **unreachable for any session
    with an active Plan** — the region between the end of the plan and the
    protected tail is supposed to be selected by LexRank, not dropped.

    ``_fitted_plan_summary`` already fills whatever budget the notes leave.
    If there is room left over after that, the same selector the planless
    path uses ranks the middle and the best units are packed in one at a
    time, so the emitted area grows to the budget rather than being
    abandoned the moment the plan alone fits.  Budget is a TARGET, not a
    ceiling: when nothing fits we keep the plan-only payload.

    Returns ``(summary, tail_start)``; the tail start never moves backwards.
    """
    middle_messages = messages[head_count:tail_start]
    if not middle_messages:
        return summary, tail_start
    middle_units = _message_units(middle_messages, start_index=head_count)
    marker_notes = {note for note in notes if _PRUNED_SKILL_RE.fullmatch(note)}
    if marker_notes:
        middle_units = [
            unit
            for unit in middle_units
            if not any(marker in unit.text for marker in marker_notes)
        ]
    recent_units = _message_units(messages[tail_start:], start_index=tail_start)
    ranked = _rank_units(middle_units, [], recent_units)
    if not ranked:
        return summary, tail_start

    selected: list[_Unit] = []
    best = summary
    for unit, _score in ranked:
        candidate = _with_lexrank_section(summary, [*selected, unit])
        if _fits(messages, head_count, tail_start, candidate, target_tokens):
            selected.append(unit)
            best = candidate
    return best, tail_start


def _lexrank_summary(selected: list[_Unit]) -> str:
    context = [unit for unit in selected if not unit.is_note]
    notes = [unit for unit in selected if unit.is_note]
    parts = [INTERNAL_FALLBACK_PREFIX]
    if context:
        parts.extend(
            [
                "## Relevant Earlier Context",
                "\n".join(unit.text for unit in sorted(context, key=lambda item: item.order)),
            ]
        )
    if notes:
        parts.extend(
            [
                "## Session Notes",
                "\n".join(f"- {unit.text}" for unit in notes),
            ]
        )
    parts.append(VERBATIM_CONTEXT_MARKER)
    return "\n\n".join(parts)


def build_internal_fallback(
    messages: list[dict[str, Any]],
    *,
    protect_head_count: int,
    protect_last_n: int,
    target_tokens: int,
    plan_context: str = "",
    minimal_plan_context: str = "",
    memory_context: str = "",
    previous_summary: str = "",
) -> InternalFallback:
    """Build the last-resort local compression payload within ``target_tokens``.

    Never returns ``None``.  ``target_tokens`` is a TARGET, not a ceiling
    (Evan, 2026-09-15): deterministic compression has no failure rung, so an
    over-target or degenerate budget yields the tightest payload this
    selector can assemble rather than stranding the session.
    """
    head_count = min(max(protect_head_count, 0), len(messages))
    tail_start = _verbatim_tail_start(messages, head_count, protect_last_n)

    # Emergency rung (Evan, 2026-09-14): when the verbatim tail itself cannot
    # fit, mask its observations back to the last turn BEFORE shrinking the
    # region.  This preserves the contiguous reasoning/action trace (which
    # tail shrinking would summarise away) and sheds the bulky, disposable
    # half of the turn instead.  `_fit_tail_start` then re-measures the
    # lightened tail and usually succeeds without advancing the boundary.
    tail_tokens = estimate_messages_tokens_rough(messages[tail_start:])
    if tail_tokens > target_tokens:
        messages = _mask_tail_observations(messages, tail_start)

    notes = _session_notes(messages, memory_context, previous_summary)

    if plan_context.strip():
        summary, fitted_tail_start = _fitted_plan_summary(
            messages,
            head_count=head_count,
            tail_start=tail_start,
            plan_context=plan_context,
            notes=notes,
            target_tokens=target_tokens,
        )
        if _fits(messages, head_count, fitted_tail_start, summary, target_tokens):
            summary, fitted_tail_start = _add_plan_lexrank_area(
                messages,
                head_count=head_count,
                tail_start=fitted_tail_start,
                summary=summary,
                notes=notes,
                target_tokens=target_tokens,
            )
            return InternalFallback(summary, head_count, fitted_tail_start, "plan")

        # The full plan is itself too large to fit — degrade to
        # goal + current step + summary against the minimal head.
        system_head = 1 if messages and messages[0].get("role") == "system" else 0
        minimal_tail_start = _verbatim_tail_start(
            messages,
            system_head,
            protect_last_n,
        )
        summary, fitted_tail_start = _fitted_plan_summary(
            messages,
            head_count=system_head,
            tail_start=minimal_tail_start,
            plan_context=minimal_plan_context or plan_context,
            notes=notes,
            target_tokens=target_tokens,
            minimal=True,
        )
        summary, fitted_tail_start = _add_plan_lexrank_area(
            messages,
            head_count=system_head,
            tail_start=fitted_tail_start,
            summary=summary,
            notes=notes,
            target_tokens=target_tokens,
        )
        return InternalFallback(
            summary,
            system_head,
            fitted_tail_start,
            "plan-minimal",
        )

    middle_messages = messages[head_count:tail_start]
    middle_units = _message_units(middle_messages, start_index=head_count)
    marker_notes = {note for note in notes if _PRUNED_SKILL_RE.fullmatch(note)}
    if marker_notes:
        middle_units = [
            unit
            for unit in middle_units
            if not any(marker in unit.text for marker in marker_notes)
        ]
    recent_units = _message_units(messages[tail_start:], start_index=tail_start)
    note_units = [
        _Unit(
            text=note,
            order=(len(messages), index),
            token_count=estimate_tokens_rough(note),
            is_note=True,
        )
        for index, note in enumerate(notes)
    ]
    candidates = middle_units + note_units
    if not candidates:
        summary = _lexrank_summary([])
        return InternalFallback(
            summary,
            head_count,
            _fit_tail_start(messages, head_count, tail_start, summary, target_tokens),
            "lexrank",
        )

    ranked = _rank_units(middle_units, note_units, recent_units)
    selected: list[_Unit] = []
    for unit, _score in ranked:
        candidate_selection = [*selected, unit]
        summary = _lexrank_summary(candidate_selection)
        if _fits(messages, head_count, tail_start, summary, target_tokens):
            selected = candidate_selection

    summary = _lexrank_summary(selected)
    if _fits(messages, head_count, tail_start, summary, target_tokens):
        return InternalFallback(summary, head_count, tail_start, "lexrank")
    # Last resort: the requested verbatim tail cannot fit at all.  Shrink
    # the region (summarising its tool scaffolding) instead of aborting —
    # an abort here is terminal for the session, and the scaffolding rows
    # were never asked to be protected verbatim.
    return InternalFallback(
        summary,
        head_count,
        _fit_tail_start(messages, head_count, tail_start, summary, target_tokens),
        "lexrank",
    )
