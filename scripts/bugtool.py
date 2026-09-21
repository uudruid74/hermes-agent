#!/usr/bin/env python3
"""Central wiki-backed bug tracking for the Hermes fleet (stdlib only)."""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

PROJECTS_ROOT = Path(os.environ.get("BUGTOOL_PROJECTS_ROOT", "/home/ekl/vault/wiki/Projects"))
STATE_ROOT = Path(os.environ.get("BUGTOOL_STATE_ROOT", "/home/ekl/.local/state/bugtool"))
DISPATCH_LOG = STATE_ROOT / "bugs-dispatched.log"
HERMES = os.environ.get("BUGTOOL_HERMES", "hermes")
LIVE_STATUSES = {"todo", "ready", "running", "blocked", "scheduled"}
def tags_from_args(args: argparse.Namespace) -> list[str]:
    """Parse comma-separated --tags into a YAML list; empty when omitted."""
    raw = getattr(args, "tags", "") or ""
    return [t.strip() for t in raw.split(",") if t.strip()]


REQUIRED_SECTIONS = ("Description", "How To Reproduce", "Actual Behavior")
ASSIGNEE_SECTION = "Assignee"
APPROVED_SECTION = "Approved to run"
# Valid fleet workers a bug can be assigned to. Dispatch requires an assignee
# from this set AND an explicit human-checked approval box (see approved_to_run).
VALID_ASSIGNEES = {"neo", "ornith", "gopher", "wintermute", "zephyr"}
SECTION_ALIASES = {
    "symptom": "Symptom",
    "repro": "Repro",
    "cause": "Suspected cause",
    "suspected-cause": "Suspected cause",
    "suspected cause": "Suspected cause",
    "kanban-tasks": "Kanban tasks",
    "kanban tasks": "Kanban tasks",
    "failure-reports": "Failure Reports",
    "failure reports": "Failure Reports",
    "resolution": "Resolution",
    # Optional grouping metadata (2026-09-16, Evan): ONE task, MANY bugs. The
    # primary bug gets its own task; its `## Related bugs` section lists the
    # other bug files to fold into the SAME piece of work. This replaces the old
    # multi-file `check` sweep, which created one task per bug.
    "related-bugs": "Related bugs",
    "related bugs": "Related bugs",
}
RELATED_SECTION = "Related bugs"


@contextmanager
def locked_root() -> Iterator[None]:
    """Serialize filesystem state transitions across all bug files."""
    PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    lock_path = STATE_ROOT / ".bugtool.lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_text(path: Path, text: str) -> None:
    """Replace a text file and durably persist both data and directory entry."""
    mode = path.stat().st_mode & 0o777
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    replaced = False
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        replaced = True
        fsync_directory(path.parent)
    finally:
        if not replaced:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def redispatch_directive(text: str, directive: Optional[str]) -> str:
    if directive is not None:
        return directive.strip()
    values = re.findall(
        r"(?ms)^## Redispatch directive\n(.*?)(?=^## |\Z)", text
    )
    return values[-1].strip() if values else ""


def dispatch_identity(path: Path, text: str, directive: Optional[str]) -> dict[str, object]:
    return {
        "bug": str(path.resolve()),
        "attempt": failure_count(text),
        "directive": redispatch_directive(text, directive),
    }


