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
# Cap-counting statuses: a BLOCKED task is not consuming the agent's attention
# (it sits waiting for a dependency), so it must not count toward the
# per-agent dispatch cap (Evan 2026-10-14).
CAP_COUNT_STATUSES = {"todo", "ready", "running", "scheduled"}
def tags_from_args(args: argparse.Namespace) -> list[str]:
    """Parse comma-separated --tags into a YAML list; empty when omitted."""
    raw = getattr(args, "tags", "") or ""
    return [t.strip() for t in raw.split(",") if t.strip()]


REQUIRED_SECTIONS = ("Description", "How To Reproduce", "Actual Behavior")
ASSIGNEE_SECTION = "Assignee"
APPROVED_SECTION = "Approved to run"
# Dependency metadata (t_cd44bbde, Phase A1): a `## Depends on` section lists
# bug slugs this bug cannot dispatch before they RESOLVE (i.e. the slugs no
# longer live in bugs/pending/). Resolution is itself a watched change, so the
# daemon's next scan picks the dependent up without a second mechanism.
DEPENDS_SECTION = "Depends on"
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
    # Dependency metadata (t_cd44bbde, Phase A1): a `## Depends on` section lists
    # bug slugs this bug cannot dispatch before they RESOLVE. `bugtool set`
    # accepts the alias forms; the canonical heading is DEPENDS_SECTION.
    "depends-on": DEPENDS_SECTION,
    "depends on": DEPENDS_SECTION,
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
    identity: dict[str, object], status: str, task_id: Optional[str] = None,
    ambiguous: bool = False,
) -> None:
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    new_log = not DISPATCH_LOG.exists()
    record = {
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        **identity,
        "status": status,
        "task_id": task_id,
    }
    if ambiguous:
        # A create attempt whose side effect could not be confirmed (2026-11-17):
        # see dispatch_is_recorded — such records block auto-retry.
        record["ambiguous"] = True
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
    """True when a prior dispatch attempt for this identity must block retry.

    Definite failures (status 'failed' WITHOUT the ambiguous marker) do NOT
    count: a rejected CLI flag (argparse, exit 2) never reached the backend
    and must not permanently block redispatch (2026-09-21: the --channel
    argparse failure deadlocked every retry of the notify-silent-fail bug).
    But an AMBIGUOUS failure (2026-11-17) — the request may have created a
    task we never read back, e.g. "connection lost after request" — DOES
    count: auto-retrying it is the duplicate-spew class. `bugtool redispatch`
    (force=True) revives both.
    """
    record = latest_dispatch_record(identity)
    if record is None:
        return False
    if record.get("status") in ("reserved", "created"):
        return True
    return record.get("status") == "failed" and bool(record.get("ambiguous"))


def die(message: str) -> None:
    raise SystemExit(f"bugtool: {message}")


def normalize_slug(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value):
        die("slug must use lowercase letters, digits, and single hyphens")
    return value


