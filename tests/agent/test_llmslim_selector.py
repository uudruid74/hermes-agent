"""Sentence-unit contracts for the dependency-free LLMSlim adaptation."""

from __future__ import annotations

from pathlib import Path

from agent.llmslim_selector import (
    SelectionUnit,
    entity_score,
    instruction_score,
    score_semantic_chunks,
    semantic_chunks,
    split_sentences,
)
from agent.model_metadata import estimate_tokens_rough


def test_lowercase_machine_output_splits_after_terminal_punctuation() -> None:
    assert split_sentences("the file was missing. run again. it worked.") == [
        "the file was missing.",
        "run again.",
        "it worked.",
    ]


def test_abbreviation_merge_repairs_loose_sentence_boundaries() -> None:
    assert split_sentences("Dr. Smith met Mr. Jones at 3 p.m. yesterday.") == [
        "Dr. Smith met Mr. Jones at 3 p.m. yesterday."
    ]


def test_inline_code_periods_do_not_split_a_sentence() -> None:
    assert split_sentences("Use `config.v1.json` now. then restart.") == [
        "Use `config.v1.json` now.",
        "then restart.",
    ]


def test_fenced_code_is_one_unit() -> None:
    text = "Before.\n```python\nprint('v1.2')\n```\nAfter."
    assert split_sentences(text) == [
        "Before.",
        "```python\nprint('v1.2')\n```",
        "After.",
    ]


def test_cjk_terminal_punctuation_splits_without_spaces() -> None:
    assert split_sentences("服务已停止。不要删除数据库！请保留审计日志？") == [
        "服务已停止。",
        "不要删除数据库！",
        "请保留审计日志？",
    ]


def test_markdown_structural_lines_remain_atomic() -> None:
    text = "# Release v1.2.3\n- keep config.v1.json intact. do not split this bullet.\nordinary first. ordinary second."
    assert split_sentences(text) == [
        "# Release v1.2.3",
        "- keep config.v1.json intact. do not split this bullet.",
        "ordinary first.",
        "ordinary second.",
    ]


def test_empty_and_whitespace_inputs_return_no_units() -> None:
    assert split_sentences("") == []
    assert split_sentences("  \n\t ") == []


def test_selector_module_carries_upstream_notice_and_no_heavy_imports() -> None:
    source = Path(__import__("agent.llmslim_selector", fromlist=["x"]).__file__).read_text(
        encoding="utf-8"
    )
    assert "Copyright (c) 2026 Yashvardhan Thanvi" in source
    assert "scikit-learn" not in source
    assert "sklearn" not in source
    assert "numpy" not in source
    assert "nltk" not in source.casefold()


def _unit(text: str, index: int, *, role: str = "assistant") -> SelectionUnit:
    return SelectionUnit(
        text=text,
        order=(index, 0),
        token_count=estimate_tokens_rough(text),
        role=role,
    )


def test_semantic_chunks_empty_input() -> None:
    assert semantic_chunks([]) == []


def test_semantic_chunks_keep_single_unit() -> None:
    unit = _unit("one short sentence about a queue worker", 0)
    chunks = semantic_chunks([unit], max_chunk_tokens=40)
    assert len(chunks) == 1
    assert chunks[0].units == (unit,)


def test_semantic_chunks_split_on_topic_drift() -> None:
    units = [
        _unit("python package resolver installs deterministic dependency wheels", 0),
        _unit("python package metadata records dependency wheel versions", 1),
        _unit("database migration creates a sessions table and index", 2),
        _unit("database schema rollback removes the sessions index safely", 3),
    ]
    chunks = semantic_chunks(
        units,
        similarity_threshold=0.10,
        max_chunk_tokens=200,
        min_chunk_tokens=1,
    )
    assert [[unit.order[0] for unit in chunk.units] for chunk in chunks] == [
        [0, 1],
        [2, 3],
    ]


