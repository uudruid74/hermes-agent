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
REQUIRED_SECTIONS = ("Symptom", "Repro", "Suspected cause")
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
}


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
    path = Path(value).expanduser().resolve()
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


def task_body(path: Path, text: str, directive: Optional[str] = None) -> str:
    parts = [f"Bug file: {path}"]
    for section in (*REQUIRED_SECTIONS, "Failure Reports"):
        value = section_value(text, section)
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
    append_dispatch_record(identity, "reserved")
    slug = path.stem.split("-", 3)[-1]
    result = subprocess.run(
        [HERMES, "kanban", "create", "--assignee", "neo", "--body", task_body(path, text, directive), f"BUG: {slug}", "--json"],
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
    atomic_write_text(path, add_task_id(text, created))
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
    if live_task_ids(text):
        return None
    if failure_count(text) >= 4 and not force:
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
    with locked_root():
        if path.exists():
            die(f"bug already exists: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# {args.title}\n\n"
            "## Symptom\n\n\n"
            "## Repro\n\n\n"
            "## Suspected cause\n\n\n"
            "## Kanban tasks\n\n\n"
            "## Resolution\n\n\n"
            "## Failure Reports\n\n",
            encoding="utf-8",
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


def cmd_check(_args: argparse.Namespace) -> None:
    pending = [path for path in all_bug_files() if path.parent.name == "pending"]
    for path in pending:
        with locked_root():
            fresh = path.read_text(encoding="utf-8")
            identity = dispatch_identity(path, fresh, None)
            if dispatch_is_recorded(identity):
                record = latest_dispatch_record(identity)
                task_id = record.get("task_id") if record else None
                print(f"{path}: dispatched={task_id or 'reserved'} failures={failure_count(fresh)}/4")
                continue
            live = live_task_ids(fresh)
            state = f"live={','.join(live) if live else 'none'} failures={failure_count(fresh)}/4"
            print(f"{path}: {state}")
            if required_complete(fresh) and not live and failure_count(fresh) < 4:
                maybe_dispatch_locked(path, fresh)
            elif failure_count(fresh) >= 4:
                print(f"manual redispatch required: {path}")


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
    new.set_defaults(func=cmd_new)
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
    resolve = commands.add_parser("resolve")
    resolve.add_argument("file")
    resolve.add_argument("summary")
    resolve.set_defaults(func=cmd_resolve)
    redispatch = commands.add_parser("redispatch")
    redispatch.add_argument("file")
    redispatch.add_argument("--directive", required=True)
    redispatch.set_defaults(func=cmd_redispatch)
    check = commands.add_parser("check")
    check.set_defaults(func=cmd_check)
    listing = commands.add_parser("list")
    listing.add_argument("project", nargs="?")
    listing.set_defaults(func=cmd_list)
    search = commands.add_parser("search")
    search.add_argument("keywords", nargs="+")
    search.set_defaults(func=cmd_search)
    return root


def main() -> None:
    args = parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
