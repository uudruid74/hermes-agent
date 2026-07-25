# Hermes Agent Fork Documentation

This document describes the fork setup, patch management workflow, post-update verification, and profile theming for the Hermes Agent fork maintained at `uudruid74/hermes-agent`.

---

## Remote Configuration

The fork uses two remotes:

| Remote | URL | Purpose |
|--------|-----|---------|
| `origin` | `https://github.com/uudruid74/hermes-agent.git` | **Gopher's fork** — push target for local changes |
| `upstream` | `https://github.com/NousResearch/hermes-agent.git` | **Upstream (NousResearch)** — source of truth for upstream changes |

```bash
# Verify remotes
git remote -v
# origin  https://github.com/uudruid74/hermes-agent.git (fetch)
# origin  https://github.com/uudruid74/hermes-agent.git (push)
# upstream  https://github.com/NousResearch/hermes-agent.git (fetch)
# upstream  https://github.com/NousResearch/hermes-agent.git (push)
```

**Branch tracking:**
- Local `main` tracks `upstream/main` (fast-forward only)
- Local changes are committed to `main` and pushed to `origin/main`

---

## Patch Management Workflow

### 1. Sync from Upstream

```bash
# Fetch latest upstream changes
git fetch upstream

# Fast-forward local main to upstream/main (no merge commits)
git rebase upstream/main

# Push to origin (Gopher's fork)
git push origin main
```

**Note:** Always use `rebase` to keep history linear. The fork is 36 commits ahead of upstream as of 2026-07-24.

### 2. Local Development

Changes are made directly on `main` branch:

```bash
# Make changes
git add -A
git commit -m "fix(component): description

Details about the fix."
git push origin main
```

### 3. Patch Categories

The fork maintains patches in these categories:

| Category | Examples |
|----------|----------|
| **Kanban/Notification fixes** | Origin routing, session context preservation, typing indicators |
| **Telegram adapter fixes** | Wake events, typing cooldown, interrupt-aware retry |
| **CLI enhancements** | `hermes send -t agent`, profile theming (agentcolor/agenticon) |
| **Gateway fixes** | MCP bridge wiring, busy-session authorization |
| **Documentation** | Live Agent README, kanban topic routing docs |

### 4. Cherry-picking from Upstream

If upstream has a fix you need:

```bash
# Find the commit on upstream
git log upstream/main --oneline --grep="fix description"

# Cherry-pick onto local main
git cherry-pick <commit-hash>
git push origin main
```

---

## Post-Update Verification Script

Run this script after every `git pull upstream/main` or rebase to verify the fork is healthy.

```bash
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
```

**Usage:**
```bash
chmod +x /home/ekl/.hermes/hermes-agent/scripts/verify-fork.sh
/home/ekl/.hermes/hermes-agent/scripts/verify-fork.sh
```

---

## Profile Theming: agentcolor / agenticon

The fork supports per-profile response header theming via `agentcolor` (hex) and `agenticon` (emoji) in each profile's `config.yaml`.

### Neo Profile (`~/.hermes/profiles/neo/config.yaml`)

```yaml
agentcolor: "#00FF00"  # Matrix green
agenticon: "🧠"        # Brain emoji
```

### Gopher Profile (`~/.hermes/profiles/gopher/config.yaml`)

```yaml
agentcolor: "#FF6B35"  # Orange
agenticon: "🦫"        # Beaver emoji
```

### How It Works

The theming is implemented in commit `9cac32039` (`feat(cli): agentcolor/agenticon profile-based response header theming`):

- **Functions added:**
  - `_get_profile_color()` — reads `agentcolor` from profile config
  - `_get_profile_icon()` — reads `agenticon` from profile config
  - `_ansi_hex()` — converts hex to ANSI escape codes

- **Files modified:**
  - `agent/agent_runtime_helpers.py`
  - `agent/prompt_builder.py`
  - `agent/redact.py`
  - `cli.py` (response header rendering)
  - `gateway/stream_consumer.py`
  - `hermes_cli/kanban.py`
  - `hermes_cli/kanban_db.py`
  - `hermes_cli/kanban_decompose.py`
  - `hermes_cli/moa_config.py`
  - `hermes_cli/web_server.py`
  - `tools/memory_tool.py`

- **Behavior:** Response headers (timestamp, profile name) use the profile's color and icon instead of hardcoded `_ACCENT`.

### Verification

Run the verification script (above) or manually check:

```bash
# Check Neo
grep -E 'agentcolor|agenticon' ~/.hermes/profiles/neo/config.yaml

# Check Gopher
grep -E 'agentcolor|agenticon' ~/.hermes/profiles/gopher/config.yaml
```

---

## Quick Reference

| Task | Command |
|------|---------|
| Sync from upstream | `git fetch upstream && git rebase upstream/main && git push origin main` |
| View local patches | `git log --oneline upstream/main..main` |
| Run verification | `./scripts/verify-fork.sh` |
| Check profile theming | `grep -E 'agentcolor|agenticon' ~/.hermes/profiles/{neo,gopher}/config.yaml` |
| Push local changes | `git add -A && git commit -m "msg" && git push origin main` |

---

## Related Files

- **Fork repo:** https://github.com/uudruid74/hermes-agent
- **Upstream repo:** https://github.com/NousResearch/hermes-agent
- **Kanban board:** `hermes-fork` board (task `t_79dcd062` — this documentation)
- **Profile configs:** `~/.hermes/profiles/neo/config.yaml`, `~/.hermes/profiles/gopher/config.yaml`

---

*Generated as part of kanban task `t_79dcd062` — Wiki: Hermes Agent Fork documentation*