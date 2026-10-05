"""Tests for the multi-board kanban layer (``hermes kanban boards …``).

Covers the post-consolidation boards-table design (one DB, board as a
field, registry in the ``boards`` table):

* Slug validation and normalisation.
* Single consolidated DB path — the ``board=`` argument never selects a
  per-board file (``HERMES_KANBAN_DB`` still pins the file).
* Current-board persistence via the ``board_state`` table and the
  ``HERMES_KANBAN_BOARD`` env var.
* Board isolation through the ``tasks.board`` column — writes on one
  board don't leak into another.
* ``create_board`` / ``list_boards`` / ``remove_board`` round trip on the
  registry table (legacy board.json / current file are import-only).
* CLI surface: ``hermes kanban boards list/create/switch/rm``.
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
# Current-board resolution
# ---------------------------------------------------------------------------

class TestCurrentBoard:
    def test_current_board_persists_in_board_state(self, fresh_home):
        """``set_current_board`` writes the ``board_state`` table, not a file."""
        kb.create_board("my-proj")
        kb.set_current_board("my-proj")
        # No legacy current file involved.
        assert not (fresh_home / "kanban" / "current").exists()
        conn = kb.connect()
        row = conn.execute(
            "SELECT value FROM board_state WHERE key = 'current_board'"
        ).fetchone()
        assert row is not None and row["value"] == "my-proj"
        assert kb.get_current_board() == "my-proj"


    def test_stale_pointer_falls_back_to_default(self, fresh_home):
        """A current_board value naming a removed/unknown slug → default."""
        kb.set_current_board("ghost-board")
        # Simulate the removal path: the row exists but the board doesn't.
        conn = kb.connect()
        conn.execute(
            "UPDATE board_state SET value='ghost-board' WHERE key='current_board'"
        )
        assert not kb.board_exists("ghost-board")
        assert kb.get_current_board() == "default"


    def test_legacy_current_file_imported_once(self, fresh_home):
        """Pre-existing <root>/kanban/current is imported into board_state."""
        current = fresh_home / "kanban" / "current"
        current.parent.mkdir(parents=True, exist_ok=True)
        # The board must exist before the file points at it (board_exists
        # gate on import) and the file must predate the first connect
        # (the import runs once on schema init).
        kb.create_board("imported-board")
        current.write_text("imported-board\n", encoding="utf-8")
        # Force a fresh init pass so the import sees the file, the way a
        # real first-boot-after-upgrade does.
        kb._INITIALIZED_PATHS.clear()
        conn = kb.connect()
        row = conn.execute(
            "SELECT value FROM board_state WHERE key = 'current_board'"
        ).fetchone()
        assert row is not None and row["value"] == "imported-board"


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


    def test_remove_current_board_reverts_to_default(self, fresh_home):
        kb.create_board("active")
        kb.set_current_board("active")
        kb.remove_board("active", archive=False)
        assert kb.get_current_board() == "default"


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


    def test_create_without_board_uses_current(self, fresh_home):
        kb.create_board("curr")
        kb.set_current_board("curr")
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="implicit", assignee="x")
            row = conn.execute(
                "SELECT board FROM tasks WHERE id = ?", (tid,)
            ).fetchone()
        assert row["board"] == "curr"
        with kb.connect() as conn:
            tasks = kb.list_tasks(conn, board="curr")
        assert [t.title for t in tasks] == ["implicit"]


    def test_env_var_overrides_current(self, fresh_home, monkeypatch):
        kb.create_board("persist")
        kb.create_board("envwin")
        kb.set_current_board("persist")
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "envwin")
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="via-env", assignee="x")
            row = conn.execute(
                "SELECT board FROM tasks WHERE id = ?", (tid,)
            ).fetchone()
        assert row["board"] == "envwin"


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
        assert data[0]["is_current"] is True


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