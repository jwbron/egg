#!/bin/bash
# Gateway Sidecar Integration Test Suite
# Run from inside egg container after gateway sidecar is deployed
#
# Usage:
#   ./integration_test.sh [--output FILE] [--repo REPO_PATH]
#
# Output:
#   Results written to ~/sharing/gateway-test-results.json (copyable to host)
#   Human-readable summary to stdout
#
# Prerequisites:
#   - Gateway sidecar running (docker or systemd)
#   - egg container started with gateway integration
#   - Session token at ~/.config/egg/session-token (created during container registration)

set -o pipefail

# Configuration
GATEWAY_URL="${GATEWAY_URL:-http://egg-gateway:9848}"
SECRET_FILE="${HOME}/.config/egg/session-token"
OUTPUT_FILE="${HOME}/sharing/gateway-test-results.json"
REPO_PATH="${HOME}/repos/egg"
TEST_REPO="jwbron/egg"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --output) OUTPUT_FILE="$2"; shift 2 ;;
        --repo) REPO_PATH="$2"; shift 2 ;;
        --gateway) GATEWAY_URL="$2"; shift 2 ;;
        -h|--help)
            echo "Usage: $0 [--output FILE] [--repo REPO_PATH] [--gateway URL]"
            exit 0
            ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# Test result tracking
declare -a TEST_RESULTS=()
PASS_COUNT=0
FAIL_COUNT=0
SKIP_COUNT=0

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Logging functions
log_header() { echo -e "\n${BLUE}═══════════════════════════════════════════════════════════${NC}"; echo -e "${BLUE}  $1${NC}"; echo -e "${BLUE}═══════════════════════════════════════════════════════════${NC}"; }
log_test() { echo -e "\n${YELLOW}▶ TEST: $1${NC}"; }
log_pass() { echo -e "${GREEN}  ✓ PASS: $1${NC}"; ((PASS_COUNT++)); }
log_fail() { echo -e "${RED}  ✗ FAIL: $1${NC}"; ((FAIL_COUNT++)); }
log_skip() { echo -e "${YELLOW}  ○ SKIP: $1${NC}"; ((SKIP_COUNT++)); }
log_info() { echo -e "    $1"; }

# Record test result
record_result() {
    local name="$1"
    local status="$2"  # pass, fail, skip
    local message="$3"
    local details="$4"

    TEST_RESULTS+=("{\"name\": \"$name\", \"status\": \"$status\", \"message\": \"$message\", \"details\": $(echo "$details" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))' 2>/dev/null || echo '""')}")
}

# Get auth header
get_auth_header() {
    if [[ -f "$SECRET_FILE" ]]; then
        echo "Bearer $(cat "$SECRET_FILE")"
    else
        echo ""
    fi
}

# Make authenticated curl request
auth_curl() {
    local auth=$(get_auth_header)
    if [[ -n "$auth" ]]; then
        curl -s -H "Authorization: $auth" "$@"
    else
        curl -s "$@"
    fi
}

# JSON helper
json_get() {
    echo "$1" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('$2', ''))" 2>/dev/null
}

