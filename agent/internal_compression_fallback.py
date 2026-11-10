"""CPU-only emergency context selection when summary providers fail."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
import json
import logging
import math
import re
import time
from collections import Counter
from typing import Any, Callable, Sequence

from agent.llmslim_selector import (
    SelectionUnit,
    entity_score,
    instruction_score,
    score_semantic_chunks,
    semantic_chunks,
    split_sentences,
)
from agent.model_metadata import estimate_messages_tokens_rough, estimate_tokens_rough

logger = logging.getLogger(__name__)


INTERNAL_FALLBACK_PREFIX = (
    "[CONTEXT WINDOW COMPRESSED]"
)
# The payload marker doubles as the "this is a compaction payload" test used
# to prune re-ingested payloads out of the ranking region (Evan, 2026-09-16).
_PAYLOAD_MARKER = INTERNAL_FALLBACK_PREFIX
# Wrapper prefixes that identify a persisted compaction payload.  Both forms
# are present on the live Ornith transcript; the LLM-summarizer variant is a
# PREFIX constant, not one of the line-anchored ``_BOILERPLATE_RES`` patterns,
# precisely so it can be tested at position 0.
_PAYLOAD_MARKERS = (
    _PAYLOAD_MARKER,
    "[PRIOR CONTEXT",
)
# Verbatim-tail promise line — the summary's own boundary marker.  t_235275a0
# ("No Sections") drops the ``## Verbatim Recent Context`` heading from the
# form below and keeps the sentence as the marker: the payload carries no
# ``##`` header, the promise stays in the DB row, and the model view strips
# the line by policy either way (its real boundary is the END-OF-CONTEXT line
# appended by ``context_compressor``).
VERBATIM_CONTEXT_MARKER = "The messages after this summary marker are preserved verbatim."
# Underscores delimit identifier components for lexical scoring.
_TOKEN_RE = re.compile(r"(?:[^\W_]|['-]){2,}", re.UNICODE)
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
# Collapsed tool-result boost (Evan, 2026-10-21 — t_0a90cfdd): *"output these
# 'collapsed' tool call results into the LexRank system with the rest of the
# data - with a little boost at that since these results are likely
# important."*  Bounded floor form (ruling 5 — never a raw score add;
# t_57cb0d4e burned 12,703 tok on unbounded promotion).  Every scorer signal
# is in [0, 1], so the natural ceiling is 1.0: the 1.05 floor lifts collapsed
# tool results above ordinary prose, and the +0.2*rel form (ceiling 1.25)
# keeps even the most relevant result at or below the session-notes floor
# (1.25 + 0.4*rel) — notes > tool results > prose, bounded at both ends.
_TOOL_SCORE_FLOOR = 1.05
_MAX_UNIT_CHARS = 1_200
_MAX_CANDIDATE_UNITS = 256
_ASSEMBLY_RESERVE_TOKENS = 24
# Protected terms: the corpus's OWN filler, as opposed to the English filler
# in _STOPWORDS.  A term seen in this fraction of units is global to the
# session and saturates every vector, so it is carried in the Protected area
# and removed from Heap scoring (Evan, 2026-09-18).  Measured: the saturation
# culprits sat at 5.9-12.2% document frequency on Ornith's live window.
_PROTECTED_DF_FLOOR = 0.06
# A short token cannot carry a filename or an identifier, so excluding them
# keeps common English words ("this", "that") out of a set whose whole job is
# filenames and domain nouns.
_PROTECTED_MIN_TERM_CHARS = 4
# Ceiling on the Protected set as a share of the vocabulary, so a degenerate
# corpus cannot Protect most of its own vocabulary and leave the Heap with
# nothing to rank on.
_PROTECTED_MAX_SHARE = 0.05
# Protected AREA size and persistence (Evan, 2026-09-18: "we'll go 2 of 3 for
# now and can widen later as needed").  The area is deliberately small — it
# is the byte-stable prefix that rides the prompt cache, so churn in it is
# what costs a full re-prefill.
_PROTECTED_TOP_K = 12
_PROTECTED_PERSIST_RUNS = 3
_PROTECTED_PERSIST_MIN = 2
# Minimum content tokens a unit must retain after boilerplate stripping to
# stay rankable.  A unit made only of wrapper text carries no context.
_MIN_UNIT_TOKENS = 2
# Minimum content tokens for a unit to be worth ranking at all (Evan,
# 2026-09-15).  Measured on 22 real payloads: the emitted area's median unit
# was 8 content tokens and 59% were under 10, so the budget was spent on
# eight-word stubs.  Cosine also saturates on degenerate short units — a
# 2-token unit whose tokens both appear in the recent tail scores a perfect
# relevance — which filled the top of the ranking with trivia.
#
# Swept 0..12 on the real payloads: the lexical proxy keeps rising with the
# floor, but that is partly the same length bias the metric has, so this is
# set by principle instead — low enough to keep a genuinely short but real
# line ("SQLite migration needs a sessions index", 5 content tokens), high
# enough to drop the stubs that were occupying the top of the ranking
# ("Step 5/9" 1 token, "Do NOT delete create/join" 3).
# Notes and pruned-skill markers are exempt (see _rank_units).
_MIN_RANKABLE_TOKENS = 5
# Fraction of a heap candidate's content words that may already be carried by
# the verbatim tail before the candidate is skipped as a repeat (Evan,
# 2026-09-15; see _heap_repeat_covered).  0.8 means "effectively a duplicate".
_HEAP_REPEAT_COVERAGE = 0.8
# MMR redundancy penalty (Evan, 2026-09-15).  Selection was pure greedy
# top-N, so the budget collapsed onto a near-duplicate clique: the emitted
# set was more internally redundant than a random pick in 21/21 payloads and
# covered half the distinct content tokens (394 vs 751).  Each unit is now
# penalised by its similarity to what is already chosen, so the budget buys
# distinct ground instead of the same line eight times.
_MMR_LAMBDA = 0.7
# Self-narration / planning chatter ("Let me check…", "I'll start by…",
# "Okay, so…") — Evan, 2026-09-15: throw these out with a regex.
#
# Measured after dedupe+floor+MMR: these templates were 59.7% of the emitted
# area in 17/17 payloads, the same recurring-template failure as the
# compaction wrapper.  They could not be deduped against each other, because
# the tail differs every time ("let me check the code" vs "let me read the
# bodies").
#
# Coverage check before deleting (Evan asked): across 8,043 narration units,
# 52.2% were 100% covered by surviving units + the verbatim tail + plan text,
# 85.4% were >=80% covered, median 100%.  The words unique to dropped lines
# are verbs of intent — examine, inventory, assess, locate, systematically —
# never facts.  Nothing durable is lost.
#
# Deliberately broad across model dialects: Claude ("Let me start by", "My
# plan is"), GPT ("To do this,", "First, I'll"), DeepSeek ("Now I'll"),
# assistant small talk ("Okay so", "Sure,", "Great,", "Hmm").  All openers
# are role-gated in `_is_narration`, so a USER line is never dropped — "Let
# me know when it's done" is a request, not narration.
_NARRATION_OPENER_RE = re.compile(
    r"(?i)^\s*"
    # optional filler/small-talk lead-in
    r"(?:(?:now|next|then|first|second|third|finally|so|ok|okay|good|great|"
    r"perfect|right|alright|sure|certainly|yes|no|hmm+|wait|actually|"
    r"and|but|also|well|let's\s+see)\b[\s,.:;-]*)*"
    r"(?:"
    # intent / self-narration.  Only phrasings that announce a NEXT ACTION
    # are listed.  Bare "looking at…" / "checking…" / "here's what I found"
    # are deliberately absent: they usually carry the finding itself
    # ("Looking at the traceback, line 42 raises KeyError"), and dropping a
    # real finding costs far more than keeping a line of chatter.
    r"let\s+me|let\s+us|let's|"
    r"i'?ll|i\s+will|i'?m\s+(?:going|gonna)\s+to|i\s+am\s+going\s+to|"
    r"i\s+(?:need|should|want|have|must|can|could)\s+to|"
    r"i'?m\s+(?:now\s+)?(?:checking|looking|reading|running|going|starting|"
    r"applying|verifying|inspecting|examining|investigating)|"
    r"my\s+(?:plan|approach|next\s+step)\s+is|"
    r"(?:now\s+)?(?:i'?ll\s+)?(?:start|begin|proceed)\s+by|"
    r"(?:to\s+do\s+this|for\s+this|to\s+start|to\s+begin)\s*[,:]"
    r")"
    r"(?:\b|$)"
)
_ROLE_ONLY_RE = re.compile(r"^\[([A-Z_]{2,12})\]:")


@lru_cache(maxsize=8192)
def _is_narration(text: str) -> bool:
    """True for an agent's own "let me do X" line — never for a user request.

    "Let me check the real current state of the code" is the assistant
    narrating its next tool call; "Let me know when it's done" is the user
    asking for something.  Both start with "let me", so the opener alone
    cannot decide it — the role has to.  Only assistant/system lines are
    demoted; anything said by the user is context and ranks normally.
    """
    role_match = _ROLE_ONLY_RE.match(text)
    if role_match and role_match.group(1) != "ASSISTANT":
        return False
    return bool(_NARRATION_OPENER_RE.match(_strip_boilerplate(text)))
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
        r"#{1,6}\s*(?:Active Plan|Protected Context|Prior Context Summary|"
        r"Relevant Earlier Context|Verbatim Recent Context|Session Notes|"
        r"Context Summary)\b\s*:?",
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
    re.compile(r"\[END OF PRIOR CONTEXT SUMMARY\]"),
)
# The Plan tool's own result markers (Evan, 2026-09-16).  The newest one in
# the compressed region is the Heap boundary: everything before it belongs to
# a step that is already complete, so ranking it spends the budget on noise
# for the step the agent is actually working.  Verified against a live Ornith
# session (24,926 rows): ``Complete Step`` 80 hits, ``>>> STEP n`` 9,
# ``Active Step n of m`` 16.
_PLAN_STEP_CHANGE_RE = re.compile(
    r"(?:"
    r"Complete Step \d+"
    r"|>>>\s*STEP\s+\d+"
    r"|Active Step \d+ of \d+"
    r")"
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
    # Between-compaction Protected state, handed back so the caller can
    # persist it on the compressor instance (2026-09-18).
    protected_memory: "ProtectedMemory | None" = None
    selection_telemetry: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _Unit:
    text: str
    order: tuple[int, ...]
    token_count: int
    is_note: bool = False
    is_marker: bool = False
    role: str = "unknown"
    semantic_chunk: int | None = None
    # Provenance for the ``[#xx]`` recovery handle and the rendered timestamp
    # (Evan, 2026-10-21 — t_0a90cfdd): the session row id of the message the
    # unit was built from (``session_search(around_message_id=xx)`` opens it;
    # row id is the ghost-twin-safe key) and the row's own timestamp — never
    # the compaction clock.  ``None`` means "no such row" (synthesized units,
    # fixture rows without ids) and renders without decorations.
    row_id: int | None = None
    stamp: float | None = None


# Text-bearing fields per the tool schemas (terminal: ``output``; file
# tools: ``content``; error paths: ``error`` / ``message``; generic tools:
# ``result``; multimodal parts: ``text``). Gate (c) opened for the
# tool-trail class (Phase 2, t_490b92fc): a decoded dict/list tool result
# used to fall through to ``""`` at the bottom of ``_content_text``, so its
# outcome was invisible to every consumer. Schema-derived union —
# task-agnostic by construction (Evan's hard rule).
_TEXT_CONTENT_KEYS = ("text", "output", "content", "error", "message", "result")


def _dict_content_text(item: dict[str, Any]) -> str:
    """Extract text from a structured content part or tool result dict."""
    if isinstance(item.get("text"), str):
        return item["text"]
    parts: list[str] = []
    for key in _TEXT_CONTENT_KEYS:
        if key == "text":
            continue
        value = item.get(key)
        if isinstance(value, str) and value:
            parts.append(value)
    return "\n".join(parts)


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
            elif isinstance(item, dict):
                piece = _dict_content_text(item)
                if piece:
                    parts.append(piece)
        return "\n".join(parts)
    if isinstance(content, dict):
        return _dict_content_text(content)
    return ""


def _is_payload_message(message: dict[str, Any]) -> bool:
    """True when a message IS a stored compaction payload row.

    Detection is text-marker-based (``_PAYLOAD_MARKERS`` prefix test) because
    the stored row keeps no metadata — every non-content field is NULL except
    ``compacted=1`` (role flip is Phase 4 of ``compress()``, which stores the
    payload under ``role="user"`` without any marker key).

    A payload is the *previous* compressor's output folded forward. It is not a
    conversation turn, and protecting it verbatim via ``protect_last_n`` is
    what built the fossil chain: prior payload -> visible turn -> protected
    tail -> ``_fits`` unpassable -> output = plan + folded prior -> next
    fossil, pinning every payload at ~59K chars regardless of target.
    """
    text = message.get("content")
    if not isinstance(text, str):
        return False
    stripped = text.lstrip()
    return any(stripped.startswith(marker) for marker in _PAYLOAD_MARKERS)


def _template_visible_role(message: dict[str, Any]) -> str | None:
    """Role as the fall back counts it for ``protect_last_n`` sizing.

    Invisible: ``tool`` rows, assistant ``tool_calls`` rows (chat-template
    alternation exempts both), and — Stage 0.1 (t_57cb0d4e) — stored
    compaction payloads. A payload is a prior compressor output, not a
    conversation turn; counting it as a "visible turn" both spends the whole
    ``protect_last_n`` budget on a fossil and (via ``_fit_tail_start``) drags
    a 59K-char row into the verbatim tail, which alone exceeds the target.
    Excluded payloads flow into the middle, where
    ``_prune_reingested_payload`` already drops their ranking weight.
    """
    role = message.get("role")
    if role == "tool" or (role == "assistant" and message.get("tool_calls")):
        return None
    if isinstance(role, str) and _is_payload_message(message):
        return None
    return role if isinstance(role, str) else None


# Gate (a) opened for the tool-trail class (Phase 2, t_490b92fc):
# ``_template_visible_role`` above hides the tool flow (tool rows and
# assistant ``tool_calls`` rows) from the turn machinery. The tool-trail
# unit class ADMITS exactly the rows that gate hides — that is its whole
# subject — so the admission boundary lives here beside the gate it opens,
# and the trail builder must never filter on template visibility. Opening
# (a) alone is a no-op (Zephyr, council 2026-10-14): (b) would still strip
# the rows and (c) would still empty non-str content — all three open
# together.
def _is_tool_flow(message: dict[str, Any]) -> bool:
    """True for the rows the template hides: tool results and tool calls."""
    role = message.get("role")
    return role == "tool" or (role == "assistant" and bool(message.get("tool_calls")))


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


# The three pure text transforms below are memoised (fix-compressor,
# t_fc6b69d2): on the 680-msg repro they are called 9.9x / 4.0x / 4.7x on
# the SAME text per build (two rank passes over overlapping windows), and
# each _strip_boilerplate call runs ~17 regex substitutions. Pure
# text->value: identical results, bounded memory, no cross-call state.
@lru_cache(maxsize=8192)
def _strip_boilerplate(text: str) -> str:
    """Remove compaction wrappers so they cannot be ranked as context.

    A previous payload's ``[CONTEXT WINDOW COMPRESSED]`` prefix, section
    headers and ``--- END OF CONTEXT SUMMARY`` rule recur in EVERY earlier
    payload, so they score as maximally "central" under LexRank while
    carrying no information at all.  Measured on a live session: 2 of the
    top-5 ranked units were pure wrapper.  ``_TOKEN_RE`` also splits
    underscore identifiers like ``context_window_compressed`` into content
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


@lru_cache(maxsize=8192)
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


@lru_cache(maxsize=8192)
def _has_wrapper_marker(text: str) -> bool:
    """True when text carries an actual compaction wrapper.

    ``_message_units`` prefixes every unit with ``[ROLE]: ``, which the
    role-prefix normalizer inside ``_strip_boilerplate`` also removes — so
    comparing stripped-vs-original text cannot tell wrapper noise apart from
    ordinary content.  Test the wrapper patterns directly instead, and leave
    every unit that never matched one completely untouched.
    """
    return any(pattern.search(text) for pattern in _BOILERPLATE_RES)


@lru_cache(maxsize=8192)
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


# Canonical-form stopwords (Evan, 2026-09-15; negation carve-out Evan, 2026-09-20).
# Two units that differ only in function words carry the same information, so
# dedupe must compare them on content words alone.
# NEGATIONS ARE NEVER STRIPPED: "Fallbacks are not allowed" and "Fallbacks are
# allowed" are OPPOSITE facts and must never collapse to one key. Turning a
# sentence into its opposite is a correctness failure of the whole system.
_STOPWORDS = frozenset(
    """
    a an and are as at be been but by can could did do does for from had has
    have he her him his how i if in into is it its me my of on or
    our out over own should so some such than that the their them then there
    these they this those to too us was we were what when where which who
    whom why will with would you your yours
    """.split()
)


@lru_cache(maxsize=8192)
def _canonical_form(text: str) -> str:
    """Content-word canonical form of a unit, for near-duplicate detection.

    Drops boilerplate and function words and orders the surviving content
    words, so ``Step 6: implement undo`` and ``6. Step — undo, implement it``
    collapse to the same key.  Word *order* is discarded on purpose: the
    near-duplicates this is built for are re-quoted re-wordings of one line,
    not re-orderings that change meaning.
    """
    cleaned = _strip_boilerplate(text).casefold()
    tokens = [
        token
        for token in _TOKEN_RE.findall(cleaned)
        if token not in _STOPWORDS and not token.isdigit()
    ]
    if len(tokens) < 2:
        # Too little survives to distinguish anything: fall back to the exact
        # token sequence so genuinely different stubs do not collide.
        return " ".join(_TOKEN_RE.findall(cleaned))
    return " ".join(sorted(set(tokens)))


def _content_token_count(text: str) -> int:
    """How many content tokens a unit carries (its real information mass)."""
    return len(_content_tokens(text))


@lru_cache(maxsize=8192)
def _content_tokens(text: str) -> frozenset[str]:
    """Content-word token set of a unit, for similarity comparisons.

    Function words are dropped; digits are KEPT.  A bare number is often the
    whole payload of a line ("11 columns", "v2", "step 5"), so discarding
    digits under-counts real content and over-triggers the length floor.
    """
    return frozenset(
        token
        for token in _TOKEN_RE.findall(_strip_boilerplate(text).casefold())
        if token not in _STOPWORDS
    )


