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
#   check-review-status    Check review results and determine next action
#   increment-cycle   Increment the pipeline cycle counter
#   get-current-phase Get the current pipeline phase
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

cmd_check_review_status() {
  local issue="${ISSUE_NUMBER:?ISSUE_NUMBER required}"
  local contract_path
  contract_path=$(get_contract_path "$issue")

  ensure_contract_exists "$contract_path"

  # Count tasks by status
  local complete_tasks incomplete_tasks blocked_tasks total_tasks
  complete_tasks=$(jq '[.phases[].tasks[] | select(.status == "complete")] | length' "$contract_path")
  incomplete_tasks=$(jq '[.phases[].tasks[] | select(.status == "incomplete")] | length' "$contract_path")
  blocked_tasks=$(jq '[.phases[].tasks[] | select(.status == "blocked")] | length' "$contract_path")
  total_tasks=$(jq '[.phases[].tasks[]] | length' "$contract_path")

  log_info "Review status: ${complete_tasks}/${total_tasks} complete, ${incomplete_tasks} incomplete, ${blocked_tasks} blocked"

  # Check if any tasks are escalated
  local escalated_tasks
  escalated_tasks=$(jq '[.phases[].tasks[] | select(.escalated == true)] | length' "$contract_path")

  if [[ "$escalated_tasks" -gt 0 ]]; then
    log_info "Found ${escalated_tasks} escalated tasks"
    echo "passed=false" >> "${GITHUB_OUTPUT:-/dev/null}"
    echo "tasks_incomplete=${incomplete_tasks}" >> "${GITHUB_OUTPUT:-/dev/null}"
    echo "escalated=true" >> "${GITHUB_OUTPUT:-/dev/null}"
    return 0
  fi

  # Check if all tasks are complete
  if [[ "$complete_tasks" -eq "$total_tasks" && "$total_tasks" -gt 0 ]]; then
    log_info "All tasks passed review"
    echo "passed=true" >> "${GITHUB_OUTPUT:-/dev/null}"
    echo "tasks_incomplete=0" >> "${GITHUB_OUTPUT:-/dev/null}"
    echo "escalated=false" >> "${GITHUB_OUTPUT:-/dev/null}"
  else
    log_info "Review found incomplete tasks"
    echo "passed=false" >> "${GITHUB_OUTPUT:-/dev/null}"
    echo "tasks_incomplete=${incomplete_tasks}" >> "${GITHUB_OUTPUT:-/dev/null}"
    echo "escalated=false" >> "${GITHUB_OUTPUT:-/dev/null}"

    # Check per-task cycle counts and escalate if needed
    local tasks_needing_escalation
    tasks_needing_escalation=$(jq '[.phases[].tasks[] | select(.review_cycles >= .max_cycles and .status != "complete")] | length' "$contract_path")

    if [[ "$tasks_needing_escalation" -gt 0 ]]; then
      log_info "Found ${tasks_needing_escalation} tasks exceeding max cycles - escalating"

      # Mark tasks as escalated
      local updated_contract
      updated_contract=$(jq '
        .phases |= map(
          .tasks |= map(
            if .review_cycles >= .max_cycles and .status != "complete" then
              .escalated = true
            else
              .
            end
          )
        )
      ' "$contract_path")

      echo "$updated_contract" > "$contract_path"
      echo "escalated=true" >> "${GITHUB_OUTPUT:-/dev/null}"
    fi

    # Increment review cycles for incomplete tasks
    local updated_contract
    updated_contract=$(jq '
      .phases |= map(
        .tasks |= map(
          if .status == "incomplete" then
            .review_cycles = (.review_cycles + 1) |
            .status = "pending"
          else
            .
          end
        )
      )
    ' "$contract_path")

    echo "$updated_contract" > "$contract_path"
  fi
}

cmd_increment_cycle() {
  local issue="${ISSUE_NUMBER:?ISSUE_NUMBER required}"
  local contract_path
  contract_path=$(get_contract_path "$issue")

  ensure_contract_exists "$contract_path"

  # Increment total cycles
  local updated_contract
  updated_contract=$(jq '.circuit_breaker.total_cycles += 1' "$contract_path")
  echo "$updated_contract" > "$contract_path"

  local new_count
  new_count=$(jq '.circuit_breaker.total_cycles' "$contract_path")
  log_info "Pipeline cycle incremented to ${new_count}"

  echo "cycle_count=${new_count}" >> "${GITHUB_OUTPUT:-/dev/null}"
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

cmd_check_circuit_breaker() {
  local issue="${ISSUE_NUMBER:?ISSUE_NUMBER required}"
  local contract_path
  contract_path=$(get_contract_path "$issue")

  ensure_contract_exists "$contract_path"

  local total_cycles max_cycles status
  total_cycles=$(jq '.circuit_breaker.total_cycles' "$contract_path")
  max_cycles=$(jq '.circuit_breaker.max_total_cycles' "$contract_path")
  status=$(jq -r '.circuit_breaker.status' "$contract_path")

  log_info "Circuit breaker: ${status} (${total_cycles}/${max_cycles} cycles)"

  echo "status=${status}" >> "${GITHUB_OUTPUT:-/dev/null}"
  echo "total_cycles=${total_cycles}" >> "${GITHUB_OUTPUT:-/dev/null}"
  echo "max_cycles=${max_cycles}" >> "${GITHUB_OUTPUT:-/dev/null}"

  if [[ "$status" == "open" ]]; then
    echo "tripped=true" >> "${GITHUB_OUTPUT:-/dev/null}"
    return 1
  elif [[ "$total_cycles" -ge "$max_cycles" ]]; then
    log_info "Circuit breaker tripped: max cycles exceeded"
    echo "tripped=true" >> "${GITHUB_OUTPUT:-/dev/null}"

    # Open the circuit breaker
    local updated_contract
    updated_contract=$(jq '.circuit_breaker.status = "open"' "$contract_path")
    echo "$updated_contract" > "$contract_path"
    return 1
  else
    echo "tripped=false" >> "${GITHUB_OUTPUT:-/dev/null}"
    return 0
  fi
}

cmd_open_circuit_breaker() {
  local issue="${ISSUE_NUMBER:?ISSUE_NUMBER required}"
  local reason="${1:-Manual trigger}"
  local contract_path
  contract_path=$(get_contract_path "$issue")

  ensure_contract_exists "$contract_path"

  local timestamp
  timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

  local updated_contract
  updated_contract=$(jq --arg ts "$timestamp" --arg reason "$reason" '
    .circuit_breaker.status = "open" |
    .audit_log += [{
      timestamp: $ts,
      actor: "system",
      role: "system",
      action: "update",
      field_path: "circuit_breaker.status",
      old_value: "closed",
      new_value: "open",
      reason: $reason
    }]
  ' "$contract_path")

  echo "$updated_contract" > "$contract_path"
  log_info "Circuit breaker opened: ${reason}"
}

cmd_close_circuit_breaker() {
  local issue="${ISSUE_NUMBER:?ISSUE_NUMBER required}"
  local contract_path
  contract_path=$(get_contract_path "$issue")

  ensure_contract_exists "$contract_path"

  local timestamp
  timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

  local updated_contract
  updated_contract=$(jq --arg ts "$timestamp" '
    .circuit_breaker.status = "closed" |
    .audit_log += [{
      timestamp: $ts,
      actor: "human",
      role: "human",
      action: "update",
      field_path: "circuit_breaker.status",
      old_value: "open",
      new_value: "closed",
      reason: "Human intervention"
    }]
  ' "$contract_path")

  echo "$updated_contract" > "$contract_path"
  log_info "Circuit breaker closed by human intervention"
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
  local cb_status cb_cycles cb_max
  cb_status=$(jq -r '.circuit_breaker.status' "$contract_path")
  cb_cycles=$(jq '.circuit_breaker.total_cycles' "$contract_path")
  cb_max=$(jq '.circuit_breaker.max_total_cycles' "$contract_path")
  echo "Circuit Breaker: ${cb_status} (${cb_cycles}/${cb_max} cycles)"

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
    check-review-status)
      cmd_check_review_status "$@"
      ;;
    increment-cycle)
      cmd_increment_cycle "$@"
      ;;
    get-current-phase)
      cmd_get_current_phase "$@"
      ;;
    set-phase)
      cmd_set_phase "$@"
      ;;
    check-circuit-breaker)
      cmd_check_circuit_breaker "$@"
      ;;
    open-circuit-breaker)
      cmd_open_circuit_breaker "$@"
      ;;
    close-circuit-breaker)
      cmd_close_circuit_breaker "$@"
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
      echo "  check-review-status     Check review results and determine next action"
      echo "  increment-cycle         Increment the pipeline cycle counter"
      echo "  get-current-phase       Get the current pipeline phase"
      echo "  set-phase <phase>       Set the current pipeline phase"
      echo "  check-circuit-breaker   Check circuit breaker status"
      echo "  open-circuit-breaker    Open the circuit breaker"
      echo "  close-circuit-breaker   Close the circuit breaker"
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
