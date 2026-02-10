# Reusable SDLC Workflows

This guide explains how to use egg's SDLC workflows in your own repositories.

## Overview

The egg project provides a set of reusable GitHub Actions workflows for AI-powered code review, autofix, conflict resolution, and SDLC pipeline management. These workflows can be called from any repository that has the required secrets configured.

## Available Workflows

### Core Review Workflow

**`reusable-review.yml`** - The foundation for all AI review bots.

```yaml
jobs:
  review:
    uses: jwbron/egg/.github/workflows/reusable-review.yml@main
    with:
      pr_number: ${{ github.event.pull_request.number }}
      bot_name: my-bot
      bot_username: ${{ vars.EGG_BOT_USERNAME }}  # REQUIRED
      branch_prefix: ${{ vars.EGG_BRANCH_PREFIX }}  # REQUIRED
      # action_ref: jwbron/egg/action@main  # Cannot be dynamic; see note below
      prompt_script: path/to/build-review-prompt.sh
      timeout: "10"
    secrets:
      BOT_APP_ID: ${{ secrets.BOT_APP_ID }}
      BOT_APP_PRIVATE_KEY: ${{ secrets.BOT_APP_PRIVATE_KEY }}
      BOT_APP_INSTALLATION_ID: ${{ secrets.BOT_APP_INSTALLATION_ID }}
      ANTHROPIC_OAUTH_TOKEN: ${{ secrets.ANTHROPIC_OAUTH_TOKEN }}
```

### Autofix Workflow

**`reusable-autofix.yml`** - Automatically fix failing checks.

```yaml
jobs:
  autofix:
    uses: jwbron/egg/.github/workflows/reusable-autofix.yml@main
    with:
      pr_number: ${{ github.event.workflow_run.pull_requests[0].number }}
      failed_workflow: ${{ github.event.workflow_run.name }}
      failed_run_id: ${{ github.event.workflow_run.id }}
      bot_username: ${{ vars.EGG_BOT_USERNAME }}
      branch_prefix: ${{ vars.EGG_BRANCH_PREFIX }}
      timeout: "20"
    secrets:
      BOT_APP_ID: ${{ secrets.BOT_APP_ID }}
      BOT_APP_PRIVATE_KEY: ${{ secrets.BOT_APP_PRIVATE_KEY }}
      BOT_APP_INSTALLATION_ID: ${{ secrets.BOT_APP_INSTALLATION_ID }}
      ANTHROPIC_OAUTH_TOKEN: ${{ secrets.ANTHROPIC_OAUTH_TOKEN }}
```

### Conflict Resolution Workflow

**`reusable-conflict-resolve.yml`** - Resolve merge conflicts automatically.

```yaml
jobs:
  resolve:
    uses: jwbron/egg/.github/workflows/reusable-conflict-resolve.yml@main
    with:
      pr_number: ${{ matrix.pr }}
      bot_username: ${{ vars.EGG_BOT_USERNAME }}
      branch_prefix: ${{ vars.EGG_BRANCH_PREFIX }}
      timeout: "30"
    secrets:
      BOT_APP_ID: ${{ secrets.BOT_APP_ID }}
      BOT_APP_PRIVATE_KEY: ${{ secrets.BOT_APP_PRIVATE_KEY }}
      BOT_APP_INSTALLATION_ID: ${{ secrets.BOT_APP_INSTALLATION_ID }}
      ANTHROPIC_OAUTH_TOKEN: ${{ secrets.ANTHROPIC_OAUTH_TOKEN }}
```

### Review Feedback Workflow

**`on-review-feedback.yml`** - Address review feedback on bot-authored PRs.

```yaml
jobs:
  feedback:
    uses: jwbron/egg/.github/workflows/on-review-feedback.yml@main
    with:
      pr_number: ${{ github.event.pull_request.number }}
      bot_username: ${{ vars.EGG_BOT_USERNAME }}
      branch_prefix: ${{ vars.EGG_BRANCH_PREFIX }}
      authorized_users: "user1,user2"  # Comma-separated list
      max_feedback_rounds: 3
    secrets:
      BOT_APP_ID: ${{ secrets.BOT_APP_ID }}
      BOT_APP_PRIVATE_KEY: ${{ secrets.BOT_APP_PRIVATE_KEY }}
      BOT_APP_INSTALLATION_ID: ${{ secrets.BOT_APP_INSTALLATION_ID }}
      ANTHROPIC_OAUTH_TOKEN: ${{ secrets.ANTHROPIC_OAUTH_TOKEN }}
```

