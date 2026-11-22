"""Sentinel-echo feedback-loop tests (t_0fb5836f).

Two halves, mirroring the joint-verification split in the bug file:

(a) Provider-window sweep — checkpoint rows whose *effective* replay content
    is exactly the interrupt sentinel (``[This response was interrupted by a
    user correction.]``) must never be sent to the provider as plain content:
    they teach the model to pattern-match the scaffolding and end its turn by
    echoing it verbatim (finish=stop, ~14 tokens), which the gateway strips
    and replaces with the "no response was generated" warning.
(b) Echo recovery — if the model still emits the sentinel as its final
    response, the turn must SUPPRESS the echo (never append/persist it) and
    retry the same turn with it masked, bounded, so the user gets a real
    answer instead of a wasted turn.

The E2E tests drive ``run_conversation`` against an in-process mock SSE
provider (same harness pattern as test_empty_turn_spiral_bound_e2e.py).
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

from agent.message_sanitization import INTERRUPT_CHECKPOINT_SENTINEL
from agent.agent_runtime_helpers import (
    sweep_sentinel_only_replay_rows,
)

CHECKPOINT = INTERRUPT_CHECKPOINT_SENTINEL
VISIBLE_DRAFT_CHECKPOINT = (
    "[This response was interrupted by a user correction.]\n\n"
    "Visible response before the interruption:\nVisible draft."
)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# ---------------------------------------------------------------------------
# Pure helper tests (no server)
# ---------------------------------------------------------------------------


class TestSentinelOnlyReplayRow:
    def test_plain_content_sentinel_is_detected(self):
        assert sweep_sentinel_only_replay_rows(
            [{"role": "assistant", "content": CHECKPOINT}]
        ) == []

    def test_api_content_sidecar_sentinel_is_detected(self):
        # Legacy hidden checkpoint rows carry the sentinel in BOTH content and
        # the api_content sidecar; either form must be swept.
        assert sweep_sentinel_only_replay_rows(
            [{"role": "assistant", "content": CHECKPOINT, "api_content": CHECKPOINT}]
        ) == []

    def test_user_role_sentinel_is_detected(self):
        assert sweep_sentinel_only_replay_rows(
            [{"role": "user", "content": CHECKPOINT}]
        ) == []

    def test_visible_draft_checkpoint_is_preserved(self):
        # A row that carries real (interrupted) content alongside the sentinel
        # header must keep replaying — it conveys the cut-off draft, not just
        # the machinery marker.
        rows = sweep_sentinel_only_replay_rows(
            [{"role": "assistant", "content": "Visible draft.", "api_content": VISIBLE_DRAFT_CHECKPOINT}]
        )
        assert len(rows) == 1
        assert rows[0]["api_content"] == VISIBLE_DRAFT_CHECKPOINT

    def test_normal_messages_are_preserved(self):
        rows = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "a real answer"},
            {"role": "tool", "content": "{}", "tool_call_id": "c1"},
        ]
        assert sweep_sentinel_only_replay_rows(rows) == rows

    def test_whitespace_only_sentinel_is_detected(self):
        assert sweep_sentinel_only_replay_rows(
            [{"role": "assistant", "content": f"  {CHECKPOINT}  "}]
        ) == []

    def test_non_string_content_is_preserved(self):
        rows = [{"role": "assistant", "content": [{"type": "text", "text": "x"}]}]
        assert sweep_sentinel_only_replay_rows(rows) == rows


class TestSweepMergesAdjacentUsers:
    def test_dropping_assistant_sentinel_merges_users(self):
        rows = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": CHECKPOINT},
            {"role": "user", "content": "actually do X"},
        ]
        swept = sweep_sentinel_only_replay_rows(rows)
        # The sentinel-only assistant row is dropped and the two user turns
        # merge into one (alternation preserved).
        assert len(swept) == 1
        assert swept[0]["role"] == "user"
        assert swept[0]["content"] == "hello\n\nactually do X"
        # Input untouched (per-call copy semantics).
        assert len(rows) == 3

    def test_consecutive_sentinel_rows_all_dropped(self):
        rows = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": CHECKPOINT},
            {"role": "assistant", "content": CHECKPOINT},
            {"role": "user", "content": "correction"},
        ]
        swept = sweep_sentinel_only_replay_rows(rows)
        assert len(swept) == 1
        assert swept[0]["content"] == "hi\n\ncorrection"


# ---------------------------------------------------------------------------
# E2E harness (in-process mock SSE provider)
# ---------------------------------------------------------------------------


class _SentinelHandler(BaseHTTPRequestHandler):
    """Mock provider. Class-level script controls the response sequence.

    ``responses`` is a list of content strings served to CHAT requests
    (those carrying a ``messages`` array) in order; the last one repeats.
    Finish reason is always "stop". Non-chat probes (model metadata, etc.)
    return 404 and do not consume the script.
    """

    responses: list = [CHECKPOINT]
    captured_requests: list = []
    chat_call_count: int = 0

    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length).decode())
        type(self).captured_requests.append(req)
        if "messages" not in req:
            self.send_response(404)
            self.end_headers()
            return
        idx = type(self).chat_call_count
        type(self).chat_call_count += 1
        content = type(self).responses[min(idx, len(type(self).responses) - 1)]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        chunks = [
            {"id": "m", "choices": [{"index": 0, "delta": {"role": "assistant", "content": content}, "finish_reason": None}]},
            {"id": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]
        for c in chunks:
            self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def do_GET(self):  # noqa: N802 (http.server API)
        self.send_response(404)
        self.end_headers()

    def log_message(self, *a, **kw):
        pass


@pytest.fixture()
def sentinel_agent():
    _SentinelHandler.responses = [CHECKPOINT]
    _SentinelHandler.captured_requests = []
    _SentinelHandler.chat_call_count = 0
    srv = HTTPServer(("127.0.0.1", 0), _SentinelHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    test_home = tempfile.mkdtemp(prefix="hermes_sentinel_echo_")
    os.makedirs(os.path.join(test_home, ".hermes"))
    with open(os.path.join(test_home, ".hermes", "config.yaml"), "w") as f:
        f.write("agent:\n  empty_turn_spiral_limit: 3\n")

    prev_home = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = os.path.join(test_home, ".hermes")

    saved_modules = {
        name: mod for name, mod in sys.modules.items()
        if name == "run_agent" or name.startswith(("agent.", "tools.", "hermes_"))
    }
    for mod in saved_modules:
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
        yield agent, _SentinelHandler
    finally:
        srv.shutdown()
        shutil.rmtree(test_home, ignore_errors=True)
        if prev_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = prev_home
        for name in list(sys.modules):
            if name == "run_agent" or name.startswith(("agent.", "tools.", "hermes_")):
                del sys.modules[name]
        sys.modules.update(saved_modules)


def _chat_calls(handler):
    """Provider LLM calls only (exclude model-metadata probes)."""
    return [r for r in handler.captured_requests if isinstance(r, dict) and "messages" in r]


def _replay_contents(request):
    out = []
    for m in request.get("messages", []):
        c = m.get("content")
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    out.append(part["text"])
    return out


class TestProviderWindowSweep:
    def test_sentinel_only_rows_never_reach_provider(self, sentinel_agent):
        agent, handler = sentinel_agent
        _SentinelHandler.responses = ["Here is your actual answer."]
        history = [
            {"role": "user", "content": "hello", "timestamp": 1.0},
            # Legacy gateway-polluted checkpoint: sentinel persisted as plain
            # content (pre-t_0c64b0d0 shape).
            {"role": "assistant", "content": CHECKPOINT, "timestamp": 2.0},
            {"role": "user", "content": "actually do X", "timestamp": 3.0},
            # Post-fix hidden checkpoint shape: sentinel in both fields.
            {"role": "assistant", "content": CHECKPOINT, "api_content": CHECKPOINT, "timestamp": 4.0},
            {"role": "user", "content": "wait, and also Y", "timestamp": 5.0},
            # Visible-draft checkpoint: must still replay (real content).
            {"role": "assistant", "content": "Visible draft.", "api_content": VISIBLE_DRAFT_CHECKPOINT, "timestamp": 6.0},
            {"role": "user", "content": "and then?", "timestamp": 7.0},
        ]
        result = agent.run_conversation(
            "and then?", conversation_history=history, task_id="t-sweep"
        )
        assert result.get("final_response") == "Here is your actual answer."
        calls = _chat_calls(handler)
        assert len(calls) >= 1
        for req in calls:
            for text in _replay_contents(req):
                assert text.strip() != CHECKPOINT, (
                    f"sentinel reached the provider verbatim: {text!r}"
                )
            joined = "\n".join(_replay_contents(req))
            assert "Visible response before the interruption:" in joined, (
                "visible-draft checkpoint must keep replaying"
            )
            assert "and then?" in joined


class TestEchoRecovery:
    def test_sentinel_echo_is_masked_and_turn_retried(self, sentinel_agent):
        agent, handler = sentinel_agent
        # First call echoes the sentinel, the retry answers for real.
        _SentinelHandler.responses = [CHECKPOINT, "Here is your actual answer."]
        result = agent.run_conversation(
            "hi", conversation_history=[], task_id="t-echo"
        )
        assert result.get("final_response") == "Here is your actual answer."
        calls = _chat_calls(handler)
        assert len(calls) == 2, (
            f"expected 2 LLM calls (echo + masked retry), got {len(calls)}"
        )
        # The echo must NOT have been appended: both requests carry the same
        # message count (the retry re-sends the identical window).
        assert len(calls[0]["messages"]) == len(calls[1]["messages"]), (
            "echo row leaked into the retry window"
        )

    def test_sentinel_echo_exhaustion_is_bounded(self, sentinel_agent):
        agent, handler = sentinel_agent
        # Provider always echoes the sentinel.
        _SentinelHandler.responses = [CHECKPOINT]
        result = agent.run_conversation(
            "hi", conversation_history=[], task_id="t-exhaust"
        )
        calls = _chat_calls(handler)
        # 1 initial + 2 masked retries = 3; the turn must terminate, not loop.
        assert len(calls) == 3, (
            f"sentinel echo retry was not bounded: {len(calls)} calls"
        )
        # Degenerate final is cleared by the existing finalizer — no echo
        # ships as the assistant message.
        assert result.get("final_response") == ""
