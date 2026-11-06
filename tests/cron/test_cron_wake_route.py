"""Cron wake dispatch: journal -> `hermes send -t` into a live gateway session (t_f961f13b).

Gap 5 — a journal cron fires a `hermes send -t` wake into the target agent's
live gateway DM session instead of spawning a short-lived cron session that
hard-closes at turn-end (the motivating bug: a later tell / pente reply landed
on a dead session). The wake is the single delivery channel — framed delivery
is suppressed (deliver='local' semantics). Routing is EXCLUSIVE (wake XOR spawn)
and only journals wake today (Evan ruling #8 -> DM; ruling #4 -> empty topic -> DM
is the only fallback). Journal marker is the structured ``skills`` entry ending
``-journal`` (Gopher, gap 5) — not prompt free-text, not a script name.
"""
from __future__ import annotations

import pytest


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

def test_fire_cron_wake_dispatches_hermes_send(monkeypatch):
    calls = {}

    def fake_spawn(argv):
        calls["argv"] = argv
        return object()  # non-None = spawn succeeded

    monkeypatch.setattr("hermes_cli._subprocess_compat.spawn_detached", fake_spawn)
    from cron.scheduler import fire_cron_wake

    ok = fire_cron_wake("neo", "the journal prompt")
    assert ok is True
    assert calls["argv"][0] == "hermes"
    assert calls["argv"][1:4] == ["send", "-t", "neo"]
    assert "the journal prompt" in calls["argv"]


def test_fire_cron_wake_refuses_empty_target(monkeypatch):
    """Never fire into the void — an empty target is an error, not a send."""
    called = []
    monkeypatch.setattr(
        "hermes_cli._subprocess_compat.spawn_detached",
        lambda argv: called.append(argv) or object(),
    )
    from cron.scheduler import fire_cron_wake

    assert fire_cron_wake("", "prompt") is False
    assert fire_cron_wake(None, "prompt") is False
    assert called == []


def test_fire_cron_wake_reports_spawn_failure(monkeypatch):
    monkeypatch.setattr("hermes_cli._subprocess_compat.spawn_detached", lambda argv: None)
    from cron.scheduler import fire_cron_wake

    assert fire_cron_wake("neo", "prompt") is False


# ---------------------------------------------------------------------------
# Dispatch orchestration — fires the wake, records 'wake fired', no spawn/fallback
# ---------------------------------------------------------------------------

def _install_dispatch_mocks(monkeypatch):
    """Wire _dispatch_cron_wake's collaborators to spies; return the record dict."""
    rec = {"fired": [], "marks": [], "finish": [], "prompt": "assembled prompt"}

    monkeypatch.setattr("cron.scheduler._build_job_prompt", lambda job: rec["prompt"])
    monkeypatch.setattr(
        "cron.scheduler.fire_cron_wake",
        lambda target, prompt: rec["fired"].append((target, prompt)) or True,
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
    _dispatch_cron_wake(job, route, "exec-1")

    # Wake fired exactly once, carrying the prompt.
    assert rec["fired"] == [("neo", "assembled prompt")]
    # last_status = "wake fired", success, no framed delivery (suppressed).
    assert rec["marks"] == [("jid1", True, None, WAKE_FIRED_STATUS)]
    assert rec["finish"][0][0] == "exec-1"
    assert rec["finish"][0][1]["delivery_outcome"] == "suppressed"


def test_dispatch_no_wake_when_script_output_empty(monkeypatch):
    """A silent run (no content) fires no wake and records a quiet success."""
    rec = _install_dispatch_mocks(monkeypatch)
    monkeypatch.setattr("cron.scheduler._build_job_prompt", lambda job: None)
    from cron.scheduler import CronWakeRoute, _dispatch_cron_wake

    job = {"id": "jid2", "skills": ["neo-journal"]}
    route = CronWakeRoute(mode="wake", target="neo", reason="journal")
    _dispatch_cron_wake(job, route, "exec-2")

    assert rec["fired"] == []
    # success + default status (not "wake fired") — nothing was dispatched.
    assert rec["marks"] == [("jid2", True, None, None)]


def test_dispatch_records_error_when_wake_fails(monkeypatch):
    """A wake that cannot fire is an honest failure — NO silent fall back to spawn."""
    rec = _install_dispatch_mocks(monkeypatch)
    monkeypatch.setattr("cron.scheduler.fire_cron_wake", lambda target, prompt: False)
    from cron.scheduler import CronWakeRoute, _dispatch_cron_wake

    job = {"id": "jid3", "skills": ["neo-journal"]}
    route = CronWakeRoute(mode="wake", target="neo", reason="journal")
    _dispatch_cron_wake(job, route, "exec-3")

    # Failure recorded; no spawn fallback marker present, and the status stays
    # the default 'error' (not 'wake fired') because nothing was dispatched.
    assert rec["fired"] == []
    assert rec["marks"][0][1] is False
    assert rec["marks"][0][3] is None


def test_dispatch_injection_block_is_fenced(monkeypatch):
    """A blocked prompt never fires a wake (injection fence holds on the wake path)."""
    rec = _install_dispatch_mocks(monkeypatch)
    from cron.scheduler import CronPromptInjectionBlocked, CronWakeRoute, _dispatch_cron_wake

    def boom(job):
        raise CronPromptInjectionBlocked("rm -rf /")

    monkeypatch.setattr("cron.scheduler._build_job_prompt", boom)
    job = {"id": "jid4", "skills": ["neo-journal"]}
    route = CronWakeRoute(mode="wake", target="neo", reason="journal")
    _dispatch_cron_wake(job, route, "exec-4")

    assert rec["fired"] == []
    assert rec["marks"][0][1] is False  # recorded as a failure, not silently dropped


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
