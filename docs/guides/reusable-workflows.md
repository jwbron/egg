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
      sdlc_label: "my-sdlc"
    secrets:
      BOT_APP_ID: ${{ secrets.BOT_APP_ID }}
      BOT_APP_PRIVATE_KEY: ${{ secrets.BOT_APP_PRIVATE_KEY }}
      BOT_APP_INSTALLATION_ID: ${{ secrets.BOT_APP_INSTALLATION_ID }}
      ANTHROPIC_OAUTH_TOKEN: ${{ secrets.ANTHROPIC_OAUTH_TOKEN }}
```

## Common Parameters

| Parameter | Description | Default |
|-----------|-------------|---------|
| `bot_username` | GitHub username of your bot | `james-in-a-box` |
| `action_ref` | Reference to egg action (documentation only; see note) | `jwbron/egg/action@main` |
| `authorized_users` | Comma-separated list of authorized users | `jwbron` |
| `branch_prefix` | Prefix for issue branches | `egg` |
| `timeout` | Timeout in minutes | varies by workflow |

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
