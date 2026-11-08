"""Cron wake dispatch: journal -> `hermes send -t` into a live gateway session (t_f961f13b).

t_94b593b6 hardening (this file): the verified CLI wake is the official and
primary mechanism and is attempted first, always — the fragile in-process
adapter injection is a last-resort optimization that runs only when the
official path cannot confirm delivery and must prove itself; a wake never
reports a failed first attempt and then a rescued fallback. The CLI wake
runs to completion and its JSON result is checked — spawn success alone is
never delivery.
"""
from __future__ import annotations

import subprocess as _real_subprocess

import pytest


@pytest.fixture(autouse=True)
def _no_live_wake(monkeypatch):
    """Module guard (t_94b593b6): no test here may reach a real `hermes send`.

    The wake paths shell out to the `hermes` binary; a test that forgets to
    fake `cron.scheduler.subprocess.run` could deliver a real wake to a live
    agent (that happened once during debugging: the 03:16 Gopher row). Any
    wake test must fake the subprocess call itself; this fixture holds the
    real one hostage and turns a miss into a loud, non-destructive failure.
    """

    def blocked(*_a, **_kw):
        raise AssertionError(
            "wake test tried to run a real subprocess — fake "
            "cron.scheduler.subprocess.run before invoking a wake path"
        )

    monkeypatch.setattr(_real_subprocess, "run", blocked)


# ---------------------------------------------------------------------------
# Journal detection (ruling #8) — structured skills signal
# ---------------------------------------------------------------------------

def test_journal_detected_by_skill_suffix():
    from cron.scheduler import is_journal_cron

    assert is_journal_cron({"skills": ["neo-journal"]}) is True
    assert is_journal_cron({"skills": ["wintermute-journal", "other"]}) is True


def test_journal_detected_on_legacy_skill_field():
    from cron.scheduler import is_journal_cron

    assert is_journal_cron({"skill": "gopher-journal"}) is True


def test_journal_marker_is_suffix_not_substring():
    """`journal-article` must NOT be a journal (suffix match, Gopher)."""
    from cron.scheduler import is_journal_cron

    assert is_journal_cron({"skills": ["journal-article"]}) is False
    assert is_journal_cron({"skills": ["journaling"]}) is False


def test_non_journal_is_not_a_journal():
    from cron.scheduler import is_journal_cron

    assert is_journal_cron({"skills": ["code-review"]}) is False
    assert is_journal_cron({}) is False


def test_cron_skill_names_normalization():
    from cron.scheduler import _cron_skill_names

    assert _cron_skill_names({"skills": ["a", " b "]}) == ["a", "b"]
    assert _cron_skill_names({"skills": "solo"}) == ["solo"]
    assert _cron_skill_names({"skill": "legacy"}) == ["legacy"]
    assert _cron_skill_names({}) == []


# ---------------------------------------------------------------------------
# Routing — EXCLUSIVE wake XOR spawn, journal -> DM (ruling #8/#4)
# ---------------------------------------------------------------------------

def test_journal_routes_to_wake_with_dm_target(monkeypatch):
    from cron.scheduler import resolve_cron_wake_route

    # Deterministic DM target (profile resolution is tested separately).
    monkeypatch.setattr("cron.scheduler._agent_dm_wake_target", lambda: "neo")
    route = resolve_cron_wake_route({"skills": ["neo-journal"]})
    assert route.is_wake is True
    assert route.mode == "wake"
    assert route.reason == "journal"
    # Ruling #8/#4: the target is the agent's DM (bare profile name).
    assert route.target == "neo"


def test_non_journal_routes_to_spawn(monkeypatch):
    """Non-journal stays on the unchanged legacy spawn path (additive)."""
    from cron.scheduler import resolve_cron_wake_route

    monkeypatch.setenv("HERMES_PROFILE", "neo")
    route = resolve_cron_wake_route({"skills": ["code-review"]})
    assert route.is_wake is False
    assert route.mode == "spawn"
    assert route.target is None


