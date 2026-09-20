"""Dependency-free sentence selection primitives for Hermes compression.

The code-span protection and abbreviation-repair design is adapted from
LLMSlim 0.5.0, Copyright (c) 2026 Yashvardhan Thanvi, under the MIT License.
The complete notice is carried in ``THIRD_PARTY_NOTICES.md``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
import math
import re

from agent.model_metadata import estimate_tokens_rough

_CODE_BLOCK_PATTERN = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_PATTERN = re.compile(r"`[^`\n]+`")
_WESTERN_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_STRUCTURAL_LINE_RE = re.compile(
    r"^\s*(?:#{1,6}\s|[-*+]\s|\d+[.)]\s|\x00BLOCK|\x00INLINE)"
)
_ABBREVIATIONS = frozenset(
    {
        "mr.",
        "mrs.",
        "ms.",
        "dr.",
        "prof.",
        "sr.",
        "jr.",
        "vs.",
        "etc.",
        "e.g.",
        "i.e.",
        "fig.",
        "eq.",
        "al.",
        "no.",
        "vol.",
        "approx.",
        "inc.",
        "ltd.",
        "co.",
        "st.",
        "a.m.",
        "p.m.",
    }
)
_TOKEN_RE = re.compile(r"[\w'-]{2,}", re.UNICODE)
_INSTRUCTION_RE = re.compile(
    r"\b(?:always|never|must(?:\s+not)?|should|do\s+not|don't|required|"
    r"ensure|remember\s+to|make\s+sure|preserve|retain|verify|avoid)\b",
    re.IGNORECASE,
)
_ENTITY_PATTERNS = (
    re.compile(r"https?://[^\s)>]+", re.IGNORECASE),
    re.compile(r"(?<!\w)(?:/[^\s/]+){2,}"),
    re.compile(r"`[^`\n]+`"),
    re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b"),
    re.compile(r"\b[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)+\b"),
    re.compile(r"\b(?:[a-f0-9]{7,40}|t_[a-z0-9]{6,})\b", re.IGNORECASE),
)
_EXTERNAL_ROLES = frozenset({"tool", "function", "rag", "retrieval", "external"})
_INSTRUCTION_ROLES = frozenset({"user", "assistant"})


@dataclass(frozen=True)
class SelectionUnit:
    """One chronological sentence candidate for extractive selection."""

    text: str
    order: tuple[int, ...]
    token_count: int
    role: str = "unknown"
    is_note: bool = False


@dataclass(frozen=True)
class SemanticChunk:
    """A locally coherent sequence of selection units."""

    index: int
    units: tuple[SelectionUnit, ...]
    token_count: int


@dataclass(frozen=True)
class ScoredUnit:
    """One candidate plus auditable source-aware score components."""

    unit: SelectionUnit
    chunk_index: int
    score: float
    centrality: float
    relevance: float
    instruction: float
    entity: float
    recency: float


@dataclass(frozen=True)
class BudgetSelection:
    """Result of exact-fit selection: changed, unchanged, or no usable output."""

    units: tuple[SelectionUnit, ...]
    state: str


def _tfidf_vectors(texts: list[str]) -> list[dict[str, float]]:
    token_counts = [Counter(_TOKEN_RE.findall(text.casefold())) for text in texts]
    document_frequency: Counter[str] = Counter()
    for counts in token_counts:
        document_frequency.update(counts.keys())

    document_count = len(texts)
    vectors: list[dict[str, float]] = []
    for counts in token_counts:
        vector = {
            token: count
            * (math.log((1 + document_count) / (1 + document_frequency[token])) + 1)
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


def _largest_fitting_prefix(text: str, max_tokens: int) -> int:
    low = 1
    high = len(text)
    best = 1
    while low <= high:
        midpoint = (low + high) // 2
        if estimate_tokens_rough(text[:midpoint]) <= max_tokens:
            best = midpoint
            low = midpoint + 1
        else:
            high = midpoint - 1
    return best


def _split_text_to_token_limit(text: str, max_tokens: int) -> list[str]:
    words = text.split()
    if not words:
        return []

    parts: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = " ".join([*current, word])
        if current and estimate_tokens_rough(candidate) > max_tokens:
            parts.append(" ".join(current))
            current = []
        if estimate_tokens_rough(word) <= max_tokens:
            current.append(word)
            continue

        if current:
            parts.append(" ".join(current))
            current = []
        remainder = word
        while estimate_tokens_rough(remainder) > max_tokens:
            split_at = _largest_fitting_prefix(remainder, max_tokens)
            parts.append(remainder[:split_at])
            remainder = remainder[split_at:]
        if remainder:
            current.append(remainder)
    if current:
        parts.append(" ".join(current))
    return parts


def _split_oversized_units(
    units: list[SelectionUnit], max_chunk_tokens: int
) -> list[SelectionUnit]:
    expanded: list[SelectionUnit] = []
    for unit in units:
        if unit.token_count <= max_chunk_tokens:
            expanded.append(unit)
            continue
        for part_index, part in enumerate(
            _split_text_to_token_limit(unit.text, max_chunk_tokens)
        ):
            expanded.append(
                replace(
                    unit,
                    text=part,
                    order=(*unit.order, part_index),
                    token_count=estimate_tokens_rough(part),
                )
            )
    return expanded


def semantic_chunks(
    units: list[SelectionUnit],
    *,
    similarity_threshold: float = 0.10,
    max_chunk_tokens: int = 300,
    min_chunk_tokens: int = 15,
) -> list[SemanticChunk]:
    """Group sentence units by running-centroid drift and a hard token cap."""

    if max_chunk_tokens < 1:
        raise ValueError("max_chunk_tokens must be at least 1")
    if min_chunk_tokens < 0:
        raise ValueError("min_chunk_tokens cannot be negative")
    if not units:
        return []

    expanded = _split_oversized_units(units, max_chunk_tokens)
    if not expanded:
        return []
    vectors = _tfidf_vectors([unit.text for unit in expanded])

    grouped: list[list[SelectionUnit]] = []
    current_units: list[SelectionUnit] = []
    current_vectors: list[dict[str, float]] = []
    current_tokens = 0
    for unit, vector in zip(expanded, vectors):
        over_cap = bool(current_units) and current_tokens + unit.token_count > max_chunk_tokens
        drifted = False
        if current_units and not over_cap:
            drifted = _cosine(vector, _centroid(current_vectors)) < similarity_threshold
            drifted = drifted and (
                current_tokens >= min_chunk_tokens or len(current_units) >= 2
            )
        if over_cap or drifted:
            grouped.append(current_units)
            current_units = []
            current_vectors = []
            current_tokens = 0
        current_units.append(unit)
        current_vectors.append(vector)
        current_tokens += unit.token_count
    if current_units:
        grouped.append(current_units)

    return [
        SemanticChunk(
            index=index,
            units=tuple(chunk_units),
            token_count=sum(unit.token_count for unit in chunk_units),
        )
        for index, chunk_units in enumerate(grouped)
    ]


def instruction_score(text: str, *, role: str) -> float:
    """Return an instruction signal only for trusted conversational sources."""

    if role.casefold() not in _INSTRUCTION_ROLES:
        return 0.0
    matches = len(_INSTRUCTION_RE.findall(text))
    return min(1.0, matches / 2.0)


def entity_score(text: str) -> float:
    """Return a bounded signal for concrete identifiers and references."""

    matches = sum(len(pattern.findall(text)) for pattern in _ENTITY_PATTERNS)
    return min(1.0, matches / 3.0)


def _local_centralities(chunk: SemanticChunk) -> list[float]:
    vectors = _tfidf_vectors([unit.text for unit in chunk.units])
    if len(vectors) == 1:
        # A singleton has no local neighbours, so it has no evidence of
        # representativeness. Treating it as maximally central makes every
        # isolated artifact outrank genuinely connected context.
        return [0.0]
    return [
        sum(_cosine(vector, other) for other in vectors if other is not vector)
        / (len(vectors) - 1)
        for vector in vectors
    ]


def _relevance_scores(
    units: list[SelectionUnit], recent_units: list[SelectionUnit]
) -> list[float]:
    if not recent_units:
        return [0.0] * len(units)
    vectors = _tfidf_vectors(
        [unit.text for unit in units] + [unit.text for unit in recent_units]
    )
    recent_centroid = _centroid(vectors[len(units) :])
    return [_cosine(vector, recent_centroid) for vector in vectors[: len(units)]]


def score_semantic_chunks(
    chunks: list[SemanticChunk],
    *,
    recent_units: list[SelectionUnit] | None = None,
) -> list[ScoredUnit]:
    """Score semantic units without allowing external text to issue directives.

    User and assistant content may receive instruction, entity, local-centrality,
    recent-relevance, and recency signals. Tool and retrieval content receives only
    factual-entity and recent-relevance signals, so words such as ``must`` and
    ``never`` in untrusted evidence cannot promote themselves.
    """

    if not chunks:
        return []
    flattened = [unit for chunk in chunks for unit in chunk.units]
    relevance = _relevance_scores(flattened, recent_units or [])
    relevance_by_order = {
        unit.order: value for unit, value in zip(flattened, relevance)
    }
    rank_by_order = {unit.order: rank for rank, unit in enumerate(flattened)}
    denominator = max(1, len(flattened) - 1)

    scored: list[ScoredUnit] = []
    for chunk in chunks:
        centralities = _local_centralities(chunk)
        for unit, centrality in zip(chunk.units, centralities):
            role = unit.role.casefold()
            external = role in _EXTERNAL_ROLES
            factual_entity = entity_score(unit.text)
            recent_relevance = relevance_by_order[unit.order]
            recency = rank_by_order[unit.order] / denominator
            instruction = instruction_score(unit.text, role=role)
            if external:
                centrality = 0.0
                recency = 0.0
                score = 0.65 * factual_entity + 0.35 * recent_relevance
            else:
                score = (
                    0.35 * centrality
                    + 0.30 * recent_relevance
                    + 0.20 * instruction
                    + 0.10 * factual_entity
                    + 0.05 * recency
                )
            scored.append(
                ScoredUnit(
                    unit=unit,
                    chunk_index=chunk.index,
                    score=score,
                    centrality=centrality,
                    relevance=recent_relevance,
                    instruction=instruction,
                    entity=factual_entity,
                    recency=recency,
                )
            )
    return scored


def select_budgeted_units(
    scored: list[ScoredUnit],
    *,
    fits: Callable[[tuple[SelectionUnit, ...]], bool],
) -> BudgetSelection:
    """Select with semantic-chunk coverage, then refill using exact-fit checks.

    ``fits`` receives the complete candidate in chronological emission order.
    The caller therefore remains responsible for assembling the real summary
    message and measuring it with Hermes's token estimator; this module neither
    guesses a ratio nor manipulates message topology.
    """

    if not scored:
        return BudgetSelection(units=(), state="no-op")

    ranked = sorted(scored, key=lambda item: (-item.score, item.unit.order))
    best_by_chunk: dict[int, ScoredUnit] = {}
    for item in ranked:
        best_by_chunk.setdefault(item.chunk_index, item)
    first_pass = sorted(
        best_by_chunk.values(), key=lambda item: (-item.score, item.unit.order)
    )
    first_ids = {id(item) for item in first_pass}
    second_pass = [item for item in ranked if id(item) not in first_ids]

    selected: list[ScoredUnit] = []
    for item in [*first_pass, *second_pass]:
        candidate = sorted([*selected, item], key=lambda value: value.unit.order)
        candidate_units = tuple(value.unit for value in candidate)
        if fits(candidate_units):
            selected = candidate

    units = tuple(item.unit for item in selected)
    if not units:
        state = "no-op"
    elif len(units) == len(scored):
        state = "unchanged"
    else:
        state = "selected"
    return BudgetSelection(units=units, state=state)


def _protect_code_spans(text: str) -> tuple[str, list[str], list[str]]:
    blocks: list[str] = []
    inline_spans: list[str] = []

    def store_block(match: re.Match[str]) -> str:
        blocks.append(match.group(0))
        return f"\x00BLOCK{len(blocks) - 1}\x00"

    def store_inline(match: re.Match[str]) -> str:
        inline_spans.append(match.group(0))
        return f"\x00INLINE{len(inline_spans) - 1}\x00"

    protected = _CODE_BLOCK_PATTERN.sub(store_block, text)
    protected = _INLINE_CODE_PATTERN.sub(store_inline, protected)
    return protected, blocks, inline_spans


def _restore_code_spans(
    sentences: list[str], blocks: list[str], inline_spans: list[str]
) -> list[str]:
    restored: list[str] = []
    for sentence in sentences:
        for index, span in enumerate(inline_spans):
            sentence = sentence.replace(f"\x00INLINE{index}\x00", span)
        for index, block in enumerate(blocks):
            sentence = sentence.replace(f"\x00BLOCK{index}\x00", block)
        restored.append(sentence)
    return restored


def _merge_false_splits(sentences: list[str]) -> list[str]:
    merged: list[str] = []
    for sentence in sentences:
        if merged:
            words = merged[-1].split()
            last_word = words[-1].casefold() if words else ""
            if last_word in _ABBREVIATIONS:
                merged[-1] = f"{merged[-1]} {sentence}"
                continue
        merged.append(sentence)
    return merged


def _split_cjk_boundaries(sentences: list[str]) -> list[str]:
    split: list[str] = []
    for sentence in sentences:
        split.extend(
            part
            for part in (piece.strip() for piece in re.split(r"(?<=[。！？])", sentence))
            if part
        )
    return split


def split_sentences(text: str) -> list[str]:
    """Split prose and machine output without splitting protected code spans."""

    text = text.strip()
    if not text:
        return []

    protected, blocks, inline_spans = _protect_code_spans(text)
    sentences: list[str] = []
    for line in protected.splitlines():
        line = line.strip()
        if not line:
            continue
        if _STRUCTURAL_LINE_RE.match(line):
            sentences.append(line)
            continue
        sentences.extend(
            part
            for part in (
                candidate.strip() for candidate in _WESTERN_SENTENCE_SPLIT_RE.split(line)
            )
            if part
        )

    sentences = _merge_false_splits(sentences)
    sentences = _split_cjk_boundaries(sentences)
    return [
        sentence.strip()
        for sentence in _restore_code_spans(sentences, blocks, inline_spans)
        if sentence.strip()
    ]
