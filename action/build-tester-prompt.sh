#!/usr/bin/env bash
# build-tester-prompt.sh — Build a focused prompt for the Tester agent
#
# The Tester agent is responsible for writing tests for the implemented changes.
# It runs after the Coder agent and reads the list of changed files from the
# Coder's handoff output.
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

get_coder_output() {
  local repo_path
  repo_path=$(get_repo_path)

  local handoff_file="${repo_path}/.egg-state/agent-outputs/coder-output.json"
  if [[ -f "$handoff_file" ]]; then
    cat "$handoff_file"
  else
    echo '{"changed_files": [], "commits": [], "summary": "No coder output found"}'
  fi
}

get_changed_files_list() {
  local coder_output="$1"
  echo "$coder_output" | jq -r '.changed_files[]? // empty' 2>/dev/null | while read -r file; do
    echo "- $file"
  done
}

get_test_patterns() {
  local repo_path
  repo_path=$(get_repo_path)

  # Detect existing test patterns in the repository using find instead of glob
  # (glob with ** requires shopt -s globstar which may not be set)
  local patterns=""

  if [[ -d "${repo_path}/tests" ]]; then
    patterns+="- tests/ directory found"$'\n'
  fi
  if [[ -d "${repo_path}/test" ]]; then
    patterns+="- test/ directory found"$'\n'
  fi
  if find "${repo_path}" -name '*_test.py' -type f 2>/dev/null | head -1 | grep -q .; then
    patterns+="- Python: *_test.py pattern"$'\n'
  fi
  if find "${repo_path}" -name 'test_*.py' -type f 2>/dev/null | head -1 | grep -q .; then
    patterns+="- Python: test_*.py pattern"$'\n'
  fi
  if find "${repo_path}" -name '*.test.ts' -type f 2>/dev/null | head -1 | grep -q .; then
    patterns+="- TypeScript: *.test.ts pattern"$'\n'
  fi
  if find "${repo_path}" -name '*.spec.ts' -type f 2>/dev/null | head -1 | grep -q .; then
    patterns+="- TypeScript: *.spec.ts pattern"$'\n'
  fi

  if [[ -z "$patterns" ]]; then
    patterns="No existing test patterns detected. Use standard conventions."
  fi

  echo "$patterns"
}

# ---------------------------------------------------------------------------
# Build Tester Prompt
# ---------------------------------------------------------------------------

