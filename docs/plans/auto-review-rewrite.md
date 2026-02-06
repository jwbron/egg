# Implementation Plan: Agent-Driven Auto-Reviews

**Issue:** [#161](https://github.com/jwbron/egg/issues/161)
**Status:** Proposed

This plan follows the [agent-mode design guidelines](../guides/agent-mode-design.md) to rewrite auto-reviews.

---

## Overview

The current auto-review implementation treats Claude as a text-in/JSON-out function. This plan rewrites it to let Claude operate as an agent with tools.

**Current flow:**
1. `build-review-prompt.sh` pre-fetches all PR data (diffs, file contents, metadata)
2. Bakes everything into a ~100K char text prompt
3. Tells Claude to produce structured JSON output
4. `post-review-comments.sh` parses JSON from logs and posts as GitHub review

**New flow:**
1. Build minimal prompt with just PR number and review guidelines
2. Claude fetches what it needs using `gh pr diff`, file reads, etc.
3. Claude posts its own review directly via `gh pr review`

---

## Phase 1: Create New Minimal Prompt Builder

**File:** `action/build-review-prompt-v2.sh` (new file, ~80 lines)

Replace the 380-line pre-fetch approach with a minimal prompt:

```
Review PR #[PR_NUMBER] in [GITHUB_REPOSITORY].

Use `gh pr diff` to see the changes. Read files for context. Check how
changed code interacts with the rest of the codebase.

[include .egg/review-rules.md if present, else brief defaults]

Post your review using `gh pr review [PR_NUMBER]`. Use inline comments where
the feedback applies to specific lines. Use --approve if the PR looks good,
--request-changes for blocking issues, or --comment for advisory feedback.

Focus on issues that require human judgment. Skip style issues that linters
catch (ruff, eslint, prettier). Be specific and suggest fixes. If the PR
looks good, approve it.

Sign your review with: — Authored by egg
```

**Key changes from current approach:**
- No pre-fetching of diffs or file contents (agent pulls what it needs)
- No JSON output schema (agent posts directly via `gh pr review`)
- No truncation limits (agent sees full context)
- Agent can explore beyond the diff (callers, tests, related files)

---

## Phase 2: Delete Post-Processing Script

**File to delete:** `action/post-review-comments.sh` (517 lines)

This script exists because the current design has Claude output JSON that then needs to be posted. With agent-driven reviews, Claude posts directly via `gh pr review` — no parsing, no line-number-to-diff-position mapping, no fallback paths.

---

## Phase 3: Update Workflow

**File:** `.github/workflows/on-pull-request.yml`

Changes:
1. **Move dismiss logic to pre-step** — Run before egg starts, not in post-processing
2. **Use new prompt builder** — `action/build-review-prompt-v2.sh`
3. **Remove post-review-comments step** — Agent posts its own review
4. **Simplify result comment** — Just success/failure status, no parsing

```yaml
steps:
  - name: Generate bot token
    # (unchanged)

  - name: Fetch PR metadata
    # (unchanged for workflow_dispatch)

  - name: Checkout main (trusted)
    # (unchanged - security model preserved)

  - name: Dismiss previous bot reviews        # NEW - moved from post-processing
    run: |
      gh api "repos/${{ github.repository }}/pulls/${{ env.PR_NUMBER }}/reviews" \
        --jq '[.[] | select(.user.login == "james-in-a-box" or .user.login == "james-in-a-box[bot]") | select(.state | test("PENDING|COMMENTED|CHANGES_REQUESTED"))] | .[].id' \
      | while read -r id; do
          gh api "repos/${{ github.repository }}/pulls/${{ env.PR_NUMBER }}/reviews/$id/dismissals" \
            -X PUT -f message="Superseded by new review" || true
        done
    env:
      GH_TOKEN: ${{ steps.generate-token.outputs.token }}
      PR_NUMBER: ${{ github.event.pull_request.number }}

  - name: Build review prompt
    run: bash action/build-review-prompt-v2.sh   # NEW script
    # (outputs: prompt-file, model)

  - name: Checkout PR branch
    # (unchanged)

  - name: Run egg review
    uses: jwbron/egg/action@main
    with:
      prompt-file: (from build step)
      model: opus                               # Always opus for reviews
      # (other inputs unchanged)

  - name: Post status comment                   # SIMPLIFIED
    if: always()
    run: |
      if [[ "${{ steps.run-egg.outcome }}" == "success" ]]; then
        BODY="egg review completed. [View run logs](${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }})"
      else
        BODY="egg review failed. [View run logs](${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }})"
      fi
      gh issue comment "${{ env.PR_NUMBER }}" --body "$BODY"
    env:
      GH_TOKEN: ${{ steps.generate-token.outputs.token }}
      PR_NUMBER: ${{ github.event.pull_request.number }}
```

---

## Phase 4: Add Review-Specific Conventions

**File:** `action/review-conventions.md` (new, ~30 lines)

A brief guide for the agent on *how* to communicate review findings (not *what* to look for):

```markdown
## Review Conventions

### Posting inline comments
Use `gh pr review --comment` with inline comments for specific line feedback:
  gh pr review 123 --comment -F review.md

**Important:** The `gh` CLI requires diff-relative line numbers for inline comments,
not absolute file line numbers. To comment on a specific line:
1. Run `gh pr diff` to see the diff with line context
2. Reference lines as they appear in the diff (added/modified lines only)
3. Use `--body` for single comments or `-F` with a file containing multiple comments

Example for inline comments on specific diff lines:
  gh api repos/OWNER/REPO/pulls/123/comments \
    -f body="Comment text" -f path="file.py" -f line=42 -f side=RIGHT

### When to approve vs request changes
- **Approve**: No blocking issues, perhaps minor suggestions
- **Request changes**: Security vulnerabilities, logic errors, breaking changes
- **Comment**: Advisory feedback, questions, suggestions for future work

### Signature
End your review with: — Authored by egg

### Rate limiting
Aim for fewer, higher-signal comments. A noisy reviewer gets ignored.
Summarize related issues rather than commenting on every instance.
```

This file would be included in the prompt if present, providing conventions without being prescriptive about review content.

---

## What to Keep (Unchanged)

| Component | Reason |
|-----------|--------|
| Skip logic (draft, bot PRs, `[skip-review]`) | Good filtering |
| Concurrency (cancel-in-progress) | Prevents duplicate reviews |
| `.egg/review-rules.md` support | Per-repo customization |
| Security model (trusted main checkout) | Prevents prompt injection |
| Model selection (opus) | Quality reviews need the best model |
| Bot token generation | Required for API access |

---

## What to Delete

| File/Component | Lines | Reason |
|----------------|-------|--------|
| `action/post-review-comments.sh` | 517 | Agent posts directly |
| `action/build-review-prompt.sh` | 381 | Replace with minimal v2 |
| JSON schema in prompt | — | Natural language output |
| Diff position mapping logic | — | Agent uses gh CLI |
| File content pre-fetching | — | Agent reads what it needs |

**Net change:** Delete ~900 lines, add ~150 lines

---

## Migration Path

Single PR approach — delete old implementation and create new one together:

1. Delete `action/build-review-prompt.sh` and `action/post-review-comments.sh`
2. Create `action/build-review-prompt-v2.sh` with minimal prompt
3. Create `action/review-conventions.md` with posting guidelines
4. Update `.github/workflows/on-pull-request.yml` with new steps
5. Test on a few PRs to validate quality

---

## Success Criteria

- [ ] Reviews are posted directly by the agent (no JSON parsing)
- [ ] Review quality matches or exceeds "@james-in-a-box review this pr" flow
- [ ] Agent can explore beyond the diff (callers, tests, architecture)
- [ ] Workflow is simpler (~50% fewer lines)
- [ ] Per-repo customization (`.egg/review-rules.md`) still works

---

## Related

- [Issue #161](https://github.com/jwbron/egg/issues/161) — Rewrite auto-reviews to use agent-driven approach
- [Issue #134](https://github.com/jwbron/egg/issues/134) — Parent issue for AI-powered code review
- [PR #146](https://github.com/jwbron/egg/pull/146) — Original Phase 1 implementation
- [Agent-Mode Design Guidelines](../guides/agent-mode-design.md) — Design principles

---

Authored by egg
