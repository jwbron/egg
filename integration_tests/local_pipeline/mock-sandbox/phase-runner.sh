#!/bin/sh
# Mock sandbox phase runner for integration tests.
#
# Validates that the orchestrator passes the correct environment and
# volumes to spawned sandbox containers.  Supports failure injection
# via prompt keywords and explicit exit-code override.
#
# Exit codes:
#   0 — success (default)
#   1 — FORCE_FAIL prompt keyword or MOCK_EXIT_CODE=1
#   2 — missing required pipeline env vars
#   3 — missing required sandbox env vars (GATEWAY_URL, etc.)
#   4 — repo volume not mounted

echo "=== Mock Sandbox ==="
echo "EGG_PIPELINE_PHASE=$EGG_PIPELINE_PHASE"
echo "EGG_PIPELINE_ID=$EGG_PIPELINE_ID"
echo "EGG_PIPELINE_MODE=$EGG_PIPELINE_MODE"
echo "EGG_PIPELINE_PROMPT=$EGG_PIPELINE_PROMPT"
echo "EGG_AGENT_ROLE=$EGG_AGENT_ROLE"
echo "EGG_REPO_PATH=$EGG_REPO_PATH"
echo "GATEWAY_URL=$GATEWAY_URL"
echo "RUNTIME_UID=$RUNTIME_UID"
echo "RUNTIME_GID=$RUNTIME_GID"
echo "===================="

# --- Check 1: required pipeline identity vars (exit 2) ---
missing=""
[ -z "$EGG_PIPELINE_PHASE" ] && missing="$missing EGG_PIPELINE_PHASE"
[ -z "$EGG_PIPELINE_ID" ] && missing="$missing EGG_PIPELINE_ID"
[ -z "$EGG_PIPELINE_MODE" ] && missing="$missing EGG_PIPELINE_MODE"

if [ -n "$missing" ]; then
    echo "ERROR: Missing required pipeline env vars:$missing"
    exit 2
fi

# --- Check 2: required sandbox infra vars (exit 3) ---
missing_infra=""
[ -z "$GATEWAY_URL" ] && missing_infra="$missing_infra GATEWAY_URL"

if [ -n "$missing_infra" ]; then
    echo "ERROR: Missing required sandbox env vars:$missing_infra"
    exit 3
fi

# --- Check 3: repo volume mounted (exit 4) ---
if [ ! -d "$EGG_REPO_PATH" ] && [ ! -d "/home/egg/repos" ]; then
    echo "ERROR: Repo volume not mounted at $EGG_REPO_PATH or /home/egg/repos"
    exit 4
fi
echo "Repo volume OK: $(ls -d ${EGG_REPO_PATH:-/home/egg/repos} 2>/dev/null)"

# --- Failure injection ---
# FORCE_FAIL in prompt → exit 1 (tests real container failure path)
case "$EGG_PIPELINE_PROMPT" in
    *FORCE_FAIL*)
        echo "FORCE_FAIL detected in prompt — exiting with code 1"
        exit 1
        ;;
esac

# Allow explicit exit code override via env
sleep ${MOCK_SLEEP:-1}
exit ${MOCK_EXIT_CODE:-0}
