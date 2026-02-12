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
#
# Prompt keywords:
#   FORCE_FAIL — exit immediately with code 1
#   CHECK_FAIL — checker always fails
#   CHECK_FAIL_THEN_PASS — checker fails first, passes on retry
#   REVIEW_NEEDS_REVISION — all reviewers return needs_revision
#   SLOW_PHASE — sleep for SLOW_PHASE_DURATION seconds (default 30)
#   FAIL_ON_PHASE=<phase> — exit code 1 only when EGG_PIPELINE_PHASE matches
#   REVIEWER_MIXED_VERDICT — first reviewer approves, second needs_revision
#   HEARTBEAT_ONLY — send heartbeats but never exit (for timeout tests)
#   PARTIAL_FAILURE — write partial draft then exit code 1

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

# --- Check 4: worktree validity (exit 5) ---
# Verify the mounted repo is a valid git worktree (has .git file with gitdir pointer)
# This catches issues like empty worktrees or root-owned worktrees that Docker may create
REPO_PATH="${EGG_REPO_PATH:-/home/egg/repos}"
GIT_PATH="$REPO_PATH/.git"

if [ -f "$GIT_PATH" ]; then
    # .git is a file - check it contains gitdir pointer (valid worktree)
    if grep -q "gitdir:" "$GIT_PATH" 2>/dev/null; then
        echo "Worktree OK: .git file contains gitdir pointer"
    else
        echo "ERROR: .git file exists but does not contain gitdir pointer"
        exit 5
    fi
elif [ -d "$GIT_PATH" ]; then
    # .git is a directory - could be a regular repo or empty dir from Docker
    if [ -f "$GIT_PATH/HEAD" ]; then
        echo "Git repo OK: .git directory with HEAD (not a worktree, but valid)"
    else
        echo "ERROR: .git directory is empty or invalid (no HEAD file)"
        exit 5
    fi
else
    # No .git at all - this is expected if the repo mount is working normally
    # The gateway mounts the worktree at the repo path
    echo "NOTE: No .git found at $GIT_PATH (expected when gateway mounts worktree at repo path)"
fi

# Report worktree status for debugging
echo "Worktree mount status:"
echo "  - Path: $REPO_PATH"
echo "  - Owner: $(stat -c '%u:%g' "$REPO_PATH" 2>/dev/null || echo 'unknown')"
echo "  - Files: $(ls -A "$REPO_PATH" 2>/dev/null | head -5 | tr '\n' ' ')"

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

    PHASE="$EGG_PIPELINE_PHASE"
    REVIEWER_TYPE="${EGG_REVIEWER_TYPE:-unified}"

    # Check for verdict override: env var > prompt keyword > default (approved)
    # REVIEWER_MIXED_VERDICT: first reviewer (unified) approves, subsequent reject
    if [ -n "$MOCK_REVIEW_VERDICT" ]; then
        VERDICT="$MOCK_REVIEW_VERDICT"
    elif echo "$EGG_PIPELINE_PROMPT" | grep -q "REVIEWER_MIXED_VERDICT"; then
        # First reviewer type (unified) approves, others need revision
        if [ "$REVIEWER_TYPE" = "unified" ]; then
            VERDICT="approved"
            echo "REVIEWER_MIXED_VERDICT: unified reviewer approves"
        else
            VERDICT="needs_revision"
            echo "REVIEWER_MIXED_VERDICT: ${REVIEWER_TYPE} reviewer requests revision"
        fi
    elif echo "$EGG_PIPELINE_PROMPT" | grep -q "REVIEW_NEEDS_REVISION"; then
        VERDICT="needs_revision"
    else
        VERDICT="approved"
    fi

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

# SLOW_PHASE: sleep for configurable duration (for timeout testing)
case "$EGG_PIPELINE_PROMPT" in
    *SLOW_PHASE*)
        SLOW_DURATION="${SLOW_PHASE_DURATION:-30}"
        echo "SLOW_PHASE detected — sleeping for ${SLOW_DURATION}s"
        sleep "$SLOW_DURATION"
        ;;
esac

# FAIL_ON_PHASE=<phase>: fail only when current phase matches
# Use case statement matching to avoid shell injection via sed
FAIL_PHASE=""
case "$EGG_PIPELINE_PROMPT" in
    *FAIL_ON_PHASE=refine*) FAIL_PHASE="refine" ;;
    *FAIL_ON_PHASE=plan*) FAIL_PHASE="plan" ;;
    *FAIL_ON_PHASE=implement*) FAIL_PHASE="implement" ;;
    *FAIL_ON_PHASE=review*) FAIL_PHASE="review" ;;
    *FAIL_ON_PHASE=pr*) FAIL_PHASE="pr" ;;
esac

if [ -n "$FAIL_PHASE" ]; then
    if [ "$EGG_PIPELINE_PHASE" = "$FAIL_PHASE" ]; then
        echo "FAIL_ON_PHASE=$FAIL_PHASE matched current phase — exiting with code 1"
        exit 1
    fi
    echo "FAIL_ON_PHASE=$FAIL_PHASE does not match current phase ($EGG_PIPELINE_PHASE) — continuing"
fi

# PARTIAL_FAILURE: write partial draft then fail
case "$EGG_PIPELINE_PROMPT" in
    *PARTIAL_FAILURE*)
        DRAFTS_DIR="${EGG_REPO_PATH:-.}/.egg-state/drafts"
        mkdir -p "$DRAFTS_DIR"
        cat > "$DRAFTS_DIR/partial-draft.md" <<PARTIAL_EOF
# Partial Draft (Incomplete)
Pipeline: $EGG_PIPELINE_ID
Phase: $EGG_PIPELINE_PHASE
This draft is intentionally incomplete — simulating mid-phase crash.
PARTIAL_EOF
        echo "PARTIAL_FAILURE: wrote partial draft to $DRAFTS_DIR/partial-draft.md"
        echo "PARTIAL_FAILURE: simulating crash mid-execution"
        exit 1
        ;;
esac

# HEARTBEAT_ONLY: send heartbeats forever (for timeout testing)
# This mode never exits — the container must be killed by timeout
case "$EGG_PIPELINE_PROMPT" in
    *HEARTBEAT_ONLY*)
        echo "HEARTBEAT_ONLY mode — sending heartbeats until killed"
        HEARTBEAT_INTERVAL="${HEARTBEAT_INTERVAL:-5}"
        SIGNALS_DIR="${EGG_REPO_PATH:-.}/.egg-state/signals"
        mkdir -p "$SIGNALS_DIR"
        HEARTBEAT_COUNT=0
        while true; do
            HEARTBEAT_COUNT=$((HEARTBEAT_COUNT + 1))
            TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || echo "2025-01-01T00:00:00Z")
            cat > "$SIGNALS_DIR/heartbeat-${HEARTBEAT_COUNT}.json" <<HEARTBEAT_EOF
{"type":"heartbeat","pipeline_id":"$EGG_PIPELINE_ID","phase":"$EGG_PIPELINE_PHASE","count":$HEARTBEAT_COUNT,"timestamp":"$TIMESTAMP"}
HEARTBEAT_EOF
            echo "Heartbeat #${HEARTBEAT_COUNT} sent"
            sleep "$HEARTBEAT_INTERVAL"
        done
        ;;
esac

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
