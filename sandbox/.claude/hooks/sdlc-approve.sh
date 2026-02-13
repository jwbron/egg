#!/usr/bin/env bash
set -euo pipefail

# SDLC Phase Approval Hook
#
# Intercepts "!approve <phase>" prompts in Claude Code's UserPromptSubmit hook.
# Reads the approval token directly from /dev/tty so Claude never sees it,
# then validates against the orchestrator's token store.
#
# Installed as root-owned (0555) to prevent Claude from modifying it.

# Read hook input from stdin (Claude Code passes JSON with prompt)
INPUT=$(cat)
PROMPT=$(echo "$INPUT" | jq -r '.user_prompt // .prompt // ""' 2>/dev/null || echo "$INPUT")

# Only intercept !approve commands for token-gated phases (refine|plan).
# NOTE: If gated phases change, also update VALID_PHASES in sdlc_tokens.py.
if [[ ! "$PROMPT" =~ ^!approve[[:space:]]+(refine|plan)$ ]]; then
    exit 0  # Pass through all other prompts
fi

PHASE="${BASH_REMATCH[1]}"
PIPELINE_ID=$(cat /tmp/.egg-sdlc-pipeline-id 2>/dev/null || true)

if [[ -z "$PIPELINE_ID" ]]; then
    echo "No SDLC pipeline active." > /dev/tty
    exit 0
fi

# Read token directly from terminal — Claude cannot see this
echo "" > /dev/tty
echo "=== SDLC Phase Approval ===" > /dev/tty
echo "Phase: $PHASE | Pipeline: $PIPELINE_ID" > /dev/tty
echo -n "Enter approval token: " > /dev/tty
read -r TOKEN < /dev/tty

if [[ -z "$TOKEN" ]]; then
    echo "No token entered." > /dev/tty
    exit 0
fi

ORCH_URL="${EGG_ORCHESTRATOR_URL:-http://egg-orchestrator:9849}"
JSON_PAYLOAD=$(jq -n \
    --arg pid "$PIPELINE_ID" \
    --arg phase "$PHASE" \
    --arg token "$TOKEN" \
    '{pipeline_id: $pid, phase: $phase, token: $token}')
RESPONSE=$(curl -s -w "\n%{http_code}" -X POST \
    "$ORCH_URL/api/v1/sdlc-tokens/approve" \
    -H "Content-Type: application/json" \
    -d "$JSON_PAYLOAD")

HTTP_CODE=$(echo "$RESPONSE" | tail -1)
BODY=$(echo "$RESPONSE" | sed '$d')

if [[ "$HTTP_CODE" == "200" ]]; then
    echo "Phase '$PHASE' approved!" > /dev/tty
else
    ERROR=$(echo "$BODY" | jq -r '.message // "Unknown error"' 2>/dev/null || echo "$BODY")
    echo "Approval failed: $ERROR" > /dev/tty
fi

# Allow "!approve <phase>" prompt to reach Claude so it checks pipeline status
exit 0
