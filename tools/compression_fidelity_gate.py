"""Post-emission fidelity gate for compressor payloads (Phase 3, t_c9cb4bec).

Council plan 2026-10-14 (NOTE B): the gate scores the ACTUAL EMITTED output —
the post-compaction window as the writer emits it — never source rows. A
source-row pre-check only catches the reader; bugs 1-4 were writer-path
corruptions. Three metrics per arm:

  (a) mid-sentence start rate of kept lines
      Headline detector is SOURCE-RELATIVE: a kept line is a violation when
      it is a strict tail of a known source line whose removed head ends
      mid-sentence (not at sentence punctuation) — the bug-1 shape
      ("Summary: ork in this rerun", "ouched.", "mport", "B with both
      tables"). Lines equal to their source (verbatim carries) and
      sentence-aligned extracts are clean. Lines deliberately marked with a
      leading '…' are honest continuations and counted separately.
      An ORTHOGRAPHIC sub-count (lowercase/dangling first character) is
      reported but not gated: legitimate generated lines (tool-trail
      digests "- read_file: …", session notes) start lowercase by design,
      so orthography alone cannot distinguish a cut from a digest line.
  (b) unique-entity recall vs raw window
      What fraction of the distinct artifacts (paths, identifiers, numbers)
      in the raw transcript window appear in the emitted output.
  (c) duplication ratio
      The fraction of output lines covered by repeated blocks (2-line
      shingles occurring more than once) — "no repeated blocks in the
      payload" (Phase 1 block-level dedup class: row re-persistence,
      fold-copy ratchets).

All detectors are task-agnostic: structural marker handling, source-relative
slicing, orthography. No domain vocabulary. The gate is the permanent
regression piece: arm scoring runs it every time and the harness fails the
fold on regressions.
"""

from __future__ import annotations

import re
from typing import Iterable

# --- line-shape helpers -------------------------------------------------

_LABEL_RE = re.compile(r"^(?:[A-Za-z][A-Za-z0-9 /_-]{0,24}):[ \t]+")
_BULLET_RE = re.compile(
    r"^(?:[-*+][ \t]+|\d{1,3}[.)][ \t]+|\[[ xX]\][ \t]+|>[ \t]?|\|[ \t]*)"
)
_HEADER_RE = re.compile(r"^#{1,6}[ \t]")
_FENCE_RE = re.compile(r"^```|^~~~")
# Generated structural/verbatim markers: not kept prose lines.
_MARKER_LINE_RE = re.compile(r"^\[(?:CONTEXT|VERBATIM|END OF|PRIOR CONTEXT)")
_PLAN_LINE_RE = re.compile(r"^\s{0,3}(?:Summary|Denied):[ \t]+(.+)$")
_DANGLING_PUNCT_RE = re.compile(r"^[,.;:!?)\]}]")
_SENTENCE_END = ".!?\"'”’"


def normalize_line(line: str) -> str:
    """Bullet/label-stripped, whitespace-collapsed form of a line."""
    text = line.strip()
    while True:
        stripped = _BULLET_RE.sub("", text, count=1)
        if stripped == text:
            break
        text = stripped
    text = _LABEL_RE.sub("", text, count=1)
    return re.sub(r"\s+", " ", text).strip()


def is_marked_continuation(line: str) -> bool:
    """A line whose head is deliberately elided and marked ('…' prefix)."""
    return line.strip().startswith("…")


def _starts_mid_sentence(text: str) -> bool:
    if not text:
        return False
    if _DANGLING_PUNCT_RE.match(text):
        return True
    first = text[0]
    return first.isalpha() and first.islower()


def _source_pool(sources: Iterable[str]) -> list[str]:
    pool = []
    for source in sources:
        for line in source.splitlines():
            normalized = normalize_line(line)
            if normalized:
                pool.append(normalized)
    return pool


def _tail_cut_hits(content: str, pool: list[str]) -> tuple[bool, int]:
    """Is ``content`` a strict tail of a source line cut mid-sentence?

    Returns ``(violation, matched)``. A match is BENIGN when the removed
    head is empty (verbatim carry) or ends at sentence punctuation
    (sentence-aligned extract). A violation needs at least one match and no
    benign one.
    """
    if len(content) < 12:
        return False, 0
    matched = 0
    benign = False
    for source in pool:
        if source == content:
            return False, 1
        if not source.endswith(content):
            continue
        matched += 1
        head = source[: len(source) - len(content)].rstrip()
        if not head or head[-1] in _SENTENCE_END:
            benign = True
    return (matched > 0 and not benign), matched


def mid_sentence_start_rate(payload: str, sources: Iterable[str] = ()) -> dict:
    """Metric (a): share of kept lines whose head was cut mid-sentence."""
    pool = _source_pool(sources)
    kept = 0
    violations: list[dict] = []
    marked = 0
    orthographic = 0
    in_fence = False
    for line in payload.splitlines():
        text = line.strip()
        if not text:
            continue
        if _FENCE_RE.match(text):
            in_fence = not in_fence
            continue
        if in_fence or _HEADER_RE.match(text) or _MARKER_LINE_RE.match(text):
            continue
        if is_marked_continuation(text):
            marked += 1
            continue
        content = normalize_line(text)
        if not content:
            continue
        kept += 1
        if _starts_mid_sentence(content):
            orthographic += 1
        cut, matched = _tail_cut_hits(content, pool)
        if cut:
            violations.append(
                {"line": text[:160], "tail_cut": True, "source_matches": matched}
            )
    return {
        "rate": (len(violations) / kept) if kept else 0.0,
        "kept_lines": kept,
        "violations": violations,
        "marked_continuations": marked,
        "orthographic_starts": orthographic,
    }


