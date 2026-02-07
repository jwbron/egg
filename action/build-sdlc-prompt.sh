#!/usr/bin/env bash
# build-sdlc-prompt.sh — Build a phase-specific prompt for SDLC pipeline
#
# Provides orientation context (issue number, current phase, branch) and
# includes phase-specific instructions and document templates.
#
# Environment variables:
#   GITHUB_REPOSITORY  — owner/repo
#   EGG_ISSUE_NUMBER   — GitHub issue number
#   EGG_PIPELINE_PHASE — Current phase (refine, plan, implement, pr)
#   EGG_BRANCH_NAME    — Current branch name
#   EGG_REPO_PATH      — Path to repository (optional, defaults to /home/egg/repos/*)
#
# Output:
#   Sets 'prompt' in $GITHUB_OUTPUT (multiline)

set -euo pipefail

MAX_BODY_CHARS=10000
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

# Wrapper around gh api that warns on failure and returns empty JSON object
gh_api_safe() {
  local stderr_file
  stderr_file=$(mktemp)
  # Ensure temp file is cleaned up on exit (including signals)
  trap 'rm -f "$stderr_file"' RETURN
  local output
  if output=$(gh api "$@" 2>"$stderr_file"); then
    echo "$output"
  else
    local rc=$?
    echo "ERROR: 'gh api $1' failed (exit $rc): $(cat "$stderr_file")" >&2
    echo "The prompt will be built with placeholder values for issue metadata." >&2
    # Return empty JSON object so jq doesn't fail
    echo "{}"
  fi
}

# Find the templates directory
find_templates_dir() {
  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  local repo_root="${script_dir}/.."
  echo "${repo_root}/docs/templates"
}

# Get contract state summary
get_contract_summary() {
  local issue_number="$1"
  local repo_path="${EGG_REPO_PATH:-$(find /home/egg/repos -maxdepth 1 -type d ! -name repos | head -1)}"

  if [[ -f "${repo_path}/.egg-state/contracts/${issue_number}.json" ]]; then
    local contract
    contract=$(cat "${repo_path}/.egg-state/contracts/${issue_number}.json")

    local current_phase
    current_phase=$(echo "$contract" | jq -r '.current_phase // "unknown"')

    local phases_summary=""
    while IFS= read -r phase; do
      local phase_id phase_name phase_status
      phase_id=$(echo "$phase" | jq -r '.id')
      phase_name=$(echo "$phase" | jq -r '.name')
      phase_status=$(echo "$phase" | jq -r '.status')
      phases_summary+="  - ${phase_id}: ${phase_name} [${phase_status}]"$'\n'

      # Add task summary
      while IFS= read -r task; do
        local task_id task_desc task_status
        task_id=$(echo "$task" | jq -r '.id')
        task_desc=$(echo "$task" | jq -r '.description')
        task_status=$(echo "$task" | jq -r '.status')
        phases_summary+="    - ${task_id}: ${task_desc} [${task_status}]"$'\n'
      done < <(echo "$phase" | jq -c '.tasks[]?' 2>/dev/null || true)
    done < <(echo "$contract" | jq -c '.phases[]?' 2>/dev/null || true)

    echo "Current phase: ${current_phase}"
    if [[ -n "$phases_summary" ]]; then
      echo ""
      echo "Implementation phases:"
      echo "$phases_summary"
    fi
  else
    echo "No contract found for issue #${issue_number}"
  fi
}

# ---------------------------------------------------------------------------
# Build phase-specific prompt
# ---------------------------------------------------------------------------

