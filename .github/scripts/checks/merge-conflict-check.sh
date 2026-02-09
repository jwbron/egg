#!/usr/bin/env bash
# merge-conflict-check.sh — Check for merge conflicts with the base branch
#
# This check detects whether the current branch has merge conflicts with
# the base branch (typically main). It performs a trial merge and reports
# any conflicts found.
#
# Environment variables:
#   BASE_BRANCH — Base branch to check against (default: main)
#   GH_TOKEN    — GitHub token for API calls (optional, for remote check)
#
# Exit codes:
#   0 - No merge conflicts
#   1 - Merge conflicts detected
#   2 - Unable to check (git error)

set -euo pipefail

BASE_BRANCH="${BASE_BRANCH:-main}"

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

log_info() {
    echo "[merge-conflict-check] $*" >&2
}

log_error() {
    echo "[merge-conflict-check] ERROR: $*" >&2
}

# ---------------------------------------------------------------------------
# Main check
# ---------------------------------------------------------------------------

log_info "Checking for merge conflicts with ${BASE_BRANCH}..."

# Ensure we're in a git repository
if ! git rev-parse --git-dir > /dev/null 2>&1; then
    log_error "Not in a git repository"
    exit 2
fi

# Get current branch
current_branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
if [[ -z "$current_branch" ]]; then
    log_error "Unable to determine current branch"
    exit 2
fi

log_info "Current branch: ${current_branch}"

# Fetch latest from origin to ensure we have up-to-date refs
if ! git fetch origin "$BASE_BRANCH" --quiet 2>/dev/null; then
    log_error "Unable to fetch origin/${BASE_BRANCH}"
    exit 2
fi

# Check for existing merge conflict markers in files
conflict_files=$(git diff --name-only --diff-filter=U 2>/dev/null || true)
if [[ -n "$conflict_files" ]]; then
    log_error "Existing unresolved merge conflicts detected:"
    echo "$conflict_files"
    exit 1
fi

# Check for merge conflict markers in tracked files
# (handles case where files were added but not committed)
marker_files=$(git grep -l '<<<<<<<\|=======\|>>>>>>>' -- '*.py' '*.js' '*.ts' '*.yml' '*.yaml' '*.json' '*.md' '*.sh' 2>/dev/null || true)
if [[ -n "$marker_files" ]]; then
    log_error "Files containing merge conflict markers:"
    echo "$marker_files"
    exit 1
fi

# Attempt a trial merge to detect conflicts
log_info "Attempting trial merge with origin/${BASE_BRANCH}..."

# Save current state
original_head=$(git rev-parse HEAD)

# Create a temporary merge
if ! git merge-tree "$(git merge-base HEAD "origin/${BASE_BRANCH}")" HEAD "origin/${BASE_BRANCH}" > /tmp/merge-tree-output 2>&1; then
    # merge-tree always succeeds, check the output for conflicts
    :
fi

# Check for conflict markers in merge-tree output
if grep -q '<<<<<<<\|CONFLICT' /tmp/merge-tree-output 2>/dev/null; then
    log_error "Merge conflicts detected with origin/${BASE_BRANCH}"

    # Extract conflict information
    conflict_info=$(grep -E 'CONFLICT|changed in both' /tmp/merge-tree-output 2>/dev/null || true)
    if [[ -n "$conflict_info" ]]; then
        echo "Conflicts:"
        echo "$conflict_info"
    fi

    exit 1
fi

# Alternative: try an actual merge in a detached HEAD state
# This is more reliable but modifies the working tree temporarily
log_info "Performing validation merge check..."

# Stash any uncommitted changes
stash_result=$(git stash push -m "merge-conflict-check" 2>&1 || echo "")
stashed=false
if [[ "$stash_result" != *"No local changes"* && "$stash_result" != "" ]]; then
    stashed=true
fi

# Try to merge
merge_result=0
if ! git merge --no-commit --no-ff "origin/${BASE_BRANCH}" > /tmp/merge-output 2>&1; then
    merge_result=1
fi

# Check for conflicts
if [[ $merge_result -ne 0 ]] || git ls-files -u | grep -q .; then
    # Abort the merge
    git merge --abort 2>/dev/null || true

    # Restore stashed changes
    if [[ "$stashed" == "true" ]]; then
        git stash pop --quiet 2>/dev/null || true
    fi

    log_error "Merge conflicts detected with origin/${BASE_BRANCH}"
    cat /tmp/merge-output 2>/dev/null || true
    exit 1
fi

# Abort the successful trial merge (we don't want to actually merge)
git merge --abort 2>/dev/null || git reset --hard "$original_head" 2>/dev/null || true

# Restore stashed changes
if [[ "$stashed" == "true" ]]; then
    git stash pop --quiet 2>/dev/null || true
fi

log_info "No merge conflicts detected"
exit 0
