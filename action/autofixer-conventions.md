# Autofixer Conventions (GitHub Actions)

Operational conventions specific to the GitHub Actions autofixer.
General autofixer rules (workflow, decision framework, etc.) are in
`shared/prompts/autofixer-rules.md`.

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

Use `gh pr checks <PR_NUMBER>` to list all checks and their status. For failed
checks, fetch the logs to understand the error:

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

**Only commit after ALL local checks pass.** Do not push partial fixes.

```bash
# After verifying all checks pass locally:
git add <specific-files>
git commit -m "Fix checks: <summary of all fixes>"
git push
```

If fixing multiple distinct issues, you may use separate commits for clarity, but
push them all together in a single push after verifying all checks pass.

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