def _protected_terms(
    texts: list[str],
    *,
    min_document_frequency: float = _PROTECTED_DF_FLOOR,
) -> frozenset[str]:
    """Corpus-global terms that saturate ranking and belong in Protected.

    Evan, 2026-09-18: *"things like filenames that appear everywhere get
    thrown in the protected area and that filename should no longer be
    relevant to how other stuff scores when we build the heap area."*

    This is the measured fix for the saturation that made the emitted area
    "no better than random picks": the stopword list removes *English*
    filler but nothing knows that `config`, `model`, `key`, `hermes` and
    `compression` are *this corpus's* filler.  Weight is ``count * idf``, so
    a global term appearing three times in a unit outweighs a rare term
    appearing once — every vector leans the same way, every cosine is
    similar, and centrality degenerates.

    Threshold is a document-frequency fraction, not a count, so it holds on
    both a 200-unit middle and a 10,000-unit window.  Guards against
    stripping genuinely discriminative vocabulary: only terms longer than
    ``_PROTECTED_MIN_TERM_CHARS`` qualify (a short token cannot carry a
    filename or an identifier), and the set is capped so a degenerate
    corpus cannot Protect most of its own vocabulary and leave the Heap
    nothing to rank on.

    Returns an empty set when nothing clears the floor — the caller then
    behaves exactly as before, so this can only ever add selectivity.
    """
    if not texts:
        return frozenset()
    document_count = len(texts)
    minimum = max(2.0, min_document_frequency * document_count)
    frequency: Counter[str] = Counter()
    for text in texts:
        frequency.update(_content_tokens(text))
    qualifying = [
        (token, count)
        for token, count in frequency.items()
        if count >= minimum and len(token) > _PROTECTED_MIN_TERM_CHARS
    ]
    if not qualifying:
        return frozenset()
    limit = max(1, int(_PROTECTED_MAX_SHARE * len(frequency)))
    qualifying.sort(key=lambda item: (-item[1], item[0]))
    return frozenset(token for token, _ in qualifying[:limit])


@dataclass(frozen=True)
class ProtectedMemory:
    """Between-compaction state for the Protected area (2026-09-18).

    Immutable so a compaction cannot mutate the caller's copy by accident —
    the compressor replaces its reference with the returned value, which is
    the same discipline the rest of this module follows.

    ``recent`` holds the last ``_PROTECTED_PERSIST_RUNS`` runs' top-K
    canonical keys, oldest first.  A unit reaches the Protected area when it
    appears in ``_PROTECTED_PERSIST_MIN`` of them, so a single-compaction
    spike never gets Protected — measured on Ornith's window, 28.4% of
    top-20 appearances last exactly one snapshot.

    ``subject`` is the session subject these keys were selected under.  A
    change to it discards the history (Evan, 2026-09-18: a subject change is
    what turns a heap compaction into a full one).

    Lives on ``ContextCompressor`` (one instance per agent, created once at
    agent_init.py:2507), so it survives between compactions without any new
    store.
    """

    recent: tuple[tuple[str, ...], ...] = ()
    subject: str = ""


def _persistent_protected(
    units: list[_Unit],
    memory: ProtectedMemory,
    *,
    top_k: int = _PROTECTED_TOP_K,
    area_budget: int | None = None,
) -> tuple[list[_Unit], ProtectedMemory]:
    """Select the globally-central units worth carrying in Protected.

    Ranked over the WHOLE window, not against the hot area (Evan,
    2026-09-18): *"we're going to rank the entire context window, not
    against the hot area, all of it.  We want to look for long term global
    things to keep."*  The window is its own reference for the relevance
    term, so "relevant" here means "close to the centre of the session"
    rather than "close to the newest work".

    Returns ``(chosen_units, updated_memory)``.  Chosen units are drawn from
    the current top-K only — a unit that has dropped out of the top-K is no
    longer globally central, so its history is not a claim on the area.

    Bootstrap: with no history the threshold is 1, so the first compaction
    accepts the whole top-K.  Without that the area would be empty until the
    third compaction, and a full compaction with no Protected area is
    exactly the case that must not happen (Evan, 2026-09-18: *"you can't
    full compact without a protected area"*).

    ``area_budget`` (step5.md item 3) bounds the ranked portion of the
    Protected area.  The caller reserves the other slots for raw head turns
    (without a Plan) or the Plan description (with a Plan).  The
    highest-ranked units win; ``None`` keeps the legacy uncapped behaviour.
    """
    if not units:
        return [], memory

    # Nothing is "global" on a window too small to have a centre.  Below the
    # Heap's own candidate cap the selector already sees every unit, so a
    # Protected area adds nothing but can crowd out notes on a tight budget
    # (caught by test_relevant_session_notes_receive_a_ranking_boost: on a
    # 5-message window the only candidate was protected and the Session Note
    # no longer fitted).  Inert here = exactly the pre-2026-09-18 behaviour.
    if len(units) <= _MAX_CANDIDATE_UNITS:
        return [], memory

    # `recent` = the whole window: relevance becomes proximity to the
    # session's own centre instead of proximity to the newest turn.
    ranked = _rank_units(units, [], units)
    if not ranked:
        return [], memory

    top = [unit for unit, _score in ranked[:top_k]]
    keys = tuple(_canonical_form(unit.text) for unit in top)
    recent = (*memory.recent, keys)[-_PROTECTED_PERSIST_RUNS:]
    needed = _PROTECTED_PERSIST_MIN if len(recent) >= _PROTECTED_PERSIST_MIN else 1
    counts: Counter[str] = Counter(key for run in recent for key in run)
    chosen: list[_Unit] = []
    seen: set[str] = set()
    for unit in top:
        key = _canonical_form(unit.text)
        if counts[key] < needed or key in seen:
            continue
        seen.add(key)
        chosen.append(unit)
    if area_budget is not None and area_budget >= 0:
        # step5.md item 3: the Protected area's size budget.  `top` is in
        # rank order, so the highest-ranked units win the cap; a unit that
        # loses its slot is recorded in memory history as before (a later
        # compaction can re-elect it if its rank recovers).
        chosen = chosen[:area_budget]
    return chosen, ProtectedMemory(recent=recent, subject=memory.subject)


def _protected_summary_block(units: list[_Unit]) -> str:
    """The Protected/global area: the byte-stable prefix, emitted above the Heap.

    Ruling 5 (t_0a90cfdd): byte-stable across compactions (it rides the
    prompt cache), never boosted, never fed tool units.  t_235275a0 drops
    the ``## Protected Context`` header with every other section header
    ("No Sections") — the absence is byte-stable too, and the header was
    model-invisible anyway (strip policy).
    """
    if not units:
        return ""
    return "\n".join(
        _render_unit(unit) for unit in sorted(units, key=lambda item: item.order)
    )


# Kept last so the two helpers above read in dependency order.
_HEAP_REPEAT_IS_NOTE_EXEMPT = True


# --------------------------------------------------------- inviolable identifiers
# The Protected selector above answers "what is too COMMON to score?" (corpus
# filler, by document frequency).  This answers the opposite question: "what is
# too SPECIFIC to lose?"  A file path, a PID, an error code or a port scores
# near-zero on every signal we have — it is rare, short, and shares no
# vocabulary with anything — so a budget squeeze drops exactly the details a
# coding session cannot continue without (the "artifact trail" failure: an agent
# that forgets which file it modified produces inconsistent work).
#
# Transferable from LLMSlim's Tier 3 and TopoCompress's evidence-preservation
# argument; both lock these by REGEX, with no model call.  Design constraints
# that keep it cheap and honest:
#   * A unit qualifies only if it carries an identifier AND is short enough to
#     afford (`_INVIOLABLE_MAX_TOKENS`).  Without the length gate a long prose
#     turn that happens to mention one path would be locked whole and would
#     crowd out better units.
#   * Every locked unit must earn its place in the emitted text; nothing is
#     exempt from the budget (see the survival check in the selection loops).
#   * Paths and identifiers only — NOT numbers.  Bare numerals are the common
#     case in tool output and locking them all would be noise, not signal.
_PATH_RE = re.compile(
    r"(?:"
    r"(?:\.{0,2}/|/)[\w.@+-]+(?:/[\w.@+-]+)+"      # ./a/b, /home/ekl/x, ../a/b
    r"|[\w@+-]+/[\w.@+-]+\.(?:py|md|db|json|yaml|yml|txt|sh|sql|log|toml|cfg)"
    r"|\b[\w@+-]+\.(?:py|md|db|sql|json|yaml|yml|sh|log|toml|cfg)\b"
    r")"
)
_IDENTIFIER_RE = re.compile(
    r"(?:"
    r"\b(?:pid|exit[ _]?code|errno|https?[_ ]?status|signal)\b[^.\n]{0,12}?\d+"
    r"|\bE[A-Z]{2,}\b"                              # ENOENT, EACCES
    r"|\b\d{1,5}/(?:tcp|udp)\b"                     # 1234/tcp
    r"|\b0x[0-9a-fA-F]{4,}\b"                       # 0xdeadbeef
    # SCREAMING_SNAKE constants, with or without a leading underscore.  A
    # suffix allow-list (`_MAX`, `_RE`, ...) looked tidy and missed
    # `_MAX_CANDIDATE_UNITS` — a name our own compressor uses.  Multi-word
    # capitals are rare in prose, so the general form is safe; single words
    # (BLACK, EMPTY) are deliberately excluded, which is what the `+` requires.
    r"|\b_?[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b"
    r")"
)
_INVIOLABLE_MAX_TOKENS = 60


@lru_cache(maxsize=8192)
def _carries_inviolable(text: str) -> bool:
    """True when a unit carries a path or an identifier worth preserving.

    Pruned-skill reload markers keep their documented ranking floor, but the
    marker's own ``SKILL_PRUNED`` identifier does not consume an artifact-lock
    slot.  Any genuine path or identifier beside the marker still does.

    Both patterns are anchored on word boundaries and are deliberately narrow;
    widening them is the way this degrades into "keep everything", which is the
    failure the length gate exists to prevent.
    """
    stripped = _PRUNED_SKILL_RE.sub("", text)
    return bool(_PATH_RE.search(stripped) or _IDENTIFIER_RE.search(stripped))


def _is_inviolable(unit: "_Unit") -> bool:
    """Lock a unit against budget-squeeze dropping — if it is small enough.

    Length is the price of the lock.  A short unit carrying a path is the
    cheapest possible way to keep the artifact trail, so it is always worth its
    tokens; a long one is not, and locking it would let a single mention of
    `context_compressor.py` protect three paragraphs of prose.

    Measures ``unit.token_count`` (``estimate_tokens_rough`` of the full text),
    NOT ``_content_token_count``.  That helper returns the size of a *set* of
    distinct content words, so 148 words of repetitive filler "count" as 13 —
    it answers "how much information does this carry", not "how much budget
    will this cost".  Using it here made the gate pass long units through.
    """
    if unit.token_count > _INVIOLABLE_MAX_TOKENS:
        return False
    return _carries_inviolable(unit.text)


def _inviolable_missing(summary: str, locked: list["_Unit"]) -> list[str]:
    """Artifacts the lock asked for that the emitted text does not carry.

    The counterpart to ``_is_inviolable``: locking sets the *priority*, this
    verifies the priority was actually honoured.  The selection loops stop at
    the budget, so a lock can still lose — and a silently dropped artifact is
    worse than one that was never locked, because the caller believes it is
    there.  Callers log these, so a squeeze is visible instead of invisible.
    """
    return [u.text for u in locked if u.text not in summary]


def _log_inviolable_dropout(
    summary: str, ranked: list[tuple["_Unit", float]]
) -> None:
    """Warn when a locked artifact lost to the budget.

    Called at the end of a selection loop so the outcome is observable.  A lock
    that silently loses is the same defect class as a test that asserts on a
    return value instead of the database: the caller believes the artifact is
    present and it is not.
    """
    locked = [u for u, _s in ranked if _is_inviolable(u)]
    if not locked:
        return
    missing = _inviolable_missing(summary, locked)
    if missing:
        logger.warning(
            "compression: %d/%d locked artifact(s) dropped by budget: %s",
            len(missing),
            len(locked),
            "; ".join(text.strip()[:80] for text in missing[:3]),
        )


# Cap on how much artifact budget can claim.  A transcript that is nothing but
# paths and error codes must not spend the whole payload on them.
_MAX_INVIOLABLE_UNITS = 12



# --- Stage 2 tier (t_57cb0d4e sweep; promote-then-bound) ---------------------
# Artifact-dedup PRIMARY key (Zephyr measured 18 distinct / 622 tok / 6.73% of
# target); canonical stays a redundancy diagnostic, never a tier-membership
# rule - "a blob that MENTIONS a path is not a path". Representative = newest
# occurrence of each distinct artifact.
# Sweep config is set POST-IMPORT by the driver (sweep_driver.py) via _configure_tier -
# no env reading in the production module, no new imports.
_TIER_SHARE_S: float = 0.0            # tier promote is OFF by default (production:
                                      # pass-through). The ordering fix (lock before
                                      # cap) below is ACTIVE regardless of S.
_TIER_CANDIDATE_UNITS: int = _MAX_CANDIDATE_UNITS

def _configure_tier(share: float, cap_units: int | None = None) -> None:
    global _TIER_SHARE_S, _TIER_CANDIDATE_UNITS
    _TIER_SHARE_S = share
    if cap_units is not None:
        _TIER_CANDIDATE_UNITS = cap_units

def _artifact_key(unit: "_Unit") -> str | None:
    """Distinct-artifact key: the matched path or identifier, else None."""
    stripped = _PRUNED_SKILL_RE.sub("", unit.text)
    m = _PATH_RE.search(stripped) or _IDENTIFIER_RE.search(stripped)
    return m.group(0) if m else None

def _tier_promote(ordered: list[tuple["_Unit", float]]) -> list[tuple["_Unit", float]]:
    """Promote a bounded artifact tier to the front of the ordered list.

    Newest occurrence representative per distinct artifact; tier ceiling =
    _TIER_SHARE_S * target fraction is enforced by the CALLER budget, so here
    the tier is deduped and ordered; the SHARE backstop applies via token mass
    check against _TIER_TOKEN_CEILING computed from target.
    """
    if _TIER_SHARE_S <= 0:
        _TIER_DIAG['calls'] += 1
        return ordered
    _TIER_DIAG['calls'] += 1
    _TIER_DIAG['input_units'] = len(ordered)
    _TIER_DIAG['input_inviolable'] = sum(1 for pair in ordered if _is_inviolable(pair[0]))
    tier_budget = _TIER_TOKEN_CEILING  # None = no backstop (S=inf): promote all distinct artifacts
    tier: list[tuple[_Unit, float]] = []
    tier_tokens = 0
    seen: set[str] = set()
    rest: list[tuple[_Unit, float]] = []
    for pair in ordered:
        key = _artifact_key(pair[0])
        if key is None or key in seen:
            rest.append(pair)
            continue
        if tier_budget is not None and tier_tokens + pair[0].token_count > tier_budget:
            rest.append(pair)
            continue
        seen.add(key)
        tier_tokens += pair[0].token_count
        tier.append(pair)
    _TIER_DIAG['promoted'] = len(tier)
    _TIER_DIAG['promoted_tokens'] = tier_tokens
    _TIER_DIAG['distinct_keys'] = len(seen)
    return tier + rest

_TIER_TOKEN_CEILING: int | None = None
_TIER_DIAG: dict = {'calls': 0, 'input_units': 0, 'input_inviolable': 0,
                    'promoted': 0, 'promoted_tokens': 0, 'distinct_keys': 0}

def _set_tier_ceiling(target_tokens: int) -> None:
    """Compute the tier backstop from target_tokens and the sweep share S."""
    global _TIER_TOKEN_CEILING
    if _TIER_SHARE_S == float("inf"):
        _TIER_TOKEN_CEILING = None  # no backstop
    elif _TIER_SHARE_S <= 0:
        _TIER_TOKEN_CEILING = None  # sweep-off: current behavior
    else:
        _TIER_TOKEN_CEILING = max(1, int(_TIER_SHARE_S * target_tokens))

# --- end Stage 2 tier ---------------------------------------------------------

def _lock_first(
    ranked: list[tuple["_Unit", float]],
) -> list[tuple["_Unit", float]]:
    """Move artifact-bearing units to the front of the selection order.

    Both selection loops pack greedily from the top of this list until the
    budget is spent, so position **is** priority.  An artifact-bearing unit is
    ranked low by construction — a file path is rare, short, and shares no
    vocabulary with anything, which is exactly what centrality and relevance
    penalise — so without this it never gets reached when the budget binds, and
    the session loses the record of what it touched.

    Promoting them costs at most ``_MAX_INVIOLABLE_UNITS`` short units and makes
    the artifact trail survive a squeeze.  The cap keeps the highest-ranked
    ones, so the order *within* both groups is untouched.
    """
    locked: list[tuple[_Unit, float]] = []
    rest: list[tuple[_Unit, float]] = []
    for pair in ranked:
        if len(locked) < _MAX_INVIOLABLE_UNITS and _is_inviolable(pair[0]):
            locked.append(pair)
        else:
            rest.append(pair)
    return locked + rest


# Two-pass budget allocation (merged 2026-09-19 from LLMSlim's documented
# failure mode; Evan: "everything but the LLM").
#
# The packer was single-pass: walk the ranked list, add every unit that fits.
# Order is global, but a *region* that produces many units still wins by
# volume.  One tool dump splits into a dozen sentence chunks, all sharing
# vocabulary, all scoring high on centrality — and it spends the budget before
# the packer ever reaches a region that produces one or two.  LLMSlim names
# this exactly: "Single-pass budget allocation ... fails badly on documents
# with mixed density: technical specs mixed with background narrative."  An
# agent transcript is that mixture, maximally.
#
# Pass 1 caps each source region at a share of the budget proportional to the
# tokens it contributes to the ranked list, so volume cannot preempt priority.
# Pass 2 hands every unspent remainder back to strict global priority order —
# a region that cannot use its share (duplicates, or content already carried
# by the verbatim tail) does not hoard it.
#
# The cap is a CEILING, never a reservation: nothing is held back, so a tight
# budget still spends everything it can.
_TWO_PASS_ALLOCATION = True
# Floor on a region's share.  Without it a region contributing two units out
# of five hundred gets a cap of a few tokens and is effectively excluded,
# which is the opposite of the point.
_ALLOC_REGION_MIN_TOKENS = 32


def _region_key(unit: "_Unit") -> tuple[int, ...]:
    """Region a unit belongs to for budget allocation.

    Semantic middle candidates use their coherent chunk as the region. Legacy
    candidates retain source-message regions. Notes remain individually
    budgeted because they are synthesized rather than transcript chunks.
    """
    region = unit.order[0]
    if unit.is_note:
        return (region, unit.order[1])
    if unit.semantic_chunk is not None:
        return (-1, unit.semantic_chunk)
    return (region,)


