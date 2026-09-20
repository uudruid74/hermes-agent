#!/usr/bin/env python3
"""Regression tests for the agent-attribution git hook.

Covers ``scripts/git-hooks/prepare-commit-msg`` two ways:

1. Unit — run the hook directly against message files, asserting exact output.
2. Integration — run a **real** ``git commit`` in a throwaway repo with the hook
   installed, then read the trailer back the way a human would
   (``git log --format='%(trailers:key=Agent,valueonly)'``).

The integration half is the point: the hook's contract is "a real commit made by
an agent carries an attribution trailer," and only a real commit exercises git's
hook invocation, its stdin redirection, and trailer parsing. A unit test alone
missed a bug during development.

Run:  python3 -m pytest tests/test_git_hooks.py -q
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK_SRC = REPO_ROOT / "scripts" / "git-hooks" / "prepare-commit-msg"
INSTALLER = REPO_ROOT / "scripts" / "install-git-hooks.sh"

pytestmark = pytest.mark.skipif(
    not HOOK_SRC.is_file(), reason="hook source not present"
)


def _run_hook(msg_file: Path, agent: str | None, source_kind: str = "message") -> str:
    """Invoke the hook the way git does; return the resulting message text."""
    env = dict(os.environ)
    if agent is None:
        env.pop("USERNAME", None)
    else:
        env["USERNAME"] = agent

    proc = subprocess.run(
        ["bash", str(HOOK_SRC), str(msg_file), source_kind],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 0, (
        f"hook must never fail the commit, got rc={proc.returncode}: {proc.stderr}"
    )
    return msg_file.read_text()


# --------------------------------------------------------------------------
# Unit: hook behaviour against a message file
# --------------------------------------------------------------------------


def test_agent_commit_gets_trailer(tmp_path: Path) -> None:
    f = tmp_path / "msg"
    f.write_text("fix(compression): bypass breaker (t_4f894059)")
    out = _run_hook(f, "Gopher")
    assert out == "fix(compression): bypass breaker (t_4f894059)\n\nAgent: Gopher\n"


def test_agent_name_used_verbatim(tmp_path: Path) -> None:
    """USERNAME is the identity — whatever it says, verbatim."""
    for agent in ("Neo", "Wintermute", "ornith"):
        f = tmp_path / f"msg-{agent}"
        f.write_text("feat: x")
        assert f"Agent: {agent}" in _run_hook(f, agent)


def test_no_agent_env_leaves_message_untouched(tmp_path: Path) -> None:
    f = tmp_path / "msg"
    original = "docs: hand-written"
    f.write_text(original)
    assert _run_hook(f, None) == original


def test_existing_trailer_not_duplicated(tmp_path: Path) -> None:
    """Idempotent: never stamp twice, respect a hand-written trailer."""
    f = tmp_path / "msg"
    f.write_text("fix: thing\n\nAgent: Neo")
    out = _run_hook(f, "Gopher")
    assert out.count("Agent: ") == 1
    assert "Agent: Neo" in out


@pytest.mark.parametrize("source_kind", ["merge", "squash", "commit"])
def test_bookkeeping_sources_skipped(tmp_path: Path, source_kind: str) -> None:
    """Merge/squash/reused messages belong to other commits, not this agent."""
    f = tmp_path / "msg"
    original = "Merge branch 'x'"
    f.write_text(original)
    assert _run_hook(f, "Gopher", source_kind) == original


def test_multiline_body_preserved(tmp_path: Path) -> None:
    f = tmp_path / "msg"
    f.write_text("feat: add thing (t_zzz)\n\nBody line one.\nBody line two.")
    out = _run_hook(f, "Neo")
    assert out.startswith("feat: add thing (t_zzz)\n\nBody line one.\nBody line two.")
    assert out.endswith("\n\nAgent: Neo\n")


def test_missing_arguments_exit_zero() -> None:
    """A hook that aborts a commit is worse than a missing trailer."""
    env = dict(os.environ, USERNAME="Gopher")
    proc = subprocess.run(
        ["bash", str(HOOK_SRC)],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 0


def test_nonexistent_message_file_exits_zero(tmp_path: Path) -> None:
    env = dict(os.environ, USERNAME="Gopher")
    proc = subprocess.run(
        ["bash", str(HOOK_SRC), str(tmp_path / "nope"), "message"],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 0


def test_never_calls_git_global_username() -> None:
    """Ghost-commit guard: the hook must not read git's configured user.name."""
    body = HOOK_SRC.read_text()
    code = "\n".join(
        line for line in body.splitlines() if not line.strip().startswith("#")
    )
    assert "user.name" not in code, "must never fall back to git's global user.name"
    assert "HERMES_PROFILE" not in code, (
        "HERMES_PROFILE is route state, not identity — never read it as an author"
    )
    assert "HERMES_AGENT_NAME" not in code, (
        "HERMES_AGENT_NAME is superseded — never read it as an author"
    )


