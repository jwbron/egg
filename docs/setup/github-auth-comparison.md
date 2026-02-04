# GitHub Authentication: App vs Personal Access Token

This document explains the differences between GitHub App authentication and Personal Access Tokens (PATs) for egg.

## Summary

**Both GitHub App and PAT authentication are supported.** However, GitHub Apps are required for certain operations that PATs cannot perform.

## Authentication Methods

### Personal Access Token (PAT)

**What it is:** A user-level token that grants permissions based on your personal GitHub account.

**Supported operations:**
- ✅ Clone repositories
- ✅ Push commits
- ✅ Create pull requests
- ✅ Comment on PRs
- ✅ Trigger workflows
- ✅ Read repository contents
- ❌ **Cannot read PR check runs** (requires GitHub App)

**When to use:**
- Personal projects
- Simple setups
- You don't need PR check status monitoring

**Setup:**
1. Go to https://github.com/settings/tokens
2. Create a token with:
   - **Read/write repos:** `repo` (full scope), `workflow`
   - **Read-only repos:** `repo` (read-only)
3. Use token in setup: `GITHUB_TOKEN=ghp_...`

### GitHub App

**What it is:** An application-level token that grants permissions to specific repositories independent of user accounts.

**Supported operations:**
- ✅ All PAT operations (clone, push, PRs, comments, workflows)
- ✅ **Read PR check runs** (via GitHub Checks API)
- ✅ Team collaboration (not tied to a single user)
- ✅ Fine-grained repository permissions

**When to use:**
- Team projects
- You need automatic token refresh (handled by gateway sidecar)
- Fine-grained permission control
- Production deployments

**Setup:**
See [github-app-setup.md](github-app-setup.md) for detailed instructions.

## Authentication Usage Audit

### Where PATs are used:

1. **Container authentication** (`egg` script, `entrypoint.py`)
   - Sets `GITHUB_TOKEN` environment variable
   - Configures `gh` CLI
   - Configures git credential helper

2. **Read-only monitoring** (`github_readonly_token` in `host_config.py`)
   - For repositories where jib only watches but doesn't write
   - Falls back to `GITHUB_TOKEN` if not set

3. **All write operations** (PRs, commits, comments)
   - Uses `GITHUB_TOKEN` from environment or config

### Where GitHub Apps are required:

1. **Token auto-refresh** (gateway sidecar `token_refresher.py`)
   - GitHub App tokens expire after 1 hour
   - Gateway sidecar auto-refreshes tokens 15 minutes before expiry
   - Not needed for PATs (they don't expire automatically)

2. **Fine-grained repository permissions**
   - GitHub Apps can be installed on specific repositories
   - Better security model for team environments

## Recommendations

### For Personal Use
```bash
# Use PAT if you don't need PR check monitoring
GITHUB_TOKEN=ghp_... (read/write)
GITHUB_READONLY_TOKEN=ghp_... (optional, for external repos)
```

### For Team Use
```bash
# Use GitHub App for full functionality
# Configure via setup.py option 1
# Tokens auto-refresh via gateway sidecar
```

### Hybrid Setup (Current Implementation)
```bash
# You can mix both:
# 1. GitHub App for repositories where you want check monitoring
# 2. PAT for repositories where you only need basic operations

# Both authentication methods work side-by-side
# The system will use GitHub App tokens when available,
# fall back to PAT otherwise
```

## Technical Details

### Token Precedence

For bot mode, tokens are managed by the gateway sidecar's in-memory token refresher.

For user mode, tokens are loaded from:
1. `GITHUB_USER_TOKEN` environment variable
2. `~/.config/egg/secrets.env` - `GITHUB_USER_TOKEN`

### Why PATs Can't Access Check Runs

The GitHub Checks API requires:
- `checks:read` permission (only available to GitHub Apps)
- PATs use OAuth scopes which don't include check runs access

From GitHub's documentation:
> "The Checks API is only available to GitHub Apps. OAuth Apps and authenticated users cannot access this endpoint."

## Migration Path

If you're currently using PATs and want PR check monitoring:

1. Set up a GitHub App (see [github-app-setup.md](github-app-setup.md))
2. Configure App credentials in `~/.config/egg/`:
   - `github-app-id`
   - `github-app-installation-id`
   - `github-app.pem`
3. Restart the gateway sidecar: `systemctl --user restart gateway`
4. Keep your PAT as `GITHUB_READONLY_TOKEN` for external repos (optional)

The gateway sidecar will automatically manage token refresh for the GitHub App.

## Related Documentation

- [GitHub App Setup](github-app-setup.md)
- [Host Configuration](../../config/README.md)