def _region_allocation(
    units: list["_Unit"], target_tokens: int
) -> dict[tuple[int, ...], int]:
    """Per-region token ceiling for pass 1, proportional to contributed tokens.

    A region is the source message (``unit.order[0]``); a unit's region is
    where it came from in the transcript, not where it landed in the ranking.
    """
    weights: dict[tuple[int, ...], int] = {}
    for unit in units:
        region = _region_key(unit)
        weights[region] = weights.get(region, 0) + unit.token_count
    total = sum(weights.values())
    if not total:
        return {}
    return {
        region: max(_ALLOC_REGION_MIN_TOKENS, int(target_tokens * weight / total))
        for region, weight in weights.items()
    }


def _allocate_two_pass(
    ranked: list[tuple["_Unit", float]],
    *,
    target_tokens: int,
    initial: str,
    render: Callable[[list["_Unit"]], str],
    fits: Callable[[str], bool],
    accept: Callable[["_Unit"], bool] | None = None,
) -> tuple[list["_Unit"], str]:
    """Pack ``ranked`` into the budget in two passes (see the note above).

    ``render`` builds the payload text for a candidate selection and ``fits``
    tests it, so both callers share this even though they emit different
    blocks (the Plan path appends a LexRank area under a plan summary, the
    planless path builds the whole payload).  ``accept`` is the caller's
    repeat filter.

    Returns ``(selected, text)``.  ``text`` is ``initial`` when nothing was
    accepted, so a caller can pass the payload it already holds and get it
    back unchanged on a transcript where nothing fits.

    A unit that fails ``fits`` is dropped rather than reconsidered: the
    rendered text only grows as units are added, so the same unit cannot fit
    later.  A unit that is merely *capped* is deferred to pass 2, and that
    distinction is the whole mechanism.
    """
    selected: list[_Unit] = []
    best = initial

    def try_add(unit: _Unit) -> bool:
        nonlocal best
        text = render([*selected, unit])
        if not fits(text):
            return False
        selected.append(unit)
        best = text
        return True

    usable = [unit for unit, _score in ranked if accept is None or accept(unit)]
    if len(usable) < 2:
        for unit in usable:
            try_add(unit)
        return selected, best

    if not _TWO_PASS_ALLOCATION:
        for unit in usable:
            try_add(unit)
        return selected, best

    quota = _region_allocation(usable, target_tokens)
    spent: dict[tuple[int, ...], int] = {}
    deferred: list[_Unit] = []
    for unit in usable:
        region = _region_key(unit)
        used = spent.get(region, 0)
        if used and used + unit.token_count > quota.get(region, 0):
            # Over its share for this pass.  Not rejected — held for pass 2,
            # where the regions that could not use their share have already
            # had their pick.
            deferred.append(unit)
            continue
        if try_add(unit):
            spent[region] = used + unit.token_count

    for unit in deferred:
        try_add(unit)

    return selected, best


def _heap_repeat_covered(
    unit_text: str,
    outside_tiers: list[frozenset[str]],
) -> bool:
    """True when a heap candidate is already carried by text outside the heap.

    Spending heap budget on a fragment the window already holds buys nothing,
    so a candidate whose content words are mostly covered by one of the
    supplied tiers is skipped.

    Scope note (Evan, 2026-09-15): the tiers passed here are the **verbatim
    tail** only.  The plan/summary text at the start of the window is NOT a
    tier — it stands in for the session-wide LexRank score, which is the Dax
    half we haven't built, and it must not be treated as a separate class of
    content.  Measured on 17 real payloads: mean coverage of an emitted heap
    unit by the verbatim tail was 0.759, and 92/178 units (51.7%) were >=80%
    covered by the window they were about to be injected into.
    """
    tokens = _content_tokens(unit_text)
    if not tokens:
        return False
    for tier in outside_tiers:
        if tier and len(tokens & tier) / len(tokens) >= _HEAP_REPEAT_COVERAGE:
            return True
    return False


def _split_oversized_unit(text: str) -> list[str]:
    """Split text at word boundaries, marking artificial continuations."""
    chunks: list[str] = []
    remaining = text
    continued = False
    while remaining:
        prefix = "…" if continued else ""
        if len(prefix) + len(remaining) <= _MAX_UNIT_CHARS:
            chunks.append(f"{prefix}{remaining}")
            break

        # Reserve space for the trailing marker.  A positional split is only
        # valid when the current token alone exceeds the available payload.
        payload_limit = _MAX_UNIT_CHARS - len(prefix) - 1
        cut = remaining.rfind(" ", 0, payload_limit + 1)
        if cut <= 0:
            cut = payload_limit
        piece = remaining[:cut].rstrip()
        remaining = remaining[cut:].lstrip()
        chunks.append(f"{prefix}{piece}…")
        continued = True
    return chunks


@lru_cache(maxsize=8192)
def _chunks(text: str) -> list[str]:
    text = _strip_boilerplate(text)
    chunks: list[str] = []
    for sentence in split_sentences(text):
        sentence = re.sub(r"\s+", " ", sentence).strip()
        if sentence:
            chunks.extend(_split_oversized_unit(sentence))
    return chunks


_MASKED_OBSERVATION = "[observation omitted — outside the last turn]"


