#!/usr/bin/env python3
"""bugtoold — vault bug-file watcher and dispatch daemon (t_cd44bbde, Phases C+C2).

Replaces Evan's four-step manual chain (check box -> close Obsidian -> commit ->
run bugtool). The daemon:

  1. watches ONLY wiki/Projects/**/bugs/**/*.md under the vault (never the
     whole vault — Obsidian churns .obsidian/workspace.json on every panel
     move, which would make any quiescence window meaningless);
  2. waits for QUIESCENCE (default 60s, Evan's number) with no further change;
  3. force-commits the dirty WATCHED PATHS ONLY — pathspec-limited `git commit
     -- <paths>`, NEVER `git add -A` (the vault is routinely dirty with agent
     WIP; c8f6e50 on 2026-10-04 swept ~2,968 unrelated lines under a
     test-shaped message). Untracked watched files are `git add`-ed
     individually first, still pathspec-only;
  4. runs the Qdrant injector (same command as the post-commit hook),
  5. scans pending bugs and, for each checked one, invokes the REAL
     `bugtool dispatch <file>` so the six gates in maybe_dispatch_locked
     decide. This daemon never reimplements or bypasses them.

Bounds (Phase C2 — this is the only component that can spawn paid workers
autonomously, same power class as the duplicate-worker incident):
  - kill switch: touch <state-dir>/KILLSWITCH -> the daemon idles (no commit,
    no injector, no dispatch) until the file is removed;
  - max dispatches per hour (default 4) counted from the decision log;
  - decision log <state-dir>/decision-log.jsonl recording every dispatch AND
    every skip with its reason;
  - systemd user unit shipped at scripts/bugtoold.service (Restart=on-failure,
    starts at login/boot via default.target; `loginctl enable-linger ekl`
    makes it start at boot without a login).

Backends: `watchfiles` is used when importable (event-driven, filtered to the
bugs glob); otherwise a stdlib polling fallback (default here — raw ctypes
inotify is a documented failure mode). Behavioural settings (quiescence,
poll interval, hourly cap) live in config.yaml under a `bugtoold:` key —
deliberately NOT HERMES_* env vars (spec non-goal).

Verified safety properties: no live dispatch in tests; all dispatches go
through the bugtool CLI; dry-run mode logs decisions without side effects.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_VAULT = Path("/home/ekl/vault")
DEFAULT_STATE_DIR = Path("/home/ekl/.local/state/bugtoold")
DEFAULT_CONFIG = Path("/home/ekl/.hermes/config.yaml")
BUGS_GLOB = "wiki/Projects/**/bugs/**/*.md"
KILLSWITCH_NAME = "KILLSWITCH"
DECISION_LOG_NAME = "decision-log.jsonl"

DEFAULTS = {
    "quiescence_seconds": 60.0,
    "poll_interval_seconds": 5.0,
    "max_dispatches_per_hour": 4,
}


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{stamp} bugtoold: {message}", flush=True)


def load_settings(config_path: Path) -> dict:
    """Read bugtoold settings from config.yaml (`bugtoold:` key). No env vars.

    Missing file/section or missing pyyaml -> defaults (a daemon must start
    bounded even when config is absent). Bad values keep the default.
    """
    settings = dict(DEFAULTS)
    try:
        import yaml  # type: ignore
    except ImportError:
        print("bugtoold: pyyaml unavailable; using built-in defaults", file=sys.stderr)
        return settings
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # malformed yaml must not kill the daemon
        print(f"bugtoold: config unreadable ({exc}); using defaults", file=sys.stderr)
        return settings
    section = data.get("bugtoold") if isinstance(data, dict) else None
    if not isinstance(section, dict):
        return settings
    for key, default in DEFAULTS.items():
        if key in section:
            try:
                value = float(section[key]) if "seconds" in key else int(section[key])
                if value <= 0:
                    raise ValueError("must be positive")
                settings[key] = value
            except (TypeError, ValueError) as exc:
                print(
                    f"bugtoold: config bugtoold.{key}={section[key]!r} invalid ({exc}); "
                    f"keeping default {default}",
                    file=sys.stderr,
                )
    return settings


class Debouncer:
    """Quiescence gate: ready() only after `quiescence_seconds` of silence."""

    def __init__(self, quiescence_seconds: float) -> None:
        self.quiescence_seconds = quiescence_seconds
        self.last_change: float | None = None

    def mark(self, now: float | None = None) -> None:
        self.last_change = time.monotonic() if now is None else now

    def ready(self, now: float | None = None) -> bool:
        if self.last_change is None:
            return False
        now = time.monotonic() if now is None else now
        return (now - self.last_change) >= self.quiescence_seconds

    def reset(self) -> None:
        self.last_change = None


def watched_files(vault: Path) -> list[Path]:
    return sorted(vault.glob(BUGS_GLOB))


def poll_changes(vault: Path, signature: dict[Path, tuple]) -> tuple[set[Path], dict[Path, tuple]]:
    """Return (changed paths, new signature). Additions/edits/removals all count:
    a bug RESOLUTION is a pending/ file disappearing — exactly the trigger the
    dependency gate needs."""
    current: dict[Path, tuple] = {}
    for path in watched_files(vault):
        try:
            stat = path.stat()
        except OSError:
            continue
        current[path] = (stat.st_mtime_ns, stat.st_size)
    changed = {
        path
        for path in set(current) | set(signature)
        if current.get(path) != signature.get(path)
    }
    return changed, current


def dirty_watched_paths(vault: Path) -> list[tuple[Path, bool]]:
    """(path, is_untracked) for watched paths dirty in the vault repo.

    Reads `git status --porcelain` but keeps ONLY paths matching the watch
    glob — the vault is routinely dirty elsewhere and must stay untouched.
    """
    result = subprocess.run(
        ["git", "-C", str(vault), "status", "--porcelain", "--", BUGS_GLOB],
        text=True, capture_output=True, check=False,
    )
    dirty: list[tuple[Path, bool]] = []
    if result.returncode != 0:
        log(f"git status failed: {(result.stderr or result.stdout).strip()[:200]}")
        return dirty
    for line in (result.stdout or "").splitlines():
        if len(line) < 4:
            continue
        status, relpath = line[:2], line[3:].strip().strip('"')
        if relpath.endswith("/"):
            continue
        path = vault / relpath
        if path.suffix != ".md":
            continue
        dirty.append((path, status.strip() == "??"))
    return dirty


def commit_watched(vault: Path, dirty: list[tuple[Path, bool]], dry_run: bool) -> bool:
    """Pathspec-limited commit of the dirty watched files. NEVER `git add -A`."""
    if not dirty:
        return False
    tracked = [str(path) for path, untracked in dirty if not untracked]
    untracked = [str(path) for path, untracked in dirty if untracked]
    if dry_run:
        log(f"dry-run: would commit {len(dirty)} watched file(s): "
            + ", ".join(str(p.name) for p, _ in dirty))
        return True
    if untracked:
        result = subprocess.run(
            ["git", "-C", str(vault), "add", "--", *untracked],
            text=True, capture_output=True, check=False,
        )
        if result.returncode:
            log(f"git add (watched untracked only) failed: {(result.stderr or result.stdout).strip()[:200]}")
            return False
    paths = [str(path) for path, _ in dirty]
    message = "bugtoold: auto-commit watched bug file changes (" + str(len(paths)) + " file(s))"
    result = subprocess.run(
        ["git", "-C", str(vault), "commit", "-m", message, "--", *paths],
        text=True, capture_output=True, check=False,
    )
    if result.returncode:
        # "nothing to commit" race (file reverted between status and commit) is benign.
        output = (result.stdout or "") + (result.stderr or "")
        if "nothing to commit" not in output:
            log(f"git commit (pathspec-limited) failed: {output.strip()[:300]}")
            return False
        return False
    log(f"committed {len(paths)} watched file(s): " + ", ".join(p.name for p, _ in dirty))
    return True


def run_injector(vault: Path, dry_run: bool) -> None:
    """Same injector the post-commit hook runs; non-fatal by design."""
    injector = Path("/home/ekl/memory-os/scripts/wiki_ingest_from_git.py")
    if not injector.is_file():
        log(f"injector missing ({injector}); skipping (non-fatal)")
        return
    if dry_run:
        log("dry-run: would run injector")
        return
    result = subprocess.run(
        [sys.executable, str(injector), "--commit-ref", "HEAD", "--vault-repo", str(vault)],
        text=True, capture_output=True, check=False,
    )
    if result.returncode:
        log(f"injector failed (non-fatal): {(result.stderr or result.stdout).strip()[:200]}")


def load_bugtool_module():
    """Fresh import of scripts/bugtool.py (env-respected per import)."""
    script = Path(__file__).resolve().parent / "bugtool.py"
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        f"bugtool_daemon_{time.monotonic_ns()}", script
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DecisionLog:
    def __init__(self, path: Path, dry_run: bool) -> None:
        self.path = path
        self.dry_run = dry_run

    def append(self, action: str, **fields) -> None:
        entry = {"ts": datetime.now(timezone.utc).isoformat(), "action": action, **fields}
        if self.dry_run:
            entry["dry_run"] = True
        log(action + ": " + json.dumps(fields, ensure_ascii=False))
        if self.dry_run:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def dispatches_last_hour(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        if not self.path.is_file():
            return 0
        count = 0
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return 0
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("action") != "dispatch":
                continue
            try:
                ts = datetime.fromisoformat(record["ts"]).timestamp()
            except (KeyError, ValueError):
                continue
            if now - ts <= 3600:
                count += 1
        return count


def scan_and_dispatch(vault: Path, settings: dict, decisions: DecisionLog, dry_run: bool) -> int:
    """One scan pass. Read-only gate introspection here (for skip REASONS);
    the actual dispatch always goes through `bugtool dispatch` (the gates in
    maybe_dispatch_locked remain authoritative). Returns dispatches made."""
    bugtool = load_bugtool_module()
    projects_root = vault / "wiki" / "Projects"
    dispatched = 0
    hourly_cap = int(settings["max_dispatches_per_hour"])
    for path in watched_files(vault):
        if path.parent.name != "pending" or not path.is_file():
            continue
        name = path.name
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            decisions.append("skip", bug=name, reason=f"unreadable: {exc}")
            continue
        if bugtool.bug_status(text) == bugtool.DISPATCHED_STATUS:
            decisions.append("skip", bug=name, reason="already dispatched (status marker)")
            continue
        if not bugtool.approved_to_run(text):
            decisions.append("skip", bug=name, reason="approval checkbox unchecked")
            continue
        missing = bugtool.missing_dispatch_fields(text)
        if missing:
            decisions.append("skip", bug=name, reason="incomplete: " + "; ".join(missing))
            continue
        worker = bugtool.assigned_worker(text)
        if not worker:
            decisions.append("skip", bug=name, reason="no valid assignee")
            continue
        if bugtool.failure_count(text) >= 4:
            decisions.append("skip", bug=name, reason="awaiting escalation (4+ failures)")
            continue
        try:
            blocked_by = bugtool.dependency_gate(text)
        except SystemExit as exc:
            decisions.append("skip", bug=name, reason=f"dependency error: {exc}")
            continue
        if blocked_by:
            decisions.append("skip", bug=name, reason="blocked by " + ", ".join(blocked_by))
            continue
        if bugtool.owned_live_task_ids(text):
            decisions.append("skip", bug=name, reason="live task exists")
            continue
        if bugtool.live_task_count_by_agent(worker) >= bugtool.MAX_LIVE_TASKS_PER_AGENT:
            decisions.append("skip", bug=name, reason=f"agent busy (cap: {worker})")
            continue
        if dispatched + decisions.dispatches_last_hour() >= hourly_cap:
            decisions.append(
                "skip", bug=name,
                reason=f"hourly cap reached ({hourly_cap} dispatches in the last hour)",
            )
            continue
        if dry_run:
            decisions.append("skip", bug=name, reason="dry-run: dispatch suppressed")
            continue
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent / "bugtool.py"),
             "dispatch", str(path)],
            text=True, capture_output=True, check=False,
            env={**os.environ, "BUGTOOL_PROJECTS_ROOT": str(projects_root)},
        )
        output = (result.stdout or "") + (result.stderr or "")
        if result.returncode == 0 and "dispatched t_" in output:
            task_id = next(
                (token for token in output.split() if token.startswith("t_")), "?"
            )
            decisions.append("dispatch", bug=name, task=task_id, worker=worker)
            dispatched += 1
        else:
            decisions.append(
                "skip", bug=name,
                reason=f"bugtool dispatch refused: {output.strip()[:200]}",
            )
    return dispatched


def cycle(vault: Path, settings: dict, decisions: DecisionLog, killswitch: Path,
          dry_run: bool, killswitch_logged: list[bool]) -> None:
    if killswitch.is_file():
        if not killswitch_logged[0]:
            decisions.append("skip", reason="kill switch engaged (remove to re-enable)")
            killswitch_logged[0] = True
        return
    killswitch_logged[0] = False
    dirty = dirty_watched_paths(vault)
    committed = commit_watched(vault, dirty, dry_run)
    if committed:
        run_injector(vault, dry_run)
    scan_and_dispatch(vault, settings, decisions, dry_run)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--once", action="store_true",
                        help="run one scan cycle and exit (no watch loop)")
    parser.add_argument("--dry-run", action="store_true",
                        help="log decisions without committing or dispatching")
    args = parser.parse_args(argv)

    settings = load_settings(args.config)
    decisions = DecisionLog(args.state_dir / DECISION_LOG_NAME, args.dry_run)
    killswitch = args.state_dir / KILLSWITCH_NAME
    vault = args.vault.resolve()
    killswitch_logged = [False]

    if args.once:
        cycle(vault, settings, decisions, killswitch, args.dry_run, killswitch_logged)
        return 0

    log(
        f"watching {BUGS_GLOB} under {vault} (quiescence "
        f"{settings['quiescence_seconds']}s, poll {settings['poll_interval_seconds']}s, "
        f"max {settings['max_dispatches_per_hour']} dispatches/hour, "
        f"kill switch {killswitch})"
    )
    debouncer = Debouncer(settings["quiescence_seconds"])
    signature: dict[Path, tuple] = {}
    try:
        try:
            import watchfiles  # type: ignore
        except ImportError:
            watchfiles = None
        poll_every = settings["poll_interval_seconds"]
        if watchfiles is not None:
            log("backend: watchfiles (event-driven)")
            bug_dirs = sorted({p.parent for p in watched_files(vault) if p.parent.is_dir()})

            def event_stream():
                # rust_timeout makes the generator yield periodically even
                # with no events, so next() below cannot block forever.
                yield from watchfiles.watch(
                    *(bug_dirs or [vault]),
                    rust_timeout=int(poll_every * 1000),
                )

            stream = event_stream()
            while True:
                try:
                    _changes, paths = next(stream)
                except StopIteration:
                    stream = event_stream()
                    continue
                for candidate in paths or ():
                    if str(candidate).endswith(".md"):
                        debouncer.mark()
                if debouncer.ready():
                    debouncer.reset()
                    cycle(vault, settings, decisions, killswitch, args.dry_run, killswitch_logged)
        else:
            log("backend: polling (watchfiles not installed)")
            while True:
                changed, signature = poll_changes(vault, signature)
                if changed:
                    debouncer.mark()
                time.sleep(poll_every)
                if debouncer.ready():
                    debouncer.reset()
                    cycle(vault, settings, decisions, killswitch, args.dry_run, killswitch_logged)
    except KeyboardInterrupt:
        log("stopped (SIGINT)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
