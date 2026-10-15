"""Phase 3 harness: fidelity gate semantics + 3-arm replay isolation (t_c9cb4bec).

The gate must catch the WRITER: source-relative head-cut detection (bug 1's
"Summary: ork in this rerun" shape), entity recall vs the raw window, and
repeated-block duplication. The harness tests pin two structural facts the
3-arm verdict rests on: (1) the loader honors the extractor contract
(tool_calls JSON-decoded, tool_call_id carried), and (2) arm i's Phase-1
module has no trail machinery while arm ii's HEAD module does — the
i-vs-ii isolation is exactly the unit-class change.
"""

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from tools.compression_ab_harness import (
    load_phase1_module,
    load_window_from_db,
    window_text,
)
from tools.compression_fidelity_gate import (
    duplication_ratio,
    entity_recall,
    mid_sentence_start_rate,
    plan_head_report,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

FULL_SUMMARY = (
    "Step 2 done: new_game + set_board in game_logic.py, per §Basic Operation. "
    "Verified by reading final file state. Tables present from prior work in "
    "this rerun; I verified their exact state rather than rewriting them). "
    "No scratch DB left at repo root. Uncommitted."
)
CUT_SUMMARY = "ork in this rerun; I verified their exact state rather than rewriting them). No scratch DB left at repo root. Uncommitted."


# ---------------------------------------------------------------- metric (a)

class TestMidSentenceStartRate:
    def test_head_cut_body_is_a_violation(self):
        plan = f" Task: t_x\n Summary: {FULL_SUMMARY}"
        payload = f"[CONTEXT WINDOW COMPRESSED]\n\n## Active Plan\n Summary: {CUT_SUMMARY}"
        result = mid_sentence_start_rate(payload, [plan])
        assert result["rate"] > 0
        assert result["violations"]

    def test_verbatim_carry_is_clean(self):
        plan = f" Summary: {FULL_SUMMARY}"
        payload = f"## Active Plan\n Summary: {FULL_SUMMARY}"
        result = mid_sentence_start_rate(payload, [plan])
        assert result["rate"] == 0.0
        assert result["violations"] == []

    def test_sentence_aligned_extract_is_clean(self):
        source = "First sentence ends here. Second sentence is complete."
        payload = "Second sentence is complete."
        result = mid_sentence_start_rate(payload, [source])
        assert result["rate"] == 0.0

    def test_uppercase_head_cut_still_caught(self):
        # "B with both tables" — cut mid-word but starts uppercase; the
        # source-relative detector must not rely on orthography. Mirrors the
        # real bug-1 row: "...DB with both tables present" tail-sliced to
        # "B with both tables present".
        source = " Summary: Tables present. Verified DB with both tables confirmed."
        payload = " Summary: B with both tables confirmed."
        result = mid_sentence_start_rate(payload, [source])
        assert result["rate"] > 0

    def test_marked_continuation_not_counted_as_kept_line(self):
        source = "a" * 300
        payload = f"…{source[150:]}"
        result = mid_sentence_start_rate(payload, [source])
        assert result["marked_continuations"] == 1
        assert result["rate"] == 0.0

    def test_orthographic_starts_reported_but_not_gated(self):
        # Tool-trail digest lines legitimately start lowercase after the
        # bullet — orthography alone must not flag them.
        payload = "- read_file: src/game_logic.py — ok (12:00:01–12:00:02)"
        result = mid_sentence_start_rate(payload, ["unrelated source text"])
        assert result["rate"] == 0.0
        assert result["orthographic_starts"] == 1

    def test_generated_marker_lines_are_not_kept_lines(self):
        payload = "[CONTEXT WINDOW COMPRESSED]\n\n[VERBATIM CONTEXT BELOW]"
        result = mid_sentence_start_rate(payload, [])
        assert result["kept_lines"] == 0


# ---------------------------------------------------------------- metric (b)

class TestEntityRecall:
    def test_recall_counts_missing_artifacts(self):
        window = (
            "Edited src/pente_mcp.py with build_internal_fallback and "
            "captured 1791923559 exit code 256 in leaderboard_query."
        )
        payload = "Edited src/pente_mcp.py and captured 1791923559."
        result = entity_recall(window, payload)
        assert 0.0 < result["recall"] < 1.0
        assert result["window_entities"] > result["found_entities"]
        missing = result["per_family"]
        assert missing["identifiers"]["found"] < missing["identifiers"]["window"]

    def test_full_recall_on_identical_text(self):
        text = "src/foo_bar.py init(DB_PATH) 4096"
        result = entity_recall(text, text)
        assert result["recall"] == 1.0

    def test_families_disjoint(self):
        from tools.compression_fidelity_gate import extract_entities

        entities = extract_entities("src/pente_mcp.py pente_mcp 4096")
        assert not (entities["paths"] & entities["identifiers"])


# ---------------------------------------------------------------- metric (c)

class TestDuplicationRatio:
    def test_repeated_block_detected(self):
        payload = "\n".join(
            ["alpha line one", "beta line two", "alpha line one", "beta line two"]
        )
        result = duplication_ratio(payload)
        assert result["ratio"] == 1.0
        assert result["repeated_blocks"] == 1

    def test_clean_payload_zero(self):
        payload = "\n".join(f"line {i} unique content" for i in range(10))
        result = duplication_ratio(payload)
        assert result["ratio"] == 0.0

    def test_single_line_window(self):
        assert duplication_ratio("one line")["ratio"] == 0.0


# ------------------------------------------------------- plan-head (bug 1)

class TestPlanHeadReport:
    def test_introduced_head_cut_flagged(self):
        plan = f" Summary: {FULL_SUMMARY}"
        payload = f" Summary: {CUT_SUMMARY}"
        report = plan_head_report(payload, plan)
        assert report["introduced_head_cuts"]

    def test_verbatim_carried(self):
        plan = f" Summary: {FULL_SUMMARY}"
        report = plan_head_report(plan, plan)
        assert report["carried_verbatim"] == 1
        assert report["introduced_head_cuts"] == []

    def test_at_rest_headless_source_not_blamed_on_writer(self):
        plan = f" Summary: {CUT_SUMMARY}"
        report = plan_head_report(plan, plan)
        assert report["carried_verbatim"] == 1
        assert report["source_bodies_headless_at_rest"] == 1

    def test_tail_cut_counted_separately(self):
        plan = f" Summary: {FULL_SUMMARY}"
        payload = f" Summary: {FULL_SUMMARY[:40]}"
        report = plan_head_report(payload, plan)
        assert report["introduced_tail_cuts"] == 1
        assert report["introduced_head_cuts"] == []


# --------------------------------------------------------------- the loader

def _make_state_db(path: Path, rows):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE messages ("
        "id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, "
        "tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL, "
        "active INTEGER DEFAULT 1)"
    )
    conn.executemany(
        "INSERT INTO messages (id, session_id, role, content, tool_call_id, "
        "tool_calls, tool_name, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


class TestLoader:
    def test_tool_calls_are_json_decoded_and_ids_carried(self, tmp_path):
        calls = [
            {
                "id": "call_1",
                "call_id": "call_1",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path": "src/a.py"}'},
            }
        ]
        db = tmp_path / "state.db"
        _make_state_db(
            db,
            [
                (1, "s1", "user", "hello", None, None, None, 1.0),
                (2, "s1", "assistant", "", None, json.dumps(calls), None, 2.0),
                (3, "s1", "tool", "file content", "call_1", None, "read_file", 3.0),
                (4, "s1", "session_meta", "meta", None, None, None, 4.0),
            ],
        )
        messages, report = load_window_from_db(str(db), "s1", None, None)
        assert report["rows_read"] == 4
        assert report["session_meta_dropped"] == 1
        assert len(messages) == 3
        tool_call_rows = [m for m in messages if m.get("tool_calls")]
        assert tool_call_rows and isinstance(tool_call_rows[0]["tool_calls"], list)
        tool_rows = [m for m in messages if m["role"] == "tool"]
        assert tool_rows[0]["tool_call_id"] == "call_1"

    def test_undecodable_tool_calls_raise(self, tmp_path):
        db = tmp_path / "state.db"
        _make_state_db(
            db,
            [(1, "s1", "assistant", "", None, "{not json", None, 1.0)],
        )
        with pytest.raises(ValueError, match="tool_calls is not valid JSON"):
            load_window_from_db(str(db), "s1", None, None)

    def test_window_text_round_trip_is_stable(self):
        messages = [
            {"role": "user", "content": "hello", "timestamp": 1.0},
            {"role": "tool", "content": "out", "tool_call_id": "c1", "timestamp": 2.0},
        ]
        assert window_text(messages) == window_text([dict(m) for m in messages])


# ---------------------------------------------------------- isolation facts

class TestArmIsolation:
    def test_phase1_module_has_no_trail_machinery(self):
        module, ref, _ = load_phase1_module(REPO_ROOT, "68d1e5fc56")
        assert ref == "68d1e5fc56"
        assert not hasattr(module, "_tool_trail_lines")
        assert not hasattr(module, "_carry_tool_trail")
        import agent.internal_compression_fallback as head

        assert hasattr(head, "_tool_trail_lines")
        assert hasattr(head, "_carry_tool_trail")

    def test_build_signatures_match_between_arms(self):
        import inspect

        module, _, _ = load_phase1_module(REPO_ROOT, "68d1e5fc56")
        import agent.internal_compression_fallback as head

        assert (
            inspect.signature(module.build_internal_fallback)
            == inspect.signature(head.build_internal_fallback)
        )


# ------------------------------------------------------------- CLI smoke

class TestHarnessSmoke:
    def test_two_arm_replay_runs_and_emits_metrics(self, tmp_path):
        rows = [
            {"role": "system", "content": "sys prompt", "timestamp": 0.0},
            {"role": "user", "content": "head user prose " + "h" * 200, "timestamp": 1.0},
            {"role": "assistant", "content": "head assistant prose " + "h" * 200, "timestamp": 2.0},
            {"role": "user", "content": "middle question 0 " + "m" * 400, "timestamp": 3.0},
            {"role": "assistant", "content": "middle answer 0 " + "m" * 400, "timestamp": 4.0},
            {"role": "user", "content": "middle question 1 " + "m" * 400, "timestamp": 5.0},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "call_id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path": "src/game_logic.py"}',
                        },
                    }
                ],
                "timestamp": 6.0,
            },
            {
                "role": "tool",
                "content": "def init(DB_PATH):\n    ... " + "t" * 400,
                "tool_call_id": "call_1",
                "tool_name": "read_file",
                "timestamp": 7.0,
            },
            {"role": "user", "content": "live question " + "l" * 100, "timestamp": 8.0},
            {"role": "assistant", "content": "live answer " + "l" * 100, "timestamp": 9.0},
        ]
        fixture = tmp_path / "window.json"
        fixture.write_text(json.dumps(rows))
        plan_context = (
            "Task: t_demo\nStatus: running\nGoal: demo\nStep 1/2\n\n"
            " → Step 1: build the thing\n Summary: Step 1 done: built the thing completely.\n"
            "  Step 2: verify the thing\n Summary: Step 2 done: verified the thing completely.\n"
        )
        plan_file = tmp_path / "plan_context.txt"
        plan_file.write_text(plan_context)
        json_out = tmp_path / "report.json"
        import subprocess

        proc = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "tools" / "compression_ab_harness.py"),
                "--fixture", str(fixture),
                "--plan-context-file", str(plan_file),
                "--arms", "i,ii",
                "--context-length", "4000",
                "--protect-first-n", "1",
                "--protect-last-n", "2",
                "--threshold-percent", "0.85",
                "--target-ratio", "0.20",
                "--json-out", str(json_out),
                "--gate",
            ],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert proc.stdout.splitlines()[0].startswith("DRIVER_SHA256 ")
        driver_hash = hashlib.sha256(
            (REPO_ROOT / "tools" / "compression_ab_harness.py").read_bytes()
        ).hexdigest()
        assert driver_hash in proc.stdout.splitlines()[0]
        assert proc.stdout.strip().endswith("HARNESS OK")
        report = json.loads(json_out.read_text())
        assert set(report["arms"]) == {"i", "ii"}
        for arm in report["arms"].values():
            metrics = arm["metrics"]
            assert set(metrics) == {
                "a_mid_sentence",
                "b_entity_recall",
                "c_duplication",
            }
            assert isinstance(metrics["a_mid_sentence"]["violations"], list)
            assert arm["plan_head_report"]["introduced_head_cuts"] == []
        assert report["verdict"]["a_mid_sentence"]["trail_delta"] is not None