def append_dispatch_record(
    identity: dict[str, object], status: str, task_id: Optional[str] = None
) -> None:
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    new_log = not DISPATCH_LOG.exists()
    record = {
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        **identity,
        "status": status,
        "task_id": task_id,
    }
    fd = os.open(DISPATCH_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        payload = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
        while payload:
            written = os.write(fd, payload)
            if written == 0:
                raise OSError("short write to dispatch log")
            payload = payload[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    if new_log:
        fsync_directory(STATE_ROOT)


def latest_dispatch_record(identity: dict[str, object]) -> Optional[dict[str, object]]:
    if not DISPATCH_LOG.exists():
        return None
    latest = None
    with DISPATCH_LOG.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"invalid dispatch log line {number}: {exc}") from exc
            if all(record.get(key) == value for key, value in identity.items()):
                latest = record
    return latest


def dispatch_is_recorded(identity: dict[str, object]) -> bool:
    return latest_dispatch_record(identity) is not None


def die(message: str) -> None:
    raise SystemExit(f"bugtool: {message}")


def normalize_slug(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value):
        die("slug must use lowercase letters, digits, and single hyphens")
    return value


def normalize_project(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
        die("project must be a single safe directory name")
    return value


def bug_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        # Relative paths from the git hook resolve against the vault root,
        # not the hook's unpredictable cwd.
        path = Path("/home/ekl/vault") / path
    path = path.resolve()
    root = PROJECTS_ROOT.resolve()
    if root not in path.parents:
        die(f"bug file must be under {root}")
    if not path.is_file():
        die(f"bug file does not exist: {path}")
    return path


def all_bug_files(project: Optional[str] = None) -> list[Path]:
    roots = [PROJECTS_ROOT / normalize_project(project)] if project else sorted(PROJECTS_ROOT.iterdir()) if PROJECTS_ROOT.exists() else []
    files: list[Path] = []
    for root in roots:
        for status in ("pending", "resolved"):
            directory = root / "bugs" / status
            if directory.exists():
                files.extend(sorted(directory.glob("*.md")))
    return sorted(files)


def section_name(value: str) -> str:
    return SECTION_ALIASES.get(value.strip().lower(), value.strip())


def section_value(text: str, section: str) -> str:
    match = re.search(rf"(?ms)^## {re.escape(section)}\n(.*?)(?=^## |\Z)", text)
    return match.group(1).strip() if match else ""


def replace_section(text: str, section: str, value: str) -> str:
    pattern = rf"(?ms)^## {re.escape(section)}\n.*?(?=^## |\Z)"
    replacement = f"## {section}\n\n{value.strip()}\n\n"
    if not re.search(pattern, text):
        return text.rstrip() + "\n\n" + replacement
    return re.sub(pattern, replacement, text, count=1)


def assigned_worker(text: str) -> Optional[str]:
    """Return the assignee from frontmatter (canonical) or the Assignee section."""
    fm = frontmatter(text)
    value = (fm.get("assignee") or section_value(text, ASSIGNEE_SECTION)).strip()
    if not value:
        return None
    worker = value.split()[0].strip().strip("`*_").lower()
    return worker if worker in VALID_ASSIGNEES else None


_SESSION_MISSING_MARKER = "MISSING-SESSION-ID (bugtool: HERMES_SESSION_ID env was empty at file time — fix dispatch will reject)"


def reporter_session(text: str) -> str:
    """Session id from frontmatter (canonical) or legacy Reporter session section."""
    fm = frontmatter(text)
    if fm.get("session"):
        return fm["session"]
    return section_value(text, "Reporter session").strip()


def frontmatter(text: str) -> dict:
    """Parse YAML-ish frontmatter into a dict (flat key: value strings)."""
    if not text.startswith("---"):
        return {}
    try:
        end = text.index("\n---", 3)
    except ValueError:
        return {}
    fm = {}
    for line in text[4:end].splitlines():
        if ":" in line and not line.startswith((" ", "-")):
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip().strip('"')
    return fm


def _fmt_list(items):
    return "tags: [" + ", ".join('"' + i + '"' for i in items) + "]"


def frontmatter_block(**fields) -> str:
    """Render YAML-ish frontmatter. tags is a converter, not a passthrough:
    - list/tuple  -> ["a", "b"]   (proper quoted array)
    - scalar str  -> "value"      (single value, never an array literal)
    - bracketed str like "[\"a\", \"b\"]" -> parsed into the proper list.
    This guarantees tags are never emitted as "["tag"...]" outside quotes."""
    lines = ["---"]
    for k, v in fields.items():
        if isinstance(v, (list, tuple)):
            items = [f'"{str(x).strip()}"' for x in v]
            lines.append(f'{k}: [{", ".join(items)}]')
        elif isinstance(v, str):
            stripped = v.strip()
            # Recover a bracketed array literal into a real list.
            if len(stripped) >= 2 and stripped[0] == "[" and stripped[-1] == "]":
                inner = stripped[1:-1].strip()
                items = [p.strip().strip('"') for p in inner.split(",")] if inner else []
                lines.append(_fmt_list(items))
            else:
                lines.append(f'{k}: "{stripped}"')
        else:
            lines.append(f'{k}: "{v}"')
    lines.append("---")
    return "\n".join(lines) + "\n"


def fix_tags_line(path: Path, text: str) -> tuple[str, bool]:
    """Repair a scalar-string tags line in place. Returns (new_text, changed)."""
    for number, raw in enumerate(text.splitlines(), start=1):
        if not raw.startswith("tags:") or ":" not in raw:
            continue
        value = raw[len("tags:"):].strip()
        if len(value) >= 2 and value[0] == '[' and value[-1] == ']':
            inner = value[1:-1].strip()
            items = [p.strip().strip('"') for p in inner.split(",")] if inner else []
            fixed = _fmt_list(items)
            if fixed == 'tags: ["bugtool"]':
                # Placeholder with no real tag content -> empty list.
                fixed = "tags: []"
        elif len(value) >= 2 and value[0] == '"':
            inner = value.strip().strip('"').strip()
            items = [p.strip().strip('"') for p in inner.split(",")] if inner else []
            fixed = _fmt_list(items)
        elif not value:
            fixed = "tags: []"
        else:
            # A lone scalar tag like "temperature-bug" -> single-item list.
            fixed = _fmt_list([value])
        if fixed != raw:
            return text.replace(raw, fixed, 1), True
    return text, False


def cmd_fix_tags(args: argparse.Namespace) -> None:
    """Repair broken tags frontmatter in existing bug files (converter into the tool)."""
    for path in all_bug_files(args.project):
        changed = False
        with locked_root():
            text = path.read_text(encoding="utf-8")
            new_text, did_change = fix_tags_line(path, text)
            if did_change:
                path.write_text(new_text, encoding="utf-8")
                changed = True
        print(f"{'fixed ' if changed else 'ok    '} {path}")


def origin_channel(text: str) -> Optional[str]:
    """Return a session channel only when the bug names a valid reporter session."""
    reporter = reporter_session(text).strip()
    if re.fullmatch(r"\d{8}_\d{6}_[0-9a-f]{6}", reporter):
        return f"session:{reporter}"
    return None


def _refresh_origin(text: str, path: Path) -> None:
    """Overwrite the live task's origin routing with THIS dispatcher's session.

    Called when a dispatch attempt hits the live-task dedup gate (2026-09-21,
    Evan): the current dispatcher's session must win over a stale original,
    or notifications keep routing to a channel that can never receive them.
    Best-effort: failures are logged, never raise.
    """
    channel = origin_channel(text)
    if not channel:
        return
    session_id = channel.split(":", 1)[1]
    task_ids_owned = owned_live_task_ids(text)
    if not task_ids_owned:
        return
    try:
        import hermes_cli.kanban_db as kb
        with kb.connect_closing() as conn:
            for task_id in task_ids_owned:
                kb.store_origin_routing(
                    conn,
                    task_id,
                    platform="session",
                    chat_id=session_id,
                    profile=(os.environ.get("USERNAME") or "").strip(),
                    overwrite=True,
                )
    except Exception as exc:  # best-effort: dispatch dedup still returns None
        print(f"origin refresh failed for {path.name}: {exc}", file=sys.stderr)


def missing_dispatch_fields(text: str) -> list[str]:
    """Human-readable list of what blocks dispatch (debugging + manual runs)."""
    missing = [s for s in REQUIRED_SECTIONS if not section_value(text, s)]
    if not assigned_worker(text):
        missing.append("Assignee (must be a valid fleet agent)")

    if not approved_to_run(text):
        missing.append("Approved to run (checkbox must be checked by Evan)")
    return missing


def approved_to_run(text: str) -> bool:
    """Approval gate: the Assigned section must contain a CHECKED box (- [x]).
    An unchecked box (- [ ]) means the human has NOT approved dispatch."""
    value = section_value(text, APPROVED_SECTION)
    return bool(re.search(r"- \[[xX]\]", value))


def required_complete(text: str) -> bool:
    return all(section_value(text, section) for section in REQUIRED_SECTIONS)


def task_ids(text: str) -> list[str]:
    return re.findall(r"\bt_[A-Za-z0-9]+\b", section_value(text, "Kanban tasks"))


def failure_count(text: str) -> int:
    return len(re.findall(r"^### Failure Report \(task t_[A-Za-z0-9]+\)", text, flags=re.MULTILINE))


def status_for_task(task_id: str) -> Optional[str]:
    result = subprocess.run(
        [HERMES, "kanban", "show", task_id], text=True, capture_output=True, check=False,
    )
    output = result.stdout + result.stderr
    match = re.search(r"(?mi)^\s*status:\s*([a-z]+)\b", output)
    return match.group(1).lower() if match else None


def live_task_ids(text: str) -> list[str]:
    return [task_id for task_id in task_ids(text) if status_for_task(task_id) in LIVE_STATUSES]


def owned_live_task_ids(text: str) -> list[str]:
    """Live tasks this bug OWNS (created for it), not ones it merely references.

    `## Kanban tasks` legitimately holds two different things:
    - entries THIS bug's own dispatch created, written bare as ``- t_xxxx``
      (see add_task_id), and
    - hand-written *references* to related tasks, which carry an annotation
      (e.g. ``- t_related — running; this bug follows it``) -- see
      #3df9b2c04 "ignore referenced live tasks".

    Only the bare form counts as "already dispatched"; a reference must never
    block this bug from getting its own task. A task that is already done or
    archived is not live, so a legitimate re-dispatch still works.
    """
    owned = []
    for line in section_value(text, "Kanban tasks").splitlines():
        match = re.match(r"^\s*[-*]\s*(\bt_[A-Za-z0-9]+\b)\s*$", line)
        if not match:
            continue
        task_id = match.group(1)
        if status_for_task(task_id) in LIVE_STATUSES:
            owned.append(task_id)
    return owned


DISPATCHED_STATUS = "dispatched"


def bug_status(text: str) -> str:
    """Return the bug file's own frontmatter status ('' when absent)."""
    return frontmatter(text).get("status", "").strip().lower()


def is_dispatched_marker(text: str, path: Path) -> bool:
    """True when this bug file's metadata says a task was already created.

    THE "already dispatched?" CHECK (2026-09-14, Evan): "just change the
    meta-data from 'pending' to 'dispatched' and do not dispatch a dispatched
    bug." Status lives in the file itself (vault, git-tracked, survives across
    machines and DB rebuilds) instead of being inferred by string-matching task
    bodies against a board listing.
    """
    status = bug_status(text)
    if status == DISPATCHED_STATUS:
        return True
    # NOTE: deliberately NOT falling back to "a task id appears under
    # `## Kanban tasks`". That section also legitimately holds *references* to
    # other tasks (see live_task_ids / #3df9b2c04, "ignore referenced live
    # tasks"), and treating any reference as "already dispatched" would block
    # dispatch for a bug that only mentions a related task. Evan's rule is
    # status-only: flip `pending` -> `dispatched` and refuse a dispatched bug.
    return False


def set_bug_status(text: str, status: str) -> str:
    """Rewrite the frontmatter `status:` line, leaving the rest untouched."""
    return re.sub(r"(?m)^status:.*$", f"status: {status}", text, count=1)


def task_body(path: Path, text: str, directive: Optional[str] = None) -> str:
    parts = [f"Bug file: {path}"]
    for section in (*REQUIRED_SECTIONS, ASSIGNEE_SECTION, APPROVED_SECTION, RELATED_SECTION, "Failure Reports"):
        value = section_value(text, section)
        if section == RELATED_SECTION and not value:
            # Optional grouping metadata: omit the heading entirely when absent
            # so every pre-existing bug body keeps its exact old shape.
            continue
        parts.extend((f"## {section}", value or "(none)"))
    if directive:
        parts.extend(("## Redispatch directive", directive))
    return "\n\n".join(parts)


def create_task(
    path: Path,
    text: str,
    identity: dict[str, object],
    directive: Optional[str] = None,
) -> Optional[str]:
    worker = assigned_worker(text)
    if not worker:
        # HARD FAILURE (2026-09-13, Evan): never fall back to a default
        # assignee. A bug with no Assignee must error, not silently
        # materialize as a task assigned to someone.
        append_dispatch_record(identity, "failed")
        print(
            "dispatch failed for %s: bug file has no Assignee — "
            "refusing to assign", path,
            file=sys.stderr,
        )
        return None
    append_dispatch_record(identity, "reserved")
    slug = path.stem.split("-", 3)[-1]
    command = [
        HERMES, "kanban", "create", "--assignee", worker,
        "--body", task_body(path, text, directive), f"BUG: {slug}",
    ]
    channel = origin_channel(text)
    if channel:
        command.extend(("--channel", channel))
    command.append("--json")
    result = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
    )
    output = result.stdout + result.stderr
    created = None
    if not result.returncode:
        try:
            response = json.loads(result.stdout)
        except json.JSONDecodeError:
            response = None
        if isinstance(response, dict):
            candidate = response.get("id")
            if isinstance(candidate, str) and re.fullmatch(r"t_[A-Za-z0-9]+", candidate):
                created = candidate
    if created is None:
        append_dispatch_record(identity, "failed")
        print(f"dispatch failed for {path}: {output.strip()}", file=sys.stderr)
        return None
    append_dispatch_record(identity, "created", created)
    # Flip the bug's own metadata to `dispatched` in the SAME locked write that
    # records the task id, so the marker can never lag behind the task.
    atomic_write_text(path, set_bug_status(add_task_id(text, created), DISPATCHED_STATUS))
    return created


def add_task_id(text: str, task_id: str) -> str:
    current = section_value(text, "Kanban tasks")
    if task_id in task_ids(text):
        return text
    entry = f"- {task_id}"
    return replace_section(text, "Kanban tasks", "\n".join(filter(None, (current, entry))))


def maybe_dispatch_locked(path: Path, text: str, directive: Optional[str] = None, force: bool = False) -> Optional[str]:
    identity = dispatch_identity(path, text, directive)
    if dispatch_is_recorded(identity):
        return None
    if not required_complete(text):
        return None
    worker = assigned_worker(text)
    if not worker:
        return None  # no valid Assignee: never dispatch
    if not approved_to_run(text):
        return None  # human approval checkbox unchecked: never dispatch
    if failure_count(text) >= 4 and not force:
        return None
    # The board is the second durable marker (with the file's own status):
    # a bug that already OWNS a live task must never get a second one. This is
    # the actual spew guard -- `is_dispatched_marker` below is status-only by
    # design, so without this a file left at status=pending (hand-edited, or
    # dispatched before the marker existed) re-creates its task every pass.
    # Only BARE entries count (bare = this bug's own task, written by
    # add_task_id); annotated hand-written references to related tasks do not
    # block dispatch (#3df9b2c04), and done/archived tasks are not live, so a
    # legitimate re-dispatch still works.
    if not force and owned_live_task_ids(text):
        # Re-dispatch with a NEW origin (2026-09-21, Evan): the live task stays,
        # but the origin routing must point at the CURRENT dispatcher's session,
        # not the original filer's (possibly dead) session. First-wins otherwise
        # pins notifications to a channel that can never receive them.
        _refresh_origin(text, path)
        print(f"nothing to dispatch for {path.name}: live task exists (origin refreshed)")
        return None
    # AUTHORITATIVE idempotency gate (2026-09-14, Evan): "just change the
    # meta-data from 'pending' to 'dispatched' and do not dispatch a dispatched
    # bug." The dispatch log is per-machine state that may be absent (it did not
    # exist before 2026-09-07, which is how 28 duplicates were created on
    # 2026-09-05). The file's own status is the durable marker — vault-tracked,
    # git-tracked, machine-independent, no board string matching.
    #
    # `force=True` is `bugtool redispatch`, the DELIBERATE revive path (see the
    # kanban skill: re-dispatching an archived/revoked bug). It bypasses this
    # gate on purpose and is the only way to intentionally re-create a task for
    # a bug that is already marked dispatched.
    if not force and is_dispatched_marker(text, path):
        print(f"nothing to dispatch for {path.name}: already dispatched")
        return None
    created = create_task(path, text, identity, directive)
    if created:
        print(f"dispatched {created}: {path}")
    return created


def escalate(path: Path, text: str) -> None:
    summaries = re.findall(r"^### Failure Report \(task (t_[A-Za-z0-9]+)\)\n\n(.+?)(?=^### |\Z)", text, flags=re.MULTILINE | re.DOTALL)
    details = "; ".join(f"{task}: {summary.strip().splitlines()[0]}" for task, summary in summaries)
    message = f"Bug auto-dispatch stopped after 4 failures: {path}. {details}"
    result = subprocess.run([HERMES, "send", "-t", "evan", message], text=True, capture_output=True, check=False)
    if result.returncode:
        print(f"escalation command failed: {(result.stderr or result.stdout).strip()}", file=sys.stderr)
    else:
        print(f"escalated to Evan: {path}")


def cmd_new(args: argparse.Namespace) -> None:
    project = normalize_project(args.project)
    slug = normalize_slug(args.slug)
    date = dt.date.fromisoformat(os.environ.get("BUGTOOL_DATE", dt.date.today().isoformat()))
    path = PROJECTS_ROOT / project / "bugs" / "pending" / f"{date.isoformat()}-{slug}.md"
    agent_name = os.environ.get("USERNAME", "").strip() or "unknown"
    # Session resolution, ground-truth-first:
    # 1. HERMES_SESSION_KEY env (always present in gateway tool shells,
    #    exact sessions.session_key format — the durable handle the CLI uses).
    # 2. HERMES_SESSION_ID env (legacy direct id, CLI/cron paths).
    # 3. state.db lookup by session_key, else most-recent-active for agent.
    session = ""
    session_key = os.environ.get("HERMES_SESSION_KEY", "").strip()
    if not session:
        session = os.environ.get("HERMES_SESSION_ID", "").strip()
    if not session and session_key:
        import glob as _glob
        for prof_dir in sorted(_glob.glob("/home/ekl/.hermes/profiles/*/state.db")):
            try:
                import sqlite3 as _sq
                conn = _sq.connect(prof_dir)
                row = conn.execute(
                    "SELECT id FROM sessions WHERE session_key = ? AND archived = 0 "
                    "ORDER BY last_activity_at DESC LIMIT 1",
                    (session_key,),
                ).fetchone()
                conn.close()
                if row and row[0]:
                    session = row[0]
                    break
            except Exception:
                continue
    if not session and agent_name != "unknown":
        import glob as _glob
        for prof_dir in sorted(_glob.glob(f"/home/ekl/.hermes/profiles/{agent_name.lower()}/state.db")):
            try:
                import sqlite3 as _sq
                conn = _sq.connect(prof_dir)
                row = conn.execute(
                    "SELECT id FROM sessions WHERE archived = 0 ORDER BY last_activity_at DESC LIMIT 1"
                ).fetchone()
                conn.close()
                if row and row[0]:
                    session = row[0]
                    break
            except Exception:
                continue
    with locked_root():
        if path.exists():
            die(f"bug already exists: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            frontmatter_block(
                title=args.title,
                type="bug",
                status="pending",
                date=date.isoformat(),
                severity=args.severity,
                filed_by=agent_name,
                assignee=args.assignee,
                session=session,
                tags=tags_from_args(args),
                summary="",
            )
            + f"\n# {args.title}\n\n"
            "## Description\n\n\n"
            "## How To Reproduce\n\n\n"
            "## Expected Behavior\n\n\n"
            "## Actual Behavior\n\n\n"
            "## Comments\n\n\n"
            "## Supporting Evidence\n\n\n"
            "## Approved to run\n\n- [ ] approved by Evan (check this box ONLY after reviewing the fix spec)\n"
            "## Kanban tasks\n\n",
            encoding="utf-8",
        )
    # Auto-commit the new bug file so it lands in vault history immediately
    # (the post-commit hook then runs the wiki injector + dispatch check on it).
    rel = os.path.relpath(path, "/home/ekl/vault")
    subprocess.run(
        ["git", "-C", "/home/ekl/vault", "add", rel],
        capture_output=True, check=False,
    )
    subprocess.run(
        ["git", "-C", "/home/ekl/vault", "commit", "-m",
         f"bug: {args.title} (filed by {agent_name})"],
        capture_output=True, check=False,
    )
    print(path)


