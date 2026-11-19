"""WAKE-PATH diagnose-ability (t_8a483fa6).

The CLI wake (``_bridge_cli_wake``) runs ``hermes send -t <target> --json``;
on nonzero exit it used to log only stderr, which was empty on the
2026-11-18 gopher-journal-daily failure — the real reason lived in the
--json stdout payload ({'error': ...}) and was silently swallowed. These
tests pin that the failure path now logs the parsed stdout error (and
status), and falls back to a truncated raw stdout slice when stdout is
not JSON.

Module guard: no test here may reach a real `hermes send` — the subprocess
is mocked everywhere in this module.
"""
from __future__ import annotations

import json
import subprocess

import pytest


@pytest.fixture(autouse=True)
def _mocked_subprocess(monkeypatch):
    """subprocess.run returns exit 1 with a JSON error payload on stdout."""

    def fake_run(argv, **kw):
        return subprocess.CompletedProcess(
            argv,
            1,
            stdout=json.dumps({"error": "bridge socket not found", "status": "failed"}),
            stderr="",
        )

    monkeypatch.setattr("cron.scheduler.subprocess.run", fake_run)


def test_cli_wake_nonzero_exit_logs_stdout_json_error(caplog):
    """The Nov-18 failure mode: stderr empty, reason only in stdout JSON."""
    from cron.scheduler import _bridge_cli_wake

    assert _bridge_cli_wake("gopher", "brief") is False

    # The logged failure must carry the parsed stdout payload's reason.
    assert "exited 1" in caplog.text
    assert "stdout error: bridge socket not found" in caplog.text
    assert "stdout status: failed" in caplog.text


def test_cli_wake_nonzero_exit_unparsable_stdout_logs_raw_slice(caplog, monkeypatch):
    """Non-JSON stdout on failure degrades to a truncated raw slice."""
    monkeypatch.setattr(
        "cron.scheduler.subprocess.run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, stdout="not json", stderr=""),
    )

    from cron.scheduler import _bridge_cli_wake

    assert _bridge_cli_wake("gopher", "brief") is False
    assert "stdout (unparsed): not json" in caplog.text
