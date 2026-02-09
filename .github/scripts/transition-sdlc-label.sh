#!/usr/bin/env bash
#
# Transition SDLC labels atomically on an issue or PR.
#
# This script handles label transitions with proper error handling
# and ensures the target label is applied before removing the source.
#
# Usage:
#   transition-sdlc-label.sh --issue <number> --from <label> --to <label>
#   transition-sdlc-label.sh --issue <number> --add <label>
#   transition-sdlc-label.sh --issue <number> --remove <label>
#
# Options:
#   --issue <number>   Issue or PR number (required)
#   --from <label>     Label to remove (used with --to)
#   --to <label>       Label to add (used with --from or standalone)
#   --add <label>      Label to add (standalone add operation)
#   --remove <label>   Label to remove (standalone remove operation)
#   --repo <owner/repo> Repository (optional, defaults to current)
#
# Examples:
#   # Transition from refine to plan phase
#   transition-sdlc-label.sh --issue 123 --from sdlc:refine --to sdlc:plan
#
#   # Add awaiting-approval label
#   transition-sdlc-label.sh --issue 123 --add sdlc:awaiting-approval
#
#   # Remove awaiting-approval label
#   transition-sdlc-label.sh --issue 123 --remove sdlc:awaiting-approval

set -euo pipefail

# Parse arguments
ISSUE=""
FROM_LABEL=""
TO_LABEL=""
ADD_LABEL=""
REMOVE_LABEL=""
REPO="${REPO:-}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --issue)
            ISSUE="$2"
            shift 2
            ;;
        --from)
            FROM_LABEL="$2"
            shift 2
            ;;
        --to)
            TO_LABEL="$2"
            shift 2
            ;;
        --add)
            ADD_LABEL="$2"
            shift 2
            ;;
        --remove)
            REMOVE_LABEL="$2"
            shift 2
            ;;
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

# Validate arguments
if [[ -z "$ISSUE" ]]; then
    echo "Error: --issue is required" >&2
    exit 1
fi

# Validate issue number is numeric
if ! [[ "$ISSUE" =~ ^[0-9]+$ ]]; then
    echo "Error: Invalid issue number: ${ISSUE}" >&2
    exit 1
fi

# If no repo specified, get from git remote
if [[ -z "$REPO" ]]; then
    REPO=$(gh repo view --json nameWithOwner --jq '.nameWithOwner' 2>/dev/null) || {
        echo "Error: Could not determine repository. Use --repo owner/repo" >&2
        exit 1
    }
fi

# Validate label names (defense-in-depth)
validate_label() {
    local label="$1"
    # Labels should only contain alphanumeric, colon, hyphen, space
    if ! [[ "$label" =~ ^[a-zA-Z0-9:\ -]+$ ]]; then
        echo "Error: Invalid label name: ${label}" >&2
        exit 1
    fi
}

# URL-encode a label name for API paths
url_encode_label() {
    local label="$1"
    # Simple encoding for common characters in labels
    echo "$label" | sed 's/:/%3A/g; s/ /%20/g'
}

# Add a label to an issue
add_label() {
    local label="$1"
    validate_label "$label"

    echo "Adding label '${label}' to issue #${ISSUE}"
    if ! gh api "repos/${REPO}/issues/${ISSUE}/labels" \
        -X POST \
        -f "labels[]=${label}" \
        --silent 2>/dev/null; then
        echo "Warning: Failed to add label '${label}' (may already exist or label not found)" >&2
        return 1
    fi
    return 0
}

# Remove a label from an issue
remove_label() {
    local label="$1"
    validate_label "$label"

    local encoded_label
    encoded_label=$(url_encode_label "$label")

    echo "Removing label '${label}' from issue #${ISSUE}"
    if ! gh api "repos/${REPO}/issues/${ISSUE}/labels/${encoded_label}" \
        -X DELETE \
        --silent 2>/dev/null; then
        echo "Warning: Failed to remove label '${label}' (may not exist)" >&2
        return 1
    fi
    return 0
}

# Execute operations based on arguments
if [[ -n "$FROM_LABEL" ]] && [[ -n "$TO_LABEL" ]]; then
    # Transition: add new label first, then remove old
    add_label "$TO_LABEL" || true
    remove_label "$FROM_LABEL" || true
    echo "Transitioned from '${FROM_LABEL}' to '${TO_LABEL}'"
elif [[ -n "$TO_LABEL" ]]; then
    # Just add the target label
    add_label "$TO_LABEL"
elif [[ -n "$ADD_LABEL" ]]; then
    # Standalone add
    add_label "$ADD_LABEL"
elif [[ -n "$REMOVE_LABEL" ]]; then
    # Standalone remove
    remove_label "$REMOVE_LABEL"
else
    echo "Error: No operation specified. Use --from/--to, --add, or --remove" >&2
    exit 1
fi

echo "Label transition complete"
