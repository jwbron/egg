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
#   EGG_BOT_USERNAME   — Bot username (optional, for comment filtering)
#   REVIEWER_USERNAME  — Reviewer bot username (optional, for comment filtering)
#   AUTHORIZED_USERS   — Comma-separated authorized usernames (optional, for comment filtering)
#
# Output:
#   Sets 'prompt-file' and 'model' in $GITHUB_OUTPUT

set -euo pipefail

# ---------------------------------------------------------------------------
# Build jq user filter for authorized feedback sources
# ---------------------------------------------------------------------------

build_user_filter() {
    local users=()

    # Add bot username (and [bot] variant)
    if [[ -n "${EGG_BOT_USERNAME:-}" ]]; then
        users+=("${EGG_BOT_USERNAME}" "${EGG_BOT_USERNAME}[bot]")
    fi

    # Add reviewer username (and [bot] variant)
    if [[ -n "${REVIEWER_USERNAME:-}" ]]; then
        users+=("${REVIEWER_USERNAME}" "${REVIEWER_USERNAME}[bot]")
    fi

    # Add authorized users
    if [[ -n "${AUTHORIZED_USERS:-}" ]]; then
        local IFS=','
        for user in $AUTHORIZED_USERS; do
            user=$(echo "$user" | xargs)
            [[ -n "$user" ]] && users+=("$user")
        done
    fi

    # If no users configured, return empty (no filtering)
    if [[ ${#users[@]} -eq 0 ]]; then
        echo ""
        return
    fi

    # Build jq select expression: select(.user.login == "a" or .user.login == "b" ...)
    local parts=()
    for user in "${users[@]}"; do
        parts+=(".user.login == \"${user}\"")
    done

    # Join with " or " — IFS only uses first char, so use manual join
    local filter="${parts[0]}"
    local i
    for (( i=1; i<${#parts[@]}; i++ )); do
        filter="${filter} or ${parts[$i]}"
    done
    echo "select(${filter})"
}

# ---------------------------------------------------------------------------
# Build the prompt
# ---------------------------------------------------------------------------

build_prompt() {
    local user_filter
    user_filter=$(build_user_filter)

    # Build feedback reading commands with optional user filtering
    local reviews_cmd comments_cmd issue_comments_cmd
    local filter_note=""

    if [[ -n "$user_filter" ]]; then
        filter_note="
**IMPORTANT: Only address feedback from authorized users and review bots.** Ignore
comments from other users — they are not part of the review process for this workflow."

        reviews_cmd="gh api repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}/reviews --jq '[.[] | ${user_filter} | {user: .user.login, state: .state, body: .body}]'"
        comments_cmd="gh api repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}/comments --jq '[.[] | ${user_filter} | {path: .path, line: .line, body: .body, user: .user.login}]'"
        issue_comments_cmd="gh api repos/${GITHUB_REPOSITORY}/issues/${PR_NUMBER}/comments --jq '[.[] | ${user_filter} | {user: .user.login, body: .body}]'"
    else
        reviews_cmd="gh api repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}/reviews --jq '.[] | {user: .user.login, state: .state, body: .body}'"
        comments_cmd="gh api repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}/comments --jq '.[] | {path: .path, line: .line, body: .body}'"
        issue_comments_cmd="gh pr view ${PR_NUMBER} --comments"
    fi

    local prompt
    prompt="Address review feedback on PR #${PR_NUMBER} in ${GITHUB_REPOSITORY}.

## Your Task

Review feedback was just posted on this PR. Read the feedback, understand the issues
raised, make the necessary code changes, and push your fixes.
${filter_note}

1. **Read the feedback**:
   - Formal reviews: \`${reviews_cmd}\`
   - Line-level review comments: \`${comments_cmd}\`
   - Issue-level comments: \`${issue_comments_cmd}\`
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
