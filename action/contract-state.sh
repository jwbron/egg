#!/bin/bash
# Contract state management for SDLC pipeline
#
# Usage:
#   contract-state.sh load <issue_number>     - Load and display contract
#   contract-state.sh phase <issue_number>    - Get current phase
#   contract-state.sh tasks <issue_number>    - List tasks and status
#   contract-state.sh cycles <issue_number>   - Get cycle counts
#   contract-state.sh commit <issue_number>   - Commit contract changes

set -euo pipefail

REPO_ROOT="${GITHUB_WORKSPACE:-$(pwd)}"

get_contract_path() {
    local issue_number="$1"
    echo "${REPO_ROOT}/.egg/contracts/${issue_number}.json"
}

cmd_load() {
    local issue_number="$1"
    local contract_file
    contract_file=$(get_contract_path "$issue_number")

    if [[ ! -f "$contract_file" ]]; then
        echo "Contract not found for issue #${issue_number}" >&2
        return 1
    fi

    cat "$contract_file"
}

cmd_phase() {
    local issue_number="$1"
    local contract_file
    contract_file=$(get_contract_path "$issue_number")

    if [[ ! -f "$contract_file" ]]; then
        echo "refine"  # Default phase
        return 0
    fi

    jq -r '.currentPhase // "refine"' "$contract_file"
}

cmd_tasks() {
    local issue_number="$1"
    local contract_file
    contract_file=$(get_contract_path "$issue_number")

    if [[ ! -f "$contract_file" ]]; then
        echo "No contract found" >&2
        return 1
    fi

    echo "Tasks for issue #${issue_number}:"
    echo ""

    jq -r '
        .phases[] |
        "Phase: \(.id) - \(.name) [\(.status)]",
        (.tasks[] | "  \(.id): \(.description) [\(.status)]\(if .commit then " (\(.commit[0:7]))" else "" end)")
    ' "$contract_file"
}

cmd_cycles() {
    local issue_number="$1"
    local contract_file
    contract_file=$(get_contract_path "$issue_number")

    if [[ ! -f "$contract_file" ]]; then
        echo "total=0"
        echo "status=closed"
        return 0
    fi

    local total
    local status
    total=$(jq -r '.circuit_breaker.total_cycles // 0' "$contract_file")
    status=$(jq -r '.circuit_breaker.status // "closed"' "$contract_file")

    echo "total=${total}"
    echo "status=${status}"
}

cmd_commit() {
    local issue_number="$1"
    local message="${2:-Update contract for issue #${issue_number}}"
    local contract_file
    contract_file=$(get_contract_path "$issue_number")

    if [[ ! -f "$contract_file" ]]; then
        echo "Contract not found" >&2
        return 1
    fi

    cd "$REPO_ROOT"
    git add "$contract_file"

    if git diff --staged --quiet; then
        echo "No changes to commit"
        return 0
    fi

    git commit -m "$message"
    echo "Contract committed"
}

# Parse command
COMMAND="${1:-}"
ISSUE_NUMBER="${2:-}"

if [[ -z "$COMMAND" ]]; then
    echo "Usage: $0 <command> <issue_number>" >&2
    echo "Commands: load, phase, tasks, cycles, commit" >&2
    exit 1
fi

if [[ -z "$ISSUE_NUMBER" && "$COMMAND" != "help" ]]; then
    echo "Error: Issue number required" >&2
    exit 1
fi

case "$COMMAND" in
    load)
        cmd_load "$ISSUE_NUMBER"
        ;;
    phase)
        cmd_phase "$ISSUE_NUMBER"
        ;;
    tasks)
        cmd_tasks "$ISSUE_NUMBER"
        ;;
    cycles)
        cmd_cycles "$ISSUE_NUMBER"
        ;;
    commit)
        cmd_commit "$ISSUE_NUMBER" "${3:-}"
        ;;
    help)
        echo "Contract State Management"
        echo ""
        echo "Commands:"
        echo "  load <issue>    - Load and display contract JSON"
        echo "  phase <issue>   - Get current pipeline phase"
        echo "  tasks <issue>   - List tasks and their status"
        echo "  cycles <issue>  - Get circuit breaker cycle counts"
        echo "  commit <issue>  - Commit contract changes"
        ;;
    *)
        echo "Unknown command: $COMMAND" >&2
        exit 1
        ;;
esac