def cmd_set(args: argparse.Namespace) -> None:
    path = bug_path(args.file)
    section = section_name(args.section)
    with locked_root():
        text = path.read_text(encoding="utf-8")
        updated = replace_section(text, section, args.text)
        path.write_text(updated, encoding="utf-8")
        created = maybe_dispatch_locked(path, updated)
    print(f"set {section}: {path}")
    if not created and required_complete(updated):
        print("dispatch deferred: a live task exists or four failures require manual redispatch")


def cmd_link_task(args: argparse.Namespace) -> None:
    if not re.fullmatch(r"t_[A-Za-z0-9]+", args.task_id):
        die("task id must look like t_<id>")
    path = bug_path(args.file)
    with locked_root():
        text = path.read_text(encoding="utf-8")
        path.write_text(add_task_id(text, args.task_id), encoding="utf-8")
    print(f"linked {args.task_id}: {path}")


def cmd_append_failure(args: argparse.Namespace) -> None:
    if not re.fullmatch(r"t_[A-Za-z0-9]+", args.task_id):
        die("task id must look like t_<id>")
    path = bug_path(args.file)
    entry = f"### Failure Report (task {args.task_id})\n\n{args.summary.strip()}\n"
    with locked_root():
        text = path.read_text(encoding="utf-8")
        if "## Failure Reports" not in text:
            die("bug file has no Failure Reports section")
        failure_header = text.rfind("## Failure Reports")
        later_section = re.search(r"(?m)^## (?!#)", text[failure_header + len("## Failure Reports"):])
        if later_section:
            entry = "\n## Failure Reports\n\n" + entry
        fd = os.open(path, os.O_WRONLY | os.O_APPEND)
        try:
            os.write(fd, ("\n" + entry).encode("utf-8"))
        finally:
            os.close(fd)
        updated = path.read_text(encoding="utf-8")
        attempts = failure_count(updated)
        if attempts >= 4:
            escalate(path, updated)
        else:
            maybe_dispatch_locked(path, updated)
    print(f"failure {attempts}/4 appended: {path}")


