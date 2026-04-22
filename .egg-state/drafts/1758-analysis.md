# Analysis: Re-review delta includes commits merged in from the base branch

> Issue: #1758 | Phase: refine

## Problem Statement

When an automated reviewer (`egg-reviewer`, `agent-mode-design`, `contract-verify`, or the in-pipeline BRC reviewers) re-reviews a PR after a base-branch merge has landed on the PR branch, the reviewer treats the merged-in base-branch work as part of the PR's delta. The reviewer then writes review text that blames the PR author for code that actually shipped on another PR.

Concrete example: [PR #1692 review #4143938728](https://github.com/jwbron/egg/pull/1692#pullrequestreview-4143938728) — the agent-mode reviewer wrote "The major new work in this delta is the babysit-pr feature (#1748 / #1756)". That feature shipped to `main` on PRs #1748/#1756 and reached PR #1692 via merge commit `76e97771`; it was not authored on the PR's branch.

Desired outcome: during an incremental re-review, the reviewer sees only commits that the PR author pushed to the PR branch since the last review — not commits that arrived via a merge from the base branch.

## Current Behavior

Re-review prompts use a two-dot snapshot diff that compares the `LAST_REVIEW_COMMIT` tree to the `HEAD` tree. Because any merge from base that landed in that window is reachable from `HEAD`, its contents show up in the diff and get attributed to the PR.

**Five affected call sites** (verified against current source on `egg/issue-1758-retry`):

- `action/build-review-prompt.sh:142` — `git diff ${LAST_REVIEW_COMMIT}..HEAD`
- `action/build-agent-mode-design-review-prompt.sh:92` — same
- `action/build-contract-verification-prompt.sh:106` — same
- `orchestrator/routes/pipelines.py:3297` — delta-branch of `diff_command` in `_build_review_prompt()`
- `orchestrator/routes/pipelines.py:3448` — duplicated in the "Delta Review" directive text

Cycle-1 full-PR reviews already use the correct three-dot form (`git diff origin/{base}...HEAD`, line 3299) and are not affected.

`_build_review_prompt()` is shared by sequential reviewers and concurrent BRC reviewers — the `concurrent: bool` parameter at line 3258 only switches verdict delivery (JSON file vs. ACK/NACK `--reason`), not the diff command. So a multi-cycle BRC review will hit the same bug even though BRC is typically single-cycle in practice.

Why three-dot alone doesn't fix it: `git diff A...HEAD` expands to `git diff $(git merge-base A HEAD)..HEAD`. When `A = LAST_REVIEW_COMMIT` is an ancestor of `HEAD`, `merge-base(A, HEAD) = A`, so three-dot collapses to two-dot. The fix must explicitly *exclude* commits reachable from the base branch, not just pick a different merge-base.

## Constraints

- **Shallow checkouts in GHA.** The PR-code checkout in `.github/workflows/reusable-review.yml:496-501` uses `actions/checkout@v4` with default depth. `origin/<base>` may not be present without an explicit `git fetch origin <base>`. The new prompt must instruct the reviewer to fetch, or the command will fail.
- **Orchestrator worktrees already maintain `origin/<base>`.** Cycle-1 prompts rely on this today (line 3299), so the orchestrator-side fix does not *require* additional fetching plumbing. (Whether to add a `git fetch origin <base>` nudge anyway for defence-in-depth symmetry with the GHA fix is decision-2 below.)
- **Non-default base branches.** The GHA shell scripts currently hard-code no base-branch awareness. The fix adds a `BASE_REF` env var to the scripts; the *workflow* (`reusable-review.yml`, not consumer workflows) owns plumbing the real base ref through its existing `pr-meta` step. Consumer workflows (`on-pull-request*.yml`) are unchanged.
- **Trusted-main checkout for prompt building** (`reusable-review.yml:481-485`). The prompt-building step runs from `main`, so any change to the shell scripts is picked up via the trusted-main checkout — there's no PR-checkout-only attack surface.
- **Backwards compatibility.** GHA reviewer bots are already deployed in production; any change must not break PR reviews already mid-flight. The change is additive (new `BASE_REF` env var with `main` default) so deployed workflows continue to function during the rollout window.
- **Cycle-1 full-PR reviews stay untouched.** Only the `is_delta_review` / re-review code path changes. Note that `is_delta_review` at `pipelines.py:3294` also requires `last_reviewed_commit` truthy; cycle > 1 with a missing marker correctly falls back to the three-dot full-PR form.
- **Output size scales with commit count.** `git log -p` output grows linearly with the number of PR-authored commits in the delta window. In practice re-review windows are small (1–10 commits); for very long-running PRs this could produce verbose output (see feedback Q2 for whether to cap this).

## Options Considered

### Option A: Patch series of PR-only commits since last review

**Approach**: Replace `git diff ${LAST_REVIEW_COMMIT}..HEAD` with `git log ${LAST_REVIEW_COMMIT}..HEAD --not origin/${BASE_REF} -p` at all five call sites. Add a `git fetch origin ${BASE_REF}` nudge to the three GHA scripts (shallow checkout). Plumb `BASE_REF` through `reusable-review.yml`'s existing `pr-meta` step.

**Pros**:
- Explicitly excludes commits reachable from the base branch — the core semantic fix the issue demands.
- Per-commit framing (`git log -p`) gives the reviewer context (author, message, patch) for each PR-authored commit; arguably *reduces* mis-attribution further vs. an aggregated diff.
- Smallest possible change — one command per call site, a few lines of test updates, one YAML env var plumb.
- Matches a human reviewer's mental model of "what did the PR author push since I last looked".
- Cycle-1 reviews remain unchanged.

**Cons**:
- Output format changes from a single aggregated diff to a per-commit patch series. Reviewers must adapt framing; LLM reviewers generally handle both well but the prompt phrasing should make the change explicit.
- Requires the reviewer's working tree to have `origin/<base>` fetched. Extra step on shallow GHA checkouts.
- Large PRs with many commits produce verbose output (per-commit headers repeat). In practice re-review deltas are small (1–10 commits) so this is rarely a problem, but it's a real size-budget consideration for long-running PRs.

### Option B: Semantic delta — compare PR-side state then vs. now

**Approach**: Reconstruct the PR-side "snapshot then" by taking `LAST_REVIEW_COMMIT`'s merge-base with `origin/<base>` and applying only the PR-authored patches up to `LAST_REVIEW_COMMIT`. Then do the same for `HEAD`. Diff the two reconstructions.

**Pros**:
- Produces a single aggregated diff like the current format — no output-shape change.
- Semantically precise: shows the net effect of PR-authored changes between the two review points.

**Cons**:
- Significantly more complex to express as a shell/git command. Either requires scripted commit replay (fragile, slow) or approximation via `git diff $(merge-base A B)..HEAD` tricks that don't generalise across merge topologies.
- Edge cases around conflict resolutions during base-branch merges are hard to handle cleanly.
- Doesn't match the issue's prescribed fix.
- Same `origin/<base>` availability constraint as Option A.

### Option C: Inline single-command hybrid — `git diff` with a pathspec/revision exclusion

**Approach**: Use `git diff $(git rev-list ${LAST_REVIEW_COMMIT}..HEAD --not origin/${BASE_REF} | tail -n 1)^..HEAD` or similar to produce a single aggregated diff over only the PR-authored commits.

**Pros**:
- Preserves the single-diff output format.

**Cons**:
- Fragile: breaks if there are no PR-authored commits in the window, or if the PR-authored commits are non-contiguous in the DAG.
- Doesn't actually *exclude* base-branch content — it just picks a range that happens to skip them when the history is linear. On a merge-in-the-middle topology the two endpoints are still separated by the merge.
- More complex to write, explain, and test than Option A.

## Recommended Approach

**Option A.** It is the issue's own prescribed fix, the smallest diff, semantically correct (explicit `--not origin/<base>`), and arguably improves review quality by framing the delta per-commit with author/message context. The main trade-offs — output shape change and the shallow-checkout fetch step — are handled cleanly by updating the prompt wording and adding one `git fetch origin <base>` instruction.

Mechanical changes (already captured in the existing `1758-plan.md` draft):

1. `orchestrator/routes/pipelines.py:3296-3300` — replace the `is_delta_review` branch of `diff_command` with `git log {last_reviewed_commit}..HEAD --not {_base_ref} -p`. Leave the cycle-1 three-dot branch alone.
2. `orchestrator/routes/pipelines.py:3443-3450` — update the "Delta Review" directive text to reference the new command and add a `git fetch origin <base_branch>` nudge.
3. `action/build-review-prompt.sh`, `build-agent-mode-design-review-prompt.sh`, `build-contract-verification-prompt.sh` — accept `BASE_REF` (default `main`), rewrite the re-review instruction to `git fetch origin ${BASE_REF}` followed by `git log ${LAST_REVIEW_COMMIT}..HEAD --not origin/${BASE_REF} -p`.
4. `.github/workflows/reusable-review.yml` — extend the `pr-meta` step (around lines 361-365) to emit `base-ref` from the existing `pr_json` jq pipeline; pass `BASE_REF: ${{ steps.pr-meta.outputs.base-ref }}` to the prompt-build step's `env:` block.
5. Test updates in `tests/action/test_build_*_prompt.py` and `orchestrator/tests/test_pipeline_prompts.py` — replace the old two-dot assertions with the new command-form assertions and add non-default-base-branch coverage (e.g. `base_ref="develop"`).

## Open Questions

The following decisions and feedback items have been registered via `egg-contract` and appear as interactive comments on this issue for the human to resolve. Listed here for visibility:

### Multiple-choice decisions

<!-- egg-hitl-decision id=decision-1 -->

**Output format for the re-review delta: which command should the reviewer run?**

- [ ] git log <sha>..HEAD --not origin/<base> -p  (per-commit patch series, issue's Option 1 — recommended)
- [ ] git diff reconstructed PR-only trees (single aggregated diff, issue's Option 2 — more complex)
- [ ] Keep git diff but add a separate summary list of PR-only commits (hybrid)
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-2 -->

**Orchestrator worktree freshness: should the orchestrator re-review prompt also include a 'git fetch origin <base>' nudge (or should we rely on worktrees already having fresh refs)?**

- [ ] Add 'git fetch origin <base>' nudge in the orchestrator Delta Review directive too — safer, mirrors GHA fix
- [ ] Skip the nudge for orchestrator — cycle-1 already depends on origin/<base> being present, so worktree setup must already refresh it
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-3 -->

**BASE_REF default when env var is unset in the three GHA shell scripts — what fallback should we use?**

- [ ] Default to 'main' (matches status quo; CI-callable scripts keep working if someone forgets to plumb BASE_REF)
- [ ] Fail loudly (exit non-zero) if BASE_REF is unset on the re-review path — prevents silent regressions but requires all callers to set it
- [ ] Other (explain in reply)

### Open-ended feedback

<!-- egg-feedback id=feedback-1 -->

- **Q1**: Are there already-posted automated reviews on open PRs that were written against the buggy `git diff <sha>..HEAD` command and should be flagged/retracted once this fix ships, or is the fix strictly forward-only?
- **Q2**: Any objection to per-commit `git log -p` output on very long PRs (>30 commits in the delta window)? In practice re-review windows are small, but if there's a concern we'd want to cap the commit count or add a pagination nudge.
- **Q3**: Non-default base branches in practice — are there any egg PRs today targeting something other than `main` (e.g. a long-lived `next` branch, chained PRs)? If not, the `BASE_REF` plumbing is still worth having but the `develop` test case is purely theoretical.

## Complexity Assessment

**Medium.** Six tasks across four file classes (orchestrator Python, three GHA shell scripts, one GHA workflow YAML, two test file groups). Single phase, no architectural decisions beyond what's already settled by the issue. Changes are mechanical once the command form is approved; the non-trivial bit is the `BASE_REF` plumbing through `reusable-review.yml` and the test helper updates to accept a new kwarg.

---

*Authored-by: egg*
