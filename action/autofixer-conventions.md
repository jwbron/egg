# Autofixer Conventions

Guidelines for how to investigate and fix check failures.

## Single-Pass Workflow (CRITICAL)

**Fix ALL issues before pushing.** The autofixer must complete all fixes in a single
pass to avoid triggering multiple workflow runs.

**Workflow:**
1. Investigate ALL failing checks first — make a complete list before fixing anything
2. Fix all auto-fixable issues without committing
3. Run checks locally — if anything fails, fix it and re-run
4. Only after ALL local checks pass: commit and push once

**Why this matters:** Each push triggers CI. Fixing one issue at a time causes the
workflow to run repeatedly, wasting CI resources and time.

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

## Running Checks Locally

**Run ALL checks locally before pushing.** This is the verification loop:

```bash
# Common check commands (varies by project)
make lint       # or: ruff check ., npm run lint
make test       # or: pytest, npm test
make build      # or: npm run build, cargo build
```

**Verification loop:**
1. Run all check commands
2. If any fail, fix the issue
3. Repeat until ALL checks pass
4. Only then commit and push

Look for a Makefile, package.json scripts, or pyproject.toml for project-specific commands.

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

## Decision Framework

**Auto-fix when:**
- The fix is mechanical (formatting, import order, type annotations)
- There's one obvious correct solution
- The change is low-risk and easily reversible
- You can verify the fix works locally

**Report instead when:**
- Multiple valid approaches exist
- The fix requires understanding business requirements
- The change could break other functionality
- Security implications need human review
- You're uncertain about the right approach

## Signature

End all PR comments with: — Authored by egg
