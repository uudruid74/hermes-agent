"""Read-only legacy-vs-semantic compression benchmark.

The live-session path opens each profile database with SQLite ``mode=ro`` and
prints aggregate metrics only. It never emits message text, identifiers, or
credentials. Run from the repository root with::

    python tests/agent/llmslim_port_benchmark.py
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import json
import logging
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterator

from agent import internal_compression_fallback as fallback_module
from agent.internal_compression_fallback import (
    _ASSEMBLY_RESERVE_TOKENS,
    _content_text,
    build_internal_fallback,
)
from agent.model_metadata import estimate_messages_tokens_rough
from tests.agent.llmslim_port_corpus import CompressionCase, compression_cases

_PROFILES = ("neo", "gopher", "ornith", "wintermute")
_LIVE_TARGET_TOKENS = 12_000
_JSON_CONTENT_PREFIX = "\x00json:"


def _decode_content(value: Any) -> Any:
    if isinstance(value, str) and value.startswith(_JSON_CONTENT_PREFIX):
        try:
            return json.loads(value[len(_JSON_CONTENT_PREFIX) :])
        except (json.JSONDecodeError, TypeError):
            return value
    return value


def _load_latest_snapshot(profile: str) -> list[dict[str, Any]]:
    db_path = Path("/home/ekl/.hermes/profiles") / profile / "state.db"
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        session = connection.execute(
            """
            SELECT s.id
            FROM sessions AS s
            JOIN messages AS m ON m.session_id = s.id AND m.active = 1
            WHERE s.id NOT LIKE 'cron_%'
            GROUP BY s.id
            ORDER BY MAX(m.id) DESC
            LIMIT 1
            """
        ).fetchone()
        if session is None:
            return []
        rows = connection.execute(
            """
            SELECT role, content, api_content, tool_call_id, tool_calls, tool_name
            FROM messages
            WHERE session_id = ? AND active = 1
            ORDER BY id
            """,
            (session["id"],),
        ).fetchall()
    finally:
        connection.close()

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "Read-only compression benchmark snapshot."}
    ]
    for row in rows:
        role = row["role"]
        if role not in {"user", "assistant", "tool"}:
            continue
        api_content = row["api_content"]
        content = api_content if isinstance(api_content, str) and api_content else row["content"]
        message: dict[str, Any] = {
            "role": role,
            "content": _decode_content(content),
        }
        if row["tool_call_id"]:
            message["tool_call_id"] = row["tool_call_id"]
        if row["tool_calls"]:
            try:
                message["tool_calls"] = json.loads(row["tool_calls"])
            except (json.JSONDecodeError, TypeError):
                message["tool_calls"] = []
        if row["tool_name"]:
            message["name"] = row["tool_name"]
        messages.append(message)
    return messages


@contextmanager
def _legacy_selector() -> Iterator[None]:
    original_rank = fallback_module._rank_units
    original_split = fallback_module.split_sentences

    def rank_without_semantics(*args: Any, **kwargs: Any):
        kwargs["semantic"] = False
        kwargs.pop("selection_telemetry", None)
        return original_rank(*args, **kwargs)

    def legacy_split(text: str) -> list[str]:
        return [
            part.strip()
            for part in re.split(r"(?<=[.!?])\s+|\n{2,}", text.strip())
            if part.strip()
        ]

    fallback_module._rank_units = rank_without_semantics
    fallback_module.split_sentences = legacy_split
    try:
        yield
    finally:
        fallback_module._rank_units = original_rank
        fallback_module.split_sentences = original_split


def _tool_pairing_intact(messages: list[dict[str, Any]], tail_start: int) -> bool:
    seen_calls: set[str] = set()
    for message in messages[tail_start:]:
        for call in message.get("tool_calls") or []:
            call_id = call.get("id")
            if isinstance(call_id, str):
                seen_calls.add(call_id)
        if message.get("role") == "tool":
            call_id = message.get("tool_call_id")
            if isinstance(call_id, str) and call_id not in seen_calls:
                return False
    return True


def _run_once(
    messages: list[dict[str, Any]],
    *,
    target_tokens: int,
    protect_head_count: int,
    protect_last_n: int,
    plan_context: str = "",
    minimal_plan_context: str = "",
    legacy: bool = False,
) -> tuple[Any, dict[str, Any]]:
    original = deepcopy(messages)
    if legacy:
        with _legacy_selector():
            result = build_internal_fallback(
                messages,
                protect_head_count=protect_head_count,
                protect_last_n=protect_last_n,
                target_tokens=target_tokens,
                plan_context=plan_context,
                minimal_plan_context=minimal_plan_context,
            )
    else:
        result = build_internal_fallback(
            messages,
            protect_head_count=protect_head_count,
            protect_last_n=protect_last_n,
            target_tokens=target_tokens,
            plan_context=plan_context,
            minimal_plan_context=minimal_plan_context,
        )
    assembled = [
        *messages[: result.head_count],
        {"role": "assistant", "content": result.summary},
        *messages[result.tail_start :],
    ]
    middle_tool_tokens = estimate_messages_tokens_rough(
        [
            message
            for message in messages[result.head_count : result.tail_start]
            if message.get("role") == "tool"
        ]
    )
    stats = result.selection_telemetry
    caller_assembled_tokens = (
        estimate_messages_tokens_rough(assembled) + _ASSEMBLY_RESERVE_TOKENS
    )
    metrics = {
        "input_rows": len(messages),
        # The fallback estimate is authoritative for its deterministic target:
        # it includes the emergency observation mask applied on a private copy.
        "assembled_tokens": int(stats.get("assembled_tokens") or 0),
        "caller_reconstruction_tokens": caller_assembled_tokens,
        "target_tokens": target_tokens,
        "fits": not bool(stats.get("over_budget")),
        "selected_count": int(stats.get("selected_count") or 0),
        "selected_tokens": int(stats.get("selected_tokens") or 0),
        "candidate_count": int(stats.get("candidate_count") or 0),
        "no_op_reason": stats.get("no_op_reason"),
        "lowercase_tool_reduction": (
            1.0 if middle_tool_tokens and "[TOOL]:" not in result.summary else None
        ),
        "middle_tool_tokens": middle_tool_tokens,
        "caller_unchanged": messages == original,
        "tail_pairing_intact": _tool_pairing_intact(messages, result.tail_start),
        "tail_starts_validly": (
            result.tail_start >= len(messages)
            or messages[result.tail_start].get("role") != "tool"
        ),
    }
    return result, metrics


def _marker_count(summary: str, markers: tuple[str, ...]) -> int:
    return sum(marker in summary for marker in markers)


def _benchmark_case(case: CompressionCase) -> dict[str, Any]:
    kwargs = {
        "target_tokens": case.target_tokens,
        "protect_head_count": case.protect_head_count,
        "protect_last_n": case.protect_last_n,
        "plan_context": case.plan_context,
        "minimal_plan_context": case.minimal_plan_context,
    }
    legacy_result, legacy = _run_once(list(case.messages), legacy=True, **kwargs)
    semantic_result, semantic = _run_once(list(case.messages), legacy=False, **kwargs)
    legacy["instructions_retained"] = _marker_count(
        legacy_result.summary, case.instruction_markers
    )
    semantic["instructions_retained"] = _marker_count(
        semantic_result.summary, case.instruction_markers
    )
    legacy["entities_retained"] = _marker_count(
        legacy_result.summary, case.entity_markers
    )
    semantic["entities_retained"] = _marker_count(
        semantic_result.summary, case.entity_markers
    )
    return {"legacy": legacy, "semantic": semantic}


def _assert_requirements(results: dict[str, Any]) -> None:
    failures: list[str] = []
    for corpus, entries in results.items():
        for name, comparison in entries.items():
            semantic = comparison["semantic"]
            label = f"{corpus}/{name}"
            if not semantic["fits"]:
                failures.append(f"{label}: semantic output exceeded equal budget")
            if semantic["selected_count"] == 0 and not semantic["no_op_reason"]:
                failures.append(f"{label}: silent no-op")
            if semantic["middle_tool_tokens"] and semantic["lowercase_tool_reduction"] != 1.0:
                failures.append(f"{label}: lowercase tool content leaked into summary")
            for invariant in ("caller_unchanged", "tail_pairing_intact", "tail_starts_validly"):
                if not semantic[invariant]:
                    failures.append(f"{label}: invariant failed: {invariant}")
    if failures:
        raise AssertionError("\n".join(failures))


def main() -> None:
    # Live transcripts can contain sensitive fragments. Silence fallback warnings
    # and emit aggregate metrics only.
    logging.disable(logging.WARNING)
    sanitized = {case.name: _benchmark_case(case) for case in compression_cases()}
    live: dict[str, Any] = {}
    for profile in _PROFILES:
        messages = _load_latest_snapshot(profile)
        legacy_result, legacy = _run_once(
            deepcopy(messages),
            target_tokens=_LIVE_TARGET_TOKENS,
            protect_head_count=1,
            protect_last_n=8,
            legacy=True,
        )
        semantic_result, semantic = _run_once(
            deepcopy(messages),
            target_tokens=_LIVE_TARGET_TOKENS,
            protect_head_count=1,
            protect_last_n=8,
            legacy=False,
        )
        # Do not emit either summary: live message content is sensitive.
        del legacy_result, semantic_result
        live[profile] = {"legacy": legacy, "semantic": semantic}

    results = {"sanitized": sanitized, "live": live}
    print(json.dumps(results, indent=2, sort_keys=True))
    _assert_requirements(results)


if __name__ == "__main__":
    main()
