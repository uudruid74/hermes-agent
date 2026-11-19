#!/usr/bin/env python3
"""Migrate per-profile projects.db rows into the single fleet-wide projects.db.

Task t_70262dbb (wintermute). The resolver (projects_db_path) already points at
the shared DB; this script moves the DATA out of the legacy per-profile stores
into `~/.hermes/projects.db` (or HERMES_PROJECTS_DB).

Precedence (wintermute's ruling, applied exactly):
  * project_meta      — union by key; INSERT OR IGNORE. Any key whose value
                        differs across profiles is logged (should be none).
  * projects          — insert by id. On conflict, prefer the row with the
                        later created_at. slug UNIQUE collisions -> STOP.
  * project_folders   — union by (project_id, path); prefer later added_at.
  * discovered_repos  — must be 0 everywhere; skipped.

Run: venv/bin/python scripts/migrate_projects_db.py
     [--target PATH] [--profile-db gopher=/path ...]  (defaults are fleet paths)
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

DEFAULT_TARGET = Path("/home/ekl/.hermes/projects.db")
DEFAULT_PROFILES = ("gopher", "neo", "zephyr", "ornith", "wintermute")
FLEET_ROOT = "/home/ekl/.hermes"


def _ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _load_sources(profile_dbs: dict[str, Path]) -> dict[str, sqlite3.Connection]:
    conns: dict[str, sqlite3.Connection] = {}
    for profile, path in profile_dbs.items():
        if not path.exists():
            raise SystemExit(f"source DB missing: {path}")
        conns[profile] = _ro(path)
    return conns


def _check_discovered_repos(sources: dict[str, sqlite3.Connection]) -> None:
    for profile, con in sources.items():
        n = con.execute("SELECT COUNT(*) FROM discovered_repos").fetchone()[0]
        if n:
            raise SystemExit(f"ABORT: {profile} has {n} discovered_repos rows (expected 0)")


def _collect_meta(
    sources: dict[str, sqlite3.Connection],
) -> tuple[list[tuple[str, str, list[str]]], list[tuple[str, dict[str, str]]]]:
    """Return [(key, chosen_value, profiles_having_key)] plus a divergence log."""
    by_key: dict[str, dict[str, str]] = {}
    for profile, con in sources.items():
        for row in con.execute("SELECT key, value FROM project_meta"):
            by_key.setdefault(row["key"], {})[profile] = row["value"]
    rows = []
    divergences = []
    for key in sorted(by_key):
        vals = by_key[key]
        distinct = sorted(set(vals.values()))
        chosen = distinct[0]
        if len(distinct) > 1:
            divergences.append((key, vals))
        rows.append((key, chosen, sorted(vals)))
    return rows, divergences


def _collect_projects(sources: dict[str, sqlite3.Connection]) -> tuple[list[dict], list[str]]:
    """Return (rows, warnings). Rows are the winner per id (later created_at)."""
    by_id: dict[str, dict] = {}
    for profile, con in sources.items():
        for r in con.execute(
            "SELECT id, slug, name, description, icon, color, board_slug, "
            "primary_path, created_at, archived FROM projects"
        ):
            d = dict(r)
            d["_profile"] = profile
            prev = by_id.get(d["id"])
            if prev is None:
                by_id[d["id"]] = d
            elif d["created_at"] > prev["created_at"]:
                by_id[d["id"]] = d
            elif d["created_at"] == prev["created_at"] and dict(d) != dict(prev):
                # identical timestamps but different content: keep deterministic
                # order (profile name) so the union is reproducible.
                if profile < prev["_profile"]:
                    by_id[d["id"]] = d
    warnings = []
    # slug uniqueness across the merged set (STOP on collision)
    slug_owner: dict[str, str] = {}
    for pid, d in by_id.items():
        prev_owner = slug_owner.get(d["slug"])
        if prev_owner is not None and prev_owner != pid:
            raise SystemExit(
                f"ABORT: slug UNIQUE collision {d['slug']!r} between {prev_owner} "
                f"and {pid}; refusing to rename."
            )
        slug_owner[d["slug"]] = pid
    return list(by_id.values()), warnings


def _collect_folders(sources: dict[str, sqlite3.Connection]) -> list[dict]:
    by_pk: dict[tuple, dict] = {}
    for profile, con in sources.items():
        for r in con.execute(
            "SELECT project_id, path, label, is_primary, added_at FROM project_folders"
        ):
            d = dict(r)
            d["_profile"] = profile
            pk = (d["project_id"], d["path"])
            prev = by_pk.get(pk)
            if prev is None or d["added_at"] > prev["added_at"]:
                by_pk[pk] = d
            elif d["added_at"] == prev["added_at"] and dict(d) != dict(prev):
                if profile < prev["_profile"]:
                    by_pk[pk] = d
    return list(by_pk.values())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--target",
        type=Path,
        default=Path(os.environ.get("HERMES_PROJECTS_DB", "") or DEFAULT_TARGET),
    )
    ap.add_argument("--profile-db", action="append", default=[], metavar="NAME=PATH",
                    help="override a profile's source DB path (repeatable)")
    args = ap.parse_args()

    target = args.target.expanduser()
    profile_dbs: dict[str, Path] = {
        p: Path(FLEET_ROOT) / "profiles" / p / "projects.db" for p in DEFAULT_PROFILES
    }
    for kv in args.profile_db:
        name, _, path = kv.partition("=")
        if name not in profile_dbs:
            raise SystemExit(f"unknown profile {name!r}")
        profile_dbs[name] = Path(path).expanduser()

    print(f"target: {target}")
    for p, path in profile_dbs.items():
        print(f"  source {p}: {path}")

    sources = _load_sources(profile_dbs)
    try:
        _check_discovered_repos(sources)
        meta_rows, divergences = _collect_meta(sources)
        if divergences:
            print("DIVERGENCE WARNING (values differ across profiles):")
            for key, vals in divergences:
                print(f"  {key}: {vals}")
        else:
            print("meta: no divergent keys across profiles")

        projects = _collect_projects(sources)[0]
        folders = _collect_folders(sources)

        con = sqlite3.connect(str(target))
        con.row_factory = sqlite3.Row
        try:
            tables = {
                r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            need = {"projects", "project_folders", "project_meta", "discovered_repos"}
            missing = need - tables
            if missing:
                raise SystemExit(f"ABORT: target missing tables {sorted(missing)}")

            con.execute("PRAGMA foreign_keys=ON")
            before = {
                t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                for t in need
            }
            con.execute("BEGIN IMMEDIATE")
            try:
                for d in sorted(projects, key=lambda x: x["id"]):
                    con.execute(
                        "INSERT OR IGNORE INTO projects "
                        "(id, slug, name, description, icon, color, board_slug, "
                        "primary_path, created_at, archived) "
                        "VALUES (:id, :slug, :name, :description, :icon, :color, "
                        ":board_slug, :primary_path, :created_at, :archived)",
                        {k: d[k] for k in (
                            "id", "slug", "name", "description", "icon", "color",
                            "board_slug", "primary_path", "created_at", "archived",
                        )},
                    )
                for d in folders:
                    con.execute(
                        "INSERT OR IGNORE INTO project_folders "
                        "(project_id, path, label, is_primary, added_at) "
                        "VALUES (:project_id, :path, :label, :is_primary, :added_at)",
                        {k: d[k] for k in ("project_id", "path", "label",
                                            "is_primary", "added_at")},
                    )
                for key, value, _profiles in meta_rows:
                    con.execute(
                        "INSERT OR IGNORE INTO project_meta (key, value) "
                        "VALUES (?, ?)",
                        (key, value),
                    )
                con.commit()
            except BaseException:
                con.rollback()
                raise

            after = {
                t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                for t in need
            }
            ic = con.execute("PRAGMA integrity_check").fetchone()[0]

            orphans = con.execute(
                "SELECT COUNT(*) FROM project_folders pf "
                "LEFT JOIN projects p ON p.id = pf.project_id "
                "WHERE p.id IS NULL"
            ).fetchone()[0]

            print(f"integrity_check: {ic}")
            print(f"orphan folders: {orphans}")
            for t in need:
                print(f"  {t}: {before[t]} -> {after[t]}")
        finally:
            con.close()
    finally:
        for con in sources.values():
            con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
