#!/bin/bash
# escalate.sh - Handle SDLC pipeline escalation
#
# This script is called when the circuit breaker opens and human intervention
# is required. It:
# - Labels the issue with 'needs-human-intervention'
# - Posts a context comment with task history and reviewer feedback
# - Creates HITL decision checkboxes for the stuck task
#
# Usage: escalate.sh <issue_number> <repo_path>
#
# Environment variables:
#   GH_TOKEN - GitHub token for API access
#   GITHUB_REPOSITORY - Repository in owner/repo format

set -euo pipefail

# ============================================================
# Configuration
# ============================================================

ISSUE_NUMBER="${1:-}"
REPO_PATH="${2:-$(pwd)}"

if [[ -z "$ISSUE_NUMBER" ]]; then
    echo "ERROR: Issue number required"
    echo "Usage: escalate.sh <issue_number> [repo_path]"
    exit 1
fi

# Validate issue number is numeric (path traversal prevention)
if ! [[ "$ISSUE_NUMBER" =~ ^[0-9]+$ ]]; then
    echo "ERROR: Invalid issue number: $ISSUE_NUMBER"
    exit 1
fi

CONTRACT_PATH="${REPO_PATH}/.egg-state/contracts/${ISSUE_NUMBER}.json"

# ============================================================
# Helper functions
# ============================================================

log_info() {
    echo "[INFO] $*"
}

log_error() {
    echo "[ERROR] $*" >&2
}

# ============================================================
# Load contract and extract escalation info
# ============================================================

if [[ ! -f "$CONTRACT_PATH" ]]; then
    log_error "Contract not found: $CONTRACT_PATH"
    exit 1
fi

log_info "Loading contract from $CONTRACT_PATH"

# Extract circuit breaker status
CB_STATUS=$(jq -r '.circuit_breaker.status' "$CONTRACT_PATH")
TOTAL_CYCLES=$(jq -r '.circuit_breaker.total_cycles' "$CONTRACT_PATH")
MAX_CYCLES=$(jq -r '.circuit_breaker.max_total_cycles' "$CONTRACT_PATH")

if [[ "$CB_STATUS" != "open" ]]; then
    log_info "Circuit breaker is not open (status: $CB_STATUS), no escalation needed"
    exit 0
fi

log_info "Circuit breaker is OPEN - triggering escalation"

# ============================================================
# Collect escalation context
# ============================================================

# Get escalated tasks
ESCALATED_TASKS=$(jq -r '
    [.phases[] |
        .tasks[] |
        select(.escalated == true) |
        "- **\(.id)**: \(.description)\n  - Status: \(.status)\n  - Cycles: \(.review_cycles)/\(.max_cycles)"
    ] | join("\n")
' "$CONTRACT_PATH")

# Get incomplete tasks
INCOMPLETE_TASKS=$(jq -r '
    [.phases[] |
        .tasks[] |
        select(.status != "complete" and .escalated != true) |
        "- **\(.id)**: \(.description) (status: \(.status))"
    ] | join("\n")
' "$CONTRACT_PATH")

# Get recent review feedback (last 5 entries)
RECENT_FEEDBACK=$(jq -r '
    [.phases[].review_feedback[-5:][]] |
    map("- [\(.timestamp)] Task \(.task_id): \(.feedback)") |
    join("\n")
' "$CONTRACT_PATH")

# Get stuck phases
STUCK_PHASES=$(jq -r '
    [.phases[] |
        select(.escalated == true) |
        "- **\(.id)** (\(.name)): \(.escalation_reason // "No reason provided")"
    ] | join("\n")
' "$CONTRACT_PATH")

# ============================================================
# Add escalation label
# ============================================================

log_info "Adding 'needs-human-intervention' label to issue #$ISSUE_NUMBER"

gh issue label add "needs-human-intervention" --issue "$ISSUE_NUMBER" 2>/dev/null || {
    log_info "Label may already exist or could not be added"
}

# ============================================================
# Build and post escalation comment
# ============================================================

COMMENT_BODY="## SDLC Pipeline Escalation Required

The pipeline has encountered a blocking condition and requires human intervention.

### Circuit Breaker Status
- **Status:** OPEN
- **Total cycles:** ${TOTAL_CYCLES}/${MAX_CYCLES}

### Escalated Tasks
${ESCALATED_TASKS:-_No tasks explicitly escalated_}

### Other Incomplete Tasks
${INCOMPLETE_TASKS:-_No other incomplete tasks_}

### Stuck Phases
${STUCK_PHASES:-_No phases explicitly stuck_}

### Recent Review Feedback
${RECENT_FEEDBACK:-_No recent feedback_}

---

## Resolution Options

Please select an option to help the pipeline proceed:

### Option 1: Provide Guidance
<!-- HITL-DECISION: guidance -->
- [ ] I will provide additional context or requirements below
- [ ] The acceptance criteria should be adjusted
- [ ] Break this task into smaller sub-tasks

### Option 2: Override
<!-- HITL-DECISION: override -->
- [ ] Mark current tasks as complete (override review)
- [ ] Skip remaining tasks in this phase
- [ ] Cancel the pipeline for this issue

### Option 3: Manual Intervention
<!-- HITL-DECISION: manual -->
- [ ] I will complete the remaining work manually
- [ ] Assign to a different agent/person

---

**Instructions:** Check one or more boxes above, then add any additional context in a reply comment. The pipeline will process your selection after a 30-second debounce period.

---

_This escalation was triggered automatically by the SDLC pipeline circuit breaker._

--- Authored by egg"

log_info "Posting escalation comment to issue #$ISSUE_NUMBER"

gh issue comment "$ISSUE_NUMBER" --body "$COMMENT_BODY"

# ============================================================
# Update contract with escalation timestamp
# ============================================================

log_info "Updating contract with escalation timestamp"

TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

# Add audit entry for escalation
jq --arg ts "$TIMESTAMP" '
    .audit_log += [{
        "timestamp": $ts,
        "actor": "system",
        "role": "system",
        "action": "transition",
        "field_path": "escalation",
        "old_value": null,
        "new_value": "triggered",
        "reason": "Circuit breaker opened - human intervention required"
    }]
' "$CONTRACT_PATH" > /tmp/contract.json && mv /tmp/contract.json "$CONTRACT_PATH"

log_info "Escalation complete"
