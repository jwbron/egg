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
        prompt="**Specialized review**: Check PR #${PR_NUMBER} in ${GITHUB_REPOSITORY} for agent-mode design anti-patterns.

This is a **re-review** — you previously reviewed this PR at commit \`${LAST_REVIEW_COMMIT}\`.

## Scope

This is a specialized design review, NOT a general code review. A separate bot handles general code quality, security, and correctness. Your job is specifically to catch agent-mode design anti-patterns.

**Only comment if you find agent-mode design issues.** If the PR has no agent-mode concerns, approve with a brief note like \"No agent-mode design concerns\" — don't provide general feedback.

## Re-review Task

1. Use \`git diff ${LAST_REVIEW_COMMIT}..HEAD\` to see changes since your last review.
2. Use \`gh pr view ${PR_NUMBER} --comments\` to check if previous feedback was addressed.
3. Focus only on the delta — don't re-review unchanged code.

## What to Look For

Read \`docs/guides/agent-mode-design.md\` for full context. Flag these anti-patterns:

1. **Pre-fetching**: Baking large diffs or file contents into prompts instead of letting agents fetch what they need
2. **Structured output for humans**: Requiring JSON when the output goes directly to humans (PR comments, reviews)
3. **Post-processing pipelines**: Scripts that parse agent output to take actions the agent could take directly
4. **How vs what**: Over-specifying procedures instead of objectives
5. **Prompt-level security**: Using instructions for constraints that should be sandbox-enforced

## What to Skip

- General code quality, style, naming — the base review bot covers this
- Security issues unrelated to agent design — the base review bot covers this
- Correctness/logic errors — the base review bot covers this

## Posting Your Review

Use \`gh pr review ${PR_NUMBER}\`:
- \`--approve\`: No agent-mode design issues found
- \`--request-changes\`: Blocking anti-patterns that should be fixed
- \`--comment\`: Advisory suggestions for better agent-mode alignment

Sign with: — Authored by egg
"
    else
        prompt="**Specialized review**: Check PR #${PR_NUMBER} in ${GITHUB_REPOSITORY} for agent-mode design anti-patterns.

## Scope

This is a specialized design review, NOT a general code review. A separate bot handles general code quality, security, and correctness. Your job is specifically to catch agent-mode design anti-patterns.

**Only comment if you find agent-mode design issues.** If the PR has no agent-mode concerns, approve with a brief note like \"No agent-mode design concerns\" — don't provide general feedback.

## What to Look For

Use \`gh pr diff ${PR_NUMBER}\` to see changes. Read \`docs/guides/agent-mode-design.md\` for full context.

Flag these anti-patterns:

1. **Pre-fetching**: Baking large diffs or file contents into prompts instead of letting agents fetch what they need
2. **Structured output for humans**: Requiring JSON when the output goes directly to humans (PR comments, reviews)
3. **Post-processing pipelines**: Scripts that parse agent output to take actions the agent could take directly
4. **How vs what**: Over-specifying procedures instead of objectives
5. **Prompt-level security**: Using instructions for constraints that should be sandbox-enforced

## What to Skip

- General code quality, style, naming — the base review bot covers this
- Security issues unrelated to agent design — the base review bot covers this
- Correctness/logic errors — the base review bot covers this

## Posting Your Review

Use \`gh pr review ${PR_NUMBER}\`:
- \`--approve\`: No agent-mode design issues found
- \`--request-changes\`: Blocking anti-patterns that should be fixed
- \`--comment\`: Advisory suggestions for better agent-mode alignment

Sign with: — Authored by egg
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
