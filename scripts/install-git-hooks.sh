#!/usr/bin/env bash
# Install this repo's version-controlled git hooks into .git/hooks.
#
# Hooks live in .git/hooks, which git does NOT track — so a fresh clone has
# none, and any hook written straight into .git/hooks is invisible to history.
# Keeping the real files in scripts/git-hooks/ (tracked, reviewable, backed up
# in the fork) and installing them from here means:
#   * the hook is versioned and restorable,
#   * re-running is safe and idempotent,
#   * a re-clone can be brought back to a working state with one command.
#
# Worktrees share the main repo's hooks directory (git resolves hooks via the
# common dir), so installing once covers every worktree of this repo.
#
# Usage:
#   scripts/install-git-hooks.sh          # install / refresh
#   scripts/install-git-hooks.sh --check  # report status, change nothing

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
hooks_src="$repo_root/scripts/git-hooks"

if [ ! -d "$hooks_src" ]; then
    echo "ERROR: no hook source directory at $hooks_src" >&2
    exit 1
fi

# --git-common-dir resolves to the shared .git even when run from a worktree.
git_common_dir="$(git -C "$repo_root" rev-parse --path-format=absolute --git-common-dir)"
hooks_dst="$git_common_dir/hooks"

check_only=0
if [ "${1:-}" = "--check" ]; then
    check_only=1
fi

mkdir -p "$hooks_dst"

status=0
installed=0
for src in "$hooks_src"/*; do
    [ -f "$src" ] || continue
    name="$(basename "$src")"
    dst="$hooks_dst/$name"

    if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
        echo "ok       $name"
        continue
    fi

    if [ "$check_only" -eq 1 ]; then
        if [ -f "$dst" ]; then
            echo "STALE    $name (differs from scripts/git-hooks/$name)"
        else
            echo "MISSING  $name"
        fi
        status=1
        continue
    fi

    cp "$src" "$dst"
    chmod +x "$dst"
    echo "installed $name"
    installed=$((installed + 1))
done

if [ "$check_only" -eq 1 ]; then
    if [ "$status" -eq 0 ]; then
        echo "All hooks installed and current."
    else
        echo "Hooks need attention — run: scripts/install-git-hooks.sh" >&2
    fi
    exit "$status"
fi

if [ "$installed" -eq 0 ]; then
    echo "All hooks already current ($hooks_dst)."
else
    echo "Installed $installed hook(s) into $hooks_dst"
fi

exit 0