def cmd_update(args: argparse.Namespace) -> None:
    """Append new information to a bug's ## Updates section + auto-commit."""
    path = bug_path(args.file)
    agent_name = os.environ.get("USERNAME", "").strip() or "unknown"
    entry = f"### Update ({dt.datetime.now().strftime('%Y-%m-%d %H:%M')}, {agent_name})\n\n{args.note.strip()}\n"
    with locked_root():
        text = path.read_text(encoding="utf-8")
        if "## Updates" not in text:
            if "## Resolution" in text:
                text = text.replace("## Resolution", "## Updates\n\n\n## Resolution", 1)
            else:
                text += "\n## Updates\n\n\n"
        idx = text.index("## Updates")
        nxt = re.search(r"(?m)^## (?!#)", text[idx + len("## Updates"):])
        insert_at = idx + len("## Updates") + (nxt.start() if nxt else len(text[idx + len("## Updates"):]))
        text = text[:insert_at] + "\n" + entry + text[insert_at:]
        path.write_text(text, encoding="utf-8")
        rel = os.path.relpath(path, "/home/ekl/vault")
        subprocess.run(["git", "-C", "/home/ekl/vault", "add", rel], capture_output=True, check=False)
        subprocess.run(["git", "-C", "/home/ekl/vault", "commit", "-m",
                        f"bug: update {path.stem} ({agent_name})"],
                       capture_output=True, check=False)
    print(f"update appended: {path}")