### Mention Response Workflow

**`on-mention.yml`** - Respond when the bot is mentioned.

```yaml
jobs:
  respond:
    uses: jwbron/egg/.github/workflows/on-mention.yml@main
    with:
      issue_or_pr_number: ${{ github.event.issue.number }}
      bot_username: ${{ vars.EGG_BOT_USERNAME }}
      branch_prefix: ${{ vars.EGG_BRANCH_PREFIX }}
      authorized_users: "user1,user2"
    secrets:
      BOT_APP_ID: ${{ secrets.BOT_APP_ID }}
      BOT_APP_PRIVATE_KEY: ${{ secrets.BOT_APP_PRIVATE_KEY }}
      BOT_APP_INSTALLATION_ID: ${{ secrets.BOT_APP_INSTALLATION_ID }}
      ANTHROPIC_OAUTH_TOKEN: ${{ secrets.ANTHROPIC_OAUTH_TOKEN }}
```

### SDLC Pipeline

**`sdlc-pipeline.yml`** - Full SDLC pipeline for issue-to-PR automation.

```yaml
jobs:
  pipeline:
    uses: jwbron/egg/.github/workflows/sdlc-pipeline.yml@main
    with:
      issue_number: ${{ github.event.issue.number }}
      bot_username: ${{ vars.EGG_BOT_USERNAME }}
      branch_prefix: ${{ vars.EGG_BRANCH_PREFIX }}  # Creates <prefix>/issue-N branches
    secrets:
      BOT_APP_ID: ${{ secrets.BOT_APP_ID }}
      BOT_APP_PRIVATE_KEY: ${{ secrets.BOT_APP_PRIVATE_KEY }}
      BOT_APP_INSTALLATION_ID: ${{ secrets.BOT_APP_INSTALLATION_ID }}
      ANTHROPIC_OAUTH_TOKEN: ${{ secrets.ANTHROPIC_OAUTH_TOKEN }}
```

The pipeline is triggered by applying the `sdlc:refine` label to an issue. You can set up the SDLC labels in your repository using the setup script:

```bash
.github/scripts/setup-sdlc-labels.sh --repo owner/repo
```


## Common Parameters

| Parameter | Description | Required |
|-----------|-------------|----------|
| `bot_username` | GitHub username of your bot | Yes |
| `branch_prefix` | Prefix for bot-owned branches | Yes |
| `action_ref` | Reference to egg action (documentation only; see note) | No |
| `authorized_users` | Comma-separated list of authorized users | No (default: `jwbron`) |
| `timeout` | Timeout in minutes | No (varies by workflow) |

## Repository Variables

**REQUIRED**: Event-triggered workflows require these repository variables to be configured:

| Variable | Purpose | Example |
|----------|---------|---------|
| `EGG_BOT_USERNAME` | Bot's GitHub username | `james-in-a-box[bot]` |
| `EGG_BRANCH_PREFIX` | Branch prefix for bot-owned branches | `egg` |

### Setting Up Repository Variables

1. Go to your repository's **Settings** → **Secrets and variables** → **Actions**
2. Click the **Variables** tab
3. Click **New repository variable**
4. Add both `EGG_BOT_USERNAME` and `EGG_BRANCH_PREFIX` with appropriate values

**Note**: Workflows will fail with a validation error if these variables are not set.

### Using in Workflows

There are two types of workflows that use bot identity:

1. **Entry-point workflows** (e.g., `on-pull-request.yml`, `on-check-failure.yml`) — These use `vars.EGG_BOT_USERNAME` and `vars.EGG_BRANCH_PREFIX` directly from repository variables.

