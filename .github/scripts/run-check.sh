#!/usr/bin/env bash
# run-check.sh — Framework for running checks in the SDLC work loop
#
# This script provides a unified interface for executing checks defined in
# the phase configuration. It handles:
# - Executing the check script
# - Capturing output and exit codes
# - Implementing retry logic
# - Producing structured JSON results
#
# Usage:
#   run-check.sh --check-id <id> --script <path> [options]
#
# Options:
#   --check-id <id>       Unique identifier for this check (required)
#   --script <path>       Path to the check script to execute (required)
#   --timeout <seconds>   Timeout in seconds (default: 300)
#   --retry <count>       Number of retries on failure (default: 0)
#   --output-file <path>  Path to write JSON result (default: stdout)
#   --working-dir <path>  Working directory for check (default: current)
#   --env <key=value>     Additional environment variable (can be repeated)
#
# Exit codes:
#   0 - Check passed
#   1 - Check failed
#   2 - Check script not found
#   3 - Invalid arguments
#   4 - Timeout
#
# Output (JSON):
#   {
#     "check_id": "check-lint",
#     "status": "passed|failed|skipped",
#     "output": "check output...",
#     "error_message": "error if failed",
#     "duration_seconds": 12.5,
#     "timestamp": "2026-02-09T10:00:00Z",
#     "attempt": 1
#   }

set -euo pipefail

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

CHECK_ID=""
SCRIPT=""
TIMEOUT=300
RETRY_COUNT=0
OUTPUT_FILE=""
WORKING_DIR=""
declare -a EXTRA_ENV=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --check-id)
            CHECK_ID="$2"
            shift 2
            ;;
        --script)
            SCRIPT="$2"
            shift 2
            ;;
        --timeout)
            TIMEOUT="$2"
            shift 2
            ;;
        --retry)
            RETRY_COUNT="$2"
            shift 2
            ;;
        --output-file)
            OUTPUT_FILE="$2"
            shift 2
            ;;
        --working-dir)
            WORKING_DIR="$2"
            shift 2
            ;;
        --env)
            EXTRA_ENV+=("$2")
            shift 2
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 3
            ;;
    esac
done

# Validate required arguments
if [[ -z "$CHECK_ID" ]]; then
    echo "Error: --check-id is required" >&2
    exit 3
fi

if [[ -z "$SCRIPT" ]]; then
    echo "Error: --script is required" >&2
    exit 3
fi

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

# Emit JSON result
emit_result() {
    local status="$1"
    local output="$2"
    local error_message="$3"
    local duration="$4"
    local attempt="$5"
    local timestamp
    timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

    local json
    json=$(jq -n \
        --arg check_id "$CHECK_ID" \
        --arg status "$status" \
        --arg output "$output" \
        --arg error_message "$error_message" \
        --argjson duration "$duration" \
        --arg timestamp "$timestamp" \
        --argjson attempt "$attempt" \
        '{
            check_id: $check_id,
            status: $status,
            output: $output,
            error_message: (if $error_message == "" then null else $error_message end),
            duration_seconds: $duration,
            timestamp: $timestamp,
            attempt: $attempt
        }')

    if [[ -n "$OUTPUT_FILE" ]]; then
        echo "$json" > "$OUTPUT_FILE"
    else
        echo "$json"
    fi
}

# ---------------------------------------------------------------------------
# Main execution
# ---------------------------------------------------------------------------

# Check if script exists
if [[ ! -f "$SCRIPT" ]]; then
    emit_result "failed" "" "Check script not found: $SCRIPT" 0 1
    exit 2
fi

# Make script executable if needed
if [[ ! -x "$SCRIPT" ]]; then
    chmod +x "$SCRIPT"
fi

# Set working directory
if [[ -n "$WORKING_DIR" ]]; then
    cd "$WORKING_DIR"
fi

# Export additional environment variables
if [[ ${#EXTRA_ENV[@]} -gt 0 ]]; then
    for env_var in "${EXTRA_ENV[@]}"; do
        if [[ -n "$env_var" ]]; then
            export "${env_var?}"
        fi
    done
fi

# Run check with retries
attempt=1
max_attempts=$((RETRY_COUNT + 1))

while [[ $attempt -le $max_attempts ]]; do
    start_time=$(date +%s.%N)

    # Create temp file for output
    output_file=$(mktemp)
    trap 'rm -f "$output_file"' EXIT

    # Run the check script with timeout
    set +e
    if command -v timeout &> /dev/null; then
        timeout --signal=TERM --kill-after=10 "$TIMEOUT" bash "$SCRIPT" > "$output_file" 2>&1
        exit_code=$?
    else
        # Fallback for systems without timeout command (macOS)
        bash "$SCRIPT" > "$output_file" 2>&1 &
        pid=$!
        if ! wait "$pid"; then
            exit_code=$?
        else
            exit_code=0
        fi
    fi
    set -e

    end_time=$(date +%s.%N)
    duration=$(echo "$end_time - $start_time" | bc 2>/dev/null || echo "0")

    # Read output
    output=$(cat "$output_file" 2>/dev/null || echo "")

    # Truncate output if too long (keep last 10000 chars)
    if [[ ${#output} -gt 10000 ]]; then
        output="...(truncated)...${output: -10000}"
    fi

    # Handle timeout
    if [[ $exit_code -eq 124 || $exit_code -eq 137 ]]; then
        if [[ $attempt -lt $max_attempts ]]; then
            echo "Check timed out after ${TIMEOUT}s, retrying (attempt $attempt/$max_attempts)..." >&2
            attempt=$((attempt + 1))
            continue
        fi
        emit_result "failed" "$output" "Check timed out after ${TIMEOUT}s" "$duration" "$attempt"
        exit 4
    fi

    # Handle success
    if [[ $exit_code -eq 0 ]]; then
        emit_result "passed" "$output" "" "$duration" "$attempt"
        exit 0
    fi

    # Handle failure with retry
    if [[ $attempt -lt $max_attempts ]]; then
        echo "Check failed (exit code $exit_code), retrying (attempt $attempt/$max_attempts)..." >&2
        attempt=$((attempt + 1))
        sleep 2  # Brief delay before retry
        continue
    fi

    # Final failure
    emit_result "failed" "$output" "Check failed with exit code $exit_code" "$duration" "$attempt"
    exit 1
done
