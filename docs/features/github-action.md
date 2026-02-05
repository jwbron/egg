# GitHub Action

Run egg as a GitHub Action for CI/CD automation.

## Overview

egg provides a composite GitHub Action that runs the autonomous coding agent within GitHub Actions workflows. This enables automated code changes triggered by events like @mentions, issue comments, or scheduled workflows.

## Usage

```yaml
- uses: jwbron/egg@main
  with:
    prompt: "Fix the failing tests in src/utils.ts"
    anthropic-oauth-token: ${{ secrets.ANTHROPIC_OAUTH_TOKEN }}
    github-token: ${{ secrets.GITHUB_TOKEN }}
```

## Inputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `prompt` | Yes | - | Task prompt for Claude Code |
| `anthropic-oauth-token` | Yes | - | Anthropic OAuth token for Claude API |
| `github-token` | Yes | `${{ github.token }}` | GitHub token for git operations |
| `bot-app-id` | No | - | GitHub App ID for bot identity |
| `bot-app-private-key` | No | - | GitHub App private key (PEM) |
| `bot-app-installation-id` | No | - | GitHub App installation ID |
| `bot-username` | No | `egg` | Bot GitHub username (for self-comment filtering) |
| `mode` | No | `auto` | Network mode: `public`, `private`, or `auto` |
| `timeout` | No | `30` | Timeout in minutes |
| `model` | No | `opus` | Claude model to use |
| `image-tag` | No | `latest` | Docker image tag |

## Outputs

| Output | Description |
|--------|-------------|
| `exit-code` | Sandbox container exit code (0 = success) |
| `pr-url` | URL of created PR, if any |
| `log-file` | Path to full Claude output log |

## Network Modes

| Mode | Behavior |
|------|----------|
| `auto` | Detects from repository visibility (public repo = public mode, private = private) |
| `public` | Full internet access, public GitHub repos |
| `private` | Anthropic API only, private GitHub repos only |

## Bot Mode vs Token Mode

**Token mode** (default): Uses the provided `github-token` for all operations. Simple but limited to the token's permissions.

**Bot mode**: Uses GitHub App credentials (`bot-app-id`, `bot-app-private-key`, `bot-app-installation-id`) for identity. The gateway generates short-lived tokens automatically. Recommended for production use.

## Example: @mention Trigger

The most common use case is triggering egg when mentioned in GitHub comments. See the [@mention trigger setup guide](../guides/mention-trigger-setup.md) for a complete walkthrough.

```yaml
name: egg @mention
on:
  issue_comment:
    types: [created]
  pull_request_review_comment:
    types: [created]

jobs:
  respond:
    if: contains(github.event.comment.body, '@my-bot')
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: jwbron/egg@main
        with:
          prompt: ${{ steps.build-prompt.outputs.prompt }}
          anthropic-oauth-token: ${{ secrets.ANTHROPIC_OAUTH_TOKEN }}
          bot-app-id: ${{ secrets.BOT_APP_ID }}
          bot-app-private-key: ${{ secrets.BOT_APP_PRIVATE_KEY }}
          bot-app-installation-id: ${{ secrets.BOT_APP_INSTALLATION_ID }}
```

## Security

- The gateway sidecar runs alongside the sandbox within the Action, enforcing the same policies as local execution
- Branch ownership rules still apply (agent can only push to `egg/*` branches)
- Merge operations are still blocked
- In private mode, network access is restricted to Anthropic API only

## Source Files

| File | Description |
|------|-------------|
| [`action/action.yml`](../../action/action.yml) | Action metadata and input/output definitions |
| [`action/entrypoint.sh`](../../action/entrypoint.sh) | Action entry point script |
| [`action/build-mention-prompt.sh`](../../action/build-mention-prompt.sh) | Prompt builder for @mention events |
| [`action/generate-config.sh`](../../action/generate-config.sh) | Config generator for the action |

## Related Documentation

- [@mention Trigger Setup](../guides/mention-trigger-setup.md) - Setting up @mention triggers
- [ADR: GitHub Actions Support](../adr/in-progress/ADR-GitHub-Actions-Support.md) - Design decisions
- [GitHub Actions Implementation Plan](../plans/github-actions-implementation-plan.md) - Implementation details