def test_route_is_exclusive(monkeypatch):
    """Wake XOR spawn: a wake always hides a target, a spawn never does."""
    from cron.scheduler import resolve_cron_wake_route

    monkeypatch.setenv("HERMES_PROFILE", "neo")
    wake = resolve_cron_wake_route({"skills": ["neo-journal"]})
    spawn = resolve_cron_wake_route({"skills": ["code-review"]})
    assert (wake.is_wake) != (spawn.is_wake)
    assert bool(wake.target) != bool(spawn.target)


def test_wake_route_requires_a_target(monkeypatch):
    """A wake with no resolvable DM target must be rejected, not fired (ruling #4)."""
    from cron.scheduler import resolve_cron_wake_route

    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.delenv("USERNAME", raising=False)
    monkeypatch.setattr(
        "cron.scheduler._agent_dm_wake_target", lambda: ""
    )
    with pytest.raises(AssertionError):
        resolve_cron_wake_route({"skills": ["neo-journal"]})


# ---------------------------------------------------------------------------
# Transport — `hermes send -t <target> <prompt>` (mirrors tools/tell_tool.py)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Transport — `hermes send -t <target> <prompt>` (mirrors tools/tell_tool.py)
# ---------------------------------------------------------------------------
#
# t_94b593b6 priority (flipped per Evan): the verified CLI wake is the
# official/primary delivery path; the in-process adapter injection is an
# optimization that must prove itself and defer on any failure.  The two
# sites share the "CLI User" identity derivation.

def test_cli_wake_is_primary_when_no_candidate(monkeypatch):
    """With no live adapter/loop, the verified CLI wake runs and confirms."""
    import subprocess

    calls = []

    def fake_run(argv, **kw):
        calls.append((argv, kw.get("timeout")))
        return subprocess.CompletedProcess(
            argv, 0, stdout='{"success": true}', stderr=""
        )

    # No adapters, no loop: gate must say "no candidate" and the CLI wake
    # must be the path that runs.
    monkeypatch.setattr("cron.scheduler.subprocess.run", fake_run)
    from cron.scheduler import _in_process_candidate_present, fire_cron_wake

    assert _in_process_candidate_present("neo", adapters=None, loop=None) is False
    ok = fire_cron_wake("gopher", "short prompt")

    assert ok is True
    assert calls == [
        (
            ["hermes", "send", "-t", "gopher", "short prompt"],
            90.0,
        )
    ]
    assert calls[0][0] == ["hermes", "send", "-t", "gopher", "short prompt"]
    # The CLI was RUN synchronously (blocking, bounded) — never detached.
    assert calls[0][1] is not None


def _live_loop():
    """A running event loop in a background thread (tests only)."""
    import asyncio
    import threading

    loop = asyncio.new_event_loop()

    def run():
        asyncio.set_event_loop(loop)
        loop.run_forever()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return loop


def test_in_process_optimization_is_last_resort(monkeypatch):
    """The in-process injection is an optimization only — it is NOT
    attempted before the official CLI wake.  It applies only when the
    official path cannot confirm delivery, and only when a live adapter
    AND loop are verifiable; then it must prove itself or the wake fails."""
    accepted = []

    class FakeAdapter:
        name = "FakeAdapter"

        async def handle_message(self, event):
            accepted.append(event)

    fake_adapter = FakeAdapter()
    fake_runner = type(
        "FakeRunner",
        (),
        {
            "adapters": {"telegram": fake_adapter},
            "_profile_adapters": {"neo": {"telegram": fake_adapter}},
            "_gateway_loop": None,
        },
    )()
    monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: fake_runner)

    loop = _live_loop()
    try:
        cli_calls = []
        monkeypatch.setattr(
            "cron.scheduler._bridge_cli_wake",
            lambda target, body, **kw: cli_calls.append((target, body)) or False,
        )
        from cron.scheduler import _in_process_candidate_present, fire_cron_wake

        # Even with adapters+loop present, the official path is tried first
        # and is the path that defines success.
        assert _in_process_candidate_present("neo", loop=loop) is True
        ok = fire_cron_wake("neo", "the journal prompt", loop=loop)

        assert ok is True
        assert cli_calls == [("neo", "the journal prompt")]
        assert len(accepted) == 1  # last-resort injection landed
        event = accepted[0]
        assert event.text.startswith("Incoming user wake (cron):")
        assert "the journal prompt" in event.text
        assert event.internal is False  # a real inbound user identity
        assert event.source.chat_id == "8900123006"
    finally:
        loop.call_soon_threadsafe(loop.stop)


