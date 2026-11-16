"""Tests for the multi-board kanban layer (``hermes kanban boards …``).

Covers the post-consolidation boards-table design (one DB, board as a
field, registry in the ``boards`` table):

* Slug validation and normalisation.
* Single consolidated DB path — the ``board=`` argument never selects a
  per-board file (``HERMES_KANBAN_DB`` still pins the file).
* NO ambient "current board": the legacy ``<root>/kanban/current`` file
  and the ``board_state.current_board`` row are inert (ripped out
  2026-11-16). A task's board is its ``tasks.board`` column.
* Board isolation through the ``tasks.board`` column — writes on one
  board don't leak into another.
* ``create_board`` / ``list_boards`` / ``remove_board`` round trip on the
  registry table (legacy board.json is import-only).
* CLI surface: ``hermes kanban boards list/create/rm`` and ``--board``.
* ``_default_spawn`` injects ``HERMES_KANBAN_BOARD`` + the single DB path
  into worker env.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# Ensure the worktree (not the stale global clone) is first on sys.path.
_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with no prior kanban state.

    The autouse hermetic conftest already nukes credentials + TZ; this
    fixture layers a per-test HERMES_HOME plus a path-init cache reset
    so each test sees a truly empty board set.
    """
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
    ):
        monkeypatch.delenv(var, raising=False)
    # Also reset hermes_constants cache so get_default_hermes_root() re-reads.
    try:
        import hermes_constants
        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    # Kanban module-level init cache must not leak between tests.
    kb._INITIALIZED_PATHS.clear()
    return home


# ---------------------------------------------------------------------------
# Slug validation
# ---------------------------------------------------------------------------

class TestSlugValidation:
    @pytest.mark.parametrize("good", [
        "default", "atm10-server", "hermes-agent", "proj_1", "a",
        "very-long-but-still-ok-slug-with-hyphens-and-numbers-1234",
    ])
    def test_accepts_valid(self, good):
        assert kb._normalize_board_slug(good) == good


    def test_empty_returns_none(self):
        assert kb._normalize_board_slug(None) is None
        assert kb._normalize_board_slug("") is None
        assert kb._normalize_board_slug("   ") is None


# ---------------------------------------------------------------------------
# Path resolution — single consolidated DB, board never selects a file
# ---------------------------------------------------------------------------

class TestPathResolution:
    def test_all_boards_share_one_db(self, fresh_home):
        """Every board resolves to the same consolidated kanban.db."""
        expected = fresh_home / "kanban" / "kanban.db"
        assert kb.kanban_db_path() == expected
        assert kb.kanban_db_path(board="default") == expected
        assert kb.kanban_db_path(board="atm10-server") == expected


    def test_env_var_db_override_still_wins(self, fresh_home, tmp_path, monkeypatch):
        """``HERMES_KANBAN_DB`` pins the file regardless of board= arg."""
        forced = tmp_path / "custom.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(forced))
        assert kb.kanban_db_path() == forced
        assert kb.kanban_db_path(board="ignored") == forced


# ---------------------------------------------------------------------------
# No ambient "current board" (ripped out 2026-11-16)
# ---------------------------------------------------------------------------

class TestNoAmbientCurrentBoard:
    def test_legacy_current_file_is_inert(self, fresh_home):
        """A pre-existing <root>/kanban/current file changes NOTHING.

        The wake-routing bug (eddon task → hermes-agent topic) came from
        ambient pointers silently selecting boards. The file must be
        ignored even on a fresh first-connect import pass.
        """
        current = fresh_home / "kanban" / "current"
        current.parent.mkdir(parents=True, exist_ok=True)
        kb.create_board("some-other-board")
        current.write_text("some-other-board\n", encoding="utf-8")
        # Force a fresh init pass so any import path would see the file.
        kb._INITIALIZED_PATHS.clear()
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="implicit", assignee="x")
            row = conn.execute(
                "SELECT board FROM tasks WHERE id = ?", (tid,)
            ).fetchone()
        assert row["board"] == "default"

    def test_board_state_current_board_row_is_inert(self, fresh_home):
        """A stale board_state.current_board row changes NOTHING."""
        kb.create_board("ghost-curr")
        with kb.connect() as conn:
            conn.execute(
                "INSERT INTO board_state (key, value) "
                "VALUES ('current_board', 'ghost-curr')"
            )
            tid = kb.create_task(conn, title="implicit", assignee="x")
            row = conn.execute(
                "SELECT board FROM tasks WHERE id = ?", (tid,)
            ).fetchone()
        assert row["board"] == "default"

    def test_workspace_paths_anchor_on_task_board(self, fresh_home):
        """Scratch workspaces anchor on the task's board, never ambient."""
        kb.create_board("board-a")
        with kb.connect() as conn:
            tid = kb.create_task(
                conn, title="on-a", assignee="x", board="board-a"
            )
            task = kb.get_task(conn, tid)
        assert task is not None
        # board=None on purpose: the task's own column must anchor it.
        ws = kb.resolve_workspace(task, board=None)
        assert ws == (
            fresh_home / "kanban" / "boards" / "board-a" / "workspaces" / tid
        )

    def test_legacy_board_json_imported(self, fresh_home):
        """Pre-existing boards/<slug>/board.json feeds the boards table."""
        bdir = fresh_home / "kanban" / "boards" / "legacy-meta"
        bdir.mkdir(parents=True)
        (bdir / "board.json").write_text(
            json.dumps({"name": "Legacy Meta", "color": "#ff0000"}),
            encoding="utf-8",
        )
        conn = kb.connect()
        row = conn.execute(
            "SELECT name, color FROM boards WHERE slug = 'legacy-meta'"
        ).fetchone()
        assert row is not None
        assert row["name"] == "Legacy Meta"
        assert row["color"] == "#ff0000"


