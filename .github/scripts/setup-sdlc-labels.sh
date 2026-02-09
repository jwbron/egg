#!/usr/bin/env bash
#
# Setup SDLC pipeline labels for the repository.
# This script is idempotent - running it multiple times is safe.
#
# Labels created:
#   sdlc:refine           - Issue is in the refine phase
#   sdlc:plan             - Issue is in the plan phase
#   sdlc:implement        - Issue is in the implement phase
#   sdlc:pr               - Issue has a PR in review
#   sdlc:awaiting-approval - Waiting for human approval
#
# Usage:
#   ./setup-sdlc-labels.sh [--repo owner/repo]
#
# Requires:
#   - gh CLI authenticated
#   - GH_TOKEN environment variable or gh auth login

set -euo pipefail

# Default to current repository if not specified
REPO="${REPO:-}"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo)
            REPO="$2"
            shift 2
            ;;
        *)
            echo "Unknown argument: $1" >&2
            exit 1
            ;;
    esac
done

# If no repo specified, get from git remote
if [[ -z "$REPO" ]]; then
    REPO=$(gh repo view --json nameWithOwner --jq '.nameWithOwner' 2>/dev/null) || {
        echo "Error: Could not determine repository. Use --repo owner/repo" >&2
        exit 1
    }
fi

echo "Setting up SDLC labels for repository: ${REPO}"

# Define labels: name|color|description
LABELS=(
    "sdlc:refine|0E8A16|SDLC pipeline: refine phase"
    "sdlc:plan|1D76DB|SDLC pipeline: plan phase"
    "sdlc:implement|D93F0B|SDLC pipeline: implement phase"
    "sdlc:pr|5319E7|SDLC pipeline: PR in review"
    "sdlc:awaiting-approval|FBCA04|SDLC pipeline: waiting for human approval"
)

for label_spec in "${LABELS[@]}"; do
    IFS='|' read -r name color description <<< "$label_spec"

    # Check if label exists
    if gh api "repos/${REPO}/labels/${name}" >/dev/null 2>&1; then
        echo "Updating label: ${name}"
        gh api "repos/${REPO}/labels/${name}" \
            -X PATCH \
            -f color="${color}" \
            -f description="${description}" \
            --silent
    else
        echo "Creating label: ${name}"
        gh api "repos/${REPO}/labels" \
            -X POST \
            -f name="${name}" \
            -f color="${color}" \
            -f description="${description}" \
            --silent
    fi
done

echo "SDLC labels setup complete"
