# Autofixer Conventions (GitHub Actions)

Operational conventions specific to the GitHub Actions autofixer.
General autofixer rules (workflow, decision framework, etc.) are in
`shared/prompts/autofixer-rules.md`.

## Per-Check Fixer Model

The autofixer operates in a CI-driven loop:
1. CI check fails → fixer is invoked with the specific failed checks
2. Fixer investigates and fixes only those checks
3. Fixer pushes fixes (does NOT re-run checks locally)
4. CI re-runs automatically after push
5. If still failing → fixer is re-invoked (up to max retries)
6. If max retries exceeded → escalation comment posted for human

**Do NOT run checks locally.** CI validates after each push. Running checks
locally wastes agent compute — CI already does this.

## Lint Workflow Structure

The Lint workflow runs parallel jobs for faster feedback. When investigating lint
failures, the job name tells you what type of linting failed:

| Job Name | What It Checks |
|----------|----------------|
| **Python** | `ruff check`, `ruff format`, `mypy` |
| **Shell** | `shellcheck` on shell scripts |
| **YAML** | `yamllint` on YAML files |
| **Docker** | `hadolint` on Dockerfiles |
| **Actions** | `actionlint` on GitHub Actions workflows |
| **Custom Checks** | Project-specific lint scripts in `scripts/check-*.py` |

When a lint job fails, check the logs to see which specific tool failed within
that job. For example, a "Python" job failure might be from ruff or mypy.

## Investigating Failures

Use `gh run view <run-id> --log-failed` to see the failure output. For broader
context:

```bash
# List checks
gh pr checks 123

# View failed workflow runs for the PR
gh run list --branch <pr-branch> --status failure

# Get logs for a specific run
gh run view <run-id> --log-failed
```

If the check is a GitHub Actions workflow, you can also examine the workflow
file to understand what commands are being run.

## Committing Fixes

Fix the issues identified from CI logs, then commit and push:

```bash
git add <specific-files>
git commit -m "Fix checks: <summary of all fixes>"
git push
```

**Do NOT run checks locally before pushing.** CI will re-run automatically.
If fixes don't resolve the issue, the fixer will be re-invoked with updated
failure context.

**Why single push matters:** Each push triggers CI. Fixing one issue at a time
causes the workflow to run repeatedly, wasting CI resources and time.

## Reporting Unfixable Issues

When you can't auto-fix an issue, post a comment explaining:

```bash
gh pr comment 123 --body "## Check Failure: <Check Name>

**What's failing:** Brief description of the error.

**Root cause:** Why this is happening.

**What needs to be done:**
- Specific action item 1
- Specific action item 2

**Suggestion:** If you have ideas for how to fix it, include them.

— Authored by egg"
```

## Signature

End all PR comments with: — Authored by egg
