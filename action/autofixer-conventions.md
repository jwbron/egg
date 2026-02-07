# Autofixer Conventions

Guidelines for how to investigate and fix check failures.

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

Before committing a fix, verify it locally:

```bash
# Common check commands (varies by project)
make lint       # or: ruff check ., npm run lint
make test       # or: pytest, npm test
make build      # or: npm run build, cargo build
```

Look for a Makefile, package.json scripts, or pyproject.toml for project-specific commands.

## Committing Fixes

When you have a fix:

```bash
git add <specific-files>
git commit -m "Fix <check-name>: <brief description>"
git push
```

Keep commits focused. If fixing multiple issues, use separate commits for clarity.

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
