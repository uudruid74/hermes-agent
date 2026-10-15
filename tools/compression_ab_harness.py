#!/usr/bin/env python3
"""3-arm same-transcript A/B harness + post-emission fidelity gate (Phase 3).

Council plan 2026-10-14, "Phase 3 — Proof (harness)". Replays ONE raw
transcript window through three compressors that differ ONLY in the
summariser (Zephyr's controls: same transcript, same model settings, same
target_ratio, same protect_head/tail):

  arm i    fallback, Phase-1 module (68d1e5fc56) — no tool-trail unit class
  arm ii   fallback, HEAD module (7272e16f41)  — tool-trail unit class
  arm iii  LLM summariser (auxiliary.compression model, production seam)

i-vs-ii is the isolation: the two fallback modules differ by exactly one
commit (the trail unit class), so the delta IS the unit-class change.

Each arm drives the PRODUCTION entry point ``ContextCompressor.compress()``
with only ``agent.context_compressor.build_internal_fallback`` swapped
(arm i) or ``internal_only`` flipped (arm iii) — region derivation, pre-pass,
fold, and emission are production code on every arm.

The fidelity gate (tools/compression_fidelity_gate.py) scores the EMITTED
post-compaction window, never source rows:

  (a) mid-sentence start rate of kept lines   (must be ~0)
  (b) unique-entity recall vs raw window      (paths/identifiers/numbers)
  (c) duplication ratio                       (no repeated blocks)

No hard-coded transcript values (Evan): every data decision is a CLI flag;
the harness generalizes to any state.db session or JSON fixture.

Provenance (driver output protocol): line 1 is this file's sha256, then
argv0/cwd/git head/module hashes and the live signatures of the swapped
functions, before any number is printed. ``HARNESS OK`` on the last line
marks a complete run (exit 0).

Usage (the Phase-3 Ornith run):
  python3 tools/compression_ab_harness.py \
      --db ~/.hermes/profiles/ornith/state.db \
      --session 20261013_150231_331cf384 --start-row 241344 --end-row 241538 \
      --plan-context-file plan_context.txt --protect-first-n 1 --protect-last-n 8 \
      --threshold-percent 0.85 --target-ratio 0.15 --context-length 78000 \
      --json-out run.json
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import inspect
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.context_compressor import (  # noqa: E402
    _MERGED_PRIOR_CONTEXT_HEADER,
    ContextCompressor,
)
import agent.context_compressor as _cc  # noqa: E402
from agent.internal_compression_fallback import (  # noqa: E402
    INTERNAL_FALLBACK_PREFIX,
)
from tools.compression_fidelity_gate import (  # noqa: E402
    duplication_ratio,
    entity_recall,
    mid_sentence_start_rate,
    plan_head_report,
)

PHASE1_FALLBACK_REF = "68d1e5fc56"  # phase-1 integrity floor (t_de77589d)
DEFAULT_SUMMARY_PREFIXES = ("[CONTEXT SUMMARY]:", "[CONTEXT WINDOW COMPRESSED")


# ---------------------------------------------------------------- loading

def load_window_from_db(
    db_path: str, session_id: str, start_row: int | None, end_row: int | None
) -> tuple[list[dict], dict]:
    """Load the raw transcript window from a Hermes state.db messages table.

    The extractor contract (5th-instance pitfall): the ``tool_calls`` column
    is JSON and MUST be decoded (raw strings score as content, silently
    zeroing tool-unit measurements); tool rows keep ``tool_call_id``.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    sql = (
        "SELECT id, role, content, tool_call_id, tool_calls, tool_name, timestamp "
        "FROM messages WHERE session_id = ?"
    )
    params: list = [session_id]
    if start_row is not None:
        sql += " AND id >= ?"
        params.append(start_row)
    if end_row is not None:
        sql += " AND id <= ?"
        params.append(end_row)
    sql += " ORDER BY id"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    messages = []
    report = {"rows_read": len(rows), "session_meta_dropped": 0, "tool_rows": 0}
    for row in rows:
        if row["role"] == "session_meta":
            report["session_meta_dropped"] += 1
            continue
        message: dict = {
            "role": row["role"],
            "content": row["content"] or "",
            "timestamp": row["timestamp"],
        }
        if row["tool_call_id"]:
            message["tool_call_id"] = row["tool_call_id"]
        if row["tool_name"]:
            message["tool_name"] = row["tool_name"]
        if row["tool_calls"]:
            try:
                message["tool_calls"] = json.loads(row["tool_calls"])
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"messages row {row['id']}: tool_calls is not valid JSON "
                    f"({exc}) — extractor contract requires decodable tool_calls"
                ) from exc
        if row["role"] == "tool":
            report["tool_rows"] += 1
        messages.append(message)
    return messages, report


