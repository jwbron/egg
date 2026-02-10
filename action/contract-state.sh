#!/usr/bin/env bash
# contract-state.sh — Contract state management for SDLC pipeline
#
# This script provides functions for managing contract state during
# the SDLC pipeline execution. It handles loading, updating, and
# committing contract changes.
#
# Usage:
#   contract-state.sh <command> [options]
#
# Commands:
#   load              Load and display contract state
#   update-after-implement  Update contract after implementation phase
#   get-current-phase Get the current pipeline phase
#   set-phase         Set the current pipeline phase
#   summary           Print contract summary
#
# Environment variables:
#   ISSUE_NUMBER    — GitHub issue number (required)
#   REPO_PATH       — Path to repository (defaults to current directory)
#   BRANCH_NAME     — Current branch name
#   GH_TOKEN        — GitHub token for git operations

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONTRACTS_DIR=".egg-state/contracts"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log_info() {
  echo "[contract-state] $1"
}

log_error() {
  echo "[contract-state] ERROR: $1" >&2
}

get_contract_path() {
  local issue="${1:?ISSUE_NUMBER required}"
  local repo_path="${REPO_PATH:-$(pwd)}"
  echo "${repo_path}/${CONTRACTS_DIR}/${issue}.json"
}

ensure_contract_exists() {
  local contract_path="$1"
  if [[ ! -f "$contract_path" ]]; then
    log_error "Contract not found at ${contract_path}"
    return 1
  fi
}

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

cmd_load() {
  local issue="${ISSUE_NUMBER:?ISSUE_NUMBER required}"
  local contract_path
  contract_path=$(get_contract_path "$issue")

  ensure_contract_exists "$contract_path"

  # Output contract state
  jq '.' "$contract_path"
}

