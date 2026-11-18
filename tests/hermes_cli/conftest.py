"""Fixtures shared across hermes_cli kanban tests."""

from __future__ import annotations

import sys
from types import ModuleType

import pytest


def _purge_hermes_modules(*prefixes: str) -> dict[str, ModuleType]:
    """Delete modules under *prefixes* from ``sys.modules``; return a snapshot.

    Used by fixtures that force-reimport hermes_* modules so a freshly set
    ``HERMES_HOME`` is picked up. The caller MUST restore the snapshot after
    the test: leaving the purge in place splits module identity for every
    later test in the process (parent-package attributes keep pointing at the
    fresh copies while a test's import-time bound functions still execute
    against the old module globals — see agent/conftest guard, issue class
    #61597). Until the fix for suite-red-b1009b27 (t_e10339f1), that split
    silently voided the npm-engine tests' monkeypatches, so the real
    managed-Node provisioning ran inside the sandboxed HERMES_HOME (~201MB
    per test tmp dir) and the full-suite verify blew its artifact budget.
    """
    saved = {
        name: mod
        for name, mod in sys.modules.items()
        if any(name == p or name.startswith(p + ".") for p in prefixes)
    }
    for name in saved:
        del sys.modules[name]
    return saved


def _restore_hermes_modules(saved: dict[str, ModuleType], *prefixes: str) -> None:
    """Undo :func:`_purge_hermes_modules`: drop fresh copies, restore saved."""
    fresh = [
        name
        for name in list(sys.modules)
        if any(name == p or name.startswith(p + ".") for p in prefixes)
        and name not in saved
    ]
    for name in fresh:
        del sys.modules[name]
    sys.modules.update(saved)


@pytest.fixture
def all_assignees_spawnable(monkeypatch):
    """Pretend every assignee maps to a real Hermes profile.

    Most dispatcher tests use synthetic assignees ("alice", "bob") that
    don't correspond to actual profile directories on disk. Without this
    patch, the dispatcher's profile-exists guard (PR #20105) routes
    those tasks into ``skipped_nonspawnable`` instead of spawning, which
    would break tests that assert spawn behavior.
    """
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


@pytest.fixture(autouse=True)
def _suppress_concurrent_hermes_gate(request, monkeypatch):
    """Default ``_detect_concurrent_hermes_instances`` to ``[]`` for every test.

    The Windows update path now refuses to proceed when another
    ``hermes.exe`` is detected (issue #26670). On a developer's Windows
    machine running the test suite via ``hermes`` itself, this would
    flag the running agent as a concurrent instance and abort every
    ``cmd_update`` test. Tests that want to exercise the gate explicitly
    re-patch ``_detect_concurrent_hermes_instances`` with their own
    return value — autouse here gives a clean default without touching
    the rest of the suite.

    Tests that need to call the REAL function (e.g. unit tests for the
    helper itself) opt out with ``@pytest.mark.real_concurrent_gate``.
    """
    if request.node.get_closest_marker("real_concurrent_gate"):
        return
    try:
        from hermes_cli import main as _cli_main
    except Exception:
        return
    # raising=False: under pytest's per-test spawn isolation, a concurrent
    # xdist worker importing a module that transitively touches hermes_cli.main
    # can briefly expose a partially-initialized module object here — one where
    # _detect_concurrent_hermes_instances isn't defined yet. A bare setattr
    # would raise AttributeError and error the (unrelated) test. The attribute
    # always exists once main.py finishes importing, so a no-op when it's
    # transiently absent is the correct, race-free default.
    monkeypatch.setattr(
        _cli_main,
        "_detect_concurrent_hermes_instances",
        lambda *_a, **_k: [],
        raising=False,
    )
