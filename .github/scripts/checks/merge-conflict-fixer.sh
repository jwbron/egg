#!/usr/bin/env bash
# merge-conflict-fixer.sh — Attempt to resolve merge conflicts automatically
#
# This script attempts to resolve merge conflicts by rebasing on the base branch.
# It only handles simple cases and will fail if conflicts require manual resolution.
#
# Environment variables:
#   BASE_BRANCH — Base branch to rebase onto (default: main)
#
# Exit codes:
#   0 - Conflicts resolved successfully
#   1 - Unable to resolve conflicts automatically
#   2 - Error during rebase

set -euo pipefail

BASE_BRANCH="${BASE_BRANCH:-main}"

log_info() {
    echo "[merge-conflict-fixer] $*"
}

log_error() {
    echo "[merge-conflict-fixer] ERROR: $*" >&2
}

log_info "Attempting to resolve merge conflicts with ${BASE_BRANCH}..."

# Ensure we're in a git repository
if ! git rev-parse --git-dir > /dev/null 2>&1; then
    log_error "Not in a git repository"
    exit 2
fi

# Abort any in-progress operations
git merge --abort 2>/dev/null || true
git rebase --abort 2>/dev/null || true

# Fetch latest from origin
log_info "Fetching latest from origin..."
git fetch origin "$BASE_BRANCH" --quiet

# Stash any uncommitted changes
stash_result=$(git stash push -m "merge-conflict-fixer" 2>&1 || echo "")
stashed=false
if [[ "$stash_result" != *"No local changes"* && -n "$stash_result" ]]; then
    stashed=true
    log_info "Stashed uncommitted changes"
fi

# Try rebase
log_info "Attempting rebase onto origin/${BASE_BRANCH}..."
if git rebase "origin/${BASE_BRANCH}"; then
    log_info "Rebase successful"

    # Restore stashed changes
    if [[ "$stashed" == "true" ]]; then
        if git stash pop --quiet; then
            log_info "Restored stashed changes"
        else
            log_error "Failed to restore stashed changes - they are still in stash"
        fi
    fi

    log_info "Merge conflicts resolved"
    exit 0
else
    # Rebase failed - abort and report
    git rebase --abort 2>/dev/null || true

    # Restore stashed changes
    if [[ "$stashed" == "true" ]]; then
        git stash pop --quiet 2>/dev/null || true
    fi

    log_error "Automatic conflict resolution failed"
    log_error "Manual intervention required to resolve conflicts"
    log_info "Suggested steps:"
    log_info "  1. git fetch origin ${BASE_BRANCH}"
    log_info "  2. git rebase origin/${BASE_BRANCH}"
    log_info "  3. Resolve conflicts manually"
    log_info "  4. git add <resolved-files>"
    log_info "  5. git rebase --continue"

    exit 1
fi
