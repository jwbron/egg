#!/usr/bin/env bash
# build-coder-prompt.sh — Build a focused prompt for the Coder agent
#
# The Coder agent is responsible for implementing code changes based on the
# plan tasks. It runs first in the multi-agent pipeline and produces a list
# of changed files for downstream agents (Tester, Documenter).
#
# Environment variables:
#   GITHUB_REPOSITORY  — owner/repo
#   EGG_ISSUE_NUMBER   — GitHub issue number
#   EGG_BRANCH_NAME    — Current branch name
#   EGG_REPO_PATH      — Path to repository (optional, defaults to /home/egg/repos/*)
#
# Output:
#   Sets 'prompt' in $GITHUB_OUTPUT (multiline)

set -euo pipefail

MAX_BODY_CHARS=10000
MAX_PROMPT_CHARS=50000

# ---------------------------------------------------------------------------
# Helpers (shared with build-sdlc-prompt.sh)
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

gh_api_safe() {
  local stderr_file
  stderr_file=$(mktemp)
  trap 'rm -f "$stderr_file"' RETURN
  local output
  if output=$(gh api "$@" 2>"$stderr_file"); then
    echo "$output"
  else
    local rc=$?
    echo "ERROR: 'gh api $1' failed (exit $rc): $(cat "$stderr_file")" >&2
    echo "{}"
  fi
}

get_repo_path() {
  echo "${EGG_REPO_PATH:-$(find /home/egg/repos -maxdepth 1 -type d ! -name repos | head -1)}"
}

get_contract_tasks() {
  local issue_number="$1"
  local repo_path
  repo_path=$(get_repo_path)

  if [[ -f "${repo_path}/.egg-state/contracts/${issue_number}.json" ]]; then
    local contract
    contract=$(cat "${repo_path}/.egg-state/contracts/${issue_number}.json")

    local tasks_output=""
    while IFS= read -r phase; do
      local phase_id phase_name phase_status
      phase_id=$(echo "$phase" | jq -r '.id')
      phase_name=$(echo "$phase" | jq -r '.name')
      phase_status=$(echo "$phase" | jq -r '.status')
      tasks_output+="### ${phase_name} [${phase_status}]"$'\n'$'\n'

      while IFS= read -r task; do
        local task_id task_desc task_status task_commit task_files
        task_id=$(echo "$task" | jq -r '.id')
        task_desc=$(echo "$task" | jq -r '.description')
        task_status=$(echo "$task" | jq -r '.status')
        task_commit=$(echo "$task" | jq -r '.commit // "none"')
        task_files=$(echo "$task" | jq -r '.files_affected | join(", ") // ""')

        tasks_output+="- **${task_id}**: ${task_desc}"$'\n'
        tasks_output+="  - Status: ${task_status}"$'\n'
        if [[ "$task_commit" != "none" && "$task_commit" != "null" ]]; then
          tasks_output+="  - Commit: ${task_commit}"$'\n'
        fi
        if [[ -n "$task_files" ]]; then
          tasks_output+="  - Files: ${task_files}"$'\n'
        fi
        tasks_output+=$'\n'
      done < <(echo "$phase" | jq -c '.tasks[]?' 2>/dev/null || true)
    done < <(echo "$contract" | jq -c '.phases[]?' 2>/dev/null || true)

    echo "$tasks_output"
  else
    echo "No contract found for issue #${issue_number}"
  fi
}

get_pending_tasks() {
  local issue_number="$1"
  local repo_path
  repo_path=$(get_repo_path)

  if [[ -f "${repo_path}/.egg-state/contracts/${issue_number}.json" ]]; then
    jq -r '.phases[].tasks[] | select(.status == "pending" or .status == "in_progress") | "- \(.id): \(.description)"' \
      "${repo_path}/.egg-state/contracts/${issue_number}.json" 2>/dev/null || true
  fi
}

# ---------------------------------------------------------------------------
# Build Coder Prompt
# ---------------------------------------------------------------------------

