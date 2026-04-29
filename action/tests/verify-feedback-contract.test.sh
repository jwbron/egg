#!/usr/bin/env bash
# Fixture-driven test for action/verify-feedback-contract.sh.
# Synthesizes comment JSON and stub issue lookups, runs the verifier, and
# asserts the violations file matches expectations.

set -euo pipefail

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ACTION_DIR="$(cd "${THIS_DIR}/.." && pwd)"
SCRIPT="${ACTION_DIR}/verify-feedback-contract.sh"

if [[ ! -x "$SCRIPT" ]]; then
  echo "FAIL: ${SCRIPT} not found or not executable" >&2
  exit 1
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

PASSES=0
FAILURES=0

# Build a JSON array containing one comment per body argument.
make_comments() {
  local out_file="$1"
  shift
  local n=$#
  if (( n == 0 )); then
    echo "[]" > "$out_file"
    return
  fi
  python3 - "$out_file" "$@" <<'PY'
import json, sys
out = sys.argv[1]
bodies = sys.argv[2:]
json.dump([{"body": b} for b in bodies], open(out, "w"))
PY
}

# Build a stub issue-lookup script for a fixed mapping.
# Args alternate: number json_path_or_empty
make_lookup() {
  local stub_file="$1"
  shift
  : > "$stub_file"
  echo "#!/usr/bin/env bash" >> "$stub_file"
  echo 'case "$1" in' >> "$stub_file"
  while (( "$#" >= 2 )); do
    local n="$1"
    local j="$2"
    shift 2
    if [[ -z "$j" ]]; then
      echo "  ${n}) exit 0 ;;" >> "$stub_file"
    else
      echo "  ${n}) cat <<'JSON'" >> "$stub_file"
      cat "$j" >> "$stub_file"
      echo "JSON" >> "$stub_file"
      echo "  ;;" >> "$stub_file"
    fi
  done
  echo '  *) exit 0 ;;' >> "$stub_file"
  echo 'esac' >> "$stub_file"
  chmod +x "$stub_file"
}

# Run the verifier with the given comments + stub lookup + run-start.
# Echoes the exit code and writes violations to $TMP/violations.txt.
run_verifier() {
  local comments="$1" stub="$2" run_start="$3"
  local violations="${TMP}/violations.txt"
  : > "$violations"
  local rc=0
  COMMENTS_JSON_FILE="$comments" \
  VIOLATIONS_FILE="$violations" \
  RUN_START="$run_start" \
  REPO="example/repo" \
  ISSUE_LOOKUP_SCRIPT="$stub" \
    "$SCRIPT" >/dev/null 2>&1 || rc=$?
  echo "$rc"
}

assert_pass() {
  local name="$1" rc="$2" violations_file="$3"
  if [[ "$rc" -eq 0 ]] && [[ ! -s "$violations_file" ]]; then
    echo "PASS: $name"
    PASSES=$((PASSES + 1))
  else
    echo "FAIL: $name — rc=$rc, violations:"
    cat "$violations_file"
    FAILURES=$((FAILURES + 1))
  fi
}

assert_violation_contains() {
  local name="$1" rc="$2" violations_file="$3" needle="$4"
  if [[ "$rc" -ne 0 ]] && grep -qF -- "$needle" "$violations_file"; then
    echo "PASS: $name"
    PASSES=$((PASSES + 1))
  else
    echo "FAIL: $name — rc=$rc, expected substring '$needle', got:"
    cat "$violations_file"
    FAILURES=$((FAILURES + 1))
  fi
}

RUN_START="2026-04-29T12:00:00Z"
BEFORE="2026-04-29T11:00:00Z"   # 1h before run start — predates threshold
AFTER="2026-04-29T12:30:00Z"    # 30m after run start
JUST_BEFORE="2026-04-29T11:59:30Z"  # 30s before run start — within 60s grace