def test_cli_win_is_used_without_failed_attempt(monkeypatch):
    """When the official CLI wake confirms delivery, the optimization is
    never attempted at all — no failed first attempt, no rescue path,
    no fallback chain.  (The pattern Evan flagged: 'reported failed, then
    hit the fallback and succeeded' must not exist.)"""
    import subprocess

    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv, 0, stdout='{"success": true}', stderr=""
        )

    optimization_calls = []
    monkeypatch.setattr("cron.scheduler.subprocess.run", fake_run)
    monkeypatch.setattr(
        "cron.scheduler._in_process_wake",
        lambda *a, **k: optimization_calls.append(a) or False,
    )
    from cron.scheduler import fire_cron_wake

    assert fire_cron_wake("gopher", "short prompt") is True

    # Exactly one delivery attempt: the official CLI wake.  The fragile
    # in-process path was never consulted.
    assert calls == [["hermes", "send", "-t", "gopher", "short prompt"]]
    assert optimization_calls == []


def test_cli_fail_and_no_candidate_is_honest_failure(monkeypatch):
    """If the official path cannot confirm AND no optimization is
    verifiable, the wake fails honestly — never a false green."""
    import subprocess

    monkeypatch.setattr(
        "cron.scheduler.subprocess.run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom"),
    )
    from cron.scheduler import fire_cron_wake

    # No adapters, no loop: the optimization is not a candidate.
    assert fire_cron_wake("neo", "prompt", adapters=None, loop=None) is False


def test_in_process_rejection_is_not_false_green(monkeypatch):
    """If the official path fails AND the last-resort optimization rejects,
    never report success."""
    import subprocess

    class RejectingAdapter:
        async def handle_message(self, event):
            raise RuntimeError("live session is wedged")

    loop = _live_loop()
    try:
        monkeypatch.setattr(
            "cron.scheduler.subprocess.run",
            lambda argv, **kw: subprocess.CompletedProcess(
                argv, 1, stdout="", stderr="boom"
            ),
        )
        from cron.scheduler import fire_cron_wake

        assert fire_cron_wake("neo", "prompt", adapters={"telegram": RejectingAdapter()}, loop=loop) is False
    finally:
        loop.call_soon_threadsafe(loop.stop)


def test_optimization_never_precedes_official_wake(monkeypatch):
    """The exact pattern Evan flagged must not exist: the in-process
    optimization must never be attempted, fail, and then be rescued by the
    CLI wake.  When adapters+loop are present the CLI wake still runs
    first and still succeeds on its own — the optimization is untouched."""
    import subprocess

    class RejectingAdapter:
        name = "RejectingAdapter"

        async def handle_message(self, event):
            raise RuntimeError("no live session for this target")

    adapters = {"telegram": RejectingAdapter()}
    cli_calls = []
    optimization_calls = []

    def fake_run(argv, **kw):
        cli_calls.append(argv)
        return subprocess.CompletedProcess(
            argv, 0, stdout='{"success": true}', stderr=""
        )

    loop = _live_loop()
    try:
        monkeypatch.setattr("cron.scheduler.subprocess.run", fake_run)
        monkeypatch.setattr(
            "cron.scheduler._in_process_wake",
            lambda *a, **k: optimization_calls.append(a) or False,
        )
        from cron.scheduler import fire_cron_wake

        ok = fire_cron_wake("neo", "prompt", adapters=adapters, loop=loop)
        assert ok is True
        assert cli_calls == [["hermes", "send", "-t", "neo", "prompt"]]
        # The fragile path was never consulted — no failed first attempt,
        # no fallback chain.
        assert optimization_calls == []
    finally:
        loop.call_soon_threadsafe(loop.stop)


def test_fire_cron_wake_refuses_empty_target(monkeypatch):
    """Never fire into the void — an empty target is a hard error before any
    delivery site, so no path can send to nothing."""
    delivered = {"to": None}

    def spy(target, body, **kw):
        delivered["to"] = target
        return False

    monkeypatch.setattr("cron.scheduler._bridge_cli_wake", spy)
    monkeypatch.setattr(
        "cron.scheduler._in_process_wake", lambda target, body, **kw: False
    )
    from cron.scheduler import fire_cron_wake

    assert fire_cron_wake("", "prompt") is False
    assert fire_cron_wake(None, "prompt") is False
    assert delivered["to"] is None


