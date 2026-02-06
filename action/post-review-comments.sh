#!/usr/bin/env bash
# post-review-comments.sh — Parse Claude's review output and post GitHub review comments
#
# Reads the Claude output log, extracts JSON review comments, and posts them
# as a GitHub pull request review with inline comments.
#
# Environment variables:
#   PR_NUMBER          — Pull request number
#   GITHUB_REPOSITORY  — owner/repo
#   GH_TOKEN           — GitHub token for API access
#   LOG_FILE           — Path to Claude's output log
#   HEAD_SHA           — Commit SHA to attach comments to
#
# The script expects Claude's output to contain a JSON summary block like:
# {
#   "summary": "Overall assessment",
#   "verdict": "approve|request_changes|comment",
#   "comments": [
#     {"file": "path", "line": N, "severity": "...", "category": "...", "comment": "..."}
#   ]
# }

set -euo pipefail

BOT_USERNAME="${BOT_USERNAME:-james-in-a-box}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Safe gh api wrapper
gh_api_safe() {
    local stderr_file
    stderr_file=$(mktemp)
    local output
    if output=$(gh api "$@" 2>"$stderr_file"); then
        rm -f "$stderr_file"
        echo "$output"
    else
        local rc=$?
        echo "WARNING: 'gh api $*' failed (exit $rc): $(cat "$stderr_file")" >&2
        rm -f "$stderr_file"
        return 1
    fi
}