issue_in_run() {
  local f="${TMP}/issue-${1}.json"
  echo "{\"number\": $1, \"created_at\": \"$AFTER\", \"pull_request\": null}" > "$f"
  echo "$f"
}
issue_before() {
  local f="${TMP}/issue-${1}.json"
  echo "{\"number\": $1, \"created_at\": \"$BEFORE\", \"pull_request\": null}" > "$f"
  echo "$f"
}
issue_grace() {
  local f="${TMP}/issue-${1}.json"
  echo "{\"number\": $1, \"created_at\": \"$JUST_BEFORE\", \"pull_request\": null}" > "$f"
  echo "$f"
}
pr_object() {
  local f="${TMP}/pr-${1}.json"
  echo "{\"number\": $1, \"created_at\": \"$AFTER\", \"pull_request\": {\"url\": \"x\"}}" > "$f"
  echo "$f"
}

# === Test 1: clean response with valid in-run deferred-to passes ===
make_comments "${TMP}/c1.json" \
  "Item 1: fixed-in-PR (commit abc123)
Item 2: deferred-to #5001
Item 3: disagree (style preference)"
i5001=$(issue_in_run 5001)
make_lookup "${TMP}/stub1.sh" 5001 "$i5001"
rc=$(run_verifier "${TMP}/c1.json" "${TMP}/stub1.sh" "$RUN_START")
assert_pass "clean response with valid disposition tags" "$rc" "${TMP}/violations.txt"

# === Test 2: phantom 'will file a follow-up' is flagged ===
make_comments "${TMP}/c2.json" "Will file a follow-up for the regex issue."
make_lookup "${TMP}/stub2.sh"
rc=$(run_verifier "${TMP}/c2.json" "${TMP}/stub2.sh" "$RUN_START")
assert_violation_contains "phantom 'file a follow-up'" "$rc" "${TMP}/violations.txt" "forbidden phrase"

# === Test 3: 'will file' next to #NNNN passes (legitimate citation) ===
make_comments "${TMP}/c3.json" \
  "I will file the issue #5002 (already filed above as deferred-to #5002)."
i5002=$(issue_in_run 5002)
make_lookup "${TMP}/stub3.sh" 5002 "$i5002"
rc=$(run_verifier "${TMP}/c3.json" "${TMP}/stub3.sh" "$RUN_START")
assert_pass "'will file' next to #NNNN treated as citation" "$rc" "${TMP}/violations.txt"

# === Test 4: 'will open an issue' alone (no #NNNN) is flagged ===
make_comments "${TMP}/c4.json" \
  "If this becomes a problem we will open an issue separately."
make_lookup "${TMP}/stub4.sh"
rc=$(run_verifier "${TMP}/c4.json" "${TMP}/stub4.sh" "$RUN_START")
assert_violation_contains "'will open an issue' without #NNNN" "$rc" "${TMP}/violations.txt" "forbidden phrase"

# === Test 5: forbidden phrase inside fenced code block is ignored ===
make_comments "${TMP}/c5.json" "$(printf '%s\n' \
  'See the contract:' \
  '```' \
  'forbidden: will file a follow-up' \
  '```' \
  'Item 1: fixed-in-PR (commit abc123)')"
make_lookup "${TMP}/stub5.sh"
rc=$(run_verifier "${TMP}/c5.json" "${TMP}/stub5.sh" "$RUN_START")
assert_pass "forbidden phrase inside fenced code block ignored" "$rc" "${TMP}/violations.txt"

# === Test 6: forbidden phrase inside inline backticks is ignored ===
make_comments "${TMP}/c6.json" \
  'Quoting the contract: forbidden phrases include `will file a follow-up` and others.'
make_lookup "${TMP}/stub6.sh"
rc=$(run_verifier "${TMP}/c6.json" "${TMP}/stub6.sh" "$RUN_START")
assert_pass "forbidden phrase inside inline backticks ignored" "$rc" "${TMP}/violations.txt"

# === Test 7: deferred-to #NNNN that does not exist is flagged ===
make_comments "${TMP}/c7.json" "Item: deferred-to #9999"
make_lookup "${TMP}/stub7.sh"  # empty mapping
rc=$(run_verifier "${TMP}/c7.json" "${TMP}/stub7.sh" "$RUN_START")
assert_violation_contains "non-existent issue" "$rc" "${TMP}/violations.txt" "issue does not exist"

