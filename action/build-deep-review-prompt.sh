#!/usr/bin/env bash
# build-deep-review-prompt.sh — Build a deep review prompt for multi-turn AI analysis
#
# Unlike the standard review prompt, deep review gives the bot direct PR access
# for exploratory investigation, test execution, and direct comment posting.
#
# Environment variables:
#   PR_NUMBER          — Pull request number to review
#   GITHUB_REPOSITORY  — owner/repo
#   GH_TOKEN           — GitHub token for API access
#   RUNNER_TEMP        — Temp directory for large prompt file
#   REVIEW_MODE        — (optional) security|plan|outsider (loads specialized prompt)
#   LINKED_ISSUE       — (optional) Issue number for plan verification mode
#
# Output:
#   Sets 'prompt-file', 'model', and 'mode' in $GITHUB_OUTPUT

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAX_DIFF_SUMMARY_CHARS=30000     # Diff summary limit (less detail than standard)
MAX_CONTEXT_CHARS=50000          # Context limit for deep review

# Files to skip (same as build-review-prompt.sh)
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
    local cmd_display
    cmd_display=$(printf "'gh api %s'" "$*")

    if output=$(gh api "$@" 2>"$stderr_file"); then
        rm -f "$stderr_file"
        echo "$output"
    else
        local rc=$?
        local stderr_content
        stderr_content=$(cat "$stderr_file")
        rm -f "$stderr_file"
        echo "WARNING: ${cmd_display} failed (exit $rc): ${stderr_content}" >&2
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

fetch_pr_files_summary() {
    # Returns a summary of changed files (less detail than standard review)
    gh_api_safe "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}/files" \
        --jq '[.[] | {
            filename: .filename,
            status: .status,
            additions: .additions,
            deletions: .deletions,
            changes: .changes
        }]'
}

fetch_issue_content() {
    local issue_number="$1"
    if [[ -z "$issue_number" ]]; then
        echo ""
        return
    fi

    gh_api_safe "repos/${GITHUB_REPOSITORY}/issues/${issue_number}" \
        --jq '{title: .title, body: .body}' 2>/dev/null || echo ""
}

fetch_review_rules() {
    # Try to fetch .egg/review-rules.md from the repo
    local b64_content
    b64_content=$(gh_api_safe "repos/${GITHUB_REPOSITORY}/contents/.egg/review-rules.md?ref=main" \
        --jq '.content // empty' 2>/dev/null)

    local content=""
    if [[ -n "$b64_content" ]]; then
        content=$(echo "$b64_content" | base64 -d 2>/dev/null) || content=""
    fi

    if [[ -z "$content" ]]; then
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

build_deep_review_prompt() {
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

    # Fetch changed files summary
    local files_json
    files_json=$(fetch_pr_files_summary)

    local file_count
    file_count=$(echo "$files_json" | jq 'length')

    # Build changed files summary
    local files_summary=""
    local security_sensitive_files=()

    while IFS= read -r file_entry; do
        local filename status additions deletions
        filename=$(echo "$file_entry" | jq -r '.filename')
        status=$(echo "$file_entry" | jq -r '.status')
        additions=$(echo "$file_entry" | jq -r '.additions')
        deletions=$(echo "$file_entry" | jq -r '.deletions')

        if should_skip_file "$filename"; then
            continue
        fi

        files_summary="${files_summary}- ${filename} (${status}, +${additions}/-${deletions})\n"

        # Track security-sensitive files
        if [[ "$filename" =~ (auth|middleware|security|password|token|secret|cred|api|endpoint|route|handler|docker|workflow|yml|yaml) ]]; then
            security_sensitive_files+=("$filename")
        fi
    done < <(echo "$files_json" | jq -c '.[]')

    # Fetch review rules (not used for outsider mode)
    local review_rules=""
    if [[ "${REVIEW_MODE:-}" != "outsider" ]]; then
        review_rules=$(fetch_review_rules)
    fi

    # Fetch linked issue content for plan verification mode
    local linked_content=""
    if [[ "${REVIEW_MODE:-}" == "plan" ]] && [[ -n "${LINKED_ISSUE:-}" ]]; then
        local issue_data
        issue_data=$(fetch_issue_content "$LINKED_ISSUE")
        if [[ -n "$issue_data" ]]; then
            local issue_title issue_body
            issue_title=$(echo "$issue_data" | jq -r '.title // ""')
            issue_body=$(echo "$issue_data" | jq -r '.body // ""')
            linked_content="### Issue #${LINKED_ISSUE}: ${issue_title}

${issue_body}"
        fi
    fi

    # Determine which prompt template to use
    local mode_prompt=""
    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

    case "${REVIEW_MODE:-}" in
        security)
            if [[ -f "${script_dir}/prompts/security-review.md" ]]; then
                mode_prompt=$(cat "${script_dir}/prompts/security-review.md")
            fi
            ;;
        plan)
            if [[ -f "${script_dir}/prompts/plan-verify.md" ]]; then
                mode_prompt=$(cat "${script_dir}/prompts/plan-verify.md")
            fi
            ;;
        outsider)
            if [[ -f "${script_dir}/prompts/outsider-review.md" ]]; then
                mode_prompt=$(cat "${script_dir}/prompts/outsider-review.md")
            fi
            ;;
    esac

    # Assemble the deep review prompt
    local prompt
    prompt="You are performing a **deep review** of PR #${PR_NUMBER}: \"${title}\" in ${GITHUB_REPOSITORY}.