build_refine_prompt() {
  local issue_number="$1"
  local issue_title="$2"
  local issue_body="$3"
  local issue_url="$4"
  local templates_dir
  templates_dir=$(find_templates_dir)

  local analysis_template=""
  if [[ -f "${templates_dir}/analysis.md" ]]; then
    analysis_template=$(cat "${templates_dir}/analysis.md")
  fi

  cat <<EOF
You are in the **refine** phase of the SDLC pipeline.

## Context

Repository: ${GITHUB_REPOSITORY}
Issue: #${issue_number} — ${issue_title}
Issue URL: ${issue_url}
Phase: refine
Branch: ${EGG_BRANCH_NAME}

## Issue Description

${issue_body}

## Your Task

Analyze this issue and produce a structured analysis document. Your goal is to:

1. Understand the problem or feature request
2. Research the current codebase to understand existing patterns
3. Identify constraints and dependencies
4. Consider multiple implementation approaches
5. Recommend an approach with justification
6. Surface any questions that need human input

## Output Format

Create an analysis document following this template:

\`\`\`markdown
${analysis_template}
\`\`\`

## Phase Restrictions

In the refine phase:
- You CAN comment on the issue (gh issue comment)
- You CAN create HITL decisions (egg-contract add-decision)
- You CANNOT push code (git push)
- You CANNOT create PRs (gh pr create)

## Next Steps

When your analysis is complete:
1. Post the analysis as an issue comment
2. If you have open questions, use \`egg-contract add-decision --question "..."\`
3. Wait for human approval to advance to the plan phase
EOF
}

build_plan_prompt() {
  local issue_number="$1"
  local issue_title="$2"
  local issue_body="$3"
  local issue_url="$4"
  local templates_dir
  templates_dir=$(find_templates_dir)

  local plan_template=""
  if [[ -f "${templates_dir}/plan.md" ]]; then
    plan_template=$(cat "${templates_dir}/plan.md")
  fi

  cat <<EOF
You are in the **plan** phase of the SDLC pipeline.

## Context

Repository: ${GITHUB_REPOSITORY}
Issue: #${issue_number} — ${issue_title}
Issue URL: ${issue_url}
Phase: plan
Branch: ${EGG_BRANCH_NAME}

## Issue Description

${issue_body}

## Your Task

Create a detailed implementation plan. Your goal is to:

1. Review any prior analysis (check issue comments)
2. Break down the work into phases with discrete tasks
3. Define clear acceptance criteria for each task
4. Identify test strategy
5. Consider rollback and risks

## Task ID Format

Tasks MUST be marked with explicit IDs using this format:
- \`[TASK-{phase}-{number}]\` — e.g., \`[TASK-1-1]\`, \`[TASK-2-3]\`

Example:
\`\`\`
- [TASK-1-1] Create contract JSON schema — Acceptance: schema validates sample contracts
- [TASK-1-2] Add Pydantic models — Acceptance: models match schema, unit tests pass
\`\`\`

These IDs will be extracted into the contract for tracking.

## Output Format

Create a plan document following this template:

\`\`\`markdown
${plan_template}
\`\`\`

## Phase Restrictions

In the plan phase:
- You CAN comment on the issue (gh issue comment)
- You CAN create HITL decisions (egg-contract add-decision)
- You CANNOT push code (git push)
- You CANNOT create PRs (gh pr create)

## Next Steps

When your plan is complete:
1. Post the plan as an issue comment
2. If you have open questions, use \`egg-contract add-decision --question "..."\`
3. Wait for human approval to advance to the implement phase
EOF
}

build_implement_prompt() {
  local issue_number="$1"
  local issue_title="$2"
  local issue_body="$3"
  local issue_url="$4"
  local contract_summary
  contract_summary=$(get_contract_summary "$issue_number")

  cat <<EOF
You are in the **implement** phase of the SDLC pipeline.

## Context

Repository: ${GITHUB_REPOSITORY}
Issue: #${issue_number} — ${issue_title}
Issue URL: ${issue_url}
Phase: implement
Branch: ${EGG_BRANCH_NAME}

## Contract State

${contract_summary}

## Issue Description

${issue_body}

## Your Task

Implement the tasks defined in the contract. For each task:

1. Check the task status with \`egg-contract show\`
2. Implement the required changes
3. Run tests to verify
4. Commit with a descriptive message
5. Link the commit: \`egg-contract add-commit --task task-1 --commit <sha>\`
6. Add notes if helpful: \`egg-contract update-notes --task task-1 --notes "..."\`

## Contract CLI Commands

- \`egg-contract show\` — View current contract state
- \`egg-contract add-commit --task <id> --commit <sha>\` — Link commit to task
- \`egg-contract update-notes --task <id> --notes <text>\` — Add implementation notes

## Phase Restrictions

In the implement phase:
- You CAN push code (git push)
- You CAN link commits to tasks (egg-contract add-commit)
- You CAN add notes (egg-contract update-notes)
- You CANNOT create PRs yet (gh pr create)

## Quality Checklist

Before advancing to PR phase:
- [ ] All tasks have linked commits
- [ ] Tests pass
- [ ] Linters pass
- [ ] No debug code left behind

## Next Steps

When implementation is complete:
1. Ensure all tasks are linked to commits
2. Push your changes: \`git push origin ${EGG_BRANCH_NAME}\`
3. Wait for reviewer to mark tasks complete
EOF
}

build_pr_prompt() {
  local issue_number="$1"
  local issue_title="$2"
  local issue_body="$3"
  local issue_url="$4"
  local contract_summary
  contract_summary=$(get_contract_summary "$issue_number")

  cat <<EOF
You are in the **pr** phase of the SDLC pipeline.

## Context

Repository: ${GITHUB_REPOSITORY}
Issue: #${issue_number} — ${issue_title}
Issue URL: ${issue_url}
Phase: pr
Branch: ${EGG_BRANCH_NAME}

## Contract State

${contract_summary}

## Issue Description

${issue_body}

## Your Task

Create a pull request for this implementation.

1. Ensure all commits are pushed
2. Create the PR with a descriptive title and body
3. Reference the issue in the PR description
4. Wait for human review and approval

## PR Format

\`\`\`bash
gh pr create --title "Brief description" --body "\$(cat <<'BODY'
## Summary
<1-3 bullet points>

## Test plan
<Steps for reviewers>

Issue: ${issue_url}

Authored-by: egg
BODY
)"
\`\`\`

## Phase Restrictions

In the PR phase:
- You CAN create and edit PRs (gh pr create, gh pr edit)
- You CAN push additional commits
- You CANNOT merge PRs (human must merge)

## Next Steps

After creating the PR:
1. Wait for human review
2. Address any feedback by pushing additional commits
3. Human will merge when ready
EOF
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

build_prompt() {
  local phase="${EGG_PIPELINE_PHASE:-implement}"
  local issue_number="${EGG_ISSUE_NUMBER:?EGG_ISSUE_NUMBER is required}"

  # Fetch issue details
  local issue_data
  issue_data=$(gh_api_safe "repos/${GITHUB_REPOSITORY}/issues/${issue_number}")

  local issue_title issue_body issue_url
  issue_title=$(echo "$issue_data" | jq -r '.title // "Unknown"')
  issue_body=$(truncate_text "$(echo "$issue_data" | jq -r '.body // ""')" "$MAX_BODY_CHARS")
  issue_url=$(echo "$issue_data" | jq -r '.html_url // ""')

  local prompt=""

  case "$phase" in
    refine)
      prompt=$(build_refine_prompt "$issue_number" "$issue_title" "$issue_body" "$issue_url")
      ;;
    plan)
      prompt=$(build_plan_prompt "$issue_number" "$issue_title" "$issue_body" "$issue_url")
      ;;
    implement)
      prompt=$(build_implement_prompt "$issue_number" "$issue_title" "$issue_body" "$issue_url")
      ;;
    pr)
      prompt=$(build_pr_prompt "$issue_number" "$issue_title" "$issue_body" "$issue_url")
      ;;
    *)
      echo "ERROR: Unknown phase: $phase" >&2
      exit 1
      ;;
  esac

  # Truncate overall prompt if needed
  prompt=$(truncate_text "$prompt" "$MAX_PROMPT_CHARS")

  # Generate a unique delimiter by appending random suffix
  # This prevents delimiter injection if issue body contains our base delimiter
  local random_suffix
  random_suffix=$(head -c 16 /dev/urandom | xxd -p | head -c 16)
  local delimiter="__EGG_PROMPT_BOUNDARY_${random_suffix}__"

  # Write multiline output using heredoc delimiter
  {
    echo "prompt<<${delimiter}"
    echo "$prompt"
    echo "${delimiter}"
  } >> "${GITHUB_OUTPUT:-/dev/null}"

  echo "SDLC prompt built for phase: $phase (${#prompt} chars)"
}

: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${EGG_ISSUE_NUMBER:?EGG_ISSUE_NUMBER is required}"

build_prompt
