"""Cell 9-L1+ — cron job wake (CLI-wake primary, in-process fallback) => outcome truth.

CONTRACT.md cell 9: the cron scheduler's wake seam must report the TRUE
outcome of `hermes send -t` — spawned | queued | dropped — and a DROPPED
wake is a hard sender-side error, never an ack (L1 law 1). The wake
transport tests (priority order, payload discipline, argv shape, journal
routing, dispatch bookkeeping) live in tests/cron/test_cron_wake_route.py
and test_cron_wake_transport.py — this file EXTENDS them; it does not
repeat them. What was missing there:

- outcome truth per response shape: the --json payload's ``status`` key
  drives _bridge_cli_wake's verdict. ``spawned`` and ``queued`` both
  count as delivered (a queued wake WILL run when the active turn
  finishes), ``suppressed`` is a benign duplicate, and ``dropped`` —
  the exact "row lands, no turn" bug class this suite exists for —
  must fail the wake even though the transport acked (exit 0, ok:true).
- the seam must actually READ the machine shape: without ``--json`` the
  CLI prints human text and every outcome key vanishes (the degraded
  exit-code-only check was the gap --json closed).
- honest failure paths: nonzero exit, ``error`` payload, unparseable
  non-JSON stdout.
- the exit-code half: a dropped/nonzero CLI must exit nonzero so shell
  callers cannot mistake it for delivery (send_cmd._emit_result).
- the seam is contract-honest when the REAL chain runs: the production
  argv goes through the REAL send_cmd.cmd_send -> REAL bridge -> REAL
  intake and the turn actually spawns; the CLI JSON the seam parses is
  produced by the code whose contract it checks, on the same socket.

L2 truth for the cron row->turn chain is the core cells' harness
(tests/dispatch/test_wake_intake_turn.py) — not repeated here.
"""

from __future__ import annotations

import asyncio
import json
import subprocess as _real_subprocess

import pytest


@pytest.fixture(autouse=True)
def _no_live_wake(monkeypatch):
    """Hostage guard (mirrors tests/cron/test_cron_wake_route.py): no test
    here may reach a real ``hermes send``. A forgotten fake fails loudly
    instead of waking a live agent."""

    def blocked(*_a, **_kw):
        raise AssertionError(
            "wake test tried to run a real subprocess — fake "
            "cron.scheduler.subprocess.run before invoking a wake path"
        )

    monkeypatch.setattr(_real_subprocess, "run", blocked)


def _fake_cli(monkeypatch, *, stdout="", stderr="", returncode=0):
    """Fake `hermes send` as a CompletedProcess with the exact rcode/stdout.

    Returns the list of argv lists the seam ran (so every test also pins
    that exactly one CLI attempt happened — no failed-then-fallback)."""
    calls: list[list[str]] = []

    def fake_run(argv, **_kw):
        calls.append([str(a) for a in argv])
        return _real_subprocess.CompletedProcess(
            argv, returncode, stdout=stdout, stderr=stderr
        )

    monkeypatch.setattr("cron.scheduler.subprocess.run", fake_run)
    return calls


def _dispatch_wake(monkeypatch, *, cli_result):
    """Run the full _dispatch_cron_wake bookkeeping with a faked transport.

    ``cli_result`` is the JSON the CLI prints. Returns the recorded
    (job_id, success, error, status) tuples from mark_job_run."""
    from cron.scheduler import CronWakeRoute, _dispatch_cron_wake

    _fake_cli(monkeypatch, stdout=cli_result)
    monkeypatch.setattr(
        "cron.scheduler._in_process_candidate_present", lambda *a, **k: False
    )
    marks: list[tuple] = []
    monkeypatch.setattr(
        "cron.scheduler.mark_job_run",
        lambda *a, **k: marks.append((*a, k.get("status"))),
    )
    monkeypatch.setattr(
        "cron.scheduler.finish_execution", lambda eid, **k: None
    )
    job = {"id": "job-outcome", "name": "Journal", "prompt": "run the journal",
           "skills": ["neo-journal"]}
    route = CronWakeRoute(mode="wake", target="neo", reason="journal")
    _dispatch_cron_wake(job, route, "exec-outcome", adapters=None, loop=None)
    return marks


# ---------------------------------------------------------------------------
# L1 outcome truth — per response shape (the status key drives the verdict)
# ---------------------------------------------------------------------------


