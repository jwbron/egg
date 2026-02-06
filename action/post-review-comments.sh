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
#   DIFF_FILE          — (optional) Path to PR diff for line number mapping
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

# Fetch PR diff patches for line number mapping
# Returns JSON: {"file.py": "patch content", ...}
fetch_pr_patches() {
    gh_api_safe "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}/files" \
        --jq '[.[] | {key: .filename, value: .patch}] | from_entries' 2>/dev/null || echo "{}"
}

# Convert absolute line number to diff position for GitHub review API
# GitHub expects a "position" in the diff, not the absolute line number
# Returns the position or empty string if line not found in diff
#
# Args: $1 = patch content, $2 = absolute line number
get_diff_position() {
    local patch="$1"
    local target_line="$2"

    if [[ -z "$patch" ]] || [[ -z "$target_line" ]]; then
        echo ""
        return
    fi

    # Parse the diff to find the position
    # Position is 1-indexed, counting all lines in the diff (including hunk headers)
    python3 -c "
import sys

patch = '''$patch'''
target_line = int('$target_line')

position = 0
current_new_line = 0

for line in patch.split('\n'):
    position += 1

    # Parse hunk header: @@ -old_start,old_count +new_start,new_count @@
    if line.startswith('@@'):
        import re
        match = re.search(r'\+(\d+)', line)
        if match:
            current_new_line = int(match.group(1)) - 1  # -1 because we increment before checking
        continue

    # Skip removed lines (they don't exist in new file)
    if line.startswith('-'):
        continue

    # Context or added line
    current_new_line += 1

    if current_new_line == target_line:
        print(position)
        sys.exit(0)

# Line not found in diff
sys.exit(1)
" 2>/dev/null || echo ""
}

# Safe gh api wrapper (with proper quoting in error message)
gh_api_safe() {
    local stderr_file
    stderr_file=$(mktemp)
    local output
    # Capture the command for error reporting (properly quoted)
    local cmd_display
    cmd_display=$(printf "'gh api %s'" "$*")

    if output=$(gh api "$@" 2>"$stderr_file"); then
        rm -f "$stderr_file"
        echo "$output"
    else
        local rc=$?
        local stderr_content
        stderr_content=$(cat "$stderr_file")
        rm -f "$stderr_file"
        echo "WARNING: ${cmd_display} failed (exit $rc): ${stderr_content}" >&2
        return 1
    fi
}