cmd_update_after_implement() {
  local issue="${ISSUE_NUMBER:?ISSUE_NUMBER required}"
  local contract_path
  contract_path=$(get_contract_path "$issue")

  ensure_contract_exists "$contract_path"

  # Check if all tasks have commits linked
  local tasks_without_commits
  tasks_without_commits=$(jq '[.phases[].tasks[] | select(.commit == null or .commit == "")] | length' "$contract_path")

  # Count tasks by status
  local pending_tasks in_progress_tasks completed_tasks
  pending_tasks=$(jq '[.phases[].tasks[] | select(.status == "pending")] | length' "$contract_path")
  in_progress_tasks=$(jq '[.phases[].tasks[] | select(.status == "in_progress")] | length' "$contract_path")
  completed_tasks=$(jq '[.phases[].tasks[] | select(.status == "complete")] | length' "$contract_path")
  local total_tasks
  total_tasks=$(jq '[.phases[].tasks[]] | length' "$contract_path")

  log_info "Task summary: ${completed_tasks}/${total_tasks} complete, ${in_progress_tasks} in progress, ${pending_tasks} pending"
  log_info "Tasks without commits: ${tasks_without_commits}"

  # Update any tasks that have commits but are still pending to in_progress
  local updated_contract
  updated_contract=$(jq '
    .phases |= map(
      .tasks |= map(
        if .commit != null and .commit != "" and .status == "pending" then
          .status = "in_progress"
        else
          .
        end
      )
    )
  ' "$contract_path")

  echo "$updated_contract" > "$contract_path"

  # Determine if implementation is complete (all tasks have commits)
  if [[ "$tasks_without_commits" -eq 0 ]]; then
    log_info "All tasks have linked commits"
    echo "complete=true" >> "${GITHUB_OUTPUT:-/dev/null}"
    echo "tasks_completed=${total_tasks}" >> "${GITHUB_OUTPUT:-/dev/null}"
  else
    log_info "Implementation incomplete: ${tasks_without_commits} tasks need commits"
    echo "complete=false" >> "${GITHUB_OUTPUT:-/dev/null}"
    echo "tasks_completed=${completed_tasks}" >> "${GITHUB_OUTPUT:-/dev/null}"
  fi
}

cmd_get_current_phase() {
  local issue="${ISSUE_NUMBER:?ISSUE_NUMBER required}"
  local contract_path
  contract_path=$(get_contract_path "$issue")

  ensure_contract_exists "$contract_path"

  local phase
  phase=$(jq -r '.current_phase' "$contract_path")

  log_info "Current phase: ${phase}"
  echo "current_phase=${phase}" >> "${GITHUB_OUTPUT:-/dev/null}"
  echo "$phase"
}

cmd_set_phase() {
  local issue="${ISSUE_NUMBER:?ISSUE_NUMBER required}"
  local new_phase="${1:?new phase required}"
  local contract_path
  contract_path=$(get_contract_path "$issue")

  ensure_contract_exists "$contract_path"

  local old_phase
  old_phase=$(jq -r '.current_phase' "$contract_path")

  if [[ "$old_phase" == "$new_phase" ]]; then
    log_info "Already in ${new_phase} phase"
    return 0
  fi

  # Update phase with audit entry
  local timestamp
  timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

  local updated_contract
  updated_contract=$(jq --arg phase "$new_phase" \
                        --arg old "$old_phase" \
                        --arg ts "$timestamp" '
    .current_phase = $phase |
    .audit_log += [{
      timestamp: $ts,
      actor: "system",
      role: "system",
      action: "transition",
      field_path: "current_phase",
      old_value: $old,
      new_value: $phase,
      reason: "Phase transition"
    }]
  ' "$contract_path")

  echo "$updated_contract" > "$contract_path"
  log_info "Phase changed from ${old_phase} to ${new_phase}"
}

cmd_summary() {
  local issue="${ISSUE_NUMBER:?ISSUE_NUMBER required}"
  local contract_path
  contract_path=$(get_contract_path "$issue")

  ensure_contract_exists "$contract_path"

  echo "=== Contract Summary for Issue #${issue} ==="
  echo ""

  local phase
  phase=$(jq -r '.current_phase' "$contract_path")
  echo "Current Phase: ${phase}"

  local total_phases
  total_phases=$(jq '.phases | length' "$contract_path")
  echo "Total Phases: ${total_phases}"

  echo ""
  echo "Tasks by Status:"
  jq -r '
    .phases[].tasks[] |
    .status
  ' "$contract_path" | sort | uniq -c | while read -r count status; do
    echo "  ${status}: ${count}"
  done

  echo ""
  local pending_decisions
  pending_decisions=$(jq '[.decisions[] | select(.resolved == false)] | length' "$contract_path")
  echo "Pending Decisions: ${pending_decisions}"

  echo ""
  echo "=== End Summary ==="
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

main() {
  local command="${1:-help}"
  shift || true

  case "$command" in
    load)
      cmd_load "$@"
      ;;
    update-after-implement)
      cmd_update_after_implement "$@"
      ;;
    get-current-phase)
      cmd_get_current_phase "$@"
      ;;
    set-phase)
      cmd_set_phase "$@"
      ;;
    summary)
      cmd_summary "$@"
      ;;
    help|--help|-h)
      echo "Usage: contract-state.sh <command> [options]"
      echo ""
      echo "Commands:"
      echo "  load                    Load and display contract state"
      echo "  update-after-implement  Update contract after implementation phase"
      echo "  get-current-phase       Get the current pipeline phase"
      echo "  set-phase <phase>       Set the current pipeline phase"
      echo "  summary                 Print contract summary"
      echo ""
      echo "Environment variables:"
      echo "  ISSUE_NUMBER  — GitHub issue number (required)"
      echo "  REPO_PATH     — Path to repository (defaults to current directory)"
      ;;
    *)
      log_error "Unknown command: $command"
      echo "Run 'contract-state.sh help' for usage information"
      exit 1
      ;;
  esac
}

main "$@"
