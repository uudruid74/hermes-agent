#!/usr/bin/env bash
# /home/ekl/.hermes/hermes-agent/scripts/verify-fork.sh
# Post-update verification for hermes-agent fork

set -euo pipefail

REPO_ROOT="/home/ekl/.hermes/hermes-agent"
cd "$REPO_ROOT"

echo "=== Hermes Agent Fork Verification ==="
echo "Date: $(date)"
echo "Repo: $REPO_ROOT"
echo

# 1. Verify remotes
echo "--- Remotes ---"
git remote -v
echo

# 2. Check branch status
echo "--- Branch Status ---"
git status --short --branch
echo

# 3. Count commits ahead/behind
echo "--- Sync Status ---"
UPSTREAM_COMMITS=$(git rev-list --count upstream/main..main 2>/dev/null || echo "0")
LOCAL_COMMITS=$(git rev-list --count main..upstream/main 2>/dev/null || echo "0")
echo "Commits ahead of upstream: $UPSTREAM_COMMITS"
echo "Commits behind upstream: $LOCAL_COMMITS"
echo

# 4. Run tests
echo "--- Running Tests ---"
if command -v pytest &> /dev/null; then
    python -m pytest tests/ -x -q --tb=short 2>&1 | head -50
    TEST_EXIT=$?
    if [ $TEST_EXIT -eq 0 ]; then
        echo "✓ All tests passed"
    else
        echo "✗ Tests failed (exit code: $TEST_EXIT)"
    fi
else
    echo "⚠ pytest not available, skipping tests"
fi
echo

# 5. Verify key files unchanged (no accidental clobber)
echo "--- Key Files Check ---"
KEY_FILES=(
    "cli.py"
    "agent/agent_init.py"
    "gateway/run.py"
    "gateway/kanban_watchers.py"
    "hermes_cli/kanban.py"
    "hermes_cli/kanban_db.py"
    "plugins/platforms/telegram/adapter.py"
)
for f in "${KEY_FILES[@]}"; do
    if git diff --quiet HEAD -- "$f" 2>/dev/null; then
        echo "  ✓ $f (clean)"
    else
        echo "  ⚠ $f (LOCAL CHANGES)"
    fi
done
echo

# 6. Verify profile configs have agentcolor/agenticon
echo "--- Profile Theming Check ---"
for profile in neo gopher; do
    CONFIG="$HOME/.hermes/profiles/$profile/config.yaml"
    if [ -f "$CONFIG" ]; then
        COLOR=$(grep -E '^\s*agentcolor:' "$CONFIG" | head -1 | sed 's/.*://' | xargs)
        ICON=$(grep -E '^\s*agenticon:' "$CONFIG" | head -1 | sed 's/.*://' | xargs)
        if [ -n "$COLOR" ] && [ -n "$ICON" ]; then
            echo "  ✓ $profile: color=$COLOR icon=$ICON"
        else
            echo "  ⚠ $profile: missing agentcolor/agenticon"
        fi
    else
        echo "  ✗ $profile: config.yaml not found"
    fi
done
echo

# 7. Verify kanban DB schema
echo "--- Kanban DB Check ---"
for db in ~/.hermes/kanban/boards/*/kanban.db; do
    if [ -f "$db" ]; then
        TABLES=$(sqlite3 "$db" ".tables" 2>/dev/null | tr '\n' ' ')
        if [[ "$TABLES" == *"tasks"* ]] && [[ "$TABLES" == *"task_runs"* ]]; then
            echo "  ✓ $(basename $(dirname $db)): schema OK"
        else
            echo "  ⚠ $(basename $(dirname $db)): missing tables"
        fi
    fi
done
echo

echo "=== Verification Complete ==="