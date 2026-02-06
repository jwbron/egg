# GitHub Authentication Setup

This guide covers setting up GitHub authentication for egg using a dedicated user account and Personal Access Token (PAT).

## Overview

Egg uses a dedicated GitHub user account (e.g., `james-in-a-box`) for all GitHub operations. This account is authenticated via a fine-grained Personal Access Token (PAT).

## Required Permissions

The PAT requires the following permissions on the target repositories:

### Read-Only Permissions

| Permission | Purpose |
|------------|--------|
| **Actions** | Read workflow run status and logs |
| **Checks** | Read check run status for CI/CD monitoring |
| **Commit statuses** | Read commit status indicators |

### Read-Write Permissions

| Permission | Purpose |
|------------|--------|
| **Contents** | Push commits, create/update files |
| **Pull requests** | Create PRs, add comments, request reviews |
| **Workflows** | Trigger and manage GitHub Actions workflows |

## Setup Steps

### 1. Create Dedicated GitHub Account

1. Create a new GitHub account for the agent (e.g., `james-in-a-box`)
2. Add a bio disclosing it's a machine account (GitHub ToS requirement)
3. Add the account as a collaborator on your repositories

### 2. Create Fine-Grained PAT

1. Log in as the dedicated account
2. Go to Settings > Developer settings > Personal access tokens > Fine-grained tokens
3. Click "Generate new token"
4. Configure:
   - **Name**: `egg-gateway`
   - **Expiration**: Set an appropriate expiration
   - **Repository access**: Select the repositories egg needs access to
   - **Permissions**: Set as listed above

### 3. Configure Token

Add the token to your secrets configuration:

```bash
# In ~/.config/egg/secrets.env
GITHUB_TOKEN="github_pat_..."
GATEWAY_BOT_NAME="james-in-a-box"
GATEWAY_BOT_BRANCH_PREFIX="egg"
```

The `gh` CLI and `git push` automatically use this token for authentication.

## Verifying Setup

```bash
# Check token scopes
gh auth status

# Test PR creation (dry-run)
gh pr create --dry-run --title "Test" --body "Test"

# Test workflow access
gh workflow list
```

## Troubleshooting

### "Resource not accessible by integration" Error

This typically means a permission is missing. Check:
1. PAT permissions in GitHub Settings
2. Repository access scope
3. Token validity / expiration

### Contents Permission Errors

If pushes fail:
1. Verify "Contents" permission is "Read and write"
2. Check branch protection rules
3. Ensure the account has collaborator access on the target repository

## Related Documentation

- [Architecture Overview](../architecture/) - System design
