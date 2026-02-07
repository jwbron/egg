#!/usr/bin/env bash
# build-review-prompt.sh — Build a review prompt for agent-driven code review
#
# This script creates a prompt that tells Claude what to review and provides
# context for re-reviews. It follows agent-mode design principles: specifying
# "what" (outcomes) rather than "how" (procedures).
#
# Environment variables:
#   PR_NUMBER          — Pull request number to review
#   GITHUB_REPOSITORY  — owner/repo
#   RUNNER_TEMP        — Temp directory for prompt file
#   LAST_REVIEW_COMMIT — (Optional) Commit SHA of last bot review, for re-reviews
#   REVIEW_FOCUS       — (Optional) Specialized focus area (e.g., "agent-mode-design")
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

    # Build the base prompt
    prompt="Review PR #${PR_NUMBER} in ${GITHUB_REPOSITORY}."

    # Check if this is a re-review (we have a previous review commit)
    if [[ -n "${LAST_REVIEW_COMMIT:-}" ]]; then
        is_rereview=true
        prompt="${prompt}

## Re-Review Context

This is a **re-review**. You previously reviewed this PR at commit \`${LAST_REVIEW_COMMIT}\`.

- Focus on changes since your last review
- Check whether previous feedback was addressed
- Don't repeat comments on unchanged code"
    fi

    # Add specialized focus if provided
    if [[ -n "${REVIEW_FOCUS:-}" ]]; then
        case "${REVIEW_FOCUS}" in
            agent-mode-design)
                prompt="${prompt}

## Focus Area

Read \`docs/guides/agent-mode-design.md\` for context on what to check.
Review this PR specifically for alignment with agent-mode design principles."
                ;;
            *)
                # Unknown focus, ignore
                ;;
        esac
    else
        # Standard review - add review rules
        prompt="${prompt}

## Review Rules

${review_rules}"
    fi

    # Add conventions
    prompt="${prompt}

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