class TestBridgeCliWakeOutcomeTruth:
    def test_spawned_status_confirms_delivery(self, monkeypatch):
        import subprocess

        calls = _fake_cli(
            monkeypatch, stdout='{"ok": true, "status": "spawned"}'
        )
        from cron.scheduler import _bridge_cli_wake

        assert _bridge_cli_wake("neo", "the brief") is True
        assert calls == [["hermes", "send", "-t", "neo", "--json", "the brief"]]

    def test_queued_status_is_delivery_not_dropped(self, monkeypatch):
        """A wake queued behind the session's active turn will still run —
        the seam counts it as delivered and says so honestly."""
        _fake_cli(monkeypatch, stdout='{"ok": true, "status": "queued"}')
        from cron.scheduler import _bridge_cli_wake

        assert _bridge_cli_wake("neo", "the brief") is True

    def test_dropped_status_is_a_hard_sender_side_error(self, monkeypatch):
        """L1 law 1, the suite's founding bug class: the transport acked
        (exit 0, ok:true) but the gateway DROPPED the wake — no turn
        spawned. The seam must fail the wake, never record wake fired."""
        import subprocess

        _fake_cli(monkeypatch, stdout='{"ok": true, "status": "dropped", "error": "no processing task"}')
        from cron.scheduler import _bridge_cli_wake

        assert _bridge_cli_wake("neo", "the brief") is False

    def test_suppressed_duplicate_is_a_benign_success(self, monkeypatch):
        """`suppressed` is the bridge's duplicate-wake answer — the wake
        target is already engaged on this work, so no new turn is wanted."""
        _fake_cli(monkeypatch, stdout='{"ok": true, "suppressed": true}')
        from cron.scheduler import _bridge_cli_wake

        assert _bridge_cli_wake("neo", "the brief") is True

    def test_legacy_ack_only_envelope_still_confirms(self, monkeypatch):
        """Pre-status bridge envelopes ({ok:true, queued:...}) carried no
        intake outcome; the seam treats them as confirmed (the 537a10306c
        era contract) rather than inventing a drop."""
        _fake_cli(monkeypatch, stdout='{"ok": true, "queued": false}')
        from cron.scheduler import _bridge_cli_wake

        assert _bridge_cli_wake("neo", "the brief") is True

    def test_exit_code_nonzero_fails_even_with_ok_json(self, monkeypatch):
        """A CLI that exits nonzero is a failed wake even if its stdout
        happens to parse as a success envelope — spawn success was never
        delivery."""
        import subprocess

        _fake_cli(
            monkeypatch,
            stdout='{"ok": true, "status": "spawned"}',
            stderr="boom",
            returncode=1,
        )
        from cron.scheduler import _bridge_cli_wake

        assert _bridge_cli_wake("neo", "the brief") is False

    def test_error_payload_fails_regardless_of_status(self, monkeypatch):
        """The CLI's own error key is a delivery failure — checked BEFORE
        the status/dropped key so a malformed envelope cannot pass."""
        _fake_cli(monkeypatch, stdout='{"ok": true, "status": "spawned", "error": "gateway unreachable"}')
        from cron.scheduler import _bridge_cli_wake

        assert _bridge_cli_wake("neo", "the brief") is False

    def test_human_text_stdout_degrades_gracefully_not_false_green(self, monkeypatch):
        """Without --json the CLI prints human text; the JSON parse yields
        {} and the envelope carries no outcome keys — the legacy-ack
        shape. The seam must not crash and must not invent a drop; the
        --json argv (pinned above) is what keeps this shape unreachable
        on the real path."""
        calls = _fake_cli(monkeypatch, stdout="sent\n")
        from cron.scheduler import _bridge_cli_wake

        assert _bridge_cli_wake("neo", "the brief") is True
        assert "--json" in calls[0], (
            "the delivery check reads outcome keys from the CLI's JSON; "
            "without --json every check degrades to exit-code-only"
        )

    def test_unparseable_json_stdout_fails_honestly(self, monkeypatch):
        """Garbage on stdout (a wedged CLI, a stack trace) must not crash
        the scheduler thread nor count as a delivery."""
        import subprocess

        _fake_cli(
            monkeypatch,
            stdout="Traceback (most recent call last): ...",
            returncode=1,
        )
        from cron.scheduler import _bridge_cli_wake

        assert _bridge_cli_wake("neo", "the brief") is False


