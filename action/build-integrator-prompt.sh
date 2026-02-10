#!/usr/bin/env bash
# build-integrator-prompt.sh — Build a focused prompt for the Integrator agent
#
# The Integrator agent is responsible for running the full test suite and
# validating that all changes work together. It runs last in the multi-agent
# pipeline, after Coder and Tester have completed.
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

get_agent_output() {
  local agent="$1"
  local repo_path
  repo_path=$(get_repo_path)

  local handoff_file="${repo_path}/.egg-state/agent-outputs/${agent}-output.json"
  if [[ -f "$handoff_file" ]]; then
    cat "$handoff_file"
  else
    echo "{}"
  fi
}

summarize_agent_output() {
  local agent="$1"
  local output
  output=$(get_agent_output "$agent")

  if [[ "$output" == "{}" ]]; then
    echo "No output found from ${agent} agent"
    return
  fi

  local summary files
  summary=$(echo "$output" | jq -r '.summary // "No summary"')

  case "$agent" in
    coder)
      files=$(echo "$output" | jq -r '.changed_files[]? // empty' | head -10 | while read -r f; do echo "  - $f"; done)
      echo "**Summary**: ${summary}"
      if [[ -n "$files" ]]; then
        echo "**Changed files**:"
        echo "$files"
      fi
      ;;
    tester)
      files=$(echo "$output" | jq -r '.test_files[]? // empty' | head -10 | while read -r f; do echo "  - $f"; done)
      local issues
      issues=$(echo "$output" | jq -r '.issues_found | length // 0')
      echo "**Summary**: ${summary}"
      if [[ -n "$files" ]]; then
        echo "**Test files**:"
        echo "$files"
      fi
      if [[ "$issues" -gt 0 ]]; then
        echo "**Issues found**: ${issues}"
      fi
      ;;
    documenter)
      files=$(echo "$output" | jq -r '.doc_files[]? // empty' | head -10 | while read -r f; do echo "  - $f"; done)
      echo "**Summary**: ${summary}"
      if [[ -n "$files" ]]; then
        echo "**Documentation files**:"
        echo "$files"
      fi
      ;;
  esac
}

detect_test_command() {
  local repo_path
  repo_path=$(get_repo_path)

  # Check for common test configurations
  if [[ -f "${repo_path}/Makefile" ]] && grep -q "^test:" "${repo_path}/Makefile" 2>/dev/null; then
    echo "make test"
  elif [[ -f "${repo_path}/package.json" ]] && jq -e '.scripts.test' "${repo_path}/package.json" >/dev/null 2>&1; then
    echo "npm test"
  elif [[ -f "${repo_path}/pyproject.toml" ]] || [[ -f "${repo_path}/pytest.ini" ]] || [[ -f "${repo_path}/setup.py" ]]; then
    echo "pytest"
  elif [[ -f "${repo_path}/go.mod" ]]; then
    echo "go test ./..."
  else
    echo "# Unable to detect test command - check project configuration"
  fi
}

detect_lint_command() {
  local repo_path
  repo_path=$(get_repo_path)

  # Check for common lint configurations
  if [[ -f "${repo_path}/Makefile" ]] && grep -q "^lint:" "${repo_path}/Makefile" 2>/dev/null; then
    echo "make lint"
  elif [[ -f "${repo_path}/package.json" ]] && jq -e '.scripts.lint' "${repo_path}/package.json" >/dev/null 2>&1; then
    echo "npm run lint"
  elif [[ -f "${repo_path}/pyproject.toml" ]] || [[ -f "${repo_path}/setup.cfg" ]]; then
    echo "ruff check ."
  else
    echo "# Unable to detect lint command - check project configuration"
  fi
}

# ---------------------------------------------------------------------------
# Build Integrator Prompt
# ---------------------------------------------------------------------------

