# CI Check Autofixer

This guide explains how the CI autofixer workflow automatically diagnoses and fixes CI failures on egg-owned PRs.

## Overview

When a CI check (e.g., Lint) fails on a PR owned by egg, a `workflow_run` event triggers the autofixer workflow. The workflow:

1. Verifies the failure is on an egg-owned branch (not human-authored)
2. Checks loop prevention guards (max 2 consecutive autofix attempts)
3. Fetches failure logs from the GitHub Actions API
4. Builds a diagnostic prompt with logs, PR context, and changed files
5. Invokes the egg action to analyze and fix the failures
6. Posts a result comment on the PR

## Architecture

```
CI check fails on egg-owned PR
  → workflow_run event fires
    → on-check-failure.yml
      → Guard: check consecutive egg commits (max 2)
      → Fetch failure logs from GitHub API
      → Build diagnostic prompt (build-check-failure-prompt.sh)
      → Run egg action to fix
      → Post result comment on PR
```

## Trigger

The workflow uses `workflow_run` rather than `check_suite` because:
- `workflow_run` gives the workflow name and run ID directly
- We can filter to specific workflows and ignore others (release, action tests)
- Logs are accessible via the GitHub API using the run ID
- `check_suite` fires for every individual check, leading to duplicate triggers

## Loop Prevention

Multiple safeguards prevent infinite fix-fail-fix cycles:

1. **Max attempts counter**: Counts consecutive commits authored by egg on the branch. After 2 consecutive autofix attempts without a passing run, the workflow posts an escalation comment requesting human review.

2. **Concurrency group**: Uses `concurrency: { group: egg-autofix-{branch}, cancel-in-progress: true }` so only one autofix runs per branch at a time.

3. **`disable_auto_fix` config**: The existing `disable_auto_fix` repo setting in `repositories.yaml` can be used to disable the autofixer per-repo.

## Phased Rollout

### Phase 1 (current) — Lint autofixer

Only triggers on `Lint` workflow failures. Lint fixes are mechanical (formatting, import ordering, type annotations) with high success rates.

### Phase 2 — Test autofixer

Extend the `workflows` list to include `Test`. Test failures require deeper analysis and may need more context in the prompt.

### Phase 3 — Security scan autofixer

Extend to `Security Scan` workflow (bandit). Security fixes need careful review.

To enable additional phases, add workflow names to the trigger:

```yaml
on:
  workflow_run:
    workflows: ["Lint", "Test"]  # Phase 2
    types: [completed]
```

## Files

| File | Purpose |
|------|---------|
| `.github/workflows/on-check-failure.yml` | Workflow triggered by CI failures |
| `action/build-check-failure-prompt.sh` | Builds diagnostic prompt from failure logs |
| `action/lib.sh` | Shared helpers (truncation, API calls, prompt output) |

## Prerequisites

Uses the same secrets as the @mention trigger:

| Secret | Description |
|--------|-------------|
| `ANTHROPIC_OAUTH_TOKEN` | Anthropic OAuth token for Claude API access |
| `BOT_APP_ID` | GitHub App ID for the bot identity |
| `BOT_APP_PRIVATE_KEY` | GitHub App private key (PEM format) |
| `BOT_APP_INSTALLATION_ID` | GitHub App installation ID |

## Configuration

### Branch Ownership

The autofixer only runs on branches prefixed with:
- `egg-` or `egg/`
- `james-in-a-box-` or `james-in-a-box/`

This prevents the autofixer from modifying human-authored PRs.

### Disabling the Autofixer

Set `disable_auto_fix: true` in your `repositories.yaml`:

```yaml
repo_settings:
  owner/repo:
    disable_auto_fix: true
```

### Adjusting Max Attempts

Edit the `MAX_ATTEMPTS` variable in the "Check autofix attempt count" step of `on-check-failure.yml`.

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Autofixer doesn't trigger | Verify the branch prefix matches (`egg-`, `james-in-a-box-`, etc.) and the failed workflow name matches the `workflows` list |
| Autofixer keeps looping | Check the guard step — it should stop after 2 consecutive egg commits. Verify git author is set to "egg" |
| Logs unavailable in prompt | Ensure the workflow has `actions: read` permission. Check that `gh api` can access the run logs |
| Escalation comment posted | The autofixer gave up after max attempts. Review the CI failures manually |

## Security Considerations

- **Trusted prompt building**: Like the @mention workflow, `build-check-failure-prompt.sh` runs from the `main` branch checkout, not the PR branch. This prevents a malicious PR from replacing the script to exfiltrate the bot token.
- **Branch restriction**: Only egg-owned branches trigger the autofixer, preventing unauthorized modifications.
- **Short-lived tokens**: Bot tokens are generated per-run with 1-hour TTL via `actions/create-github-app-token`.
