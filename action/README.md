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

For design details, inputs, outputs, and implementation notes, see the [GitHub Actions Support ADR](../docs/adr/in-progress/ADR-GitHub-Actions-Support.md).

For the existing @mention trigger workflow, see [`.github/workflows/on-mention.yml`](../.github/workflows/on-mention.yml) and [`build-mention-prompt.sh`](build-mention-prompt.sh).
