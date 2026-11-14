"""Bounded tool-pair collapse inside the protected tail (2026-11-10 spec).

Evan-approved design relayed by Gopher: ``protect_last_n: N`` keeps its public
meaning — N *visible turns* bound the tail region — but on tool-dense sessions
that window swallows ~150 tool pairs (Ornith's wedged session: 8 visible turns
= 323 rows, tool output = 78% of the protected tail). Within the protected
region the N most recent tool-call PAIRS (assistant ``tool_calls`` entry + its
matching tool result rows) now stay verbatim with FULL results, and every
OLDER pair in the region collapses to one one-line tool-trail digest (the
Phase-2 t_490b92fc line format) placed where the pair's rows were.

Acceptance (spec items 1-5) plus belt survival: digest rows must survive
``repair_message_sequence`` (the pre-API alternation/pairing belt) instead of
being dropped as stray tool results. The belt normalizes their wire role to
``role=assistant`` narration (``role=tool`` without a ``tool_call_id`` is
invalid OpenAI shape and strict providers reject the request with HTTP 400),
and Pass 0 folds runs of them into adjacent assistant turns.
"""

from __future__ import annotations

import json

from agent.agent_runtime_helpers import repair_message_sequence
from agent.internal_compression_fallback import (
    collapse_tail_tool_pairs,
    is_trail_digest_message,
)


# ---------------------------------------------------------------------------
# Fixture builders — the measured Ornith shape: 8 visible turns / 150 pairs
# ---------------------------------------------------------------------------


def _tool_pair(i: int, *, output_len: int = 800) -> list[dict]:
    cid = f"call_{i}"
    call_row = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": cid,
                "type": "function",
                "function": {
                    "name": "terminal",
                    "arguments": json.dumps({"command": f"run step {i}"}),
                },
            }
        ],
    }
    result_row = {
        "role": "tool",
        "tool_call_id": cid,
        "content": f"step {i} output " + ("x" * output_len),
    }
    return [call_row, result_row]


def _visible_turn(i: int) -> list[dict]:
    return [
        {"role": "user", "content": f"request {i}"},
        {"role": "assistant", "content": f"answer {i}"},
    ]


def _ornith_shape(*, n_visible: int = 8, n_pairs: int = 150) -> list[dict]:
    """8 visible turns with 150 tool pairs interleaved (Ornith's shape)."""
    rows: list[dict] = []
    base = n_pairs // n_visible
    extra = n_pairs % n_visible
    pair = 0
    for turn in range(n_visible):
        rows.extend(_visible_turn(turn))
        count = base + (1 if turn < extra else 0)
        for _ in range(count):
            rows.extend(_tool_pair(pair))
            pair += 1
    return rows


def _tail_tokens(rows: list[dict]) -> int:
    from agent.internal_compression_fallback import estimate_messages_tokens_rough

    return estimate_messages_tokens_rough(rows)


# ---------------------------------------------------------------------------
# Acceptance 1 — 8 newest pairs verbatim, ~142 older pairs digested, tail
# tool weight collapses toward digest size
# ---------------------------------------------------------------------------


