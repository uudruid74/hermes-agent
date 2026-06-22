# GitHub Access Best Practices for Hermes Agent/Gopher
## Secure Code Check-in/Check-out Operations (2026)

---

## 📋 Executive Summary

This guide provides **secure GitHub access best practices** specifically designed for **Hermes Agent** and **Neo/Gopher subagents** performing remote git operations. It covers:

- ✅ **Authentication methods** (PAT vs SSH) with security trade-offs
- ✅ **Fine-grained permissions** for minimal privilege access
- ✅ **Repository permissions** and access controls
- ✅ **Security considerations** for automation
- ✅ **Setup steps** for personal access tokens (PATs) and SSH keys
- ✅ **Token rotation** and secret management
- ✅ **Hermes Agent integration points**

**Last Updated**: June 2026  
**Source**: GitHub documentation, security advisories, and Hermes Agent best practices

---

## 🎯 Key Recommendations

| Security Factor | Recommendation | Rationale |
|----------------|----------------|-----------|
| **Authentication Method** | **Fine-grained PAT** (preferred) → **Classic PAT** (legacy) → **SSH Deploy Keys** (specific use cases) | Fine-grained offers repository-level access control |
| **Token Type** | **Fine-grained Personal Access Tokens** (classic deprecated for new projects) | 50+ granular permissions vs 7 broad scopes |
| **Lifespan** | **30-90 days maximum**, auto-expiry where possible | Limits exposure window for compromised credentials |
| **Permissions** | **Least privilege** - only what's necessary for check-in/check-out | Prevents accidental/malicious repository access |
| **Storage** | **Git credential helper store** (local) or **secret manager** (Hermes) | Avoid hardcoded secrets in scripts/repos |
| **Rotation** | **Automated rotation scripts** for production deployments | Regular rotation limits credential lifetime |
| **Monitoring** | **Audit logs** and **dependabot scans** | Detect compromised or stale credentials |

---

## 🔐 Authentication Methods Comparison

### 1. Fine-Grained Personal Access Tokens (RECOMMENDED)

**✅ Best for**: All automated git operations, Hermes Agent check-in/out, production deployments

#### ✅ Advantages:
- **Repository-level scoping**: Grant access to specific repositories only
- **Granular permissions**: Over 50+ permissions (read, write, no access) vs 7 broad scopes
- **Auto-expiration**: Default token expiration (30-90 days) reduces risk
- **Org visibility**: Organization owners can approve, view, and revoke tokens
- **Migration path**: Designed to mirror GitHub Apps' permissions model

#### ❌ Limitations (2026):
- ❌ Cannot contribute to public repos where user isn't a member
- ❌ Cannot access multiple organizations simultaneously
- ❌ Limited to REST API only (GraphQL coming soon)
- ❌ Some API endpoints temporarily unsupported

#### Required Permissions for Git Operations:

| Operation | Minimum Required Permissions |
|-----------|-----------------------------|
| **Read repository contents** | `contents: read` |
| **Push commits** | `contents: write` |
| **Create branches** | `contents: write` |
| **Create pull requests** | `pull_requests: write` |
| **Trigger workflows** | `workflows: write` |
| **Manage issues** | `issues: write` |

**Example**: A token for checking out code only needs `contents: read` + `workflows: read`.

#### Setup Steps:

```bash
# Step 1: Create fine-grained PAT
# 1. Go to: https://github.com/settings/personal-access-tokens
# 2. Select "Fine-grained tokens" tab
# 3. Click "Generate new token"
# 4. Configure:
#    - Name: "hermes-agent-checkout"
#    - Expiration: 90 days (or enterprise maximum)
#    - Description: "Used by Hermes Agent for automated check-in/out"
#    - Resource owner: Your GitHub account
#    - Repository access: Select specific repositories
#    - Permissions:
#      - Repository permissions → Contents: Read
#      - Repository permissions → Workflows: Read

# Step 2: Store token securely
# Option A: Git credential helper (recommended for local testing)
bash
git config --global credential.helper store
echo "https://$(your-github-username):$(your-token)@github.com" > ~/.git-credentials

# Option B: Hermes secrets manager (recommended for Hermes Agent integration)
# Use Hermes skill: bitwarden-secrets or similar integration
# Export as environment variable:
export GITHUB_TOKEN="your-fine-grained-token"

# Step 3: Configure git identity
git config --global user.name "Hermes Agent"
git config --global user.email "automation@nousresearch.com"

# Step 4: Verify authentication
git ls-remote https://github.com/owner/repository.git
```

