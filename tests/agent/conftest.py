"""Shared fixtures for tests/agent.

The order-dependent pollution class (tests that pass alone but fail after
other modules ran) traced to ONE leaked global: ``agent_runtime_helpers.
_current_agent``. Tests that build real AIAgent/prologue contexts leave it
pointing at their agent — whose SessionDB is closed at fixture teardown —
so any LATER test whose code path resolves plan/execution state
(``resolve_active_for_agent -> get_compression_root -> conn.execute``)
crashes on the dead connection ('NoneType' object has no attribute
'execute'), and the write-gate/safe-root classifiers misclassify. Resetting
the global after every test kills the class at the source instead of
chasing 100+ victim tests.
"""

from __future__ import annotations

import sys

import pytest


@pytest.fixture(autouse=True)
def _reset_current_agent():
    yield
    from agent import agent_runtime_helpers

    agent_runtime_helpers._current_agent = None


@pytest.fixture(autouse=True)
def _module_table_guard():
    """Snapshot/restore runtime module identity around every test.

    Several tests purge ``run_agent``/``agent.*``/``tools.*``/``hermes_*``
    from sys.modules and re-import fresh copies (to exercise import-time
    behavior). Without a restore that splits module identity for every
    later test in the process: parent-package attributes (``agent.x``)
    keep pointing at the fresh copies while sys.modules may hold another —
    ``import agent.x as md`` then binds a different object than the one a
    test's bound imports/functions use, so state set on ``md`` is invisible
    to the code under test (issue class of #61597).

    This guard is the class-level kill: it snapshots the table before each
    test and puts it back (module entries AND parent-package attrs)
    afterwards, so any purge style is contained. Per-test monkeypatching of
    module *attributes* is unaffected (monkeypatch restores its own).
    """
    saved = {
        name: mod
        for name, mod in sys.modules.items()
        if name == "run_agent" or name.startswith(("agent.", "tools.", "hermes_"))
    }
    yield
    for name in list(sys.modules):
        if name == "run_agent" or name.startswith(("agent.", "tools.", "hermes_")):
            del sys.modules[name]
    sys.modules.update(saved)
    for name, mod in saved.items():
        parent_name, _, child = name.rpartition(".")
        parent = sys.modules.get(parent_name)
        if parent is not None:
            setattr(parent, child, mod)
