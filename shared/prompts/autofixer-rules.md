<!-- Shared autofixer rules: consumed by GHA prompt scripts AND orchestrator pipelines.
     Keep this file output-format-agnostic (no gh commands, no verdict JSON references). -->

## Default Autofixer Rules

**Auto-fixable (commit fixes directly):**
- Lint errors (formatting, import order, code style)
- Type errors with clear fixes
- Simple test failures with obvious fixes
- Missing or outdated dependencies in lock files

**Report only (post comment explaining what's needed):**
- Complex logic errors requiring design decisions
- Security issues requiring architectural changes
- Test failures from unclear requirements
- Build failures from missing environment config
