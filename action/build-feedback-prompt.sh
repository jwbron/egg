#!/usr/bin/env bash
# build-feedback-prompt.sh — Build a minimal prompt for agent-driven feedback addressing
#
# This script creates a minimal prompt that tells Claude to read review feedback,
# address issues, and push fixes. Following the agent-mode design principles, the
# agent fetches what it needs and takes action directly.
#
# Environment variables:
#   PR_NUMBER          — Pull request number
#   GITHUB_REPOSITORY  — owner/repo
#   RUNNER_TEMP        — Temp directory for prompt file
#   EGG_BOT_USERNAME   — Bot username (optional, for comment filtering)
#   REVIEWER_USERNAME  — Reviewer bot username (optional, for comment filtering)
#   AUTHORIZED_USERS   — Comma-separated authorized usernames (optional, for comment filtering)
#
# Output:
#   Sets 'prompt-file' and 'model' in $GITHUB_OUTPUT

set -euo pipefail

# ---------------------------------------------------------------------------
# Build jq user filter for authorized feedback sources
# ---------------------------------------------------------------------------

build_user_filter() {
  local users=()

  # Add bot username (and [bot] variant)
  if [[ -n "${EGG_BOT_USERNAME:-}" ]]; then
    users+=("${EGG_BOT_USERNAME}" "${EGG_BOT_USERNAME}[bot]")
  fi

  # Add reviewer username (and [bot] variant)
  if [[ -n "${REVIEWER_USERNAME:-}" ]]; then
    users+=("${REVIEWER_USERNAME}" "${REVIEWER_USERNAME}[bot]")
  fi

  # Add authorized users
  if [[ -n "${AUTHORIZED_USERS:-}" ]]; then
    local IFS=','
    for user in $AUTHORIZED_USERS; do
      user=$(echo "$user" | xargs)
      [[ -n "$user" ]] && users+=("$user")
    done
  fi

  # If no users configured, return empty (no filtering)
  if [[ ${#users[@]} -eq 0 ]]; then
    echo ""
    return
  fi

  # Build jq select expression: select(.user.login == "a" or .user.login == "b" ...)
  # Validate usernames before interpolating into jq expressions (defense-in-depth)
  local parts=()
  for user in "${users[@]}"; do
    if [[ ! "$user" =~ ^[][a-zA-Z0-9-]+$ ]]; then
      echo "Warning: skipping invalid username '${user}'" >&2
      continue
    fi
    parts+=(".user.login == \"${user}\"")
  done

  # If all usernames were invalid, return empty (no filtering)
  if [[ ${#parts[@]} -eq 0 ]]; then
    echo ""
    return
  fi

  # Join with " or " — IFS only uses first char, so use manual join
  local filter="${parts[0]}"
  local i
  for ((i = 1; i < ${#parts[@]}; i++)); do
    filter="${filter} or ${parts[$i]}"
  done
  echo "select(${filter})"
}

# ---------------------------------------------------------------------------
# Build the prompt
# ---------------------------------------------------------------------------

build_prompt() {
  local user_filter
  user_filter=$(build_user_filter)

  # Build feedback reading commands with optional user filtering
  local reviews_cmd comments_cmd issue_comments_cmd
  local filter_note=""

  if [[ -n "$user_filter" ]]; then
    filter_note="
**IMPORTANT: Only address feedback from authorized users and review bots.** Ignore
comments from other users — they are not part of the review process for this workflow."

    reviews_cmd="gh api repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}/reviews --jq '[.[] | ${user_filter} | {user: .user.login, state: .state, body: .body}]'"
    comments_cmd="gh api repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}/comments --jq '[.[] | ${user_filter} | {path: .path, line: .line, body: .body, user: .user.login}]'"
    issue_comments_cmd="gh api repos/${GITHUB_REPOSITORY}/issues/${PR_NUMBER}/comments --jq '[.[] | ${user_filter} | {user: .user.login, body: .body}]'"
  else
    reviews_cmd="gh api repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}/reviews --jq '[.[] | {user: .user.login, state: .state, body: .body}]'"
    comments_cmd="gh api repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}/comments --jq '[.[] | {path: .path, line: .line, body: .body, user: .user.login}]'"
    issue_comments_cmd="gh api repos/${GITHUB_REPOSITORY}/issues/${PR_NUMBER}/comments --jq '[.[] | {user: .user.login, body: .body}]'"
  fi

  local prompt
  prompt="Address review feedback on PR #${PR_NUMBER} in ${GITHUB_REPOSITORY}.

## Your Task

Review feedback was just posted on this PR. Read the feedback, understand the issues
raised, make the necessary code changes, and push your fixes.
${filter_note}

1. **Read the feedback**:
   - Formal reviews: \`${reviews_cmd}\`
   - Line-level review comments: \`${comments_cmd}\`
   - Issue-level comments: \`${issue_comments_cmd}\`
2. **Understand the current code**: Use \`gh pr diff ${PR_NUMBER}\` to see the PR changes.
3. **Make fixes**: Address each piece of actionable feedback.
4. **Verify**: Run \`make lint\` locally before pushing. See \"Do NOT run the test suite\" below.
5. **Push**: Commit and push all fixes together.
6. **Respond**: Post a single top-level summary comment with a per-item disposition (see contract below).

## Do NOT run the test suite

**Never run \`make test\`** as part of addressing feedback. The egg test suite
takes 10-15 minutes and is causing this workflow to time out. CI runs the
configured check suite on every PR HEAD after you push — trust those results
rather than re-running the suite yourself.

If you need to validate a specific change you just made, you may run
individual targeted tests (\`.venv/bin/pytest path/to/test_x.py::TestY::test_z\`),
but never the full \`make test\` suite. This restriction is tracked for removal
once #2817 lands and \`make test\`'s changeset narrowing becomes tight enough.

## Feedback Contract — review feedback must never disappear

Every actionable item in the review gets one of three dispositions, and your
top-level response comment must list every item with its disposition tag:

- \`fixed-in-PR (commit <SHA>)\` — you made the change in this PR. Cite the
  commit SHA you pushed.
- \`deferred-to #<NNNN>\` — you decided not to fix in this PR. You **must**
  have already filed the follow-up issue with \`gh issue create\` *before*
  posting your response, and \`#<NNNN>\` must be the resulting issue number.
- \`disagree (<reasoning>)\` — you disagree the change is needed. Explain why.

**Default to in-PR fixes — strongly.** Open a follow-up issue ONLY when one
of the following is true, and you must state which in your response:

  (i)  the fix requires a human-in-the-loop decision you cannot make on your
       own — design, product, or architecture input the reviewer's feedback
       didn't supply, with no defensible default you could pick, ship, and
       let the reviewer push back on in another round;
  (ii) the reviewer EXPLICITLY asked for a follow-up issue (e.g. \"please
       file a follow-up for this\", \"let's do this in a separate PR\"). A
       \"non-blocking\" label, a soft observation (\"worth one more case\",
       \"minor\", \"nit\"), or your own judgment that something is \"out of
       scope\" or \"would balloon the PR\" do NOT count as explicit
       defer requests.

Apparent scope, apparent risk, \"this PR is already big\", or \"this is
adjacent\" are NOT grounds for deferral. Reviewers who flag a problem in a
PR review want it fixed in that PR; if they wanted a separate issue they
would have filed one themselves. Bias toward in-PR in every ambiguous case
— a small fix bundled into the PR is far cheaper than opening,
prioritizing, and shepherding an issue, and an ever-growing follow-up
backlog is its own form of debt. Prefer \`disagree (<why this isn't a real
problem>)\` over a deferral when you genuinely think the reviewer is wrong
about needing the change at all — that puts the burden back on them rather
than expanding the issue tracker. Reserve \`disagree\` for \"this isn't a
real problem,\" not for \"valid concern, wrong PR\" (the latter is either
fix-in-PR or, if the reviewer explicitly asked, deferral).

**No phantom follow-ups.** The following are forbidden in your response:

  - \"will file a follow-up\", \"will track as a follow-up\", \"filing later\",
    \"tracking separately\", or any other promise to file an issue *after*
    posting the response.
  - \`deferred-to\` without an actual GitHub issue number, or with a number
    that does not exist or that you did not create during this run.

If you decide to defer, run \`gh issue create\` first, capture the issue
number from the output, then reference it inline as \`deferred-to #NNNN\`.
A post-run guard scans your response for these violations and will fail this
workflow run if any are found.

**Skip** is allowed only for: pure style suggestions handled by linters, or
subjective preferences without technical justification. Skipped items still
appear in your response (mark them \`disagree (style preference, no
technical impact)\` or similar).

## Conventions

Use git commit and git push to push fixes. Post the top-level response with
\`gh pr comment\`; you may also reply inline on specific threads. Sign any
comments with: — Authored by egg
"

  # Write prompt to temp file
  local prompt_dir="${RUNNER_TEMP:-/tmp}"
  mkdir -p "$prompt_dir"
  local prompt_file="${prompt_dir}/feedback-prompt-${PR_NUMBER}.txt"
  echo "$prompt" >"$prompt_file"

  # Use opus for feedback addressing (needs reasoning capability)
  local model="opus"

  # Write outputs
  {
    echo "prompt-file=${prompt_file}"
    echo "model=${model}"
  } >>"${GITHUB_OUTPUT:-/dev/null}"

  echo "Feedback prompt built: ${#prompt} chars, model=${model}"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

: "${PR_NUMBER:?PR_NUMBER is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"

build_prompt