build_integrator_prompt() {
  local issue_number="${EGG_ISSUE_NUMBER:?EGG_ISSUE_NUMBER is required}"

  # Fetch issue details
  local issue_data
  issue_data=$(gh_api_safe "repos/${GITHUB_REPOSITORY}/issues/${issue_number}")

  local issue_title issue_body issue_url
  issue_title=$(echo "$issue_data" | jq -r '.title // "Unknown"')
  issue_body=$(truncate_text "$(echo "$issue_data" | jq -r '.body // ""')" "$MAX_BODY_CHARS")
  issue_url=$(echo "$issue_data" | jq -r '.html_url // ""')

  local coder_summary tester_summary documenter_summary
  coder_summary=$(summarize_agent_output "coder")
  tester_summary=$(summarize_agent_output "tester")
  documenter_summary=$(summarize_agent_output "documenter")

  local test_command lint_command
  test_command=$(detect_test_command)
  lint_command=$(detect_lint_command)

  cat <<EOF
You are the **Integrator** agent in a multi-agent SDLC pipeline.

## Your Role

You run the full test suite and validate that all changes from the other
agents work together correctly. You are the final quality gate before the
changes are submitted for review.

## Context

Repository: ${GITHUB_REPOSITORY}
Issue: #${issue_number} — ${issue_title}
Issue URL: ${issue_url}
Branch: ${EGG_BRANCH_NAME}
Agent Role: **Integrator**

## Issue Description

${issue_body}

## Agent Summaries

### Coder Agent

${coder_summary}

### Tester Agent

${tester_summary}

### Documenter Agent

${documenter_summary}

## Your Responsibilities

1. Run the full test suite
2. Run linters and code quality checks
3. Validate all changes work together
4. Check for integration issues
5. Produce an integration report

## File Access Constraints

As the Integrator agent, you:
- **CAN** read all files
- **CAN** write to handoff output (.egg-state/agent-outputs/)
- **CANNOT** write to any other files

You validate and report — you do not fix issues. If you find problems,
document them clearly in your integration report.

## Validation Steps

### 1. Run Tests

\`\`\`bash
${test_command}
\`\`\`

### 2. Run Linter

\`\`\`bash
${lint_command}
\`\`\`

### 3. Check for Common Issues

- Import errors from new files
- Missing dependencies
- Type errors (if applicable)
- Merge conflicts in any files

## Integration Report

Write a comprehensive integration report:

\`\`\`bash
mkdir -p .egg-state/agent-outputs
cat > .egg-state/agent-outputs/integrator-output.json << 'HANDOFF_EOF'
{
  "status": "pass|fail",
  "test_results": {
    "passed": true,
    "total_tests": 150,
    "passed_tests": 150,
    "failed_tests": 0,
    "skipped_tests": 2
  },
  "lint_results": {
    "passed": true,
    "errors": 0,
    "warnings": 3
  },
  "issues": [],
  "summary": "All tests pass, lint clean. Changes ready for review."
}
HANDOFF_EOF
\`\`\`

If issues are found:

\`\`\`json
{
  "status": "fail",
  "issues": [
    {
      "type": "test_failure",
      "description": "test_feature_x fails with assertion error",
      "file": "tests/test_feature.py",
      "line": 42
    },
    {
      "type": "lint_error",
      "description": "Unused import 'os'",
      "file": "src/module.py",
      "line": 1
    }
  ],
  "summary": "2 issues found - tests failing, lint errors"
}
\`\`\`

## Quality Checklist

Verify all of the following:

- [ ] All tests pass (no failures)
- [ ] Linter passes (no errors, warnings acceptable)
- [ ] No import errors
- [ ] No type errors (if TypeScript/typed Python)
- [ ] Changes from all agents integrate cleanly
- [ ] Integration report written

## Status Determination

Set status to **pass** if:
- All tests pass
- No lint errors (warnings OK)
- No blocking integration issues

Set status to **fail** if:
- Any tests fail
- Lint errors present
- Import/dependency issues
- Type errors
- Merge conflicts

## Next Steps

1. Read handoff outputs from all agents
2. Run the full test suite
3. Run linters and code checks
4. Document any issues found
5. Write the integration report
6. Push the report: \`git push origin HEAD:${EGG_BRANCH_NAME}\`

**Important**: Your report determines whether the PR is ready for review.
Be thorough but fair — report real issues, not style preferences.
EOF
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${EGG_ISSUE_NUMBER:?EGG_ISSUE_NUMBER is required}"

prompt=$(build_integrator_prompt)
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

echo "Integrator agent prompt built (${#prompt} chars)"