def cmd_resolve(args: argparse.Namespace) -> None:
    path = bug_path(args.file)
    if path.parent.name != "pending":
        die("only pending bugs can be resolved")
    target = path.parent.parent / "resolved" / path.name
    with locked_root():
        if target.exists():
            die(f"resolved file already exists: {target}")
        text = replace_section(path.read_text(encoding="utf-8"), "Resolution", args.summary)
        path.write_text(text, encoding="utf-8")
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(path, target)
    print(f"resolved: {target}")


def cmd_redispatch(args: argparse.Namespace) -> None:
    path = bug_path(args.file)
    with locked_root():
        text = path.read_text(encoding="utf-8")
        updated = text.rstrip() + f"\n\n## Redispatch directive\n\n{args.directive.strip()}\n"
        path.write_text(updated, encoding="utf-8")
        created = maybe_dispatch_locked(path, updated, args.directive, force=True)
    if not created:
        die("redispatch did not create a task; required sections may be incomplete or a task is live")


def cmd_dispatch(args: argparse.Namespace) -> None:
    """Check gate fields for one bug file; dispatch if approved, else report blockers."""
    path = bug_path(args.file)
    if path.parent.name != "pending":
        print(f"nothing to dispatch for {path.name}: only pending/ bugs dispatch "
              f"(this file is in {path.parent.name}/ — already resolved or archived)")
        raise SystemExit(0)
    with locked_root():
        text = path.read_text(encoding="utf-8")
        missing = missing_dispatch_fields(text)
        if missing:
            print(f"cannot dispatch {path.name}: missing/incomplete: " + "; ".join(missing))
            raise SystemExit(1)
        created = maybe_dispatch_locked(path, text)
    if created:
        print(f"dispatched {created} -> {path.name}")
    else:
        print(f"nothing to dispatch for {path.name}: live task exists or already dispatched")