# ---------------------------------------------------------------------------
# Board CRUD on the registry table
# ---------------------------------------------------------------------------

class TestBoardCRUD:
    def test_create_list_roundtrip(self, fresh_home):
        meta = kb.create_board("roundtrip", name="Round Trip", color="#00ff00")
        assert meta["slug"] == "roundtrip"
        slugs = [b["slug"] for b in kb.list_boards()]
        assert "default" in slugs
        assert "roundtrip" in slugs
        assert kb.board_exists("roundtrip")


    def test_remove_archives_row_not_files(self, fresh_home):
        kb.create_board("recycle")
        kb.remove_board("recycle", archive=True)
        # Archived boards keep their row, flagged.
        assert kb.board_exists("recycle")
        conn = kb.connect()
        row = conn.execute(
            "SELECT archived FROM boards WHERE slug = 'recycle'"
        ).fetchone()
        assert row is not None and row["archived"] == 1
        # Excluded from the active list, present with include_archived.
        assert "recycle" not in [b["slug"] for b in kb.list_boards(include_archived=False)]
        assert "recycle" in [b["slug"] for b in kb.list_boards(include_archived=True)]


    def test_remove_delete_drops_row(self, fresh_home):
        kb.create_board("gone")
        kb.remove_board("gone", archive=False)
        assert not kb.board_exists("gone")
        slugs = [b["slug"] for b in kb.list_boards()]
        assert "gone" not in slugs


    def test_remove_board_leaves_no_trace(self, fresh_home):
        kb.create_board("active")
        kb.remove_board("active", archive=False)
        assert not kb.board_exists("active")


    def test_default_board_cannot_be_removed(self, fresh_home):
        with pytest.raises(ValueError, match="cannot be removed"):
            kb.remove_board("default")


    def test_rename_updates_metadata(self, fresh_home):
        kb.create_board("slug-immutable")
        kb.write_board_metadata("slug-immutable", name="New Display Name")
        assert kb.read_board_metadata("slug-immutable")["name"] == "New Display Name"
        # Slug must not change.
        assert kb.board_exists("slug-immutable")


# ---------------------------------------------------------------------------
# Board isolation — via the tasks.board column, one shared DB
# ---------------------------------------------------------------------------

class TestConnectionIsolation:
    def test_tasks_do_not_leak_across_boards(self, fresh_home):
        kb.create_board("alpha")
        kb.create_board("beta")

        with kb.connect() as conn:
            kb.create_task(conn, title="alpha-task-1", assignee="dev", board="alpha")
            kb.create_task(conn, title="alpha-task-2", assignee="dev", board="alpha")
            kb.create_task(conn, title="beta-only", assignee="dev", board="beta")

        with kb.connect() as conn:
            a = kb.list_tasks(conn, board="alpha")
            b = kb.list_tasks(conn, board="beta")
            d = kb.list_tasks(conn, board="default")

        assert {t.title for t in a} == {"alpha-task-1", "alpha-task-2"}
        assert {t.title for t in b} == {"beta-only"}
        assert d == []


    def test_board_column_written_on_create(self, fresh_home):
        """The board= param is persisted, not dropped (the DEFAULT fill bug)."""
        kb.create_board("pinned")
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="on-pinned", assignee="x", board="pinned")
            row = conn.execute(
                "SELECT board FROM tasks WHERE id = ?", (tid,)
            ).fetchone()
        assert row["board"] == "pinned"


    def test_create_without_board_goes_to_default(self, fresh_home):
        """No board param → 'default'. Never an ambient pointer."""
        kb.create_board("curr")
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="implicit", assignee="x")
            row = conn.execute(
                "SELECT board FROM tasks WHERE id = ?", (tid,)
            ).fetchone()
        assert row["board"] == "default"


    def test_env_var_pins_board_on_create(self, fresh_home, monkeypatch):
        kb.create_board("persist")
        kb.create_board("envwin")
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "envwin")
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="via-env", assignee="x")
            row = conn.execute(
                "SELECT board FROM tasks WHERE id = ?", (tid,)
            ).fetchone()
        assert row["board"] == "envwin"


    def test_explicit_board_wins_over_env_var(self, fresh_home, monkeypatch):
        """An explicitly-passed board outranks the env pin."""
        kb.create_board("pinned")
        kb.create_board("envwin")
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "envwin")
        with kb.connect() as conn:
            tid = kb.create_task(
                conn, title="explicit", assignee="x", board="pinned"
            )
            row = conn.execute(
                "SELECT board FROM tasks WHERE id = ?", (tid,)
            ).fetchone()
        assert row["board"] == "pinned"