2. **Reusable workflows** (e.g., `reusable-review.yml`, `reusable-autofix.yml`) — These receive `bot_username` and `branch_prefix` via the `with:` input block from calling workflows.

For entry-point workflows, reference the variables directly:

```yaml
# In an entry-point workflow (e.g., on-pull-request.yml)
jobs:
  review:
    uses: ./.github/workflows/reusable-review.yml
    with:
      pr_number: ${{ github.event.pull_request.number }}
      bot_name: review
      bot_username: ${{ vars.EGG_BOT_USERNAME }}
      branch_prefix: ${{ vars.EGG_BRANCH_PREFIX }}
      # ...
```

When calling reusable workflows from external repositories, pass these values via the `with:` block:

```yaml
# In your repository's caller workflow
jobs:
  review:
    uses: jwbron/egg/.github/workflows/reusable-review.yml@main
    with:
      pr_number: ${{ github.event.pull_request.number }}
      bot_name: review
      bot_username: ${{ vars.EGG_BOT_USERNAME }}
      branch_prefix: ${{ vars.EGG_BRANCH_PREFIX }}
      # ...
```

## Important Limitations

### `action_ref` Parameter

GitHub Actions' `uses:` field cannot accept dynamic expressions. The `action_ref` parameter is provided for documentation and wrapper workflow use, but you cannot pass it dynamically to change which action is used.

**Workaround:** If you need to use a different action reference, you must:
1. Fork the reusable workflow
2. Update the `uses:` line directly in your fork
3. Reference your forked workflow

### Secret Names

The workflows expect specific secret names. You must configure these secrets in your repository:

- `BOT_APP_ID` - GitHub App ID
- `BOT_APP_PRIVATE_KEY` - GitHub App private key
- `BOT_APP_INSTALLATION_ID` - GitHub App installation ID
- `ANTHROPIC_OAUTH_TOKEN` - Anthropic API token

## Example: Complete Review Bot Setup

Here's a complete example of setting up a code review bot:

```yaml
# .github/workflows/review.yml
name: AI Code Review

on:
  pull_request:
    types: [opened, synchronize, reopened]

jobs:
  review:
    permissions:
      contents: read
      pull-requests: write
    uses: jwbron/egg/.github/workflows/reusable-review.yml@main
    with:
      pr_number: ${{ github.event.pull_request.number }}
      bot_name: review
      bot_username: ${{ vars.EGG_BOT_USERNAME }}
      branch_prefix: ${{ vars.EGG_BRANCH_PREFIX }}
      timeout: "10"
    secrets:
      BOT_APP_ID: ${{ secrets.BOT_APP_ID }}
      BOT_APP_PRIVATE_KEY: ${{ secrets.BOT_APP_PRIVATE_KEY }}
      BOT_APP_INSTALLATION_ID: ${{ secrets.BOT_APP_INSTALLATION_ID }}
      ANTHROPIC_OAUTH_TOKEN: ${{ secrets.ANTHROPIC_OAUTH_TOKEN }}
```

## Custom Prompt Scripts

Most workflows accept a `prompt_script` parameter to customize the prompt generation. Create your own prompt builder script that:

1. Writes the prompt to a file
2. Sets the `prompt-file` output variable
3. Optionally sets the `model` output variable

Example:
```bash
#!/bin/bash
# action/my-review-prompt.sh

PROMPT_FILE="/tmp/review-prompt.md"
cat > "$PROMPT_FILE" << 'EOF'
Review this PR for code quality and security issues.
Focus on:
- Type safety
- Error handling
- Security vulnerabilities
EOF

echo "prompt-file=$PROMPT_FILE" >> "$GITHUB_OUTPUT"
echo "model=opus" >> "$GITHUB_OUTPUT"
```

## Security Considerations

1. **Trusted checkout:** Review workflows check out the main branch first for prompt building to prevent malicious PRs from injecting content.

2. **Authorization:** Most workflows include authorization checks. Configure `authorized_users` to restrict who can trigger bot actions.

3. **Self-trigger prevention:** Workflows automatically prevent the bot from triggering itself.

4. **Fork handling:** Push-based workflows skip fork PRs since bots cannot push to forks.
