#!/usr/bin/env bash
# test-check-authorization.sh - Unit tests for check-authorization.sh
#
# Run with: bash .github/scripts/test-check-authorization.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-authorization.sh"

# Test counters
TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m' # No Color

# Helper to run the script with given env vars and capture output
run_check() {
  local output_file
  output_file=$(mktemp)

  # Run the script with GITHUB_OUTPUT set to capture outputs
  GITHUB_OUTPUT="$output_file" "$CHECK_SCRIPT" 2>/dev/null || true

  cat "$output_file"
  rm -f "$output_file"
}

# Test assertion helper
assert_output_contains() {
  local output="$1"
  local expected="$2"
  local test_name="$3"

  TESTS_RUN=$((TESTS_RUN + 1))

  if echo "$output" | grep -q "$expected"; then
    echo -e "${GREEN}PASS${NC}: $test_name"
    TESTS_PASSED=$((TESTS_PASSED + 1))
  else
    echo -e "${RED}FAIL${NC}: $test_name"
    echo "  Expected output to contain: $expected"
    echo "  Actual output: $output"
    TESTS_FAILED=$((TESTS_FAILED + 1))
  fi
}

echo "Running check-authorization.sh tests..."
echo "========================================"
echo ""

# Test 1: Authorized user (single user in list)
echo "Test 1: Authorized user (single user in list)"
output=$(SENDER_LOGIN="jwbron" AUTHORIZED_USERS="jwbron" run_check)
assert_output_contains "$output" "authorized=true" "Single authorized user returns true"
echo ""

# Test 2: Unauthorized user
echo "Test 2: Unauthorized user"
output=$(SENDER_LOGIN="malicious-actor" AUTHORIZED_USERS="jwbron" run_check)
assert_output_contains "$output" "authorized=false" "Unauthorized user returns false"
echo ""

# Test 3: Multiple authorized users
echo "Test 3: Multiple authorized users"
output=$(SENDER_LOGIN="team-member" AUTHORIZED_USERS="jwbron, team-member, other-dev" run_check)
assert_output_contains "$output" "authorized=true" "User in multi-user list returns true"
echo ""

# Test 4: User not in multi-user list
echo "Test 4: User not in multi-user list"
output=$(SENDER_LOGIN="outsider" AUTHORIZED_USERS="jwbron, team-member, other-dev" run_check)
assert_output_contains "$output" "authorized=false" "User not in multi-user list returns false"
echo ""

# Test 5: Bot self-trigger prevention (exact match)
echo "Test 5: Bot self-trigger prevention (exact match)"
output=$(SENDER_LOGIN="egg" AUTHORIZED_USERS="jwbron, egg" BOT_USERNAME="egg" run_check)
assert_output_contains "$output" "authorized=false" "Bot exact match is blocked"
assert_output_contains "$output" "reason=Bot self-trigger prevention" "Bot blocked with correct reason"
echo ""

# Test 6: Bot self-trigger prevention (with [bot] suffix)
echo "Test 6: Bot self-trigger prevention (with [bot] suffix)"
output=$(SENDER_LOGIN="egg[bot]" AUTHORIZED_USERS="jwbron" BOT_USERNAME="egg" run_check)
assert_output_contains "$output" "authorized=false" "Bot[bot] suffix is blocked"
echo ""

# Test 7: Missing SENDER_LOGIN
echo "Test 7: Missing SENDER_LOGIN"
output=$(SENDER_LOGIN="" AUTHORIZED_USERS="jwbron" run_check)
assert_output_contains "$output" "authorized=false" "Missing SENDER_LOGIN returns false"
assert_output_contains "$output" "reason=SENDER_LOGIN is required" "Correct error message for missing sender"
echo ""

# Test 8: Missing AUTHORIZED_USERS
echo "Test 8: Missing AUTHORIZED_USERS"
output=$(SENDER_LOGIN="jwbron" AUTHORIZED_USERS="" run_check)
assert_output_contains "$output" "authorized=false" "Missing AUTHORIZED_USERS returns false"
assert_output_contains "$output" "reason=AUTHORIZED_USERS is required" "Correct error message for missing authorized users"
echo ""

# Test 9: Whitespace handling in user list
echo "Test 9: Whitespace handling in user list"
output=$(SENDER_LOGIN="alice" AUTHORIZED_USERS="  alice  ,  bob  ,  charlie  " run_check)
assert_output_contains "$output" "authorized=true" "Whitespace trimmed from user list"
echo ""

# Test 10: Org prefix stored (without API call since no GH_TOKEN)
echo "Test 10: Org authorization format accepted"
output=$(SENDER_LOGIN="org-member" AUTHORIZED_USERS="@myorg, jwbron" CHECK_ORG_MEMBERSHIP="false" run_check)
# Without org membership check enabled, user should not be authorized by org alone
assert_output_contains "$output" "authorized=false" "Org format accepted but membership not checked without flag"
echo ""

# Test 11: Mixed users and orgs - user match takes precedence
echo "Test 11: Mixed users and orgs - user match takes precedence"
output=$(SENDER_LOGIN="jwbron" AUTHORIZED_USERS="@myorg, jwbron" run_check)
assert_output_contains "$output" "authorized=true" "Direct user match works with mixed list"
echo ""

# Test 12: Case sensitivity
echo "Test 12: Case sensitivity"
output=$(SENDER_LOGIN="JWbron" AUTHORIZED_USERS="jwbron" run_check)
# GitHub usernames are case-sensitive in our implementation
assert_output_contains "$output" "authorized=false" "Username comparison is case-sensitive"
echo ""

# Test 13: Empty entries in list
echo "Test 13: Empty entries in list"
output=$(SENDER_LOGIN="alice" AUTHORIZED_USERS=",,alice,," run_check)
assert_output_contains "$output" "authorized=true" "Empty entries in list are skipped"
echo ""

# Summary
echo "========================================"
echo "Test Results: $TESTS_PASSED/$TESTS_RUN passed"
if [[ $TESTS_FAILED -gt 0 ]]; then
  echo -e "${RED}$TESTS_FAILED test(s) failed${NC}"
  exit 1
else
  echo -e "${GREEN}All tests passed!${NC}"
  exit 0
fi