# ---------------------------------------------------------------------------
# Worker spawn env injection
# ---------------------------------------------------------------------------

class TestWorkerSpawnEnv:
    """Ensure the dispatcher pins ``HERMES_KANBAN_BOARD`` / DB / workspaces on spawn.

    We monkey-patch ``subprocess.Popen`` to capture the child env without
    actually spawning anything.
    """

    def test_default_spawn_sets_env_vars(self, fresh_home, monkeypatch):
        captured = {}

        class FakeProc:
            pid = 12345

        def fake_popen(cmd, *args, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env", {})
            return FakeProc()

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        kb.create_board("spawntest")

        task = kb.Task(
            id="t_abc",
            title="worker test",
            body=None,
            assignee="teknium",
            status="ready",
            priority=0,
            created_by="user",
            created_at=0,
            started_at=None,
            completed_at=None,
            workspace_kind="scratch",
            workspace_path=None,
            claim_lock=None,
            claim_expires=None,
            tenant=None,
        )

        kb._default_spawn(task, str(fresh_home / "ws"), board="spawntest")

        env = captured["env"]
        assert env["HERMES_KANBAN_BOARD"] == "spawntest"
        assert env["HERMES_KANBAN_TASK"] == "t_abc"
        # One consolidated DB for every board.
        expected_db = fresh_home / "kanban" / "kanban.db"
        assert env["HERMES_KANBAN_DB"] == str(expected_db)


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------

def _cli(args: list[str], env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Run ``hermes kanban …`` with PYTHONPATH pinned to the worktree."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_WORKTREE)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban"] + args,
        env=env,
        capture_output=True,
        text=True,
        cwd=str(_WORKTREE),
        timeout=30,
    )


class TestCLI:
    def test_boards_list_default_only(self, tmp_path):
        env = {"HERMES_HOME": str(tmp_path)}
        res = _cli(["boards", "list", "--json"], env_extra=env)
        assert res.returncode == 0, res.stderr
        data = json.loads(res.stdout)
        slugs = [b["slug"] for b in data]
        assert slugs == ["default"]


    def test_boards_switch_removed(self, tmp_path):
        """`boards switch` fails loudly — no ambient current board."""
        env = {"HERMES_HOME": str(tmp_path)}
        res = _cli(["boards", "switch", "default"], env_extra=env)
        assert res.returncode == 2
        assert "--board" in res.stderr


    def test_per_board_task_isolation_via_cli(self, tmp_path):
        env = {"HERMES_HOME": str(tmp_path)}
        assert _cli(["boards", "create", "projA"], env_extra=env).returncode == 0
        assert _cli(["boards", "create", "projB"], env_extra=env).returncode == 0

        # Create one task on each via --board.
        r = _cli(["--board", "projA", "create", "Task A", "--assignee", "dev"], env_extra=env)
        assert r.returncode == 0, r.stderr
        r = _cli(["--board", "projB", "create", "Task B", "--assignee", "dev"], env_extra=env)
        assert r.returncode == 0, r.stderr

        # list on each board only shows its own.
        listA = _cli(["--board", "projA", "list", "--json"], env_extra=env)
        listB = _cli(["--board", "projB", "list", "--json"], env_extra=env)
        listD = _cli(["list", "--json"], env_extra=env)

        titlesA = [t["title"] for t in json.loads(listA.stdout)]
        titlesB = [t["title"] for t in json.loads(listB.stdout)]
        titlesD = [t["title"] for t in json.loads(listD.stdout)]

        assert titlesA == ["Task A"]
        assert titlesB == ["Task B"]
        assert titlesD == []


    def test_boards_list_counts_are_scoped_by_slug(self, fresh_home):
        env = {"HERMES_HOME": str(fresh_home)}
        for slug in ("alpha", "beta"):
            result = _cli(["boards", "create", slug], env_extra=env)
            assert result.returncode == 0, result.stderr

        with kb.connect() as conn:
            alpha_todo = kb.create_task(
                conn, title="Alpha todo", assignee="dev"
            )
            alpha_blocked = kb.create_task(
                conn, title="Alpha blocked", assignee="dev"
            )
            beta_blocked = kb.create_task(
                conn, title="Beta blocked", assignee="dev"
            )
            conn.execute(
                "UPDATE tasks SET board='alpha', status='todo' WHERE id=?",
                (alpha_todo,),
            )
            conn.execute(
                "UPDATE tasks SET board='alpha', status='blocked' WHERE id=?",
                (alpha_blocked,),
            )
            conn.execute(
                "UPDATE tasks SET board='beta', status='blocked' WHERE id=?",
                (beta_blocked,),
            )

        result = _cli(["boards", "list", "--json"], env_extra=env)
        assert result.returncode == 0, result.stderr
        counts = {board["slug"]: board["counts"] for board in json.loads(result.stdout)}

        assert counts["alpha"] == {"blocked": 1, "todo": 1}
        assert counts["beta"] == {"blocked": 1}