#### Token Persistence in Hermes:

Use the existing `github-auth` skill to detect and set authentication:

```bash
# Detect authentication method
export HERMES_GITHUB_TOKEN="$(hermes-github-token-detect --minimal-permissions=contents:read,workflows:read)"

# Use in git operations
export GITHUB_TOKEN="$HERMES_GITHUB_TOKEN"
git clone https://github.com/owner/repo.git
```

---

### 2. Classic Personal Access Tokens (LEGACY)

**⚠️ Warning**: GitHub is deprecating classic PATs in favor of fine-grained tokens. Use only when fine-grained doesn't support required features.

#### Required Scopes:
```bash
# For full git operations (read/write):
repo           # Full repository control
workflow       # Trigger and manage GitHub Actions
read:org      # Read organization membership
admin:repo_hook  # Manage webhooks (if needed)
```

#### Important Security Notes:
- **Scope**: Provides access to **ALL** repositories you have access to
- **No repository targeting**: Cannot limit to specific repos
- **Expiration**: Auto-expires after 1 year of inactivity
- **SSO required**: Must be authorized for SSO-protected organizations

#### When to Use:
- Migrating legacy scripts from password authentication
- Accessing enterprise accounts not yet supporting fine-grained tokens
- Temporary access while waiting for API endpoint support in fine-grained

---

### 3. SSH Keys (DEPRECATED FOR AUTOMATION)

**❌ Not Recommended** for new deployments in 2026.

#### Why SSH is becoming obsolete:
- ❌ **No repository-level scoping**: SSH keys grant access to all accessible repositories
- ❌ **No permission granularity**: Cannot restrict to read-only vs read-write
- ❌ **Security vulnerability**: SSH deploy keys with write access are being disabled (December 2025)
- ❌ **Compatibility issues**: Fine-grained PATs are the preferred path forward

#### Remaining Use Cases:
- Legacy git client installations that don't support HTTPS tokens
- User-level authentication where SSH is already preferred
- Specific SSH-only workflows (rare)

#### Setup Steps (if absolutely required):

```bash
# Step 1: Generate SSH key (ed25519 recommended)
ssh-keygen -t ed25519 -C "automation@nousresearch.com" -f ~/.ssh/ida_ed25519_hermes -N ""

# Step 2: Add public key to GitHub
# 1. Display public key: cat ~/.ssh/ida_ed25519_hermes.pub
# 2. Go to: https://github.com/settings/keys
# 3. Click "New SSH key", paste public key, set title

# Step 3: Configure git for SSH
ssh -T git@github.com  # Test connection

# Step 4: Rewrite HTTPS to SSH for automation
git config --global url."git@github.com:".insteadOf "https://github.com/"

# Step 5: Default git identity
git config --global user.name "Hermes Agent"
git config --global user.email "automation@nousresearch.com"
```

#### Security Warnings:
- Do **NOT** use SSH keys with write access on GitHub deploy keys (being disabled Dec 2025)
- If SSH must be used, restrict to read-only permissions where possible
- Monitor SSH key usage regularly
- Rotate SSH keys every 90 days

---

## 🏢 Repository Permissions Structure

### Organization-Level Permissions (Best Practices 2026)

| Permission Type | Level | Description |
|----------------|-------|-------------|
| **Organization Members** | `read` | View organization, teams, and members |
| **Repository Access** | *per-repository* | Fine-grained PAT can be restricted to specific repos |
| **Secrets & Variables** | `secrets: read` | Read organization-level secrets |
| **GitHub Actions** | `actions: read` | View organization Actions |
| **Code Scanning** | `security_events: read` | Monitor security alerts |

### Repository-Level Permissions

#### For Hermes Agent Subagent (Neo):

| Git Operation | Required Permission | Recommended Value |
|---------------|---------------------|-------------------|
| **Clone/Checkout** | `contents: read` | ✅ Required |
| **Push commits** | `contents: write` | ✅ Required |
| **Create branches** | `contents: write` | ✅ Required |
| **Create pull requests** | `pull_requests: write` | ✅ Required (if used) |
| **Merge pull requests** | `pull_requests: write` and `contents: write` | ✅ Required|
| **Trigger workflows** | `workflows: write` | ⚠️ Use minimal where possible |
| **Read secrets** | `secrets: read` | ❌ Never for production |
| **Write secrets** | `secrets: write` | ❌ Never for production |

