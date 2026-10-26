"""Flat-file plan spec: import / export (t_d3583771).

The plan should BE the spec. ``plan import`` creates a Plan from a flat
markdown file (``# Title``, global description, ``## step title`` +
expansion), exactly as ``new`` + assign — the expansion is immutable ground
truth inside the plan, so the worker never re-reads an external spec file.
``plan export`` writes the plan back to markdown with every step's completion
summary and denial message plus the full claim/denial history.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools import plan as plan_module  # noqa: E402
from tools import plan_binding_adapter  # noqa: E402
from tests.tools.test_plan_step_review import (  # noqa: E402
    _Agent,
    _delegated_plan,
    _install,
)

SPEC = """# Parser spec

Ship the v2 parser. All rules in this document are binding for the run.

## Implement tokenizer
The tokenizer splits on ASCII whitespace and keeps quoted spans intact.

## Wire the parser
The parser must accept the glyph array exactly as specified:
["glyph-a", "glyph-b"]. No phantom sections.
"""


def _write_spec(tmp_path, text: str = SPEC) -> str:
    path = tmp_path / "spec.md"
    path.write_text(text, encoding="utf-8")
    return str(path)


def _approve(monkeypatch):
    monkeypatch.setattr(
        plan_module, "clarify_tool", lambda *_a, **_k: '{"user_response":"Approve"}'
    )


# --------------------------------------------------------------- parse format


def test_parse_plan_spec_reads_title_global_and_sections():
    title, goal, titles, expansions = plan_binding_adapter.parse_plan_spec(SPEC)
    assert title == "Parser spec"
    assert goal.startswith("Ship the v2 parser.")
    assert titles == ["Implement tokenizer", "Wire the parser"]
    assert expansions[0].startswith("The tokenizer splits")
    assert '["glyph-a", "glyph-b"]' in expansions[1]
    assert "No phantom sections." in expansions[1]


def test_parse_strips_the_export_run_record():
    spec = (
        "# T\n\nglobal\n\n"
        "## S1\n\nbody line\n\n"
        "> **Summary:** claimed work\n"
        "> **Denied:** first defect\n"
        "> **Claims and denials:**\n"
        "> - claimed (2026-10-23 05:21:07, ornith): claimed work\n"
        "> - denied (2026-10-23 05:24:10, neo): first defect\n"
    )
    title, goal, titles, expansions = plan_binding_adapter.parse_plan_spec(spec)
    assert title == "T"
    assert goal == "global"
    assert titles == ["S1"]
    assert expansions == ["body line"], "run record must not leak into the spec"


def test_parse_keeps_authored_trailing_blockquotes():
    spec = "# T\n\nglobal\n\n## S1\n\nbody\n\n> an authored quote\n"
    _title, _goal, _titles, expansions = plan_binding_adapter.parse_plan_spec(spec)
    assert expansions == ["body\n\n> an authored quote"]


def test_parse_keeps_h1_lines_inside_step_bodies():
    """A ``#``-headed line in a step body is spec content — never a silent
    stop (the run-7 failure class this feature exists to remove)."""
    spec = "# T\n\nglobal\n\n## S1\n\nbody\n\n# Appendix notes\n\n## S2\n\nsecond\n"
    title, goal, titles, expansions = plan_binding_adapter.parse_plan_spec(spec)
    assert title == "T"
    assert goal == "global"
    assert titles == ["S1", "S2"]
    assert expansions == ["body\n\n# Appendix notes", "second"]


# ----------------------------------------------------------------- plan import


def test_import_creates_the_plan_with_expansions(monkeypatch, tmp_path):
    conn, _sent = _install(monkeypatch)
    _approve(monkeypatch)
    creator = _Agent("neo", "20261023_010101_abcdef")
    result = plan_module.plan(creator, "import", file=_write_spec(tmp_path))
    assert result.startswith("TASK APPROVED")
    row = conn.execute("SELECT * FROM tasks").fetchone()
    assert row["title"] == "Parser spec"
    assert row["task_goal"].startswith("Ship the v2 parser.")
    assert json.loads(row["task_steps"]) == ["Implement tokenizer", "Wire the parser"]
    expansions = json.loads(row["task_step_expansions"])
    assert expansions[0].startswith("The tokenizer splits")
    assert "No phantom sections." in expansions[1]
    assert row["status"] == "manual", "import behaves exactly like `new`"

    # remind renders the current step's full expansion (the plan IS the spec)
    remind = plan_module.plan(creator, "remind")
    assert "Active Step 1 of 2: Implement tokenizer" in remind
    assert "Spec:" in remind
    assert "The tokenizer splits on ASCII whitespace" in remind


def test_import_with_assignee_delegates_to_the_worker(monkeypatch, tmp_path):
    conn, sent = _install(monkeypatch)
    _approve(monkeypatch)
    creator = _Agent("neo", "20261023_020202_abcdef")
    worker = _Agent("ornith", "20261023_020203_abcdef")
    result = plan_module.plan(
        creator, "import", file=_write_spec(tmp_path), assignee="ornith"
    )
    assert "ornith has been notified" in result
    row = conn.execute("SELECT * FROM tasks").fetchone()
    assert row["assignee"] == "ornith"
    assert row["status"] == "blocked"
    assert sent, "the worker must be woken to claim the plan"

    shown = plan_module.plan(worker, "continue", task_id=row["id"])
    assert "Spec:" in shown
    assert "The tokenizer splits on ASCII whitespace" in shown


def test_import_refuses_oversized_step_title(monkeypatch, tmp_path):
    conn, _sent = _install(monkeypatch)
    creator = _Agent("neo", "20261023_030303_abcdef")
    spec = "# T\n\nglobal\n\n## " + "t" * 200 + "\n\nbody\n"
    result = plan_module.plan(creator, "import", file=_write_spec(tmp_path, spec))
    assert result.startswith("ERROR:")
    assert "128" in result
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_import_refuses_oversized_expansion(monkeypatch, tmp_path):
    conn, _sent = _install(monkeypatch)
    creator = _Agent("neo", "20261023_040404_abcdef")
    spec = "# T\n\nglobal\n\n## S1\n\n" + "x" * 2100 + "\n"
    result = plan_module.plan(creator, "import", file=_write_spec(tmp_path, spec))
    assert result.startswith("ERROR:")
    assert "step 1 expansion" in result
    assert "2048" in result
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_import_refuses_oversized_global_description(monkeypatch, tmp_path):
    conn, _sent = _install(monkeypatch)
    creator = _Agent("neo", "20261023_050505_abcdef")
    spec = "# T\n\n" + "g" * 2100 + "\n\n## S1\n\nbody\n"
    result = plan_module.plan(creator, "import", file=_write_spec(tmp_path, spec))
    assert result.startswith("ERROR:")
    assert "2048" in result
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_import_refuses_an_empty_step_title(monkeypatch, tmp_path):
    conn, _sent = _install(monkeypatch)
    creator = _Agent("neo", "20261023_090909_abcdef")
    spec = "# T\n\nglobal\n\n## \n\nbody\n"
    result = plan_module.plan(creator, "import", file=_write_spec(tmp_path, spec))
    assert result.startswith("ERROR:")
    assert "empty" in result
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_import_requires_a_file_and_export_requires_a_task_id(monkeypatch):
    conn, _sent = _install(monkeypatch)
    creator = _Agent("neo", "20261023_060606_abcdef")
    assert "requires file" in plan_module.plan(creator, "import")
    assert "requires task_id" in plan_module.plan(creator, "export")


# ----------------------------------------------------------------- plan export


def test_export_includes_summaries_denials_and_full_history(monkeypatch, tmp_path):
    conn, _sent, worker, dispatcher, task_id = _delegated_plan(monkeypatch)
    plan_module.plan(worker, "advance", summary="claimed one", step=1)
    plan_module.plan(
        dispatcher, "review", task_id=task_id, decision="denied",
        reason="first defect: missing import",
    )
    plan_module.plan(worker, "advance", summary="claimed again", step=1)
    plan_module.plan(dispatcher, "review", task_id=task_id, decision="approved")

    out = tmp_path / "plan.md"
    result = plan_module.plan(
        dispatcher, "export", task_id=task_id, file=str(out)
    )
    assert result == f"Plan {task_id} exported to {out}"
    text = out.read_text(encoding="utf-8")

    assert text.startswith("# Review gate")
    assert "Ship verified work" in text
    assert "## inspect" in text
    assert "## implement" in text
    # every step's completion summary and denial message
    assert "> **Summary:** claimed again" in text
    # the full claim/denial history — including the rejection that the
    # approval later cleared from the plan block
    assert "> **Claims and denials:**" in text
    assert "- claimed (" in text
    assert "- denied (" in text
    assert "first defect: missing import" in text
    assert "- approved (" in text


def test_export_without_file_returns_the_markdown(monkeypatch):
    _conn, _sent, worker, dispatcher, task_id = _delegated_plan(monkeypatch)
    text = plan_module.plan(dispatcher, "export", task_id=task_id)
    assert text.startswith("# Review gate")
    assert "## inspect" in text


def test_export_round_trips_through_import(monkeypatch, tmp_path):
    conn, _sent = _install(monkeypatch)
    _approve(monkeypatch)
    creator = _Agent("neo", "20261023_070707_abcdef")
    plan_module.plan(creator, "import", file=_write_spec(tmp_path))
    row = conn.execute("SELECT * FROM tasks").fetchone()
    text = plan_module.plan(creator, "export", task_id=row["id"])
    title, goal, titles, expansions = plan_binding_adapter.parse_plan_spec(text)
    assert title == "Parser spec"
    assert goal == row["task_goal"]
    assert titles == json.loads(row["task_steps"])
    assert expansions == json.loads(row["task_step_expansions"])


def test_export_unknown_task_is_an_error(monkeypatch):
    _conn, _sent = _install(monkeypatch)
    creator = _Agent("neo", "20261023_080808_abcdef")
    result = plan_module.plan(creator, "export", task_id="t_missing1")
    assert result.startswith("ERROR:")


# ---------------------------------------------- approval prompt + delivery


def _big_spec(n: int = 10, pad: int = 800) -> str:
    """n steps whose expansions blow past Telegram's 4,096-char cap."""
    parts = ["# Pente tell transport", "", "Ship the run. Every rule binds.", ""]
    for i in range(1, n + 1):
        parts.append(f"## Step {i} title")
        parts.append(f"EXPANSION-MARKER-{i} " + ("spec-ground-truth " * (pad // 18)))
        parts.append("")
    return "\n".join(parts)


def test_approval_prompt_is_compact_and_body_keeps_expansions(monkeypatch, tmp_path):
    """t_bbe67f44: the approval prompt embedded the FULL plan body (6,292
    chars > Telegram's 4,096 cap), the send failed, and the gate archived
    the plan. Evan 2026-10-24: "It should only send the small steps, not
    the expansions." Prompt = title + goal + step titles; tasks.body keeps
    the expansions verbatim."""
    conn, _sent = _install(monkeypatch)
    captured = {}

    def _capture(question, **kw):
        captured["question"] = question
        captured["choices"] = kw.get("choices")
        return '{"user_response":"Approve"}'

    monkeypatch.setattr(plan_module, "clarify_tool", _capture)
    creator = _Agent("neo", "20261024_010101_abcdef")
    result = plan_module.plan(creator, "import", file=_write_spec(tmp_path, _big_spec()))
    assert result.startswith("TASK APPROVED"), result

    question = captured["question"]
    # (a) the delivered prompt fits the platform cap with room to spare
    assert len(question) < 4096, f"prompt is {len(question)} chars"
    # the buttons ride the same prompt (Approve / Deny / Yolo)
    assert captured["choices"] == ["Approve", "Deny", "Yolo"]
    # title + numbered step TITLES are present
    assert "## Plan: Pente tell transport" in question
    for i in range(1, 11):
        assert f"{i}. Step {i} title" in question
    # expansions are excluded from the prompt…
    assert "EXPANSION-MARKER-1" not in question
    # (c) …but untouched in the stored body, and export still returns them
    row = conn.execute("SELECT * FROM tasks").fetchone()
    assert "EXPANSION-MARKER-1" in row["body"]
    text = plan_module.plan(creator, "export", task_id=row["id"])
    assert "EXPANSION-MARKER-1" in text


def test_undeliverable_prompt_is_never_a_denial(monkeypatch, tmp_path):
    """A transport failure means NO human was asked (Evan 2026-10-24):
    "never archive it and never report denied". The plan stays blocked for
    approval and the caller is told it was a delivery failure."""
    conn, _sent = _install(monkeypatch)
    from tools.clarify_gateway import CLARIFY_UNDELIVERABLE_MARKER

    monkeypatch.setattr(
        plan_module,
        "clarify_tool",
        lambda *_a, **_k: json.dumps({"user_response": CLARIFY_UNDELIVERABLE_MARKER}),
    )
    creator = _Agent("neo", "20261024_020202_abcdef")
    result = plan_module.plan(
        creator, "import", file=_write_spec(tmp_path), assignee="ornith"
    )
    assert "could not be delivered" in result, result
    assert "Plan denied" not in result
    row = conn.execute("SELECT * FROM tasks").fetchone()
    assert row["status"] == "blocked", "transport failure must not archive the plan"
    assert row["block_kind"] == "approval", "the plan must stay awaiting approval"


def test_no_approval_prompt_embeds_a_full_body():
    """Every approval prompt goes through render_approval_prompt — the
    hand-maintained-middles class (decision/step, parent_task_id, `file`):
    one shared renderer, no copy-pasted body embeddings left to drift."""
    root = Path(__file__).resolve().parents[2]
    for rel in ("tools/plan_binding_adapter.py", "tools/plan.py"):
        src = (root / rel).read_text(encoding="utf-8")
        assert 'f"Approve plan {task_id}?\\n\\n{' not in src, rel