def load_window_from_fixture(path: str) -> tuple[list[dict], dict]:
    data = json.loads(Path(path).read_text())
    if not isinstance(data, list) or not all(isinstance(m, dict) for m in data):
        raise ValueError(f"{path}: fixture must be a JSON list of message dicts")
    return data, {"rows_read": len(data), "fixture": path}


def window_text(messages: list[dict]) -> str:
    """Deterministic text serialization shared by input and output scoring."""
    parts = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            content = json.dumps(content, ensure_ascii=False, sort_keys=True)
        elif not isinstance(content, str):
            content = "" if content is None else str(content)
        if content:
            parts.append(content)
        tool_calls = message.get("tool_calls")
        if tool_calls:
            parts.append(json.dumps(tool_calls, ensure_ascii=False, sort_keys=True))
        name = message.get("tool_name")
        if name:
            parts.append(str(name))
    return "\n".join(parts)


def extract_payload_block(messages: list[dict]) -> str:
    """The compaction rows among the emitted window (summary / merged fold)."""
    parts = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, str) or not content:
            continue
        if (
            ContextCompressor._starts_with_summary_prefix(content)
            or _MERGED_PRIOR_CONTEXT_HEADER in content
            or content.startswith(INTERNAL_FALLBACK_PREFIX)
        ):
            parts.append(content)
    return "\n".join(parts)


# ------------------------------------------------------------- provenance

def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def load_phase1_module(repo_root: Path, ref: str) -> tuple[Any, str, str]:
    """Extract the Phase-1 fallback module from git and import it isolated."""
    proc = subprocess.run(
        ["git", "show", f"{ref}:agent/internal_compression_fallback.py"],
        cwd=repo_root, capture_output=True, text=True, check=True,
    )
    source = proc.stdout
    with tempfile.NamedTemporaryFile(
        "w", suffix=".py", prefix="phase1_fallback_", delete=False
    ) as handle:
        handle.write(source)
        tmp_path = handle.name
    spec = importlib.util.spec_from_file_location(
        "phase1_internal_compression_fallback", tmp_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot build an import spec for the Phase-1 module ({tmp_path})")
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves string annotations through sys.modules at class
    # creation time — the module must be registered before exec.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module, ref, _sha256_text(source)


def print_provenance(args, phase1_info) -> None:
    driver_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    print(f"DRIVER_SHA256 {driver_hash}")
    print(f"argv0: {sys.argv[0]}")
    print(f"cwd: {Path.cwd()}")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True
    ).stdout.strip()
    print(f"git_head: {head}")
    if phase1_info is not None:
        module, ref, source_hash = phase1_info
        print(f"phase1_ref: {ref}  module_sha256: {source_hash}")
        print(
            "signature build_internal_fallback(phase1): "
            f"{inspect.signature(module.build_internal_fallback)}"
        )
    head_hash = _sha256_text(
        (REPO_ROOT / "agent" / "internal_compression_fallback.py").read_text()
    )
    print(f"head_module_sha256: {head_hash}")
    print(
        "signature build_internal_fallback(head): "
        f"{inspect.signature(_cc.build_internal_fallback)}"
    )
    print(
        "signature ContextCompressor._generate_summary: "
        f"{inspect.signature(ContextCompressor._generate_summary)}"
    )
    print(
        "signature ContextCompressor.compress: "
        f"{inspect.signature(ContextCompressor.compress)}"
    )
    print(f"controls: {json.dumps({k: v for k, v in vars(args).items() if k != 'func'}, sort_keys=True)}")