class TestBoundedCollapse:
    def test_eight_newest_pairs_verbatim_older_pairs_digested(self):
        rows = _ornith_shape()
        new_rows, collapsed = collapse_tail_tool_pairs(rows, keep_pairs=8)

        digests = [r for r in new_rows if is_trail_digest_message(r)]
        assert len(digests) == 150 - 8, (
            f"expected 142 digests for 150 pairs with 8 kept, got {len(digests)}"
        )
        # Every digest is a one-line tool-trail digest in the ledger format.
        for d in digests:
            assert d["role"] == "tool"
            assert not d.get("tool_call_id")
            assert d["content"].startswith("[TOOL]: ")
            assert "\n" not in d["content"]

        # The 8 newest pairs ride verbatim: their rows survive byte-identical.
        tail_pairs = _ornith_shape()[-16:]  # last 8 pairs = 16 rows
        kept_tool_rows = [
            r for r in new_rows if r.get("role") == "tool" and not is_trail_digest_message(r)
        ]
        kept_call_rows = [
            r for r in new_rows if r.get("role") == "assistant" and r.get("tool_calls")
        ]
        assert kept_call_rows == tail_pairs[0::2], "newest 8 call rows must be verbatim"
        assert kept_tool_rows == tail_pairs[1::2], "newest 8 result rows must be verbatim"
        assert len(collapsed) == 2 * 142, "collapsed originals are the removed rows"

    def test_tail_tool_weight_collapses_toward_digest_size(self):
        rows = _ornith_shape()
        before = _tail_tokens(rows)
        new_rows, _ = collapse_tail_tool_pairs(rows, keep_pairs=8)
        after = _tail_tokens(new_rows)
        assert after < before * 0.5, (
            f"tail weight must drop from {before} to under half, got {after}"
        )


# ---------------------------------------------------------------------------
# Acceptance 2 — digests survive the belt as normalized assistant
# narration and the belt output carries NO orphan role=tool rows (wire
# contract: every role=tool message needs its tool_call_id)
# ---------------------------------------------------------------------------


class TestAlternationAndBelt:
    def test_digests_survive_belt_and_no_orphan_tool_rows(self):
        rows = _ornith_shape()
        new_rows, _ = collapse_tail_tool_pairs(rows, keep_pairs=8)
        digest_lines = [r["content"] for r in new_rows if is_trail_digest_message(r)]
        assert len(digest_lines) == 142
        msgs = [dict(r) for r in new_rows]
        repair_message_sequence(None, msgs)
        # Regression for the cheaperinference HTTP 400 ("Tool message must
        # have tool_call_id"): the belt output must contain no role=tool
        # message without a tool_call_id.
        assert all(
            m.get("tool_call_id") for m in msgs if m.get("role") == "tool"
        ), "belt output must not carry orphan role=tool messages"
        kept_tool_rows = [
            m for m in msgs if m.get("role") == "tool" and m.get("tool_call_id")
        ]
        assert len(kept_tool_rows) == 8, "the 8 newest pairs ride verbatim"
        # Digest provenance survives as assistant narration (Pass 0 folds
        # runs of digest rows into adjacent assistant turns).
        joined = "\n".join(
            m["content"] for m in msgs if isinstance(m.get("content"), str)
        )
        lost = [d for d in digest_lines if d not in joined]
        assert not lost, f"{len(lost)} digest lines were lost at the belt"

    def test_belt_drops_unmarked_strays_but_keeps_digests(self):
        # The normalization is narrow: a bare stray tool row is still
        # dropped; a digest row survives as normalized assistant narration.
        stray = {"role": "tool", "tool_call_id": "call_gone", "content": "orphan"}
        digest = {"role": "tool", "content": "[TOOL]: terminal run — ok"}
        msgs = [
            {"role": "user", "content": "hi"},
            stray,
            digest,
            {"role": "assistant", "content": "ok"},
        ]
        repaired = repair_message_sequence(None, msgs)
        assert stray not in msgs
        assert digest in msgs
        assert digest["role"] == "assistant", (
            "digest rows must leave the belt as assistant narration — "
            "role=tool without tool_call_id is rejected by strict providers"
        )
        # 3 repairs: 1 digest role normalization + 1 assistant merge (the
        # digest folds into the trailing assistant turn) + 1 stray drop.
        assert repaired == 3


# ---------------------------------------------------------------------------
# Acceptance 3 — light-session invariant: <N pairs in window → byte-identical
# behavior to today
# ---------------------------------------------------------------------------


