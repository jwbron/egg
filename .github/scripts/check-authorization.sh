#!/usr/bin/env bash
# check-authorization.sh - Reusable authorization checking for GitHub workflows
#
# This script checks if a user is authorized to trigger a workflow.
# It supports:
# - Comma-separated list of authorized usernames
# - Bot self-trigger prevention
# - GitHub organization membership checks (optional)
#
# Required environment variables:
#   SENDER_LOGIN       - GitHub username of the user triggering the workflow
#   AUTHORIZED_USERS   - Comma-separated list of authorized GitHub usernames
#
# Optional environment variables:
#   BOT_USERNAME       - GitHub username of the bot (for self-trigger prevention)
#   GH_TOKEN           - GitHub token for organization membership lookups
#   CHECK_ORG_MEMBERSHIP - Set to "true" to check org membership for authorized orgs
#
# Outputs (via GITHUB_OUTPUT if set, otherwise stdout):
#   authorized=true|false
#   reason=<explanation>

set -euo pipefail

# Get inputs from environment
SENDER_LOGIN="${SENDER_LOGIN:-}"
AUTHORIZED_USERS="${AUTHORIZED_USERS:-}"
BOT_USERNAME="${BOT_USERNAME:-}"
CHECK_ORG_MEMBERSHIP="${CHECK_ORG_MEMBERSHIP:-false}"

# Output helper function
output() {
  local key="$1"
  local value="$2"
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    echo "${key}=${value}" >> "$GITHUB_OUTPUT"
  else
    echo "${key}=${value}"
  fi
}

# Log helper function
log() {
  echo "$1" >&2
}

# Validate required inputs
if [[ -z "$SENDER_LOGIN" ]]; then
  log "Error: SENDER_LOGIN is required"
  output "authorized" "false"
  output "reason" "SENDER_LOGIN is required"
  exit 0
fi

if [[ -z "$AUTHORIZED_USERS" ]]; then
  log "Error: AUTHORIZED_USERS is required"
  output "authorized" "false"
  output "reason" "AUTHORIZED_USERS is required"
  exit 0
fi

# Check for bot self-trigger
if [[ -n "$BOT_USERNAME" ]]; then
  if [[ "$SENDER_LOGIN" == "$BOT_USERNAME" || "$SENDER_LOGIN" == "${BOT_USERNAME}[bot]" ]]; then
    log "Ignoring self-trigger from bot: $SENDER_LOGIN"
    output "authorized" "false"
    output "reason" "Bot self-trigger prevention"
    exit 0
  fi
fi

# Check if sender is in the authorized users list
is_authorized=false
authorized_orgs=()

IFS=',' read -ra entries <<< "$AUTHORIZED_USERS"
for entry in "${entries[@]}"; do
  entry=$(echo "$entry" | xargs)  # trim whitespace

  # Skip empty entries
  [[ -z "$entry" ]] && continue

  # Check if it's an org (prefixed with @) or a user
  if [[ "$entry" == @* ]]; then
    # It's an org - collect for later membership check
    org_name="${entry:1}"  # Remove @ prefix
    authorized_orgs+=("$org_name")
  else
    # It's a username - direct match
    if [[ "$SENDER_LOGIN" == "$entry" ]]; then
      is_authorized=true
      log "User '$SENDER_LOGIN' found in authorized users list"
      break
    fi
  fi
done

# If not directly authorized and we have orgs to check, check org membership
if [[ "$is_authorized" != "true" && ${#authorized_orgs[@]} -gt 0 && "$CHECK_ORG_MEMBERSHIP" == "true" ]]; then
  if [[ -z "${GH_TOKEN:-}" ]]; then
    log "Warning: GH_TOKEN not set, skipping org membership checks"
  else
    for org in "${authorized_orgs[@]}"; do
      log "Checking if '$SENDER_LOGIN' is a member of org '$org'..."

      # Use GitHub API to check membership
      # Returns 204 if member, 404 if not, 302 if public member
      http_code=$(curl -s -o /dev/null -w "%{http_code}" \
        -H "Authorization: Bearer $GH_TOKEN" \
        -H "Accept: application/vnd.github+json" \
        "https://api.github.com/orgs/${org}/members/${SENDER_LOGIN}" 2>/dev/null || echo "000")

      if [[ "$http_code" == "204" || "$http_code" == "302" ]]; then
        is_authorized=true
        log "User '$SENDER_LOGIN' is a member of authorized org '$org'"
        break
      elif [[ "$http_code" == "404" ]]; then
        log "User '$SENDER_LOGIN' is not a member of org '$org'"
      else
        log "Warning: Failed to check org membership for '$org' (HTTP $http_code)"
      fi
    done
  fi
fi

# Output result
if [[ "$is_authorized" == "true" ]]; then
  output "authorized" "true"
  output "reason" "User is authorized"
else
  log "Sender '$SENDER_LOGIN' not in authorized users: $AUTHORIZED_USERS"
  output "authorized" "false"
  output "reason" "User '$SENDER_LOGIN' not in authorized users list"
fi
