#!/usr/bin/env bash
# build-autofixer-prompt.sh — Build a minimal prompt for agent-driven check autofix
#
# This script creates a minimal prompt that tells Claude to investigate check
# failures, fix what it can, and report on anything that requires human
# intervention. Following the agent-mode design principles, the agent fetches
# what it needs and takes action directly.
#
# Environment variables:
#   PR_NUMBER          — Pull request number
#   GITHUB_REPOSITORY  — owner/repo
#   FAILED_WORKFLOW    — Name of the workflow that failed (optional)
#   FAILED_RUN_ID      — Run ID of the failed workflow (optional)
#   RUNNER_TEMP        — Temp directory for prompt file
#
# Output:
#   Sets 'prompt-file' and 'model' in $GITHUB_OUTPUT

set -euo pipefail

# ---------------------------------------------------------------------------
# Fetch autofixer rules (or use defaults)
# ---------------------------------------------------------------------------

fetch_autofixer_rules() {
    local rules_file=".egg/autofixer-rules.md"

    if [[ -f "$rules_file" ]]; then
        cat "$rules_file"
    else
        # Default autofixer rules when no repo-specific rules exist
        cat <<'EOF'
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
EOF
    fi
}

# ---------------------------------------------------------------------------
# Build the prompt
# ---------------------------------------------------------------------------

build_prompt() {
    local autofixer_rules
    autofixer_rules=$(fetch_autofixer_rules)

    # Load autofixer conventions if available
    local conventions_file
    conventions_file="$(dirname "$0")/autofixer-conventions.md"
    local conventions=""
    if [[ -f "$conventions_file" ]]; then
        conventions=$(cat "$conventions_file")
    fi

    # Build workflow context
    local workflow_context=""
    if [[ -n "${FAILED_WORKFLOW:-}" && "${FAILED_WORKFLOW}" != "manual" ]]; then
        workflow_context="The **${FAILED_WORKFLOW}** workflow failed."
        if [[ -n "${FAILED_RUN_ID:-}" ]]; then
            workflow_context="${workflow_context} Run ID: ${FAILED_RUN_ID}."
        fi
    fi

    local prompt
    prompt="Fix failing checks on PR #${PR_NUMBER} in ${GITHUB_REPOSITORY}.

${workflow_context}

## Your task

**IMPORTANT: Fix ALL issues in a single pass. Do not push until all checks pass locally.**

1. **Investigate ALL failures first**: Use \`gh pr checks ${PR_NUMBER}\` to list all failing checks. For each failed check, examine the logs to understand what's wrong. Make a complete list of all issues before fixing anything.

2. **Fix without pushing**: For each auto-fixable issue (lint errors, formatting, simple type errors), make the fix but do NOT commit or push yet.

3. **Verify locally**: Run all checks locally (e.g., \`make lint\`, \`make test\`, \`make build\`). If any check still fails, go back to step 2 and fix it. Repeat until ALL checks pass locally.

4. **Push once**: After all local checks pass, commit all fixes together and push once.

5. **Report what you can't fix**: If any issue requires human decision-making or is too complex to auto-fix, post a comment on the PR explaining:
   - What's failing and why
   - What needs to be done to fix it
   - Any relevant context or suggestions

## Autofixer Rules

${autofixer_rules}

## Conventions

${conventions:-Use git commit and git push to push fixes. Use gh pr comment to report issues you cannot auto-fix. Sign comments with: -- Authored by egg}
"

    # Write prompt to temp file
    local prompt_dir="${RUNNER_TEMP:-/tmp}"
    mkdir -p "$prompt_dir"
    local prompt_file="${prompt_dir}/autofixer-prompt-${PR_NUMBER}.txt"
    echo "$prompt" > "$prompt_file"

    # Use opus for autofixing (needs reasoning capability)
    local model="opus"

    # Write outputs
    {
        echo "prompt-file=${prompt_file}"
        echo "model=${model}"
    } >> "${GITHUB_OUTPUT:-/dev/null}"

    echo "Autofixer prompt built: ${#prompt} chars, model=${model}"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

: "${PR_NUMBER:?PR_NUMBER is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"

build_prompt