# Extract JSON from Claude's output
# Claude may wrap JSON in markdown code fences or include preamble/postamble
extract_review_json() {
    local log_content="$1"

    # Try to find the summary JSON block (the final comprehensive one)
    # Look for JSON with "summary", "verdict", and "comments" fields
    local json_block

    # First, try to extract from ```json ... ``` blocks
    json_block=$(echo "$log_content" | \
        grep -Pzo '(?s)```json\s*\n\{[^`]*"summary"[^`]*"comments"[^`]*\}\s*\n```' | \
        sed 's/```json//g; s/```//g' | \
        tr -d '\0' | \
        tail -1)

    if [[ -n "$json_block" ]] && echo "$json_block" | jq -e . >/dev/null 2>&1; then
        echo "$json_block"
        return 0
    fi

    # Try to find bare JSON object with required fields
    json_block=$(echo "$log_content" | \
        grep -Pzo '(?s)\{[^{}]*"summary"[^{}]*"verdict"[^{}]*"comments"\s*:\s*\[[^\]]*\][^{}]*\}' | \
        tr -d '\0' | \
        tail -1)

    if [[ -n "$json_block" ]] && echo "$json_block" | jq -e . >/dev/null 2>&1; then
        echo "$json_block"
        return 0
    fi

    # Try a more lenient pattern - just look for any object with "comments" array
    json_block=$(echo "$log_content" | \
        python3 -c "
import sys
import re
import json

content = sys.stdin.read()

# Find all JSON-like blocks
pattern = r'\{[^{}]*\"comments\"\s*:\s*\[.*?\][^{}]*\}'
matches = re.findall(pattern, content, re.DOTALL)

for match in reversed(matches):
    try:
        obj = json.loads(match)
        if 'comments' in obj:
            print(json.dumps(obj))
            sys.exit(0)
    except:
        continue

# If no structured output found, create an empty one
print(json.dumps({'summary': 'Review completed but no structured output found.', 'verdict': 'comment', 'comments': []}))
" 2>/dev/null)

    if [[ -n "$json_block" ]]; then
        echo "$json_block"
        return 0
    fi

    # Fallback: no structured review found
    echo '{"summary": "Review completed but no structured output found.", "verdict": "comment", "comments": []}'
}

# Dismiss previous reviews from the bot to avoid clutter
dismiss_previous_reviews() {
    echo "Checking for previous bot reviews to dismiss..."

    # Get all reviews on this PR
    local reviews
    reviews=$(gh_api_safe "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}/reviews" \
        --jq "[.[] | select(.user.login == \"${BOT_USERNAME}\" or .user.login == \"${BOT_USERNAME}[bot]\") | {id: .id, state: .state}]" 2>/dev/null || echo "[]")

    if [[ "$reviews" == "[]" ]]; then
        echo "No previous bot reviews found."
        return 0
    fi

    # Dismiss each pending review (can't dismiss approved/changes_requested after merge)
    echo "$reviews" | jq -c '.[]' | while read -r review; do
        local review_id state
        review_id=$(echo "$review" | jq -r '.id')
        state=$(echo "$review" | jq -r '.state')

        # Only dismiss PENDING, COMMENTED, CHANGES_REQUESTED reviews
        if [[ "$state" =~ ^(PENDING|COMMENTED|CHANGES_REQUESTED)$ ]]; then
            echo "Dismissing review ${review_id} (state: ${state})..."
            gh_api_safe "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}/reviews/${review_id}/dismissals" \
                -X PUT \
                -f message="Superseded by new review" \
                >/dev/null 2>&1 || echo "Could not dismiss review ${review_id}"
        fi
    done
}

# Post the review
post_review() {
    local summary="$1"
    local verdict="$2"
    local comments_json="$3"

    # Map verdict to GitHub review event
    # We use COMMENT for all verdicts to keep reviews advisory-only
    # (not blocking merges). The verdict is included in the summary text.
    local event="COMMENT"

    # Build the review body
    local severity_counts
    severity_counts=$(echo "$comments_json" | jq -r '
        group_by(.severity) |
        map("\(.[0].severity): \(length)") |
        join(", ")
    ' 2>/dev/null || echo "")

    local body="## AI Code Review

${summary}

"
    if [[ -n "$severity_counts" ]]; then
        body="${body}**Issues found:** ${severity_counts}

"
    fi

    body="${body}---
*This is an automated review. Please evaluate suggestions carefully.*

\u2014 Authored by egg"

    # Build comments array for GitHub API
    # GitHub expects: [{path, line, side, body}, ...]
    local gh_comments
    gh_comments=$(echo "$comments_json" | jq -c '
        [.[] | {
            path: .file,
            line: .line,
            side: "RIGHT",
            body: "**\(.severity)** (\(.category)): \(.comment)"
        }]
    ' 2>/dev/null || echo "[]")

    # Validate comments have required fields and filter out invalid ones
    gh_comments=$(echo "$gh_comments" | jq -c '
        [.[] | select(.path != null and .path != "" and .line != null and .line > 0)]
    ')

    local comment_count
    comment_count=$(echo "$gh_comments" | jq 'length')

    echo "Posting review with ${comment_count} inline comments..."

    # Build the review payload
    local payload
    if [[ "$comment_count" -gt 0 ]]; then
        payload=$(jq -n \
            --arg commit_id "$HEAD_SHA" \
            --arg event "$event" \
            --arg body "$body" \
            --argjson comments "$gh_comments" \
            '{commit_id: $commit_id, event: $event, body: $body, comments: $comments}')
    else
        payload=$(jq -n \
            --arg commit_id "$HEAD_SHA" \
            --arg event "$event" \
            --arg body "$body" \
            '{commit_id: $commit_id, event: $event, body: $body}')
    fi

    # Post the review
    if echo "$payload" | gh_api_safe "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}/reviews" \
        -X POST \
        --input - >/dev/null; then
        echo "Review posted successfully!"
    else
        echo "Failed to post review with inline comments. Posting as regular comment..."
        post_fallback_comment "$body" "$comments_json"
    fi
}

# Fallback: post as a regular PR comment if review API fails
post_fallback_comment() {
    local body="$1"
    local comments_json="$2"

    # Include inline comments in the body since we can't post them inline
    local comment_count
    comment_count=$(echo "$comments_json" | jq 'length')

    if [[ "$comment_count" -gt 0 ]]; then
        local comments_text
        comments_text=$(echo "$comments_json" | jq -r '
            .[] | "- **\(.file):\(.line)** [\(.severity)/\(.category)] \(.comment)"
        ')
        body="${body}

### Inline Comments

${comments_text}"
    fi

    echo "Posting fallback comment..."
    gh_api_safe "repos/${GITHUB_REPOSITORY}/issues/${PR_NUMBER}/comments" \
        -X POST \
        -f body="$body" >/dev/null

    echo "Fallback comment posted."
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

: "${PR_NUMBER:?PR_NUMBER is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${LOG_FILE:?LOG_FILE is required}"
: "${HEAD_SHA:?HEAD_SHA is required}"

if [[ ! -f "$LOG_FILE" ]]; then
    echo "ERROR: Log file not found: $LOG_FILE"
    exit 1
fi

echo "Parsing Claude's review output from: $LOG_FILE"

# Read the log file
log_content=$(cat "$LOG_FILE")

# Extract the review JSON
review_json=$(extract_review_json "$log_content")

echo "Extracted review JSON:"
echo "$review_json" | jq -C . 2>/dev/null || echo "$review_json"

# Parse the review
summary=$(echo "$review_json" | jq -r '.summary // "Review completed."')
verdict=$(echo "$review_json" | jq -r '.verdict // "comment"')
comments=$(echo "$review_json" | jq -c '.comments // []')

echo ""
echo "Summary: $summary"
echo "Verdict: $verdict"
echo "Comments: $(echo "$comments" | jq 'length')"

# Dismiss previous bot reviews
dismiss_previous_reviews

# Post the new review
post_review "$summary" "$verdict" "$comments"

echo "Done!"
