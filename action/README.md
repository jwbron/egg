# egg GitHub Action

Composite GitHub Action for running egg in CI/CD workflows.

## Overview

This action runs the egg autonomous coding agent within GitHub Actions. It sets up the gateway sidecar and sandbox container, passes a prompt to Claude Code, and outputs results.

## Files

| File | Purpose |
|------|---------|
| `action.yml` | Action metadata, inputs, outputs |
| `entrypoint.sh` | Main entry point that orchestrates container setup and execution |
| `build-mention-prompt.sh` | Builds structured prompts from GitHub @mention events |
| `generate-config.sh` | Generates runtime configuration from action inputs |

## Quick Start

```yaml
- uses: jwbron/egg@main
  with:
    prompt: "Fix the failing tests"
    anthropic-oauth-token: ${{ secrets.ANTHROPIC_OAUTH_TOKEN }}
```

## Documentation

For full usage documentation including all inputs, outputs, examples, and security considerations, see the [GitHub Action feature documentation](../docs/features/github-action.md).

For setting up @mention triggers, see the [@mention trigger setup guide](../docs/guides/mention-trigger-setup.md).
