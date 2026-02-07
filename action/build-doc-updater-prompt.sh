#!/usr/bin/env bash
# build-doc-updater-prompt.sh — Build a prompt for doc-updater bot
#
# This script creates a prompt that tells Claude to analyze recent code changes
# and determine if documentation updates are needed. If so, it should create
# a PR with the updates.
#
# Environment variables:
#   COMMIT_SHA         — (Optional) Analyze changes from this commit
#   DRY_RUN            — (Optional) If "true", analyze only, don't create PR
#   GITHUB_REPOSITORY  — owner/repo
#   RUNNER_TEMP        — Temp directory for prompt file
#
# Output:
#   Sets 'prompt_file' and 'model' in $GITHUB_OUTPUT

set -euo pipefail

# ---------------------------------------------------------------------------
# Get recent changes
# ---------------------------------------------------------------------------

get_changed_files() {
    local base_commit="${COMMIT_SHA:-HEAD~1}"

    # Get list of changed files (excluding docs and markdown)
    git diff --name-only "${base_commit}..HEAD" 2>/dev/null | \
        grep -v -E '^docs/' | \
        grep -v -E '\.md$' || true
}

get_commit_messages() {
    local base_commit="${COMMIT_SHA:-HEAD~1}"

    # Get commit messages since base (usually just the merged commit)
    git log --oneline "${base_commit}..HEAD" 2>/dev/null || echo "Unable to get commit messages"
}

# ---------------------------------------------------------------------------
# Build the prompt
# ---------------------------------------------------------------------------

build_prompt() {
    local changed_files
    local commit_messages
    local base_commit="${COMMIT_SHA:-HEAD~1}"

    changed_files=$(get_changed_files)
    commit_messages=$(get_commit_messages)

    # If no code files changed, skip
    if [[ -z "$changed_files" ]]; then
        echo "No code files changed (only docs/markdown), skipping doc-updater"
        # Create a minimal prompt that exits immediately
        local prompt_file="${RUNNER_TEMP:-/tmp}/doc-updater-prompt.txt"
        echo "No code files changed since ${base_commit}. Nothing to do." > "$prompt_file"
        {
            echo "prompt_file=${prompt_file}"
            echo "model=haiku"
        } >> "${GITHUB_OUTPUT:-/dev/null}"
        return
    fi

    local prompt
    prompt=$(cat <<PROMPT_EOF
# Doc Updater Task

Analyze recent code changes and determine if documentation needs to be updated.
If updates are needed, create a PR with the changes.

## Context

Recent commits (since ${base_commit}):
\`\`\`
${commit_messages}
\`\`\`

Changed files:
\`\`\`
${changed_files}
\`\`\`

## Your Task

1. **Analyze the changes**: Read the changed files and understand what was modified.
   Use \`git diff ${base_commit}..HEAD -- <file>\` to see specific changes.

2. **Check documentation impact**: For each significant change, determine if any
   documentation needs to be updated. Consider:

   - **Component READMEs**: If code in \`gateway/\`, \`sandbox/\`, \`shared/\`, \`action/\`,
     \`bin/\`, or \`config/\` changed, check if the corresponding README needs updates.

   - **Architecture docs**: If system design or security model changed, check
     \`docs/architecture/\` and relevant ADRs.

   - **Development docs**: If project structure, build process, or workflows changed,
     check \`docs/development/\`.

   - **Task-specific guides**: If a new workflow or capability was added, consider
     if \`docs/index.md\` task-specific guides need updating.

   - **API changes**: If gateway endpoints or CLI arguments changed, check the
     relevant component documentation.

3. **If updates are needed**:
   - Create a new branch: \`egg/doc-update-<short-description>\`
   - Make the documentation changes
   - Create a PR with:
     - Title: \`docs: <brief description>\` (under 50 chars)
     - Body: Explain what code changes prompted the doc updates
     - Add \`[doc-updater]\` tag at the end of the title to prevent loops

4. **If no updates are needed**:
   - Report that documentation is up to date
   - No PR needed

## Guidelines

- Only update docs that are actually outdated. Don't make unnecessary changes.
- Keep doc updates minimal and focused. Don't rewrite entire sections.
- Preserve existing doc style and formatting.
- Don't add new documentation for minor internal changes.
- Focus on user-facing docs and architectural changes.
- If a change is purely internal (refactoring, tests), docs likely don't need updates.

## PR Format (if creating one)

\`\`\`
docs: Update <component> docs for <change> [doc-updater]

Update documentation to reflect changes from <commit(s)>:
- <what was updated and why>

Triggered by: <link to merged PR or commit>

Authored-by: egg
\`\`\`
PROMPT_EOF
)

    # Add dry run instruction if applicable
    if [[ "${DRY_RUN:-false}" == "true" ]]; then
        prompt+="

## Dry Run Mode

This is a dry run. Analyze the changes and describe what documentation updates
you WOULD make, but do NOT create any branches or PRs. Just report your findings."
    fi

    # Write prompt to temp file
    local prompt_file="${RUNNER_TEMP:-/tmp}/doc-updater-prompt.txt"
    echo "$prompt" > "$prompt_file"

    # Use sonnet for doc analysis (good balance of capability and speed)
    local model="sonnet"

    # Write outputs
    {
        echo "prompt_file=${prompt_file}"
        echo "model=${model}"
    } >> "${GITHUB_OUTPUT:-/dev/null}"

    echo "Doc-updater prompt built: ${#prompt} chars, model=${model}"
    echo "Changed files: $(echo "$changed_files" | wc -l) files"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"

build_prompt