class TestFireCronWakeOutcomeTruth:
    """fire_cron_wake composes the CLI wake with the in-process fallback —
    the composition must not launder a drop."""

    @pytest.fixture
    def no_in_process_candidate(self, monkeypatch):
        monkeypatch.setattr(
            "cron.scheduler._in_process_candidate_present",
            lambda *a, **k: False,
        )

    def test_dropped_cli_never_rescued_by_in_process(
        self, monkeypatch, no_in_process_candidate
    ):
        """A DROPPED verdict from the official path must end the wake —
        never a false green via a fallback the seam never earned."""
        _fake_cli(monkeypatch, stdout='{"ok": true, "status": "dropped"}')
        from cron.scheduler import fire_cron_wake

        assert fire_cron_wake("neo", "the brief") is False

    def test_error_result_with_live_candidate_still_fails_honestly(
        self, monkeypatch, no_in_process_candidate
    ):
        """Transport-level failure (error payload) with a verifiable
        in-process candidate that cannot land -> honest False."""
        _fake_cli(monkeypatch, stdout='{"error": "bridge socket not found"}', returncode=1)
        from cron.scheduler import fire_cron_wake

        assert fire_cron_wake("gopher", "the brief") is False

    def test_spawned_cli_result_is_delivered_no_fallback_attempt(self, monkeypatch):
        """A confirmed CLI wake never consults the in-process path (the
        failed-then-fallback pattern must not exist)."""
        calls = _fake_cli(monkeypatch, stdout='{"ok": true, "status": "spawned"}')
        optimization_calls: list = []
        monkeypatch.setattr(
            "cron.scheduler._in_process_wake",
            lambda *a, **k: optimization_calls.append(a) or False,
        )
        from cron.scheduler import fire_cron_wake

        assert fire_cron_wake("gopher", "the brief") is True
        assert len(calls) == 1
        assert optimization_calls == []


class TestDispatchRecordsHonestOutcome:
    """The outcome the scheduler records for the job — wake fired only on
    a confirmed delivery, an honest failure otherwise."""

    def test_confirmed_spawn_records_wake_fired(self, monkeypatch):
        marks = _dispatch_wake(
            monkeypatch, cli_result='{"ok": true, "status": "spawned"}'
        )
        from cron.scheduler import WAKE_FIRED_STATUS

        assert marks == [("job-outcome", True, None, WAKE_FIRED_STATUS)]

    def test_dropped_wake_records_failure_not_wake_fired(self, monkeypatch):
        """Law 1 at the bookkeeping layer: a dropped wake is recorded as a
        failed run with the true reason — ``last_status`` must never say
        'wake fired' for a turn that never spawned (the false-green bug
        this whole suite was founded on)."""
        import logging

        marks = _dispatch_wake(
            monkeypatch, cli_result='{"ok": true, "status": "dropped"}'
        )
        assert len(marks) == 1
        job_id, success, error, _kw = marks[0]
        assert job_id == "job-outcome"
        assert success is False
        assert error, "a dropped wake must record the failure reason"

    def test_queued_wake_still_records_wake_fired(self, monkeypatch):
        """A queued wake is a scheduled delivery — the job did its part."""
        marks = _dispatch_wake(
            monkeypatch, cli_result='{"ok": true, "status": "queued"}'
        )
        from cron.scheduler import WAKE_FIRED_STATUS

        assert marks == [("job-outcome", True, None, WAKE_FIRED_STATUS)]


# ---------------------------------------------------------------------------
# The exit-code half of the seam — send_cmd._emit_result must make a
# dropped / failed wake nonzero so the scheduler's check can never see a
# false zero (spawn success was the original false-green bug).
# ---------------------------------------------------------------------------


class TestWakeCliExitTruth:
    @pytest.mark.parametrize(
        "stdout,expected",
        [
            # Real CLI-boundary shapes: _send_bridge_command relabels the
            # bridge's {ok, status} envelope to {success, status, queued}.
            ('{"success": true, "status": "spawned", "queued": false}', 0),
            ('{"success": true, "status": "queued", "queued": true}', 0),
            ('{"success": true, "queued": true}', 0),
            ('{"success": true, "suppressed": true}', 0),
            ('{"success": true, "status": "dropped", "queued": false}', 1),
            ('{"error": "bridge socket not found"}', 1),
        ],
    )
    def test_exit_code_tracks_delivery_truth(self, stdout, expected):
        """The CLI exit code IS half the seam's check (returncode != 0
        fails the wake): a dropped wake must exit nonzero, a confirmed
        or queued wake zero. ``{"success": true, "status": "dropped"}``
        exiting 0 was the false-green this closes."""
        from hermes_cli.send_cmd import _FAILURE_EXIT, _emit_result

        code = _emit_result(stdout, json_mode=True, quiet=True)
        if expected == 0:
            assert code not in (1, _FAILURE_EXIT)
        else:
            assert code == _FAILURE_EXIT