def normalize_project(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
        die("project must be a single safe directory name")
    root = PROJECTS_ROOT / value
    if not root.is_dir():
        known = sorted(p.name for p in PROJECTS_ROOT.iterdir() if p.is_dir()) if PROJECTS_ROOT.exists() else []
        die(f"project does not exist: {value} (known project dirs: {', '.join(known) or 'none'})")
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
                for p in sorted(directory.glob("*.md")):
                    # Navigation/boilerplate pages are not bug files (2026-11-16):
                    # lint/repair must never inject approval sections into them.
                    if p.name in ("Home.md", "INDEX.md"):
                        continue
                    files.append(p)
    return sorted(files)


def section_name(value: str) -> str:
    return SECTION_ALIASES.get(value.strip().lower(), value.strip())


def section_value(text: str, section: str) -> str:
    # Exact heading, or heading with a parenthetical/annotation suffix
    # (e.g. "## How To Reproduce (current behaviour — the gap)") — the
    # dispatch gate must not fail files whose authors qualified the title.
    match = re.search(
        rf"(?m)^## {re.escape(section)}[ \t]*(?:[（(—-][^\n]*)?\n(?P<body>.*?)(?=\n^## |\n?\Z)",
        text,
        re.S,
    )
    return match.group("body").strip() if match else ""


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


REQUIRED_FRONTMATTER_KEYS = ("title", "type", "status", "date", "severity", "filed_by", "assignee", "session", "tags", "summary")
# Legacy/alias frontmatter keys seen in free-handed bug files; lint migrates
# these to canonical names rather than just flagging them (Evan 2026-11-16:
# "fix all the bugs missing a damn checkbox" — silent drift like
# `created:`/`reporter:` instead of `date:`/`filed_by:` is exactly the damage).
FRONTMATTER_ALIASES = {
    "created": "date",
    "reporter": "filed_by",
    "author": "filed_by",
    "author_profile": "filed_by",
    "assigned": "assignee",
    "sev": "severity",
    "kind": "type",
}
APPROVED_CHECKBOX_LINE = "- [ ] approved by Evan (check this box ONLY after reviewing the fix spec)"
CHECKED_BOX = "- [x] approved by Evan"


def frontmatter_raw(text: str) -> dict[str, str]:
    """Parse frontmatter keeping raw key names (frontmatter() also strips values).

    Handles block-form values (`tags:\n  - x`) — a key with no inline value
    followed by indented list items is non-empty (2026-10-05 fix-tags
    block-form bug class; lint must not false-positive "empty tags" on it).
    """
    if not text.startswith("---"):
        return {}
    try:
        end = text.index("\n---", 3)
    except ValueError:
        return {}
    fm: dict[str, str] = {}
    lines = text[4:end].splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if ":" in line and not line.startswith((" ", "-", "\t")):
            k, _, v = line.partition(":")
            key = k.strip()
            val = v.strip().strip('"')
            i += 1
            # Consume block-form continuation lines (indented list items).
            if not val:
                block: list[str] = []
                while i < len(lines) and (
                    lines[i].startswith((" ", "\t"))
                ):
                    block.append(lines[i].strip())
                    i += 1
                if block:
                    fm[key] = " ".join(block)
                    continue
            fm[key] = val
        else:
            i += 1
    return fm


def replace_frontmatter_key(text: str, old: str, new: str) -> str:
    """Rename a frontmatter key in place, preserving order."""
    pattern = rf"(?m)^{re.escape(old)}:(.*)$"
    return re.sub(pattern, rf"{new}:\1", text, count=1)


def bug_lint_issues(text: str, path: Path) -> tuple[list[str], list[str]]:
    """Return (auto_fixable, manual) issue lists for one bug file.

    Auto-fixable: things bugtool can safely repair without inventing facts
    (alias-key migration, missing approval SECTION with an UNCHECKED box —
    agents may add an unchecked box; only Evan ever ticks a box).
    Manual: things that need a human/forensics input (missing author, missing
    date, no frontmatter at all) — repair mode backfills them only when the
    value is supplied via CLI flags.
    """
    auto: list[str] = []
    manual: list[str] = []
    if not text.lstrip().startswith("---"):
        manual.append("no frontmatter block")
        # Still lint the body for the approval section even without frontmatter.
        if not re.search(rf"(?m)^## {re.escape(APPROVED_SECTION)}\s*$", text):
            manual.append(f"missing `## {APPROVED_SECTION}` section (no frontmatter; needs author info anyway)")
        return auto, manual
    raw = frontmatter_raw(text)
    for legacy, canonical in FRONTMATTER_ALIASES.items():
        if legacy in raw and canonical not in raw:
            auto.append(f"non-canonical key `{legacy}:` -> `{canonical}:`")
    canonical = {FRONTMATTER_ALIASES.get(k, k): v for k, v in raw.items()}
    # Resolved bugs are archive records: routing fields (session/assignee) are
    # only required while a bug is live (pending) — backfilling them
    # post-hoc would be fabrication, not repair (2026-11-16).
    is_resolved = "resolved" in path.parts or canonical.get("status", "").lower() == "resolved"
    required = REQUIRED_FRONTMATTER_KEYS if not is_resolved else tuple(
        k for k in REQUIRED_FRONTMATTER_KEYS if k not in ("session", "assignee")
    )
    for key in required:
        value = canonical.get(key, "").strip()
        if not value:
            if key == "tags":
                auto.append("empty `tags:` — repair will emit `tags: []`")
            elif key == "summary":
                auto.append("empty `summary:` — repair will leave empty (cosmetic)")
            elif key == "title":
                manual.append("missing `title:` (needs value from CLI flag or heading)")
            else:
                manual.append(f"missing/empty `{key}:` (needs value from CLI flag or forensics)")
    if not re.search(rf"(?m)^## {re.escape(APPROVED_SECTION)}\s*$", text):
        auto.append(f"missing `## {APPROVED_SECTION}` section — repair inserts UNCHECKED box (only Evan ever ticks a box)")
    else:
        section = section_value(text, APPROVED_SECTION)
        if not re.search(r"- \[[ xX]\]", section):
            auto.append(f"`## {APPROVED_SECTION}` section has no checkbox line — repair inserts UNCHECKED box")

    # 5. Dispatch-gate body sections (2026-11-16): a bug file without
    # Description/How To Reproduce/Actual Behavior (or fix-spec equivalents)
    # passes frontmatter lint yet is UNDISPATCHABLE — bugtoold skips it
    # forever with "incomplete" and nobody notices. Lint must flag it.
    # NOTE: an UNCHECKED approval box is the normal filed state (awaiting
    # Evan) — NOT a lint failure. Only a missing SECTION (no box at all,
    # checked above) or missing body content is structural.
    is_resolved_check = "resolved" in path.parts
    if not is_resolved_check:
        missing_sections = [
            s for s in REQUIRED_SECTIONS if not section_value(text, s)
        ]
        for sec in missing_sections:
            manual.append(f"missing `## {sec}` section (dispatch gate; needs content from the filing context — rename an equivalent section or write it)")
        if not assigned_worker(text):
            fm_asg = (frontmatter_raw(text).get("assignee") or "").strip().strip('"').lower()
            if fm_asg == "unassigned":
                pass  # explicit "awaiting Evan's routing" — legitimate filed state
            else:
                manual.append("missing `## Assignee` section (dispatch gate; needs a valid fleet agent from the filing context)")
    return auto, manual


def _lint_apply_fixes(
    text: str,
    path: Path,
    author: Optional[str],
    date_str: Optional[str],
    assignee: Optional[str],
    session: Optional[str] = None,
    title: Optional[str] = None,
    severity: Optional[str] = None,
    bug_type: Optional[str] = None,
) -> tuple[str, list[str]]:
    """Apply the auto-fixable set (and flag-supplied backfills) to one file's text."""
    applied: list[str] = []
    if not text.lstrip().startswith("---"):
        return text, ["no frontmatter block — cannot auto-fix; needs manual authorship"]
    raw = frontmatter_raw(text)
    # 1. Migrate alias keys to canonical names, preserving order.
    for legacy, canonical in sorted(FRONTMATTER_ALIASES.items()):
        if legacy in raw and canonical not in raw:
            text = replace_frontmatter_key(text, legacy, canonical)
            applied.append(f"`{legacy}:` -> `{canonical}:`")
    raw = frontmatter_raw(text)
    canonical = {FRONTMATTER_ALIASES.get(k, k): v for k, v in raw.items()}
    # 2. Backfill missing/empty keys from CLI flags.
    if author and not canonical.get("filed_by", "").strip():
        text = _insert_frontmatter_key(text, "filed_by", author)
        applied.append(f"backfilled `filed_by: \"{author}\"`")
    if assignee and not canonical.get("assignee", "").strip():
        text = _insert_frontmatter_key(text, "assignee", assignee)
        applied.append(f"backfilled `assignee: \"{assignee}\"`")
    if date_str and not canonical.get("date", "").strip():
        text = _insert_frontmatter_key(text, "date", date_str)
        applied.append(f"backfilled `date: \"{date_str}\"`")
    if session and not canonical.get("session", "").strip():
        text = _insert_frontmatter_key(text, "session", session)
        applied.append(f"backfilled `session: \"{session}\"`")
    if title and not canonical.get("title", "").strip():
        text = _insert_frontmatter_key(text, "title", title)
        applied.append(f"backfilled `title: \"{title}\"`")
    if severity and not canonical.get("severity", "").strip():
        text = _insert_frontmatter_key(text, "severity", severity)
        applied.append(f"backfilled `severity: \"{severity}\"`")
    if bug_type and not canonical.get("type", "").strip():
        text = _insert_frontmatter_key(text, "type", bug_type)
        applied.append(f"backfilled `type: \"{bug_type}\"`")
    # 3. Fill empty tags/summary keys with benign defaults.
    raw = frontmatter_raw(text)
    if "tags" in raw and not raw["tags"].strip():
        text = re.sub(r"(?m)^tags:\s*$", "tags: []", text, count=1)
        applied.append("emptied `tags:` -> `tags: []`")
    if "summary" in raw and not raw["summary"].strip(" \""):
        text = re.sub(r'(?m)^summary:\s*""?\s*$', 'summary: ""', text, count=1)
        applied.append("normalized empty `summary:`")

    # 4. Missing approval section -> insert UNCHECKED box before `## Kanban tasks`,
    #    or append at end. NEVER touch an existing checkbox state.
    if not re.search(rf"(?m)^## {re.escape(APPROVED_SECTION)}\s*$", text):
        block = f"\n## {APPROVED_SECTION}\n\n{APPROVED_CHECKBOX_LINE}\n"
        if re.search(rf"(?m)^## {re.escape('Kanban tasks')}\s*$", text):
            text = re.sub(
                rf"(?m)^(## Kanban tasks\s*)$",
                rf"{block.strip()}\n\n\1",
                text,
                count=1,
            )
            applied.append("inserted `## Approved to run` with UNCHECKED box before Kanban tasks")
        else:
            text = text.rstrip() + "\n\n" + block.strip() + "\n"
            applied.append("appended `## Approved to run` with UNCHECKED box at end")
    else:
        section = section_value(text, APPROVED_SECTION)
        if not re.search(r"- \[[ xX]\]", section):
            # Section exists but has no checkbox line inside — add unchecked box.
            text = replace_section(
                text, APPROVED_SECTION, APPROVED_CHECKBOX_LINE
            )
            applied.append("inserted UNCHECKED approval checkbox into existing section")
    return text, applied


def _insert_frontmatter_key(text: str, key: str, value: str) -> str:
    """Insert `key: "value"` as the last line of the frontmatter block."""
    try:
        end = text.index("\n---", 3)
    except ValueError:
        return text
    return text[:end] + f'\n{key}: "{value}"' + text[end:]


def cmd_lint(args: argparse.Namespace) -> None:
    """Lint bug files for structural integrity; optionally repair (2026-11-16).

    Root cause this guards: agents free-handing bug files with write_file
    instead of `bugtool new`, then hand-edits deleting frontmatter keys or
    the approval section. `bugtool new` always writes the full canonical
    frontmatter + UNCHECKED approval checkbox; drift comes from bypasses.

    Rules enforced (honoring the human-authored checkbox contract):
      - Preferred: canonical frontmatter keys present and non-empty:
        title/type/status/date/severity/filed_by/assignee/session/tags/summary
      - Alias keys (created/reporter/author/...) migrate to canonical names.
      - Every bug file MUST have a `## Approved to run` section containing a
        checkbox. Missing section / missing box -> repair inserts an UNCHECKED
        box. lint NEVER ticks, unticks, or rewrites an existing checkbox —
        that state belongs to Evan alone.
    """
    broken_count = 0
    files = all_bug_files(args.project)
    if args.file:
        files = [bug_path(args.file)]
    # Backfill flags imply --repair (the flags exist to repair).
    if any((args.author, args.date, args.assignee,
            getattr(args, "session", None), getattr(args, "title", None),
            getattr(args, "severity", None), getattr(args, "bug_type", None))):
        args.repair = True
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            die(f"bug file not found: {path}")
        auto, manual = bug_lint_issues(text, path)
        if not auto and not manual:
            continue
        broken_count += 1
        rel = path.name if args.project or args.file else path
        print(f"FAIL {rel}")
        for issue in auto:
            print(f"  fixable:   {issue}")
        for issue in manual:
            per_file_fix = issue.replace(
                " (needs value from CLI flag or forensics)", ""
            ).replace(" (needs value from CLI flag or heading)", "")
            print(f"  needs-auth: {per_file_fix}")
        if args.repair:
            with locked_root():
                current = path.read_text(encoding="utf-8")
                fixed, applied = _lint_apply_fixes(
                    current, path, args.author, args.date, args.assignee,
                    session=getattr(args, "session", None),
                    title=getattr(args, "title", None),
                    severity=getattr(args, "severity", None),
                    bug_type=getattr(args, "bug_type", None),
                )
                if fixed != current:
                    atomic_write_text(path, fixed)
                    print(f"  repaired:  {len(applied)} change(s)")
                    for line in applied:
                        print(f"    - {line}")
                else:
                    print("  repaired:  nothing to apply")
    if not broken_count:
        print("all bug files pass lint")
    else:
        print(f"\n{broken_count} file(s) failed lint")
        if not args.repair:
            print("run with --repair to auto-fix safe issues (checkbox states are never touched)")
    if getattr(args, "lint_exit_code", False):
        sys.exit(1 if broken_count else 0)


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


def bug_slug_from_path(path: Path) -> str:
    """Slug portion of a bug filename (create_task uses the same derivation)."""
    return path.stem.split("-", 3)[-1]


def _bugs_dir(status: str) -> list[Path]:
    if not PROJECTS_ROOT.exists():
        return []
    dirs = []
    for project_dir in sorted(p for p in PROJECTS_ROOT.iterdir() if p.is_dir()):
        directory = project_dir / "bugs" / status
        if directory.is_dir():
            dirs.extend(sorted(directory.glob("*.md")))
    return dirs


def _parse_dependency_slugs(section: str) -> list[str]:
    """Comma/whitespace-separated slugs; optional date prefix stripped.

    Anything that is not a plain lowercase-hyphen slug after normalization is a
    hard refusal — a malformed `Depends on` entry can never silently pass.
    """
    slugs: list[str] = []
    for raw in re.split(r"[,\s]+", section):
        token = raw.strip().strip("`'\"")
        if not token:
            continue
        token = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", token)
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", token):
            die(f"invalid dependency slug '{token}' in {DEPENDS_SECTION} (expected a bug slug)")
        slugs.append(token)
    return slugs


def dependency_gate(text: str) -> list[str]:
    """Dependency slugs from `## Depends on` that are STILL pending.

    Returns the unresolved slugs (dispatch blockers). Hard refusals, never
    silent passes (t_cd44bbde): a slug matching NO known bug file (typo or
    deleted file) dies with a distinct message — otherwise a typo would block
    the bug forever with no signal. A dependency cycle among pending bugs dies
    loudly with the cycle spelled out; it must never loop.

    Resolution is the release: when a dependency's file leaves bugs/pending/
    (moved to resolved/), the slug drops out of the returned list and the next
    dispatch pass picks the dependent up — no second mechanism needed.
    """
    slugs = _parse_dependency_slugs(section_value(text, DEPENDS_SECTION))
    if not slugs:
        return []
    pending = {bug_slug_from_path(p) for p in _bugs_dir("pending")}
    known = pending | {bug_slug_from_path(p) for p in _bugs_dir("resolved")}
    unknown = [slug for slug in slugs if slug not in known]
    if unknown:
        die(
            f"dependency '{unknown[0]}' does not match any bug file under "
            f"{PROJECTS_ROOT} — refusing to dispatch on a missing dependency"
        )
    _die_on_dependency_cycle()
    return [slug for slug in slugs if slug in pending]


def _die_on_dependency_cycle() -> None:
    """DFS over the pending-bug dependency graph; die() naming the cycle."""
    graph: dict[str, list[str]] = {}
    for path in _bugs_dir("pending"):
        deps = _parse_dependency_slugs(section_value(path.read_text(encoding="utf-8"), DEPENDS_SECTION))
        if deps:
            graph[bug_slug_from_path(path)] = deps
    WHITE, GRAY, BLACK = 0, 1, 2
    color = dict.fromkeys(graph, WHITE)

    def visit(node: str, stack: list[str]) -> None:
        color[node] = GRAY
        stack.append(node)
        for dep in graph.get(node, []):
            if dep not in graph:
                continue  # resolved or unknown: unknown dies in dependency_gate
            if color[dep] == GRAY:
                cycle = stack[stack.index(dep):] + [dep]
                die("dependency cycle detected: " + " -> ".join(cycle))
            if color[dep] == WHITE:
                visit(dep, stack)
        stack.pop()
        color[node] = BLACK

    for slug in sorted(graph):
        if color[slug] == WHITE:
            visit(slug, [])


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

# Per-agent cap (t_cd44bbde, Phase B): at most this many LIVE tasks per assignee
# before further dispatches for that agent are DEFERRED (never dropped — the
# bug stays pending+checked and the next pass retries).
# 0 = unlimited disabled; 5 = default generous cap (Evan 2026-10-14: cap 1 raised;
# Neo's OpenAI-bound queue clears, blocked tasks no longer count toward attention).
MAX_LIVE_TASKS_PER_AGENT = 5


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


def live_task_count_by_agent(assignee: str) -> int:
    """How many LIVE kanban tasks the agent currently holds (Phase B cap).

    Queries the real board via `hermes kanban list --assignee <agent> --json`
    and counts tasks in LIVE_STATUSES. "Busy" means ANY live task from any
    source (t_cd44bbde design call): a task occupies the agent equally whether
    this tool dispatched it or a human did, and the cap exists to stop one
    agent from drowning in concurrent work.

    Fails OPEN with a loud stderr warning: the cap is overload protection,
    not a correctness gate (correctness = dispatch log + owned_live_task_ids +
    is_dispatched_marker). A transient board-query failure must not wedge
    every dispatch forever — it is logged and the dispatch proceeds.
    """
    result = subprocess.run(
        [HERMES, "kanban", "list", "--assignee", assignee, "--json"],
        text=True, capture_output=True, check=False,
    )
    output = result.stdout if result.returncode == 0 else ""
    if not output.strip():
        if result.returncode:
            print(
                f"bugtool: per-agent cap unavailable (kanban list exit "
                f"{result.returncode}: {(result.stderr or '').strip()[:200]}); "
                f"cap bypassed this pass",
                file=sys.stderr,
            )
        return 0
    try:
        records = json.loads(output)
    except json.JSONDecodeError:
        print(
            "bugtool: per-agent cap unavailable (kanban list returned invalid "
            "JSON); cap bypassed this pass",
            file=sys.stderr,
        )
        return 0
    if not isinstance(records, list):
        return 0
    return sum(1 for record in records if record.get("status") in CAP_COUNT_STATUSES)


# Global one-at-a-time gate (Evan 2026-11-17): "We should be able to dispatch
# these just by setting the assignee and checking the box. We need to make sure
# we don't dispatch more than 1 task at a time though." The checkbox flow must
# never fan out across assignees (the per-agent cap below does not stop N bugs
# going to N different agents in one hook pass).
MAX_LIVE_BUG_TASKS = 1


def live_bug_task_count() -> int:
    """How many live BUG tasks the queue currently holds (global gate).

    Counts tasks titled with this tool's own convention (``BUG: <slug>``,
    see create_task / the auto-resolve lifecycle) in CAP_COUNT_STATUSES — a
    blocked bug task does not hold the queue (Evan 2026-10-14: blocked is not
    consuming attention), and done/archived are not live.

    Fails OPEN with a loud stderr warning, same contract as the per-agent cap:
    this is overload protection, not a correctness gate (correctness lives in
    the dispatch log + owned_live_task_ids + is_dispatched_marker).
    """
    result = subprocess.run(
        [HERMES, "kanban", "list", "--json"],
        text=True,
        capture_output=True,
        check=False,
    )
    output = result.stdout if result.returncode == 0 else ""
    if not output.strip():
        if result.returncode:
            print(
                f"bugtool: global one-at-a-time gate unavailable (kanban list "
                f"exit {result.returncode}: {(result.stderr or '').strip()[:200]}); "
                f"gate bypassed this pass",
                file=sys.stderr,
            )
        return 0
    try:
        records = json.loads(output)
    except json.JSONDecodeError:
        print(
            "bugtool: global one-at-a-time gate unavailable (kanban list "
            "returned invalid JSON); gate bypassed this pass",
            file=sys.stderr,
        )
        return 0
    if not isinstance(records, list):
        return 0
    return sum(
        1
        for record in records
        if record.get("status") in CAP_COUNT_STATUSES
        and str(record.get("title", "")).startswith("BUG:")
    )


def task_body(path: Path, text: str, directive: Optional[str] = None) -> str:
    parts = [f"Bug file: {path}"]
    for section in (*REQUIRED_SECTIONS, ASSIGNEE_SECTION, APPROVED_SECTION, RELATED_SECTION, "Failure Reports"):
        value = section_value(text, section)
        if section == RELATED_SECTION and not value:
            # Optional grouping metadata: omit the heading entirely when absent
            # so every pre-existing bug body keeps its exact old shape.
            continue
        if section == ASSIGNEE_SECTION and not value:
            # Frontmatter `assignee:` is canonical (assigned_worker reads it
            # first, section second); `## Assignee` is the legacy location.
            # Without this fallback, a frontmatter-only bug renders "(none)"
            # here even though dispatch resolved a real worker — the task
            # body then misreports the assignee to every downstream reader
            # (e.g. `assignee: unassigned` in a kanban completion notice for
            # a task that is plainly assigned).
            value = assigned_worker(text) or ""
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
        # Definite vs ambiguous failure (2026-11-17, the spew guard): an
        # argparse/usage rejection (exit 2) provably never reached the backend,
        # so the bug must stay retryable (2026-09-21: a rejected CLI flag must
        # not deadlock every retry). Anything else may have created a task we
        # failed to read back ("connection lost after request") — record it as
        # ambiguous so dispatch_is_recorded suppresses auto-retry; `bugtool
        # redispatch` (force=True) is the deliberate revive path.
        append_dispatch_record(
            identity, "failed", ambiguous=(result.returncode != 2)
        )
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
    # Dependency gate (t_cd44bbde, Phase A2): a `## Depends on` section must be
    # fully resolved (no listed slug still in bugs/pending/) before dispatch.
    # Sits after approved_to_run / the escalation check and before the spew
    # guard, so a 4-failure bug still escalates to Evan instead of silently
    # sitting behind a dependency. Missing slug / cycle die loudly inside
    # dependency_gate — never a silent pass.
    unresolved_deps = dependency_gate(text)
    if unresolved_deps:
        print(f"nothing to dispatch for {path.name}: blocked by {', '.join(unresolved_deps)}")
        return None
    # Global one-at-a-time gate (Evan 2026-11-17): at most MAX_LIVE_BUG_TASKS
    # live bug task fleet-wide, regardless of assignee. DEFER, never drop
    # (same semantics as the per-agent cap): the file stays pending+checked
    # and the next pass picks it up when the live task completes. force=True
    # (bugtool redispatch) bypasses: the human asked for it.
    if not force and live_bug_task_count() >= MAX_LIVE_BUG_TASKS:
        print(
            f"nothing to dispatch for {path.name}: deferred, a bug task is "
            f"already live (one at a time)"
        )
        return None
    # Per-agent cap (t_cd44bbde, Phase B): defer, never drop. The bug file is
    # deliberately left untouched — still pending + checked — so the next
    # dispatch pass (daemon scan or manual) picks it up when the agent frees
    # up. force=True (bugtool redispatch) bypasses: the human asked for it.
    if not force and worker and live_task_count_by_agent(worker) >= MAX_LIVE_TASKS_PER_AGENT:
        print(
            f"nothing to dispatch for {path.name}: deferred, {worker} already "
            f"has {MAX_LIVE_TASKS_PER_AGENT} live task(s) (cap)"
        )
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
    result = subprocess.run([HERMES, "send", "-u", "evan", message], text=True, capture_output=True, check=False)
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


def cmd_reconcile(args: argparse.Namespace) -> None:
    """Sync pending/ directory state with each file's frontmatter status.

    `reconcile` (Evan, 2026-09-22): files he resolved manually in Obsidian
    (or that agents left in pending/ after finishing) carry
    frontmatter `status: resolved` while sitting in pending/ — the same
    incomplete-cleanup drift that stranded notify-wrong-telegram-id and
    pente-new-transport. One command finds and moves them:

    - pending/ file with status resolved → moved to resolved/
    - pending/ file with any terminal status (resolved/done/archived) → moved
    - pending/ file that is not a bug file at all (no type: bug frontmatter,
      e.g. vault-sync junk like Home.md/INDEX.md) → reported, NOT moved
    - files marked pending → untouched; `--force-resolve` moves them to
      resolved/ with the given summary (for files Evan declares done but
      whose frontmatter he didn't update)

    Read-only on everything else; no dispatch, no checkbox writes.
    """
    import re as _re

    moved = 0
    for path in all_bug_files(getattr(args, "project", None)):
        if path.parent.name != "pending":
            continue
        text = path.read_text(encoding="utf-8")
        fm = frontmatter(text)
        fm_status = (fm.get("status") or "").strip().lower()
        if not fm.get("type"):
            print(f"{path.name}: not a bug file (no type: bug) — left in place, consider deleting")
            continue
        if fm_status in {"resolved", "done", "archived"}:
            target = path.parent.parent / "resolved" / path.name
            with locked_root():
                if target.exists():
                    print(f"{path.name}: SKIPPED — resolved/ twin already exists")
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(path, target)
            print(f"{path.name}: frontmatter says '{fm_status}' — moved to resolved/")
            moved += 1
        elif getattr(args, "force_resolve", False):
            summary = getattr(args, "summary", "") or "Manually resolved by Evan (reconcile)"
            target = path.parent.parent / "resolved" / path.name
            with locked_root():
                if target.exists():
                    print(f"{path.name}: SKIPPED — resolved/ twin already exists")
                    continue
                updated = replace_section(text, "Resolution", summary)
                path.write_text(updated, encoding="utf-8")
                _set_frontmatter_status(updated, "resolved", path)
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(path, target)
            print(f"{path.name}: force-resolved → moved to resolved/")
            moved += 1
    print(f"reconcile complete: {moved} moved")


def _set_frontmatter_status(text: str, status: str, path: Path) -> str:
    import re as _re

    m = _re.search(r"(?m)^status:\s*.*$", text)
    if m:
        text = text[:m.start()] + f"status: \"{status}\"" + text[m.end():]
    else:
        text = _re.sub(r"(?m)^type:.*$", lambda mm: mm.group(0) + f"\nstatus: \"{status}\"", text, count=1)
    return text


def cmd_check(args: argparse.Namespace) -> None:
    """Sweep pending bugs and dispatch every one Evan ALREADY checked.

    Bare `check` (Evan's workflow, restored 2026-09-22): he edits bug files in
    Obsidian and ticks the `## Approved to run` boxes himself, then runs ONE
    command to dispatch everything checked. This sweep NEVER writes approval
    boxes — it only dispatches files whose box is already checked. A bug with
    an unchecked box is reported and skipped, never approved behind his back.
    The old danger (48327b3778, Sep 16) was the sweep dispatching on
    required-fields alone, stamping boxes on Evan's behalf — that stamping
    stays in the Evan-only `check-and-dispatch` path.

    Requires --i-am-evan (Evan, 2026-09-22): the sweep dispatches paid
    workers, so it stays a human-issued command even though it never
    writes approval boxes.
    """
    if not getattr(args, "i_am_evan", False):
        die(
            "check refuses to run without --i-am-evan.\n"
            "This sweep DISPATCHES a paid worker for every already-checked "
            "bug. It never writes approval boxes, but only Evan runs it."
        )
    dispatched: list[str] = []
    for path in all_bug_files(getattr(args, "project", None)):
        if path.parent.name != "pending":
            continue
        with locked_root():
            fresh = path.read_text(encoding="utf-8")
            identity = dispatch_identity(path, fresh, None)
            if dispatch_is_recorded(identity):
                record = latest_dispatch_record(identity)
                task_id = record.get("task_id") if record else None
                print(f"{path.name}: dispatched={task_id or 'reserved'} failures={failure_count(fresh)}/4")
                continue
            live = live_task_ids(fresh)
            if not approved_to_run(fresh):
                state = f"live={','.join(live) if live else 'none'} failures={failure_count(fresh)}/4"
                print(f"{path.name}: {state} — NOT checked, skipped")
                continue
            print(f"{path.name}: checked, dispatching…")
            created = maybe_dispatch_locked(path, fresh)
            if created:
                dispatched.append(created)
                print(f"  dispatched {created}")
            else:
                print("  dispatch deferred: live task exists or failure limit reached")
    if not dispatched:
        print("no checked bugs to dispatch")


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
    lint = commands.add_parser(
        "lint",
        help="validate bug file frontmatter and approval checkboxes; optionally repair",
    )
    lint.add_argument("project", nargs="?", default=None)
    lint.add_argument("--file", default=None, help="lint a single bug file instead of a project")
    lint.add_argument("--repair", action="store_true", help="apply auto-fixable repairs in place")
    lint.add_argument("--author", default=None, help="backfill filed_by when missing (implies --repair)")
    lint.add_argument("--date", default=None, help="backfill date when missing (implies --repair)")
    lint.add_argument("--assignee", default=None, help="backfill assignee when missing (implies --repair)")
    lint.add_argument("--session", default=None, help="backfill session when missing (implies --repair)")
    lint.add_argument("--title", default=None, help="backfill title when missing (implies --repair)")
    lint.add_argument("--severity", default=None, help="backfill severity when missing (implies --repair); normal|high")
    lint.add_argument("--bug-type", default=None, help="backfill type when missing (implies --repair)")
    lint.set_defaults(func=cmd_lint)
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
        "check",
        help=(
            "SWEEP (--i-am-evan required): dispatch every pending bug whose "
            "approval box Evan already checked in the file. Never writes boxes."
        ),
    )
    check.add_argument("project", nargs="?")
    check.add_argument("--i-am-evan", dest="i_am_evan", action="store_true",
                       help="required — the sweep dispatches paid workers")
    check.set_defaults(func=cmd_check)
    recon = commands.add_parser(
        "reconcile",
        help=(
            "SYNC: move pending/ files whose frontmatter says resolved/done/"
            "archived to resolved/; report non-bug junk files. Read-only on "
            "approval state."
        ),
    )
    recon.add_argument("project", nargs="?")
    recon.add_argument("--force-resolve", dest="force_resolve", action="store_true",
                       help="also move files still marked pending (Evan declares them done)")
    recon.add_argument("--summary", default="",
                       help="Resolution summary used with --force-resolve")
    recon.set_defaults(func=cmd_reconcile)
    cad = commands.add_parser(
        "check-and-dispatch",
        help=(
            "WRITES: Evan-only. Checks the approval box on ONE bug AND dispatches it "
            "(requires --i-am-evan). Agents must not run this — use `dispatch`, "
            "`list`, `search`, or read the file."
        ),
    )
    cad.add_argument("file", nargs="?", default="",
                       help="the ONE bug file to approve and dispatch")
    cad.add_argument("--i-am-evan", dest="i_am_evan", action="store_true",
                       help="required confirmation that Evan is performing this approval himself")
    cad.set_defaults(func=cmd_check_and_dispatch)
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
