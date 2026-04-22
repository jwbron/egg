## Task Analysis

**Problem statement**: When a PR is re-reviewed by an automated reviewer (`egg-reviewer`, `agent-mode-design`, `contract-verify`) after a base-branch merge has landed on the PR branch, the reviewer attributes merged-in work to the PR author — producing wrong design/correctness claims about code that wasn't authored on the PR.

**Source context**: Issue #1758 documents the bug with concrete evidence (PR #1692 review #4143938728). The agent-mode reviewer blamed babysit-pr work that actually shipped to `main` via PRs #1748 / #1756 and was pulled into the PR via merge commit `76e97771`.

**System context**: Two parallel review systems hit the same bug.

1. **GHA review bots** — three shell scripts in `action/` build a review prompt. Built from a trusted `main` checkout (`.github/workflows/reusable-review.yml:481-494`), then fed to the reviewer agent running against a PR-code checkout (`reusable-review.yml:496-501`, shallow).
2. **Orchestrator pipelines** — `_build_review_prompt()` in `orchestrator/routes/pipelines.py:3214` is shared by sequential reviewers and concurrent BRC reviewers (the `concurrent` flag only switches verdict delivery). The `is_delta_review` branch emits the buggy command at line 3260 and repeats it at line 3411 in the "Delta Review" directive.

Cycle-1 full-PR reviews correctly use `git diff origin/{base}...HEAD` (three-dot with a merge-base that isn't the last-review commit). **Only the incremental re-review path hits the bug.**

**Technical root cause**: `git diff A..HEAD` is a snapshot diff. When `A = LAST_REVIEW_COMMIT` is an ancestor of `HEAD` and a merge from base lands between them, the merge's contents appear in the diff. Three-dot (`A...HEAD = git diff $(git merge-base A HEAD)..HEAD`) collapses to two-dot because `merge-base(A, HEAD) = A`. The fix must exclude commits reachable from `origin/${base}` — `git log A..HEAD --not origin/${base} -p` does this.

**Files affected:**
- `action/build-review-prompt.sh:142` — replace two-dot diff; add `BASE_REF` env var (default `main`); add `git fetch origin ${BASE_REF}` instruction (PR-code checkout is shallow)
- `action/build-agent-mode-design-review-prompt.sh:92` — same change
- `action/build-contract-verification-prompt.sh:106` — same change
- `orchestrator/routes/pipelines.py:3259-3263` — replace the delta `diff_command` with `git log {last_reviewed_commit}..HEAD --not {_base_ref} -p`
- `orchestrator/routes/pipelines.py:3408-3413` — update "Delta Review" directive text (and add base-branch fetch nudge)
- `.github/workflows/reusable-review.yml` — extend `pr-meta` step to emit `base-ref` from its existing `pr_json` jq pipeline; pass `BASE_REF` env to prompt-build step. No consumer-workflow changes needed — plumbing is internal to `reusable-review.yml`.
- `tests/action/test_build_review_prompt.py` — update assertion at line 169, add `BASE_REF` test
- `tests/action/test_build_agent_mode_design_review_prompt.py` — same-shape updates
- `tests/action/test_build_contract_verification_prompt.py` — same-shape updates
- `orchestrator/tests/test_pipeline_prompts.py` — update delta-diff assertions; add non-default-base test

**Risks / edge cases:**
- `origin/${BASE_REF}` must exist in the reviewer's clone. The GHA PR-code checkout is shallow, so updated prompts explicitly instruct `git fetch origin ${BASE_REF}` first. Orchestrator worktrees already maintain `origin/${base}` (cycle-1 relies on it today).
- `git log … -p` produces a per-commit patch series rather than a single aggregated diff. LLM reviewers handle both formats; per-commit framing arguably reduces mis-attribution further.
- Non-default bases: plumbed through `reusable-review.yml`'s existing `pr-meta` step (internal change — no consumer workflow edits needed).
- Cycle-1 full-PR reviews are untouched.