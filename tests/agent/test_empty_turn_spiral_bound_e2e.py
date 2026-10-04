
"""End-to-end: the empty-turn spiral bound fires through the real turn loop.

Drives ``run_conversation`` against an in-process mock provider returning
empty no-payload assistant responses (finish_reason=length) and asserts the
turn TERMINATES with the explicit spiral-error response instead of issuing
another call. The bound is sourced from config.yaml
(``agent.empty_turn_spiral_limit`` — no env var) with a small limit so the
test stays fast.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class _EmptyHandler(BaseHTTPRequestHandler):
    captured_requests: list = []

    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length).decode())
        type(self).captured_requests.append(req)
        # ALWAYS a poison response: empty content, finish_reason=length.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        chunks = [
            {"id": "m", "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]},
            {"id": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]},
        ]
        for c in chunks:
            self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, *a, **kw):
        pass


@pytest.fixture()
def spiral_agent():
    _EmptyHandler.captured_requests = []
    srv = HTTPServer(("127.0.0.1", 0), _EmptyHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    test_home = tempfile.mkdtemp(prefix="hermes_spiral_e2e_")
    os.makedirs(os.path.join(test_home, ".hermes"))
    # config.yaml: bound = 3 (small so E2E is fast); no env var per AGENTS.md.
    with open(os.path.join(test_home, ".hermes", "config.yaml"), "w") as f:
        f.write("agent:\n  empty_turn_spiral_limit: 3\n")
    prev_home = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = os.path.join(test_home, ".hermes")

    for mod in list(sys.modules):
        if mod == "run_agent" or mod.startswith("agent.") or mod.startswith("tools.") or mod.startswith("hermes_"):
            del sys.modules[mod]
    from run_agent import AIAgent

    agent = AIAgent(
        api_key="test-key", base_url=f"http://127.0.0.1:{port}/v1",
        provider="openai-compat", model="test-model",
        max_iterations=10, enabled_toolsets=[],
        quiet_mode=True, skip_context_files=True, skip_memory=True,
        save_trajectories=False, platform="cli",
    )
    agent.valid_tool_names = set()

    try:
        yield agent, _EmptyHandler
    finally:
        srv.shutdown()
        shutil.rmtree(test_home, ignore_errors=True)
        if prev_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = prev_home


def _chat_calls(handler):
    """Provider LLM calls only (exclude model-metadata probes, which lack 'messages')."""
    return [r for r in handler.captured_requests if isinstance(r, dict) and "messages" in r]


def test_spiral_bound_fires_end_to_end(spiral_agent):
    agent, handler = spiral_agent
    result = agent.run_conversation("spiral probe", conversation_history=[], task_id="t")

    resp = str(result.get("final_response", ""))
    assert "Empty-turn spiral bound" in resp, f"bound did not fire; got: {resp[:200]}"
    assert result.get("completed") is False
    # The turn must STOP calling the provider once the bound trips: a handful
    # of calls is the continuation ladder + the tripping call, not a runaway.
    calls = len(_chat_calls(handler))
    assert calls <= 12, f"runaway loop continued past the bound: {calls} calls"
    # Counter must be AT or OVER the limit at trip time and NOT reset by the
    # bound-trip path (bound trips return before finalize_turn).
    assert agent._poison_row_appends_session >= 3


def test_spiral_trip_uses_cumulative_count_across_preset(spiral_agent):
    """The bound counts appends since last recovery, agent-scoped, across turn boundaries.

    Preset 2 → first poison append makes it 3 → next loop head trips before
    another call. The recovery-reset behavior itself is pinned by the
    finalize_turn tests in test_empty_turn_spiral_bound.py; this test pins
    the loop-head side: the agent-scoped counter carries INTO the next turn
    (cached-agent semantics) and trips there.
    """
    agent, handler = spiral_agent
    agent._poison_row_appends_session = 2  # e.g. carried from a prior degenerate turn
    result = agent.run_conversation("cumulative probe", conversation_history=[], task_id="t2")

    resp = str(result.get("final_response", ""))
    assert "Empty-turn spiral bound" in resp, f"expected carry-over trip; got: {resp[:200]}"
    # Exactly one provider call happened: loop-head passed at 2, the first
    # poison append made it 3, the next loop head tripped before another call.
    assert len(_chat_calls(handler)) == 1, (
        f"expected exactly 1 LLM call (trip at next loop head), got {len(_chat_calls(handler))}"
    )
