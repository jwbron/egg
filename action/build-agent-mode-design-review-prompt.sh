#!/usr/bin/env bash
# build-agent-mode-design-review-prompt.sh — Build a prompt for agent-mode design review
#
# This script creates a prompt that reviews PRs for alignment with
# agent-mode design principles as documented in docs/guides/agent-mode-design.md.
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
# Build the prompt
# ---------------------------------------------------------------------------

build_prompt() {
    local prompt
    local is_rereview=false

    # Check if this is a re-review (we have a previous review commit)
    if [[ -n "${LAST_REVIEW_COMMIT:-}" ]]; then
        is_rereview=true
        prompt="Re-review PR #${PR_NUMBER} in ${GITHUB_REPOSITORY} for agent-mode design alignment.

This is a **re-review** — you previously reviewed this PR at commit \`${LAST_REVIEW_COMMIT}\`.

## Your Task

1. **Review only new changes**: Use \`git diff ${LAST_REVIEW_COMMIT}..HEAD\` to see what changed since your last review.
2. **Check previous feedback**: Use \`gh pr view ${PR_NUMBER} --comments\` to see previous review comments.
3. **Verify issues addressed**: Confirm that any concerns from your previous review have been addressed.
4. **Focus on the delta**: Your new review should focus on the changes since \`${LAST_REVIEW_COMMIT}\`, not re-review unchanged code.

For full PR context if needed: \`gh pr diff ${PR_NUMBER}\`

## Review Focus

Read \`docs/guides/agent-mode-design.md\` for the design principles to check.

Focus on whether the PR code:
1. Pre-fetches data the agent could fetch itself
2. Requires structured output for human-facing content
3. Uses post-processing pipelines that parse agent output
4. Specifies \"how\" instead of \"what\"
5. Adds constraints beyond what the sandbox enforces

## Review Conventions

Post your review using \`gh pr review ${PR_NUMBER}\`. Use --approve if the PR looks good, --request-changes for blocking issues, or --comment for advisory feedback. Be specific and suggest fixes. Sign your review with: — Authored by egg
"
    else
        prompt="Review PR #${PR_NUMBER} in ${GITHUB_REPOSITORY} for agent-mode design alignment.

Use \`gh pr diff ${PR_NUMBER}\` to see the changes. Read files for context.

## Review Focus

Read \`docs/guides/agent-mode-design.md\` for the design principles to check.

Focus on whether the PR code:
1. Pre-fetches data the agent could fetch itself
2. Requires structured output for human-facing content
3. Uses post-processing pipelines that parse agent output
4. Specifies \"how\" instead of \"what\"
5. Adds constraints beyond what the sandbox enforces

## Review Conventions

Post your review using \`gh pr review ${PR_NUMBER}\`. Use --approve if the PR looks good, --request-changes for blocking issues, or --comment for advisory feedback. Be specific and suggest fixes. Sign your review with: — Authored by egg
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
    echo "Agent-mode design review prompt built: ${#prompt} chars, model=${model}, type=${review_type}"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

: "${PR_NUMBER:?PR_NUMBER is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"

build_prompt
