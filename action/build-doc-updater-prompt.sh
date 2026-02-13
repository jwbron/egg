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

get_diff_stats() {
    local base_commit="${COMMIT_SHA:-HEAD~1}"

    # Get diffstat summary to help the agent gauge change magnitude
    git diff --stat "${base_commit}..HEAD" 2>/dev/null | tail -1 || true
}

get_new_files() {
    local base_commit="${COMMIT_SHA:-HEAD~1}"

    # List files that were added (not modified), excluding docs/markdown
    git diff --name-only --diff-filter=A "${base_commit}..HEAD" 2>/dev/null | \
        grep -v -E '^docs/' | \
        grep -v -E '\.md$' || true
}

find_related_docs() {
    local base_commit="${COMMIT_SHA:-HEAD~1}"

    # Extract meaningful terms from changed CODE file paths (not docs) to
    # find documentation that discusses the same components/concepts.
    # We focus on domain-specific terms (e.g. "hitl", "sdlc", "gateway")
    # and filter out generic project structure words.
    local code_files
    code_files=$(git diff --name-only "${base_commit}..HEAD" 2>/dev/null | \
        grep -v -E '^docs/' | \
        grep -v -E '\.md$' || true)

    if [[ -z "$code_files" ]]; then
        return
    fi

    local path_terms
    path_terms=$(echo "$code_files" | \
        sed 's|/| |g; s|\.| |g; s|_| |g; s|-| |g' | \
        tr ' ' '\n' | \
        tr '[:upper:]' '[:lower:]' | \
        grep -E '^[a-z0-9]+$' | \
        grep -v -E '^(src|lib|pkg|cmd|internal|test|tests|unit|spec|py|ts|tsx|js|jsx|json|yml|yaml|md|txt|cfg|toml|ini|lock|go|rs|java|sh|bash|css|scss|html|init|main|index|utils|helpers|common|config|setup|__pycache__|node_modules|dist|build|vendor|egg|action|sandbox|github|workflows|on|push|prompt|integration|service|server|client|handler|manager|factory|model|view|controller|schema|migration|fixture|mock|stub)$' | \
        grep -E '.{4,}' | \
        sort -u || true)

    # Extract key terms from commit subject lines only (not full bodies,
    # which contain too much noise). Focus on feature/component nouns.
    local commit_terms
    commit_terms=$(git log --format='%s' "${base_commit}..HEAD" 2>/dev/null | \
        sed 's/\[.*\]//g; s/([^)]*)//g; s/#[0-9]*//g' | \
        sed 's/[^a-zA-Z]/ /g' | \
        tr ' ' '\n' | \
        tr '[:upper:]' '[:lower:]' | \
        grep -E '^[a-z]+$' | \
        grep -v -E '^(the|and|for|with|from|that|this|not|but|can|all|its|into|also|new|add|fix|update|change|move|remove|use|make|set|get|run|docs|code|commit|merge|push|pull|review|test|bug|feat|chore|refactor|style|perf|revert|egg|none|before|after|when|only|some|more|other|wait|failing|failed|workflow|pipeline)$' | \
        grep -E '.{4,}' | \
        sort -u || true)

    # Combine and deduplicate terms, take top candidates
    local all_terms
    all_terms=$(printf '%s\n%s\n' "$path_terms" "$commit_terms" | \
        grep -v '^$' | sort -u | head -20)

    if [[ -z "$all_terms" ]]; then
        return
    fi

    # Build grep pattern from terms
    local pattern
    pattern=$(echo "$all_terms" | tr '\n' '|' | sed 's/|$//')

    # Get list of docs changed in this commit (already being processed)
    local changed_docs
    changed_docs=$(git diff --name-only "${base_commit}..HEAD" 2>/dev/null | \
        grep -E '^docs/' || true)

    # Search all doc files for references to these terms, excluding:
    # - structural docs (already checked separately in step 3)
    # - docs changed in this same commit (already being processed)
    local results
    results=$(grep -rl -i -E "$pattern" docs/ 2>/dev/null | \
        grep -v -E '(docs/index\.md|docs/development/STRUCTURE\.md|docs/architecture/README\.md)$' | \
        sort -u || true)

    # Also search root-level markdown files (README.md is excluded here
    # because it gets explicit handling as a structural doc in step 3)
    local root_md_results
    root_md_results=$(grep -rl -i -E "$pattern" ./*.md 2>/dev/null | \
        sed 's|^\./||' | \
        grep -v -E '^README\.md$' | \
        sort -u || true)

    # Combine results from docs/ and root-level
    results=$(printf '%s\n%s' "$results" "$root_md_results" | grep -v '^$' | sort -u)

    # Filter out docs that were changed in the same commit
    if [[ -n "$changed_docs" ]]; then
        local exclude_pattern
        exclude_pattern=$(echo "$changed_docs" | tr '\n' '|' | sed 's/|$//')
        echo "$results" | grep -v -E "^($exclude_pattern)$" || true
    else
        echo "$results"
    fi
}

# ---------------------------------------------------------------------------
# High-risk file detection heuristics
# ---------------------------------------------------------------------------

detect_high_risk_docs() {
    local changed_files="$1"
    local flags=""

    if echo "$changed_files" | grep -qE 'sandbox/egg_lib/cli\.py'; then
        flags+="README_CLI "
    fi

    if echo "$changed_files" | grep -qE '(gateway/phase_filter\.py|gateway/policy\.py|\.egg/phase-permissions\.json)'; then
        flags+="README_ENFORCEMENT "
    fi

    if echo "$changed_files" | grep -qE '(docker-compose|bin/egg-deploy|sandbox/egg_lib/(compose|deploy))'; then
        flags+="DEPLOYMENT_GUIDE "
    fi

    if echo "$changed_files" | grep -qE '(action/action\.yml|action/entrypoint\.sh)'; then
        flags+="README_ACTION ACTION_README "
    fi

    if echo "$changed_files" | grep -qE '\.github/workflows/'; then
        flags+="GITHUB_AUTOMATION "
    fi

    if echo "$changed_files" | grep -qE 'orchestrator/'; then
        flags+="README_ORCHESTRATION "
    fi

    echo "$flags"
}

build_high_risk_instructions() {
    local flags="$1"
    local instructions=""

    if [[ "$flags" == *"README_CLI"* ]]; then
        instructions+="- **CLI Reference**: Compare argparse definitions in \`sandbox/egg_lib/cli.py\` against CLI Reference and Flags tables in \`README.md\`. Check for missing flags, changed descriptions, or reordered arguments.\n"
    fi

    if [[ "$flags" == *"README_ENFORCEMENT"* ]]; then
        instructions+="- **Enforcement tables**: Compare \`gateway/phase_filter.py\` and \`.egg/phase-permissions.json\` against the \"What's Enforced\" and \"Phase Permissions\" tables in \`README.md\`.\n"
    fi

    if [[ "$flags" == *"DEPLOYMENT_GUIDE"* ]]; then
        instructions+="- **Deployment guide**: Check \`docs/guides/deployment.md\` for consistency with README Quick Start and CLI Reference. Ensure deployment commands and options match.\n"
    fi

    if [[ "$flags" == *"README_ACTION"* ]]; then
        instructions+="- **GitHub Action inputs**: Compare \`action/action.yml\` inputs against the GitHub Action section in \`README.md\`.\n"
    fi

    if [[ "$flags" == *"ACTION_README"* ]]; then
        instructions+="- **Action README**: Check \`action/README.md\` for accuracy against \`action/action.yml\` and \`action/entrypoint.sh\`.\n"
    fi

    if [[ "$flags" == *"GITHUB_AUTOMATION"* ]]; then
        instructions+="- **Workflow table**: Check \`docs/guides/github-automation.md\` for accuracy against actual workflow files in \`.github/workflows/\`.\n"
    fi

    if [[ "$flags" == *"README_ORCHESTRATION"* ]]; then
        instructions+="- **Orchestration section**: Check the Multi-Agent Orchestration section in \`README.md\` against files in \`orchestrator/\`.\n"
    fi

    printf '%b' "$instructions"
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
    local diff_stats
    local new_files
    local related_docs
    diff_stats=$(get_diff_stats)
    new_files=$(get_new_files)
    related_docs=$(find_related_docs)

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

    # Detect high-risk file patterns that need specific doc cross-references
    local high_risk_flags high_risk_instructions high_risk_step
    high_risk_flags=$(detect_high_risk_docs "$changed_files")
    high_risk_instructions=$(build_high_risk_instructions "$high_risk_flags")

    # Build the conditional step 3b for the prompt
    if [[ -n "$high_risk_flags" ]]; then
        high_risk_step=$(cat <<'HRSTEP'
3b. **Cross-reference high-risk sections** (flagged changes detected):

HRSTEP
)
        high_risk_step+="${high_risk_instructions}"
        high_risk_step+="
    For each flagged section:
    - Read the SOURCE file to extract the current definitions
    - Read the TARGET doc section to check for discrepancies
    - If they differ, update the doc to match the source"
    else
        high_risk_step=""
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

Diff summary: ${diff_stats}

Changed files:
\`\`\`
${changed_files}
\`\`\`

New files added:
\`\`\`
${new_files:-none}
\`\`\`

Docs that reference related terms (may need updating):
\`\`\`
${related_docs:-none found}
\`\`\`

High-risk doc flags (auto-detected from changed files):
\`\`\`
${high_risk_flags:-none}
\`\`\`

## Your Task

1. **Analyze the changes**: Read the changed files and understand what was modified.
   Use \`git diff ${base_commit}..HEAD -- <file>\` to see specific changes.
   Pay special attention to newly added files — they often introduce new features
   or capabilities that existing docs don't cover.

2. **Check documentation impact**: Determine if documentation needs updating.
   Docs need updating when:

   - **New files introduce new tools, CLIs, or components** that aren't mentioned
     in existing docs (STRUCTURE.md, architecture/README.md, index.md).
   - **New features or capabilities** that users or agents need to know about.
   - **Breaking changes** that make existing documentation incorrect.
   - **New configuration options** or API changes.
   - **Architecture changes** that affect documented system design.

   Skip updates for: internal refactoring that doesn't change interfaces,
   performance improvements, bug fixes, test-only changes, or prompt tuning.

3. **Check these structural docs** (read them, don't delegate to sub-agents for
   large files):
   - \`docs/development/STRUCTURE.md\` — Does it list all current directories and
     key files? Are new packages/modules missing?
   - \`docs/architecture/README.md\` — Does it cover the components added/changed?
   - \`docs/index.md\` — Are new docs or templates referenced?
   - \`README.md\` — Does the root README reflect the current state? Check:
     - CLI Reference and Flags tables (compare with \`sandbox/egg_lib/cli.py\` argparse)
     - "What's Enforced" table (compare with \`gateway/phase_filter.py\` and \`.egg/phase-permissions.json\`)
     - Phase Permissions table
     - Multi-Agent Orchestration section
     - GitHub Automation workflow table
     - Quick Start instructions

${high_risk_step}
4. **Check related docs**: The "Docs that reference related terms" list above
   shows doc files that mention concepts related to the code changes. For each
   file, read it and check whether it describes behavior, interfaces, or
   workflows that were affected by this commit. Prioritize guides (\`docs/guides/\`)
   and implemented ADRs (\`docs/adr/implemented/\`) — these are most likely to
   need updates. You can skip docs that only mention the terms in passing
   (e.g., a table of contents entry) without discussing the changed feature.

   **Skip ADRs larger than 10KB** — these are reference material that rarely
   need updating from code changes, and reading them burns significant context.

   This step is critical — guides and ADRs that discuss the same feature area
   often need updating when that feature changes. For example, if a commit adds
   a new CLI flag, any guide that documents that CLI needs to mention the new
   flag. If a commit changes a workflow's behavior, any guide or ADR that
   describes that workflow needs to reflect the new behavior.

   Pay special attention to \`docs/guides/deployment.md\` — it must stay in sync
   with the README Quick Start section. If either document's deployment
   instructions changed, verify both are consistent.

5. **If updates are needed**:
   - Create a new branch: \`egg/doc-update-<short-description>\`
   - Make the documentation changes
   - Create a PR with:
     - Title: \`docs: <brief description>\` (under 50 chars)
     - Body: Explain what code changes prompted the doc updates
     - Add \`[doc-updater]\` tag at the end of the title to prevent loops

6. **If no updates are needed**:
   - Report that documentation is up to date
   - No PR needed

## Guidelines

### When docs DO need updates
- **New files added**: If new source files introduce tools, CLIs, libraries, or
  components, the project structure and architecture docs likely need updating.
  A commit that adds 500+ lines of new code almost always introduces something
  that should be documented.
- **New features**: Genuinely new capabilities users need to know about.
- **Breaking changes**: Changes that make existing documentation incorrect.
- **New configuration options**: Options users can set.
- **API changes**: New endpoints, changed parameters, removed functionality.

### When to skip doc updates
- **Internal refactoring**: Changes that don't alter interfaces or capabilities.
- **Bug fixes**: Unless the bug was documented as expected behavior.
- **Test-only changes**: New or updated tests without feature changes.
- **Prompt/config tuning**: Internal configuration that doesn't change documented
  interfaces.

### How to update docs
- **Modify existing content** rather than appending new sections. If a change refines
  existing behavior, update the existing description in place.
- **Don't add new sections** unless introducing genuinely new concepts.
- **Keep it brief**: A one-line clarification is often better than a new paragraph.
- **Remove outdated content**: If behavior changed, remove or update the old description.

### General principles
- Preserve existing doc style and formatting.
- Focus on user-facing docs and architectural changes.

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