# ---------------------------------------------------------------------------
# Real contract: the seam's verdict and the CLI's actual behavior must agree
# ---------------------------------------------------------------------------


class TestRealCliWakeChainAgreesWithSeam:
    """The production argv through the REAL cmd_send -> REAL bridge ->
    REAL intake: the outcome is spawned, the turn exists, the same JSON
    shape the seam parses is what the real CLI emitted."""

    def test_cron_wake_argv_lands_turn_and_seam_confirms(
        self, wake_env, monkeypatch, tmp_path
    ):
        # The mandatory visible copy spawns `hermes send -u`; hold the
        # real spawn_detached hostage so this test cannot launch anything
        # and a code change that stops faking it fails loudly.
        def blocked_spawn(argv, **_kw):
            raise AssertionError(
                "visible-copy spawn reached a real spawn_detached — the "
                "test must keep faking hermes_cli._subprocess_compat.spawn_detached"
            )

        monkeypatch.setattr(
            "hermes_cli._subprocess_compat.spawn_detached", blocked_spawn
        )

        runner, adapter, recorder = wake_env
        from tests.dispatch.wake_harness import (
            patch_send_tool_cli_env,
            run_cli_send_in_process,
            start_bridge_on,
        )

        smt = patch_send_tool_cli_env(monkeypatch)
        from hermes_cli import send_cmd

        monkeypatch.setattr(send_cmd, "_load_hermes_env", lambda: None)
        # The mandatory visible copy spawns `hermes send -u`; route it to
        # a throwaway fake so no platform send is attempted.
        monkeypatch.setattr(
            "hermes_cli._subprocess_compat.spawn_detached",
            lambda argv, **kw: object(),
        )
        # tools.send_message_tool resolves the bridge socket for the
        # ACTIVE profile home; point it at the test bridge.
        monkeypatch.setenv("HERMES_PROFILE", "")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "bridge-home"))
        bridge_home = tmp_path / "bridge-home"
        bridge_home.mkdir(parents=True)
        # bridge_socket_path() derives /tmp/hermes/mcp_bridge.<name>.sock
        # from the profile dir name; make the two sides agree.
        import gateway.mcp_bridge as mcp_bridge

        monkeypatch.setattr(
            mcp_bridge, "BRIDGE_SOCKET", str(tmp_path / "mcp_bridge.test.sock")
        )

        argv = ["hermes", "send", "-t", "telegram:8900123006", "--json", "[cron-wake] Journals\njob_id: j1\nrun the journal"]

        async def scenario():
            server, sock = await start_bridge_on(runner, monkeypatch, tmp_path)
            try:
                monkeypatch.setattr(
                    smt, "bridge_socket_path", lambda *_a, **_k: str(sock)
                )
                code, out = await asyncio.to_thread(run_cli_send_in_process, argv)
                await asyncio.wait_for(recorder.done.wait(), timeout=15)
                return code, out
            finally:
                server.close()
                await server.wait_closed()

        code, out = asyncio.run(scenario())

        assert adapter.errors == [], [str(e) for e in adapter.errors]
        assert code == 0, f"cmd_send exited {code}: {out}"
        payload = json.loads(out)
        # The CLI reports the bridge's true intake outcome...
        assert payload.get("status") == "spawned", payload
        assert payload.get("success") is True, payload
        # ...and the turn actually spawned on the real intake handler.
        assert len(recorder.turns) == 1, recorder.turns
        turn = recorder.turns[0]
        assert "[cron-wake] Journals" in turn["message"], turn["message"]
        assert turn["source"].chat_id == "8900123006"
        # The seam, given the exact stdout this real CLI produced, confirms.
        monkeypatch.setattr(
            "cron.scheduler.subprocess.run",
            lambda *a, **k: _real_subprocess.CompletedProcess(a, 0, stdout=out, stderr=""),
        )
        from cron.scheduler import _bridge_cli_wake

        assert _bridge_cli_wake("telegram:8900123006", "the brief") is True
