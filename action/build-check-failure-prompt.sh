#!/usr/bin/env bash
# build-check-failure-prompt.sh — Build a prompt from CI check failure logs
#
# Fetches failure logs from a GitHub Actions workflow run and constructs a
# diagnostic prompt so the egg agent can fix the issue.
#
# Environment variables:
#   GITHUB_REPOSITORY  — owner/repo
#   FAILED_RUN_ID      — workflow run ID that failed
#   FAILED_WORKFLOW     — name of the failed workflow (e.g., "Lint", "Test")
#   HEAD_BRANCH        — PR branch name
#   HEAD_SHA           — commit SHA that triggered the failure
#
# Output:
#   Sets 'prompt' in $GITHUB_OUTPUT (multiline)

set -euo pipefail

# shellcheck source=action/lib.sh
source "$(dirname "$0")/lib.sh"

: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${FAILED_RUN_ID:?FAILED_RUN_ID is required}"
: "${FAILED_WORKFLOW:?FAILED_WORKFLOW is required}"
: "${HEAD_BRANCH:?HEAD_BRANCH is required}"
: "${HEAD_SHA:?HEAD_SHA is required}"

# ---------------------------------------------------------------------------
# Fetch failure data
# ---------------------------------------------------------------------------

# Find the PR number for this branch
find_pr_number() {
  gh_api_safe "repos/${GITHUB_REPOSITORY}/pulls" \
    -f head="${GITHUB_REPOSITORY%%/*}:${HEAD_BRANCH}" \
    -f state=open \
    --jq '.[0].number // empty'
}

# Fetch failed jobs and their logs
fetch_failed_jobs() {
  local run_id="$1"
  gh_api_safe "repos/${GITHUB_REPOSITORY}/actions/runs/${run_id}/jobs" \
    --jq '.jobs[] | select(.conclusion == "failure") | {id: .id, name: .name, steps: [.steps[] | select(.conclusion == "failure") | {name: .name, number: .number}]}'
}

# Fetch logs for a specific job (returns plain text)
fetch_job_logs() {
  local job_id="$1"
  # gh api returns raw log text for this endpoint
  local logs
  logs=$(gh_api_safe "repos/${GITHUB_REPOSITORY}/actions/jobs/${job_id}/logs")
  if [[ -z "$logs" ]]; then
    logs="(logs unavailable)"
  fi
  truncate_text "$logs" "$MAX_LOG_CHARS"
}

# Fetch changed files for the PR
fetch_pr_changed_files() {
  local pr_number="$1"
  if [[ -n "$pr_number" && "$pr_number" != "null" ]]; then
    fetch_pr_files "$pr_number"
  fi
}

# ---------------------------------------------------------------------------
# Build the prompt
# ---------------------------------------------------------------------------

build_failure_prompt() {
  local pr_number
  pr_number=$(find_pr_number)

  local pr_title=""
  local pr_url=""
  local changed_files=""

  if [[ -n "$pr_number" && "$pr_number" != "null" ]]; then
    local pr_details
    pr_details=$(fetch_pr_details "$pr_number")
    pr_title=$(echo "$pr_details" | jq -r '.title // ""')
    pr_url=$(echo "$pr_details" | jq -r '.html_url // ""')
    changed_files=$(fetch_pr_changed_files "$pr_number")
  fi

  # Collect failed job info and logs
  local failed_jobs_json
  failed_jobs_json=$(fetch_failed_jobs "$FAILED_RUN_ID")

  local failure_details=""
  while IFS= read -r job_json; do
    [[ -z "$job_json" ]] && continue
    local job_id job_name failed_steps
    job_id=$(echo "$job_json" | jq -r '.id')
    job_name=$(echo "$job_json" | jq -r '.name')
    failed_steps=$(echo "$job_json" | jq -r '.steps[] | "- \(.name)"')

    local job_logs
    job_logs=$(fetch_job_logs "$job_id")

    failure_details+="### Job: ${job_name}
Failed steps:
${failed_steps}

Log output:
\`\`\`
${job_logs}
\`\`\`

"
  done <<< "$failed_jobs_json"

  # Fall back if no structured failure details were captured
  if [[ -z "$failure_details" ]]; then
    failure_details="(Could not fetch detailed failure logs. Check the workflow run URL for details.)"
  fi

  local prompt="CI checks failed on your PR and need to be fixed.

Repository: ${GITHUB_REPOSITORY}
Pull Request: #${pr_number:-unknown} — ${pr_title:-unknown}
PR URL: ${pr_url:-unknown}
PR branch: ${HEAD_BRANCH}
Failed commit: ${HEAD_SHA}
Failed workflow: ${FAILED_WORKFLOW} (run #${FAILED_RUN_ID})

## Failed checks

${failure_details}

## PR changed files
${changed_files:-unknown}

## Your task
Analyze the CI failure logs above. Identify the root cause of each failure,
fix the code, commit, and push. Common fixes include:
- Lint errors: run the appropriate linter fix commands, formatting corrections
- Test failures: fix broken tests or the code they test
- Type errors: add/fix type annotations

After pushing fixes, post a comment on the PR summarizing what you changed."

  emit_prompt "$prompt"
  echo "Failed workflow: ${FAILED_WORKFLOW}, run: ${FAILED_RUN_ID}"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

build_failure_prompt
