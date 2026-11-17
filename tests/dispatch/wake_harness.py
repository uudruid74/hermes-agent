"""Shared L2 harness for the Dispatch/Wake Conformance Suite (tests/dispatch/).

The contract (CONTRACT.md law 2) locks the ROW => INTAKE => TURN chain:
any path that writes a user-role row into a session MUST route through the
intake handler that spawns the turn — ``gateway/run.py``
``_handle_message_with_agent`` (the ``inbound message:`` site). The test
assertion is the TURN, not the row.

This harness wires the REAL intake handler to a fake adapter, exactly as the
card specifies. What stays REAL:

- ``GatewayRunner`` construction and every step of ``_handle_message_with_agent``
  (session resolution, topic recovery, task binding, context build, hooks,
  transcript persistence, final-response delivery).
- The bridge seam: ``mcp_bridge._handle_inject`` (and, in cell 2, the real
  Unix-socket bridge server) reports spawned/queued/dropped truthfully.
- The CLI wake seam: ``send_cmd.cmd_send`` (the ``hermes send -t`` path).

What is faked (the seams a unit suite must fake):

- The platform adapter: ``WakeAdapter.handle_message`` installs the session
  guard, then invokes the REAL intake handler (the contract a real adapter
  upholds), recording the events it received.
- The model loop: ``TurnRecorder`` replaces ``runner._run_agent`` at the
  instance boundary. It records the turn spawn (message/session) and returns
  a minimal completed result the real intake post-processes. This is the
  same seam the repo's gateway tests fake with ``run_agent.AIAgent``; here
  the whole intake runs real up to the agent-run boundary.
- External transports: ``spawn_detached`` / ``subprocess.run`` are routed
  in-process so no real ``hermes`` binary, socket, or network is touched.

DROPPED IS NEVER SUCCESS: the harness asserts the true outcome; a wake the
adapter swallows never reports a turn.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import types
from types import SimpleNamespace

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import SendResult
from gateway.session import SessionStore, build_session_key

# ---------------------------------------------------------------------------
# Model-loop seam
# ---------------------------------------------------------------------------


class TurnRecorder:
    """Replaces ``runner._run_agent``; records the turn spawn.

    The intake handler runs REAL all the way to the agent-run boundary; this
    recorder stands in for the model loop (same seam the repo's gateway tests
    fake via ``run_agent.AIAgent``) and returns a minimal completed result the
    real intake post-processes — transcript persistence, final-response
    delivery, typing cleanup.
    """

    def __init__(self):
        self.turns = []
        self.done = asyncio.Event()
        self.errors = []

    async def run(self, **kwargs):
        try:
            self.turns.append(kwargs)
            return {
                "final_response": "wake acknowledged: "
                + str(kwargs.get("message", ""))[:60],
                "response_previewed": False,
                "messages": [
                    {"role": "user", "content": str(kwargs.get("message", ""))},
                    {"role": "assistant", "content": "wake acknowledged"},
                ],
                "api_calls": 1,
                "history_offset": 2,
                "last_prompt_tokens": 10,
            }
        finally:
            self.done.set()


# ---------------------------------------------------------------------------
# Fake adapter (real intake underneath)
# ---------------------------------------------------------------------------


class WakeAdapter:
    """Mimics the real adapter contract on the wake seam.

    ``handle_message`` installs the session guard, then spawns the intake
    processing task — which invokes the REAL ``_handle_message_with_agent``.
    A busy session (guard already installed) queues and spawns no turn, so
    the bridge reports ``queued`` truthfully. An adapter that swallows the
    event (never installs the guard) leaves the bridge to report
    ``dropped`` — never a silent success.
    """

    def __init__(self, runner, platform=Platform.TELEGRAM):
        self.runner = runner
        self.platform = platform
        self.config = SimpleNamespace(extra={})
        self._active_sessions = {}
        self.handled_events = []
        self.sent = []
        self.errors = []

    async def handle_message(self, event):
        self.handled_events.append(event)
        key = build_session_key(
            event.source, group_sessions_per_user=True, thread_sessions_per_user=False
        )
        if key in self._active_sessions:
            return  # busy: queued, no new turn
        self._active_sessions[key] = asyncio.Event()
        asyncio.create_task(self._process(event, key))

    async def _process(self, event, key):
        try:
            # The real adapters claim a run generation before invoking the
            # intake; the intake's post-run finalize checks it (a stale
            # generation discards the result before delivery).
            gen = self.runner._begin_session_run_generation(key)
            await self.runner._handle_message_with_agent(event, event.source, key, gen)
        except Exception as exc:  # surface hidden intake failures to the test
            self.errors.append(exc)
        finally:
            self._active_sessions.pop(key, None)

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(
            {"chat_id": chat_id, "content": content, "metadata": metadata}
        )
        return SendResult(success=True, message_id=f"m-{len(self.sent)}")

    async def send_typing(self, chat_id, metadata=None):
        return None

    async def stop_typing(self, chat_id):
        return None


class _Writer:
    def __init__(self):
        self.payload = None
        self.closed = False

    def write(self, data):
        self.payload = json.loads(data.decode("utf-8"))

    def close(self):
        self.closed = True


# ---------------------------------------------------------------------------
# Runner bootstrap
# ---------------------------------------------------------------------------


def build_runner(monkeypatch, tmp_path):
    """Construct a REAL GatewayRunner wired to a WakeAdapter + TurnRecorder.

    Mirrors the established gateway-test bootstrap (tests/gateway/
    test_42039_duplicate_user_message.py): real constructor, real session
    store, minimal process-level sandboxing. The intake handler and the
    whole wake chain stay real; only the model loop and platform transport
    are faked by the harness.
    """
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"}
    )
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length", lambda *a, **k: 100_000
    )
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})

    config = GatewayConfig()
    runner = gateway_run.GatewayRunner(config)
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._is_user_authorized = lambda _source: True
    runner._session_db = None
    runner._recover_telegram_topic_thread_id = lambda _source: None

    # Give the runner a home channel so the intake never fires the
    # "/sethome" setup notice — the turn's final response is then the only
    # outbound delivery (mirrors a deployed gateway).
    from gateway.config import HomeChannel

    runner.config.get_home_channel = lambda _p: HomeChannel(
        platform=Platform.TELEGRAM, chat_id="8900123006", name="Test Home"
    )

    store = SessionStore(sessions_dir=tmp_path / "sessions", config=config)
    runner.session_store = store

    adapter = WakeAdapter(runner)
    runner.adapters[Platform.TELEGRAM] = adapter

    recorder = TurnRecorder()
    runner._run_agent = recorder.run
    return runner, adapter, recorder


async def start_bridge_on(runner, monkeypatch, tmp_path):
    """Start the REAL bridge socket server bound to ``runner``."""
    import gateway.mcp_bridge as mcp_bridge

    sock = tmp_path / "mcp_bridge.test.sock"
    monkeypatch.setattr(mcp_bridge, "BRIDGE_SOCKET", str(sock))
    server = await mcp_bridge.start_bridge_server(runner)
    return server, sock


def inject_payload(**overrides):
    payload = {
        "platform": "telegram",
        "chat_id": "8900123006",
        "text": "Incoming message from zephyr follows: --- test",
        "chat_type": "dm",
        "user_context": {
            "user_id": "evan",
            "platform_user_id": "8900123006",
            "sender_name": "CLI User",
        },
    }
    payload.update(overrides)
    return payload


def run_async_immediately(coro):
    return asyncio.run(coro)


def patch_send_tool_cli_env(monkeypatch):
    """Fake the CLI-side environment ``send_message_tool`` needs for the
    internal wake branch: a configured-but-fake telegram platform, CLI user
    identity, no in-process gateway runner (so the send crosses the bridge
    socket), and the inner asyncio runner.

    Returns the ``tools.send_message_tool`` module for ``bridge_socket_path``
    patching.
    """
    from types import SimpleNamespace

    import gateway.run as gateway_run

    from gateway.config import PlatformConfig
    from tools import send_message_tool as smt

    fake_pconfig = PlatformConfig(enabled=True, token="x", extra={})
    fake_config = SimpleNamespace(
        platforms={Platform.TELEGRAM: fake_pconfig},
        get_home_channel=lambda _p: None,
    )
    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: fake_config)
    monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False)
    monkeypatch.setattr("model_tools._run_async", run_async_immediately)
    monkeypatch.setattr(
        "gateway.session_context.get_session_env",
        lambda name, default="": {
            "HERMES_SESSION_USER_ID": "operator-id",
            "HERMES_SESSION_TELEGRAM_ID": "telegram-user-id",
            "HERMES_SESSION_USER_NAME": "Operator",
        }.get(name, default),
    )
    monkeypatch.setattr(gateway_run, "_gateway_runner_ref", lambda: None)
    return smt


# ---------------------------------------------------------------------------
# In-process CLI wake runner (no subprocess / no network)
# ---------------------------------------------------------------------------


def run_cli_send_in_process(argv):
    """Execute a ``hermes send -t ...`` argv through the REAL ``cmd_send``.

    Returns ``(exit_code, stdout_text)`` — the JSON result the CLI emits
    to stdout (the same payload ``cron.scheduler._bridge_cli_wake``
    verifies). ``cmd_send`` sys.exits; ``_emit_result`` prints the JSON
    when ``json=True``.
    """
    argv = [str(a) for a in argv]
    if "-t" not in argv:
        raise ValueError("argv must contain -t <target> <message>")
    i = argv.index("-t")
    target = argv[i + 1]
    # The message is the first non-flag token after the target: production
    # callers (cron.scheduler._bridge_cli_wake) interpose flags like
    # ``--json`` between the target and the body.
    j = i + 2
    while j < len(argv) and argv[j].startswith("-"):
        if argv[j] == "--task-id" and j + 1 < len(argv):
            j += 2  # the flag consumes a value
            continue
        j += 1
    if j >= len(argv):
        raise ValueError(f"no message found after -t in {argv!r}")
    message = argv[j]

    task_id = None
    if "--task-id" in argv:
        task_id = argv[argv.index("--task-id") + 1]

    from hermes_cli import send_cmd

    args = argparse.Namespace(
        to=None,
        user=target,
        message=message,
        file=None,
        subject=None,
        list_targets=False,
        json=True,
        quiet=True,
        raw=False,
        task_id=task_id,
    )
    import contextlib
    import io

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            send_cmd.cmd_send(args)
        code = 0
    except SystemExit as exc:
        code = int(exc.code or 0)
    return code, buf.getvalue()
