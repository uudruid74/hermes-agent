# Plan task-identity reconciliation (t_32b1ed86)

This document supersedes task-local Plan/session fixes. Runtime task identity is
only `execution_bindings(profile, compression_root_session_id)`. `sessions.task_id`,
`canonical_session_id`, and `HERMES_KANBAN_TASK` are migration/bootstrap data and
must not be added as runtime fallbacks.

## Historical behavior matrix

| Provenance | Preserved invariant | Final authority | Regression coverage |
|---|---|---|---|
| t_ceca8b15 | terminal top-level close clears binding; nested close restores explicit parent | `execution_bindings.close_plan` | `tests/hermes_cli/test_execution_bindings.py` |
| t_1efd1efd / 6fb1036b7 | unanswered approval stays `blocked/approval`, never denial | `plan_authorizations` + adapter | `tests/tools/test_plan_tool.py` |
| t_c2472725 / 055e0f51 / facf7e32 / 0bb6f9a0 | explicit historical IDs for remind/block/archive; neutral test close | Plan adapter | `tests/tools/test_plan_tool.py`, `test_plan_tool_debug.py` |
| t_87904541 / 4e6b2722 / 48fa3949 / de13f3ea | compression-root identity replaces canonical-session authority | binding resolver | `test_plan_binding_adapter.py`, `test_plan_execution_binding_lineage.py` |
| 9e229a23 | dashboard approval remains durable/atomic | `plan_authorizations.resolve_plan_in_txn` | `tests/tools/test_plan_tool.py` |
| t_e79e2c12 / f99dab05 | Plan wrapper forwards board, cron, root, kind, debug flags | `tools/plan_tool.py` | `tests/tools/test_plan_tool.py` |
| t_bc92291c / t_dacd1f6c | deterministic fail-closed binding kernel | `hermes_cli/execution_bindings.py` | `tests/hermes_cli/test_execution_bindings.py` |
| t_aa4631b3 | no code salvaged | N/A | provenance only |
| wake recovery 09aa29622 / 7a9d00c52 | preserve task creator provenance without merging wake branch | `_cmd_dispatch(created_by=...)` | Plan tool regression suite |

## Operational rules

* New nested plans require an explicit `parent_task_id`; implicit nesting is
  forbidden.
* Approval, activation, binding, and binding audit use one transaction, with
  nested savepoints where an authorization operation is called inside it.
* `done`, `fail`, and `test-complete` close through the same compare-and-set
  kernel transition.
* Current Plan context belongs only in the current user message's `api_content`
  sidecar. Never alter the cached system prompt or synthesize a user turn.
* `hermes_cli.execution_binding_migration.migration_manifest` is read-only:
  it produces deterministic candidates/conflicts and never infers approval or
  completion. Applying a manifest is operator/Wintermute work.

When revisiting any task above, extend the shared binding kernel and its matrix
row; do not restore session, canonical-session, or environment fallbacks.