# Extract JSON from Claude's output
# Claude may wrap JSON in markdown code fences or include preamble/postamble
# Uses Python for robust JSON parsing that handles nested objects correctly
extract_review_json() {
    local log_content="$1"

    # Use Python for robust JSON extraction that handles nested braces correctly.
    # The log file contains stream-json output from Claude Code, with lines like:
    #   {"type":"user","message":{"content":[...]}}      ← prompt (may contain PR diff!)
    #   {"type":"assistant","message":{"content":[...]}}  ← Claude's response
    #   {"type":"result","result":"<Claude's final text>"}
    # We must extract from the "result" event only, otherwise template JSON
    # embedded in the PR diff (e.g. security-review.md examples) can be matched.
    local json_block
    json_block=$(echo "$log_content" | python3 -c "
import sys
import re
import json

raw_input = sys.stdin.read()

def find_json_objects(text):
    '''Find all valid JSON objects in text using bracket matching.'''
    objects = []
    i = 0
    while i < len(text):
        if text[i] == '{':
            # Found potential JSON start, find matching close brace
            depth = 1
            j = i + 1
            in_string = False
            escape_next = False

            while j < len(text) and depth > 0:
                c = text[j]

                if escape_next:
                    escape_next = False
                elif c == '\\\\' and in_string:
                    escape_next = True
                elif c == '\"' and not escape_next:
                    in_string = not in_string
                elif not in_string:
                    if c == '{':
                        depth += 1
                    elif c == '}':
                        depth -= 1
                j += 1

            if depth == 0:
                candidate = text[i:j]
                try:
                    obj = json.loads(candidate)
                    if isinstance(obj, dict):
                        objects.append(obj)
                except json.JSONDecodeError:
                    pass
                i = j
            else:
                i += 1
        else:
            i += 1
    return objects

def search_for_review(text):
    '''Search text for a review JSON object. Returns it or None.'''
    # First, try to extract from markdown code blocks
    code_block_pattern = r'\`\`\`(?:json)?\s*\n(.*?)\n\`\`\`'
    code_blocks = re.findall(code_block_pattern, text, re.DOTALL)

    # Check code blocks first (in reverse order to get the final summary)
    for block in reversed(code_blocks):
        for obj in find_json_objects(block):
            if 'summary' in obj and 'comments' in obj:
                return obj

    # Then check for bare JSON
    all_objects = find_json_objects(text)

    # Look for the summary object (should have summary, verdict, comments)
    for obj in reversed(all_objects):
        if 'summary' in obj and 'comments' in obj:
            return obj

    # Fallback: try to find any object with comments array
    for obj in reversed(all_objects):
        if 'comments' in obj and isinstance(obj.get('comments'), list):
            obj.setdefault('summary', 'Review completed.')
            obj.setdefault('verdict', 'comment')
            return obj

    return None

# Step 1: Try to extract from stream-json result events.
# This narrows the search to Claude's actual output, avoiding prompt content.
result_text = None
for line in raw_input.splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        event = json.loads(line)
        if isinstance(event, dict) and event.get('type') == 'result' and 'result' in event:
            result_text = str(event['result'])
    except (json.JSONDecodeError, TypeError):
        continue

if result_text is not None:
    review = search_for_review(result_text)
    if review is not None:
        print(json.dumps(review))
        sys.exit(0)

# Step 2: Fallback — scan full content (backward compat for non-stream-json logs)
review = search_for_review(raw_input)
if review is not None:
    print(json.dumps(review))
    sys.exit(0)

# No structured output found
print(json.dumps({
    'summary': 'Review completed but no structured output found.',
    'verdict': 'comment',
    'comments': []
}))
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

    if [[ "$reviews" == "[]" ]] || [[ -z "$reviews" ]]; then
        echo "No previous bot reviews found."
        return 0
    fi

    # Collect all review IDs to dismiss first (avoids subshell issues with while read)
    local review_ids=()
    local review_states=()

    while IFS= read -r review; do
        local review_id state
        review_id=$(echo "$review" | jq -r '.id')
        state=$(echo "$review" | jq -r '.state')

        # Only dismiss PENDING, COMMENTED, CHANGES_REQUESTED reviews
        if [[ "$state" =~ ^(PENDING|COMMENTED|CHANGES_REQUESTED)$ ]]; then
            review_ids+=("$review_id")
            review_states+=("$state")
        fi
    done < <(echo "$reviews" | jq -c '.[]' 2>/dev/null)

    local dismiss_count=${#review_ids[@]}
    if [[ "$dismiss_count" -eq 0 ]]; then
        echo "No dismissible reviews found."
        return 0
    fi

    echo "Found ${dismiss_count} reviews to dismiss."

    # Now dismiss each review
    local failed=0
    for i in "${!review_ids[@]}"; do
        local review_id="${review_ids[$i]}"
        local state="${review_states[$i]}"

        echo "Dismissing review ${review_id} (state: ${state})..."
        if ! gh_api_safe "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}/reviews/${review_id}/dismissals" \
            -X PUT \
            -f message="Superseded by new review" \
            >/dev/null 2>&1; then
            echo "WARNING: Could not dismiss review ${review_id}"
            ((failed++)) || true
        fi
    done

    if [[ "$failed" -gt 0 ]]; then
        echo "WARNING: Failed to dismiss ${failed} of ${dismiss_count} reviews"
    fi
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

— Authored by egg"

    # Fetch PR patches for line number mapping
    echo "Fetching PR patches for line number mapping..."
    local patches_json
    patches_json=$(fetch_pr_patches)

    # Build comments array for GitHub API with proper diff position mapping
    # GitHub expects: [{path, position, side, body}, ...]
    # Note: 'position' is diff-relative, not 'line' (absolute)
    local gh_comments="[]"
    local skipped_comments="[]"

    while IFS= read -r comment; do
        local file line severity category comment_text
        file=$(echo "$comment" | jq -r '.file // ""')
        line=$(echo "$comment" | jq -r '.line // 0')
        severity=$(echo "$comment" | jq -r '.severity // "comment"')
        category=$(echo "$comment" | jq -r '.category // "general"')
        comment_text=$(echo "$comment" | jq -r '.comment // ""')

        # Skip invalid comments
        if [[ -z "$file" ]] || [[ "$line" -le 0 ]]; then
            continue
        fi

        # Get the patch for this file
        local patch
        patch=$(echo "$patches_json" | jq -r --arg f "$file" '.[$f] // ""')

        # Convert absolute line number to diff position
        local position
        position=$(get_diff_position "$patch" "$line")

        local comment_body="**${severity}** (${category}): ${comment_text}"

        if [[ -n "$position" ]]; then
            # Valid diff position found
            gh_comments=$(echo "$gh_comments" | jq -c \
                --arg path "$file" \
                --argjson position "$position" \
                --arg body "$comment_body" \
                '. + [{path: $path, position: $position, side: "RIGHT", body: $body}]')
        else
            # Line not in diff - add to skipped for fallback display
            skipped_comments=$(echo "$skipped_comments" | jq -c \
                --arg file "$file" \
                --argjson line "$line" \
                --arg severity "$severity" \
                --arg category "$category" \
                --arg comment "$comment_text" \
                '. + [{file: $file, line: $line, severity: $severity, category: $category, comment: $comment}]')
            echo "WARNING: Line $line in $file not found in diff, will include in body"
        fi
    done < <(echo "$comments_json" | jq -c '.[]' 2>/dev/null)

    # Add skipped comments to body if any
    local skipped_count
    skipped_count=$(echo "$skipped_comments" | jq 'length')
    if [[ "$skipped_count" -gt 0 ]]; then
        local skipped_text
        skipped_text=$(echo "$skipped_comments" | jq -r '
            .[] | "- **\(.file):\(.line)** [\(.severity)/\(.category)] \(.comment)"
        ')
        body="${body}

### Additional comments (lines not in diff)

${skipped_text}"
    fi

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
        # Pass all original comments to fallback (including skipped ones)
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