def cmd_check_and_dispatch(args: argparse.Namespace) -> None:
    """Approve ONE bug (check its box) and dispatch it -- in a single step.

    WRITES: this stamps the `## Approved to run` checkbox and creates a kanban
    task. It is the deliberate approval action, not a status query. Reading bug
    state is `bugtool list` / `bugtool search`, or just read the file; directory
    (`pending/` vs `resolved/`) is the status.

    Scoped to the ONE file named on the command line. The previous `check`
    command additionally swept every pending bug and dispatched each
    gate-complete one, so a single invocation could spawn several paid workers;
    that sweep is gone. Grouping multiple bugs into one piece of work is now
    expressed as data -- the primary bug's `## Related bugs` section -- and
    travels as one task body.
    """
    if not getattr(args, "i_am_evan", False):
        die(
            "check-and-dispatch refuses to run without --i-am-evan.\n"
            "This command writes an APPROVAL on Evan's behalf and dispatches a\n"
            "worker. Agents: do not pass this flag. If a bug needs approval, ask\n"
            "Evan -- he checks the box or runs this himself."
        )
    if not getattr(args, "file", ""):
        die("check-and-dispatch requires a bug file (one bug per invocation)")

    path = bug_path(args.file)
    if path.parent.name != "pending":
        print(f"nothing to dispatch for {path.name}: only pending/ bugs dispatch "
              f"(this file is in {path.parent.name}/ — already resolved or archived)")
        raise SystemExit(0)

    with locked_root():
        text = path.read_text(encoding="utf-8")
        updated = replace_section(text, APPROVED_SECTION, "- [x] approved by Evan\n")
        atomic_write_text(path, updated)
        created = maybe_dispatch_locked(path, updated)

    print(f"approved {path}")
    if created:
        print(f"dispatched {created} -> {path.name}")
        related = section_value(updated, RELATED_SECTION)
        if related:
            print(f"related bugs folded into {created}:")
            for line in related.splitlines():
                if line.strip():
                    print(f"  {line.strip()}")
    else:
        missing = missing_dispatch_fields(updated)
        if missing:
            print("dispatch blocked: missing/incomplete: " + "; ".join(missing))
        elif is_dispatched_marker(updated, path):
            print("nothing to dispatch: already dispatched")
        else:
            print("dispatch deferred: a live task exists or four failures require manual redispatch")


