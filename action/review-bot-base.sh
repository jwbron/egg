#!/usr/bin/env bash
# review-bot-base.sh — Reusable base framework for building review bots
#
# This module provides common functions for building review bot prompts.
# It follows agent-mode design principles: minimal prompts that tell Claude
# what to do, not how to do it.
#
# Usage:
#   source "$(dirname "$0")/review-bot-base.sh"
#   BOT_NAME="my-bot"
#   BOT_DEFAULT_RULES="..."
#   BOT_TASK_DESCRIPTION="..."
#   build_bot_prompt
#
# Required environment variables:
#   PR_NUMBER          — Pull request number
#   GITHUB_REPOSITORY  — owner/repo
#   RUNNER_TEMP        — Temp directory for prompt file
#
# Optional environment variables:
#   LAST_REVIEW_COMMIT — For re-reviews, commit SHA of last bot review
#
# Required shell variables (set before calling build_bot_prompt):
#   BOT_NAME           — Bot identifier (e.g., "review", "agent-mode-design")
#   BOT_DEFAULT_RULES  — Default rules when no repo-specific rules exist
#   BOT_TASK_DESCRIPTION — What the bot should do (shown after "## Your Task")
#
# Optional shell variables:
#   BOT_CONVENTIONS_FILE — Path to conventions file (defaults to <bot>-conventions.md)
#   BOT_DEFAULT_CONVENTIONS — Default conventions if no file exists
#   BOT_MODEL          — Model to use (defaults to "opus")

set -euo pipefail

# ---------------------------------------------------------------------------
# Fetch bot-specific rules (from .egg/<bot>-rules.md or use defaults)
# ---------------------------------------------------------------------------

fetch_bot_rules() {
    local rules_file=".egg/${BOT_NAME}-rules.md"

    if [[ -f "$rules_file" ]]; then
        cat "$rules_file"
    else
        echo "${BOT_DEFAULT_RULES}"
    fi
}

# ---------------------------------------------------------------------------
# Load conventions from file or use defaults
# ---------------------------------------------------------------------------

load_conventions() {
    local conventions_file="${BOT_CONVENTIONS_FILE:-$(dirname "$0")/${BOT_NAME}-conventions.md}"

    if [[ -f "$conventions_file" ]]; then
        cat "$conventions_file"
    else
        echo "${BOT_DEFAULT_CONVENTIONS:-}"
    fi
}

# ---------------------------------------------------------------------------
# Build re-review preamble (if LAST_REVIEW_COMMIT is set)
# ---------------------------------------------------------------------------

build_rereview_preamble() {
    if [[ -z "${LAST_REVIEW_COMMIT:-}" ]]; then
        echo ""
        return
    fi

    cat <<EOF
This is a **re-review** — you previously reviewed this PR at commit \`${LAST_REVIEW_COMMIT}\`.

## Prior Review Context

1. **Review only new changes**: Use \`git diff ${LAST_REVIEW_COMMIT}..HEAD\` to see what changed since your last review.
2. **Check previous feedback**: Use \`gh pr view ${PR_NUMBER} --comments\` to see previous review comments.
3. **Verify issues addressed**: Confirm that any concerns from your previous review have been addressed.
4. **Focus on the delta**: Your new review should focus on the changes since \`${LAST_REVIEW_COMMIT}\`, not re-review unchanged code.

For full PR context if needed: \`gh pr diff ${PR_NUMBER}\`

---

EOF
}

# ---------------------------------------------------------------------------
# Build the complete prompt
# ---------------------------------------------------------------------------

build_bot_prompt() {
    # Validate required variables
    : "${BOT_NAME:?BOT_NAME is required}"
    : "${BOT_DEFAULT_RULES:?BOT_DEFAULT_RULES is required}"
    : "${BOT_TASK_DESCRIPTION:?BOT_TASK_DESCRIPTION is required}"
    : "${PR_NUMBER:?PR_NUMBER is required}"
    : "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"

    local bot_rules
    bot_rules=$(fetch_bot_rules)

    local conventions
    conventions=$(load_conventions)

    local rereview_preamble
    rereview_preamble=$(build_rereview_preamble)

    local is_rereview=false
    local review_type_label="initial"
    if [[ -n "${LAST_REVIEW_COMMIT:-}" ]]; then
        is_rereview=true
        review_type_label="re-review (since ${LAST_REVIEW_COMMIT:0:7})"
    fi

    # Build the prompt header based on review type
    local prompt_header
    if [[ "$is_rereview" == "true" ]]; then
        prompt_header="Re-review PR #${PR_NUMBER} in ${GITHUB_REPOSITORY}."
    else
        prompt_header="Review PR #${PR_NUMBER} in ${GITHUB_REPOSITORY}.

Use \`gh pr diff ${PR_NUMBER}\` to see the changes. Read files for context. Check how
changed code interacts with the rest of the codebase."
    fi

    local prompt
    prompt="${prompt_header}

${rereview_preamble}## Your Task

${BOT_TASK_DESCRIPTION}

## Review Rules

${bot_rules}

## Review Conventions

${conventions:-Post your review using \`gh pr review ${PR_NUMBER}\`. Use --approve if the PR looks good, --request-changes for blocking issues, or --comment for advisory feedback. Be specific and suggest fixes. Sign your review with: — Authored by egg}
"

    # Write prompt to temp file
    local prompt_dir="${RUNNER_TEMP:-/tmp}"
    mkdir -p "$prompt_dir"
    local prompt_file="${prompt_dir}/${BOT_NAME}-prompt-${PR_NUMBER}.txt"
    echo "$prompt" > "$prompt_file"

    local model="${BOT_MODEL:-opus}"

    # Write outputs
    {
        echo "prompt-file=${prompt_file}"
        echo "model=${model}"
    } >> "${GITHUB_OUTPUT:-/dev/null}"

    echo "${BOT_NAME} prompt built: ${#prompt} chars, model=${model}, type=${review_type_label}"
}
