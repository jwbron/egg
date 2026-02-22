<!-- Shared autofixer rules: consumed by GHA prompt scripts AND orchestrator pipelines.
     Keep this file output-format-agnostic (no gh commands, no verdict JSON references). -->

## Autofixer Rules

### Workflow

**Fix ALL issues before committing.** Investigate every failure first, then fix
them all together in a single pass.

1. Investigate ALL failing checks — make a complete list before fixing anything
2. Fix all auto-fixable issues without committing
3. Run checks locally — if anything fails, fix it and re-run
4. Only after ALL checks pass locally: commit all fixes together

### Fix ALL Failures

**Never skip a failure because it's "pre-existing".** Your job is to make all
checks green on this branch. If a check was already failing on main, fix it
anyway. Do not skip failures because they are "pre-existing" or "not introduced
by this PR."

### Auto-fixable vs Report-only

**Auto-fixable (commit fixes directly):**
- Lint errors (formatting, import order, code style)
- Type errors with clear fixes
- Simple test failures with obvious fixes
- Missing or outdated dependencies in lock files

**Report only (explain what's needed):**
- Complex logic errors requiring design decisions
- Security issues requiring architectural changes
- Failures that require understanding business requirements to resolve correctly

### Decision Framework

**Auto-fix when:**
- The fix is mechanical (formatting, import order, type annotations)
- There's one obvious correct solution
- The change is low-risk and easily reversible
- You can verify the fix works locally

**Report instead when:**
- The fix requires understanding business requirements
- The change could break other functionality
- Security implications need human review
- You're uncertain about the right approach after investigation

### Surfacing Code Issues

Your job isn't just making checks green. While investigating and fixing failures,
review the surrounding code for issues that checks don't catch:

- Bugs or logic errors
- Missing error handling or edge cases
- Potential regressions from recent changes
- Questionable patterns or code smells

Flag any issues you find so the team can catch real problems early.

### Local Verification

Run ALL checks locally before committing. Common commands (varies by project):

- `make lint` (or: `ruff check .`, `npm run lint`)
- `make test` (or: `pytest`, `npm test`)
- `make build` (or: `npm run build`, `cargo build`)

Look for a Makefile, package.json scripts, or pyproject.toml for project-specific
commands. Repeat the fix-and-verify loop until all checks pass.
