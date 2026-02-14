# egg GitHub Action

Composite GitHub Action for running egg in CI/CD workflows.

## Overview

This action runs the egg autonomous coding agent within GitHub Actions. It sets up the gateway sidecar and sandbox container, passes a prompt to Claude Code, and outputs results.

## Files

| File | Purpose |
|------|---------|
| `action.yml` | Action metadata, inputs, outputs |
| `entrypoint.sh` | Main entry point that orchestrates container setup and execution |
| `generate-config.sh` | Generates runtime configuration from action inputs |
| **Prompt Builders** | |
| `build-mention-prompt.sh` | Builds structured prompts from GitHub @mention events |
| `build-review-prompt.sh` | Builds prompts for PR review workflows |
| `build-feedback-prompt.sh` | Builds prompts for addressing review feedback workflows |
| `build-autofixer-prompt.sh` | Builds prompts for autofixer workflows |
| `build-agent-mode-design-review-prompt.sh` | Builds prompts for agent-mode design reviews |
| `build-doc-updater-prompt.sh` | Builds prompts for documentation update workflows |
| `build-onboarding-doc-prompt.sh` | Builds prompts for full-repo documentation onboarding |
| `build-sdlc-prompt.sh` | Builds phase-specific prompts for SDLC pipeline workflows |
| `build-unified-review-prompt.sh` | Unified review prompt builder for all SDLC phases |
| `build-conflict-prompt.sh` | Builds prompts for merge conflict resolution |
| `build-contract-verification-prompt.sh` | Builds prompts for contract verification reviews |
| **Role-Specific Prompt Builders** | |
| `build-coder-prompt.sh` | Coder agent prompt builder |
| `build-tester-prompt.sh` | Tester agent prompt builder |
| `build-documenter-prompt.sh` | Documenter agent prompt builder |
| `build-integrator-prompt.sh` | Integrator agent prompt builder |
| **Work Loop Review Prompt Builders** | |
| `build-agent-mode-design-review-prompt-workloop.sh` | Agent-mode design review for work loop |
| `build-contract-verification-prompt-workloop.sh` | Contract verification review for work loop |
| `build-code-review-prompt-workloop.sh` | Code review for work loop |
| **Utilities** | |
| `contract-state.sh` | Contract state management utility for SDLC pipeline |
| `populate-contract-tasks.py` | Populates contract tasks from plan document (runs before implement phase) |
| **Convention Documents** | |
| `review-conventions.md` | Code review conventions and guidelines |
| `autofixer-conventions.md` | Autofixer workflow conventions |
| `conflict-conventions.md` | Merge conflict resolution conventions |

## Quick Start

```yaml
- uses: jwbron/egg/action@v0
  with:
    prompt: "Fix the failing tests"
    anthropic-oauth-token: ${{ secrets.ANTHROPIC_OAUTH_TOKEN }}
    # Optional: enable separate reviewer bot for PR reviews
    # reviewer-app-id: ${{ secrets.REVIEWER_APP_ID }}
    # reviewer-app-private-key: ${{ secrets.REVIEWER_APP_PRIVATE_KEY }}
    # reviewer-app-installation-id: ${{ secrets.REVIEWER_APP_INSTALLATION_ID }}
    # Optional: redirect checkpoints to separate repo for privacy
    # checkpoint-repo: ${{ vars.EGG_CHECKPOINT_REPO }}
```

> **Note:** Use `@main` until the first release (v0.1.0) creates the `@v0` tag.

### Version Pinning

For stability, pin to a major version (receives all patch and minor updates):
```yaml
uses: jwbron/egg/action@v0
```

For full reproducibility, pin to an exact version:
```yaml
uses: jwbron/egg/action@v0.1.0
```

## Checkpoint Repository Configuration

By default, checkpoints (session transcripts and tool call data) are stored in the same repository on the `egg/checkpoints/v2` branch. To keep this data private or separate, use the `checkpoint-repo` input:

```yaml
- uses: jwbron/egg/action@v0
  with:
    prompt: "Task description"
    anthropic-oauth-token: ${{ secrets.ANTHROPIC_OAUTH_TOKEN }}
    github-token: ${{ secrets.CROSS_REPO_TOKEN }}
    checkpoint-repo: "owner/checkpoint-repo-name"
```

The value must be in `owner/repo` format. The `github-token` must have write access to both the source repository and the checkpoint repository. The default `github.token` is scoped to the current repository, so cross-repo checkpoints require a PAT or GitHub App token with access to both repositories. To configure this globally across workflows, set the `EGG_CHECKPOINT_REPO` repository variable in Settings > Secrets and variables > Actions > Variables, then reference it with `checkpoint-repo: ${{ vars.EGG_CHECKPOINT_REPO }}`.

## Documentation

For design details, inputs, outputs, and implementation notes, see the [GitHub Actions Support ADR](../docs/adr/in-progress/ADR-GitHub-Actions-Support.md).

For the existing @mention trigger workflow, see [`.github/workflows/on-mention.yml`](../.github/workflows/on-mention.yml) and [`build-mention-prompt.sh`](build-mention-prompt.sh).

## See Also

- [GitHub Automation Guide](../docs/guides/github-automation.md) - Overview of all built-in workflows
- [Agent-Mode Design](../docs/guides/agent-mode-design.md) - Design principles for agent workflows