def test_wake_target_shapes_resolve():
    from cron.scheduler import _resolve_wake_platform_and_chat

    assert _resolve_wake_platform_and_chat("gopher") == ("telegram", "8900123006")
    assert _resolve_wake_platform_and_chat("telegram:8900123006") == (
        "telegram",
        "8900123006",
    )
    assert _resolve_wake_platform_and_chat("gopher:telegram:8900123006") == (
        "telegram",
        "8900123006",
    )
    assert _resolve_wake_platform_and_chat("discord:my-channel") == (
        "discord",
        "my-channel",
    )
    assert _resolve_wake_platform_and_chat("gopher:discord:chan:7") == (
        "discord",
        "chan",
    )


def test_wake_target_shapes_reject_garbage():
    from cron.scheduler import _resolve_wake_platform_and_chat

    assert _resolve_wake_platform_and_chat("") == (None, None)
    assert _resolve_wake_platform_and_chat(None) == (None, None)
    assert _resolve_wake_platform_and_chat("bogus:maybe:chat") == (None, None)
    assert _resolve_wake_platform_and_chat("telegram:") == (None, None)

def _install_dispatch_mocks(monkeypatch):
    """Wire _dispatch_cron_wake's collaborators to spies; return the record dict."""
    rec = {
        "fired": [],
        "marks": [],
        "finish": [],
        "wake_bodies": [],
        "prompt": "assembled prompt",
    }

    monkeypatch.setattr("cron.scheduler._build_wake_payload", lambda job: rec["prompt"])
    monkeypatch.setattr(
        "cron.scheduler.fire_cron_wake",
        lambda target, prompt, **kw: (
            rec["fired"].append((target, prompt, kw.get("body"), kw.get("adapters"))),
            True,
        )[-1],
    )
    # mark_job_run / finish_execution are imported into cron.scheduler's
    # namespace (scheduler.py:286-287) — patch the scheduler binding so the
    # spies intercept instead of touching the real jobs store.
    monkeypatch.setattr(
        "cron.scheduler.mark_job_run",
        lambda job_id, success, error=None, delivery_error=None, status=None: rec[
            "marks"
        ].append((job_id, success, error, status)),
    )
    monkeypatch.setattr(
        "cron.scheduler.finish_execution",
        lambda eid, **kw: rec["finish"].append((eid, kw)),
    )
    return rec


def test_dispatch_fires_wake_and_records_status(monkeypatch):
    rec = _install_dispatch_mocks(monkeypatch)
    from cron.scheduler import WAKE_FIRED_STATUS, CronWakeRoute, _dispatch_cron_wake

    job = {"id": "jid1", "skills": ["neo-journal"]}
    route = CronWakeRoute(mode="wake", target="neo", reason="journal")
    _dispatch_cron_wake(job, route, "exec-1", adapters=None, loop=None)

    # Wake fired exactly once, carrying the SHORT wake body (never the full
    # assembled brief — t_94b593b6).
    assert len(rec["fired"]) == 1
    target, prompt, body, adapters = rec["fired"][0]
    assert target == "neo"
    assert prompt == "assembled prompt"
    assert body == prompt
    # last_status = "wake fired", success, no framed delivery (suppressed).
    assert rec["marks"] == [("jid1", True, None, WAKE_FIRED_STATUS)]
    assert rec["finish"][0][0] == "exec-1"
    assert rec["finish"][0][1]["delivery_outcome"] == "suppressed"
    # Threads adapters/loop through to the wake site (in-process injection
    # becomes possible) instead of a detached spawn.
    assert adapters is None


