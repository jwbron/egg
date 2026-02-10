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
| `build-review-prompt.sh` | Builds prompts for PR review workflows |
| `build-feedback-prompt.sh` | Builds prompts for addressing review feedback workflows |
| `build-autofixer-prompt.sh` | Builds prompts for autofixer workflows |
| `build-agent-mode-design-review-prompt.sh` | Builds prompts for agent-mode design reviews |
| `build-doc-updater-prompt.sh` | Builds prompts for documentation update workflows |
| `build-sdlc-prompt.sh` | Builds phase-specific prompts for SDLC pipeline workflows |
| `contract-state.sh` | Contract state management utility for SDLC pipeline |
| `populate-contract-tasks.py` | Populates contract tasks from plan document (runs before implement phase) |
| `generate-config.sh` | Generates runtime configuration from action inputs |

## Quick Start

```yaml
- uses: jwbron/egg/action@v0
  with:
    prompt: "Fix the failing tests"
    anthropic-oauth-token: ${{ secrets.ANTHROPIC_OAUTH_TOKEN }}
```

### Version Pinning

For stability, pin to a major version (receives all patch and minor updates):
```yaml
uses: jwbron/egg/action@v0
```

For full reproducibility, pin to an exact version:
```yaml
uses: jwbron/egg/action@v0.1.0
```

## Documentation

For design details, inputs, outputs, and implementation notes, see the [GitHub Actions Support ADR](../docs/adr/in-progress/ADR-GitHub-Actions-Support.md).

For the existing @mention trigger workflow, see [`.github/workflows/on-mention.yml`](../.github/workflows/on-mention.yml) and [`build-mention-prompt.sh`](build-mention-prompt.sh).

## See Also

- [GitHub Automation Guide](../docs/guides/github-automation.md) - Overview of all built-in workflows
- [Agent-Mode Design](../docs/guides/agent-mode-design.md) - Design principles for agent workflows