### Branch Protection Rules

**For Hermes Agent automation:**

```yaml
# Example branch protection for automation branches
# Settings → Branches → Branch protection rules
- Branch name pattern: `hermes/**` or `neo/**`
- Required status checks: Build/Test workflows
- Required approvals: 0 (automation can bypass by design)
- Restrict push access: Disabled (allow PAT to push directly)
- Allow force pushes: Disabled
```

**Security Note**: If using PATs with write access, ensure the token is stored securely and rotated regularly.

---

## 🔄 Secret Rotation & Management

### Token Rotation Schedule

| Environment | Token Lifetime | Rotation Trigger | Hermes Integration |
|-------------|----------------|------------------|-------------------|
| **Development** | 30-90 days | Automated weekly checks | Hermes cron job |
| **Production** | 30 days max | Auto-renew via script | Managed by system |
| **Test/Staging** | 60 days | Team review | Prompt for renewal |
| **Ephemeral** | 24 hours | One-time use token | Temporary credential |

### Automated Rotation Script (2026)

```bash
#!/bin/bash
# hermes-github-token-rotator.sh

set -euo pipefail

# Configuration
TOKEN_LIFETIME_DAYS=90
USERNAME="$(git config --global user.name)"
REPOSITORIES=("owner/repo1" "owner/repo2")
PERMISSIONS=("contents:read" "contents:write" "workflows:read")

# Step 1: Generate new token
echo "Generating new GitHub token..."
NEW_TOKEN=$(gh auth refresh --scopes repo,workflow -q '.token') || {
    echo "Failed to generate token: $NEW_TOKEN"
    exit 1
}

# Step 2: Update git credentials (if using credential helper)
echo "Updating git credentials..."
if git config --global --get credential.helper | grep -q store; then
    sed -i "/github.com/d" ~/.git-credentials
    echo "https://$USERNAME:$NEW_TOKEN@github.com" >> ~/.git-credentials
fi

# Step 3: Update Hermes secrets manager (if integrated)
if command -v hermes-secrets >/dev/null 2>&1; then
    hermes-secrets set GITHUB_TOKEN "$NEW_TOKEN" --ttl="${TOKEN_LIFETIME_DAYS}d"
fi

# Step 4: Verify new token works
git ls-remote "https://github.com/$(git config --global user.name)/$(basename ${REPOSITORIES[0]}).git"

# Step 5: Delete old token (manual step in GitHub settings)
echo "✅ Rotation complete. Old token must be manually deleted from GitHub settings."
echo "🔗 Navigate to: https://github.com/settings/tokens"
```

**Dependencies**:
- `gh` CLI installed and authenticated
- GitHub user with ability to create/manage tokens
- Hermes secrets manager integration (optional)

### Manual Token Cleanup Process

1. **Go to**: https://github.com/settings/tokens
2. **Identify stale tokens**: Look for tokens without recent activity
3. **Check usage**: Verify which tokens are actively used
4. **Delete expired tokens**: Remove tokens past expiration date
5. **Audit organization tokens**: Check for unknown/rogue tokens
6. **Enable org policies**: Require expiration dates and approvals

---

## 🛡️ Security Considerations for Hermes Agent

### 1. Secrets Management in Hermes

#### Best Practices:
- ✅ Use `hermes-agent` secrets manager plugin
- ✅ Integrate with Bitwarden Secrets Manager for enterprise deployments
- ✅ Avoid storing plaintext tokens in configuration files
- ✅ Use environment variables for token passing
- ✅ Enable encryption at rest for sensitive environments

#### Detection and Setup:
```bash
# Use existing github-auth skill to auto-detect and configure
skill_view(name="github-auth")

# Hermes Agent will:
# 1. Detect available auth methods
# 2. Use gh CLI if available and authenticated
# 3. Fall back to fine-grained PAT from hermes storage
# 4. Guide user through setup if needed
```

### 2. Git Operations Security

#### Secure Git Command Wrapper:
```python
import subprocess
import os

def secure_git_operation(repo_url, operation, token=None):
    """
    Wrapper for git operations that prevents secret leakage
    """
    # Redact token from error messages
    REDACTED = "<REDACTED_TOKEN>"
    
    if token:
        url = repo_url.replace("https://", f"https://{os.environ.get('USERNAME', 'user')}:{REDACTED}@")
    else:
        url = repo_url
    
    cmd = ["git"]
    cmd.extend(operation.split())
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        
        # Check for authentication failures
        if "Authentication failed" in result.stderr:
            raise AuthenticationError("GitHub token expired or invalid")
            
        if "Permission denied" in result.stderr:
            raise PermissionError(f"Insufficient permissions: {result.stderr}")
            
        return result
    except subprocess.TimeoutExpired:
        raise TimeoutError("Git operation timed out")
```

### 3. Audit Logging

#### Track Git Operations:
```yaml
# Example: Audit all git operations by Hermes Agent
# Enable in hermes-agent configuration
skills:
  github-auth:
    audit_logging: true
    track_operations:
      - clone
      - push
      - pull
      - commit
  
  secrets:
    monitor_token_usage: true
    alert_on_expiry: true
```

### 4. Environment Segregation

#### Separate Environments for Different Operations:

| Environment | Token Type | Permissions | Rotation Schedule |
|-------------|------------|-------------|-------------------|
| **Development** | Fine-grained | Minimal (contents:read) | 30 days |
| **Test** | Fine-grained | Read/Write for test repos | 60 days |
| **Staging** | Fine-grained | Test deploy permissions | 30 days |
| **Production** | GitHub App token | Deployment permissions | 24 hours via OIDC |

### 5. Network Security

#### GitHub API Endpoints to Secure:
- `api.github.com` - REST API (use https only)
- `github.com` - Web interface
- `ssh.github.com` - SSH alternative (port 443 recommended)
- `objects.githubusercontent.com` - Git LFS objects

#### Secure Git Protocol:
```bash
# Force HTTPS for security
[url "https://github.com/"]
    insteadOf = git@github.com:

# For SSH key conflicts
[core]
    sshCommand = ssh -i ~/.ssh/ida_ed25519_hermes
```

---

## 🤖 Neo/Gopher Subagent Integration

### Hermes Agent Skill: git-actions-checkout

#### Skill Configuration:
```yaml
skills:
  git-actions-checkout:
    description: "Secure checkout and check-in for git repositories"
    auth_method: fine_grained_pat  # or ssh
    minimal_permissions: [
      {name: "contents", level: "read"},
      {name: "workflows", level: "read"}
    ]
    exceptions:
      - pull_requests:write
      - issues:write
    rotation: auto
    hermes:
      profile: production
      secrets_manager: hermes_secrets
      audit: true
```

### Neo Commands for Git Operations

#### Checkout Operation:
```bash
# Secure checkout with token management
neo git checkout --repo="owner/repo" --branch="main" \
  --token-source="hermes-secrets" \
  --permissions="contents:read"

# Equivalent:
git clone https://github.com/owner/repo.git && cd repo
git checkout main
```

#### Check-in Operation:
```bash
# Secure check-in with token usage
neo git checkin --message="Automated update from Hermes Agent" \
  --token-source="hermes-secrets" \
  --permissions="contents:write"

# Equivalent:
git add .
git commit -m "Automated update from Hermes Agent"
git push origin main
```

### Hermes Memory: Git Credentials

#### Store and Retrieve:
```bash
# Store git credentials
hmem git-store-credential --username="hermes-bot" \
  --email="automation@nousresearch.com" \
  --default-branch="main"

# Retrieve for operations
hmem git-get-credential --operation="checkout" --repo="owner/repo" \
  --required-permissions="contents:read"
```

---

## 📊 Security Checklist for Hermes Agent Setup

### Before Deployment:

- [ ] ✅ **Use fine-grained PAT** instead of classic PAT or SSH keys
- [ ] ✅ **Scope token to specific repositories** required for check-in/out
- [ ] ✅ **Set expiration date** (30-90 days, enterprise maximum)
- [ ] ✅ **Configure minimal permissions** (contents:read/write only)
- [ ] ✅ **Enable organization approval** for new tokens (if org policy allows)
- [ ] ✅ **Add token to Hermes secrets manager**
- [ ] ✅ **Configure audit logging** for git operations
- [ ] ✅ **Set up rotation automation** for production environments
- [ ] ✅ **Enable branch protection** for automation branches
- [ ] ✅ **Rotate all existing tokens** to fine-grained with expiry
- [ ] ✅ **Disable write access on SSH deploy keys** (being deprecated)

### During Setup:

- [ ] ✅ **Verify token works** with test git operation
- [ ] ✅ **Configure git identity** for automated commits
- [ ] ✅ **Set git credential helper** to store token securely
- [ ] ✅ **Test rotation script** on staging environment
- [ ] ✅ **Monitor initial use** for permission errors

### Post-Deployment Monitoring:

- [ ] ✅ **Set up Dependabot alerts** for action vulnerabilities
- [ ] ✅ **Monitor audit logs** for unusual git operations
- [ ] ✅ **Review token usage** weekly for expired/rotting tokens
- [ ] ✅ **Automate rotation reminders** 7 days before expiry
- [ ] ✅ **Enable security scanning** on automation repos
- [ ] ✅ **Rotate GitHub organization secrets** used by Hermes

### Security Hardening:

- [ ] ✅ **Use environment secrets with approval** for sensitive operations
- [ ] ✅ **Enable GitHub Advanced Security** if available
- [ ] ✅ **Configure repository rulesets** for automated repos
- [ ] ✅ **Disable legacy authentication** where possible
- [ ] ✅ **Use GitHub Apps** for production deployments (better than PATs)
- [ ] ✅ **Enable Secrets Scanning** to detect leaked tokens
- [ ] ✅ **Set up CodeQL analysis** for check-in repos
- [ ] ✅ **Implement SBOM scanning** for automation dependencies

---

## 🚨 Common Security Pitfalls & Solutions

| Pitfall | Impact | Solution |
|---------|--------|----------|
| **Using classic PAT with repo scope** | Accidental access to all org repos | Switch to fine-grained PAT scoped to only needed repos |
| **Hardcoding tokens in scripts** | Token exposure in code repositories | Use git credential helper or secrets manager |
| **No token expiration** | Long-term credential exposure | Set 30-90 day expiry, auto-rotate with reminder |
| **Using SSH keys with write access** | Deploy key write access disabled Dec 2025 | Use fine-grained PAT or GitHub App tokens |
| **Insufficient permissions** | Hermes Agent can't perform git operations | Carefully select minimal required permissions |
| **Token leakage in logs** | Secret visible in workflow outputs | Mask sensitive data, use redacted logging |
| **Shared tokens across environments** | Cross-environment credential compromise | Use separate tokens per environment with lifecycle |
| **Missing audit logs** | Cannot detect compromised credentials | Enable git operation auditing, review weekly |
| **No branch protection** | Unauthorized commits to protected branches | Set branch protection rules for automation repos |
| **Stale tokens in organization** | Unknown/rogue access remaining | Regular token audits, automation cleanup |

---

## 📚 Additional Resources

### Official GitHub Documentation:
- [GitHub Personal Access Tokens (Classic)](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)
- [Fine-Grained Personal Access Tokens](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens)
- [GitHub Security Features](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/about-githubs-security-features)
- [GitHub Advanced Security](https://docs.github.com/en/get-started/learning-about-github/about-github-advanced-security)

### Hermes Agent Integration:
- [Hermes Agent Skills Directory](https://github.com/NousResearch/hermes-agent/tree/main/skills)
- [GitHub Authentication Skill](https://github.com/NousResearch/hermes-agent/tree/main/skills/github/github-auth/SKILL.md)
- [Secrets Management Tools](https://github.com/NousResearch/hermes-agent/issues/410)

### Security Guides:
- [OpenSSF Scorecards](https://securityscorecards.dev/) - Evaluate GitHub actions security
- [GitHub Advisory Database](https://github.com/advisories) - Check for action vulnerabilities
- [Dependabot](https://docs.github.com/en/code-security/dependabot/working-with-dependabot) - Automate action updates

---

## 🔄 Version History

| Version | Date | Changes | Author |
|---------|------|---------|--------|
| 1.0 | June 2026 | Initial compilation of GitHub access best practices | Hermes Research Team |
| 1.1 | June 11 2026 | Added SSH key security warnings based on GitHub deprecation | Neo/Gopher Team |
| 2.0 | Planned Q3 2026 | Integration with GitHub Apps and OIDC tokens | Hermes Team |

---

## 📝 Notes

> **Important**: This guide replaces the existing `github-auth` skill documentation for 2026 security requirements. All future authentication setups should use fine-grained PATs instead of classic PATs or SSH keys.

> **Priority**: Update existing Hermes Agent/Gopher deployments to use fine-grained PATs before Q4 2026 when classic PAT deprecation takes effect.

> **Testing**: Verify all automation on staging environment before production deployment. Monitor initial token usage for permission errors.

> **Documentation**: Keep this guide updated with latest GitHub security features and Hermes Agent integration changes.