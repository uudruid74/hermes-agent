"""Guard: the executor's hardcoded arg whitelists must forward EVERY declared arg.

WHY THIS EXISTS (2026-09-19)
----------------------------
``agent/tool_executor.py`` special-cases a handful of tools with a hardcoded
branch that rebuilds the call argument-by-argument::

    elif function_name == "plan_tool":
        def _execute(next_args: dict) -> Any:
            from tools.plan_tool import plan_tool as _plan_tool
            return _plan_tool(
                agent=agent,
                command=next_args.get("command", ""),
                ...
                pre_approved=next_args.get("pre_approved", False),
            )

That list is **hand-maintained**, and when a parameter was added to the schema
and the tool function, nobody updated the middle.  Three live defects came from
exactly this shape:

  * ``plan_tool``: ``proof`` and ``step`` were declared in ``PLAN_TOOL_SCHEMA``
    and accepted by ``plan_tool()``, but never forwarded.  Every ``advance``
    reached the two-phase completion gate with ``proof=None``, so a step with a
    commit hash + test result was recorded as a bare CLAIM and returned
    unchanged — *"STEP n NOT ADVANCED — verify before claiming"* — and no
    ``RECEIPT`` was ever written.  The gate was blamed for three calls before
    the real cause was found.
  * ``plan_tool``: ``parent_task_id`` — a nested sub-plan silently became a
    root plan.
  * ``session_search``: ``profile`` — cross-profile recall silently read the
    CURRENT profile's database instead.

None of these raise, log, or look wrong.  The parameter is present at both ends
and dropped in the middle, which is why a source-level guard is the only cheap
detector.

WHAT IT CHECKS
--------------
For each ``elif function_name == "<tool>":`` branch that builds its call with
``next_args.get(...)``, resolve the tool's declared schema properties and assert
every one is forwarded.  An unmapped tool is a hard failure (add it to
``SCHEMAS``) rather than a silent skip — the guard must not go quiet when a new
special-cased tool appears.
"""

from __future__ import annotations

import importlib
import pathlib
import re
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
EXECUTOR = REPO / "agent" / "tool_executor.py"

# tool name -> (module, schema attribute candidates)
SCHEMAS = {
    "plan_tool": ("tools.plan_tool", ["PLAN_TOOL_SCHEMA"]),
    "clarify": ("tools.clarify_tool", ["CLARIFY_SCHEMA"]),
    "memory": ("tools.memory_tool", ["MEMORY_SCHEMA"]),
    "read_terminal": ("tools.read_terminal_tool", ["READ_TERMINAL_SCHEMA"]),
    "session_search": ("tools.session_search_tool", ["SESSION_SEARCH_SCHEMA"]),
}


def _forwarded_args(source: str) -> dict[str, set[str]]:
    """tool -> set of arg names its executor CALL BRANCH forwards.

    Only branches that actually build a tool call count.  A bare
    ``elif function_name == "skill_manage": agent._iters_since_skill = 0`` is a
    state-flag marker, not a call site — treating it as one made an earlier
    version of this guard scan a 1000-line window and attribute other tools'
    arguments to it.
    """
    blocks: dict[str, set[str]] = {}
    for match in re.finditer(r'elif function_name == "([a-z_0-9]+)":', source):
        name = match.group(1)
        start = match.end()
        # Bound the window at the NEXT branch marker of either kind.  Stopping
        # only at `elif` let a state-flag branch swallow 1000 lines of later
        # `if function_name ==` branches and inherit their arguments.
        candidates = [
            pos
            for pat in (r'elif function_name ==', r'if function_name ==')
            if (pos := source.find(pat, start)) != -1
        ]
        body = source[start: min(candidates) if candidates else start + 6000]
        # A real call branch opens with `def _execute(next_args: ...)`.
        if "def _execute(next_args" not in body:
            continue
        keys = set(re.findall(r'next_args\.get\(\s*"([a-z_0-9]+)"', body))
        if keys:
            blocks[name] = keys
    return blocks


def _declared_args(module_name: str, attrs: list[str]) -> set[str]:
    sys.path.insert(0, str(REPO))
    mod = importlib.import_module(module_name)
    for attr in attrs:
        spec = getattr(mod, attr, None)
        if isinstance(spec, dict) and spec.get("parameters"):
            return set((spec["parameters"].get("properties") or {}).keys())
    raise AssertionError(
        f"{module_name}: no schema dict among {attrs} — update SCHEMAS in this test"
    )


@pytest.fixture(scope="module")
def forwarded() -> dict[str, set[str]]:
    if not EXECUTOR.exists():
        pytest.skip("tool_executor.py not present")
    return _forwarded_args(EXECUTOR.read_text())


def test_every_special_cased_tool_is_covered(forwarded):
    """A new hardcoded branch must be added to SCHEMAS, not silently ignored."""
    uncovered = sorted(set(forwarded) - set(SCHEMAS))
    assert not uncovered, (
        f"tool_executor.py special-cases {uncovered} with an arg whitelist but "
        f"this guard does not know their schemas. Add them to SCHEMAS — an "
        f"unmapped tool means this guard goes quiet exactly when it is needed."
    )


@pytest.mark.parametrize("tool", sorted(SCHEMAS))
def test_executor_forwards_all_declared_args(tool, forwarded):
    """The whitelist must pass through every arg the schema declares."""
    if tool not in forwarded:
        pytest.skip(f"{tool} has no next_args whitelist in tool_executor.py")
    module_name, attrs = SCHEMAS[tool]
    declared = _declared_args(module_name, attrs)
    forwarded_args = forwarded[tool]
    dropped = sorted(declared - forwarded_args)
    assert not dropped, (
        f"{tool}: tool_executor.py drops declared argument(s) {dropped}. "
        f"They are present in the schema and accepted by the tool, but the "
        f"hardcoded branch in tool_executor.py never forwards them — so the "
        f"tool silently behaves as if they were not supplied. This is how "
        f"plan_tool lost `proof`/`step` (2026-09-19)."
    )


def test_guard_is_load_bearing(monkeypatch, forwarded):
    """Prove the check fails when an arg is dropped — not just that it passes.

    Reproduces the original defect shape in-memory: remove `proof` from the
    forwarded set and confirm the comparison catches it. Without this the guard
    could pass for the wrong reason (e.g. schema resolution returning nothing).
    """
    if "plan_tool" not in forwarded:
        pytest.skip("plan_tool has no whitelist in this build")
    declared = _declared_args("tools.plan_tool", SCHEMAS["plan_tool"][1])
    assert "proof" in declared, "fixture assumption: proof is a declared arg"

    mutated = dict(forwarded)
    mutated["plan_tool"] = set(forwarded["plan_tool"]) - {"proof"}
    dropped = sorted(declared - mutated["plan_tool"])
    assert "proof" in dropped, "the guard must detect a dropped `proof`"
