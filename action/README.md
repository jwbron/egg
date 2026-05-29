# egg GitHub Action

Composite GitHub Action for running egg in CI/CD workflows.

## Overview

This action runs the egg autonomous coding agent **directly in the GitHub
Actions runner as a bare process** — no Docker, no gateway, no sandbox
container (#2866). It installs the Claude CLI + `egg_agent`, then runs
`python3 -m egg_agent` against the checked-out repo.

The runner is already ephemeral, and the **GitHub token you pass as
`github-token` is the capability boundary** — there is no gateway enforcing an
operation allowlist. Pass a GitHub App installation token (minted by the
calling workflow via `actions/create-github-app-token`) so the agent acts as
your bot/reviewer identity; the App installation's permission scope is what
bounds it. The Agent SDK's role-based write restrictions (`EGG_AGENT_ROLE`)
still apply. (k3s pipeline agents keep the gateway; this bare-process path is
only for the short-lived PR bots.)

## Files

| File | Purpose |
|------|---------|
| `action.yml` | Action metadata, inputs, outputs |
| `entrypoint.sh` | Installs auth/identity, then runs `python3 -m egg_agent` |
| `bin/gh` | Slim `gh` shim placed ahead of the real `gh` on PATH; injects the `egg-automated-review` marker into `gh pr review` (the rest passes through). Replaces the gateway-coupled sandbox `gh` wrapper for this path. |
| **Prompt Builders** | |
| `build-review-prompt.sh` | Builds prompts for PR review workflows |
| `build-feedback-prompt.sh` | Builds prompts for addressing review feedback workflows |
| `build-autofixer-prompt.sh` | Builds prompts for autofixer workflows |
| `build-agent-mode-design-review-prompt.sh` | Builds prompts for agent-mode design reviews |
| `build-doc-updater-prompt.sh` | Builds prompts for documentation update workflows |
| `build-conflict-prompt.sh` | Builds prompts for merge conflict resolution |
| `build-contract-verification-prompt.sh` | Builds prompts for contract verification reviews |
| **Validators** | |
| `verify-feedback-contract.sh` | Verifies feedback-addressing agent response comments against the contract (response posted, no phantom follow-up phrases, deferred-to issues are real and new) |
| **Convention Documents** | |
| `review-conventions.md` | Code review conventions and guidelines |
| `autofixer-conventions.md` | Autofixer workflow conventions |
| `conflict-conventions.md` | Merge conflict resolution conventions |

Prompt builders load review criteria from `shared/prompts/` (shared with the local orchestrator) with inline fallbacks for rollout safety. Repositories can override criteria via `.egg/` files (e.g., `.egg/review-rules.md`).

### Reviewer Delta / Re-Review Plumbing

The three reviewer prompt builders — `build-review-prompt.sh`, `build-agent-mode-design-review-prompt.sh`, and `build-contract-verification-prompt.sh` — accept an optional `BASE_REF` environment variable (default `main`) in addition to `LAST_REVIEW_COMMIT`. When `LAST_REVIEW_COMMIT` is set (re-review path), the generated prompt instructs the agent to run:

```bash
git fetch origin ${BASE_REF}
git log ${LAST_REVIEW_COMMIT}..HEAD --not origin/${BASE_REF} -p
```

instead of a two-dot `git diff`. This shows only PR-side commits pushed since the last review and excludes any commits that reached the branch via a base-branch merge, fixing the attribution bug where merged-in base work was treated as part of the delta (see [#1758](https://github.com/jwbron/egg/issues/1758)).

`reusable-review.yml` plumbs `BASE_REF` automatically from the PR's `base.ref` (via the `pr-meta` step), so callers do not need to set it manually. Initial-review (no `LAST_REVIEW_COMMIT`) prompts are unaffected.

## Quick Start

```yaml
# Mint the bot/reviewer App token in the workflow, then hand it to the action.
- uses: actions/create-github-app-token@v1
  id: bot-token
  with:
    app-id: ${{ secrets.BOT_APP_ID }}
    private-key: ${{ secrets.BOT_APP_PRIVATE_KEY }}

- uses: jwbron/egg/action@v0
  with:
    prompt: "Fix the failing tests"
    anthropic-oauth-token: ${{ secrets.ANTHROPIC_OAUTH_TOKEN }}
    github-token: ${{ steps.bot-token.outputs.token }}
    # bot-username sets the git commit author for any commits the agent makes.
    bot-username: ${{ vars.EGG_BOT_USERNAME }}
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

## Documentation

For usage details and workflow setup, see the [GitHub Automation Guide](../docs/guides/github-automation.md).

## See Also

- [GitHub Automation Guide](../docs/guides/github-automation.md) - Overview of all built-in workflows
- [Agent-Mode Design](../docs/guides/agent-mode-design.md) - Design principles for agent workflows