def cmd_list(args: argparse.Namespace) -> None:
    for path in all_bug_files(args.project):
        print(f"{path.parent.name}\t{failure_count(path.read_text(encoding='utf-8'))}/4\t{path}")


def cmd_search(args: argparse.Namespace) -> None:
    query = " ".join(args.keywords).lower()
    hits = 0
    for path in all_bug_files():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if query in line.lower():
                print(f"{path}:{number}: {line}")
                hits += 1
    if not hits:
        raise SystemExit(1)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="bugtool")
    commands = root.add_subparsers(dest="command", required=True)
    new = commands.add_parser("new")
    new.add_argument("project")
    new.add_argument("slug")
    new.add_argument("title")
    new.add_argument("--assignee", default="")
    new.add_argument("--tags", default="")
    new.add_argument("--severity", default="normal", choices=["normal", "high"])
    new.set_defaults(func=cmd_new)
    fix = commands.add_parser("fix-tags", help="repair broken tags frontmatter in existing bug files")
    fix.add_argument("project", nargs="?")
    fix.set_defaults(func=cmd_fix_tags)
    set_cmd = commands.add_parser("set")
    set_cmd.add_argument("file")
    set_cmd.add_argument("section")
    set_cmd.add_argument("text")
    set_cmd.set_defaults(func=cmd_set)
    link = commands.add_parser("link-task")
    link.add_argument("file")
    link.add_argument("task_id")
    link.set_defaults(func=cmd_link_task)
    failure = commands.add_parser("append-failure")
    failure.add_argument("file")
    failure.add_argument("task_id")
    failure.add_argument("summary")
    failure.set_defaults(func=cmd_append_failure)
    upd = commands.add_parser("update")
    upd.add_argument("file")
    upd.add_argument("note")
    upd.set_defaults(func=cmd_update)
    resolve = commands.add_parser("resolve")
    resolve.add_argument("file")
    resolve.add_argument("summary")
    resolve.set_defaults(func=cmd_resolve)
    redispatch = commands.add_parser("redispatch")
    redispatch.add_argument("file")
    redispatch.add_argument("--directive", required=True)
    redispatch.set_defaults(func=cmd_redispatch)
    dispatch = commands.add_parser(
        "dispatch",
        help="gate-check ONE bug file and dispatch it if Evan already approved it (read-only on approval state)",
    )
    dispatch.add_argument("file")
    dispatch.set_defaults(func=cmd_dispatch)
    check = commands.add_parser(
        "check-and-dispatch",
        help=(
            "WRITES: Evan-only. Checks the approval box on ONE bug AND dispatches it "
            "(requires --i-am-evan). Agents must not run this — use `dispatch`, "
            "`list`, `search`, or read the file."
        ),
    )
    check.add_argument("file", nargs="?", default="",
                       help="the ONE bug file to approve and dispatch")
    check.add_argument("--i-am-evan", dest="i_am_evan", action="store_true",
                       help="required confirmation that Evan is performing this approval himself")
    check.set_defaults(func=cmd_check_and_dispatch)
    listing = commands.add_parser(
        "list", help="READ-ONLY: per-file status + failure counts (use this to inspect bug state)"
    )
    listing.add_argument("project", nargs="?")
    listing.set_defaults(func=cmd_list)
    search = commands.add_parser(
        "search", help="READ-ONLY: find bugs by content"
    )
    search.add_argument("keywords", nargs="+")
    search.set_defaults(func=cmd_search)
    return root


def main() -> None:
    args = parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
