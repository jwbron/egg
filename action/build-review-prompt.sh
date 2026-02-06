#!/usr/bin/env bash
# build-review-prompt.sh — Build a minimal review prompt for agent-driven code review
#
# This script creates a minimal prompt that tells Claude to fetch what it needs
# and post its own review directly via `gh pr review`. This replaces the old
# approach of pre-fetching all PR data and parsing structured JSON output.
#
# Environment variables:
#   PR_NUMBER          — Pull request number to review
#   GITHUB_REPOSITORY  — owner/repo
#   RUNNER_TEMP        — Temp directory for prompt file
#
# Output:
#   Sets 'prompt-file' and 'model' in $GITHUB_OUTPUT

set -euo pipefail

# ---------------------------------------------------------------------------
# Fetch review rules (or use defaults)
# ---------------------------------------------------------------------------

fetch_review_rules() {
    local rules_file=".egg/review-rules.md"

    if [[ -f "$rules_file" ]]; then
        cat "$rules_file"
    else
        # Default review rules when no repo-specific rules exist
        cat <<'EOF'
## Default Review Rules

Focus on:
- Security issues (vulnerabilities, unsafe patterns, credential leaks)
- Correctness (logic errors, edge cases, error handling gaps)
- Code quality (readability, maintainability, naming)

Skip:
- Style issues handled by linters (formatting, import order)
- Type annotation completeness (type checkers handle this)
- Auto-generated files (migrations, lock files)
EOF
    fi
}

# ---------------------------------------------------------------------------
# Build the prompt
# ---------------------------------------------------------------------------

build_prompt() {
    local review_rules
    review_rules=$(fetch_review_rules)

    # Load review conventions if available
    local conventions_file
    conventions_file="$(dirname "$0")/review-conventions.md"
    local conventions=""
    if [[ -f "$conventions_file" ]]; then
        conventions=$(cat "$conventions_file")
    fi

    local prompt
    prompt="Review PR #${PR_NUMBER} in ${GITHUB_REPOSITORY}.

Use \`gh pr diff ${PR_NUMBER}\` to see the changes. Read files for context. Check how
changed code interacts with the rest of the codebase.

## Review Rules

${review_rules}

## Review Conventions

${conventions:-Post your review using \`gh pr review ${PR_NUMBER}\`. Use --approve if the PR looks good, --request-changes for blocking issues, or --comment for advisory feedback. Be specific and suggest fixes. Sign your review with: — Authored by egg}
"

    # Write prompt to temp file
    local prompt_file="${RUNNER_TEMP:-/tmp}/review-prompt-${PR_NUMBER}.txt"
    echo "$prompt" > "$prompt_file"

    # Always use opus for reviews
    local model="opus"

    # Write outputs
    {
        echo "prompt-file=${prompt_file}"
        echo "model=${model}"
    } >> "${GITHUB_OUTPUT:-/dev/null}"

    echo "Review prompt built: ${#prompt} chars, model=${model}"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

: "${PR_NUMBER:?PR_NUMBER is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"

build_prompt