build_tester_prompt() {
  local issue_number="${EGG_ISSUE_NUMBER:?EGG_ISSUE_NUMBER is required}"

  # Fetch issue details
  local issue_data
  issue_data=$(gh_api_safe "repos/${GITHUB_REPOSITORY}/issues/${issue_number}")

  local issue_title issue_body issue_url
  issue_title=$(echo "$issue_data" | jq -r '.title // "Unknown"')
  issue_body=$(truncate_text "$(echo "$issue_data" | jq -r '.body // ""')" "$MAX_BODY_CHARS")
  issue_url=$(echo "$issue_data" | jq -r '.html_url // ""')

  local coder_output
  coder_output=$(get_coder_output)

  local changed_files
  changed_files=$(get_changed_files_list "$coder_output")

  local coder_summary
  coder_summary=$(echo "$coder_output" | jq -r '.summary // "No summary provided"')

  local test_patterns
  test_patterns=$(get_test_patterns)

  # Use quoted heredoc ('EOF') to prevent shell interpretation of content
  # Variables are explicitly expanded only where safe using printf
  cat <<'EOF'
You are the **Tester** agent in a multi-agent SDLC pipeline.

## Your Role

You write tests for the code changes implemented by the Coder agent. You run
after the Coder and should not modify any non-test code.

## Context

EOF
  # Safely output dynamic content with printf to prevent injection
  printf 'Repository: %s\n' "${GITHUB_REPOSITORY}"
  printf 'Issue: #%s — %s\n' "${issue_number}" "${issue_title}"
  printf 'Issue URL: %s\n' "${issue_url}"
  printf 'Branch: %s\n' "${EGG_BRANCH_NAME}"
  cat <<'EOF'
Agent Role: **Tester**

## Issue Description

EOF
  printf '%s\n\n' "${issue_body}"
  cat <<'EOF'
## Coder Agent Summary

EOF
  printf '%s\n\n' "${coder_summary}"
  cat <<'EOF'
## Changed Files (from Coder)

EOF
  printf '%s\n\n' "${changed_files:-"No changed files reported by Coder agent"}"
  cat <<'EOF'
## Existing Test Patterns

EOF
  printf '%s\n' "${test_patterns}"
  cat <<'EOF'

## Your Responsibilities

1. Read the Coder's handoff output to understand what changed
2. Write tests for the new or modified code
3. Ensure adequate test coverage for the changes
4. Run tests to verify they pass
5. Report any coverage gaps or testing concerns

## File Access Constraints

As the Tester agent, you:
- **CAN** write to test directories (tests/, test/, **/tests/)
- **CAN** write to test files (*_test.py, test_*.py, *.test.ts, *.spec.ts)
- **CAN** write to handoff output (.egg-state/agent-outputs/)
- **CANNOT** write to source code files (only test code)
- **CANNOT** write to documentation files (docs/, README.md)
- **CANNOT** write to contract files (.egg-state/contracts/)

If you find bugs or issues, document them in your handoff output rather than
fixing them directly.

## Testing Guidelines

1. **Follow existing patterns**: Match the test style and framework used in the repo
2. **Test the changes**: Focus on the files modified by the Coder
3. **Edge cases**: Include tests for error conditions and edge cases
4. **Descriptive names**: Use clear test names that explain what's being tested
5. **No implementation changes**: If code needs fixing, report it in handoff

## Handoff Output

When you complete your work, write a handoff file:

\`\`\`bash
mkdir -p .egg-state/agent-outputs
cat > .egg-state/agent-outputs/tester-output.json << 'HANDOFF_EOF'
{
  "test_files": [
    "tests/test_feature.py",
    "tests/unit/test_module.py"
  ],
  "coverage_summary": "Added 15 tests covering the new feature",
  "issues_found": [],
  "commits": ["ghi9012"]
}
HANDOFF_EOF
\`\`\`

If you find issues that need fixing, document them:

\`\`\`json
{
  "issues_found": [
    {
      "file": "src/module.py",
      "line": 42,
      "description": "Potential null pointer when input is empty"
    }
  ]
}
\`\`\`

## Quality Checklist

Before completing:
- [ ] Tests written for all changed files
- [ ] Tests follow existing patterns and conventions
- [ ] All tests pass
- [ ] Edge cases and error conditions covered
- [ ] Handoff file written with test files list
- [ ] Any issues found documented in handoff

## Next Steps

1. Read the Coder's handoff output: \`.egg-state/agent-outputs/coder-output.json\`
2. Write tests for each changed file
3. Run the test suite to verify all tests pass
4. Write the handoff output file
EOF
  printf '5. Push your changes: `git push origin HEAD:%s`\n' "${EGG_BRANCH_NAME}"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${EGG_ISSUE_NUMBER:?EGG_ISSUE_NUMBER is required}"

prompt=$(build_tester_prompt)
prompt=$(truncate_text "$prompt" "$MAX_PROMPT_CHARS")

# Generate unique delimiter
random_suffix=$(head -c 16 /dev/urandom | xxd -p | head -c 16)
delimiter="__EGG_PROMPT_BOUNDARY_${random_suffix}__"

# Use opus for testing - needs reasoning about test coverage and edge cases
model="opus"

# Write multiline output
{
  echo "prompt<<${delimiter}"
  echo "$prompt"
  echo "${delimiter}"
  echo "model=${model}"
} >> "${GITHUB_OUTPUT:-/dev/null}"

echo "Tester agent prompt built (${#prompt} chars, model=${model})"
