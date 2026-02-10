#!/usr/bin/env bash
# build-conflict-prompt.sh — Build a minimal prompt for agent-driven conflict resolution
#
# This script creates a minimal prompt that tells Claude to resolve merge
# conflicts by merging the base branch into the PR branch, resolving conflicts
# intelligently, and pushing the result. Using merge (not rebase) preserves
# PR history and enables easy retry via revert if resolution fails.
#
# Environment variables:
#   PR_NUMBER          — Pull request number
#   GITHUB_REPOSITORY  — owner/repo
#   BASE_REF           — Base branch to merge from (e.g., main)
#   RUNNER_TEMP        — Temp directory for prompt file
#
# Output:
#   Sets 'prompt-file' and 'model' in $GITHUB_OUTPUT

set -euo pipefail

# ---------------------------------------------------------------------------
# Fetch conflict resolution rules (or use defaults)
# ---------------------------------------------------------------------------

fetch_conflict_rules() {
    local rules_file=".egg/conflict-rules.md"

    if [[ -f "$rules_file" ]]; then
        cat "$rules_file"
    else
        # Default conflict resolution rules when no repo-specific rules exist
        cat <<'EOF'
## Default Conflict Resolution Rules

**Auto-resolvable (resolve and push):**
- Lock files (package-lock.json, yarn.lock, poetry.lock) — regenerate
- Additive changes (both sides add different content to the same file)
- Formatting conflicts (whitespace, import order)
- Version bumps in package.json, pyproject.toml

**Escalate to human (abort and post comment):**
- Semantic conflicts (both sides modify the same logic differently)
- Breaking API changes that conflict
- Security-sensitive code (auth, encryption, access control)
- Database migrations that conflict
- Configuration conflicts affecting production
EOF
    fi
}

# ---------------------------------------------------------------------------
# Build the prompt
# ---------------------------------------------------------------------------

build_prompt() {
    local conflict_rules
    conflict_rules=$(fetch_conflict_rules)

    # Load conflict conventions if available
    local conventions_file
    conventions_file="$(dirname "$0")/conflict-conventions.md"
    local conventions=""
    if [[ -f "$conventions_file" ]]; then
        conventions=$(cat "$conventions_file")
    fi

    local base_ref="${BASE_REF:-main}"

    local prompt
    prompt="Resolve merge conflicts on PR #${PR_NUMBER} in ${GITHUB_REPOSITORY}.

The PR has conflicts with the \`${base_ref}\` branch that need to be resolved.

## Your task

**IMPORTANT: Resolve conflicts via merge (not rebase), verify locally, then push once.**

Using merge instead of rebase preserves the PR's commit history. If the resolution is wrong, the merge commit can be easily reverted without losing any work.

1. **Fetch the base branch**: Run \`git fetch origin ${base_ref}\` to get the latest.

2. **Preview the merge**: Run \`git merge --no-commit origin/${base_ref}\` to see conflicts without committing yet.

3. **Analyze each conflict**: Before resolving, categorize each conflict:
   - **Additive** — Both sides add different content (safe to include both)
   - **Lock file** — package-lock.json, yarn.lock, etc. (regenerate, never merge manually)
   - **Formatting** — Whitespace, import order (use linter to normalize)
   - **Semantic** — Both sides modify same logic differently (may need human review)
   - **Security-sensitive** — Auth, encryption, access control (always escalate)

4. **Resolve each conflict**:
   - For each conflicting file, examine the conflict markers (\`<<<<<<<\`, \`=======\`, \`>>>>>>>\`)
   - Understand what both sides changed and why
   - Resolve based on the rules and categorization above
   - After resolving a file: \`git add <file>\`

5. **Commit the merge**: After resolving all conflicts, run \`git commit\` to complete the merge. Use a descriptive message like: \"Merge origin/${base_ref} into <branch-name>: resolve conflicts in <files>\"

6. **Verify locally**: Run all checks (\`make lint\`, \`make test\`, \`make build\` or equivalent). Fix any issues introduced by the resolution.

7. **Push the result**: After all checks pass, run \`git push\` to update the PR. Do NOT use \`--force\` or \`--force-with-lease\`.

8. **If you cannot resolve**: If any conflict requires human judgment (semantic conflicts, breaking API changes, security-sensitive code):
   - Run \`git merge --abort\` to restore the original state
   - Post a comment on the PR explaining:
     - Which files have conflicts that need human review
     - What each side of the conflict is trying to do
     - Why you couldn't auto-resolve (what decision is needed)

9. **If resolution was wrong** (CI fails after push): If post-merge CI fails due to your resolution:
   - Revert the merge commit: \`git revert -m 1 HEAD\`
   - Push the revert: \`git push\`
   - Post a comment explaining what went wrong
   - You may attempt another merge with a different resolution strategy

## Conflict Resolution Rules

${conflict_rules}

## Conventions

${conventions:-Resolve conflicts conservatively. When in doubt, abort and escalate to human review. Use git push (never --force) to push the merged branch. Sign comments with: — Authored by egg}
"

    # Write prompt to temp file
    local prompt_dir="${RUNNER_TEMP:-/tmp}"
    mkdir -p "$prompt_dir"
    local prompt_file="${prompt_dir}/conflict-prompt-${PR_NUMBER}.txt"
    echo "$prompt" > "$prompt_file"

    # Use opus for conflict resolution (needs reasoning capability for merges)
    local model="opus"

    # Write outputs
    {
        echo "prompt-file=${prompt_file}"
        echo "model=${model}"
    } >> "${GITHUB_OUTPUT:-/dev/null}"

    echo "Conflict resolution prompt built: ${#prompt} chars, model=${model}"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

: "${PR_NUMBER:?PR_NUMBER is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"

build_prompt
