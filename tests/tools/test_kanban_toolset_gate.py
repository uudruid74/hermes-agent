"""Kanban toolset gating for everyone incl. ``-t``-woken gateway workers (t_e518054c).

Evan's ruling #2: "Enable the kanban toolset for EVERYONE."

Before this change the toolset gate keyed on ``HERMES_KANBAN_TASK`` (CLI
spawn path) or a profile toolset flag — so a ``-t``-woken gateway session
(neither env nor flag) neither saw nor could call ``kanban_show`` /
``kanban_complete`` / ``kanban_heartbeat``.

Gate semantics after the change:
- lifecycle tools (``_check_kanban_mode``): available to every run except
  delegate_task children (that carve-out is a separate landed safety rule);
- board-routing tools (``_check_kanban_orchestrator_mode`` → kanban_list /
  kanban_unblock): hidden from task workers — identity via env (CLI spawn)
  OR ``sessions.task_id`` binding (``-t`` wake), matching the ownership
  gate from t_20c014fc (``_worker_task_scope``);
- the schema layer (model_tools) appends the kanban toolset for any worker
  identity so restricted profile toolsets cannot strip the surface, but a
  non-worker restricted toolset (e.g. webhook-safe) stays restricted.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clean_gate(monkeypatch):
    """Deterministic gating inputs: no env, no binding, no profile toolset."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_CLAIM_LOCK", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr("tools.kanban_tools._profile_has_kanban_toolset", lambda: False)
    monkeypatch.setattr("tools.kanban_tools._session_bound_task_id", lambda: None)
    monkeypatch.setattr("tools.kanban_tools._is_delegated_child_context", lambda: False)
    # check_fn results are TTL-cached in the registry — clear so each test
    # sees its own gating inputs.
    from tools import registry as tr

    tr._check_fn_cache.clear()
    tr._check_fn_last_good.clear()
    yield
    tr._check_fn_cache.clear()
    tr._check_fn_last_good.clear()


def _tool_names(enabled):
    import tools.kanban_tools  # noqa: F401  — registers the kanban entries
    from model_tools import _compute_tool_definitions

    defs = _compute_tool_definitions(enabled, quiet_mode=True)
    return {t["function"]["name"] for t in defs}


# ---------------------------------------------------------------------------
# Gate layer — tools/kanban_tools.py check_fns
# ---------------------------------------------------------------------------

def test_lifecycle_gate_open_without_env_or_profile():
    """Ruling #2: lifecycle tools need no env flag and no profile opt-in."""
    from tools import kanban_tools as kt

    assert kt._check_kanban_mode() is True


def test_board_routing_open_for_unbound_session():
    """Unbound sessions get the full board-routing surface too (everyone)."""
    from tools import kanban_tools as kt

    assert kt._check_kanban_orchestrator_mode() is True


def test_board_routing_hidden_for_woken_worker_with_profile_toolset(monkeypatch):
    """A ``-t``-woken worker is a worker even when its profile opts into the
    toolset: board-routing tools stay orchestrator-only (split preserved)."""
    monkeypatch.setattr("tools.kanban_tools._session_bound_task_id", lambda: "t_wake")
    monkeypatch.setattr("tools.kanban_tools._profile_has_kanban_toolset", lambda: True)
    from tools import kanban_tools as kt

    assert kt._check_kanban_orchestrator_mode() is False


def test_board_routing_hidden_for_env_worker(monkeypatch):
    """CLI-spawned workers keep the existing worker split."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_env")
    from tools import kanban_tools as kt

    assert kt._check_kanban_orchestrator_mode() is False


def test_delegate_children_still_excluded(monkeypatch):
    """The delegate_task child carve-out (landed safety rule) survives."""
    monkeypatch.setattr("tools.kanban_tools._is_delegated_child_context", lambda: True)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_env")
    from tools import kanban_tools as kt

    assert kt._check_kanban_mode() is False
    assert kt._check_kanban_orchestrator_mode() is False


def test_orchestrator_guard_refuses_woken_worker(monkeypatch):
    """Belt-and-suspenders handler guard must key on worker identity, not env."""
    monkeypatch.setattr("tools.kanban_tools._session_bound_task_id", lambda: "t_wake")
    from tools import kanban_tools as kt

    assert kt._require_orchestrator_tool("kanban_list") is not None


def test_orchestrator_guard_allows_unbound_session():
    from tools import kanban_tools as kt

    assert kt._require_orchestrator_tool("kanban_list") is None


# ---------------------------------------------------------------------------
# Schema layer — model_tools worker-surface append
# ---------------------------------------------------------------------------

def test_schema_includes_lifecycle_tools_for_woken_worker(monkeypatch):
    """Acceptance: a ``-t``-woken gateway session SEES the kanban tools even
    when its toolsets are restricted (no env, session binding only)."""
    monkeypatch.setattr("tools.kanban_tools._session_bound_task_id", lambda: "t_wake")
    names = _tool_names(["web"])
    assert {"kanban_show", "kanban_complete", "kanban_heartbeat"} <= names


def test_schema_appends_for_env_worker(monkeypatch):
    """CLI-spawn path is unchanged: restricted toolsets still get the surface."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_env")
    names = _tool_names(["web"])
    assert "kanban_show" in names


def test_schema_keeps_non_worker_restricted_toolsets_restricted():
    """No blanket append: a restricted non-worker toolset (webhook-safe style)
    stays restricted. 'Everyone' means default surfaces, not deliberate
    injection-safe postures."""
    names = _tool_names(["web"])
    assert "kanban_show" not in names