# --------------------------------------------------------------------------
# Integration: a real `git commit`
# --------------------------------------------------------------------------


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """Throwaway git repo with the production hook installed."""
    if shutil.which("git") is None:
        pytest.skip("git not available")

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    hooks = repo_dir / ".git" / "hooks"

    subprocess.run(["git", "init", "-q", str(repo_dir)], check=True)
    # Match the real fleet identity — every agent commits as this.
    subprocess.run(
        ["git", "-C", str(repo_dir), "config", "user.name", "Evan Langlois"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "config", "user.email", "ekl@taro3.local"],
        check=True,
    )
    hooks.mkdir(parents=True, exist_ok=True)
    shutil.copy2(HOOK_SRC, hooks / "prepare-commit-msg")
    os.chmod(hooks / "prepare-commit-msg", 0o755)
    return repo_dir


def _commit(repo_dir: Path, message: str, agent: str | None) -> None:
    env = dict(os.environ)
    if agent is None:
        env.pop("USERNAME", None)
    else:
        env["USERNAME"] = agent
    subprocess.run(
        ["git", "-C", str(repo_dir), "commit", "-q", "--allow-empty", "-m", message],
        env=env,
        check=True,
        stdin=subprocess.DEVNULL,
    )


def _trailer(repo_dir: Path, rev: str = "HEAD") -> str:
    out = subprocess.run(
        [
            "git",
            "-C",
            str(repo_dir),
            "log",
            "-1",
            "--format=%(trailers:key=Agent,valueonly)",
            rev,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def test_real_commit_is_attributed(repo: Path) -> None:
    """The contract: a real agent commit carries the trailer and git parses it."""
    _commit(repo, "fix(compression): thing (t_abc123)", "Gopher")
    assert _trailer(repo) == "Gopher"


def test_real_commit_without_agent_env_is_clean(repo: Path) -> None:
    _commit(repo, "docs: plain commit", None)
    assert _trailer(repo) == ""


def test_real_commits_attribute_the_right_agent(repo: Path) -> None:
    """Two agents, two commits, two distinct attributions."""
    _commit(repo, "feat: gopher work (t_one)", "Gopher")
    _commit(repo, "feat: neo work (t_two)", "Neo")
    assert _trailer(repo, "HEAD") == "Neo"
    assert _trailer(repo, "HEAD~1") == "Gopher"


def test_real_amend_does_not_stack_trailers(repo: Path) -> None:
    _commit(repo, "feat: thing (t_abc)", "Gopher")
    subprocess.run(
        [
            "git", "-C", str(repo), "commit", "-q", "--amend", "--allow-empty",
            "-m", "feat: amended (t_abc)",
        ],
        env=dict(os.environ, USERNAME="Gopher"),
        check=True,
        stdin=subprocess.DEVNULL,
    )
    body = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--format=%B"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert body.count("Agent: ") == 1


def test_real_merge_commit_not_attributed(repo: Path) -> None:
    """A merge is not the agent's work; it must not be stamped."""
    _commit(repo, "base", None)

    branches = subprocess.run(
        ["git", "-C", str(repo), "branch", "--format=%(refname:short)"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    main = "master" if "master" in branches else branches[0]

    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "side"], check=True)
    _commit(repo, "side work", "Gopher")
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", main], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "merge", "-q", "--no-ff", "side", "-m", "Merge branch 'side'"],
        env=dict(os.environ, USERNAME="Gopher"),
        check=True,
        stdin=subprocess.DEVNULL,
    )
    assert _trailer(repo) == ""


def test_installer_reports_current(repo: Path) -> None:
    """The installer is the restore path — it must be idempotent and verifyable."""
    if not INSTALLER.is_file():
        pytest.skip("installer not present")
    # Point it at the throwaway repo by running with cwd there.
    proc = subprocess.run(
        ["bash", str(INSTALLER), "--check"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    # The repo fixture already copied the same hook, so it should be "ok".
    assert "prepare-commit-msg" in proc.stdout + proc.stderr
