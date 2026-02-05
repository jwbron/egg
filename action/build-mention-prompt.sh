#!/usr/bin/env bash
# build-mention-prompt.sh — Build a context-rich prompt from a GitHub event payload
#
# Reads $GITHUB_EVENT_PATH (JSON event payload) and uses gh CLI to fetch
# additional context, then outputs a structured prompt via $GITHUB_OUTPUT.
#
# Environment variables:
#   GITHUB_EVENT_NAME  — event type (issue_comment, pull_request_review_comment, issues)
#   GITHUB_EVENT_PATH  — path to event JSON payload
#   GITHUB_REPOSITORY  — owner/repo
#   BOT_USERNAME       — bot name to strip from mention (default: james-in-a-box)
#
# Output:
#   Sets 'prompt' in $GITHUB_OUTPUT (multiline)

set -euo pipefail

# shellcheck source=action/lib.sh
source "$(dirname "$0")/lib.sh"

BOT_USERNAME="${BOT_USERNAME:-james-in-a-box}"

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

  emit_prompt "$prompt"
  echo "Event: $GITHUB_EVENT_NAME"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

: "${GITHUB_EVENT_NAME:?GITHUB_EVENT_NAME is required}"
: "${GITHUB_EVENT_PATH:?GITHUB_EVENT_PATH is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"

build_prompt
