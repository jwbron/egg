#!/usr/bin/env bash
# build-review-prompt.sh — Build a review prompt for AI-powered code review
#
# Fetches PR details, diffs, and file contents, then assembles a structured
# prompt for Claude to review the code changes.
#
# Environment variables:
#   PR_NUMBER          — Pull request number to review
#   GITHUB_REPOSITORY  — owner/repo
#   GH_TOKEN           — GitHub token for API access
#   RUNNER_TEMP        — Temp directory for large prompt file
#
# Output:
#   Sets 'prompt-file' and 'model' in $GITHUB_OUTPUT

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAX_DIFF_CHARS=15000        # Per-file diff limit
MAX_FILE_CHARS=30000        # Per-file content limit
MAX_PROMPT_CHARS=100000     # Overall prompt limit
MODEL_THRESHOLD_FILES=5     # Use opus for PRs with more than this many files

# Files to skip (generated, binary, lock files)
SKIP_PATTERNS=(
    '\.lock$'
    '\.min\.js$'
    '\.min\.css$'
    'package-lock\.json$'
    'yarn\.lock$'
    'pnpm-lock\.yaml$'
    'Pipfile\.lock$'
    'poetry\.lock$'
    'Gemfile\.lock$'
    'composer\.lock$'
    'go\.sum$'
    'Cargo\.lock$'
    '\.pyc$'
    '\.pyo$'
    '__pycache__'
    '\.class$'
    '\.jar$'
    '\.war$'
    '\.so$'
    '\.dylib$'
    '\.dll$'
    '\.exe$'
    '\.bin$'
    '\.png$'
    '\.jpg$'
    '\.jpeg$'
    '\.gif$'
    '\.ico$'
    '\.svg$'
    '\.woff'
    '\.ttf$'
    '\.eot$'
    '\.pdf$'
    '\.zip$'
    '\.tar'
    '\.gz$'
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

truncate_text() {
    local text="$1"
    local max_chars="$2"
    if [[ ${#text} -gt $max_chars ]]; then
        echo "${text:0:$max_chars}

... (truncated, ${#text} total chars)"
    else
        echo "$text"
    fi
}

should_skip_file() {
    local filename="$1"
    for pattern in "${SKIP_PATTERNS[@]}"; do
        if [[ "$filename" =~ $pattern ]]; then
            return 0
        fi
    done
    return 1
}

# Safe gh api wrapper
gh_api_safe() {
    local stderr_file
    stderr_file=$(mktemp)
    local output
    if output=$(gh api "$@" 2>"$stderr_file"); then
        rm -f "$stderr_file"
        echo "$output"
    else
        local rc=$?
        echo "WARNING: 'gh api $*' failed (exit $rc): $(cat "$stderr_file")" >&2
        rm -f "$stderr_file"
        return 0
    fi
}

# ---------------------------------------------------------------------------
# Fetch PR data
# ---------------------------------------------------------------------------

fetch_pr_details() {
    gh_api_safe "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}" \
        --jq '{
            title: .title,
            body: .body,
            state: .state,
            base: .base.ref,
            head: .head.ref,
            head_sha: .head.sha,
            user: .user.login,
            html_url: .html_url
        }'
}

fetch_pr_files() {
    # Returns JSON array of changed files with patches
    gh_api_safe "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}/files" \
        --jq '[.[] | {
            filename: .filename,
            status: .status,
            additions: .additions,
            deletions: .deletions,
            patch: .patch
        }]'
}

fetch_file_content() {
    local filename="$1"
    local ref="$2"
    # Fetch raw file content from the head commit
    gh_api_safe "repos/${GITHUB_REPOSITORY}/contents/${filename}?ref=${ref}" \
        --jq '.content // empty' 2>/dev/null | base64 -d 2>/dev/null || echo ""
}

fetch_review_rules() {
    # Try to fetch .egg/review-rules.md from the repo
    local content
    content=$(gh_api_safe "repos/${GITHUB_REPOSITORY}/contents/.egg/review-rules.md?ref=main" \
        --jq '.content // empty' 2>/dev/null | base64 -d 2>/dev/null || echo "")

    if [[ -z "$content" ]]; then
        # Default review rules
        cat <<'EOF'
## Default Review Rules

Focus on:
- Security issues (vulnerabilities, unsafe patterns, credential leaks)
- Correctness (logic errors, edge cases, error handling gaps)
- Code quality (readability, maintainability, naming)

Skip:
- Style issues handled by linters (formatting, import order)
- Type annotation completeness (type checkers handle this)
- Auto-generated files (migrations, lock files)
EOF
    else
        echo "$content"
    fi
}

# ---------------------------------------------------------------------------
# Build the prompt
# ---------------------------------------------------------------------------

build_prompt() {
    local pr_details
    pr_details=$(fetch_pr_details)

    local title body base head head_sha user html_url
    title=$(echo "$pr_details" | jq -r '.title // "Untitled"')
    body=$(echo "$pr_details" | jq -r '.body // ""')
    base=$(echo "$pr_details" | jq -r '.base // "main"')
    head=$(echo "$pr_details" | jq -r '.head // "unknown"')
    head_sha=$(echo "$pr_details" | jq -r '.head_sha // ""')
    user=$(echo "$pr_details" | jq -r '.user // "unknown"')
    html_url=$(echo "$pr_details" | jq -r '.html_url // ""')

    # Fetch changed files
    local files_json
    files_json=$(fetch_pr_files)

    local file_count
    file_count=$(echo "$files_json" | jq 'length')

    # Determine model based on file count
    local model="haiku"
    if [[ "$file_count" -gt "$MODEL_THRESHOLD_FILES" ]]; then
        model="opus"
    fi

    # Fetch review rules
    local review_rules
    review_rules=$(fetch_review_rules)

    # Build changed files section
    local changed_files_section=""
    local file_contents_section=""
    local skipped_files=""

    while IFS= read -r file_entry; do
        local filename status additions deletions patch
        filename=$(echo "$file_entry" | jq -r '.filename')
        status=$(echo "$file_entry" | jq -r '.status')
        additions=$(echo "$file_entry" | jq -r '.additions')
        deletions=$(echo "$file_entry" | jq -r '.deletions')
        patch=$(echo "$file_entry" | jq -r '.patch // ""')

        # Skip binary/generated files
        if should_skip_file "$filename"; then
            skipped_files="${skipped_files}${filename} (skipped: generated/binary)\n"
            continue
        fi

        # Add to changed files section
        changed_files_section="${changed_files_section}
### ${filename}
Status: ${status} (+${additions}/-${deletions})

\`\`\`diff
$(truncate_text "$patch" "$MAX_DIFF_CHARS")
\`\`\`
"

        # Fetch full file content for modified/added files
        if [[ "$status" != "removed" ]]; then
            local content
            content=$(fetch_file_content "$filename" "$head_sha")
            if [[ -n "$content" ]]; then
                file_contents_section="${file_contents_section}
### ${filename}
\`\`\`
$(truncate_text "$content" "$MAX_FILE_CHARS")
\`\`\`
"
            fi
        fi
    done < <(echo "$files_json" | jq -c '.[]')

    # Assemble the full prompt
    local prompt
    prompt="You are reviewing PR #${PR_NUMBER}: \"${title}\" in ${GITHUB_REPOSITORY}.

Author: ${user}
Branch: ${head} -> ${base}
URL: ${html_url}

## PR Description

${body:-No description provided.}

## Review Rules

${review_rules}

## Changed Files (${file_count} files)
${changed_files_section}
"

    # Add file contents section if we have any
    if [[ -n "$file_contents_section" ]]; then
        prompt="${prompt}
## Full File Context
${file_contents_section}
"
    fi

    # Add skipped files note
    if [[ -n "$skipped_files" ]]; then
        prompt="${prompt}
## Skipped Files
$(echo -e "$skipped_files")
"
    fi

    # Add review instructions
    prompt="${prompt}
## Instructions

Review this PR for:
1. **Security issues** — vulnerabilities, unsafe patterns, credential leaks, injection risks
2. **Correctness** — logic errors, edge cases, error handling gaps, race conditions
3. **Code quality** — readability, maintainability, naming, unnecessary complexity
4. **Standards compliance** — project conventions per review rules above

For each issue found, output a structured JSON block:
\`\`\`json
{
  \"file\": \"path/to/file\",
  \"line\": <line_number_in_file>,
  \"severity\": \"critical|warning|suggestion\",
  \"category\": \"security|correctness|quality|standards\",
  \"comment\": \"Description of the issue and suggested fix\"
}
\`\`\`

The \"line\" field must be the actual line number in the file (as shown in the GitHub file viewer on the HEAD commit), NOT a diff-relative position.

At the end, provide a summary in this format:
\`\`\`json
{
  \"summary\": \"Overall assessment of the PR\",
  \"verdict\": \"approve|request_changes|comment\",
  \"comments\": [<all the individual comment objects above>]
}
\`\`\`

Rules for comments:
- Only comment on things that are actually wrong or risky
- Do not comment on style preferences already handled by linters (ruff, eslint, prettier)
- Do not repeat what ruff, mypy, shellcheck, or bandit would catch
- Focus on issues that require human judgment to detect
- Be specific: reference the exact line and explain why it's a problem
- Suggest a fix when possible
- Bias toward fewer, higher-signal comments — a noisy reviewer gets ignored

If the PR looks good with no significant issues, output:
\`\`\`json
{\"summary\": \"No significant issues found. The changes look good.\", \"verdict\": \"approve\", \"comments\": []}
\`\`\`
"

    # Truncate overall prompt if needed
    prompt=$(truncate_text "$prompt" "$MAX_PROMPT_CHARS")

    # Write prompt to temp file (avoids GITHUB_OUTPUT size limits)
    local prompt_file="${RUNNER_TEMP:-/tmp}/review-prompt-${PR_NUMBER}.txt"
    echo "$prompt" > "$prompt_file"

    # Write outputs
    {
        echo "prompt-file=${prompt_file}"
        echo "model=${model}"
    } >> "${GITHUB_OUTPUT:-/dev/null}"

    echo "Review prompt built: ${#prompt} chars, ${file_count} files, model=${model}"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

: "${PR_NUMBER:?PR_NUMBER is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"

build_prompt