def _strip_tail_tool_rows(
    messages: list[dict[str, Any]], tail_start: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Tail promise rung (Evan, 2026-10-04 — t_57cb0d4e): keep the last turn
    whole, strip tool calls and observations from all earlier turns.

    Fires only when the masked tail is still over budget. Tool rows and
    tool-call rows are scaffolding — the reasoning around them is what
    carries context — so the promise keeps every user/assistant prose turn
    and drops only the heavy mechanical half. Tool-call assistants are
    dropped whole (their prose lives in the following assistant turn when
    there is one); observations drop likewise. Returns ``(messages,
    dropped_rows)`` — gate (b) opened for the tool-trail class (Phase 2,
    t_490b92fc): whole-removal hands the removed rows to the caller so the
    tool-trail ledger digests their actions instead of losing them. The
    input list is never mutated.
    """
    tail = messages[tail_start:]
    keep_from = len(tail)
    for offset in range(len(tail) - 1, -1, -1):
        if _template_visible_role(tail[offset]) is not None:
            keep_from = offset
            break
    out = list(messages[:tail_start])
    dropped: list[dict[str, Any]] = []
    for offset, message in enumerate(tail):
        if offset < keep_from and _is_tool_flow(message):
            dropped.append(message)
            continue
        out.append(message)
    return out, dropped


# ------------------------------------------- bounded tail tool-pair collapse
# Evan-approved design (2026-11-10, relayed by Gopher): ``protect_last_n``
# keeps its public meaning — N visible turns bound the tail region — but
# within that window the N most recent tool-call PAIRS stay verbatim with
# FULL results while every OLDER pair collapses to one one-line tool-trail
# digest (the Phase-2 t_490b92fc line format) in place. Measured need
# (Ornith's wedged session): 8 visible turns swallowed 323 rows / 159 tool
# rows — tool output was 78% of the protected tail and off-limits.

_TRAIL_DIGEST_PREFIX = "[TOOL]: "


def is_trail_digest_message(message: dict[str, Any]) -> bool:
    """True for an in-stream tool-trail digest row (bounded tail collapse).

    Digest rows carry the ledger's ``[TOOL]: `` line as content and NO
    ``tool_call_id`` — they are provenance lines, not tool results — so the
    pre-API pairing belt (``repair_message_sequence`` Pass 1) must exempt
    them instead of dropping them as strays.
    """
    if message.get("role") != "tool" or message.get("tool_call_id"):
        return False
    content = message.get("content")
    return isinstance(content, str) and content.startswith(_TRAIL_DIGEST_PREFIX)


def _tool_call_rows_by_id(
    rows: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """cid -> assistant tool_calls row, for orphan-result digest rendering."""
    by_id: dict[str, dict[str, Any]] = {}
    for message in rows:
        if message.get("role") != "assistant" or not message.get("tool_calls"):
            continue
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            cid = str(call.get("id") or call.get("call_id") or "")
            if cid and cid not in by_id:
                by_id[cid] = message
    return by_id


def collapse_tail_tool_pairs(
    tail_rows: list[dict[str, Any]],
    keep_pairs: int,
    *,
    call_rows: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Bounded tool-pair collapse inside the protected tail.

    Within ``tail_rows`` the ``keep_pairs`` most recent tool-call pairs
    (assistant ``tool_calls`` entry + its matching tool result rows) stay
    verbatim with FULL results; every OLDER pair collapses to one one-line
    tool-trail digest placed where the pair's rows were. Visible rows are
    never touched. Row-atomic rule (Oct-27 ruling: kept rows are never
    mutated): an assistant row holding any kept call is kept whole with all
    its results, so kept-pair counts exceed ``keep_pairs`` at those
    straddles. Fewer than ``keep_pairs`` pairs → byte-identical output
    (light-session invariant).

    ``call_rows`` maps cid -> the assistant row for pairs whose call entry
    lives outside the tail (orphan results) — needed only to render their
    digest line.

    Returns ``(new_rows, collapsed_rows)`` — ``collapsed_rows`` are the
    removed originals (telemetry/provenance). They are already digested in
    place: one channel per row, they must NOT additionally feed the trail
    ledger. The input list is never mutated.
    """
    if keep_pairs < 0:
        keep_pairs = 0
    if not tail_rows:
        return tail_rows, []
    call_rows = call_rows or {}

    # Units: one per tool_call id. Ordered by first row in the tail.
    unit_rows: dict[str, list[dict[str, Any]]] = {}
    unit_order: dict[str, int] = {}
    row_units: dict[int, set[str]] = {}
    for offset, message in enumerate(tail_rows):
        role = message.get("role")
        if role == "assistant" and message.get("tool_calls"):
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                cid = str(call.get("id") or call.get("call_id") or "")
                if not cid:
                    continue
                unit_rows.setdefault(cid, []).append(message)
                unit_order.setdefault(cid, offset)
                row_units.setdefault(offset, set()).add(cid)
        elif role == "tool":
            cid = str(message.get("tool_call_id") or "")
            if not cid or is_trail_digest_message(message):
                continue
            unit_rows.setdefault(cid, []).append(message)
            unit_order.setdefault(cid, offset)
            row_units.setdefault(offset, set()).add(cid)

    if not unit_rows:
        return tail_rows, []
    ordered = sorted(unit_rows, key=lambda cid: unit_order[cid])

    # Keep set: the newest N units, expanded to full rows — any unit sharing
    # a row with a kept unit is kept too (row-atomic straddle rule).
    keep: set[str] = set(ordered[-keep_pairs:]) if keep_pairs else set()
    changed = True
    while changed:
        changed = False
        for cid in list(keep):
            for unit in unit_rows[cid]:
                offset = tail_rows.index(unit) if unit in tail_rows else None
                if offset is None:
                    continue
                for peer in row_units.get(offset, ()):
                    if peer not in keep:
                        keep.add(peer)
                        changed = True

    # Render one digest line per collapsed pair (the t_490b92fc format,
    # 1:1 — the fusion pass over a single occurrence is one line).
    digest_for: dict[str, str] = {}
    for cid in ordered:
        if cid in keep:
            continue
        render_rows = unit_rows[cid]
        if render_rows and not (
            render_rows[0].get("role") == "assistant" and render_rows[0].get("tool_calls")
        ):
            call_row = call_rows.get(cid)
            if call_row is not None:
                render_rows = [call_row] + render_rows
        occurrences = _tool_trail_scan(render_rows, start_index=0)
        lines = _tool_trail_lines(occurrences)
        digest_for[cid] = (
            lines[0].text
            if lines
            else f"{_TRAIL_DIGEST_PREFIX}tool_call_id {cid} — collapsed"
        )

    # Assemble: visible rows and kept units pass through byte-identical;
    # each collapsed pair's rows collapse to one digest at its first row.
    first_row_cid: dict[int, str] = {}
    for cid in ordered:
        offset = unit_order[cid]
        first_row_cid.setdefault(offset, cid)

    dropped_ids: set[int] = set()
    for cid in ordered:
        if cid in keep:
            continue
        for unit in unit_rows[cid]:
            for offset, row in enumerate(tail_rows):
                if row is unit:
                    dropped_ids.add(offset)

    out: list[dict[str, Any]] = []
    collapsed: list[dict[str, Any]] = []
    emitted: set[str] = set()
    for offset, message in enumerate(tail_rows):
        if offset in dropped_ids:
            collapsed.append(message)
            cid = first_row_cid.get(offset)
            if cid is not None and cid not in emitted and cid in digest_for:
                out.append({"role": "tool", "content": digest_for[cid]})
                emitted.add(cid)
            continue
        out.append(message)
    return out, collapsed


def _is_observation(message: dict[str, Any]) -> bool:
    """True for environment observations (tool results).

    Observations are the verbose, disposable half of a turn.  They are kept
    verbatim inside the protected tail and masked everywhere else — they are
    never eligible for the lexrank middle and never feed the relevance
    centroid (Evan, 2026-09-14).
    """
    return message.get("role") == "tool"


def _prune_reingested_payload(text: str, *, is_newest: bool = False) -> str:
    """Reduce a re-ingested compaction payload to nothing worth ranking.

    Evan, 2026-09-16: *"that is a hermes compression output format. That isn't
    supposed to be there. That is for LLMs"* … *"we need to regex prune those
    results when they hit mid-window."*

    A payload is the API wire format for one compaction, but it is persisted
    as a real transcript message, so on the next cycle it sits in the middle
    and is ranked like history.  Measured on the live Ornith session: 37 such
    rows, the newest 18,720 chars (~4,680 tokens, 5.8% of an 80,128 window),
    with 14 consecutive payload pairs re-selecting 98–99% of the previous one
    into the next.

    Every section of a payload is regenerated each cycle rather than
    accumulated — the Plan from the kanban DB, the heap by LexRank, the
    verbatim tail by ``protect_last_n`` — so ranking it spends budget on a
    copy of what the window already holds.  The body is therefore dropped
    whole; ``_strip_boilerplate`` remains responsible for the wrapper lines on
    genuine content.

    ``is_newest`` is retained for callers that know a text is the payload being
    assembled right now; ``_message_units`` does NOT use it, because that
    payload is appended after compression and so is never inside the ranked
    region.

    Detection is a PREFIX test, not a substring search.  A payload is emitted
    with the marker at position 0; a ``session_search`` result that merely
    *quotes* a payload carries the marker mid-string and is ordinary content
    that must still rank.  A substring test pruned that result to nothing —
    caught by ``test_ordinary_content_is_untouched`` before it shipped.

    Two wrapper forms exist on the live transcript and both are machinery:

    * ``[CONTEXT WINDOW COMPRESSED]`` — the internal fallback payload (35 rows)
    * ``[PRIOR CONTEXT — for reference only; not a new message]`` — the
      LLM-summarizer payload (2 rows, role=assistant)
    """
    if is_newest:
        return text
    stripped = text.lstrip()
    if any(stripped.startswith(marker) for marker in _PAYLOAD_MARKERS):
        return ""
    return text


# ------------------------------------------------- terminal decision spine (Evan, 2026-09-23)
# The agent's own `echo "=== label ==="` header lines, emitted before every
# terminal call, are the decision spine of a trajectory: self-authored intent
# recorded while it was fresh.  After compaction the model re-orients on
# decisions, not on tool output (which it already acted on), so these markers
# are worth far more than their token count suggests.  Each is paired with a
# sibling exit-code tombstone so intent and outcome survive together — a bare
# `=== deploy ===` that outlived its failed command would read as success.
#
# The two halves live in DIFFERENT messages — the command in the assistant
# tool_call arguments, the outcome in the tool result JSON — and the
# compressor's `_message_units` skips both (tool results are observations,
# tool_calls are ignored).  This section reconstructs the fused unit by
# correlating on ``tool_call_id``.
_ECHO_MARKER_RE = re.compile(
    r"""echo\s+["']\s*((?:={3,})[^"']*?(?:={3,}))\s*["']"""
)
_ECHO_LABEL_DIGIT_RE = re.compile(r"\d")


def _is_echo_marker_command(command: str) -> bool:
    """True when a terminal command carries an `echo "=== label ==="` marker."""
    return bool(command) and bool(_ECHO_MARKER_RE.search(command))


def _echo_marker_verbatim(command: str) -> str:
    """The byte-exact echoed marker line (``=== label ===``), or ''.

    Captures the full span between the quotes so the echoed output survives
    compaction verbatim — the agent's own line, un-normalized.  This is the
    evidence layer; a Dax "spore" is contractually an exact source extract.
    """
    match = _ECHO_MARKER_RE.search(command or "")
    return match.group(1) if match else ""


def _echo_marker_label(marker: str) -> str:
    """The label inside a verbatim ``=== label ===`` marker, for the keep rules."""
    return re.sub(r"^=+\s*|\s*=+$", "", marker).strip()


def _marker_keep_label(label: str) -> bool:
    """Apply the keep rules: >=2 ws-words, or a digit, or an opening paren.

    Stage 1 (t_57cb0d4e, Evan ruling 2026-10-04): the word threshold is
    lowered from ``>3`` to ``>=2`` — the KISS form (Wintermute): not a new
    detector, the same rule with one number changed. Measured on the real
    supply (report §5a): the old threshold dropped ~48% of labels on the
    production extractor, and the dropped class is the decision spine
    ('Verify Dovecot' 283x, 'Auth test' 227x). 1-word residuals stay cut —
    their top members are genuine noise ('Status' 212x, 'RUN' 138x).
    ``description`` is excluded at any threshold (Evan ruling 2, Wintermute's
    information test): it carries no decision information regardless of how
    many words it has.

    Underscores count as whitespace so ``test_undo_scratch.py`` is 3 words,
    not 1.  Line-refs (``undo (875,965)``) survive via the digit/paren rules.
    """
    words = [word for word in re.split(r"[\s_]+", label) if word]
    # ``description`` excluded at any threshold (Evan ruling 2): it marks
    # nothing specific — 'Verify Dovecot' says what was done; 'description'
    # says only "this is a description". Case-folded: wrappers emit it in
    # several spellings.
    if label.strip().casefold() == "description":
        return False
    if len(words) >= 2:
        return True
    if _ECHO_LABEL_DIGIT_RE.search(label):
        return True
    return "(" in label


def _parse_exit_code(content: str) -> int | None:
    """Best-effort ``exit_code`` from a terminal tool-result JSON string.

    Returns ``None`` when the result is not terminal-shaped (or already
    masked into a placeholder), so non-terminal observations are skipped.
    """
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        data = None
    if isinstance(data, dict) and "exit_code" in data:
        try:
            return int(data["exit_code"])
        except (ValueError, TypeError):
            return None
    match = re.search(r'"exit_code"\s*:\s*(-?\d+)', content)
    if match:
        return int(match.group(1))
    # The one-line stub form (``[terminal] ran `cmd` -> exit 0, 1 lines
    # output``) keeps the exit code in its arrow suffix — by the time a
    # masked row reaches the compressor the JSON is long gone, and ruling 1
    # (t_235275a0) wants exit codes preserved on the collapsed line.
    match = re.search(r"->\s*exit(?:_code)?\s*[:=]?\s*(-?\d+)", content)
    if match:
        return int(match.group(1))
    return None


def _render_exit_tombstone(command: str, exit_code: int) -> str:
    """Sibling ``[exit …]`` line, using terminal_tool's semantics where known."""
    if exit_code == 0:
        return "[exit 0]"
    try:
        from tools.terminal_tool import _interpret_exit_code
        note = _interpret_exit_code(command, exit_code)
    except ImportError:
        note = None
    if note:
        return f"[exit {exit_code} — {note}]"
    return f"[exit {exit_code}]"


def _terminal_marker_units(
    messages: list[dict[str, Any]],
    start_index: int = 0,
) -> list[_Unit]:
    """Fuse each ``echo "=== label ==="`` decision marker with its exit code.

    Scans assistant terminal tool_calls for the marker command, pairs each
    with its tool result by ``tool_call_id``, and emits one unit per marker —
    the verbatim label line plus a sibling exit tombstone.  A marker whose
    label fails the keep rules is dropped whole, and a marker whose tool
    result is missing (masked/missing) is skipped rather than hallucinated.

    The unit's ``order`` is the tool-result offset, so markers stay in strict
    chronological order through compaction.
    """
    pending: dict[str, str] = {}
    results: dict[str, int] = {}
    result_offsets: dict[str, int] = {}
    result_rows: dict[str, int | None] = {}
    result_stamps: dict[str, float | None] = {}
    for offset, message in enumerate(messages):
        role = message.get("role")
        if role == "assistant":
            for tc in message.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                if not isinstance(fn, dict):
                    continue
                if fn.get("name") != "terminal":
                    continue
                args = fn.get("arguments")
                command = ""
                if isinstance(args, str):
                    try:
                        command = json.loads(args).get("command", "")
                    except (ValueError, TypeError):
                        command = ""
                if _is_echo_marker_command(command):
                    pending[str(tc.get("id") or "")] = command
        elif role == "tool":
            cid = str(message.get("tool_call_id") or "")
            if cid in pending:
                content = _content_text(message.get("content"))
                exit_code = _parse_exit_code(content)
                if exit_code is not None:
                    results[cid] = exit_code
                    result_offsets[cid] = offset
                    result_rows[cid] = _row_id_of(message)
                    result_stamps[cid] = _stamp_of(message)

    units: list[_Unit] = []
    for cid, command in pending.items():
        if cid not in results:
            continue
        marker = _echo_marker_verbatim(command)
        label = _echo_marker_label(marker)
        if not label or not _marker_keep_label(label):
            continue
        exit_code = results[cid]
        # Unit format (t_235275a0): role-tagged like every other emitted
        # unit; the ``=== label ===`` span survives byte-exact as a
        # substring (the marker is the evidence layer).
        text = f"[TOOL]: {marker}\n{_render_exit_tombstone(command, exit_code)}"
        units.append(
            _Unit(
                text=text,
                order=(start_index + result_offsets[cid], 0),
                token_count=estimate_tokens_rough(text),
                is_marker=True,
                role="tool",
                row_id=result_rows.get(cid),
                stamp=result_stamps.get(cid),
            )
        )
    return units


def _split_marker_tiers(
    markers: list[_Unit], recent_count: int
) -> tuple[list[_Unit], list[_Unit]]:
    """Split fused markers into (recent-unconditional, ranked-rest).

    The ``recent_count`` markers closest to the hot window are returned first,
    most-recent-first, to be locked ahead of the ranked heap.  The remainder
    are returned in chronological order to compete on rank as an ordinary
    candidate class.  ``recent_count <= 0`` sends everything to the ranked tier.
    """
    if not markers:
        return [], []
    ordered = sorted(markers, key=lambda unit: unit.order, reverse=True)
    if recent_count <= 0:
        return [], sorted(markers, key=lambda unit: unit.order)
    # Recent tier: most-recent-first.  Ranked tier: chronological, so it can
    # be appended to middle_units and compete as an ordinary candidate class.
    recent = ordered[:recent_count]
    ranked = sorted(ordered[recent_count:], key=lambda unit: unit.order)
    return recent, ranked


def _recent_markers_first(
    ranked: list[tuple[_Unit, float]],
    recent_markers: list[_Unit],
) -> list[tuple[_Unit, float]]:
    """Prepend the recent markers so they pack before the ranked heap.

    "Unconditional" here is a selection-ORDER guarantee, not a budget
    exemption: the recent markers are not ranked, they lead the packing order
    in recency order — but the packer still stops at the budget, so a marker
    can lose on a very tight window.  That is the hybrid Evan described: the
    recent markers get priority, the rest compete, and nothing is exempt from
    the real token budget.
    """
    return [(unit, float("inf")) for unit in recent_markers] + ranked


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

    Re-ingested compaction payloads are pruned here (Evan, 2026-09-16).

    There is deliberately NO positional exemption.  An earlier version exempted
    the newest message on the theory that it might be the payload under
    construction — but that payload is appended AFTER compression, so it is
    never inside the region being ranked, and the exemption was slice-relative
    (a payload was wrongly exempted whenever it happened to land last in the
    slice passed in).  Every payload inside the transcript is historical.
    """
    units: list[_Unit] = []
    for message_offset, message in enumerate(messages):
        if not include_observations and _is_observation(message):
            continue
        role = str(message.get("role") or "unknown")
        role_label = role.upper()
        content = _prune_reingested_payload(_content_text(message.get("content")))
        # Provenance (Evan, 2026-10-21 — t_0a90cfdd): every unit carries the
        # source row's id and original stamp so the emitted line can render
        # ``[#xx]`` (resolvable via ``session_search(around_message_id=xx)``)
        # and a timestamp that is never the compaction clock.
        row_id = _row_id_of(message)
        stamp = _stamp_of(message)
        for unit_offset, chunk in enumerate(_chunks(content)):
            text = f"[{role_label}]: {chunk}"
            units.append(
                _Unit(
                    text=text,
                    order=(start_index + message_offset, unit_offset),
                    token_count=estimate_tokens_rough(text),
                    role=role.casefold(),
                    row_id=row_id,
                    stamp=stamp,
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
            normalized = _split_oversized_unit(normalized)[0]
        if normalized and normalized not in seen:
            seen.add(normalized)
            notes.append(normalized)
            if len(notes) >= _MAX_CANDIDATE_UNITS:
                break
    return notes


def _tfidf_vectors(
    texts: list[str], *, drop: frozenset[str] = frozenset()
) -> list[dict[str, float]]:
    """TF-IDF vectors for ``texts``.

    ``drop`` removes corpus-global Protected terms from every vector before
    weighting (Evan, 2026-09-18).  They are still counted for document
    frequency so the remaining weights are unchanged — they simply carry no
    weight in the Heap's scoring after being Protected.
    """
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
            if token not in drop
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
        for position, index in enumerate(sampled_indices):
            scores[index] = sampled_scores[position]
        # Fill the gaps between samples with the nearer neighbour's score
        # rather than leaving them at 0.0 (Evan, 2026-09-18).  Centrality is
        # 40% of the unit score, so zeroing the unsampled units scores
        # (1 - 256/count) of the window on relevance and recency alone — at
        # count = 10,248 that is 97.5% of the window unable to compete on
        # centrality, which is what made the selector behave like a random
        # draw.  Measured: at 1,024 units the number of exactly-zero scores
        # (768) equals the number of unsampled units, i.e. every zero was
        # this artifact, not a property of the graph.
        #
        # ponytail: nearest-neighbour fill, not interpolation.  The sampled
        # indices are evenly spaced by construction, so the two are nearly
        # identical here, and nearest-neighbour cannot invent a score between
        # two neighbours that were both low.  Upgrading to a real dynamic
        # PageRank (warm-started turn-by-turn) is the Dax path if this stops
        # being good enough.
        for position in range(len(sampled_indices) - 1):
            start = sampled_indices[position]
            end = sampled_indices[position + 1]
            if end - start <= 1:
                continue
            left_score = sampled_scores[position]
            right_score = sampled_scores[position + 1]
            midpoint = (start + end) / 2
            for index in range(start + 1, end):
                scores[index] = left_score if index <= midpoint else right_score
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
    parts = [INTERNAL_FALLBACK_PREFIX, plan_context.strip()]
    if notes:
        parts.append("\n".join(f"- {note}" for note in notes))
    parts.append(VERBATIM_CONTEXT_MARKER)
    return "\n\n".join(parts)


def _minimal_plan_summary(minimal_plan_context: str, notes: list[str]) -> str:
    parts = [INTERNAL_FALLBACK_PREFIX, minimal_plan_context.strip()]
    if notes:
        parts.append("\n".join(f"- {note}" for note in notes))
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


def _blank_selection_telemetry() -> dict[str, Any]:
    return {
        "selector": "semantic",
        "instruction_units_found": 0,
        "instruction_units_kept": 0,
        "entity_units_found": 0,
        "entity_units_kept": 0,
        "chunk_count": 0,
        "candidate_count": 0,
        "candidate_tokens": 0,
        "selected_count": 0,
        "selected_tokens": 0,
        "achieved_ratio": 0.0,
        "selection_state": "no-op",
        "no_op_reason": None,
        "assembled_tokens": 0,
        "over_budget": False,
    }


def _record_selected_units(
    telemetry: dict[str, Any], selected: list[_Unit]
) -> None:
    telemetry["selected_count"] = len(selected)
    telemetry["selected_tokens"] = sum(unit.token_count for unit in selected)
    telemetry["tool_units_selected"] = sum(
        1 for unit in selected if unit.role == "tool"
    )
    telemetry["instruction_units_kept"] = sum(
        instruction_score(unit.text, role=unit.role) > 0 for unit in selected
    )
    telemetry["entity_units_kept"] = sum(
        entity_score(unit.text) > 0 for unit in selected
    )
    candidate_tokens = int(telemetry.get("candidate_tokens") or 0)
    telemetry["achieved_ratio"] = (
        round(telemetry["selected_tokens"] / candidate_tokens, 6)
        if candidate_tokens
        else 0.0
    )
    if not selected:
        telemetry["selection_state"] = "no-op"
        telemetry["no_op_reason"] = telemetry.get("no_op_reason") or (
            "no_candidates"
            if not telemetry.get("candidate_count")
            else "no_selected_candidates"
        )
    elif len(selected) == telemetry.get("candidate_count"):
        telemetry["selection_state"] = "unchanged"
        telemetry["no_op_reason"] = None
    else:
        telemetry["selection_state"] = "selected"
        telemetry["no_op_reason"] = None


def _semantic_rank_candidates(
    candidates: list[_Unit],
    reference_units: list[_Unit],
    telemetry: dict[str, Any] | None = None,
    *,
    tool_boost: bool = False,
) -> list[tuple[_Unit, float]]:
    """Rank a prepared middle with dependency-free LLMSlim semantics.

    ``tool_boost`` (Evan, 2026-10-21 — t_0a90cfdd) applies the bounded
    collapsed-tool-result floor at heap-selection time only; the byte-stable
    Protected/global area ranks without it (ruling 5).
    """

    selection_units = [
        SelectionUnit(
            text=unit.text,
            order=unit.order,
            token_count=unit.token_count,
            role=unit.role,
            is_note=unit.is_note,
            is_marker=unit.is_marker,
        )
        for unit in candidates
    ]
    recent_units = [
        SelectionUnit(
            text=unit.text,
            order=unit.order,
            token_count=unit.token_count,
            role=unit.role,
            is_note=unit.is_note,
            is_marker=unit.is_marker,
        )
        for unit in reference_units
    ]
    chunks = semantic_chunks(selection_units)
    scored = score_semantic_chunks(chunks, recent_units=recent_units)
    if telemetry is not None:
        telemetry["chunk_count"] = len(chunks)
        telemetry["candidate_count"] = len(scored)
        telemetry["candidate_tokens"] = sum(
            item.unit.token_count for item in scored
        )
        telemetry["instruction_units_found"] = sum(
            item.instruction > 0 for item in scored
        )
        telemetry["entity_units_found"] = sum(item.entity > 0 for item in scored)
    # Provenance rides through selection (t_0a90cfdd): llmslim's SelectionUnit
    # carries no row fields, so the source-row metadata is carried around the
    # score step keyed by ``order`` (a split part shares its source row via
    # the leading order components).
    provenance = {unit.order: (unit.row_id, unit.stamp) for unit in candidates}
    ranked: list[tuple[_Unit, float]] = []
    for item in scored:
        row_id, stamp = (
            provenance.get(item.unit.order)
            or provenance.get(item.unit.order[:2])
            or (None, None)
        )
        unit = _Unit(
            text=item.unit.text,
            order=item.unit.order,
            token_count=item.unit.token_count,
            is_note=item.unit.is_note,
            is_marker=item.unit.is_marker,
            role=item.unit.role,
            semantic_chunk=item.chunk_index,
            row_id=row_id,
            stamp=stamp,
        )
        score = item.score
        if _PRUNED_SKILL_RE.search(unit.text):
            score = max(score, _NOTE_SCORE_FLOOR)
        elif unit.is_note:
            score = max(score, 1.25 + 0.4 * item.relevance)
        elif tool_boost and unit.role == "tool":
            # Bounded preference bump (ruling 2 + 5): floor form, heap only.
            score = max(score, _TOOL_SCORE_FLOOR + 0.2 * item.relevance)
        ranked.append((unit, score))

    ordered = sorted(
        ranked,
        key=lambda pair: (-pair[1], pair[0].order),
    )
    # Stage 2 (t_57cb0d4e sweep): promote-then-bound. The lock must see the FULL
    # ranked list before the [:256] cap, else 416/442 inviolables die unseen
    # (measured on the production anchor window). Promote the artifact tier
    # (distinct artifacts only, newest occurrence representative), bound it by
    # the _TIER_SHARE ceiling, then cap the non-locked remainder.
    return _lock_first(_tier_promote(ordered))[:_TIER_CANDIDATE_UNITS]


def _rank_units(
    middle_units: list[_Unit],
    note_units: list[_Unit],
    recent_units: list[_Unit],
    *,
    reference_units: list[_Unit] | None = None,
    protected_units: list[_Unit] | None = None,
    semantic: bool = False,
    selection_telemetry: dict[str, Any] | None = None,
    tool_boost: bool = False,
) -> list[tuple[_Unit, float]]:
    """Rank candidate units by centrality + relevance + recency, best first.

    Shared by both fallback paths so a session with an active Plan selects
    its middle with the same selector as one without (Evan, 2026-09-15:
    the Plan must not make the LexRank area unreachable).

    ``reference_units`` overrides the relevance reference.  Default is the
    newest work, which is what the Heap wants ("close to the live turn").
    The Protected selector passes the whole window instead, so relevance
    becomes "close to the centre of the session" (Evan, 2026-09-18).

    ``tool_boost`` applies the bounded collapsed-tool-result floor (heap
    selection only — the Protected/global selector never opts in).
    """
    candidates = middle_units + note_units
    if not candidates:
        return []

    reference_units = recent_units if reference_units is None else reference_units

    # Drop unusable units and collapse near-duplicates (Evan, 2026-09-15).
    #
    # Dedupe runs on the CONTENT-WORD canonical form, not on raw text: the
    # emitted area was filling with re-quoted re-wordings of one line
    # ("Step 8: Leaderboard via `list`" appearing twice, "Do NOT delete
    # create/join" and "Do NOT delete create/join/move/watch" side by side),
    # each of which scored high on all three signals because near-identical
    # units reinforce each other's centrality.  Comparing function-word-
    # stripped, order-normalised forms catches those; comparing raw text did
    # not.
    #
    # An earlier version gated this on `_has_wrapper_marker`, so plain
    # duplicates that never carried a wrapper were kept and only wrapper text
    # was ever collapsed.  That was the bug.
    deduped: list[_Unit] = []
    seen_text: set[str] = set()
    short: list[_Unit] = []
    narration: list[_Unit] = []
    for unit in candidates:
        if _is_boilerplate_only(unit.text) or _is_fragment(unit.text):
            continue
        # Notes, pruned-skill reload markers, terminal decision markers, and
        # collapsed tool results are protected content: they are short by
        # nature and were never the junk this filter targets.  A length floor
        # applied to them empties the emitted block and takes the whole
        # fallback down with it.  Terminal markers (the agent's own
        # `=== label ===` + exit tombstone) are a decision spine, not prose —
        # they must compete on rank, not be filtered for being terse.  The
        # VCC-style collapsed tool line ("tool: target — exit 0") is terse by
        # construction (t_0a90cfdd) and must reach the pool the same way.
        protected = (
            unit.is_note
            or unit.is_marker
            or unit.role == "tool"
            or bool(_PRUNED_SKILL_RE.search(unit.text))
        )
        if not protected:
            if _is_narration(unit.text):
                # "Let me check the code" / "Now let me see the bodies".
                # Evan, 2026-09-15: throw these out, do not just demote them.
                # They were 59.7% of the emitted area in 17/17 payloads — the
                # same recurring-template failure as the compaction wrapper,
                # and the reason the area scored like a random draw.  Held
                # back rather than discarded outright so a transcript made
                # only of narration still emits something (see below).
                narration.append(unit)
                continue
            if _content_token_count(unit.text) < _MIN_RANKABLE_TOKENS:
                # Too short to carry context, and short units inflate cosine.
                # Held back rather than discarded: see the fallback below.
                #
                # EXCEPT when the unit is inviolable.  A line carrying a path,
                # a pid or an error code is short BY NATURE — the token count
                # that makes it "too short to rank" is the same property that
                # makes it cheap to keep and impossible to recover.  Measured
                # 2026-09-19: `edited agent/system_prompt.py` is 4 content
                # tokens and `killed pid 1476953` is 3, against a floor of 5,
                # so both were dropped BEFORE ranking and the artifact lock
                # never saw them.  Without this exemption the lock is dead code
                # for the very units it exists to protect.
                #
                # Only the length floor is waived.  Boilerplate, fragments and
                # narration are still excluded above, so this cannot pull back
                # the junk those filters exist for.
                if not _is_inviolable(unit):
                    short.append(unit)
                    continue
            key = _canonical_form(unit.text)
            if not key or key in seen_text:
                continue
            seen_text.add(key)
        deduped.append(unit)
    if not deduped and short:
        # Every candidate was below the floor.  An empty area is worse than a
        # weak one — the caller has no other middle context to fall back on —
        # so on a transcript that is nothing but short lines, keep them.
        deduped = short
    if not deduped and narration:
        # Same reasoning for a transcript that is nothing but agent
        # narration: narration is poor context, but it beats an empty block.
        deduped = narration
    candidates = deduped
    if not candidates:
        return []

    # Protected terms are detected over the WHOLE window (Evan, 2026-09-18):
    # *"we're going to rank the entire context window, not against the hot
    # area, all of it.  We want to look for long term global things to
    # keep."*  Detecting them over the middle alone would miss a term that
    # is globally common because the recent tail keeps using it.
    protected_terms = _protected_terms(
        [unit.text for unit in candidates] + [unit.text for unit in recent_units]
    )

    # The Protected AREA is scored out of the Heap's candidate pool when the
    # caller supplies one (Evan, 2026-09-18: its hits are removed as scoring
    # terms for the Heap, and no duplication is allowed across regions).
    if protected_units:
        protected_keys = {_canonical_form(unit.text) for unit in protected_units}
        removed = [
            unit for unit in candidates if _canonical_form(unit.text) in protected_keys
        ]
        if removed:
            candidates = [
                unit for unit in candidates if _canonical_form(unit.text) not in protected_keys
            ]
            if not candidates:
                return []
            protected_terms = protected_terms | frozenset(
                token for unit in removed for token in _content_tokens(unit.text)
            )

    if semantic:
        return _semantic_rank_candidates(
            candidates,
            reference_units,
            telemetry=selection_telemetry,
            tool_boost=tool_boost,
        )

    all_vectors = _tfidf_vectors(
        [unit.text for unit in candidates] + [unit.text for unit in reference_units],
        drop=protected_terms,
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
        elif tool_boost and unit.role == "tool":
            # Bounded preference bump (Evan rulings 2 + 5, t_0a90cfdd):
            # floor form, heap only — never a raw score add.
            score = max(score, _TOOL_SCORE_FLOOR + 0.2 * relevant)
        scores.append(score)

    ordered = sorted(
        zip(candidates, scores),
        key=lambda item: (-item[1], item[0].order),
    )
    # Stage 2 (t_57cb0d4e sweep): promote-then-bound, as site 1. The
    # [:_TIER_CANDIDATE_UNITS] cap bounds _mmr_order's INPUT, not its
    # output (fix-compressor, t_fc6b69d2): MMR is O(n^2) on what it
    # receives. Inner _lock_first runs full-list so the locked artifacts
    # land inside the cap; the outer one re-asserts them after MMR's
    # permutation.
    return _lock_first(_mmr_order(_lock_first(_tier_promote(ordered))[:_TIER_CANDIDATE_UNITS]))


def _mmr_order(
    ranked: list[tuple[_Unit, float]],
    *,
    lam: float = _MMR_LAMBDA,
) -> list[tuple[_Unit, float]]:
    """Re-order a ranked list so redundant near-duplicates are pushed down.

    Maximal Marginal Relevance over the ranked candidates.  Both call sites
    select greedily from the top of this list until the token budget is
    spent, so the ORDER *is* the selection policy — pure score order spent
    the budget on a near-duplicate clique (measured: more internally
    redundant than a random pick in 21/21 payloads, half the distinct
    content tokens).

    Each step picks the candidate maximising ``score - lam * max_similarity``
    against everything already picked, where similarity is the Jaccard
    overlap of content-word sets. O(n^2) on <=256 candidates: the max
    similarity is maintained incrementally across picks, so each token-set
    pair is scored once; recomputing the max inside the scan would be
    O(n^3) and blow this bound even at the cap (fix-compressor, t_fc6b69d2).

    The returned scores are the original ranking scores, unchanged: this
    decides order, not value, so callers that only read the score are
    unaffected.
    """
    if len(ranked) < 2 or lam <= 0:
        return ranked

    remaining = list(ranked)
    token_sets = {id(unit): _content_tokens(unit.text) for unit, _score in remaining}
    chosen: list[tuple[_Unit, float]] = []
    max_similarity = {id(unit): 0.0 for unit, _score in remaining}

    while remaining:
        best_index = 0
        best_value = float("-inf")
        for index, (unit, score) in enumerate(remaining):
            value = score - lam * max_similarity[id(unit)]
            if value > best_value:
                best_value = value
                best_index = index
        unit, score = remaining.pop(best_index)
        chosen.append((unit, score))
        picked = token_sets[id(unit)]
        if picked:
            for candidate, _score in remaining:
                tokens = token_sets[id(candidate)]
                if not tokens:
                    continue
                similarity = len(tokens & picked) / len(tokens | picked)
                if similarity > max_similarity[id(candidate)]:
                    max_similarity[id(candidate)] = similarity

    return chosen


def _render_stream(*groups: Sequence["_Unit | _TrailLine"]) -> str:
    """One chronological stream of rendered units (t_235275a0 "No Sections").

    Every emitted unit carries ``[#id]`` + role tag + the row's own stamp
    (ruling 3).  Groups (heap selection, marker spine, tool trail, …) merge
    into a single order-keyed run so the payload is a chronological stream,
    never a sectioned manifest.
    """
    units = [unit for group in groups for unit in group]
    return "\n".join(
        _render_unit(unit) for unit in sorted(units, key=lambda item: item.order)
    )


def _with_lexrank_section(
    summary: str,
    selected: list[_Unit],
    protected: list[_Unit] | None = None,
    extras: Sequence["_Unit | _TrailLine"] = (),
) -> str:
    """Append the heap stream below the Plan and Protected, above the marker.

    Order is Plan, Protected, Heap, verbatim tail (Evan, 2026-09-18):
    *"separate messages for plan, protected, and heap."*  Plan holds the
    long-term goals and is byte-stable between step changes; Protected holds
    the globally-central units, which are byte-stable between subject
    changes; the heap is re-selected every cycle and has no such claim.
    Ordering is therefore load-bearing, not cosmetic — the two stable
    sections ride the prompt cache and only the heap churns.
    """
    blocks: list[str] = []
    protected_block = _protected_summary_block(protected or [])
    if protected_block:
        blocks.append(protected_block)
    stream = _render_stream(selected, extras)
    if stream:
        blocks.append(stream)
    if not blocks:
        return summary
    block = "\n\n".join(blocks)
    if VERBATIM_CONTEXT_MARKER in summary:
        head, _, rest = summary.partition(VERBATIM_CONTEXT_MARKER)
        return head.rstrip() + "\n\n" + block + "\n\n" + VERBATIM_CONTEXT_MARKER + rest
    return summary.rstrip() + "\n\n" + block


def _plan_step_change_index(
    messages: list[dict[str, Any]], start: int, end: int
) -> int:
    """Index of the newest Plan step-change marker in ``messages[start:end]``.

    Evan, 2026-09-16: *"If there is an active plan, scan for those markers it
    leaves and only rank from that point forward. Everything before the step
    change is noise."*  Content belonging to completed steps is not context
    for the current step, and ranking it is what let a payload re-select
    itself into the next cycle.

    The three markers are the plan tool's own results — they are already in
    the transcript and nothing new is written to produce them:

    * ``Complete Step 3: <next step>``  — ``advance`` moving the step
    * ``>>> STEP 3: <step> <<<``        — ``remind`` / approval output
    * ``Active Step 3 of 9: ...``       — ``remind`` on a resumed Plan

    Returns ``start`` when no marker is present (nothing to bound — rank the
    whole region, which is the pre-existing behaviour).
    """
    boundary = start
    for index in range(start, end):
        content = messages[index].get("content")
        if not isinstance(content, str) or not content:
            continue
        if _PLAN_STEP_CHANGE_RE.search(content):
            boundary = index
    return boundary


def _add_plan_lexrank_area(
    messages: list[dict[str, Any]],
    *,
    head_count: int,
    tail_start: int,
    summary: str,
    notes: list[str],
    target_tokens: int,
    protected: list[_Unit] | None = None,
    selection_telemetry: dict[str, Any] | None = None,
    marker_recent_count: int = 0,
    tool_units: list[_Unit] | None = None,
    extras: Sequence["_Unit | _TrailLine"] = (),
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
    # Heap boundary (Evan, 2026-09-16): rank only from the newest Plan
    # step-change marker forward.  Content before it belongs to completed
    # steps and is noise for the step being worked.  With no marker the
    # boundary is ``head_count`` and the whole middle ranks, as before.
    rank_start = _plan_step_change_index(messages, head_count, tail_start)
    middle_messages = messages[rank_start:tail_start]
    if not middle_messages and not tool_units:
        if selection_telemetry is not None:
            selection_telemetry["no_op_reason"] = "no_candidates"
        return summary, tail_start
    middle_units = _message_units(middle_messages, start_index=rank_start) + list(
        tool_units or []
    )
    # Stage 1 (t_57cb0d4e): markers do NOT enter the ranked heap — the spine
    # (see _select_spine_units) is their selection path, under its own
    # ceiling. Feeding them here re-coupled retention to the heap's leftover
    # budget and ran them through the score≈0 / 256-cap funnel (4-of-37
    # survived); the double-entry also duplicated marker lines across the
    # spine block and the heap block. Recent-tier locking
    # (``_recent_markers_first``) is superseded by the spine's ceiling, which
    # cuts oldest-first — the newest decision marks are exactly what survives.
    marker_notes = {note for note in notes if _PRUNED_SKILL_RE.fullmatch(note)}
    if marker_notes:
        middle_units = [
            unit
            for unit in middle_units
            if not any(marker in unit.text for marker in marker_notes)
        ]
    recent_units = _message_units(messages[tail_start:], start_index=tail_start)
    ranked = _rank_units(
        middle_units,
        [],
        recent_units,
        protected_units=protected,
        semantic=True,
        selection_telemetry=selection_telemetry,
        tool_boost=True,
    )
    if not ranked:
        if selection_telemetry is not None:
            selection_telemetry["no_op_reason"] = (
                selection_telemetry.get("no_op_reason") or "no_ranked_candidates"
            )
        return summary, tail_start

    # Do not spend heap budget re-stating the verbatim tail — it is already in
    # the window.  The plan/summary text is deliberately NOT a tier: it stands
    # in for the session-wide LexRank score (the Dax half), so it is not a
    # separate class of content to dedupe against (Evan, 2026-09-15).
    # _content_text, not a raw join: multimodal messages carry `content` as a
    # list of parts, and a raw join raises TypeError mid-compression.
    outside_tiers = [
        _content_tokens(
            "\n".join(_content_text(m.get("content")) for m in messages[tail_start:])
        ),
    ]

    # Two-pass packing (2026-09-19).  Single-pass let one many-unit region
    # spend the budget before the packer reached a one-unit region, which is
    # the mixed-density failure LLMSlim documents; the allocator caps each
    # region's pass-1 share and hands the remainder back in priority order.
    # Budget is still a TARGET: when nothing fits we keep the plan-only
    # payload, which is what ``initial`` returns unchanged.
    selected, best = _allocate_two_pass(
        ranked,
        target_tokens=target_tokens,
        initial=summary,
        render=lambda chosen: _with_lexrank_section(summary, chosen, protected, extras),
        fits=lambda text: _fits(messages, head_count, tail_start, text, target_tokens),
        accept=lambda unit: unit.is_marker
        or not _heap_repeat_covered(unit.text, outside_tiers),
    )
    if selection_telemetry is not None:
        _record_selected_units(selection_telemetry, selected)
    _log_inviolable_dropout(best, ranked)
    return best, tail_start


def _lexrank_summary(
    selected: list[_Unit],
    protected: list[_Unit] | None = None,
    extras: Sequence["_Unit | _TrailLine"] = (),
) -> str:
    context = [unit for unit in selected if not unit.is_note]
    notes = [unit for unit in selected if unit.is_note]
    parts = [INTERNAL_FALLBACK_PREFIX]
    protected_block = _protected_summary_block(protected or [])
    if protected_block:
        parts.append(protected_block)
    # t_235275a0 ("No Sections"): one chronological stream of unit-format
    # lines, no ``##`` headers anywhere.  The summary's tail boundary is the
    # ``--- END OF CONTEXT SUMMARY ---`` line context_compressor appends.
    stream = _render_stream(context, extras)
    if stream:
        parts.append(stream)
    if notes:
        # t_e4812748 bug 1: note lines keep their ``- `` bullet and gain the
        # ``[#N]`` handle and stamp via ``_render_unit`` (synthesized notes
        # without a source row render exactly as before).
        parts.append("\n".join(f"- {_render_unit(unit)}" for unit in notes))
    parts.append(VERBATIM_CONTEXT_MARKER)
    return "\n\n".join(parts)


# Fold-section delimiters (t_fede7b73).  The fold block is self-delimiting so
# a re-fold removes EXACTLY the section it previously inserted.  The old cut
# heuristic ("to the next ## header") is the section-skeleton form of the
# fold-copy ratchet: the folded prior is full of ## sections, so the cut
# stopped INSIDE the stale fold, orphaned its text as payload sections, and
# the fresh copy landed beside it — measured 0.85-0.90 block duplication on
# the ornith window (Phase-3 fidelity gate, t_c9cb4bec).
#
# t_235275a0 ("No Sections"): the fold header moves from the section form to
# a bracket marker — both sentinels are already on the model-view strip list,
# so the fold stays DB-side scaffolding and the emitted payload carries zero
# ``##`` headers.  Payloads persisted by earlier builds still parse: the
# legacy section header is recognized by ``_find_fold_start``.
_PCS_HEADER = "[PRIOR CONTEXT SUMMARY]"
_PCS_LEGACY_HEADERS = ("## Prior Context Summary",)
_PCS_END_MARKER = "[END OF PRIOR CONTEXT SUMMARY]"
_SECTION_HEADER_RE = re.compile(r"^#{1,6}\s+\S")
# ``[#xx]`` recovery handle on a rendered unit line (ruling 3).
_HANDLE_RE = re.compile(r"\[#(\d+)\]")
# Unit-line decorations around a rendered unit's own text.
_UNIT_DECOR_RE = re.compile(r"^(?:\[#\d+\]\s+)?(?:\[[A-Z]+\]:\s+)?")


def _strip_unit_decor(line: str) -> str:
    """Strip the ``[#id] `` / ``[ROLE]: `` wrapper from a rendered line."""
    return _UNIT_DECOR_RE.sub("", line)


def _manifest_line_key(line: str) -> tuple[str, str, str] | None:
    """``(tool, target, outcome)`` for a tool-action line in any format.

    Handles the ledger bullet form (``- tool: target — outcome``), the unit
    form (``[#1] [TOOL]: tool: target — outcome (…)``) and the call-reference
    form (``tell → tool_call_id <cid>``).  The stamp is not part of the key —
    the same action re-emitted under a different clock is the same fact
    (fold-copy ratchet, t_fede7b73; overlap rule, t_235275a0).
    """
    body = _strip_unit_decor(line.strip())
    if body.startswith("- "):
        body = body[2:].strip()
    if " — " not in body:
        return None
    identity, outcome = body.split(" — ", 1)
    identity = re.sub(r"\s*\([^()]*\)\s*$", "", identity).strip()
    outcome = re.sub(r"\s*\([^()]*\)\s*$", "", outcome).strip()
    match = re.match(r"^(?P<tool>[a-z][\w-]*)\s*[:→]\s*(?P<target>.*)$", identity)
    if not match:
        return None
    tool = match.group("tool").strip().lower()
    target = re.sub(r"\s+", " ", match.group("target")).strip()
    return tool, target, outcome


def _find_fold_start(summary: str) -> tuple[int, int]:
    """``(index, header length)`` of the earliest fold header, either form."""
    best = (-1, 0)
    for header in (_PCS_HEADER, *_PCS_LEGACY_HEADERS):
        index = summary.find(header)
        if index >= 0 and (best[0] < 0 or index < best[0]):
            best = (index, len(header))
    return best


def _normalized_fold_line(line: str) -> str:
    """Whitespace-collapsed form for fold de-duplication line matching."""
    return re.sub(r"\s+", " ", line.strip())


def _drop_repeated_fold_lines(block: str, current_lines: set[str]) -> str:
    """Drop lines the current summary already states (fold-copy ratchet).

    A block re-stating a section header the current summary renders is the
    same SECTION twice in one payload — drop it whole rather than carry the
    section again (or orphan its content under a missing header).  Other
    re-stated content lines are dropped individually; unique lines survive.
    """
    lines = block.splitlines()
    for line in lines:
        if (
            _SECTION_HEADER_RE.match(line)
            and _normalized_fold_line(line) in current_lines
        ):
            return ""
    kept = [
        line
        for line in lines
        if not line.strip() or _normalized_fold_line(line) not in current_lines
    ]
    return "\n".join(kept).strip()


def _canonical_prior_summary(previous_summary: str, current_summary: str) -> str:
    """Flatten one persisted fallback payload into unique semantic paragraphs.

    Internal fallback output is a transport envelope persisted as a transcript
    row.  Carrying that envelope wholesale recursively nests its prefix, prior
    header, and verbatim boundary on every fallback compaction.  Strip only
    those exact transport lines, then retain each semantic paragraph once.
    Paragraphs already regenerated in the current payload need not be carried
    again; older facts absent from the current window still survive.

    Fold-copy de-duplication (t_fede7b73): paragraphs that re-state section
    headers or content lines already present in the current summary are
    dropped before emission — the payload must not carry the same sections
    twice in one block.  Only text the current summary already states is
    dropped, so nothing absent from the current summary is lost.
    """
    prior = (previous_summary or "").strip()
    if not prior:
        return prior

    transport_lines = {
        INTERNAL_FALLBACK_PREFIX,
        _PCS_HEADER,
        *_PCS_LEGACY_HEADERS,
        _PCS_END_MARKER,
        "## Verbatim Recent Context",
        "The messages after this summary marker are preserved verbatim.",
        # t_e4812748 bug 2: the LLM-summarizer envelope pair and the
        # context_compressor boundary line are transport too — a re-ingested
        # prior must never re-nest its own wrappers inside the fold.
        "[PRIOR CONTEXT — for reference only; not a new message]",
        "[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]",
        "--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---",
    }
    current_blocks = {
        block.strip() for block in re.split(r"\n{2,}", current_summary) if block.strip()
    }
    current_lines = {
        _normalized_fold_line(line)
        for line in (current_summary or "").splitlines()
        if line.strip()
    }
    # t_235275a0 ("No duplicate/overlapping manifest-style sections"):
    # a folded line is dropped when it re-states a unit the current payload
    # already carries (same ``[#N]`` row handle), when it re-states a tool
    # action the current stream covers (same tool/target/outcome), or when it
    # is a manifest line with no target at all — the worthless
    # ``terminal:  — ok`` class the smoke-test block was full of.
    current_handles = set(_HANDLE_RE.findall(current_summary or ""))
    current_manifest = {
        key
        for key in (
            _manifest_line_key(line) for line in (current_summary or "").splitlines()
        )
        if key is not None
    }

    def _fold_line_kept(line: str) -> bool:
        stripped = line.strip()
        if not stripped:
            return True
        for handle in _HANDLE_RE.findall(stripped):
            if handle in current_handles:
                return False
        key = _manifest_line_key(stripped)
        if key is None:
            return True
        if not key[1]:
            return False
        return key not in current_manifest

    blocks: list[str] = []
    seen: set[str] = set()
    for raw_block in re.split(r"\n{2,}", prior):
        block = "\n".join(
            line
            for line in raw_block.splitlines()
            if line.strip() not in transport_lines
        ).strip()
        if not block or block in seen or block in current_blocks:
            continue
        seen.add(block)
        block = _drop_repeated_fold_lines(block, current_lines)
        if not block:
            continue
        block = "\n".join(
            line for line in block.splitlines() if _fold_line_kept(line)
        ).strip()
        if not block:
            continue
        blocks.append(block)
    return "\n\n".join(blocks)


# Stage 0.3 (t_57cb0d4e): the prior-summary fold is capped to a share of
# ``target_tokens`` and counted pre-gate. Measured floor: the uncapped fold of
# a saturated fossil is ~14,758 tok against a 9,241 target — accounting alone
# would make ``_fits`` permanently unpassable (the fixed point survives), so
# the cap is mandatory and must leave real tail room. The cap is a SHARE, not
# a constant: it scales with the payload. A/B (ornith 20261002_141842_0ea896a6
# replay, target 9,241): with the tail promise active the gate first passes at
# a cap share ~= the 0.14-0.16 band and the fold converges from 59K to under
# 10K within two iterations; shares >=0.20 re-close the gate on dense tails.
# 0.16 sits above the pass line with margin while keeping the fold a minority
# of the budget.
_FOLD_CAP_SHARE = 0.16


def _cap_prior_tokens(prior: str, cap_tokens: int) -> str:
    """Trim a canonical prior to ``cap_tokens`` at whole-paragraph boundaries.

    Paragraph-wise so the fold carries whole blocks (a half paragraph is
    noise); at least one block is always kept when any prior exists at all,
    so the fold degrades to "newest paragraph" rather than vanishing.
    """
    if cap_tokens <= 0 or not prior:
        return ""
    blocks = prior.split("\n\n")
    keeps: list[str] = []
    tok = 0
    for block in blocks:
        t = estimate_tokens_rough(block)
        if keeps and tok + t > cap_tokens:
            break
        keeps.append(block)
        tok += t
    return "\n\n".join(keeps)


def _drop_stale_fold(summary: str) -> str:
    """Remove fold sections previously inserted by ``_with_prior_summary``.

    Only sentinel-delimited folds are cut: a bare ``## Prior Context Summary``
    heading that arrived inside quoted unit content is payload text, not a
    fold.  Cutting at "the next ## header" (the pre-t_fede7b73 heuristic)
    stopped INSIDE the fold's own section-skeleton content — orphaning the
    previous fold's text as payload sections while a fresh copy landed beside
    it (the measured 0.85-0.90 block duplication, fold-copy ratchet family).
    The sentinel makes the replacement exact and the re-fold idempotent.
    """
    while True:
        start, header_len = _find_fold_start(summary)
        if start < 0:
            return summary
        end = summary.find(_PCS_END_MARKER, start + header_len)
        if end < 0:
            return summary
        head = summary[:start].rstrip()
        tail = summary[end + len(_PCS_END_MARKER):].lstrip()
        summary = (head + ("\n\n" if head and tail else "") + tail).strip()


def _with_prior_summary(
    summary: str,
    previous_summary: str,
    target_tokens: int | None = None,
) -> str:
    """Fold recovered prior context into one canonical fallback block.

    Stage 0.3 (t_57cb0d4e): when ``target_tokens`` is given, the canonical
    prior is capped at ``_FOLD_CAP_SHARE`` of it before folding. Callers that
    pass a target account the capped fold pre-gate; the legacy two-arg form
    (uncapped, post-gate) is kept for callers outside the budget loop.

    The fold is self-delimiting and replaced wholesale on re-fold (the
    t_57cb0d4e convergence guard, exact since t_fede7b73): any fold this
    builder already inserted is dropped before the fresh one is assembled, so
    the payload carries the fold exactly once and ``make_result``'s re-fold is
    idempotent.  The canonical prior is de-duplicated against the summary's
    remaining content before emission (t_fede7b73).
    """
    summary = _drop_stale_fold(summary)
    prior = _canonical_prior_summary(previous_summary, summary)
    if not prior:
        return summary
    if target_tokens is not None:
        capped = _cap_prior_tokens(
            prior, int(target_tokens * _FOLD_CAP_SHARE)
        )
        prior = capped  # cap applies even when empty: an over-cap fold folds nothing
    if not prior:
        return summary
    block = f"{_PCS_HEADER}\n{prior}\n\n{_PCS_END_MARKER}"
    if VERBATIM_CONTEXT_MARKER in summary:
        head, _, rest = summary.partition(VERBATIM_CONTEXT_MARKER)
        return head.rstrip() + "\n\n" + block + "\n\n" + VERBATIM_CONTEXT_MARKER + rest
    return summary.rstrip() + "\n\n" + block


# ---------------------------------------------------------------- Stage 1
# Marker spine (Wintermute design, canonical-inputs §11; t_57cb0d4e).
#
# INVARIANT: marker retention must not depend on which regime the budget
# happens to land in. A marker that passes the keep gate is retained because
# it is a decision mark — never because the heap had leftover budget. Before
# this spine, markers rode `_allocate_two_pass` (the heap's leftover), so the
# plan-only regime dropped them wholesale (measured: target=15000 → 0 markers)
# and `_MAX_CANDIDATE_UNITS`' score≈0 funnel let 4-of-37 survive.
#
# Three parts, all bounded:
#   1. SEPARATE FROM SCORE — spine units bypass `_rank_units` and the 256 cap.
#   2. OWN EMISSION PATH — a fixed small reserve carved from target BEFORE
#      tail measurement; callable from both branches (plan and planless).
#   3. HARD CEILING — a TOKEN SHARE, not a count, so a 20-marker spine is not
#      a rounding error on a 15K-token payload and a 6.7% grab on a 3K one.
#      The ceiling is the anti-bug-4 guard: markers must never outrank real
#      content; boundedness is what prevents the 578c48e436 failure class.
#
# Reserve/ceiling sizing — A/B'd on the ornith replay (target=9,241; the
# task's harness comments carry the tables; do not guess these). The anchor
# run (LLM t_e147304f) kept 1.2 markers/payload at a 0.20% token share — that
# is the FLOOR on the goal ("pass line: >= the anchor's density, with
# evidence", §12), not the ceiling. First cut: reserve 0.25% (~23 tok ≈ 2-3
# markers at the observed ~9 tok/unit) — above the anchor with margin; ceiling
# 0.60% (~55 tok ≈ 6 markers) — bounded ~5x anchor, far below the 6.7% grab
# the share-form exists to prevent. A/B upward from here; the kill criterion
# is marker/content share drifting past the ceiling's own accounting.
_MARK_SPINE_SHARE = 0.0025  # reserved spine budget carved before tail sizing
_MARK_SPINE_CEILING_SHARE = 0.0060  # hard ceiling: the anti-bug-4 guard


def _marker_spine_reserve(target_tokens: int) -> int:
    """Fixed token reserve for the marker spine, carved before tail sizing."""
    return max(0, int(target_tokens * _MARK_SPINE_SHARE))


def _marker_spine_budget(target_tokens: int) -> int:
    """Hard ceiling for the emitted spine, in tokens (the anti-bug-4 guard)."""
    return max(_marker_spine_reserve(target_tokens),
               int(target_tokens * _MARK_SPINE_CEILING_SHARE))


def _marker_spine_block(units: list[_Unit]) -> str:
    """Render the spine: marker units in session order, unit format, no header."""
    if not units:
        return ""
    return "\n".join(
        _render_unit(unit) for unit in sorted(units, key=lambda item: item.order)
    )


def _select_spine_units(
    spine: list[_Unit],
    budget_tokens: int,
    *,
    previous_summary: str = "",
) -> list[_Unit]:
    """Spine keep-set under its ceiling (the anti-bug-4 guard).

    Units are kept oldest-first while the ceiling holds — when the budget is
    short, the OLDEST decision marks cut first, mirroring how the tail keeps
    the newest work.  ``previous_summary`` dedupe (convergence guard,
    t_57cb0d4e): labels still carried inside the prior payload's fold are
    already in the emitted text, so re-admitting them here just regrows the
    block every cycle.  A marker earns spine seats only while it is NEW
    information.  t_235275a0: the kept units render inside the one
    chronological stream — no ``## Decision Markers`` section.
    """
    if not spine or budget_tokens <= 0:
        return []
    carried: set[str] = set()
    for raw in (previous_summary or "").splitlines():
        line = _strip_unit_decor(raw.strip())
        if line.startswith("===") and line.endswith("==="):
            carried.add(line)
    kept: list[_Unit] = []
    spent = 0
    seen_lines: set[str] = set()
    for unit in sorted(spine, key=lambda item: item.order):
        line = (
            _strip_unit_decor(unit.text.splitlines()[0].strip()) if unit.text else ""
        )
        if not line or line in seen_lines or line in carried:
            # Identical repeated labels (e.g. `=== EXIT $? ===` fired once per
            # pipeline stage) carry no additional decision information; the
            # exit context lives in the already-fused tombstone of the first.
            # Labels already folded in from the previous payload: same rule.
            continue
        if kept and spent + unit.token_count > budget_tokens:
            break
        seen_lines.add(line)
        kept.append(unit)
        spent += unit.token_count
    return kept
# ``description`` excluded from the widened keep gate (Evan ruling 2,
# Wintermute's information test): see _marker_keep_label (t_57cb0d4e).


# ------------------------------------------------------- Phase 2 tool-trail
# Tool-trail unit class (council plan 2026-10-14, t_490b92fc): the tool
# provenance ledger nemotron preserved and our fallback dropped by
# construction. One line per action, pair-aware — an error->fix sequence is
# ONE ledger line carrying its interval (t_first–t_second) — bounded by a
# floor-and-ceiling quota, ranked on its own text-shape salience path, and
# emitted like the marker spine: outside the ranker/heap funnel, own
# budget, own block.
#
# STAGE 1 — admission + quota. Admission is SIGNAL-PURE (Wintermute COND 1):
# identity/order signals only — tool_call_id dedup, same-target grouping,
# recency buckets (the order tuple), session order. No content-shape signal
# admits or rejects a row. The dedup key is (target, outcome-class), NOT
# (target) (COND 2): naive newest-occurrence dedup on (target) erases the
# error->fix pair — the highest-value content class measured — while
# (target, outcome-class) keeps both keys and fuses them into one line
# (NOTE A: a pair spans two rows and two clocks; a single snapshot ts would
# recreate the timestamp-loss bug at unit level). NOTE C: one source row ->
# one unit identity with tool_call_id included — ghost twins (same
# tool_call_id re-persisted under the compaction clock) collapse to one
# occurrence instead of feeding the dup machinery this ledger replaces.
#
# STAGE 2 — salience on its OWN path: text-shape score over the rendered
# line (names a path / command / edit / exit). NOT routed through
# llmslim_selector.py — its _EXTERNAL_ROLES branch zeroes tool rows (the
# 37-in/4-survive/0-emitted failure) — and not through _rank_units.
# llmslim_selector.py stays untouched (council).
#
# STAGE 3 — emission: one line per action with provenance (tool, target,
# outcome, interval), chronological, under a hard ceiling.
#
# Quota = floor AND ceiling (Zephyr: "bound+account, cap don't exempt"):
# the FLOOR is a token reserve carved from the tail's budget BEFORE tail
# measurement (carve -> measure -> gate -> emit), so the trail can never be
# sized to zero; the CEILING is the hard cap on the emitted block — the
# anti-bug-4 guard ("trail must not outrank real content"). Both shares are
# Phase-3 tunables pending the 3-arm harness sweep; the ceiling is a
# minority share, well under the fold's 0.16.
_TRAIL_FLOOR_SHARE = 0.005
_TRAIL_CEILING_SHARE = 0.05


def _tool_trail_reserve(target_tokens: int) -> int:
    """Floor: token reserve for the trail, carved before tail sizing."""
    return max(0, int(target_tokens * _TRAIL_FLOOR_SHARE))


def _tool_trail_budget(target_tokens: int) -> int:
    """Ceiling: hard cap for the emitted trail block (anti-bug-4 guard)."""
    return max(
        _tool_trail_reserve(target_tokens),
        int(target_tokens * _TRAIL_CEILING_SHARE),
    )


_TRAIL_TARGET_MAX = 96
_TRAIL_SHELL_RE = re.compile(r"[|;&$`]")
_TRAIL_URL_RE = re.compile(r"^https?://", re.I)
_TRAIL_EDIT_TOOL_RE = re.compile(
    r"\b(?:patch|write_file|edit|multi_edit|apply_patch|str_replace)\b", re.I
)
_TRAIL_EXIT_RE = re.compile(r"\bexit[ _]?-?\d+\b", re.I)

# The action-object argument names, per the tool schemas (terminal:
# ``command``; file tools: ``path``; search: ``pattern``; web: ``query`` /
# ``urls``; write/create tools: ``path`` before ``content``). Schema
# vocabulary, task-agnostic — not values observed in any particular data.
_ACTION_ARG_HINTS = (
    "command", "path", "pattern", "query", "url", "urls", "target", "file",
    "script", "expression", "prompt", "goal", "title", "subject", "message",
    "note", "body", "text", "content", "code", "source", "destination", "input",
)

# Result-dict target hints for ``_target_from_result``: the object-ish subset
# of the schema vocabulary (t_235275a0).
_RESULT_TARGET_HINTS = (
    "command", "path", "pattern", "query", "url", "file", "script",
    "expression", "subject", "name", "title", "source", "destination",
)


@dataclass(frozen=True)
class _TrailOccurrence:
    """One admitted tool action (one source row -> one unit identity)."""

    tool: str
    target: str          # full argument value — the grouping identity
    target_display: str  # truncated rendering for the digest line
    outcome: str         # rendered label ("exit 1", "error: ...", "ok")
    outcome_class: str   # "ok" | "error" | "void" — the dedup key half
    order: tuple[int, ...]
    t_first: float | None
    t_second: float | None
    row_id: int | None = None  # session row id of the outcome row ([#xx] handle)


@dataclass(frozen=True)
class _TrailLine:
    """One fused ledger line — the pair-as-unit (COND 2 / NOTE A)."""

    text: str
    key: str
    order: tuple[int, ...]
    token_count: int
    salience: int
    row_id: int | None = None  # provenance of the first outcome row
    stamp: float | None = None


def _row_id_of(message: dict[str, Any]) -> int | None:
    """Session row id of a message dict — the ``[#xx]`` recovery handle.

    ``get_messages`` rows carry ``id``; ``_rows_to_conversation`` stamps the
    gateway's live history with ``_row_id`` (underscore-prefixed so every
    transport strips it before the wire).  Accept either — production
    messages reach the compressor as one or the other, and a silent ``None``
    here is exactly the t_235275a0 smoke-test finding ("grep ``[#`` → 0").
    """
    for key in ("id", "_row_id"):
        raw = message.get(key)
        if isinstance(raw, int) and not isinstance(raw, bool):
            return raw
    return None


def _stamp_of(message: dict[str, Any]) -> float | None:
    """The row's own clock (never the compaction clock)."""
    raw_ts = message.get("timestamp")
    if isinstance(raw_ts, (int, float)) and not isinstance(raw_ts, bool):
        return float(raw_ts)
    return None


# Result-stub target recovery (t_235275a0).  Production rows frequently carry
# ``function.arguments == "{}"`` (the corrupted-args sanitizer's rewrite, and
# the re-persisted stub copies), so the call arguments cannot name the
# action's object and every emitted line degenerated to ``write_file:  — ok``
# ("filenames are pretty fucking important! Those lines are worthless without
# it." — Evan).  The paired tool RESULT row keeps a one-line stub that DOES
# carry the object (``[terminal] ran `cmd` -> exit 0``,
# ``[write_file] wrote to /path (N lines)``, ``[patch] replace in /path (...)``,
# ``[read_file] read /path from line N (...)``,
# ``[search_files] content search for 'x' in /path -> N matches``), so the
# target is recovered from the stub when the arguments are empty.  Schema
# vocabulary only — the shapes are Hermes' own result-stub format.
_STUB_TARGET_RES: tuple[re.Pattern[str], ...] = (
    # [terminal] ran `cmd` -> exit N / [execute_code] `code` (N lines output)
    re.compile(r"^\[[^\]]+\][^`]*?`([^`]+)`"),
    # [read_file] read <path> from line N (…)
    re.compile(r"^\[[^\]]+\]\s+read\s+(.+?)\s+from line\b"),
    # [write_file] wrote to <path> (…)
    re.compile(r"^\[[^\]]+\]\s+wrote to\s+(.+?)\s*\("),
    # [patch] replace in <path> (…) / [patch] patch in <path> (…)
    re.compile(r"^\[[^\]]+\]\s+\w+\s+in\s+(.+?)\s*\("),
    # [search_files] content search for 'pattern' in <path> -> N matches
    re.compile(r"\bin\s+(\S+)\s*->"),
    # [skill_view] name=<name> (…)
    re.compile(r"\bname=(\S+)"),
    # [web_extract] <url> (…)
    re.compile(r"^\[[^\]]+\]\s+(https?://\S+)"),
    # [set_session] note=<text> (N chars result)
    re.compile(r"\bnote=(.+?)\s*\(\d[\d,]* chars result\)\s*$"),
)


def _target_from_result(content: Any) -> str:
    """Recover the action's target from a tool RESULT row's content.

    Handles the three production result shapes: a decoded dict, a JSON
    string (write_file's summary), and the one-line stub the row mask leaves
    behind (``[terminal] ran `cmd` -> exit 0``).  Corruption markers may sit
    in front of the stub, so every line is tried, not just the first.
    """
    data: dict[str, Any] | None = content if isinstance(content, dict) else None
    if data is None and isinstance(content, str):
        stripped = content.strip()
        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                data = parsed
    if data is not None:
        for key in ("resolved_path", "path"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        files = data.get("files_modified")
        if isinstance(files, list) and files:
            first = files[0]
            if isinstance(first, str) and first.strip():
                return first.strip()
        # Object-ish schema names only — a result dict's "message"/"note"/
        # "text"/"content" fields are OUTCOMES, never the action's object (the
        # longest-string fallback that helps call arguments would surface
        # ``"ok"`` here).
        for name in _RESULT_TARGET_HINTS:
            value = data.get(name)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""
    text = _content_text(content).strip()
    if not text:
        return ""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not line.strip().startswith("["):
            continue
        # The stub may span lines — a terminal command embeds newlines and the
        # closing backtick lands lines later — so match from the stub head to
        # the end of the content instead of line-by-line.
        span = "\n".join(lines[index:])
        for pattern in _STUB_TARGET_RES:
            match = pattern.search(span)
            if match:
                target = re.sub(r"\s+", " ", match.group(1)).strip()
                if target:
                    return target
    return ""


def _tool_target(args: dict[str, Any]) -> str:
    """The action's object, from the call arguments (tool-schema derived).

    The first schema-hint name present wins; otherwise the longest string
    value. Deterministic for identical arguments, which is all same-target
    grouping requires. Content blobs can win the fallback pick, but never
    the hinted one — the hint names are the tools' own parameters.
    """
    for name in _ACTION_ARG_HINTS:
        value = args.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    candidates = [
        value.strip()
        for value in args.values()
        if isinstance(value, str) and value.strip()
    ]
    return max(candidates, key=len) if candidates else ""


def _outcome_of(content: Any) -> tuple[str, str]:
    """(outcome-class, label) from a tool result row's content.

    Schema-derived fields (``exit_code`` / ``error`` / ``success``). Reads
    the RAW content — a decoded dict's ``exit_code`` never reaches
    ``_content_text`` (only string fields do), so extracting from the text
    form alone would lose the outcome of exactly the rows gate (c) opened.
    """
    data: dict[str, Any] | None = content if isinstance(content, dict) else None
    if data is None and isinstance(content, str):
        try:
            parsed = json.loads(content)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            data = parsed
    if data is not None:
        exit_code = data.get("exit_code")
        if isinstance(exit_code, int):
            return ("ok" if exit_code == 0 else "error"), f"exit {exit_code}"
        error = data.get("error")
        if isinstance(error, str) and error.strip():
            return "error", f"error: {error.strip()[:40]}"
        if error:
            return "error", f"error: {str(error)[:40]}"
        if isinstance(data.get("success"), bool):
            ok = bool(data["success"])
            return ("ok" if ok else "error"), ("ok" if ok else "failed")
    text = _content_text(content)
    exit_code = _parse_exit_code(text)
    if exit_code is not None:
        return ("ok" if exit_code == 0 else "error"), f"exit {exit_code}"
    if text.strip():
        return "ok", "ok"
    return "void", "no result"


def _trail_identity(tool: str, display: str) -> str:
    """``tool: target`` — or ``tool → tool_call_id …`` for call references.

    A message-send tool's body is CONTENT, not the action's object (the
    t_235275a0 smoke-test line ``tell: Forensic read of … — ok`` swallowed
    the whole body into the target slot); the settled form points at the
    call instead: ``tell → tool_call_id <cid>``.
    """
    if display.startswith("tool_call_id "):
        return f"{tool} → {display}"
    return f"{tool}: {display}"


# Tools whose action-object is the message itself — rendered as a call
# reference, never by swallowing the body (t_235275a0).
_CALLREF_TARGET_TOOLS = frozenset({"tell"})
# t_e4812748 bug 3: only these tools' targets are shell/code COMMANDS (their
# informative end is the head, so over-cap displays head-truncate). Every
# other target — paths, patterns, prose — keeps its tail when clipped.
_COMMAND_TARGET_TOOLS = frozenset({"terminal", "execute_code", "shell", "bash"})
# t_d3583771 (plan-import-export-flat-file-spec item 4): once the global
# area carries the ground truth, plan tool calls are noise — they must never
# become ledger lines or heap candidates. The plan block is their only
# selection path (same rule as echo markers: nothing is double-emitted).
_PLAN_NOISE_TOOLS = frozenset({"plan"})


def _tool_trail_scan(
    rows: list[dict[str, Any]], start_index: int = 0
) -> list[_TrailOccurrence]:
    """Admit tool actions from a message region (STAGE 1, signal-pure).

    Identity/order only: one occurrence per tool_call_id (NOTE C — ghost
    twins collapse here), paired by tool_call_id, ordered by session
    position. Echo-marker commands are skipped — the marker spine is their
    ONLY selection path ("nothing is double-emitted"). Unpaired rows are
    skipped rather than hallucinated (same rule as the spine).

    Target recovery (t_235275a0): when the call arguments carry no object
    (``arguments == "{}"`` on production rows), the paired result stub is
    parsed for the filename/command; failing that the line references the
    tool_call_id rather than emitting a bare tool name.
    """
    pending: dict[str, tuple[str, str, str, float | None]] = {}
    occurrences: list[_TrailOccurrence] = []
    seen_calls: set[str] = set()
    for offset, message in enumerate(rows):
        role = message.get("role")
        ts = _stamp_of(message)
        if role == "assistant":
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function")
                if not isinstance(fn, dict):
                    continue
                fn_name = str(fn.get("name") or "tool")
                if fn_name in _PLAN_NOISE_TOOLS:
                    continue
                args_raw = fn.get("arguments")
                args: dict[str, Any] = {}
                if isinstance(args_raw, str):
                    try:
                        parsed_args = json.loads(args_raw)
                    except (ValueError, TypeError):
                        parsed_args = None
                    if isinstance(parsed_args, dict):
                        args = parsed_args
                elif isinstance(args_raw, dict):
                    args = args_raw
                command = args.get("command")
                if isinstance(command, str) and _is_echo_marker_command(command):
                    continue
                cid = str(call.get("id") or call.get("call_id") or "")
                if not cid or cid in seen_calls:
                    continue
                seen_calls.add(cid)
                if fn_name in _CALLREF_TARGET_TOOLS:
                    target = display = f"tool_call_id {cid}"
                else:
                    target = _tool_target(args)
                    # One-line collapse (VCC-style): a multi-line argument
                    # value must not break the rendered ledger/unit line.
                    display = re.sub(r"\s+", " ", target).strip()
                    if len(display) > _TRAIL_TARGET_MAX:
                        # t_e4812748 bug 3: head-truncate COMMANDS only.
                        # For paths and prose the informative end is the tail
                        # ("/home/…/gopher/state...." lost the filename; a
                        # tell target lost its conclusion) — keep the tail
                        # for everything that is not a shell/code command.
                        if fn_name in _COMMAND_TARGET_TOOLS:
                            display = display[:_TRAIL_TARGET_MAX].rstrip() + "…"
                        else:
                            display = "…" + display[-_TRAIL_TARGET_MAX:].lstrip()
                pending[cid] = (fn_name, target, display, ts)
        elif role == "tool":
            cid = str(message.get("tool_call_id") or "")
            entry = pending.pop(cid, None)
            if entry is None:
                continue
            fn_name, target, display, call_ts = entry
            outcome_class, outcome = _outcome_of(message.get("content"))
            if not display:
                # Empty call arguments (the corrupted-args sanitizer and the
                # re-persisted stub copies both leave ``"{}"``): recover the
                # object from the result stub so the line keeps its
                # filename/command (t_235275a0).
                target = display = _target_from_result(message.get("content"))
            if not display:
                # Nothing recoverable: reference the call — NOT a bare tool
                # name ("those lines are worthless without it").
                target = display = f"tool_call_id {cid}"
            occurrences.append(
                _TrailOccurrence(
                    tool=fn_name,
                    target=target,
                    target_display=display,
                    outcome=outcome,
                    outcome_class=outcome_class,
                    order=(start_index + offset, 0),
                    t_first=call_ts if call_ts is not None else ts,
                    t_second=ts,
                    row_id=_row_id_of(message),
                )
            )
    return occurrences


def _tool_trail_lines(occurrences: list[_TrailOccurrence]) -> list[_TrailLine]:
    """Pair-aware fusion (COND 2 / NOTE A): one line per action segment.

    Occurrences group by (tool, target); inside a group the outcome-class
    sequence is run-length compressed (the (target, outcome-class) dedup —
    repeats of one class collapse to one representative) and then split
    into segments of at most two runs, the pair-as-unit. An error->fix
    pair therefore survives as ONE line carrying the fused interval
    t_first–t_second, never as two competing rows.
    """
    by_target: dict[tuple[str, str], list[_TrailOccurrence]] = {}
    for occurrence in occurrences:
        by_target.setdefault((occurrence.tool, occurrence.target), []).append(occurrence)

    lines: list[_TrailLine] = []
    for (tool, _target), occs in by_target.items():
        runs: list[list[_TrailOccurrence]] = []
        for occurrence in occs:
            if runs and runs[-1][-1].outcome_class == occurrence.outcome_class:
                runs[-1].append(occurrence)
            else:
                runs.append([occurrence])
        for start in range(0, len(runs), 2):
            segment = runs[start:start + 2]
            fused = [occurrence for run in segment for occurrence in run]
            labels = "→".join(run[0].outcome for run in segment)
            display = fused[0].target_display
            # Unit format (t_235275a0, ruling 3): ``[#id] [ROLE]: text
            # (HH:MM:SS)`` — the handle and the row's own stamp ride
            # ``_render_unit``; the fused span lives in the ``→``-joined
            # outcome labels (``exit 1→exit 0``), never a second clock.
            key = f"{_trail_identity(tool, display)} — {labels}"
            text = f"[TOOL]: {key}"
            lines.append(
                _TrailLine(
                    text=text,
                    key=key,
                    order=fused[0].order,
                    token_count=estimate_tokens_rough(text),
                    salience=_trail_salience(text),
                    row_id=fused[0].row_id,
                    # t_e4812748 bug 4: the handle names fused[0]'s outcome
                    # row, so the clock is that row's own (t_second) and never
                    # another row's — a missing clock renders no clock at all
                    # rather than the pair's call-side one (the old t_first
                    # fallback printed a different row's time beside [#N]).
                    stamp=fused[0].t_second,
                )
            )
    return lines


def _trail_salience(text: str) -> int:
    """STAGE 2, own path: text-shape salience of a rendered ledger line.

    Names a path / command / edit / exit — one point each. Never routed
    through llmslim_selector (its external-role branch zeroes tool rows) or
    _rank_units: this is the trail class's own scoring path.
    """
    score = 0
    if _PATH_RE.search(text):
        score += 1
    if _TRAIL_SHELL_RE.search(text):
        score += 1
    if _TRAIL_EDIT_TOOL_RE.search(text):
        score += 1
    if _TRAIL_EXIT_RE.search(text):
        score += 1
    return score


def _trail_ts(stamp: float) -> str:
    """UTC wall-clock for a row timestamp (interval provenance, NOTE A)."""
    return time.strftime("%H:%M:%S", time.gmtime(stamp))


def _collapsed_tool_units(
    rows: list[dict[str, Any]], start_index: int = 0
) -> list[_Unit]:
    """Collapse tool rows into LexRank units (Evan, 2026-10-21 — t_0a90cfdd).

    *"Tool rows were not supposed to be excluded... They were supposed to be
    collapsed (the way vcc does) not discarded."*  One unit per action
    segment, VCC-style: tool name + target + outcome on one line, exit codes
    preserved, error->fix pairs fused — the same collapse the trail ledger
    renders, fed into the LexRank candidate pool instead of being thrown out.
    ``role="tool"`` keeps llmslim's external-role policy intact (tool text
    cannot self-promote through instruction wording); the small preference
    boost is applied by the rankers, bounded by ``_TOOL_SCORE_FLOOR``.

    Echo-marker commands are skipped here — the marker spine is their ONLY
    selection path ("nothing is double-emitted").
    """
    units: list[_Unit] = []
    for line in _tool_trail_lines(_tool_trail_scan(rows, start_index=start_index)):
        text = f"[TOOL]: {line.key}"
        units.append(
            _Unit(
                text=text,
                order=line.order,
                token_count=estimate_tokens_rough(text),
                role="tool",
                row_id=line.row_id,
                stamp=line.stamp,
            )
        )
    return units


_HANDLE_LEGEND_PREFIX = "[#N] = session row id"


def _with_handle_legend(summary: str, session_id: str | None = None) -> str:
    """One legend line naming the ``[#N]`` key with its ready-to-run command.

    t_e4812748 bug 1b (Evan 2026-10-22): "the marker contains the command to
    open a particular line — don't make small models like Ornith derive it".
    The worked example uses a REAL handle from this payload. Idempotent: an
    existing legend line is replaced, never duplicated. No handles → no
    legend.
    """
    lines = summary.splitlines()
    lines = [ln for ln in lines if not ln.startswith(_HANDLE_LEGEND_PREFIX)]
    first = None
    for ln in lines:
        match = _HANDLE_RE.search(ln)
        if match:
            first = int(match.group(1))
            break
    if first is None:
        return "\n".join(lines)
    sid_arg = f"session_id='{session_id}', " if session_id else ""
    legend = (
        f"{_HANDLE_LEGEND_PREFIX} (rows may age out) — open one with "
        f"session_search({sid_arg}around_message_id={first}, window=5); "
        f"e.g. session_search({sid_arg}around_message_id={first}, window=5) "
        f"opens [#N={first}] — same call with any [#N]'s number."
    )
    insert_at = 1 if lines and lines[0].strip() == INTERNAL_FALLBACK_PREFIX else 0
    lines.insert(insert_at, legend)
    return "\n".join(lines)


def _render_unit(unit: "_Unit | _TrailLine") -> str:
    """Emit a heap unit with its recovery handle and original stamp (ruling 3).

    ``[#xx]`` is the session row id (``session_search(around_message_id=xx)``
    opens the row); the clock is the row's own timestamp, never the compaction
    clock.  Role tags already live in ``unit.text`` (``[ROLE]: `` prefixes).
    Units without a source row (notes, fixtures) render plain.
    """
    handle = f"[#{unit.row_id}] " if unit.row_id is not None else ""
    stamp = f" ({_trail_ts(unit.stamp)})" if unit.stamp is not None else ""
    return f"{handle}{unit.text}{stamp}"


def _tool_trail_block(lines: list[_TrailLine]) -> str:
    """Render the trail lines in unit format, chronological, no header.

    Every line carries its ``[#id]`` handle, ``[TOOL]`` role tag and the
    row's own stamp (t_235275a0 ruling 3) via ``_render_unit`` — the same
    ``[#id] [ROLE]: text (HH:MM:SS)`` form as every other emitted unit.
    """
    if not lines:
        return ""
    return "\n".join(
        _render_unit(item) for item in sorted(lines, key=lambda item: item.order)
    )


def _carried_trail_keys(previous_summary: str) -> set[str]:
    """Action keys already carried by the previous payload.

    Same convergence guard as the marker spine's label dedupe: the fold
    carries old tool lines forward, so re-admitting them would regrow the
    trail every cycle.  Keys are the rendered action identity minus its
    stamp/interval, extracted from BOTH the legacy ledger form
    (``- tool: target — outcome (…)``) and the unit form
    (``[#id] [TOOL]: tool: target — outcome (…)``) anywhere in the payload
    (t_235275a0: no section header bounds the trail anymore).
    """
    keys: set[str] = set()
    for raw in (previous_summary or "").splitlines():
        line = raw.strip()
        if not line or " — " not in line:
            continue
        body = _strip_unit_decor(line)
        if body.startswith("- "):
            body = body[2:].strip()
        if _manifest_line_key(body) is None:
            continue
        keys.add(re.sub(r" \([^()]*\)$", "", body).strip())
    return keys


def _trail_line_unit(line: _TrailLine) -> _Unit:
    """The trail line as a stream unit (same text, provenance, and order)."""
    return _Unit(
        text=line.text,
        order=line.order,
        token_count=line.token_count,
        role="tool",
        row_id=line.row_id,
        stamp=line.stamp,
    )


def _select_trail_lines(
    trail: list[_TrailLine],
    budget_tokens: int,
    *,
    previous_summary: str = "",
    selection_telemetry: dict[str, Any] | None = None,
) -> list[_TrailLine]:
    """Trail keep-set under its ceiling (the anti-bug-4 guard).

    Selection order is Stage-2 salience first with recency as the
    tiebreak — the cut drops lowest-salience/oldest first — while the
    emitted stream stays chronological (curated content keeps session
    order). Lines the previous payload already carries are skipped (the
    convergence guard above). t_235275a0: the kept lines render inside the
    one chronological stream as ``[#id] [TOOL]: …`` units — no
    ``## Actions & Outcomes`` section.
    """
    if not trail or budget_tokens <= 0:
        return []
    carried = _carried_trail_keys(previous_summary)
    kept: list[_TrailLine] = []
    spent = 0
    seen: set[str] = set()
    for line in sorted(
        trail, key=lambda item: (-item.salience, tuple(-part for part in item.order))
    ):
        if line.key in carried or line.key in seen:
            continue
        if kept and spent + line.token_count > budget_tokens:
            # Greedy fill: a bulky low-salience line must not block smaller
            # ones from the same quota.
            continue
        seen.add(line.key)
        kept.append(line)
        spent += line.token_count
    if selection_telemetry is not None:
        info = selection_telemetry.setdefault("tool_trail", {})
        info["emitted_lines"] = len(kept)
        info["emitted_tokens"] = spent
        info["carried_skipped"] = len(carried)
    return kept


_OMISSION_MARKER_PREFIX = (
    "[CONTEXT WINDOW COMPRESSED — middle omitted for length (null mode)]"
)


def _omission_marker_summary(dropped: int) -> str:
    """step5.md item 4: the null-mode payload — an omission marker, only.

    ``dropped`` is the count of middle messages scooped out.  The marker
    rides ``make_result``'s standard assembly (a real compaction: the
    middle leaves the transcript), so downstream persistence and pruning
    see the same shape as every other mode — the payload is just empty
    of selected content by design.
    """
    return (
        f"{_OMISSION_MARKER_PREFIX}\n"
        f"Context omitted for length: {max(dropped, 0)} middle message(s) "
        "were removed without summarisation (internal_only=null control "
        "baseline). Head and verbatim tail are unchanged."
    )


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
    protected_memory: "ProtectedMemory | None" = None,
    session_subject: str = "",
    marker_recent_count: int = 0,
    null_mode: bool = False,
) -> InternalFallback:
    """Build the last-resort local compression payload within ``target_tokens``.

    Never returns ``None``.  ``target_tokens`` is a TARGET, not a ceiling
    (Evan, 2026-09-15): deterministic compression has no failure rung, so an
    over-target or degenerate budget yields the tightest payload this
    selector can assemble rather than stranding the session.

    ``protected_memory`` carries the Protected area's state between
    compactions, and ``session_subject`` is the subject those keys were
    selected under.  When the subject changes the history is discarded and
    the area is rebuilt from scratch — that is the *full* compaction; with
    the subject unchanged only the Heap is rebuilt, which is the *heap*
    compaction that leaves the cacheable prefix byte-stable (Evan,
    2026-09-18).

    ``null_mode`` (step5.md item 4): the control baseline.  Skip the Heap,
    the LexRank selection and the Protected selection entirely — scoop the
    middle out and emit only an omission marker.  Nothing else.  This is
    the floor the Heap must beat to earn its cost.
    """
    raw_head_count = min(max(protect_head_count, 0), len(messages))
    session_id = next(
        (
            str(message.get("session_id"))
            for message in messages
            if message.get("session_id")
        ),
        None,
    )
    system_head = 1 if messages and messages[0].get("role") == "system" else 0
    protected_first_n = max(0, raw_head_count - system_head)
    protected_area_size = protected_first_n + (-(-max(protect_last_n, 0) // 2))
    plan_active = bool(plan_context.strip())
    # Without a Plan, the area's first protect_first_n slots are the raw head.
    # With a Plan, the Plan description replaces those raw early turns and
    # occupies one reserved slot; globally ranked units fill the rest.
    head_count = system_head if plan_active and not null_mode else raw_head_count
    tail_start = _verbatim_tail_start(messages, head_count, protect_last_n)
    # Bounded tail tool-pair collapse (Evan-approved, 2026-11-10): within
    # the protected tail keep only the N most recent tool pairs verbatim
    # (protect_last_n = the pair keep count — the config's double meaning);
    # OLDER tool pairs in the window collapse to one-line tool-trail
    # digests in place. Runs BEFORE tail measurement so the mask/strip
    # rungs and _fits see the light tail (Ornith shape: 78% of the
    # protected tail was tool output inside the window).
    _tail_region, _collapsed_tail_pairs = collapse_tail_tool_pairs(
        messages[tail_start:],
        protect_last_n,
        call_rows=_tool_call_rows_by_id(messages[:tail_start]),
    )
    messages = messages[:tail_start] + _tail_region
    selection_telemetry = _blank_selection_telemetry()

    memory = protected_memory or ProtectedMemory()
    if session_subject and memory.subject and memory.subject != session_subject:
        memory = ProtectedMemory(subject=session_subject)
    elif session_subject:
        memory = ProtectedMemory(recent=memory.recent, subject=session_subject)

    # Stage 2 sweep: size the tier backstop BEFORE any ranked arm runs. The
    # plan arm's _rank_units (:2229 region) executes ahead of the planless arm,
    # so sizing at :2874 leaves the plan path reading a stale ceiling from the
    # previous build call (None on gen0) — measured: S=0.05 == S=inf payloads.
    _set_tier_ceiling(target_tokens)

    if null_mode:
        # step5.md item 4: scoop the middle out, leave only the omission
        # marker.  No ranking, no Protected selection, no prior-summary
        # folding beyond the marker itself — this payload exists to be the
        # floor, so it must never do more work than it must.
        marker = _omission_marker_summary(len(messages[head_count:tail_start]))
        selection_telemetry["selected_count"] = 0
        selection_telemetry["no_op_reason"] = "null_mode"
        # The Protected area is untouched in null mode: carrying state
        # forward would re-introduce a selection the mode must not do, and
        # an area that never changes is the cacheable baseline.
        return InternalFallback(
            marker,
            head_count,
            tail_start,
            "null",
            memory,
            dict(selection_telemetry),
        )

    # Emergency rung (Evan, 2026-09-14): when the verbatim tail itself cannot
    # fit, mask its observations back to the last turn BEFORE shrinking the
    # region.  This preserves the contiguous reasoning/action trace (which
    # tail shrinking would summarise away) and sheds the bulky, disposable
    # half of the turn instead.  `_fit_tail_start` then re-measures the
    # lightened tail and usually succeeds without advancing the boundary.
    #
    # Tail promise (Evan ruling, 2026-10-04 — t_57cb0d4e): masking sheds only
    # ~16% on a tool-dense tail because the bulky rows are the tool RESULTS
    # below the mask floor and the payload-augmented user turns. When the
    # visible-turn keep is still over budget after masking, strip tool calls
    # and observations from all but the LAST turn (the active-work request
    # stays verbatim), then let `_fit_tail_start` bound whatever remains.
    #
    # Stage 1 carve (§11): the spine reserve is subtracted from the tail's
    # budget BEFORE tail measurement, so a 32K tail cannot size it to zero.
    # The tool-trail FLOOR is carved the same way (carve -> measure -> gate
    # -> emit), so the trail can never be sized to zero either.
    tail_tokens = estimate_messages_tokens_rough(messages[tail_start:])
    tail_target = max(
        0,
        target_tokens
        - _marker_spine_reserve(target_tokens)
        - _tool_trail_reserve(target_tokens),
    )
    # Gates (b) + the mask rung: whatever the two rungs destroy from the
    # tail is captured for the tool-trail ledger instead of vanishing. The
    # originals are recovered by tool_call_id because the mask replaces
    # content before the strip drops the row.
    tail_tool_originals = [
        message for message in messages[tail_start:] if _is_tool_flow(message)
    ]
    originals_by_call = {
        str(message.get("tool_call_id") or ""): message
        for message in tail_tool_originals
        if message.get("role") == "tool"
    }
    captured_tail_rows: list[dict[str, Any]] = []
    if tail_tokens > tail_target:
        messages = _mask_tail_observations(messages, tail_start)
        tail_tokens = estimate_messages_tokens_rough(messages[tail_start:])
    if tail_tokens > tail_target:
        messages, dropped_rows = _strip_tail_tool_rows(messages, tail_start)
        tail_tokens = estimate_messages_tokens_rough(messages[tail_start:])
        captured_tail_rows.extend(
            originals_by_call.get(str(row.get("tool_call_id") or ""), row)
            if row.get("role") == "tool"
            else row
            for row in dropped_rows
        )
    for row in messages[tail_start:]:
        # The mask rung keeps its stubs in place but replaced their
        # content — those originals carry the outcomes.
        if row.get("role") == "tool" and row.get("content") == _MASKED_OBSERVATION:
            captured_tail_rows.append(
                originals_by_call.get(str(row.get("tool_call_id") or ""), row)
            )

    # Tool rows are COLLAPSED into the LexRank pool, not discarded (Evan,
    # 2026-10-21 — t_0a90cfdd: *"They were supposed to be collapsed (the way
    # vcc does) not discarded"*).  The ranked region's actions become boosted
    # heap units (``_collapsed_tool_units``); the trail ledger digests only
    # the rows the heap cannot see — actions from before the newest Plan
    # step-change (completed steps stay out of the ranking per Evan
    # 2026-09-16, but their provenance is not noise) and the rows the tail
    # rungs destroyed.  One channel per row: nothing dropped, and no
    # duplication across regions — rows the verbatim tail keeps raw (the
    # newest N tool pairs and the visible rows) are deliberately NOT
    # digested anywhere, and the OLDER tail pairs that
    # ``collapse_tail_tool_pairs`` digested in place above are likewise NOT
    # re-digested here (one channel per row: the digest rows ARE their
    # ledger line).
    rank_start = _plan_step_change_index(messages, head_count, tail_start)
    tool_units = _collapsed_tool_units(
        messages[rank_start:tail_start], start_index=rank_start
    )
    trail_lines = _tool_trail_lines(
        _tool_trail_scan(messages[head_count:rank_start], start_index=head_count)
        + _tool_trail_scan(captured_tail_rows, start_index=tail_start)
    )
    selection_telemetry["tool_trail"] = {
        "candidate_lines": len(trail_lines),
        "captured_tail_rows": len(captured_tail_rows),
        "floor_reserve": _tool_trail_reserve(target_tokens),
        "ceiling_tokens": _tool_trail_budget(target_tokens),
    }
    selection_telemetry["collapsed_tool_units"] = len(tool_units)

    # Guaranteed-seat selection (the t_490b92fc §11 coupling invariant) runs
    # BEFORE the heap budget loops so every render — including the loop's fit
    # probes — measures the final stream: caps honored AND accounted.  The
    # kept lines merge into the chronological stream as units (t_235275a0:
    # no ``## Actions & Outcomes`` section).
    trail_extras = [
        _trail_line_unit(line)
        for line in _select_trail_lines(
            trail_lines,
            _tool_trail_budget(target_tokens),
            previous_summary=previous_summary,
            selection_telemetry=selection_telemetry,
        )
    ]

    notes = _session_notes(messages, memory_context, previous_summary)

    # Protected is selected from the whole window, once, so every path below
    # emits the same area (Evan, 2026-09-18: "you can't full compact without
    # a protected area").  Ranking the entire window is the intent; this
    # bounds the per-iteration cost the same way `_MAX_CANDIDATE_UNITS`
    # already does for the Heap, with `_rank_units` selecting the top-K of
    # however many units the window holds.
    window_units = _message_units(messages[head_count:tail_start], start_index=head_count)
    # step5.md item 3: the *whole* Protected area is
    # ``protect_first_n + ceil(protect_last_n / 2)`` slots.  A planless run
    # spends protect_first_n of them on raw head turns.  An active Plan replaces
    # those raw turns and spends one slot on the Plan description.  Ranked
    # global units may use only the remaining slots — they are not an extra
    # area layered on top of the formula.
    reserved_slots = 1 if plan_active else protected_first_n
    protected_area_budget = max(0, protected_area_size - reserved_slots)
    protected, memory = _persistent_protected(
        window_units, memory, area_budget=protected_area_budget
    )

    def make_result(
        summary: str,
        result_head_count: int,
        result_tail_start: int,
        mode: str,
    ) -> InternalFallback:
        # Stage 0.3 (t_57cb0d4e): the folded summary is capped (share of
        # target) and ACCOUNTED here, before the result is returned, and the
        # tail boundary is re-fit at the real target if the folded payload
        # breaks it. Previously the fold landed entirely post-gate and was
        # only logged as `over_budget` — the loop that pinned every payload
        # at ~59K chars. The capped fold shrinks each cycle (smaller payload
        # -> smaller next fold), which is the convergence the report demands.
        summary = _with_prior_summary(summary, previous_summary, target_tokens)
        # t_e4812748 bug 1b: the legend rides AFTER the fold so it also
        # explains handles the fold carried in, and BEFORE the accounting so
        # its tokens are counted like everything else.
        summary = _with_handle_legend(summary, session_id)
        assembled_tokens = (
            estimate_messages_tokens_rough(
                [
                    *messages[:result_head_count],
                    {"role": "assistant", "content": summary},
                    *messages[result_tail_start:],
                ]
            )
            + _ASSEMBLY_RESERVE_TOKENS
        )
        if assembled_tokens > target_tokens:
            refit = _fit_tail_start(
                messages, result_head_count, result_tail_start, summary, target_tokens
            )
            if refit != result_tail_start:
                result_tail_start = refit
                assembled_tokens = (
                    estimate_messages_tokens_rough(
                        [
                            *messages[:result_head_count],
                            {"role": "assistant", "content": summary},
                            *messages[result_tail_start:],
                        ]
                    )
                    + _ASSEMBLY_RESERVE_TOKENS
                )
        selection_telemetry["assembled_tokens"] = assembled_tokens
        selection_telemetry["over_budget"] = assembled_tokens > target_tokens
        if (
            selection_telemetry["selected_count"] == 0
            and selection_telemetry["no_op_reason"] is None
        ):
            selection_telemetry["no_op_reason"] = "no_candidates"
        return InternalFallback(
            summary,
            result_head_count,
            result_tail_start,
            mode,
            memory,
            dict(selection_telemetry),
        )

    # Stage 0.3 pre-gate accounting is inline in the plan path below (the
    # gate measures the summary WITH its capped fold); the planless path
    # accounts the fold in make_result and re-fits the tail there.

    if plan_context.strip():
        # Spine source (Stage 1): markers bypass the ranker entirely — selected
        # from the pre-tail middle via the production extractor, no 256 cap.
        # Recency-bound (§11 part 2, "dedicated, recency-bounded spine"): the
        # spine carries the most recent ``marker_recent_count`` markers; the
        # ceiling cuts oldest-first on top of that bound. Without the recency
        # bound the spine source grows with the window while the dedupe only
        # sees the capped fold, and cross-cycle re-admission ratchets the
        # block upward (measured: 6 -> 38 markers over chained folds).
        all_spine_units = _terminal_marker_units(
            messages[head_count:tail_start], start_index=head_count
        )
        spine_units = all_spine_units[-marker_recent_count:] if marker_recent_count > 0 else []
        extras = [
            *_select_spine_units(
                spine_units,
                _marker_spine_budget(target_tokens),
                previous_summary=previous_summary,
            ),
            *trail_extras,
        ]
        # Gate measures the summary WITH the capped fold (Stage 0.3): plan +
        # notes compete against the folded summary's real weight. Tail-shrink
        # fallback is unchanged (Stage 0.2: _fit_tail_start works once the
        # payload stops protecting the fossil).
        summary, fitted_tail_start = _fitted_plan_summary(
            messages,
            head_count=head_count,
            tail_start=tail_start,
            plan_context=plan_context,
            notes=notes,
            target_tokens=target_tokens,
        )
        gated_summary = _with_prior_summary(summary, previous_summary, target_tokens)
        if _fits(messages, head_count, fitted_tail_start, gated_summary, target_tokens):
            summary, fitted_tail_start = _add_plan_lexrank_area(
                messages,
                head_count=head_count,
                tail_start=fitted_tail_start,
                summary=gated_summary,
                notes=notes,
                target_tokens=target_tokens,
                protected=protected,
                selection_telemetry=selection_telemetry,
                marker_recent_count=marker_recent_count,
                tool_units=tool_units,
                extras=extras,
            )
            # Stage 0.3: the fold is applied ONCE — `gated_summary` above — and
            # the heap's budget math (`_fits` inside
            # `_add_plan_lexrank_area`) runs against that same folded text via
            # `summary`. make_result folds no-ops (canonical dedup) because
            # the summary already carries the fold.  Spine and trail seats are
            # already inside the stream (see `extras` above).
            return make_result(summary, head_count, fitted_tail_start, "plan")

        # The full plan is itself too large to fit — degrade to
        # goal + current step + summary against the minimal head.
        system_head = 1 if messages and messages[0].get("role") == "system" else 0
        minimal_tail_start = _verbatim_tail_start(
            messages,
            system_head,
            protect_last_n,
        )
        # Spine rides the minimal branch too (§11 part 2: both branches),
        # recency-bounded the same as the primary path.  Selected up front so
        # the heap's fit probes measure the final stream.
        _all_spine = _terminal_marker_units(
            messages[system_head:tail_start], start_index=system_head
        )
        minimal_extras = [
            *_select_spine_units(
                _all_spine[-marker_recent_count:] if marker_recent_count > 0 else [],
                _marker_spine_budget(target_tokens),
                previous_summary=previous_summary,
            ),
            *trail_extras,
        ]
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
            protected=protected,
            selection_telemetry=selection_telemetry,
            marker_recent_count=marker_recent_count,
            tool_units=tool_units,
            extras=minimal_extras,
        )
        summary = _with_prior_summary(summary, previous_summary, target_tokens)
        return make_result(
            summary,
            system_head,
            fitted_tail_start,
            "plan-minimal",
        )

    # Same Heap boundary as the Plan path (Evan, 2026-09-16).  The rule is
    # "IF there is an active plan" — but a Plan that has just closed still
    # leaves its markers in the transcript, and content belonging to completed
    # steps is noise for the current step either way.  Applying it on both
    # paths also keeps the two selectors interchangeable, which the shared
    # `_rank_units` contract already assumes.
    rank_start = _plan_step_change_index(messages, head_count, tail_start)
    middle_messages = messages[rank_start:tail_start]
    middle_units = _message_units(middle_messages, start_index=rank_start) + tool_units
    # Stage 1 (t_57cb0d4e): markers bypass the ranker entirely — the spine is
    # their ONLY selection path, under its own ceiling. Nothing enters the
    # 256-cap / score≈0 funnel, and nothing is double-emitted.
    # Recency-bound the spine source (§11 part 2), same as the plan path.
    all_spine_units = _terminal_marker_units(middle_messages, start_index=rank_start)
    spine_units = (
        all_spine_units[-marker_recent_count:] if marker_recent_count > 0 else []
    )
    extras = [
        *_select_spine_units(
            spine_units,
            _marker_spine_budget(target_tokens),
            previous_summary=previous_summary,
        ),
        *trail_extras,
    ]
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
            role="memory",
        )
        for index, note in enumerate(notes)
    ]
    candidates = middle_units + note_units
    if not candidates:
        selection_telemetry["no_op_reason"] = "no_candidates"
        # The spine and the trail still emit here: an empty heap pool must not
        # cost the markers their guaranteed seat (§11 part 2) nor the ledger
        # its floor — these are the rows the heap cannot carry.  Both seats
        # ride `extras`, so no separate carry step exists to skip.
        summary = _lexrank_summary([], protected, extras)
        return make_result(
            summary,
            head_count,
            _fit_tail_start(messages, head_count, tail_start, summary, target_tokens),
            "lexrank",
        )

    ranked = _rank_units(
        middle_units,
        note_units,
        recent_units,
        protected_units=protected,
        semantic=True,
        selection_telemetry=selection_telemetry,
        tool_boost=True,
    )
    # Same gate as the plan path: only the verbatim tail counts as a repeat
    # source.  Notes are exempt (they route separately below).
    # _content_text, not a raw join: multimodal messages carry `content` as a
    # list of parts, and a raw join raises TypeError mid-compression.
    outside_tiers = [
        _content_tokens(
            "\n".join(_content_text(m.get("content")) for m in messages[tail_start:])
        ),
    ]
    # Same two-pass packing as the Plan path, and the same reasoning: the
    # budget is a TARGET, and `initial` is the empty-heap payload that comes
    # back unchanged when nothing fits.
    selected, summary = _allocate_two_pass(
        ranked,
        target_tokens=target_tokens,
        initial=_lexrank_summary([], protected, extras),
        render=lambda chosen: _lexrank_summary(chosen, protected, extras),
        fits=lambda text: _fits(messages, head_count, tail_start, text, target_tokens),
        accept=lambda unit: unit.is_note
        or unit.is_marker
        or not _heap_repeat_covered(unit.text, outside_tiers),
    )
    # Guaranteed-seat emission (planless branch, §11 part 2): the markers'
    # seat and the trail's floor ride `extras` inside the stream, outside the
    # heap's leftover — the exact coupling the invariant forbids.  The fold is
    # applied here so the final gate below measures the real payload.
    summary = _with_prior_summary(summary, previous_summary, target_tokens)
    _record_selected_units(selection_telemetry, selected)
    _log_inviolable_dropout(summary, ranked)
    if _fits(messages, head_count, tail_start, summary, target_tokens):
        return make_result(summary, head_count, tail_start, "lexrank")
    # Last resort: the requested verbatim tail cannot fit at all.  Shrink
    # the region (summarising its tool scaffolding) instead of aborting —
    # an abort here is terminal for the session, and the scaffolding rows
    # were never asked to be protected verbatim.
    return make_result(
        summary,
        head_count,
        _fit_tail_start(messages, head_count, tail_start, summary, target_tokens),
        "lexrank",
    )