def test_dispatch_records_error_when_wake_fails(monkeypatch):
    """A wake that cannot fire is an honest failure — NO silent fall back to spawn."""
    rec = _install_dispatch_mocks(monkeypatch)
    monkeypatch.setattr("cron.scheduler.fire_cron_wake", lambda target, prompt, **kw: False)
    from cron.scheduler import CronWakeRoute, _dispatch_cron_wake

    job = {"id": "jid3", "skills": ["neo-journal"]}
    route = CronWakeRoute(mode="wake", target="neo", reason="journal")
    _dispatch_cron_wake(job, route, "exec-3", adapters=None, loop=None)

    # Failure recorded; no spawn fallback marker present, and the status stays
    # the default 'error' (not 'wake fired') because nothing was dispatched.
    assert rec["fired"] == []
    assert rec["marks"] == [("jid3", False, rec["marks"][0][2], None)]
    assert rec["marks"][0][1] is False
    assert rec["marks"][0][3] is None


# ---------------------------------------------------------------------------
# Wake payload discipline (t_94b593b6) — short wakes, never the full brief
# ---------------------------------------------------------------------------

def test_wake_payload_never_inlines_skills():
    """A journal job's wake carries names, not the 106K-char skill embed."""
    from cron.scheduler import _build_wake_payload, _wake_max_chars

    job = {
        "id": "wj",
        "name": "Journals",
        "prompt": "run the weekly journal",
        "skills": ["gopher-journal", "hermes-cli"],
    }
    body = _build_wake_payload(job)
    assert "gopher-journal" in body  # named, for the agent to load itself
    assert len(body) << 5000
    assert len(body) <= _wake_max_chars()


def test_wake_payload_truncates_overdraft_prompts():
    """An absurdly over-long prompt is capped with a visible tail marker."""
    from cron.scheduler import _build_wake_payload, _wake_max_chars

    job = {
        "id": "wj",
        "name": "huge",
        "prompt": "A" * (_wake_max_chars() + 5000),
        "skills": [],
    }
    body = _build_wake_payload(job)
    assert len(body) <= _wake_max_chars()
    assert "truncated" in body
    assert "...[wake payload truncated from" in body


def test_dispatch_threads_adapters_to_wake(monkeypatch):
    """t_94b593b6: adapters/loop reach the wake site so the last-resort
    in-process injection is available when the official CLI wake cannot
    confirm."""
    rec = _install_dispatch_mocks(monkeypatch)
    from cron.scheduler import CronWakeRoute, _dispatch_cron_wake

    fake_adapters = {"telegram": object()}
    loop = object()
    job = {"id": "jid-thread", "skills": ["neo-journal"]}
    route = CronWakeRoute(mode="wake", target="neo", reason="journal")
    _dispatch_cron_wake(job, route, "exec-t", adapters=fake_adapters, loop=loop)

    target, prompt, body, adapters = rec["fired"][0]
    assert adapters is fake_adapters


# ---------------------------------------------------------------------------
# last_status override on mark_job_run (gap 5)
# ---------------------------------------------------------------------------

def test_mark_job_run_writes_wake_fired_status(tmp_path):
    from cron.jobs import load_jobs, mark_job_run, use_cron_store

    with use_cron_store(tmp_path):
        # Seed one job into the isolated store.
        import json

        cron_dir = tmp_path / "cron"
        cron_dir.mkdir(parents=True, exist_ok=True)
        (cron_dir / "jobs.json").write_text(
            json.dumps({"jobs": [{"id": "wj1", "name": "j", "schedule": {"kind": "once"}}]}),
            encoding="utf-8",
        )
        mark_job_run("wj1", True, None, status="wake fired")
        jobs = load_jobs()
        assert jobs[0]["last_status"] == "wake fired"


def test_mark_job_run_default_status_unchanged(tmp_path):
    from cron.jobs import load_jobs, mark_job_run, use_cron_store

    with use_cron_store(tmp_path):
        import json

        cron_dir = tmp_path / "cron"
        cron_dir.mkdir(parents=True, exist_ok=True)
        (cron_dir / "jobs.json").write_text(
            json.dumps({"jobs": [{"id": "wj2", "name": "j", "schedule": {"kind": "once"}}]}),
            encoding="utf-8",
        )
        mark_job_run("wj2", True, None)
        mark_job_run("wj2", False, "boom")
        jobs = load_jobs()
        # Last call wins: error status preserved when no override is given.
        assert jobs[0]["last_status"] == "error"
