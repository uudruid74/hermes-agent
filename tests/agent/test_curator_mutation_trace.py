"""Curator-mutation trace + guard symmetry (Zephyr 2026-10-20).

Finding 1: the background review fork is persistence-isolated
(``_persist_disabled``) but its tools make DURABLE writes to the parent's
skill tree — structurally invisible in the parent's context. On 2026-10-20
Gopher hallucinated explanations for skill files the curator had changed
("The skill file has state I didn't create"). Shape (a): every mutating call
is extracted from the fork's messages and surfaced to the parent as one
synthetic note row.

Fixture shapes are the REAL call sequences from the three bg-review windows
on gopher session 20261020_001937_450e6ab9 (extracted by Zephyr).
"""

from types import SimpleNamespace

from agent.background_review import (
    extract_curator_mutations,
    surface_curator_mutations,
)


def _call(cid, name, args):
    import json

    return {
        "role": "assistant",
        "tool_calls": [
            {"id": cid, "function": {"name": name, "arguments": json.dumps(args)}}
        ],
    }


def _result(cid, content):
    return {"role": "tool", "tool_call_id": cid, "content": content}


# Window 2 + 3 call sequences (Zephyr's extraction, 2026-10-20).
REAL_WINDOW_MESSAGES = [
    _call("c1", "skill_manage", {
        "action": "write_file", "name": "ragamuffin",
        "file_path": "references/camofox-form-automation.md",
    }),
    _result("c1", '{"success": true, "path": ".../camofox-form-automation.md"}'),
    _call("c2", "skill_manage", {
        "action": "patch", "name": "ragamuffin",
    }),
    _result("c2", '{"success": true, "message": "Patched SKILL.md"}'),
    _call("c3", "skill_manage", {
        "action": "patch", "name": "gopher-journal",
    }),
    _result(
        "c3",
        '"error": "gopher-journal: the current SKILL.md content has not been '
        'loaded in this review turn"',
    ),
    _call("c4", "skill_manage", {
        "action": "write_file", "name": "gopher-journal",
        "file_path": "references/hub-sync-divergence-2026-10-20.md",
    }),
    _result("c4", '{"success": true}'),
    _call("c5", "write_file", {"path": "/tmp/somewhere.md"}),
    _result("c5", '{"success": true}'),
    _call("c6", "patch", {"file_path": "/tmp/other.md"}),
    _result("c6", '{"success": true}'),
    _call("c7", "terminal", {"command": "git show HEAD:x.md > /tmp/x.md"}),
    _result("c7", '{"output": ""}'),
    _call("c8", "skill_view", {"name": "gopher-journal"}),
    _result("c8", "not a skill"),
]


class TestExtractCuratorMutations:
    def test_full_mutating_surface_is_covered(self):
        lines = extract_curator_mutations(REAL_WINDOW_MESSAGES)
        joined = "\n".join(lines)
        # every mutating tool in the real windows is represented
        assert "skill_manage(write_file)" in joined
        assert "skill_manage(patch)" in joined
        assert "write_file" in joined
        assert "patch" in joined
        assert "terminal" in joined
        # the four files the fork ACTUALLY mutated are attributed
        assert "references/camofox-form-automation.md" in joined
        assert "references/hub-sync-divergence-2026-10-20.md" in joined
        # the refused call is marked, not silently dropped
        assert "[ERROR]" in joined
        # read-only tools never appear
        assert "skill_view" not in joined

    def test_terminal_keeps_the_command_line(self):
        lines = extract_curator_mutations(REAL_WINDOW_MESSAGES)
        assert any("git show HEAD:x.md" in line for line in lines)

    def test_empty_history_yields_no_lines(self):
        assert extract_curator_mutations([]) == []
        assert extract_curator_mutations(None) == []


class TestSurfaceToParent:
    def test_one_note_row_reaches_the_parent_context(self):
        parent = SimpleNamespace(_session_messages=[{"role": "user", "content": "hi"}])
        lines = extract_curator_mutations(REAL_WINDOW_MESSAGES)
        surface_curator_mutations(parent, lines)
        rows = parent._session_messages
        assert len(rows) == 2
        note = rows[-1]
        assert note["role"] == "user"
        assert note["content"].startswith("[BACKGROUND CURATOR")
        assert "NOT a task request" in note["content"]
        assert "camofox-form-automation.md" in note["content"]

    def test_no_mutations_no_row(self):
        parent = SimpleNamespace(_session_messages=[])
        surface_curator_mutations(parent, [])
        assert parent._session_messages == []

    def test_missing_message_list_is_harmless(self):
        surface_curator_mutations(SimpleNamespace(), ["x"])  # must not raise


class TestWriteFileGuardSymmetry:
    def test_new_reference_file_requires_skill_md_anchor(self, tmp_path, monkeypatch):
        """The observed asymmetry: a NEW reference file wrote while the
        paired SKILL.md patch was refused — reference docs for a change the
        SKILL.md never took. Now the anchor read is required."""
        import tools.skill_manager_tool as smt

        skill_dir = tmp_path / "gopher-journal"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# gopher-journal\n")

        monkeypatch.setattr(
            smt, "_find_skill", lambda name: {"path": skill_dir, "name": name}
        )
        monkeypatch.setattr(smt, "_org_mirror_write_guard", lambda *a, **k: None)
        # ownership passes (it is not under test here)
        monkeypatch.setattr(smt, "_background_review_write_guard", lambda *a, **k: None)
        import tools.skill_provenance as sp

        monkeypatch.setattr(sp, "is_background_review", lambda: True)
        smt._reset_background_review_read_marks()

        result = smt._write_file(
            "gopher-journal",
            "references/cron-cadence-history-2026-10-20.md",
            "history",
        )
        assert result["success"] is False
        assert "SKILL.md anchor" in result["error"]
        # and nothing was written
        assert not (skill_dir / "references").exists()