build_coder_prompt() {
  local issue_number="${EGG_ISSUE_NUMBER:?EGG_ISSUE_NUMBER is required}"

  # Fetch issue details
  local issue_data
  issue_data=$(gh_api_safe "repos/${GITHUB_REPOSITORY}/issues/${issue_number}")

  local issue_title issue_body issue_url
  issue_title=$(echo "$issue_data" | jq -r '.title // "Unknown"')
  issue_body=$(truncate_text "$(echo "$issue_data" | jq -r '.body // ""')" "$MAX_BODY_CHARS")
  issue_url=$(echo "$issue_data" | jq -r '.html_url // ""')

  local contract_tasks
  contract_tasks=$(get_contract_tasks "$issue_number")

  local pending_tasks
  pending_tasks=$(get_pending_tasks "$issue_number")

  # Use quoted heredoc ('EOF') to prevent shell interpretation of content
  # Variables are explicitly expanded only where safe
  cat <<'EOF'
You are the **Coder** agent in a multi-agent SDLC pipeline.

## Your Role

You implement code changes based on the plan tasks. You are the first agent
to run, and your output (list of changed files) will be used by downstream
agents (Tester, Documenter).

## Context

EOF
  # Safely output dynamic content with printf to prevent injection
  printf 'Repository: %s\n' "${GITHUB_REPOSITORY}"
  printf 'Issue: #%s — %s\n' "${issue_number}" "${issue_title}"
  printf 'Issue URL: %s\n' "${issue_url}"
  printf 'Branch: %s\n' "${EGG_BRANCH_NAME}"
  cat <<'EOF'
Agent Role: **Coder**

## Issue Description

EOF
  printf '%s\n\n' "${issue_body}"
  cat <<'EOF'
## Contract Tasks

EOF
  printf '%s\n' "${contract_tasks}"
  cat <<'EOF'

## Tasks To Complete

Focus on the following pending tasks:

EOF
  printf '%s\n' "${pending_tasks}"
  cat <<'EOF'
## Your Responsibilities

1. Read and understand the implementation plan
2. Implement code changes for the pending tasks
3. Run tests to verify your changes work
4. Commit with descriptive messages
5. Link commits to tasks using the contract CLI
6. Output the list of changed files for downstream agents

## File Access Constraints

As the Coder agent, you:
- **CAN** write to source code files (*.py, *.ts, *.tsx, *.js, *.go, etc.)
- **CAN** write to configuration files (*.yml, *.yaml, *.json)
- **CANNOT** write to documentation files (docs/, README.md)
- **CANNOT** write to contract files (.egg-state/contracts/)

Documentation updates will be handled by the Documenter agent.

## Contract CLI Commands

Link your commits to tasks as you complete them:

\`\`\`bash
# Link a commit to a task
egg-contract add-commit --task task-1-1 --commit \$(git rev-parse HEAD)

# Add implementation notes
egg-contract update-notes --task task-1-1 --notes "Implemented X using Y approach"

# View current contract state
egg-contract show
\`\`\`

## Handoff Output

When you complete your work, write a handoff file for downstream agents:

\`\`\`bash
mkdir -p .egg-state/agent-outputs
cat > .egg-state/agent-outputs/coder-output.json << 'HANDOFF_EOF'
{
  "changed_files": [
    "path/to/file1.py",
    "path/to/file2.ts"
  ],
  "commits": [
    "abc1234",
    "def5678"
  ],
  "summary": "Brief description of changes"
}
HANDOFF_EOF
\`\`\`

## Quality Checklist

Before completing:
- [ ] All pending tasks have been implemented
- [ ] Each task has a linked commit
- [ ] Tests pass (run the test suite)
- [ ] Linters pass (run the linter)
- [ ] No debug code left behind
- [ ] Handoff file written with changed files list

## Next Steps

1. Implement the pending tasks one by one
2. Commit and link each task as you complete it
3. Write the handoff output file
EOF
  printf '4. Push your changes: `git push origin HEAD:%s`\n' "${EGG_BRANCH_NAME}"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${EGG_ISSUE_NUMBER:?EGG_ISSUE_NUMBER is required}"

prompt=$(build_coder_prompt)
prompt=$(truncate_text "$prompt" "$MAX_PROMPT_CHARS")

# Generate unique delimiter
random_suffix=$(head -c 16 /dev/urandom | xxd -p | head -c 16)
delimiter="__EGG_PROMPT_BOUNDARY_${random_suffix}__"

# Write multiline output
{
  echo "prompt<<${delimiter}"
  echo "$prompt"
  echo "${delimiter}"
} >> "${GITHUB_OUTPUT:-/dev/null}"

echo "Coder agent prompt built (${#prompt} chars)"
