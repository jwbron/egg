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
echo "EGG_REVIEWER_TYPE=$EGG_REVIEWER_TYPE"
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

# --- Checker role handling ---
# When spawned as a checker, write check results and exit.
if [ "$EGG_AGENT_ROLE" = "checker" ]; then
    echo "Checker role detected — writing check results"
    CHECKS_DIR="${EGG_REPO_PATH:-.}/.egg-state/checks"
    mkdir -p "$CHECKS_DIR"

    # Track autofix attempt count via a state file
    ATTEMPT_FILE="$CHECKS_DIR/.autofix-attempt-count"
    if [ -f "$ATTEMPT_FILE" ]; then
        ATTEMPT_COUNT=$(cat "$ATTEMPT_FILE")
        ATTEMPT_COUNT=$((ATTEMPT_COUNT + 1))
    else
        ATTEMPT_COUNT=1
    fi
    echo "$ATTEMPT_COUNT" > "$ATTEMPT_FILE"

    # Determine check result: MOCK_CHECK_RESULT env var or prompt keywords
    #   "fail" — always fail
    #   "fail-then-pass" — fail on first attempt, pass on subsequent
    #   default — all pass
    # Prompt keywords (checked via EGG_PIPELINE_PROMPT):
    #   CHECK_FAIL_THEN_PASS → fail-then-pass
    #   CHECK_FAIL → fail (must check after CHECK_FAIL_THEN_PASS)
    if [ -z "$MOCK_CHECK_RESULT" ]; then
        case "$EGG_PIPELINE_PROMPT" in
            *CHECK_FAIL_THEN_PASS*) MOCK_CHECK_RESULT="fail-then-pass" ;;
            *CHECK_FAIL*) MOCK_CHECK_RESULT="fail" ;;
        esac
    fi

    if [ "$MOCK_CHECK_RESULT" = "fail" ]; then
        ALL_PASSED="false"
    elif [ "$MOCK_CHECK_RESULT" = "fail-then-pass" ]; then
        if [ "$ATTEMPT_COUNT" -le 1 ]; then
            ALL_PASSED="false"
        else
            ALL_PASSED="true"
        fi
    else
        ALL_PASSED="true"
    fi

    RESULTS_FILE="$CHECKS_DIR/implement-results.json"
    if [ "$ALL_PASSED" = "true" ]; then
        cat > "$RESULTS_FILE" <<CHECK_EOF
{"all_passed":true,"checks":[{"name":"pytest","passed":true,"output":"All tests passed"},{"name":"lint","passed":true,"output":"No lint errors"}]}
CHECK_EOF
    else
        cat > "$RESULTS_FILE" <<CHECK_EOF
{"all_passed":false,"checks":[{"name":"pytest","passed":false,"output":"2 tests failed: test_foo, test_bar"},{"name":"lint","passed":true,"output":"No lint errors"}]}
CHECK_EOF
    fi

    echo "Wrote check results: all_passed=$ALL_PASSED to $RESULTS_FILE (attempt $ATTEMPT_COUNT)"
    sleep ${MOCK_SLEEP:-1}
    exit ${MOCK_EXIT_CODE:-0}
fi

# --- Reviewer role handling ---
# When spawned as a reviewer, write a typed verdict JSON file and exit.
if [ "$EGG_AGENT_ROLE" = "reviewer" ]; then
    echo "Reviewer role detected — writing verdict"
    REVIEWS_DIR="${EGG_REPO_PATH:-.}/.egg-state/reviews"
    mkdir -p "$REVIEWS_DIR"

    # Check for verdict override: env var > prompt keyword > default (approved)
    if [ -n "$MOCK_REVIEW_VERDICT" ]; then
        VERDICT="$MOCK_REVIEW_VERDICT"
    elif echo "$EGG_PIPELINE_PROMPT" | grep -q "REVIEW_NEEDS_REVISION"; then
        VERDICT="needs_revision"
    else
        VERDICT="approved"
    fi
    PHASE="$EGG_PIPELINE_PHASE"
    REVIEWER_TYPE="${EGG_REVIEWER_TYPE:-unified}"

    # Typed verdict file path: {phase}-{reviewer_type}-review.json
    VERDICT_FILE="$REVIEWS_DIR/${PHASE}-${REVIEWER_TYPE}-review.json"

    TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || echo "2025-01-01T00:00:00Z")

    if [ "$VERDICT" = "needs_revision" ]; then
        FEEDBACK="Mock ${REVIEWER_TYPE} reviewer feedback: please revise the ${PHASE} draft."
    else
        FEEDBACK=""
    fi

    cat > "$VERDICT_FILE" <<VERDICT_EOF
{"reviewer":"${REVIEWER_TYPE}","verdict":"${VERDICT}","summary":"Mock ${VERDICT} verdict (${REVIEWER_TYPE})","feedback":"${FEEDBACK}","timestamp":"${TIMESTAMP}"}
VERDICT_EOF

    echo "Wrote verdict: $VERDICT to $VERDICT_FILE (type=$REVIEWER_TYPE)"
    sleep ${MOCK_SLEEP:-1}
    exit ${MOCK_EXIT_CODE:-0}
fi

# --- Worker: write draft files for refine/plan phases ---
DRAFTS_DIR="${EGG_REPO_PATH:-.}/.egg-state/drafts"
case "$EGG_PIPELINE_PHASE" in
    refine)
        mkdir -p "$DRAFTS_DIR"
        cat > "$DRAFTS_DIR/analysis.md" <<DRAFT_EOF
# Analysis Draft (Mock)
Pipeline: $EGG_PIPELINE_ID
This is a mock analysis.
DRAFT_EOF
        echo "Wrote mock analysis draft to $DRAFTS_DIR/analysis.md"
        ;;
    plan)
        mkdir -p "$DRAFTS_DIR"
        cat > "$DRAFTS_DIR/plan.md" <<DRAFT_EOF
# Plan Draft (Mock)
Pipeline: $EGG_PIPELINE_ID
## Task 1: Implement feature
DRAFT_EOF
        echo "Wrote mock plan draft to $DRAFTS_DIR/plan.md"
        ;;
esac

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
