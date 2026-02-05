#!/usr/bin/env bash
# lib.sh — Shared helpers for egg GitHub Actions prompt-building scripts
#
# Source this file from other scripts:
#   source "$(dirname "$0")/lib.sh"
#
# Provides:
#   truncate_text TEXT MAX_CHARS  — Truncate text with "... (truncated)" suffix
#   jq_raw EXPR                  — Parse $GITHUB_EVENT_PATH with jq -r
#   gh_api_safe ARGS...          — Non-fatal wrapper around gh api
#   fetch_recent_comments NUM    — Fetch last 10 comments on an issue/PR
#   fetch_pr_files NUM           — Fetch changed files for a PR
#   fetch_pr_details NUM         — Fetch PR metadata as JSON
#   emit_prompt PROMPT           — Write prompt to $GITHUB_OUTPUT
#
# Expected environment:
#   GITHUB_REPOSITORY  — owner/repo
#   GITHUB_OUTPUT      — path to output file (optional, defaults to /dev/null)
#   GITHUB_EVENT_PATH  — path to event JSON (required by jq_raw)

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

MAX_BODY_CHARS="${MAX_BODY_CHARS:-10000}"
MAX_COMMENT_CHARS="${MAX_COMMENT_CHARS:-2000}"
MAX_PROMPT_CHARS="${MAX_PROMPT_CHARS:-50000}"
MAX_LOG_CHARS="${MAX_LOG_CHARS:-10000}"

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

# Write prompt to $GITHUB_OUTPUT using heredoc delimiter
emit_prompt() {
  local prompt="$1"

  # Truncate overall prompt if needed
  prompt=$(truncate_text "$prompt" "$MAX_PROMPT_CHARS")

  {
    echo "prompt<<__EGG_PROMPT_BOUNDARY_7f3a9c__"
    echo "$prompt"
    echo "__EGG_PROMPT_BOUNDARY_7f3a9c__"
  } >> "${GITHUB_OUTPUT:-/dev/null}"

  echo "Prompt built (${#prompt} chars)"
}
