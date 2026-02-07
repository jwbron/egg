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
#   LAST_REVIEW_COMMIT — (Optional) Commit SHA of last bot review, for re-reviews
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
    local is_rereview=false

    # Check if this is a re-review (we have a previous review commit)
    if [[ -n "${LAST_REVIEW_COMMIT:-}" ]]; then
        is_rereview=true
        prompt="Re-review PR #${PR_NUMBER} in ${GITHUB_REPOSITORY}.

This is a **re-review** — you previously reviewed this PR at commit \`${LAST_REVIEW_COMMIT}\`.

## Your Task

1. **Review only new changes**: Use \`git diff ${LAST_REVIEW_COMMIT}..HEAD\` to see what changed since your last review.
2. **Check previous feedback**: Use \`gh pr view ${PR_NUMBER} --comments\` to see previous review comments.
3. **Verify issues addressed**: Confirm that any concerns from your previous review have been addressed.
4. **Focus on the delta**: Your new review should focus on the changes since \`${LAST_REVIEW_COMMIT}\`, not re-review unchanged code.

For full PR context if needed: \`gh pr diff ${PR_NUMBER}\`

## Review Rules

${review_rules}

## Review Conventions

${conventions:-Post your review using \`gh pr review ${PR_NUMBER}\`. Use --approve if the PR looks good, --request-changes for blocking issues, or --comment for advisory feedback. Be specific and suggest fixes. Sign your review with: — Authored by egg}
"
    else
        prompt="Review PR #${PR_NUMBER} in ${GITHUB_REPOSITORY}.

Use \`gh pr diff ${PR_NUMBER}\` to see the changes. Read files for context. Check how
changed code interacts with the rest of the codebase.

## Review Rules

${review_rules}

## Review Conventions

${conventions:-Post your review using \`gh pr review ${PR_NUMBER}\`. Use --approve if the PR looks good, --request-changes for blocking issues, or --comment for advisory feedback. Be specific and suggest fixes. Sign your review with: — Authored by egg}
"
    fi

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

    local review_type="initial"
    if [[ "$is_rereview" == "true" ]]; then
        review_type="re-review (since ${LAST_REVIEW_COMMIT:0:7})"
    fi
    echo "Review prompt built: ${#prompt} chars, model=${model}, type=${review_type}"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

: "${PR_NUMBER:?PR_NUMBER is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"

build_prompt
