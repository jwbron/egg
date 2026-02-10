# Reusable SDLC Workflows

This guide explains how to use egg's SDLC workflows in your own repositories.

## Overview

The egg project provides a set of reusable GitHub Actions workflows for AI-powered code review, autofix, conflict resolution, and SDLC pipeline management. These workflows can be called from any repository that has the required secrets configured.

## Version Pinning

All workflow examples below use `@main`. After the first release (v0.1.0) creates the `@v0` tag, switch to `@v0` for stability.

**For stability** (recommended after first release), pin to a major version:
```yaml
uses: jwbron/egg/.github/workflows/reusable-review.yml@v0
```

**For full reproducibility**, pin to an exact version:
```yaml
uses: jwbron/egg/.github/workflows/reusable-review.yml@v0.1.0
```

**For latest development** (not recommended for production):
```yaml
uses: jwbron/egg/.github/workflows/reusable-review.yml@main
```

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
      bot_username: my-bot-username  # GitHub username of your bot
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
      bot_username: my-bot-username
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
      bot_username: my-bot-username
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
      bot_username: my-bot-username
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
      bot_username: my-bot-username
      authorized_users: "user1,user2"
      mention_patterns: "@my-bot,@mybot"
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
      bot_username: my-bot-username
      branch_prefix: "my-bot"  # Creates my-bot/issue-N branches
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

| Parameter | Description | Default |
|-----------|-------------|---------|
| `bot_username` | GitHub username of your bot | `egg` |
| `action_ref` | Reference to egg action (documentation only; see note) | `jwbron/egg/action@main` |
| `authorized_users` | Comma-separated list of authorized users | `jwbron` |
| `branch_prefix` | Prefix for issue branches | `egg` |
| `timeout` | Timeout in minutes | varies by workflow |

## Repository Variables

For easier configuration across multiple workflows, you can set `BOT_USERNAME` as a GitHub Actions repository variable instead of hardcoding it in each workflow file.

### Setting Up `BOT_USERNAME`

1. Go to your repository's **Settings** → **Secrets and variables** → **Actions**
2. Click the **Variables** tab
3. Click **New repository variable**
4. Set **Name** to `BOT_USERNAME` and **Value** to your bot's GitHub username

### Using in Workflows

There are two types of workflows that use `BOT_USERNAME`:

1. **Entry-point workflows** (e.g., `on-pull-request.yml`, `on-check-failure.yml`) — These use `vars.BOT_USERNAME` directly from the repository variable.

2. **Reusable workflows** (e.g., `reusable-review.yml`, `reusable-autofix.yml`) — These receive `bot_username` via the `with:` input block from calling workflows.

For entry-point workflows, reference the variable directly:

```yaml
# In an entry-point workflow (e.g., on-pull-request.yml)
jobs:
  review:
    uses: ./.github/workflows/reusable-review.yml
    with:
      pr_number: ${{ github.event.pull_request.number }}
      bot_name: review
      bot_username: ${{ vars.BOT_USERNAME || 'egg' }}
      # ...
```

When calling reusable workflows from external repositories, pass `bot_username` via the `with:` block:

```yaml
# In your repository's caller workflow
jobs:
  review:
    uses: jwbron/egg/.github/workflows/reusable-review.yml@main
    with:
      pr_number: ${{ github.event.pull_request.number }}
      bot_name: review
      bot_username: ${{ vars.BOT_USERNAME || 'egg' }}
      # ...
```

The `|| 'egg'` fallback ensures the workflow runs even if the variable isn't set.

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
      bot_username: my-review-bot
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