class TestLightSessionInvariant:
    def test_fewer_pairs_than_keep_is_byte_identical(self):
        rows: list[dict] = []
        rows.extend(_visible_turn(0))
        rows.extend(_tool_pair(0))
        rows.extend(_visible_turn(1))
        rows.extend(_tool_pair(1))
        rows.extend(_tool_pair(2))
        new_rows, collapsed = collapse_tail_tool_pairs(rows, keep_pairs=8)
        assert collapsed == []
        assert new_rows == rows, "light sessions must be byte-identical"


# ---------------------------------------------------------------------------
# Acceptance 4 — Oct-27 ruling: kept visible rows and kept pairs are NEVER
# mutated; only OLDER tool scaffolding in the window collapses
# ---------------------------------------------------------------------------


class TestNeverMutateKept:
    def test_visible_rows_and_kept_pairs_never_mutated(self):
        rows = _ornith_shape()
        snapshot = [dict(r) for r in rows]
        new_rows, _ = collapse_tail_tool_pairs(rows, keep_pairs=8)
        assert rows == snapshot, "input list must never be mutated"
        # Every non-digest output row is byte-identical to an input row.
        for r in new_rows:
            if is_trail_digest_message(r):
                continue
            assert r in snapshot, f"non-digest row was mutated: {r!r}"

    def test_straddle_rule_keeps_whole_rows(self):
        # One assistant row carrying 3 calls at the keep boundary: row-atomic
        # kept sets must keep the whole row and ALL its results (kept rows are
        # never mutated), so kept pairs exceed keep_pairs at the straddle.
        cid = [f"call_s{i}" for i in range(3)]
        straddle_row = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": c,
                    "type": "function",
                    "function": {
                        "name": "terminal",
                        "arguments": json.dumps({"command": f"cmd {c}"}),
                    },
                }
                for c in cid
            ],
        }
        results = [{"role": "tool", "tool_call_id": c, "content": f"out {c} " + "y" * 500} for c in cid]
        rows = [_visible_turn(0)[0]] + [_tool_pair(9)[0], _tool_pair(9)[1]] + [straddle_row] + results
        new_rows, collapsed = collapse_tail_tool_pairs(rows, keep_pairs=1)
        # keep=1 would keep just the newest pair, but its assistant row holds
        # all 3 calls — the row is kept whole with all 3 results.
        assert straddle_row in new_rows
        assert all(r in new_rows for r in results), "straddle row keeps all results"
        # The older pair (below the straddle) still collapsed — one digest
        # line per collapsed pair.
        digests = [r for r in new_rows if is_trail_digest_message(r)]
        assert len(digests) == 1, "one digest line per collapsed pair"


# ---------------------------------------------------------------------------
# §8 mutation proof — the keep rule is load-bearing: with keep_pairs=0 the
# fixture collapses everything, so the keep assertions above are not vacuous
# ---------------------------------------------------------------------------


class TestKeepRuleLoadBearing:
    def test_keep_zero_collapses_the_whole_fixture(self):
        rows = _ornith_shape()
        new_rows, collapsed = collapse_tail_tool_pairs(rows, keep_pairs=0)
        digests = [r for r in new_rows if is_trail_digest_message(r)]
        assert len(digests) == 150, (
            "with keep_pairs=0 every pair must digest — proves the fixture "
            "distinguishes kept from collapsed and the 8-kept rule matters"
        )
        assert len(collapsed) == 300


# ---------------------------------------------------------------------------
# Acceptance 5 — the config comment carries the double meaning
# ---------------------------------------------------------------------------


class TestConfigComment:
    def test_protect_last_n_comment_states_the_double_meaning(self):
        import inspect

        import hermes_cli.config_defaults as cfg

        source = inspect.getsource(cfg)
        idx = source.find('"protect_last_n"')
        assert idx != -1
        window = " ".join(source[idx : idx + 600].replace("#", " ").split())
        assert "N visible turns" in window or "visible turns bound the tail" in window
        assert "tool-call pairs" in window or "tool pairs" in window
