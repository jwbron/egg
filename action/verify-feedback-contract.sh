#!/usr/bin/env bash
# Verifies the feedback-addressing agent's top-level PR response comments
# against the contract in action/build-feedback-prompt.sh:
#
#   1. The agent must post at least one response (count == 0 is a violation).
#   2. No phantom follow-up phrases. Specific phrases (e.g. "filing later")
#      are always violations; broad alternations ("will file", "will track",
#      "will open an issue") are only flagged when no `#NNNN` appears on the
#      same line — legitimate citations like "filed as #1234" pass.
#   3. Each `deferred-to #NNNN` must point to a real issue (not a PR) created
#      during this run, with a 60s grace window for clock skew between the
#      runner and GitHub server timestamps.
#
# Inputs (env):
#   COMMENTS_JSON_FILE — path to a JSON array of candidate comments, each
#                        with a `.body` field. The caller is responsible for
#                        pre-filtering to bot-authored comments posted during
#                        this run.
#   RUN_START          — ISO 8601 UTC timestamp marking the start of this run.
#   VIOLATIONS_FILE    — path to write violations to (truncated on entry).
#   REPO               — owner/name; used by the default issue-lookup.
#   ISSUE_LOOKUP_SCRIPT— optional path to a stub script taking an issue
#                        number as $1 and printing the issue JSON to stdout.
#                        Used by tests; if unset, falls back to `gh api`.
#
# Exit code: 0 if no violations; 1 otherwise.

set -euo pipefail

: "${COMMENTS_JSON_FILE:?required}"
: "${RUN_START:?required}"
: "${VIOLATIONS_FILE:?required}"

: > "$VIOLATIONS_FILE"

run_start_epoch=$(date -u -d "$RUN_START" +%s)
threshold_epoch=$((run_start_epoch - 60))

forbidden_specific='(filing later'
forbidden_specific+='|tracking separately'
forbidden_specific+='|file a follow[- ]?up'
forbidden_specific+='|track.{0,15}follow[- ]?up'
forbidden_specific+='|follow[- ]?up later)'

forbidden_broad='(will file|will track|will open an issue)'

count=$(jq 'length' < "$COMMENTS_JSON_FILE")
echo "Scanning ${count} response comment(s)"

if [[ "$count" -eq 0 ]]; then
  echo "no top-level response comment posted by the agent" >> "$VIOLATIONS_FILE"
fi

lookup_issue() {
  local n="$1"
  if [[ -n "${ISSUE_LOOKUP_SCRIPT:-}" ]]; then
    "$ISSUE_LOOKUP_SCRIPT" "$n" 2>/dev/null || true
  else
    gh api "repos/${REPO}/issues/${n}" 2>/dev/null || true
  fi
}

strip_code() {
  awk 'BEGIN{in_fence=0}
       /^[[:space:]]*```/ { in_fence = !in_fence; next }
       !in_fence' \
    | sed 's/`[^`]*`//g'
}

while IFS= read -r b64body; do
  [[ -z "$b64body" ]] && continue
  decoded=$(echo "$b64body" | base64 -d)
  scan_text=$(echo "$decoded" | strip_code | grep -v '^[[:space:]]*>' || true)

  specific_matches=$(echo "$scan_text" \
    | grep -iEo "$forbidden_specific" 2>/dev/null \
    | sort -u | paste -sd, - || true)

  broad_matches=$(echo "$scan_text" \
    | grep -iE "$forbidden_broad" 2>/dev/null \
    | grep -Ev '#[0-9]+' \
    | grep -iEo "$forbidden_broad" 2>/dev/null \
    | sort -u | paste -sd, - || true)

  if [[ -n "$specific_matches" || -n "$broad_matches" ]]; then
    matches=$(printf '%s,%s' "$specific_matches" "$broad_matches" \
              | sed 's/^,//; s/,$//; s/,,/,/g')
    echo "forbidden phrase(s): ${matches}" >> "$VIOLATIONS_FILE"
  fi

  while IFS= read -r issue_num; do
    [[ -z "$issue_num" ]] && continue
    issue_json=$(lookup_issue "$issue_num")
    if [[ -z "$issue_json" ]]; then
      echo "deferred-to #${issue_num}: issue does not exist" >> "$VIOLATIONS_FILE"
      continue
    fi
    is_pr=$(echo "$issue_json" | jq -r '.pull_request != null' 2>/dev/null || echo "false")
    if [[ "$is_pr" == "true" ]]; then
      echo "deferred-to #${issue_num}: refers to a PR, not an issue" >> "$VIOLATIONS_FILE"
      continue
    fi
    created=$(echo "$issue_json" | jq -r '.created_at' 2>/dev/null || true)
    if [[ -z "$created" || "$created" == "null" ]]; then
      continue
    fi
    created_epoch=$(date -u -d "$created" +%s 2>/dev/null || echo 0)
    if (( created_epoch < threshold_epoch )); then
      msg="deferred-to #${issue_num}: issue created at ${created}"
      msg+=" predates this run (started ${RUN_START}); re-deferring to a"
      msg+=" prior-round issue is not allowed — create a fresh follow-up"
      msg+=" or fix in-PR"
      echo "$msg" >> "$VIOLATIONS_FILE"
    fi
  done < <(echo "$scan_text" | grep -ioE 'deferred-to #[0-9]+' \
            | grep -oE '[0-9]+' | sort -u)
done < <(jq -r '.[].body | @base64' < "$COMMENTS_JSON_FILE")

if [[ -s "$VIOLATIONS_FILE" ]]; then
  exit 1
fi
exit 0