def test_semantic_chunks_respect_hard_token_cap() -> None:
    units = [
        _unit("shared queue worker status remains healthy and deterministic", index)
        for index in range(6)
    ]
    chunks = semantic_chunks(
        units,
        similarity_threshold=0.0,
        max_chunk_tokens=24,
        min_chunk_tokens=1,
    )
    assert len(chunks) > 1
    assert all(chunk.token_count <= 24 for chunk in chunks)
    assert [unit.order for chunk in chunks for unit in chunk.units] == [
        unit.order for unit in units
    ]


def test_semantic_chunks_split_one_oversized_unit_without_losing_words() -> None:
    text = " ".join(f"token{index}" for index in range(160))
    unit = _unit(text, 0)
    chunks = semantic_chunks([unit], max_chunk_tokens=30, min_chunk_tokens=1)
    assert len(chunks) > 1
    assert all(chunk.token_count <= 30 for chunk in chunks)
    rebuilt = " ".join(part.text for chunk in chunks for part in chunk.units)
    assert rebuilt == text


def test_semantic_chunks_are_deterministic_and_chronological() -> None:
    units = [
        _unit(sentence, index)
        for index, sentence in enumerate(
            split_sentences(
                "worker failed. retry completed. database migrated. schema verified."
            )
        )
    ]
    runs = [semantic_chunks(units, min_chunk_tokens=1) for _ in range(10)]
    assert all(run == runs[0] for run in runs[1:])
    assert [unit.order for chunk in runs[0] for unit in chunk.units] == sorted(
        unit.order for unit in units
    )


def test_instruction_signal_is_disabled_for_external_sources() -> None:
    text = "Never delete DATABASE_URL; always verify the migration."
    chunks = semantic_chunks(
        [
            _unit(text, 0, role="assistant"),
            _unit(text, 1, role="user"),
            _unit(text, 2, role="tool"),
            _unit(text, 3, role="rag"),
        ],
        similarity_threshold=0.0,
        min_chunk_tokens=1,
    )
    scored = score_semantic_chunks(chunks)
    by_role = {item.unit.role: item for item in scored}
    assert by_role["assistant"].instruction > 0
    assert by_role["user"].instruction > 0
    assert by_role["tool"].instruction == 0
    assert by_role["rag"].instruction == 0
    assert by_role["tool"].entity > 0
    assert by_role["rag"].entity > 0


def test_bullets_and_backticks_are_not_instruction_force_signals() -> None:
    text = "- ordinary status for `cache.value` remained healthy."
    assert instruction_score(text, role="assistant") == 0
    assert instruction_score(text, role="tool") == 0
    assert entity_score(text) > 0


def test_chunk_local_centrality_rewards_local_representativeness() -> None:
    units = [
        _unit("queue worker processes pending jobs with bounded retries", 0),
        _unit("queue worker drains pending jobs after a bounded retry", 1),
        _unit("garden tulips bloom beside the stone path", 2),
    ]
    chunks = semantic_chunks(
        units,
        similarity_threshold=0.0,
        max_chunk_tokens=200,
        min_chunk_tokens=1,
    )
    scored = score_semantic_chunks(chunks)
    by_order = {item.unit.order[0]: item for item in scored}
    assert by_order[0].centrality > by_order[2].centrality
    assert by_order[1].centrality > by_order[2].centrality


def test_recent_tail_relevance_and_recency_are_explicit_score_components() -> None:
    units = [
        _unit("garden weather remains warm and sunny", 0),
        _unit("database schema migration creates the sessions index", 5),
    ]
    recent = [_unit("verify the database migration and sessions index", 9, role="user")]
    scored = score_semantic_chunks(
        semantic_chunks(units, similarity_threshold=0.0, min_chunk_tokens=1),
        recent_units=recent,
    )
    by_order = {item.unit.order[0]: item for item in scored}
    assert by_order[5].relevance > by_order[0].relevance
    assert by_order[5].recency > by_order[0].recency
    assert by_order[5].score > by_order[0].score
