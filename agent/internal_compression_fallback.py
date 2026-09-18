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


# Canonical-form stopwords (Evan, 2026-09-15).  Two units that differ only in
# function words carry the same information, so dedupe must compare them on
# content words alone.
_STOPWORDS = frozenset(
    """
    a an and are as at be been but by can could did do does for from had has
    have he her him his how i if in into is it its me my no nor not of on or
    our out over own should so some such than that the their them then there
    these they this those to too us was we were what when where which who
    whom why will with would you your yours
    """.split()
)


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


def _heap_repeat_covered(
    unit_text: str,
    tier_tokens: list[frozenset[str]],
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
    for tier in tier_tokens:
        if tier and len(tokens & tier) / len(tokens) >= _HEAP_REPEAT_COVERAGE:
            return True
    return False


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
        role = str(message.get("role") or "unknown").upper()
        content = _prune_reingested_payload(_content_text(message.get("content")))
        for unit_offset, chunk in enumerate(_chunks(content)):
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
        # Notes and pruned-skill reload markers are protected content: they
        # are short by nature ("Critical deployment note: never restart the
        # database automatically.") and were never the junk this filter
        # targets.  A length floor applied to them empties the emitted block
        # and takes the whole fallback down with it.
        protected = unit.is_note or bool(_PRUNED_SKILL_RE.search(unit.text))
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

    all_vectors = _tfidf_vectors(
        [unit.text for unit in candidates] + [unit.text for unit in recent_units],
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
        scores.append(score)

    ordered = sorted(
        zip(candidates, scores),
        key=lambda item: (-item[1], item[0].order),
    )[:_MAX_CANDIDATE_UNITS]
    return _mmr_order(ordered)


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
    overlap of content-word sets.  O(n^2) on <=256 candidates.

    The returned scores are the original ranking scores, unchanged: this
    decides order, not value, so callers that only read the score are
    unaffected.
    """
    if len(ranked) < 2 or lam <= 0:
        return ranked

    remaining = list(ranked)
    token_sets = {id(unit): _content_tokens(unit.text) for unit, _score in remaining}
    chosen: list[tuple[_Unit, float]] = []
    picked_tokens: list[frozenset[str]] = []

    while remaining:
        best_index = 0
        best_value = float("-inf")
        for index, (unit, score) in enumerate(remaining):
            tokens = token_sets[id(unit)]
            overlap = 0.0
            if tokens and picked_tokens:
                for previous in picked_tokens:
                    union = len(tokens | previous)
                    if union:
                        similarity = len(tokens & previous) / union
                        if similarity > overlap:
                            overlap = similarity
            value = score - lam * overlap
            if value > best_value:
                best_value = value
                best_index = index
        unit, score = remaining.pop(best_index)
        chosen.append((unit, score))
        picked_tokens.append(token_sets[id(unit)])

    return chosen


def _with_lexrank_section(summary: str, selected: list[_Unit]) -> str:
    """Append the heap block below the Plan, above the verbatim marker.

    Plan first, heap second (Evan, 2026-09-16) — *"Plan should be first"*.  The
    Plan holds the long-term goals and the step list and is byte-stable
    between step changes, so it rides the prompt cache; the heap is
    re-selected every cycle and has no such claim.  Ordering is therefore
    load-bearing, not cosmetic.
    """
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
    if not middle_messages:
        return summary, tail_start
    middle_units = _message_units(middle_messages, start_index=rank_start)
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

    selected: list[_Unit] = []
    best = summary
    for unit, _score in ranked:
        if _heap_repeat_covered(unit.text, outside_tiers):
            continue
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

    # Same Heap boundary as the Plan path (Evan, 2026-09-16).  The rule is
    # "IF there is an active plan" — but a Plan that has just closed still
    # leaves its markers in the transcript, and content belonging to completed
    # steps is noise for the current step either way.  Applying it on both
    # paths also keeps the two selectors interchangeable, which the shared
    # `_rank_units` contract already assumes.
    rank_start = _plan_step_change_index(messages, head_count, tail_start)
    middle_messages = messages[rank_start:tail_start]
    middle_units = _message_units(middle_messages, start_index=rank_start)
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
    # Same gate as the plan path: only the verbatim tail counts as a repeat
    # source.  Notes are exempt (they route separately below).
    # _content_text, not a raw join: multimodal messages carry `content` as a
    # list of parts, and a raw join raises TypeError mid-compression.
    outside_tiers = [
        _content_tokens(
            "\n".join(_content_text(m.get("content")) for m in messages[tail_start:])
        ),
    ]
    selected: list[_Unit] = []
    for unit, _score in ranked:
        if not unit.is_note and _heap_repeat_covered(unit.text, outside_tiers):
            continue
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
