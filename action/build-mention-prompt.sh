#!/usr/bin/env bash
# build-mention-prompt.sh — Build a context-rich prompt from a GitHub event payload
#
# Reads $GITHUB_EVENT_PATH (JSON event payload) and uses gh CLI to fetch
# additional context, then outputs a structured prompt via $GITHUB_OUTPUT.
#
# Environment variables:
#   GITHUB_EVENT_NAME  — event type (issue_comment, pull_request_review_comment, pull_request_review, issues)
#   GITHUB_EVENT_PATH  — path to event JSON payload
#   GITHUB_REPOSITORY  — owner/repo
#   BOT_USERNAME       — bot name to strip from mention (default: james-in-a-box)
#
# Output:
#   Sets 'prompt' in $GITHUB_OUTPUT (multiline)

set -euo pipefail

BOT_USERNAME="${BOT_USERNAME:-james-in-a-box}"
MAX_BODY_CHARS=10000
MAX_COMMENT_CHARS=2000
MAX_PROMPT_CHARS=50000

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

truncate_text() {
  local text="$1"
  local max_chars="$2"
  if [[ ${#text} -gt $max_chars ]]; then
    echo "${text:0:$max_chars}... (truncated)"
  else
    echo "$text"
  fi
}

jq_raw() {
  jq -r "$1" "$GITHUB_EVENT_PATH"
}

# Wrapper around gh api that warns on failure instead of silently swallowing errors
gh_api_safe() {
  local stderr_file
  stderr_file=$(mktemp)
  local output
  if output=$(gh api "$@" 2>"$stderr_file"); then
    rm -f "$stderr_file"
    echo "$output"
  else
    local rc=$?
    echo "WARNING: 'gh api $1' failed (exit $rc): $(cat "$stderr_file")" >&2
    rm -f "$stderr_file"
    return 0  # non-fatal — prompt is built with missing section
  fi
}

# Fetch last 10 comments on an issue/PR
fetch_recent_comments() {
  local issue_number="$1"
  gh_api_safe "repos/${GITHUB_REPOSITORY}/issues/${issue_number}/comments" \
    --jq '.[-10:][] | "@\(.user.login): \(.body)"' \
    | while IFS= read -r line; do
        truncate_text "$line" "$MAX_COMMENT_CHARS"
      done
}

# Fetch changed files for a PR
fetch_pr_files() {
  local pr_number="$1"
  gh_api_safe "repos/${GITHUB_REPOSITORY}/pulls/${pr_number}/files" \
    --jq '.[].filename'
}

# Fetch PR details
fetch_pr_details() {
  local pr_number="$1"
  gh_api_safe "repos/${GITHUB_REPOSITORY}/pulls/${pr_number}" \
    --jq '{title: .title, body: .body, state: .state, merged: .merged, base: .base.ref, head: .head.ref, html_url: .html_url}'
}

# ---------------------------------------------------------------------------
# Build prompt based on event type
# ---------------------------------------------------------------------------

build_prompt() {
  local prompt=""

  case "$GITHUB_EVENT_NAME" in
    issue_comment)
      local issue_number
      local comment_body
      local issue_title
      local issue_body
      local issue_state
      local issue_labels
      local issue_url
      local pr_url

      issue_number=$(jq_raw '.issue.number')
      comment_body=$(jq_raw '.comment.body')
      issue_title=$(jq_raw '.issue.title')
      issue_body=$(truncate_text "$(jq_raw '.issue.body // ""')" "$MAX_BODY_CHARS")
      issue_state=$(jq_raw '.issue.state')
      issue_labels=$(jq_raw '[.issue.labels[].name] | join(", ") // "none"')
      issue_url=$(jq_raw '.issue.html_url')
      pr_url=$(jq_raw '.issue.pull_request.url // empty' 2>/dev/null || true)

      if [[ -n "$pr_url" && "$pr_url" != "null" ]]; then
        # This is a comment on a PR (via the issues API)
        local pr_details
        pr_details=$(fetch_pr_details "$issue_number")
        local pr_state pr_base pr_head pr_merged pr_body_raw pr_body
        pr_state=$(echo "$pr_details" | jq -r '.state')
        pr_base=$(echo "$pr_details" | jq -r '.base')
        pr_head=$(echo "$pr_details" | jq -r '.head')
        pr_merged=$(echo "$pr_details" | jq -r '.merged')
        pr_body_raw=$(echo "$pr_details" | jq -r '.body // ""')
        pr_body=$(truncate_text "$pr_body_raw" "$MAX_BODY_CHARS")

        local pr_display_state="$pr_state"
        if [[ "$pr_merged" == "true" ]]; then
          pr_display_state="merged"
        fi

        local changed_files
        changed_files=$(fetch_pr_files "$issue_number")

        local recent_comments
        recent_comments=$(fetch_recent_comments "$issue_number")

        prompt="You were mentioned in a GitHub pull request comment.

Repository: ${GITHUB_REPOSITORY}
Pull Request: #${issue_number} — ${issue_title}
PR URL: ${issue_url}
PR state: ${pr_display_state}
PR base: ${pr_base} <- ${pr_head}

## PR description
${pr_body}

## Changed files
${changed_files}

## Recent conversation (last 10 comments)
${recent_comments}

## Your task
@jwbron said: ${comment_body}

You are checked out on the PR's head branch (${pr_head}). Read the
conversation and perform the requested task. You can modify code, push
commits, and post comments."

      else
        # This is a comment on an issue
        local recent_comments
        recent_comments=$(fetch_recent_comments "$issue_number")

        prompt="You were mentioned in a GitHub issue comment.

Repository: ${GITHUB_REPOSITORY}
Issue: #${issue_number} — ${issue_title}
Issue URL: ${issue_url}
Issue state: ${issue_state}
Issue labels: ${issue_labels}

## Issue description
${issue_body}

## Recent conversation (last 10 comments)
${recent_comments}

## Your task
@jwbron said: ${comment_body}

Read the conversation above and perform the requested task. After completing
your work, post a comment on the issue summarizing what you did."
      fi
      ;;

    pull_request_review_comment)
      local pr_number
      local pr_title
      local pr_url
      local pr_head
      local pr_base
      local comment_body
      local comment_path
      local comment_line
      local diff_hunk

      pr_number=$(jq_raw '.pull_request.number')
      pr_title=$(jq_raw '.pull_request.title')
      pr_url=$(jq_raw '.pull_request.html_url')
      pr_head=$(jq_raw '.pull_request.head.ref')
      pr_base=$(jq_raw '.pull_request.base.ref')
      comment_body=$(jq_raw '.comment.body')
      comment_path=$(jq_raw '.comment.path')
      comment_line=$(jq_raw '.comment.line // .comment.original_line // "unknown"')
      diff_hunk=$(truncate_text "$(jq_raw '.comment.diff_hunk // ""')" "$MAX_BODY_CHARS")

      local pr_details
      pr_details=$(fetch_pr_details "$pr_number")
      local pr_body_raw pr_body pr_state pr_merged pr_display_state
      pr_state=$(echo "$pr_details" | jq -r '.state')
      pr_merged=$(echo "$pr_details" | jq -r '.merged')
      pr_body_raw=$(echo "$pr_details" | jq -r '.body // ""')
      pr_body=$(truncate_text "$pr_body_raw" "$MAX_BODY_CHARS")
      pr_display_state="$pr_state"
      if [[ "$pr_merged" == "true" ]]; then
        pr_display_state="merged"
      fi

      local changed_files
      changed_files=$(fetch_pr_files "$pr_number")

      local recent_comments
      recent_comments=$(fetch_recent_comments "$pr_number")

      prompt="You were mentioned in an inline code review comment.

Repository: ${GITHUB_REPOSITORY}
Pull Request: #${pr_number} — ${pr_title}
PR URL: ${pr_url}
PR state: ${pr_display_state}
PR base: ${pr_base} <- ${pr_head}

## PR description
${pr_body}

## Changed files
${changed_files}

## Recent conversation (last 10 comments)
${recent_comments}

## Review comment context
File: ${comment_path}
Line: ${comment_line}
Diff hunk:
${diff_hunk}

Comment by @jwbron:
${comment_body}

## Your task
Address this inline review comment. You are checked out on the PR's head
branch (${pr_head}). Make the requested changes, commit, and push. Then reply to the
review comment confirming what you changed."
      ;;

    pull_request_review)
      local pr_number
      local pr_title
      local pr_url
      local pr_head
      local pr_base
      local review_body
      local review_state

      pr_number=$(jq_raw '.pull_request.number')
      pr_title=$(jq_raw '.pull_request.title')
      pr_url=$(jq_raw '.pull_request.html_url')
      pr_head=$(jq_raw '.pull_request.head.ref')
      pr_base=$(jq_raw '.pull_request.base.ref')
      review_body=$(jq_raw '.review.body')
      review_state=$(jq_raw '.review.state')

      local pr_details
      pr_details=$(fetch_pr_details "$pr_number")
      local pr_body_raw pr_body pr_state pr_merged pr_display_state
      pr_state=$(echo "$pr_details" | jq -r '.state')
      pr_merged=$(echo "$pr_details" | jq -r '.merged')
      pr_body_raw=$(echo "$pr_details" | jq -r '.body // ""')
      pr_body=$(truncate_text "$pr_body_raw" "$MAX_BODY_CHARS")
      pr_display_state="$pr_state"
      if [[ "$pr_merged" == "true" ]]; then
        pr_display_state="merged"
      fi

      local changed_files
      changed_files=$(fetch_pr_files "$pr_number")

      local recent_comments
      recent_comments=$(fetch_recent_comments "$pr_number")

      # Fetch review comments (inline comments attached to this review)
      local review_comments=""
      local review_id
      review_id=$(jq_raw '.review.id')
      review_comments=$(gh_api_safe "repos/${GITHUB_REPOSITORY}/pulls/${pr_number}/reviews/${review_id}/comments" \
        --jq '.[] | "### \(.path):\(.line // .original_line // "?")\n\(.diff_hunk)\n\n\(.body)\n"' 2>/dev/null || true)

      prompt="You were mentioned in a pull request review submission.

Repository: ${GITHUB_REPOSITORY}
Pull Request: #${pr_number} — ${pr_title}
PR URL: ${pr_url}
PR state: ${pr_display_state}
PR base: ${pr_base} <- ${pr_head}
Review state: ${review_state}

## PR description
${pr_body}

## Changed files
${changed_files}

## Recent conversation (last 10 comments)
${recent_comments}

## Review body
${review_body}
"

      if [[ -n "$review_comments" ]]; then
        prompt+="
## Inline review comments
${review_comments}
"
      fi

      prompt+="
## Your task
Address the review feedback above. You are checked out on the PR's head
branch (${pr_head}). Make the requested changes, commit, and push. Then reply
to the review confirming what you changed."
      ;;

    issues)
      local issue_number
      local issue_title
      local issue_url
      local issue_body
      local issue_labels

      issue_number=$(jq_raw '.issue.number')
      issue_title=$(jq_raw '.issue.title')
      issue_url=$(jq_raw '.issue.html_url')
      issue_body=$(truncate_text "$(jq_raw '.issue.body // ""')" "$MAX_BODY_CHARS")
      issue_labels=$(jq_raw '[.issue.labels[].name] | join(", ") // "none"')

      prompt="A new GitHub issue was opened mentioning you.

Repository: ${GITHUB_REPOSITORY}
Issue: #${issue_number} — ${issue_title}
Issue URL: ${issue_url}
Labels: ${issue_labels}

## Issue description
${issue_body}

## Your task
Read the issue above and work on it. Create a branch, implement the
changes, write tests, and open a pull request. Post a comment on the
issue with a link to the PR."
      ;;

    *)
      echo "ERROR: Unsupported event type: $GITHUB_EVENT_NAME" >&2
      exit 1
      ;;
  esac

  # Truncate overall prompt if needed
  prompt=$(truncate_text "$prompt" "$MAX_PROMPT_CHARS")

  # Write multiline output using heredoc delimiter
  {
    echo "prompt<<__EGG_PROMPT_BOUNDARY_7f3a9c__"
    echo "$prompt"
    echo "__EGG_PROMPT_BOUNDARY_7f3a9c__"
  } >> "${GITHUB_OUTPUT:-/dev/null}"

  echo "Prompt built for event: $GITHUB_EVENT_NAME (${#prompt} chars)"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

: "${GITHUB_EVENT_NAME:?GITHUB_EVENT_NAME is required}"
: "${GITHUB_EVENT_PATH:?GITHUB_EVENT_PATH is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"

build_prompt
