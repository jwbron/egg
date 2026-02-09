#!/usr/bin/env bash
# push-contract-update.sh — Conflict-resistant contract push utility
#
# This script pushes contract updates using a "reset-and-reapply" pattern that
# handles merge conflicts. When git rebase fails due to conflicts (common with
# JSON files that multiple workflows modify concurrently), the script:
#
# 1. Aborts the failed rebase
# 2. Resets to the remote HEAD (discarding our conflicted local commit)
# 3. Re-applies the jq transformation from scratch on the fresh remote state
# 4. Creates a new commit and retries the push
#
# This approach is idempotent: the jq transformation is applied to whatever the
# current remote state is, rather than trying to merge two conflicting commits.
#
# Usage:
#   push-contract-update.sh [OPTIONS]
#
# Required environment variables:
#   CONTRACT_PATH   - Path to the contract JSON file
#   BRANCH_NAME     - Git branch name to push to
#   COMMIT_MESSAGE  - Git commit message
#
# Optional environment variables:
#   JQ_FILTER       - jq filter to apply (if not using JQ_SCRIPT_PATH)
#   JQ_SCRIPT_PATH  - Path to a script that applies the jq transformation
#                     (mutually exclusive with JQ_FILTER)
#   MAX_RETRIES     - Maximum push attempts (default: 3)
#   SOFT_FAIL       - If "true", warn but don't exit on failure (default: false)
#
# Examples:
#
#   # Simple field update
#   CONTRACT_PATH=".egg-state/contracts/123.json" \
#   BRANCH_NAME="egg/issue-123" \
#   COMMIT_MESSAGE="Advance to PR phase" \
#   JQ_FILTER='.current_phase = "pr"' \
#   push-contract-update.sh
#
#   # Using a script for complex transformations
#   CONTRACT_PATH=".egg-state/contracts/123.json" \
#   BRANCH_NAME="egg/issue-123" \
#   COMMIT_MESSAGE="Update review state" \
#   JQ_SCRIPT_PATH="/tmp/apply-review-update.sh" \
#   push-contract-update.sh
#
#   # Soft-failure mode (checkpoint)
#   SOFT_FAIL=true \
#   CONTRACT_PATH=".egg-state/contracts/123.json" \
#   BRANCH_NAME="egg/issue-123" \
#   COMMIT_MESSAGE="Checkpoint state" \
#   JQ_FILTER='.checkpoint = true' \
#   push-contract-update.sh

set -uo pipefail

# Validate required environment variables
: "${CONTRACT_PATH:?CONTRACT_PATH environment variable is required}"
: "${BRANCH_NAME:?BRANCH_NAME environment variable is required}"
: "${COMMIT_MESSAGE:?COMMIT_MESSAGE environment variable is required}"

# Optional with defaults
MAX_RETRIES="${MAX_RETRIES:-3}"
SOFT_FAIL="${SOFT_FAIL:-false}"

# Must have either JQ_FILTER or JQ_SCRIPT_PATH (or neither for external updates)
if [[ -n "${JQ_FILTER:-}" && -n "${JQ_SCRIPT_PATH:-}" ]]; then
  echo "::error::Cannot specify both JQ_FILTER and JQ_SCRIPT_PATH"
  exit 1
fi

# Function to apply the jq transformation
apply_transformation() {
  if [[ -n "${JQ_SCRIPT_PATH:-}" ]]; then
    # Run the external script to apply the transformation
    if ! bash "$JQ_SCRIPT_PATH"; then
      echo "::error::JQ_SCRIPT_PATH script failed"
      return 1
    fi
  elif [[ -n "${JQ_FILTER:-}" ]]; then
    # Apply the jq filter directly
    if ! jq "$JQ_FILTER" "$CONTRACT_PATH" > /tmp/contract-update.json; then
      echo "::error::jq filter failed"
      return 1
    fi
    mv /tmp/contract-update.json "$CONTRACT_PATH"
  fi
  # If neither is set, assume contract was already modified by external process
}

# Function to clean up any rebase state
cleanup_rebase() {
  # Abort any in-progress rebase
  git rebase --abort 2>/dev/null || true
  # Clean up any unmerged files by resetting to HEAD
  git checkout -- . 2>/dev/null || true
  # Ensure we're in a clean state
  git reset --hard HEAD 2>/dev/null || true
}

# Function to handle push failure and retry
handle_failure() {
  local attempt="$1"

  echo "Push failed (attempt $attempt/$MAX_RETRIES), resetting and reapplying..."

  # Clean up any rebase or merge state
  cleanup_rebase

  # Fetch latest remote state
  if ! git fetch origin "${BRANCH_NAME}"; then
    echo "::warning::Failed to fetch from origin"
    return 1
  fi

  # Reset to remote HEAD (discarding our failed commit)
  if ! git reset --hard "origin/${BRANCH_NAME}"; then
    echo "::warning::Failed to reset to origin/${BRANCH_NAME}"
    return 1
  fi

  # Re-apply the transformation on the fresh remote state
  if ! apply_transformation; then
    echo "::warning::Failed to re-apply transformation"
    return 1
  fi

  # Stage and commit
  git add "$CONTRACT_PATH"
  if ! git commit -m "$COMMIT_MESSAGE"; then
    echo "::warning::Nothing to commit after re-applying transformation"
    # This could happen if our changes are already in the remote
    # In that case, we're done - no push needed
    return 0
  fi

  return 0
}

# Main push loop
push_succeeded=false

for i in $(seq 1 "$MAX_RETRIES"); do
  if git push origin "${BRANCH_NAME}"; then
    echo "Push succeeded"
    push_succeeded=true
    break
  elif [[ $i -eq $MAX_RETRIES ]]; then
    if [[ "$SOFT_FAIL" == "true" ]]; then
      echo "::warning::Push failed after $MAX_RETRIES attempts (soft-fail mode)"
    else
      echo "::error::Push failed after $MAX_RETRIES attempts"
      exit 1
    fi
  else
    handle_failure "$i" || {
      if [[ "$SOFT_FAIL" == "true" ]]; then
        echo "::warning::Recovery failed during attempt $i (soft-fail mode)"
        break
      else
        echo "::error::Recovery failed during attempt $i"
        exit 1
      fi
    }
  fi
done

if [[ "$push_succeeded" == "true" ]]; then
  exit 0
elif [[ "$SOFT_FAIL" == "true" ]]; then
  exit 0
else
  exit 1
fi