# === Test 8: deferred-to predating run is flagged ===
make_comments "${TMP}/c8.json" "Item: deferred-to #4000"
i4000=$(issue_before 4000)
make_lookup "${TMP}/stub8.sh" 4000 "$i4000"
rc=$(run_verifier "${TMP}/c8.json" "${TMP}/stub8.sh" "$RUN_START")
assert_violation_contains "issue predates run" "$rc" "${TMP}/violations.txt" "predates this run"

# === Test 9: deferred-to within 60s grace window passes ===
make_comments "${TMP}/c9.json" "Item: deferred-to #4500"
i4500=$(issue_grace 4500)
make_lookup "${TMP}/stub9.sh" 4500 "$i4500"
rc=$(run_verifier "${TMP}/c9.json" "${TMP}/stub9.sh" "$RUN_START")
assert_pass "issue within 60s grace window passes" "$rc" "${TMP}/violations.txt"

# === Test 10: deferred-to to a PR object is flagged ===
make_comments "${TMP}/c10.json" "Item: deferred-to #2294"
ipr=$(pr_object 2294)
make_lookup "${TMP}/stub10.sh" 2294 "$ipr"
rc=$(run_verifier "${TMP}/c10.json" "${TMP}/stub10.sh" "$RUN_START")
assert_violation_contains "PR object instead of issue" "$rc" "${TMP}/violations.txt" "refers to a PR"

# === Test 11: case-insensitive Deferred-To #NNNN is verified ===
make_comments "${TMP}/c11.json" "Item: Deferred-To #4000"
i4000=$(issue_before 4000)
make_lookup "${TMP}/stub11.sh" 4000 "$i4000"
rc=$(run_verifier "${TMP}/c11.json" "${TMP}/stub11.sh" "$RUN_START")
assert_violation_contains "case-insensitive Deferred-To" "$rc" "${TMP}/violations.txt" "predates this run"

# === Test 12: empty comment list is itself a violation ===
make_comments "${TMP}/c12.json"  # no bodies
make_lookup "${TMP}/stub12.sh"
rc=$(run_verifier "${TMP}/c12.json" "${TMP}/stub12.sh" "$RUN_START")
assert_violation_contains "no response posted" "$rc" "${TMP}/violations.txt" "no top-level response comment"

# === Test 13: quoted reviewer text (`> ...`) is ignored ===
make_comments "${TMP}/c13.json" "$(printf '%s\n' \
  '> reviewer said: will file a follow-up' \
  'Item 1: fixed-in-PR (commit abc123)')"
make_lookup "${TMP}/stub13.sh"
rc=$(run_verifier "${TMP}/c13.json" "${TMP}/stub13.sh" "$RUN_START")
assert_pass "quoted reviewer text ignored" "$rc" "${TMP}/violations.txt"

# === Test 14: mixed in-run and predating deferrals — only the bad one flagged ===
make_comments "${TMP}/c14.json" "Item A: deferred-to #5100
Item B: deferred-to #4100"
i5100=$(issue_in_run 5100)
i4100=$(issue_before 4100)
make_lookup "${TMP}/stub14.sh" 5100 "$i5100" 4100 "$i4100"
rc=$(run_verifier "${TMP}/c14.json" "${TMP}/stub14.sh" "$RUN_START")
if [[ "$rc" -ne 0 ]] \
  && grep -q "#4100" "${TMP}/violations.txt" \
  && ! grep -q "#5100" "${TMP}/violations.txt"; then
  echo "PASS: only predating deferral flagged in mixed comment"
  PASSES=$((PASSES + 1))
else
  echo "FAIL: mixed deferrals — rc=$rc, violations:"
  cat "${TMP}/violations.txt"
  FAILURES=$((FAILURES + 1))
fi

echo
echo "Results: ${PASSES} passed, ${FAILURES} failed"
if (( FAILURES > 0 )); then
  exit 1
fi
exit 0
