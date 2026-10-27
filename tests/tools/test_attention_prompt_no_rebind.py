"""Attention prompt must steer the dispatcher to answer the tool_limit_hit ask.

Bug 2026-10-27 (Evan): the parked-state prompt told the DISPATCHER to run the
worker's bind action ("re-bind with 'plan continue'"), which binds the calling
session and steals the worker's binding. The correct recovery is answering the
pending tool_limit_hit ask via 'plan review' with a 'response'. Per Evan: the
prompt must say "answer the tool_limit_hit ask" and the word "continue" must
appear NOWHERE in it.
"""

from unittest.mock import patch

import tools.plan_binding_adapter as pba


def _compose():
    task = {
        "id": "t_test1234",
        "task_steps": '["step one", "step two"]',
        "task_goal": "test goal",
    }
    pending = {"task_stepno": 1}
    with patch.object(pba, "_last_step_summary", lambda *a, **kw: ""), \
         patch.object(pba, "_step_expansions", lambda *a, **kw: {}):
        return pba._attention_request_text(task, pending, used=200, maximum=200)


def test_attention_prompt_names_the_ask_not_the_bind_action():
    text = _compose().lower()
    assert "tool_limit_hit" in text, "prompt must name the tool_limit_hit ask"
    assert "plan review" in text, "prompt must steer to plan review"
    assert "continue" not in text, (
        "prompt must not mention the worker's bind action — it binds the "
        "dispatcher's session and steals the worker's binding"
    )
