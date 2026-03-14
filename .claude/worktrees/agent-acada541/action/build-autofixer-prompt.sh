#!/usr/bin/env bash
# DEPRECATED: Use build-check-fixer-prompt.sh instead.
# This script is kept for external repos that still reference it.
# New repos should use reusable-check-fixer.yml with build-check-fixer-prompt.sh.
#
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
    local script_dir
    script_dir="$(dirname "$0")"
    local shared_file="${script_dir}/../shared/prompts/autofixer-rules.md"

    if [[ -f "$rules_file" ]]; then
        # User override takes priority
        cat "$rules_file"
    elif [[ -f "$shared_file" ]]; then
        # Shared criteria (anchored to trusted checkout via script dir)
        cat "$shared_file"
    else
        # Inline fallback for rollout safety
        cat <<'EOF'
## Autofixer Rules

### Fix ALL Failures

**Never skip a failure because it's "pre-existing".** Fix all checks on this branch.

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

Fix ALL failing checks on this PR. Use \`gh pr checks ${PR_NUMBER}\` to find failures,
examine logs, fix issues, verify locally, then commit and push once.

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