Author: ${user}
Branch: ${head} -> ${base}
URL: ${html_url}

## PR Description

${body:-No description provided.}
"

    # Add linked content for plan verification
    if [[ -n "$linked_content" ]]; then
        prompt="${prompt}
## Linked Issue/Plan

${linked_content}
"
    fi

    # Add review rules (except for outsider mode)
    if [[ -n "$review_rules" ]]; then
        prompt="${prompt}
## Review Rules

${review_rules}
"
    fi

    # Add changed files summary
    prompt="${prompt}
## Changed Files (${file_count} files)

$(echo -e "$files_summary")
"

    # Add security-sensitive files note if applicable
    if [[ ${#security_sensitive_files[@]} -gt 0 ]]; then
        prompt="${prompt}
### Security-Sensitive Files in This PR

$(printf '%s\n' "${security_sensitive_files[@]}" | sed 's/^/- /')

Pay extra attention to these files.
"
    fi

    # Add mode-specific instructions or default deep review instructions
    if [[ -n "$mode_prompt" ]]; then
        # Substitute placeholders in mode prompt
        mode_prompt="${mode_prompt//\{pr_number\}/$PR_NUMBER}"
        mode_prompt="${mode_prompt//\{title\}/$title}"
        mode_prompt="${mode_prompt//\{owner\}/${GITHUB_REPOSITORY%%/*}}"
        mode_prompt="${mode_prompt//\{repo\}/${GITHUB_REPOSITORY##*/}}"
        mode_prompt="${mode_prompt//\{pr_description\}/$body}"
        mode_prompt="${mode_prompt//\{linked_content\}/$linked_content}"
        mode_prompt="${mode_prompt//\{changed_files\}/$(echo -e "$files_summary")}"
        mode_prompt="${mode_prompt//\{file_contents\}/[Use the Read tool to view file contents as needed]}"

        prompt="${prompt}
---

${mode_prompt}
"
    else
        # Default deep review instructions
        prompt="${prompt}
## Deep Review Instructions

You have **full access** to the repository and can perform multi-turn analysis.

### Available Capabilities

1. **Read any file** - Use the Read tool to examine files beyond the diff
2. **Run tests** - Use Bash to run \`pytest\`, \`jest\`, \`make test\`, etc.
3. **Post comments directly** - Use \`gh pr review\` to post inline comments
4. **Explore the codebase** - Follow chains of investigation

### Review Process

1. Start by reading the changed files in full
2. Investigate each concern by:
   - Reading related files for context
   - Running tests to validate suspected issues
   - Checking for similar patterns elsewhere
3. Post inline comments as you find issues
4. For concrete fixes, use GitHub suggestion blocks

### Comment Format

For inline comments, use \`gh pr review ${PR_NUMBER} --comment\` with this body format:

\`\`\`
**[severity]** (category): Description

[If you have a fix, include a suggestion block:]
\`\`\`suggestion
corrected code here
\`\`\`
\`\`\`

### Guardrails

- Maximum 10 inline comments per review
- 30-minute time limit
- Do NOT modify code or push commits
- Do NOT approve or request changes, only comment
- Focus on the most important issues

### Focus Areas

1. **Security vulnerabilities** - Auth bypasses, injection, data exposure
2. **Correctness bugs** - Logic errors, edge cases, race conditions
3. **Significant quality issues** - Not style, but structural problems

Do NOT comment on:
- Style issues (linters handle this)
- Minor improvements
- Things that static analyzers catch

### Getting Started

Begin by reading the changed files to understand what this PR does:
$(echo -e "$files_summary" | head -10 | sed 's/^/1. Read /')

Then investigate any concerns by exploring related code and running tests.
"
    fi

    # Truncate overall prompt if needed
    prompt=$(truncate_text "$prompt" "$MAX_CONTEXT_CHARS")

    # Write prompt to temp file
    local prompt_file="${RUNNER_TEMP:-/tmp}/deep-review-prompt-${PR_NUMBER}.txt"
    echo "$prompt" > "$prompt_file"

    # Deep review always uses opus and has longer timeout
    local model="opus"

    # Write outputs
    {
        echo "prompt-file=${prompt_file}"
        echo "model=${model}"
        echo "mode=deep-review"
        echo "timeout=30"
    } >> "${GITHUB_OUTPUT:-/dev/null}"

    echo "Deep review prompt built: ${#prompt} chars, ${file_count} files, mode=${REVIEW_MODE:-default}"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

: "${PR_NUMBER:?PR_NUMBER is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"

build_deep_review_prompt
