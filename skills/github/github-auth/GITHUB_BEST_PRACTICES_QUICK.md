# GitHub Access Best Practices for Hermes Agent/Gopher

## Quick Reference Guide (2026 Edition)

### TL;DR - What Should Hermes Agent Use?

**✅ FINE-GRAINED PAT** (Personal Access Token) 
- For all git operations: `git clone`, `git push`, `git commit`, etc.
- Configure with **specific repository access only**
- Set **permissions to minimum required**: `contents:read` or `contents:write`
- Set **expiry**: 90 days (auto-renew/reminder)
- Store in **Hermes secrets manager** or **Git credential helper**

### Why Not SSH Keys or Classic PATs?

- ❌ **SSH keys**: No repository scoping, being deprecated for automation
- ❌ **Classic PATs**: Broad access to ALL your repositories, no granularity
- ✅ **Fine-grained PATs**: Specific repo access, 50+ granular permissions, auto-expiry

### Required Setup (Copy-paste ready):

```bash
# 1. Generate fine-grained PAT (90 days)
#    Go to: https://github.com/settings/personal-access-tokens
#    Name: "hermes-agent-checkout"
#    Permissions: contents:read, workflows:read
#    Select specific repositories only

# 2. Store token securely (choose one)
# Option A: Hermes secrets manager
hermes-secrets set GITHUB_TOKEN "your...token" --ttl=90d

# Option B: Git credential helper
git config --global credential.helper store
echo "https://$(whoami):your_token@github.com" > ~/.git-credentials

# Option C: Environment variable
export GITHUB_TOKEN="your...token"

# 3. Configure git identity
git config --global user.name "Hermes Agent"
git config --global user.email "automation@nousresearch.com"

# 4. Test operation
git ls-remote https://github.com/owner/repo.git
```

### Hermes Agent Integration:

```yaml
# In hermes-agent configuration
skills:
  github-auth:
    auth_method: fine_grained_pat
    minimal_permissions: ["contents:read", "workflows:read"]
    secrets_manager: hermes_secrets
    rotation: auto
```

### Token Rotation Script:

```bash
# hermes-github-rotate.sh
NEW_TOKEN=$(gh auth refresh --scopes repo,workflow -q '.token')
# Update git credentials and Hermes storage
# Delete old token from https://github.com/settings/tokens
```

---

## Authentication Method Decision Tree

```
┌─────────────────────────────────────────┐
│  What authentication method to use?    │
└─────────────────────────────────────────┘
                    ↓
        Is gh CLI available?
                    ↓
     [Yes]           │             [No]
        Use gh auth  │         Use git PAT
                    ↓
   Is fine-grained PAT available?
                    ↓
    ┌──────────────┴──────────────┐
    │                           │
[Yes] fine-grained PAT     [No] classic PAT
    │                           │
    │      (Repository-specific) │  (Legacy - all repos)
    │                           │
    ↓                           ↓
Use fine-grained PAT       Fallback to git credential
(#1 priority)              helper or SSH key
                           (deprecated 2026)
```

---

## Permission Matrix for Git Operations

| Operation | Read Permissions Needed | Write Permissions Needed |
|-----------|-------------------------|-------------------------|
| **Clone** | contents:read, workflows:read | None |
| **Pull** | contents:read, workflows:read | None |
| **Push** | contents:read, workflows:read | contents:write |
| **Commit** | contents:read | contents:write |
| **Create PR** | contents:read | pull_requests:write |
| **Merge PR** | contents:read | contents:write + pull_requests:write |
| **Create branch** | contents:read, pull_requests:read | contents:write |

---

## ✅ Security Checklist (Copy-paste)

```
□ Use fine-grained PAT instead of classic PAT or SSH
□ Scope token to specific repositories needed
□ Set expiration (90 days max)
□ Configure minimal permissions (contents:read/write only)
□ Store token in Hermes secrets manager
□ Enable audit logging for git operations
□ Set rotation reminder 7 days before expiry
□ Verify token works with test git operation
□ Disable write access on SSH deploy keys
□ Regularly audit tokens (weekly)
```

---

## 🚨 Critical Security Alerts

### 2026 GitHub Security Policy Changes:

1. **December 1, 2025** → SSH deploy keys with write access **automatically disabled**
   - Action: Switch to fine-grained PAT immediately
   - If still using SSH deploy keys: Re-add as read-only or use PAT

2. **Q4 2026** → Classic PATs officially deprecated
   - Action: Migrate all tokens to fine-grained before this date

3. **Enterprise customers** → Mandatory token expiration policies
   - Action: Set token lifetime to 90 days maximum


### Hermes Agent Integration Issues:

- If getting `remote: Permission to denied` → Token lacks proper scopes
- If getting `Authentication failed` → Token expired or wrong
- If using SSH → Check ~/.ssh/config for ssh.github.com:443

---

## 📞 Need Help?

- **Current Setup**: `skill_view(name="github-auth")`
- **Permissions**: Check https://github.com/settings/personal-access-tokens
- **Organization**: Contact org admin for PAT approval requirements
- **Production**: Use GitHub Apps instead of PATs for deployment tokens

---

*Last Updated: June 2026  |  Hermes Agent v1.4+*
