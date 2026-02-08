#!/usr/bin/env bash
# build-feedback-prompt.sh — Build a minimal prompt for agent-driven feedback addressing
#
# This script creates a minimal prompt that tells Claude to read review feedback,
# address issues, and push fixes. Following the agent-mode design principles, the
# agent fetches what it needs and takes action directly.
#
# Environment variables:
#   PR_NUMBER          — Pull request number
#   GITHUB_REPOSITORY  — owner/repo
#   RUNNER_TEMP        — Temp directory for prompt file
#
# Output:
#   Sets 'prompt-file' and 'model' in $GITHUB_OUTPUT

set -euo pipefail

# ---------------------------------------------------------------------------
# Build the prompt
# ---------------------------------------------------------------------------

build_prompt() {
    local prompt
    prompt="Address review feedback on PR #${PR_NUMBER} in ${GITHUB_REPOSITORY}.

## Your Task

Review feedback was just posted on this PR. Read the feedback, understand the issues
raised, make the necessary code changes, and push your fixes.

1. **Read the feedback**:
   - Issue-level comments: \`gh pr view ${PR_NUMBER} --comments\`
   - Formal reviews: \`gh api repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}/reviews --jq '.[] | {user: .user.login, state: .state, body: .body}'\`
   - Line-level review comments: \`gh api repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}/comments --jq '.[] | {path: .path, line: .line, body: .body}'\`
2. **Understand the current code**: Use \`gh pr diff ${PR_NUMBER}\` to see the PR changes.
3. **Make fixes**: Address each piece of actionable feedback.
4. **Verify**: Run tests and linters locally before pushing (\`make lint\`, \`make test\`).
5. **Push**: Commit and push all fixes together.
6. **Reply**: If you disagree with any feedback or cannot address it, reply to the specific review comment explaining your reasoning.

## Feedback Rules

Address all actionable review feedback:

**Fix**: Correctness issues, security concerns, logic errors, missing error handling,
resource leaks, breaking changes, pattern violations.

**Respond (do not fix)**: If you disagree with feedback, post a reply explaining your
reasoning instead of making the change. Be respectful but firm.

**Skip**: Pure style suggestions that linters handle, subjective preferences without
technical justification.

## Conventions

Use git commit and git push to push fixes. If you need to respond to review feedback,
use \`gh pr comment\` or reply inline. Sign any comments with: — Authored by egg
"

    # Write prompt to temp file
    local prompt_dir="${RUNNER_TEMP:-/tmp}"
    mkdir -p "$prompt_dir"
    local prompt_file="${prompt_dir}/feedback-prompt-${PR_NUMBER}.txt"
    echo "$prompt" > "$prompt_file"

    # Use opus for feedback addressing (needs reasoning capability)
    local model="opus"

    # Write outputs
    {
        echo "prompt-file=${prompt_file}"
        echo "model=${model}"
    } >> "${GITHUB_OUTPUT:-/dev/null}"

    echo "Feedback prompt built: ${#prompt} chars, model=${model}"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

: "${PR_NUMBER:?PR_NUMBER is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"

build_prompt