# ------------------------------------------------------------------- arms

def make_compressor(args, *, internal_only, summary_model="") -> ContextCompressor:
    compressor = ContextCompressor(
        model=args.main_model,
        threshold_percent=args.threshold_percent,
        protect_first_n=args.protect_first_n,
        protect_last_n=args.protect_last_n,
        summary_target_ratio=args.target_ratio,
        quiet_mode=True,
        config_context_length=args.context_length,
        internal_only=internal_only,
        summary_model_override=summary_model or "",
        abort_on_summary_failure=False,
    )
    # Unbound-session state the production runtime sets up; a bare replay
    # instance must degrade exactly like an unbound runtime (both helpers
    # document "" / no-op behaviour for the unbound case).
    compressor._session_db = None
    compressor._session_id = ""
    compressor._previous_summary = None
    compressor._summary_has_user_turn = None
    compressor._summary_failure_cooldown_until = 0.0
    return compressor


def run_arm(
    arm_name: str,
    args,
    messages: list[dict],
    plan_context: str,
    minimal_plan_context: str,
    memory_context: str,
    *,
    use_phase1: bool = False,
    use_llm: bool = False,
    summary_model: str = "",
) -> dict:
    """Drive the production compress() entry point with the summariser swapped."""
    internal_only = False if use_llm else True
    compressor = make_compressor(
        args,
        internal_only=internal_only,
        summary_model=summary_model if use_llm else "",
    )
    original_builder = _cc.build_internal_fallback
    if use_phase1:
        if PHASE1_MODULE is None:
            raise RuntimeError("arm i requested but the Phase-1 module was not loaded")
        _cc.build_internal_fallback = PHASE1_MODULE.build_internal_fallback
    try:
        started = time.monotonic()
        out_messages = compressor.compress(
            [dict(message) for message in messages],
            current_tokens=None,
            force=True,
            memory_context=memory_context,
            plan_context=plan_context,
            minimal_plan_context=minimal_plan_context,
        )
        duration = time.monotonic() - started
    finally:
        _cc.build_internal_fallback = original_builder

    emitted = window_text(out_messages)
    block = extract_payload_block(out_messages)
    result = {
        "arm": arm_name,
        "duration_s": round(duration, 2),
        "window_rows_in": len(messages),
        "window_rows_out": len(out_messages),
        "emitted_chars": len(emitted),
        "payload_block_chars": len(block),
        "compressor_state": {
            "summary_fallback_used": bool(getattr(compressor, "_last_summary_fallback_used", False)),
            "summary_dropped_count": int(getattr(compressor, "_last_summary_dropped_count", 0) or 0),
            "summary_error": getattr(compressor, "_last_summary_error", None),
            "fallback_mode": (compressor._last_compression_telemetry or {}).get("fallback_mode")
            if isinstance(getattr(compressor, "_last_compression_telemetry", None), dict)
            else None,
        },
    }
    return {
        "result": result,
        "out_messages": out_messages,
        "emitted_text": emitted,
        "block_text": block,
    }


