# Analysis — Re-review delta includes commits merged in from the base branch

## Problem Statement

When an automated reviewer (`egg-reviewer`, `agent-mode-design`, `contract-verify`, or the in-pipeline BRC reviewers) re-reviews a PR after a base-branch merge has landed on the PR branch, the reviewer treats the merged-in base-branch work as part of the PR's delta and writes review text that blames the PR author for code that actually shipped on another PR.

**Concrete example**: PR #1692 review #4143938728 — the agent-mode reviewer wrote "The major new work in this delta is the babysit-pr feature (#1748 / #1756)". That feature shipped to `main` on PRs #1748/#1756 and reached PR #1692 via merge commit `76e97771`; it was not authored on the PR's branch.

**Desired outcome**: during an incremental re-review, the reviewer sees only commits that the PR author pushed to the PR branch since the last review — not commits that arrived via a merge from the base branch.

## Current Behavior

Re-review prompts use a two-dot snapshot diff (`git diff ${LAST_REVIEW_COMMIT}..HEAD`) that compares the `LAST_REVIEW_COMMIT` tree to the `HEAD` tree. Any merge from base that landed in that window is reachable from `HEAD`, so its contents show up in the diff and get attributed to the PR.

**Five affected call sites:**

- `action/build-review-prompt.sh:142`
- `action/build-agent-mode-design-review-prompt.sh:92`
- `action/build-contract-verification-prompt.sh:106`
- `orchestrator/routes/pipelines.py:3297` — delta-branch of `diff_command` in `_build_review_prompt()`
- `orchestrator/routes/pipelines.py:3448` — duplicated in the "Delta Review" directive text

`_build_review_prompt()` is shared by sequential reviewers and concurrent BRC reviewers; the `concurrent: bool` parameter only switches verdict delivery, not the diff command. So a multi-cycle BRC review hits the same bug.

Cycle-1 full-PR reviews already use the correct three-dot form (`git diff origin/{base}...HEAD`) and are not affected.

**Why three-dot alone doesn't fix it**: `git diff A...HEAD` expands to `git diff $(git merge-base A HEAD)..HEAD`. When `A = LAST_REVIEW_COMMIT` is an ancestor of `HEAD`, `merge-base(A, HEAD) = A`, so three-dot collapses to two-dot. The fix must explicitly *exclude* commits reachable from the base branch.

## Constraints

- **Shallow GHA checkouts.** The PR-code checkout in `.github/workflows/reusable-review.yml:496-501` uses `actions/checkout@v4` with default depth. `origin/<base>` may not be present without an explicit `git fetch origin <base>`. The new prompt must instruct the reviewer to fetch, or the command will fail.
- **Orchestrator worktrees already maintain `origin/<base>`** (cycle-1 relies on it today, line 3299).
- **Non-default base branches.** The GHA scripts currently hard-code no base awareness. `reusable-review.yml` must plumb the real base ref through an env var so PRs targeting anything other than `main` still work.
- **Trusted-main checkout for prompt building** (`reusable-review.yml:481-485`). Shell-script changes are picked up via the trusted-main checkout — no PR-checkout attack surface.
- **Backwards compatibility.** `BASE_REF` defaults to `main` so deployed workflows continue to function during rollout.
- **Cycle-1 full-PR reviews stay untouched.** Only the `is_delta_review` / re-review code path changes.

## Options Considered

**Option A — Per-commit patch series (selected)**
Replace `git diff ${LAST_REVIEW_COMMIT}..HEAD` with `git log ${LAST_REVIEW_COMMIT}..HEAD --not origin/${BASE_REF} -p` at all five sites. Add a `git fetch origin ${BASE_REF}` nudge to the three GHA scripts (shallow checkout). Plumb `BASE_REF` through `reusable-review.yml`'s existing `pr-meta` step.
- **Pros**: Explicitly excludes base-branch commits; per-commit framing gives author/message context; smallest diff; matches issue's prescribed fix.
- **Cons**: Output shape changes from aggregated diff to per-commit patch series (LLM reviewers handle both). Requires `origin/<base>` fetched.

**Option B — Semantic delta via reconstructed PR-side trees**
- **Pros**: Single aggregated diff preserves output shape.
- **Cons**: Significantly more complex to express as a shell/git command. Fragile on merge topologies with conflict resolution.

**Option C — Inline hybrid with revision pathspec**
- **Pros**: Preserves single-diff output.
- **Cons**: Fragile: breaks on non-contiguous PR commits in the DAG. Doesn't actually *exclude* base-branch content on merge-in-the-middle topologies.

## Decisions (resolved)

| # | Decision | Resolution |
|---|---|---|
| 1 | Output format for re-review delta | Per-commit `git log <sha>..HEAD --not origin/<base> -p` (Option A) |
| 2 | Orchestrator `git fetch origin <base>` nudge | Add it — mirror the GHA fix for safety |
| 3 | `BASE_REF` default when unset | Default to `main` (status quo) |

## Open Questions (resolved)

- **Q1 — Retract old reviews written against the buggy command on open PRs?** No. Strictly forward-only; don't retroactively flag or retract.
- **Q2 — Objection to per-commit output on long PRs (>30 commits)?** No objection. Per-commit format is fine for large PRs. Don't add commit-count caps.
- **Q3 — Non-default base branches today?** No egg PRs target non-main today, but `BASE_REF` plumbing still matters — other repos using this system will have long-lived branches or chained PRs. Keep the plumbing and the non-default-base test case (`develop`).

## Complexity

**Medium.** Six tasks across four file classes, single phase, no architectural decisions beyond what's already settled. Changes are mechanical once the command form is approved; non-trivial bit is the `BASE_REF` plumbing through `reusable-review.yml`.