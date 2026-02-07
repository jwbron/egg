#!/bin/bash
# Escalation script for SDLC pipeline
#
# Usage: escalate.sh <issue_number> [reason]
#
# This script:
# 1. Labels the issue with 'needs-human-intervention'
# 2. Posts a context comment with task history
# 3. Creates HITL decision checkboxes

set -euo pipefail

ISSUE_NUMBER="${1:-}"
REASON="${2:-Circuit breaker triggered}"
REPO_ROOT="${GITHUB_WORKSPACE:-$(pwd)}"

if [[ -z "$ISSUE_NUMBER" ]]; then
    echo "Error: Issue number required" >&2
    echo "Usage: $0 <issue_number> [reason]" >&2
    exit 1
fi

CONTRACT_FILE="${REPO_ROOT}/.egg/contracts/${ISSUE_NUMBER}.json"

# Check for gh CLI
if ! command -v gh &> /dev/null; then
    echo "Error: gh CLI not found" >&2
    exit 1
fi

# Add label
echo "Adding needs-human-intervention label..."
gh issue edit "$ISSUE_NUMBER" --add-label "needs-human-intervention" || true

# Build context from contract
CONTEXT=""
if [[ -f "$CONTRACT_FILE" ]]; then
    CURRENT_PHASE=$(jq -r '.currentPhase // "unknown"' "$CONTRACT_FILE")
    TOTAL_CYCLES=$(jq -r '.circuit_breaker.total_cycles // 0' "$CONTRACT_FILE")

    # Get task summary
    TASK_SUMMARY=$(jq -r '
        .phases[] |
        "### \(.name) [\(.status)]",
        (.tasks[] |
            "- **\(.id)**: \(.description)",
            "  - Status: \(.status)",
            "  - Cycles: \(.review_cycles)/\(.max_cycles)",
            (if .feedback then "  - Feedback: \(.feedback | join(\"; \"))" else "" end),
            (if .escalated then "  - **ESCALATED**" else "" end)
        )
    ' "$CONTRACT_FILE" 2>/dev/null || echo "Unable to parse contract")

    CONTEXT="
**Current Phase**: ${CURRENT_PHASE}
**Total Cycles**: ${TOTAL_CYCLES}

## Task Status

${TASK_SUMMARY}
"
fi

# Build the escalation comment
COMMENT_BODY=$(cat << EOF
## Pipeline Escalation Required

**Reason**: ${REASON}

${CONTEXT}

---

## Human Decision Required

Please select one option to proceed:

- [ ] **Continue** - Reset cycle count and retry the stuck task
- [ ] **Skip task** - Mark the current task as failed and continue to next
- [ ] **Abort pipeline** - Stop the pipeline for this issue

### Additional Context

If you need to provide guidance or context, add a comment below before making a selection.

---

*This escalation was triggered automatically by the SDLC pipeline circuit breaker.*

— Authored by egg
EOF
)

# Post the comment
echo "Posting escalation comment..."
gh issue comment "$ISSUE_NUMBER" --body "$COMMENT_BODY"

echo "Escalation complete for issue #${ISSUE_NUMBER}"
echo "Waiting for human decision via checkbox selection."
