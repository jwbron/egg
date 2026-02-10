#!/usr/bin/env bash
# build-documenter-prompt.sh — Build a focused prompt for the Documenter agent
#
# The Documenter agent is responsible for updating documentation for the
# implemented changes. It runs after the Coder agent and can run in parallel
# with the Tester agent.
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

find_related_docs() {
  local repo_path
  repo_path=$(get_repo_path)

  local docs=""

  # Check for main README
  if [[ -f "${repo_path}/README.md" ]]; then
    docs+="- README.md (project root)"$'\n'
  fi

  # Check for docs directory
  if [[ -d "${repo_path}/docs" ]]; then
    docs+="- docs/ directory found"$'\n'
    # List top-level docs
    find "${repo_path}/docs" -maxdepth 1 -name "*.md" -type f 2>/dev/null | while read -r doc; do
      local basename
      basename=$(basename "$doc")
      docs+="  - docs/${basename}"$'\n'
    done
  fi

  # Check for component READMEs in common locations
  for dir in src lib shared gateway sandbox action; do
    if [[ -f "${repo_path}/${dir}/README.md" ]]; then
      docs+="- ${dir}/README.md"$'\n'
    fi
  done

  if [[ -z "$docs" ]]; then
    docs="No documentation files found."
  fi

  echo "$docs"
}

# ---------------------------------------------------------------------------
# Build Documenter Prompt
# ---------------------------------------------------------------------------

build_documenter_prompt() {
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

  local related_docs
  related_docs=$(find_related_docs)

  # Use quoted heredoc ('EOF') to prevent shell interpretation of content
  # Variables are explicitly expanded only where safe using printf
  cat <<'EOF'
You are the **Documenter** agent in a multi-agent SDLC pipeline.

## Your Role

You update documentation for the code changes implemented by the Coder agent.
You run after the Coder and can run in parallel with the Tester agent. You
should not modify any code or test files.

## Context

EOF
  # Safely output dynamic content with printf to prevent injection
  printf 'Repository: %s\n' "${GITHUB_REPOSITORY}"
  printf 'Issue: #%s — %s\n' "${issue_number}" "${issue_title}"
  printf 'Issue URL: %s\n' "${issue_url}"
  printf 'Branch: %s\n' "${EGG_BRANCH_NAME}"
  cat <<'EOF'
Agent Role: **Documenter**

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
  if [[ -n "$changed_files" ]]; then
    printf '%s\n\n' "${changed_files}"
  else
    echo "No changed files reported by Coder agent"
    echo ""
  fi
  cat <<'EOF'
## Existing Documentation

EOF
  printf '%s\n' "${related_docs}"
  cat <<'EOF'

## Your Responsibilities

1. Read the Coder's handoff output to understand what changed
2. Identify which documentation needs updating
3. Update relevant documentation files
4. Ensure documentation is accurate and helpful
5. Do not add unnecessary documentation

## File Access Constraints

As the Documenter agent, you:
- **CAN** write to documentation files (docs/, *.md)
- **CAN** write to README files (README.md, CHANGELOG.md)
- **CAN** write to handoff output (.egg-state/agent-outputs/)
- **CANNOT** write to source code files (*.py, *.ts, *.js, etc.)
- **CANNOT** write to test files (tests/, *_test.py, *.test.ts)
- **CANNOT** write to contract files (.egg-state/contracts/)

## Documentation Guidelines

1. **Only update what's needed**: Don't add documentation for its own sake
2. **Keep it concise**: Clear and brief is better than verbose
3. **Follow existing style**: Match the tone and format of existing docs
4. **Focus on the "why"**: Explain concepts, not just what the code does
5. **Update relevant sections**: Don't rewrite entire documents

### What to Document

- New features or capabilities
- Changed behavior or APIs
- New configuration options
- Breaking changes (with migration guidance)
- Updated examples or usage patterns

### What NOT to Document

- Implementation details that aren't user-facing
- Code that's self-explanatory
- Temporary or internal changes
- Every small bug fix

## Handoff Output

When you complete your work, write a handoff file:

```bash
mkdir -p .egg-state/agent-outputs
cat > .egg-state/agent-outputs/documenter-output.json << 'HANDOFF_EOF'
{
  "doc_files": [
    "docs/guides/feature.md",
    "README.md"
  ],
  "summary": "Updated feature guide and README with new API usage",
  "commits": ["jkl3456"]
}
HANDOFF_EOF
```

If no documentation updates are needed, document that:

```json
{
  "doc_files": [],
  "summary": "No documentation updates needed - internal refactoring only",
  "commits": []
}
```

## Quality Checklist

Before completing:
- [ ] Reviewed all changed files from Coder
- [ ] Updated only necessary documentation
- [ ] Documentation is accurate and clear
- [ ] No code or test files modified
- [ ] Handoff file written

## Next Steps

1. Read the Coder's handoff output: `.egg-state/agent-outputs/coder-output.json`
2. Review the changed files to understand what was implemented
3. Update relevant documentation files
4. Write the handoff output file
EOF
  printf '5. Push your changes: `git push origin HEAD:%s`\n' "${EGG_BRANCH_NAME}"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${EGG_ISSUE_NUMBER:?EGG_ISSUE_NUMBER is required}"

prompt=$(build_documenter_prompt)
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

echo "Documenter agent prompt built (${#prompt} chars)"
