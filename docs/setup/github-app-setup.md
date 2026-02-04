# GitHub App Setup

This guide covers setting up the `egg` GitHub App for automated PR creation and workflow management.

## Required Permissions

The GitHub App requires the following permissions to function properly:

### Read-Only Permissions

| Permission | Purpose |
|------------|--------|
| **Actions** | Read workflow run status and logs |
| **Checks** | Read check run status for CI/CD monitoring |
| **Commit statuses** | Read commit status indicators |
| **Dependabot alerts** | Read security vulnerability alerts |
| **Discussions** | Read repository discussions |
| **Merge queues** | Read merge queue status |

### Read-Write Permissions

| Permission | Purpose |
|------------|--------|
| **Contents** | Push commits, create/update files |
| **Pull requests** | Create PRs, add comments, request reviews |
| **Workflows** | Trigger and manage GitHub Actions workflows |

## Installation Steps

### 1. Create GitHub App

1. Go to GitHub Settings > Developer settings > GitHub Apps
2. Click "New GitHub App"
3. Configure:
   - **Name**: `egg`
   - **Homepage URL**: Your documentation URL
   - **Webhook**: Disable unless needed

### 2. Set Permissions

Under "Permissions & events":

**Repository permissions:**
- Actions: Read-only
- Checks: Read-only
- Commit statuses: Read-only
- Contents: Read and write
- Dependabot alerts: Read-only
- Discussions: Read-only
- Merge queues: Read-only
- Pull requests: Read and write
- Workflows: Read and write

### 3. Generate Private Key

1. Scroll to "Private keys"
2. Click "Generate a private key"
3. Save the `.pem` file securely

### 4. Install App

1. Go to the "Install App" tab
2. Select the repositories to grant access to
3. Confirm installation

### 5. Configure Token

The GitHub token is configured in your environment:

```bash
# In your shell config or .env
export GITHUB_TOKEN="your-token-here"
```

The `gh` CLI and `git push` automatically use this token for authentication.

## Verifying Permissions

To verify the app has correct permissions:

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
1. App permissions in GitHub Settings
2. Installation scope (which repos have access)
3. Token validity

### Workflow Permission Errors

If you see errors like "refusing to allow a GitHub App to create or update workflow":
1. Ensure "Workflows" permission is set to "Read and write"
2. Re-install the app to pick up new permissions

### Contents Permission Errors

If pushes fail:
1. Verify "Contents" permission is "Read and write"
2. Check branch protection rules
3. Ensure the app is installed on the target repository

## Related Documentation

- [Slack App Setup](slack-app-setup.md) - Slack integration configuration
- [Architecture Overview](../architecture/) - System design
