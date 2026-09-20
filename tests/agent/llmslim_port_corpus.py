"""Sanitized, deterministic corpus for the LLMSlim fallback port.

The shapes are derived from real Hermes sessions, but all names, paths, values,
and tool outputs are synthetic.  No state.db content is copied into this file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CompressionCase:
    name: str
    messages: tuple[dict[str, Any], ...]
    target_tokens: int
    protect_head_count: int = 1
    protect_last_n: int = 3
    plan_context: str = ""
    minimal_plan_context: str = ""
    instruction_markers: tuple[str, ...] = ()
    entity_markers: tuple[str, ...] = ()


def _message(role: str, content: str, **extra: Any) -> dict[str, Any]:
    return {"role": role, "content": content, **extra}


def _lowercase_machine_output() -> CompressionCase:
    messages: list[dict[str, Any]] = [
        _message("system", "Synthetic compression baseline system prompt."),
        _message("user", "Diagnose the backup pipeline and preserve exact evidence."),
    ]
    for index in range(10):
        call_id = f"backup-{index}"
        messages.extend(
            [
                _message(
                    "assistant",
                    "",
                    tool_calls=[{"id": call_id, "type": "function", "function": {"name": "probe", "arguments": "{}"}}],
                ),
                _message(
                    "tool",
                    (
                        f"probe {index}. exit code 0. wrote {40 + index} rows. "
                        f"database /srv/demo/backup-{index}.sqlite remained healthy. "
                        "elapsed 0.2s. checksum verified."
                    ),
                    tool_call_id=call_id,
                ),
                _message(
                    "assistant",
                    f"Backup probe {index} completed; checkpoint BACKUP_CHECKPOINT_{index} is usable.",
                ),
            ]
        )
    messages.extend(
        [
            _message("user", "Never delete the backup database during diagnosis."),
            _message("assistant", "I will preserve the database and report the safest repair."),
        ]
    )
    return CompressionCase(
        name="lowercase_machine_output",
        messages=tuple(messages),
        target_tokens=520,
        instruction_markers=("Never delete the backup database",),
        entity_markers=("/srv/demo/backup-4.sqlite", "BACKUP_CHECKPOINT_4"),
    )


def _mixed_density() -> CompressionCase:
    dense = " ".join(
        f"worker shard {index} processed synthetic queue traffic and repeated ordinary status details."
        for index in range(80)
    )
    return CompressionCase(
        name="mixed_density",
        messages=(
            _message("system", "Synthetic compression baseline system prompt."),
            _message("user", "Repair the queue without changing its public API."),
            _message("assistant", dense),
            _message("user", "The control socket is CONTROL_SOCKET_9443."),
            _message("assistant", "Schema migration SCHEMA_V7 must run before consumers restart."),
            _message("user", "Do not restart the consumer until the migration is verified."),
            _message("assistant", "The queue uses ordinary retry semantics and bounded batches."),
            _message("user", "Continue with the queue repair."),
            _message("assistant", "I am ready to apply the verified change."),
        ),
        target_tokens=360,
        instruction_markers=(
            "must run before consumers restart",
            "Do not restart the consumer",
        ),
        entity_markers=("CONTROL_SOCKET_9443", "SCHEMA_V7"),
    )


def _active_plan() -> CompressionCase:
    return CompressionCase(
        name="active_plan",
        messages=(
            _message("system", "Synthetic compression baseline system prompt."),
            _message("user", "Implement the migration plan."),
            _message("assistant", "Complete Step 1: inspect the existing schema."),
            _message("tool", "legacy schema dump omitted", tool_call_id="schema-1"),
            _message("assistant", "The migration file is /srv/demo/migrations/007_queue.sql."),
            _message("user", "Never modify production data while building the dry run."),
            _message("assistant", "Active Step 2 of 3: build and verify the dry run."),
            _message("user", "Proceed with the current step only."),
            _message("assistant", "The dry-run implementation is in progress."),
        ),
        target_tokens=420,
        plan_context=(
            "Task: Synthetic migration\nGoal: safely migrate queue data\n"
            "Step 2/3: build and verify a dry run\nSummary: schema inspection is complete"
        ),
        minimal_plan_context=(
            "Goal: safely migrate queue data\n"
            "Current step 2/3: build and verify a dry run"
        ),
        instruction_markers=("Never modify production data",),
        entity_markers=("/srv/demo/migrations/007_queue.sql",),
    )


def _artifact_and_reingested_payload() -> CompressionCase:
    return CompressionCase(
        name="artifact_and_reingested_payload",
        messages=(
            _message("system", "Synthetic compression baseline system prompt."),
            _message("user", "Continue the worker repair."),
            _message(
                "assistant",
                "[CONTEXT WINDOW COMPRESSED]\n## Relevant Earlier Context\n"
                "stale payload should not rank\n## Verbatim Recent Context",
            ),
            _message("assistant", "edited /srv/demo/worker.py"),
            _message("assistant", "worker pid 424242 exited with EPIPE"),
            _message("user", "Always preserve the rollback script."),
            _message("assistant", "The rollback script is ROLLBACK_SAFE_17."),
            _message("user", "What is the verified recovery sequence?"),
            _message("assistant", "First validate the worker, then stage the rollback."),
        ),
        target_tokens=340,
        instruction_markers=("Always preserve the rollback script",),
        entity_markers=("/srv/demo/worker.py", "pid 424242", "EPIPE", "ROLLBACK_SAFE_17"),
    )


def _multilingual() -> CompressionCase:
    return CompressionCase(
        name="multilingual",
        messages=(
            _message("system", "Synthetic compression baseline system prompt."),
            _message("user", "Preserve multilingual deployment constraints."),
            _message("assistant", "el servicio falló. reinicie solo el proceso de prueba. verifique PUERTO_8443."),
            _message("user", "本番データを削除しないでください。監査ログを保持してください。"),
            _message("assistant", "服务已停止。不要删除数据库。请保留 AUDIT_CN_9。"),
            _message("user", "Never remove the audit trail."),
            _message("assistant", "I will preserve every audit record."),
        ),
        target_tokens=260,
        instruction_markers=(
            "本番データを削除しないでください",
            "不要删除数据库",
            "Never remove the audit trail",
        ),
        entity_markers=("PUERTO_8443", "AUDIT_CN_9"),
    )


def _real_session_shape() -> CompressionCase:
    messages: list[dict[str, Any]] = [
        _message("system", "Synthetic compression baseline system prompt."),
        _message("user", "Find and fix the synthetic gateway regression."),
    ]
    for index in range(18):
        call_id = f"call-{index}"
        messages.extend(
            [
                _message(
                    "assistant",
                    "",
                    tool_calls=[{"id": call_id, "type": "function", "function": {"name": "read", "arguments": "{}"}}],
                ),
                _message(
                    "tool",
                    (
                        f"row {index}. status ok. lowercase diagnostic line. "
                        f"socket GATEWAY_SOCKET_{index}. file /srv/demo/gateway/{index}.log."
                    ),
                    tool_call_id=call_id,
                ),
            ]
        )
        if index % 3 == 0:
            messages.append(
                _message(
                    "assistant",
                    f"Evidence batch {index} points to synthetic gateway lifecycle handling.",
                )
            )
        if index % 6 == 0:
            messages.append(
                _message("user", f"Keep verified evidence batch USER_ANCHOR_{index}."),
            )
    messages.extend(
        [
            _message("user", "Do not restart the live gateway automatically."),
            _message("assistant", "I will report the fix and leave restart control to the operator."),
        ]
    )
    return CompressionCase(
        name="real_session_shape",
        messages=tuple(messages),
        target_tokens=620,
        instruction_markers=("Do not restart the live gateway",),
        entity_markers=("GATEWAY_SOCKET_12", "USER_ANCHOR_12", "/srv/demo/gateway/12.log"),
    )


def compression_cases() -> tuple[CompressionCase, ...]:
    return (
        _lowercase_machine_output(),
        _mixed_density(),
        _active_plan(),
        _artifact_and_reingested_payload(),
        _multilingual(),
        _real_session_shape(),
    )
