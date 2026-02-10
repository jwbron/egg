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
# Fetch PR context from GitHub API
# ---------------------------------------------------------------------------

fetch_pr_context() {
    local pr_json
    pr_json=$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}" 2>/dev/null || echo "{}")

    local pr_title pr_body pr_author base_ref head_ref
    pr_title=$(echo "$pr_json" | jq -r '.title // ""')
    pr_body=$(echo "$pr_json" | jq -r '.body // ""')
    pr_author=$(echo "$pr_json" | jq -r '.user.login // ""')
    base_ref=$(echo "$pr_json" | jq -r '.base.ref // "main"')
    head_ref=$(echo "$pr_json" | jq -r '.head.ref // ""')

    if [[ -n "$pr_title" ]]; then
        cat <<EOF
## PR Context

**Title:** ${pr_title}
**Author:** ${pr_author}
**Branch:** ${head_ref} → ${base_ref}

**Description:**
${pr_body:-"(No description provided)"}
EOF
    fi
}

fetch_commit_messages() {
    local base_ref="${BASE_REF:-main}"

    # Get commits unique to the PR branch (not in base)
    local pr_commits
    pr_commits=$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}/commits" 2>/dev/null \
        | jq -r '.[] | "- \(.sha[0:7]): \(.commit.message | split("\n")[0])"' 2>/dev/null \
        | head -20 || echo "")

    # Get recent commits on base branch
    local base_commits
    base_commits=$(gh api "repos/${GITHUB_REPOSITORY}/commits?sha=${base_ref}&per_page=10" 2>/dev/null \
        | jq -r '.[] | "- \(.sha[0:7]): \(.commit.message | split("\n")[0])"' 2>/dev/null \
        | head -10 || echo "")

    if [[ -n "$pr_commits" || -n "$base_commits" ]]; then
        cat <<EOF

## Commit History

### PR Branch Commits (what this PR adds):
${pr_commits:-"(Unable to fetch commits)"}

### Recent Base Branch Commits (what's being merged in):
${base_commits:-"(Unable to fetch commits)"}
EOF
    fi
}

fetch_review_comments() {
    # Get review comments that might inform conflict resolution
    local comments
    comments=$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}/comments" 2>/dev/null \
        | jq -r '.[] | select(.body | test("conflict|merge|rebase|fix"; "i") | not) | "**@\(.user.login)** on \(.path):\n\(.body)\n"' 2>/dev/null \
        | head -c 2000 || echo "")

    # Get general PR comments
    local pr_comments
    pr_comments=$(gh api "repos/${GITHUB_REPOSITORY}/issues/${PR_NUMBER}/comments" 2>/dev/null \
        | jq -r '.[] | select(.user.login | test("\\[bot\\]$") | not) | select(.body | contains("<!-- egg-") | not) | "**@\(.user.login):** \(.body | split("\n")[0])"' 2>/dev/null \
        | head -20 || echo "")

    if [[ -n "$comments" || -n "$pr_comments" ]]; then
        cat <<EOF

## Review Feedback

These comments may provide context for how to resolve conflicts:

${comments}${pr_comments:-"(No relevant review comments)"}
EOF
    fi
}

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

    # Fetch enhanced context from GitHub API
    local pr_context commit_messages review_comments
    pr_context=$(fetch_pr_context)
    commit_messages=$(fetch_commit_messages)
    review_comments=$(fetch_review_comments)

    local prompt
    prompt="Resolve merge conflicts on PR #${PR_NUMBER} in ${GITHUB_REPOSITORY}.

${pr_context}

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
   - For each conflicting file, read the ENTIRE file (not just the conflict markers) to understand context
   - Examine the conflict markers (\`<<<<<<<\`, \`=======\`, \`>>>>>>>\`) and surrounding code
   - Understand what both sides changed and why—the full file context is crucial
   - Check if the conflict affects function signatures, imports, or dependencies used elsewhere
   - Resolve based on the rules and categorization above
   - After resolving a file: \`git add <file>\`

5. **Commit the merge**: After resolving all conflicts, run \`git commit\` to complete the merge. Use a descriptive message like: \"Merge origin/${base_ref} into <branch-name>: resolve conflicts in <files>\"

6. **Verify locally**: Run all checks (\`make lint\`, \`make test\`, \`make build\` or equivalent). Fix any issues introduced by the resolution.

7. **Push the result**: After all checks pass, run \`git push\` to update the PR. Do NOT use \`--force\` or \`--force-with-lease\`.

8. **Post a summary comment**: After successfully pushing, post a comment on the PR with:
   - A list of files that had conflicts and how each was resolved
   - The conflict category for each file (additive, lock file, formatting, semantic)
   - Any concerns or edge cases the reviewer should check
   - Example format:
     \`\`\`
     ## Conflict Resolution Summary

     Resolved merge conflicts with \`${base_ref}\`:

     | File | Category | Resolution |
     |------|----------|------------|
     | package-lock.json | Lock file | Regenerated via npm install |
     | src/utils.ts | Additive | Included both new functions |

     **Please review:** The utils.ts changes—both sides added similar validation logic.

     — Authored by egg
     \`\`\`

9. **If you cannot resolve**: If any conflict requires human judgment (semantic conflicts, breaking API changes, security-sensitive code):
   - Run \`git merge --abort\` to restore the original state
   - Post a comment on the PR explaining:
     - Which files have conflicts that need human review
     - What each side of the conflict is trying to do
     - Why you couldn't auto-resolve (what decision is needed)

10. **If resolution was wrong** (CI fails after push): If post-merge CI fails due to your resolution:
   - Revert the merge commit: \`git revert -m 1 HEAD\`
   - Push the revert: \`git push\`
   - Post a comment explaining what went wrong
   - You may attempt another merge with a different resolution strategy

${commit_messages}

${review_comments}

## Conflict Resolution Rules

${conflict_rules}

## Semantic Analysis

When resolving conflicts, consider:
1. **What is each side trying to accomplish?** Read the commit messages and PR description.
2. **Are the changes complementary or contradictory?** Additive changes can often be merged; contradictory logic needs human input.
3. **Does the resolution maintain the intent of both changes?** The goal is to preserve both contributions, not pick a winner.
4. **What would break if you choose wrong?** Tests, type checking, and runtime behavior—verify all three.

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