# ------------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_argument_group("transcript source")
    source.add_argument("--db", help="state.db path (read-only)")
    source.add_argument("--session", help="session_id in the state.db")
    source.add_argument("--start-row", type=int, default=None)
    source.add_argument("--end-row", type=int, default=None)
    source.add_argument("--fixture", help="JSON fixture (list of message dicts)")
    source.add_argument("--export-fixture", help="write the loaded window here")

    plan = parser.add_argument_group("plan context")
    plan.add_argument("--plan-context-file")
    plan.add_argument("--minimal-plan-context-file")
    plan.add_argument("--plan-task", help="kanban task id to render plan context from")
    plan.add_argument("--kanban-db", help="kanban.db path for --plan-task")
    plan.add_argument("--memory-context-file")

    controls = parser.add_argument_group("controls (identical across arms)")
    controls.add_argument("--context-length", type=int, default=78000)
    controls.add_argument("--protect-first-n", type=int, default=3)
    controls.add_argument("--protect-last-n", type=int, default=20)
    controls.add_argument("--threshold-percent", type=float, default=0.50)
    controls.add_argument("--target-ratio", type=float, default=0.20)
    controls.add_argument(
        "--main-model", default="harness/replay",
        help="main-model name for compressor identity fields (not the summariser)",
    )
    controls.add_argument(
        "--summary-model", default=None,
        help="LLM summariser model (arm iii); default: "
        "auxiliary.compression.model from config; '' disables arm iii",
    )
    controls.add_argument("--arms", default="i,ii,iii", help="subset of i,ii,iii")

    repo = parser.add_argument_group("repo / gate")
    repo.add_argument("--repo", default=str(REPO_ROOT))
    repo.add_argument("--phase1-ref", default=PHASE1_FALLBACK_REF)
    repo.add_argument("--json-out")
    repo.add_argument("--gate", action="store_true", help="exit 1 on threshold violation")
    repo.add_argument("--max-mid-sentence-rate", type=float, default=0.0)
    repo.add_argument("--max-duplication-ratio", type=float, default=0.0)
    args = parser.parse_args()

    global PHASE1_MODULE
    arms_selected = {a.strip() for a in args.arms.split(",") if a.strip()}
    repo_root = Path(args.repo)

    phase1_info = None
    if "i" in arms_selected:
        PHASE1_MODULE, ref, source_hash = load_phase1_module(repo_root, args.phase1_ref)
        phase1_info = (PHASE1_MODULE, ref, source_hash)
    print_provenance(args, phase1_info)

    # ---- transcript
    if args.fixture:
        messages, loader_report = load_window_from_fixture(args.fixture)
    elif args.db and args.session:
        messages, loader_report = load_window_from_db(
            args.db, args.session, args.start_row, args.end_row
        )
    else:
        print("ERROR: provide --fixture or --db + --session")
        return 2
    if not messages:
        print("ERROR: empty window")
        return 2
    print(f"loader: {json.dumps(loader_report, sort_keys=True)}")
    raw_text = window_text(messages)
    raw_hash = _sha256_text(raw_text)
    print(f"raw_window: rows={len(messages)} chars={len(raw_text)} sha256={raw_hash}")
    raw_reference = {
        "a_mid_sentence": mid_sentence_start_rate(raw_text, [raw_text]),
        "c_duplication": duplication_ratio(raw_text),
    }
    print(
        f"raw_window_reference: (a) rate={raw_reference['a_mid_sentence']['rate']:.4f} "
        f"orthographic_starts={raw_reference['a_mid_sentence']['orthographic_starts']} "
        f"(c) dup={raw_reference['c_duplication']['ratio']:.4f} "
        f"blocks={raw_reference['c_duplication']['repeated_blocks']} "
        f"(baseline subtraction: writer-added vs carried-from-source)"
    )

    if args.export_fixture:
        Path(args.export_fixture).write_text(json.dumps(messages, ensure_ascii=False, indent=1))
        print(f"fixture_exported: {args.export_fixture}")

    # ---- plan context
    if args.plan_context_file:
        plan_context = Path(args.plan_context_file).read_text()
        minimal = (
            Path(args.minimal_plan_context_file).read_text()
            if args.minimal_plan_context_file
            else ""
        )
    elif args.plan_task and args.kanban_db:
        from tools.plan_binding_adapter import _format_plan

        conn = sqlite3.connect(f"file:{args.kanban_db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        task = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (args.plan_task,)
        ).fetchone()
        if task is None:
            print(f"ERROR: plan task {args.plan_task} not found")
            return 2
        plan_context = _format_plan(conn, task, include_summaries=True)
        minimal = _format_plan(conn, task, include_summaries=True, minimal=True)
        conn.close()
    else:
        plan_context, minimal = "", ""
    memory_context = (
        Path(args.memory_context_file).read_text() if args.memory_context_file else ""
    )
    print(
        f"plan_context: chars={len(plan_context)} sha256={_sha256_text(plan_context)} "
        f"minimal_chars={len(minimal)} memory_chars={len(memory_context)}"
    )

    summary_model = args.summary_model
    if summary_model is None:
        try:
            from hermes_cli.config import load_config_readonly

            cfg = load_config_readonly() or {}
            summary_model = ((cfg.get("auxiliary") or {}).get("compression") or {}).get(
                "model", ""
            ) or ""
        except (ImportError, KeyError, AttributeError, OSError):
            summary_model = ""
        print(f"summary_model (config auxiliary.compression.model): {summary_model!r}")

    # ---- arms
    sources = [raw_text, plan_context, minimal, memory_context]
    arm_outputs = {}
    if "i" in arms_selected:
        arm_outputs["i"] = run_arm(
            "i_fallback_phase1", args, messages, plan_context, minimal, memory_context,
            use_phase1=True,
        )
    if "ii" in arms_selected:
        arm_outputs["ii"] = run_arm(
            "ii_fallback_trail", args, messages, plan_context, minimal, memory_context,
        )
    if "iii" in arms_selected and summary_model:
        arm_outputs["iii"] = run_arm(
            "iii_llm", args, messages, plan_context, minimal, memory_context,
            use_llm=True, summary_model=summary_model,
        )

    # ---- gate scoring
    report_arms = {}
    for key, output in arm_outputs.items():
        emitted = output["emitted_text"]
        block = output["block_text"] or emitted
        metrics = {
            "a_mid_sentence": mid_sentence_start_rate(emitted, sources),
            "b_entity_recall": entity_recall(raw_text, emitted),
            "c_duplication": duplication_ratio(emitted),
        }
        block_metrics = {
            "a_mid_sentence_block": mid_sentence_start_rate(block, sources),
            "c_duplication_block": duplication_ratio(block),
        }
        plan_report = plan_head_report(emitted, plan_context) if plan_context else None
        report_arms[key] = {
            **output["result"],
            "metrics": metrics,
            "block_metrics": block_metrics,
            "plan_head_report": plan_report,
        }
        print(
            f"\n== arm {key} ({output['result']['arm']}) ==\n"
            f"  rows_out={output['result']['window_rows_out']} "
            f"emitted_chars={output['result']['emitted_chars']} "
            f"payload_block_chars={output['result']['payload_block_chars']}\n"
            f"  (a) mid-sentence rate={metrics['a_mid_sentence']['rate']:.4f} "
            f"violations={len(metrics['a_mid_sentence']['violations'])} "
            f"kept_lines={metrics['a_mid_sentence']['kept_lines']} "
            f"orthographic_starts={metrics['a_mid_sentence']['orthographic_starts']} "
            f"marked_continuations={metrics['a_mid_sentence']['marked_continuations']}\n"
            f"  (b) entity recall={metrics['b_entity_recall']['recall']:.4f} "
            f"({metrics['b_entity_recall']['found_entities']}/"
            f"{metrics['b_entity_recall']['window_entities']})\n"
            f"  (c) duplication={metrics['c_duplication']['ratio']:.4f} "
            f"blocks={metrics['c_duplication']['repeated_blocks']} "
            f"dup_lines={metrics['c_duplication']['duplicated_lines']}"
        )
        if plan_report is not None:
            print(
                f"  plan heads: bodies={plan_report['payload_bodies']} "
                f"verbatim={plan_report['carried_verbatim']} "
                f"introduced_head_cuts={len(plan_report['introduced_head_cuts'])} "
                f"tail_cuts={plan_report['introduced_tail_cuts']} "
                f"unmatched={plan_report['unmatched']} "
                f"headless_at_rest={plan_report['source_bodies_headless_at_rest']}"
            )
        for violation in metrics["a_mid_sentence"]["violations"][:8]:
            print(f"    A-VIOLATION: {violation['line']}")

    # ---- verdict
    verdict = {}
    metric_value_key = {
        "a_mid_sentence": "rate",
        "b_entity_recall": "recall",
        "c_duplication": "ratio",
    }

    def _metric(arm: str, name: str) -> float | None:
        entry = report_arms.get(arm)
        return None if entry is None else float(entry["metrics"][name][metric_value_key[name]])

    for name in ("a_mid_sentence", "b_entity_recall", "c_duplication"):
        v1 = _metric("i", name)
        v2 = _metric("ii", name)
        v3 = _metric("iii", name)
        entry: dict = {"arm_i": v1, "arm_ii": v2, "arm_iii": v3}
        if v1 is not None and v2 is not None:
            entry["trail_delta"] = round(v2 - v1, 6)
        if v1 is not None and v3 is not None:
            entry["nemotron_delta"] = round(v3 - v1, 6)
            if v2 is not None:
                entry["trail_moves_toward_nemotron"] = abs(v2 - v3) < abs(v1 - v3)
        verdict[name] = entry

    print("\n== verdict (does the trail class move toward nemotron?) ==")
    for name, entry in verdict.items():
        print(f"  {name}: {json.dumps(entry, sort_keys=True)}")

    # ---- gate
    violations = []
    for key, entry in report_arms.items():
        rate = entry["metrics"]["a_mid_sentence"]["rate"]
        dup = entry["metrics"]["c_duplication"]["ratio"]
        if rate > args.max_mid_sentence_rate + 1e-9:
            violations.append(f"arm {key}: (a) rate {rate:.4f} > {args.max_mid_sentence_rate}")
        if dup > args.max_duplication_ratio + 1e-9:
            violations.append(f"arm {key}: (c) ratio {dup:.4f} > {args.max_duplication_ratio}")
        plan_report = entry.get("plan_head_report")
        if plan_report and plan_report["introduced_head_cuts"]:
            violations.append(
                f"arm {key}: introduced plan head cuts: {plan_report['introduced_head_cuts'][:3]}"
            )
    gate = {
        "passed": not violations,
        "thresholds": {
            "max_mid_sentence_rate": args.max_mid_sentence_rate,
            "max_duplication_ratio": args.max_duplication_ratio,
        },
        "violations": violations,
    }
    print(f"\ngate: {json.dumps(gate, sort_keys=True)}")

    full_report = {
        "provenance": {
            "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "argv0": sys.argv[0],
            "cwd": str(Path.cwd()),
            "git_head": subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True
            ).stdout.strip(),
            "phase1_ref": phase1_info[1] if phase1_info else None,
            "phase1_module_sha256": phase1_info[2] if phase1_info else None,
            "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        "controls": {
            "loader": loader_report,
            "raw_window_chars": len(raw_text),
            "raw_window_sha256": raw_hash,
            "raw_window_reference": {
                "a_rate": raw_reference["a_mid_sentence"]["rate"],
                "a_orthographic_starts": raw_reference["a_mid_sentence"]["orthographic_starts"],
                "c_ratio": raw_reference["c_duplication"]["ratio"],
                "c_repeated_blocks": raw_reference["c_duplication"]["repeated_blocks"],
            },
            "context_length": args.context_length,
            "protect_first_n": args.protect_first_n,
            "protect_last_n": args.protect_last_n,
            "threshold_percent": args.threshold_percent,
            "target_ratio": args.target_ratio,
            "summary_model": summary_model,
            "plan_context_sha256": _sha256_text(plan_context),
            "arms_selected": sorted(arms_selected),
        },
        "arms": report_arms,
        "verdict": verdict,
        "gate": gate,
    }
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(full_report, ensure_ascii=False, indent=1))
        print(f"json_out: {args.json_out}")

    print("\n== summary ==")
    print(f"{'arm':<22} {'(a) mid-sent':>12} {'(b) recall':>10} {'(c) dup':>8}")
    for key in ("i", "ii", "iii"):
        entry = report_arms.get(key)
        if entry is None:
            continue
        m = entry["metrics"]
        print(
            f"{entry['arm']:<22} {m['a_mid_sentence']['rate']:>12.4f} "
            f"{m['b_entity_recall']['recall']:>10.4f} {m['c_duplication']['ratio']:>8.4f}"
        )
    if args.gate and violations:
        print("HARNESS GATE FAILED")
        return 1
    print("HARNESS OK")
    return 0


PHASE1_MODULE: Any = None

if __name__ == "__main__":
    sys.exit(main())
