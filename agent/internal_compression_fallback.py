"""CPU-only emergency context selection when summary providers fail."""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from collections import Counter
from typing import Any

from agent.model_metadata import estimate_messages_tokens_rough, estimate_tokens_rough


INTERNAL_FALLBACK_PREFIX = (
    "[CONTEXT WINDOW COMPRESSED — INTERNAL FALLBACK]\n"
    "All configured context-summary providers failed. This context was "
    "reconstructed locally without an LLM."
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
_MAX_UNIT_CHARS = 1_200
_ASSEMBLY_RESERVE_TOKENS = 24


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
    """Keep at least ``protect_last_n`` while avoiding summary/tail merging."""

    tail_start = max(head_count, len(messages) - max(protect_last_n, 0))
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


def _chunks(text: str) -> list[str]:
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


def _message_units(messages: list[dict[str, Any]], start_index: int = 0) -> list[_Unit]:
    units: list[_Unit] = []
    for message_offset, message in enumerate(messages):
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


def _session_notes(
    messages: list[dict[str, Any]],
    memory_context: str,
    previous_summary: str,
) -> list[str]:
    candidates: list[str] = []
    if memory_context and memory_context.strip():
        candidates.append(memory_context.strip())
    if previous_summary:
        candidates.extend(_PRUNED_SKILL_RE.findall(previous_summary))
    for source in [previous_summary, *[_content_text(msg.get("content")) for msg in messages]]:
        if not source:
            continue
        candidates.extend(match.strip() for match in _SESSION_NOTE_RE.findall(source))

    notes: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = re.sub(r"\s+", " ", candidate).strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            notes.append(normalized)
    return notes


def _tfidf_vectors(texts: list[str]) -> list[dict[str, float]]:
    token_counts = [Counter(_TOKEN_RE.findall(text.casefold())) for text in texts]
    document_frequency: Counter[str] = Counter()
    for counts in token_counts:
        document_frequency.update(counts)

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


def _lexrank(vectors: list[dict[str, float]], threshold: float = 0.1) -> list[float]:
    count = len(vectors)
    if not count:
        return []
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


def _plan_summary(plan_context: str, notes: list[str]) -> str:
    parts = [INTERNAL_FALLBACK_PREFIX, "## Active Plan", plan_context.strip()]
    if notes:
        parts.extend(["## Session Notes", "\n".join(f"- {note}" for note in notes)])
    parts.append(VERBATIM_CONTEXT_MARKER)
    return "\n\n".join(parts)


def _minimal_plan_summary(minimal_plan_context: str) -> str:
    return "\n\n".join(
        [INTERNAL_FALLBACK_PREFIX, "## Active Plan", minimal_plan_context.strip()]
    )


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
) -> InternalFallback | None:
    """Build the last-resort local compression payload within ``target_tokens``."""

    if target_tokens <= 0:
        return None
    head_count = min(max(protect_head_count, 0), len(messages))
    tail_start = _verbatim_tail_start(messages, head_count, protect_last_n)
    notes = _session_notes(messages, memory_context, previous_summary)

    if plan_context.strip():
        for keep_notes in range(len(notes), -1, -1):
            summary = _plan_summary(plan_context, notes[:keep_notes])
            if _fits(messages, head_count, tail_start, summary, target_tokens):
                return InternalFallback(summary, head_count, tail_start, "plan")

        system_head = 1 if messages and messages[0].get("role") == "system" else 0
        minimal = _minimal_plan_summary(minimal_plan_context or plan_context)
        if _fits(messages, system_head, len(messages), minimal, target_tokens):
            return InternalFallback(minimal, system_head, len(messages), "plan-minimal")
        return None

    middle_messages = messages[head_count:tail_start]
    middle_units = _message_units(middle_messages, start_index=head_count)
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
        if _fits(messages, head_count, tail_start, summary, target_tokens):
            return InternalFallback(summary, head_count, tail_start, "lexrank")
        return None

    all_vectors = _tfidf_vectors(
        [unit.text for unit in candidates] + [unit.text for unit in recent_units]
    )
    candidate_vectors = all_vectors[: len(candidates)]
    recent_vectors = all_vectors[len(candidates) :]
    recent_centroid = _centroid(recent_vectors)
    relevance = [_cosine(vector, recent_centroid) for vector in candidate_vectors]
    centrality = _lexrank(candidate_vectors)
    scores: list[float] = []
    for unit, central, relevant in zip(candidates, centrality, relevance):
        score = 0.6 * central + 0.4 * relevant
        if _PRUNED_SKILL_RE.search(unit.text):
            score = 2.0
        elif unit.is_note:
            score = score * 1.25 if relevant > 0.0 else -1.0
        scores.append(score)

    ranked = sorted(
        zip(candidates, scores),
        key=lambda item: (-item[1], item[0].order),
    )
    selected: list[_Unit] = []
    for unit, score in ranked:
        if score < 0:
            continue
        candidate_selection = [*selected, unit]
        summary = _lexrank_summary(candidate_selection)
        if _fits(messages, head_count, tail_start, summary, target_tokens):
            selected = candidate_selection

    summary = _lexrank_summary(selected)
    if not _fits(messages, head_count, tail_start, summary, target_tokens):
        return None
    return InternalFallback(summary, head_count, tail_start, "lexrank")