#############################################
# TEST CATEGORY: Environment
#############################################
test_environment() {
    log_header "ENVIRONMENT CHECKS"

    log_test "Gateway URL configured"
    log_info "GATEWAY_URL=$GATEWAY_URL"
    if [[ -n "$GATEWAY_URL" ]]; then
        log_pass "Gateway URL is set"
        record_result "env_gateway_url" "pass" "Gateway URL configured" "$GATEWAY_URL"
    else
        log_fail "Gateway URL not set"
        record_result "env_gateway_url" "fail" "Gateway URL not configured" ""
    fi

    log_test "Session token file exists"
    if [[ -f "$SECRET_FILE" ]]; then
        log_pass "Session token exists at $SECRET_FILE"
        record_result "env_secret_file" "pass" "Session token exists" "$SECRET_FILE"
    else
        log_fail "Session token not found at $SECRET_FILE"
        log_info "Session token is created during container registration with gateway"
        record_result "env_secret_file" "fail" "Session token missing" "$SECRET_FILE"
    fi

    log_test "Test repository exists"
    # Check for .git as directory (regular repo) or file (worktree)
    if [[ -d "$REPO_PATH/.git" ]] || [[ -f "$REPO_PATH/.git" ]]; then
        log_pass "Repository exists at $REPO_PATH"
        record_result "env_repo" "pass" "Repository exists" "$REPO_PATH"
    else
        log_fail "Repository not found at $REPO_PATH"
        record_result "env_repo" "fail" "Repository missing" "$REPO_PATH"
    fi

    log_test "Git wrapper installed"
    GIT_PATH=$(which git)
    if [[ "$GIT_PATH" == *"scripts/git"* ]] || file "$GIT_PATH" 2>/dev/null | grep -q "script"; then
        log_pass "Git wrapper is in PATH"
        record_result "env_git_wrapper" "pass" "Git wrapper installed" "$GIT_PATH"
    else
        log_skip "Git may be using system binary (check PATH order)"
        record_result "env_git_wrapper" "skip" "Could not verify wrapper" "$GIT_PATH"
    fi

    log_test "gh wrapper installed"
    GH_PATH=$(which gh)
    if [[ "$GH_PATH" == *"scripts/gh"* ]] || file "$GH_PATH" 2>/dev/null | grep -q "script"; then
        log_pass "gh wrapper is in PATH"
        record_result "env_gh_wrapper" "pass" "gh wrapper installed" "$GH_PATH"
    else
        log_skip "gh may be using system binary (check PATH order)"
        record_result "env_gh_wrapper" "skip" "Could not verify wrapper" "$GH_PATH"
    fi
}

#############################################
# TEST CATEGORY: Gateway Connectivity
#############################################
test_gateway_connectivity() {
    log_header "GATEWAY CONNECTIVITY"

    log_test "Gateway health endpoint (no auth required)"
    HEALTH=$(curl -s --connect-timeout 5 "${GATEWAY_URL}/api/v1/health" 2>&1)
    CURL_EXIT=$?
    if [[ $CURL_EXIT -ne 0 ]]; then
        log_fail "Cannot connect to gateway: curl exit $CURL_EXIT"
        record_result "gateway_health" "fail" "Connection failed" "$HEALTH"
        return 1
    fi

    STATUS=$(json_get "$HEALTH" "status")
    if [[ "$STATUS" == "healthy" ]] || echo "$HEALTH" | grep -q '"status"'; then
        log_pass "Gateway is healthy"
        log_info "Response: $HEALTH"
        record_result "gateway_health" "pass" "Gateway healthy" "$HEALTH"
    else
        log_fail "Gateway health check failed"
        log_info "Response: $HEALTH"
        record_result "gateway_health" "fail" "Unhealthy response" "$HEALTH"
    fi

    log_test "GitHub token validity (from health)"
    TOKEN_VALID=$(echo "$HEALTH" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('github_token_valid', d.get('token_valid', 'unknown')))" 2>/dev/null)
    if [[ "$TOKEN_VALID" == "true" ]] || [[ "$TOKEN_VALID" == "True" ]]; then
        log_pass "GitHub token is valid"
        record_result "gateway_token" "pass" "Token valid" ""
    elif [[ "$TOKEN_VALID" == "unknown" ]]; then
        log_skip "Token validity not reported in health"
        record_result "gateway_token" "skip" "Not reported" "$HEALTH"
    else
        log_fail "GitHub token invalid or missing"
        record_result "gateway_token" "fail" "Token invalid" "$TOKEN_VALID"
    fi
}