# --- metric (b): unique-entity recall -----------------------------------

_PATH_TOKEN_RE = re.compile(
    r"(?:~|\.{1,2})?/[A-Za-z0-9_.\-/]*[A-Za-z0-9]"
    r"|[A-Za-z0-9_.\-]+\.[A-Za-z0-9]{1,8}"
)
_IDENT_TOKEN_RE = re.compile(
    r"\b[A-Za-z_][A-Za-z0-9_\-]{5,}\b"
    r"|\b[a-z]+[A-Z][A-Za-z0-9]{3,}\b"
    r"|\b[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]+\b"
    r"|\b[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_.]*\b"
)
_NUMBER_TOKEN_RE = re.compile(r"\b\d{2,}(?:\.\d+)?\b")


def extract_entities(text: str) -> dict[str, set[str]]:
    """Distinct task-agnostic artifact families: paths, identifiers, numbers."""
    paths = set(_PATH_TOKEN_RE.findall(text))
    identifiers = set(_IDENT_TOKEN_RE.findall(text))
    numbers = set(_NUMBER_TOKEN_RE.findall(text))
    identifiers -= paths  # keep families disjoint for per-family recall
    return {"paths": paths, "identifiers": identifiers, "numbers": numbers}


def entity_recall(raw_window_text: str, payload: str) -> dict:
    """Metric (b): fraction of raw-window artifacts present in the payload."""
    window = extract_entities(raw_window_text)
    emitted = extract_entities(payload)
    per_family = {}
    found_all: set[str] = set()
    window_all: set[str] = set()
    for family, values in window.items():
        window_all |= values
        hit = values & emitted.get(family, set())
        found_all |= hit
        per_family[family] = {
            "window": len(values),
            "found": len(hit),
            "recall": (len(hit) / len(values)) if values else 1.0,
        }
    return {
        "recall": (len(found_all) / len(window_all)) if window_all else 1.0,
        "window_entities": len(window_all),
        "found_entities": len(found_all),
        "per_family": per_family,
    }


# --- metric (c): duplication ratio --------------------------------------

def duplication_ratio(payload: str) -> dict:
    """Metric (c): share of lines covered by repeated 2-line blocks."""
    lines = [re.sub(r"\s+", " ", line).strip() for line in payload.splitlines()]
    lines = [line for line in lines if line]
    if len(lines) < 2:
        return {
            "ratio": 0.0,
            "lines": len(lines),
            "repeated_blocks": 0,
            "duplicated_lines": 0,
        }
    seen: dict[tuple[str, str], list[int]] = {}
    for index in range(len(lines) - 1):
        seen.setdefault((lines[index], lines[index + 1]), []).append(index)
    duplicated: set[int] = set()
    blocks = 0
    for positions in seen.values():
        if len(positions) > 1:
            blocks += 1
            for index in positions:
                duplicated.add(index)
                duplicated.add(index + 1)
    total = len(lines)
    return {
        "ratio": (len(duplicated) / total) if total else 0.0,
        "lines": total,
        "repeated_blocks": blocks,
        "duplicated_lines": len(duplicated),
    }


# --- plan-head report (bug 1, targeted) --------------------------------

def _plan_bodies(text: str) -> list[str]:
    bodies = []
    for line in text.splitlines():
        match = _PLAN_LINE_RE.match(line)
        if match:
            bodies.append(match.group(1).strip())
    return bodies


def plan_head_report(payload: str, plan_context: str) -> dict:
    """Did the writer introduce head cuts in plan summaries?

    Each 'Summary:'/'Denied:' body in the payload is compared against the
    source bodies rendered into ``plan_context`` (what the writer receives).
    Exact match = carried verbatim (intact head). A strict suffix of a longer
    source body = an INTRODUCED head cut (bug 1). A prefix of a longer source
    body = a tail cut (reported separately). Source bodies already starting
    mid-sentence were born cut UPSTREAM (pre-Phase-1 write-time cap) — not
    this writer's doing, counted as ``source_bodies_headless_at_rest``.
    """
    source_bodies = _plan_bodies(plan_context)
    payload_bodies = _plan_bodies(payload)
    report = {
        "payload_bodies": len(payload_bodies),
        "source_bodies": len(source_bodies),
        "carried_verbatim": 0,
        "introduced_head_cuts": [],
        "introduced_tail_cuts": 0,
        "unmatched": 0,
        "source_bodies_headless_at_rest": 0,
    }
    for body in payload_bodies:
        if any(
            body != source and source.endswith(body) and len(source) > len(body)
            and (source[len(source) - len(body) - 1].isalnum()
                 or source[len(source) - len(body) - 1] == "_")
            for source in source_bodies
        ):
            report["introduced_head_cuts"].append(body[:120])
            continue
        if any(
            body != source and source.startswith(body) and len(source) > len(body)
            for source in source_bodies
        ):
            report["introduced_tail_cuts"] += 1
            continue
        if body in source_bodies:
            report["carried_verbatim"] += 1
        else:
            report["unmatched"] += 1
    for source in source_bodies:
        if _starts_mid_sentence(source):
            report["source_bodies_headless_at_rest"] += 1
    return report