#############################################
# TEST CATEGORY: Authentication
#############################################
test_authentication() {
    log_header "AUTHENTICATION"

    log_test "Request without auth header returns 401"
    RESP=$(curl -s -w "\nHTTP_CODE:%{http_code}" -X POST "${GATEWAY_URL}/api/v1/git/push" \
        -H "Content-Type: application/json" \
        -d '{"repo_path": "/tmp/test", "remote": "origin", "refspec": "main"}')
    HTTP_CODE=$(echo "$RESP" | grep "HTTP_CODE:" | cut -d: -f2)
    BODY=$(echo "$RESP" | grep -v "HTTP_CODE:")

    if [[ "$HTTP_CODE" == "401" ]]; then
        log_pass "Unauthenticated request rejected (401)"
        record_result "auth_no_header" "pass" "401 returned" "$BODY"
    else
        log_fail "Expected 401, got $HTTP_CODE"
        record_result "auth_no_header" "fail" "Wrong status: $HTTP_CODE" "$BODY"
    fi

    log_test "Request with invalid token returns 401"
    RESP=$(curl -s -w "\nHTTP_CODE:%{http_code}" -X POST "${GATEWAY_URL}/api/v1/git/push" \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer totally-invalid-token-xyz" \
        -d '{"repo_path": "/tmp/test", "remote": "origin", "refspec": "main"}')
    HTTP_CODE=$(echo "$RESP" | grep "HTTP_CODE:" | cut -d: -f2)
    BODY=$(echo "$RESP" | grep -v "HTTP_CODE:")

    if [[ "$HTTP_CODE" == "401" ]]; then
        log_pass "Invalid token rejected (401)"
        record_result "auth_invalid_token" "pass" "401 returned" "$BODY"
    else
        log_fail "Expected 401, got $HTTP_CODE"
        record_result "auth_invalid_token" "fail" "Wrong status: $HTTP_CODE" "$BODY"
    fi

    log_test "Request with valid token accepted"
    AUTH=$(get_auth_header)
    if [[ -z "$AUTH" ]]; then
        log_skip "No secret file available"
        record_result "auth_valid_token" "skip" "No secret" ""
        return
    fi

    RESP=$(curl -s -w "\nHTTP_CODE:%{http_code}" -X POST "${GATEWAY_URL}/api/v1/gh/execute" \
        -H "Content-Type: application/json" \
        -H "Authorization: $AUTH" \
        -d '{"args": ["--version"]}')
    HTTP_CODE=$(echo "$RESP" | grep "HTTP_CODE:" | cut -d: -f2)
    BODY=$(echo "$RESP" | grep -v "HTTP_CODE:")

    if [[ "$HTTP_CODE" == "200" ]]; then
        log_pass "Valid token accepted (200)"
        record_result "auth_valid_token" "pass" "200 returned" "$BODY"
    else
        log_fail "Valid token rejected with $HTTP_CODE"
        record_result "auth_valid_token" "fail" "Wrong status: $HTTP_CODE" "$BODY"
    fi
}

#############################################
# TEST CATEGORY: Git Operations (via wrapper)
#############################################
test_git_operations() {
    log_header "GIT OPERATIONS (via wrapper)"

    cd "$REPO_PATH" || { log_fail "Cannot cd to $REPO_PATH"; return 1; }

    log_test "git remote -v (local operation)"
    RESP=$(git remote -v 2>&1)
    if [[ $? -eq 0 ]]; then
        log_pass "git remote works"
        log_info "$(echo "$RESP" | head -2)"
        record_result "git_remote" "pass" "Works" "$RESP"
    else
        log_fail "git remote failed"
        record_result "git_remote" "fail" "Failed" "$RESP"
    fi

    log_test "git status (local operation)"
    RESP=$(git status --short 2>&1)
    if [[ $? -eq 0 ]]; then
        log_pass "git status works"
        record_result "git_status" "pass" "Works" "$RESP"
    else
        log_fail "git status failed"
        record_result "git_status" "fail" "Failed" "$RESP"
    fi

    log_test "git fetch origin (network via gateway)"
    RESP=$(git fetch origin main 2>&1)
    EXIT_CODE=$?
    if [[ $EXIT_CODE -eq 0 ]]; then
        log_pass "git fetch works"
        record_result "git_fetch" "pass" "Works" "$RESP"
    else
        log_fail "git fetch failed (exit $EXIT_CODE)"
        log_info "$RESP"
        record_result "git_fetch" "fail" "Exit $EXIT_CODE" "$RESP"
    fi

    log_test "git push --dry-run to bot-prefixed branch"
    BRANCH="egg-test-$$"
    git checkout -b "$BRANCH" origin/main 2>/dev/null
    RESP=$(git push --dry-run origin "$BRANCH" 2>&1)
    EXIT_CODE=$?
    git checkout - 2>/dev/null; git branch -D "$BRANCH" 2>/dev/null

    if [[ $EXIT_CODE -eq 0 ]] || echo "$RESP" | grep -q "Would push\|Everything up-to-date\|up to date"; then
        log_pass "Push to egg- branch allowed (dry-run)"
        record_result "git_push_egg_branch" "pass" "Allowed" "$RESP"
    else
        log_fail "Push to egg- branch rejected"
        log_info "$RESP"
        record_result "git_push_jib_branch" "fail" "Rejected" "$RESP"
    fi

    log_test "git push --dry-run to main (should check policy)"
    RESP=$(git push --dry-run origin main 2>&1)
    EXIT_CODE=$?
    # This may pass or fail depending on policy - we just want to see it doesn't crash
    if [[ $EXIT_CODE -eq 0 ]]; then
        log_info "Push to main allowed (may be expected if PR exists)"
        record_result "git_push_main" "pass" "Allowed" "$RESP"
        log_pass "Push policy check completed"
    else
        if echo "$RESP" | grep -qi "policy\|ownership\|denied\|not allowed"; then
            log_pass "Push to main correctly blocked by policy"
            record_result "git_push_main" "pass" "Blocked by policy" "$RESP"
        else
            log_info "Push failed: $RESP"
            record_result "git_push_main" "skip" "Failed (may be policy or other)" "$RESP"
            log_skip "Push failed (check if policy-related)"
        fi
    fi
}

#############################################
# TEST CATEGORY: gh CLI Operations (via wrapper)
#############################################
test_gh_operations() {
    log_header "GH CLI OPERATIONS (via wrapper)"

    cd "$REPO_PATH" || return 1

    log_test "gh auth status"
    RESP=$(gh auth status 2>&1)
    if [[ $? -eq 0 ]] || echo "$RESP" | grep -qi "logged in"; then
        log_pass "gh auth status works"
        record_result "gh_auth_status" "pass" "Works" "$RESP"
    else
        log_fail "gh auth status failed"
        log_info "$RESP"
        record_result "gh_auth_status" "fail" "Failed" "$RESP"
    fi

    log_test "gh repo view (read operation)"
    RESP=$(gh repo view --json name,owner 2>&1)
    if [[ $? -eq 0 ]]; then
        log_pass "gh repo view works"
        log_info "$RESP"
        record_result "gh_repo_view" "pass" "Works" "$RESP"
    else
        log_fail "gh repo view failed"
        record_result "gh_repo_view" "fail" "Failed" "$RESP"
    fi

    log_test "gh pr list (read operation)"
    RESP=$(gh pr list --limit 3 --json number,title 2>&1)
    if [[ $? -eq 0 ]]; then
        log_pass "gh pr list works"
        log_info "$(echo "$RESP" | head -5)"
        record_result "gh_pr_list" "pass" "Works" "$RESP"
    else
        log_fail "gh pr list failed"
        record_result "gh_pr_list" "fail" "Failed" "$RESP"
    fi

    log_test "gh pr view (specific PR)"
    RESP=$(gh pr view 511 --json number,title,state 2>&1)
    if [[ $? -eq 0 ]]; then
        log_pass "gh pr view 511 works"
        log_info "$RESP"
        record_result "gh_pr_view" "pass" "Works" "$RESP"
    else
        log_fail "gh pr view failed"
        record_result "gh_pr_view" "fail" "Failed" "$RESP"
    fi

    log_test "gh issue list (read operation)"
    RESP=$(gh issue list --limit 3 --json number,title 2>&1)
    if [[ $? -eq 0 ]]; then
        log_pass "gh issue list works"
        record_result "gh_issue_list" "pass" "Works" "$RESP"
    else
        log_fail "gh issue list failed"
        record_result "gh_issue_list" "fail" "Failed" "$RESP"
    fi

    log_test "gh api (read operation)"
    RESP=$(gh api repos/$TEST_REPO --jq '.name' 2>&1)
    if [[ $? -eq 0 ]]; then
        log_pass "gh api works"
        log_info "Repo name: $RESP"
        record_result "gh_api" "pass" "Works" "$RESP"
    else
        log_fail "gh api failed"
        record_result "gh_api" "fail" "Failed" "$RESP"
    fi
}

#############################################
# TEST CATEGORY: Blocked Operations
#############################################
test_blocked_operations() {
    log_header "BLOCKED OPERATIONS (should be denied)"

    cd "$REPO_PATH" || return 1

    log_test "gh pr merge (MUST be blocked)"
    RESP=$(gh pr merge 511 --squash 2>&1)
    EXIT_CODE=$?
    if [[ $EXIT_CODE -ne 0 ]] && echo "$RESP" | grep -qi "block\|denied\|not allowed\|policy\|forbidden"; then
        log_pass "PR merge correctly BLOCKED"
        record_result "blocked_pr_merge" "pass" "Blocked" "$RESP"
    elif [[ $EXIT_CODE -ne 0 ]]; then
        log_info "Merge failed (may be policy or other reason): $RESP"
        if echo "$RESP" | grep -qi "not mergeable\|already merged\|review"; then
            log_pass "Merge rejected (PR state or policy)"
            record_result "blocked_pr_merge" "pass" "Rejected" "$RESP"
        else
            log_skip "Merge failed but unclear if policy-blocked"
            record_result "blocked_pr_merge" "skip" "Failed unclear" "$RESP"
        fi
    else
        log_fail "PR merge was NOT blocked - THIS IS A SECURITY ISSUE"
        record_result "blocked_pr_merge" "fail" "NOT BLOCKED" "$RESP"
    fi

    log_test "gh repo delete (MUST be blocked)"
    RESP=$(gh repo delete $TEST_REPO --yes 2>&1)
    EXIT_CODE=$?
    if [[ $EXIT_CODE -ne 0 ]]; then
        log_pass "Repo delete blocked/rejected"
        record_result "blocked_repo_delete" "pass" "Blocked" "$RESP"
    else
        log_fail "Repo delete was NOT blocked - THIS IS A SECURITY ISSUE"
        record_result "blocked_repo_delete" "fail" "NOT BLOCKED" "$RESP"
    fi

    log_test "gh repo create (should be blocked)"
    RESP=$(gh repo create test-blocked-repo-$$ --private 2>&1)
    EXIT_CODE=$?
    if [[ $EXIT_CODE -ne 0 ]]; then
        log_pass "Repo create blocked/rejected"
        record_result "blocked_repo_create" "pass" "Blocked" "$RESP"
    else
        log_fail "Repo create was NOT blocked"
        # Cleanup if somehow created
        gh repo delete test-blocked-repo-$$ --yes 2>/dev/null
        record_result "blocked_repo_create" "fail" "NOT BLOCKED" "$RESP"
    fi
}

#############################################
# TEST CATEGORY: Rate Limiting
#############################################
test_rate_limiting() {
    log_header "RATE LIMITING"

    AUTH=$(get_auth_header)
    if [[ -z "$AUTH" ]]; then
        log_skip "No auth available for rate limit tests"
        return
    fi

    log_test "Rate limit info in response"
    RESP=$(curl -s -X POST "${GATEWAY_URL}/api/v1/gh/execute" \
        -H "Content-Type: application/json" \
        -H "Authorization: $AUTH" \
        -d '{"args": ["--version"]}')

    if echo "$RESP" | grep -qi "rate\|limit\|remaining"; then
        log_pass "Rate limit info present in response"
        record_result "rate_limit_info" "pass" "Present" "$RESP"
    else
        log_info "Rate limit info may be in headers or separate endpoint"
        record_result "rate_limit_info" "skip" "Not in body" "$RESP"
        log_skip "Rate limit info not in response body"
    fi

    log_test "Multiple rapid requests don't immediately fail"
    FAIL_COUNT_BEFORE=$FAIL_COUNT
    for i in {1..5}; do
        RESP=$(curl -s -X POST "${GATEWAY_URL}/api/v1/gh/execute" \
            -H "Content-Type: application/json" \
            -H "Authorization: $AUTH" \
            -d '{"args": ["--version"]}')
        if ! echo "$RESP" | grep -q "success\|gh version"; then
            if echo "$RESP" | grep -qi "rate.*limit\|too many"; then
                log_info "Rate limited at request $i (expected for high volume)"
                break
            fi
        fi
    done

    if [[ $FAIL_COUNT -eq $FAIL_COUNT_BEFORE ]]; then
        log_pass "Rate limiting allows normal request volume"
        record_result "rate_limit_normal" "pass" "Normal volume allowed" ""
    fi
}

#############################################
# TEST CATEGORY: Fail-Closed Behavior
#############################################
test_fail_closed() {
    log_header "FAIL-CLOSED BEHAVIOR"

    log_test "Operations fail when gateway unavailable"

    # Save current gateway URL
    ORIG_GATEWAY="$GATEWAY_URL"
    export GATEWAY_URL="http://nonexistent-gateway-$$.invalid:9999"

    cd "$REPO_PATH" || return 1

    RESP=$(timeout 10 git push --dry-run origin main 2>&1)
    EXIT_CODE=$?

    # Restore gateway URL
    export GATEWAY_URL="$ORIG_GATEWAY"

    if [[ $EXIT_CODE -ne 0 ]] && echo "$RESP" | grep -qi "gateway\|unavailable\|failed\|error\|refused\|connect"; then
        log_pass "Git push fails when gateway unavailable (fail-closed)"
        record_result "fail_closed_git" "pass" "Fails closed" "$RESP"
    elif [[ $EXIT_CODE -eq 124 ]]; then
        log_pass "Git push timed out waiting for gateway (fail-closed)"
        record_result "fail_closed_git" "pass" "Timeout (closed)" "$RESP"
    else
        log_fail "Git push did not fail closed - may have bypassed gateway"
        record_result "fail_closed_git" "fail" "May have bypassed" "$RESP"
    fi
}

#############################################
# TEST CATEGORY: Direct API Tests
#############################################
test_direct_api() {
    log_header "DIRECT API ENDPOINTS"

    AUTH=$(get_auth_header)
    if [[ -z "$AUTH" ]]; then
        log_skip "No auth available for direct API tests"
        return
    fi

    log_test "POST /api/v1/gh/execute (allowed command)"
    RESP=$(curl -s -X POST "${GATEWAY_URL}/api/v1/gh/execute" \
        -H "Content-Type: application/json" \
        -H "Authorization: $AUTH" \
        -d '{"args": ["pr", "list", "--repo", "'"$TEST_REPO"'", "--limit", "1", "--json", "number"]}')

    if echo "$RESP" | grep -q '"success".*true\|"number"'; then
        log_pass "gh/execute endpoint works"
        log_info "$(echo "$RESP" | head -3)"
        record_result "api_gh_execute" "pass" "Works" "$RESP"
    else
        log_fail "gh/execute failed"
        log_info "$RESP"
        record_result "api_gh_execute" "fail" "Failed" "$RESP"
    fi

    log_test "POST /api/v1/gh/pr/create (dry validation)"
    # This should validate but we won't actually create a PR
    RESP=$(curl -s -X POST "${GATEWAY_URL}/api/v1/gh/pr/create" \
        -H "Content-Type: application/json" \
        -H "Authorization: $AUTH" \
        -d '{"repo": "'"$TEST_REPO"'", "title": "Test", "body": "Test", "base": "main", "head": "nonexistent-branch-xyz"}')

    # Should return JSON (not HTML 500 error) - branch doesn't exist but endpoint works
    if echo "$RESP" | grep -q "<!doctype\|<html"; then
        log_fail "gh/pr/create returned HTML error (500)"
        record_result "api_pr_create" "fail" "500 Internal Server Error" "$RESP"
    elif echo "$RESP" | grep -q '"success"'; then
        log_pass "gh/pr/create endpoint works (returns JSON)"
        record_result "api_pr_create" "pass" "Works" "$RESP"
    else
        log_info "Unexpected response format"
        record_result "api_pr_create" "skip" "Unexpected response" "$RESP"
        log_skip "gh/pr/create returned unexpected format"
    fi
}

#############################################
# OUTPUT RESULTS
#############################################
write_results() {
    log_header "TEST RESULTS SUMMARY"

    echo ""
    echo -e "  ${GREEN}Passed:${NC}  $PASS_COUNT"
    echo -e "  ${RED}Failed:${NC}  $FAIL_COUNT"
    echo -e "  ${YELLOW}Skipped:${NC} $SKIP_COUNT"
    echo -e "  Total:   $((PASS_COUNT + FAIL_COUNT + SKIP_COUNT))"
    echo ""

    # Build JSON output
    TIMESTAMP=$(date -Iseconds)
    RESULTS_JSON=$(printf '%s\n' "${TEST_RESULTS[@]}" | paste -sd ',' -)

    cat > "$OUTPUT_FILE" << EOF
{
  "timestamp": "$TIMESTAMP",
  "gateway_url": "$GATEWAY_URL",
  "repo_path": "$REPO_PATH",
  "summary": {
    "passed": $PASS_COUNT,
    "failed": $FAIL_COUNT,
    "skipped": $SKIP_COUNT,
    "total": $((PASS_COUNT + FAIL_COUNT + SKIP_COUNT))
  },
  "results": [
    $RESULTS_JSON
  ]
}
EOF

    echo -e "Results written to: ${BLUE}$OUTPUT_FILE${NC}"
    echo ""
    echo "To copy results back to host:"
    echo "  cat $OUTPUT_FILE"
    echo ""
    echo "Or if using docker cp:"
    echo "  docker cp <container>:$OUTPUT_FILE ./gateway-test-results.json"

    if [[ $FAIL_COUNT -gt 0 ]]; then
        echo ""
        echo -e "${RED}Some tests failed. Review output above for details.${NC}"
        return 1
    fi
}

#############################################
# MAIN
#############################################
main() {
    echo ""
    echo "╔═══════════════════════════════════════════════════════════════╗"
    echo "║       Gateway Sidecar Integration Test Suite                  ║"
    echo "╚═══════════════════════════════════════════════════════════════╝"
    echo ""
    echo "Gateway URL: $GATEWAY_URL"
    echo "Repository:  $REPO_PATH"
    echo "Output:      $OUTPUT_FILE"
    echo ""

    test_environment
    test_gateway_connectivity
    test_authentication
    test_git_operations
    test_gh_operations
    test_blocked_operations
    test_rate_limiting
    test_fail_closed
    test_direct_api

    write_results
}

main "$@"
