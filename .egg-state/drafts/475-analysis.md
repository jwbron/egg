# Analysis: Update action references to @v0 after first release

> Issue: #475 | Phase: refine

## Problem Statement

After merging PR #471 (semantic versioning infrastructure), all GitHub Action references in the repository still point to `@main` instead of `@v0`. This was intentionally deferred because the `v0` floating tag doesn't exist until the first release (v0.1.0) is created. Once the release is made, all internal workflow references and documentation examples need to be updated from `@main` to `@v0` to follow semantic versioning best practices.

**Current state**: All action references use `@main` (28 occurrences across 15 workflow files + 13 occurrences in 4 documentation files)

**Desired outcome**: All action references use `@v0` for stability while allowing minor/patch updates

## Current Behavior

The repository currently references actions using `@main`:

### Workflow Files (28 occurrences in 15 files)

| File | Occurrences | Reference Types |
|------|-------------|-----------------|
| `sdlc-multi-agent.yml` | 4 | `uses:` statements |
| `reusable-autofix.yml` | 3 | `default:` input + `uses:` statement |
| `sdlc-work-loop.yml` | 2 | `uses:` statements |
| `reusable-review.yml` | 2 | `default:` input + `uses:` statement |
| `reusable-conflict-resolve.yml` | 2 | `default:` input + `uses:` statement |
| `on-review-feedback.yml` | 2 | `default:` input + `uses:` statement |
| `on-mention.yml` | 2 | `default:` input + `uses:` statement |
| `on-push-doc-updater.yml` | 2 | `default:` input + `uses:` statement |
| `self-improvement.yml` | 2 | `default:` input + `uses:` statement |
| `on-merge-conflict.yml` | 2 | Comments (documentation) |
| `sdlc-pipeline.yml` | 1 | `default:` input |
| `on-check-failure.yml` | 1 | Comment (documentation) |
| `on-pull-request.yml` | 1 | Comment (documentation) |
| `on-pull-request-agent-mode-design.yml` | 1 | Comment (documentation) |
| `on-pull-request-contract-verify.yml` | 1 | Comment (documentation) |

### Documentation Files (13 occurrences in 4 files)

| File | Occurrences |
|------|-------------|
| `docs/guides/reusable-workflows.md` | 10 |
| `action/README.md` | 1 |
| `README.md` | 1 |
| `docs/guides/deployment.md` | 1 |

## Constraints

- **Prerequisite**: The `v0` tag must exist before this change can be merged (created by first release v0.1.0)
- **CI validation**: Once changed to `@v0`, CI checks will fail if the tag doesn't exist
- **Ordering**: PR #471 must be merged first (already done as of 2026-02-10)
- **Scope**: Changes are purely mechanical text substitution - no logic changes
- **Comments**: Some references are in comments (documentation purposes) and should also be updated for consistency

## Options Considered

### Option A: Wait for Release, Then Batch Update

**Approach**: Wait until the first release (v0.1.0) creates the `v0` tag, then update all 41 occurrences in a single PR.

**Pros**:
- Single atomic change
- CI will pass immediately
- Clear and simple workflow

**Cons**:
- Blocked until release happens
- None significant

### Option B: Create PR Now, Merge After Release

**Approach**: Create the PR now with all changes, keep it open until the release creates the `v0` tag, then merge.

**Pros**:
- Work is prepared in advance
- Ready to merge immediately after release

**Cons**:
- PR will have failing CI checks until release
- May accumulate merge conflicts if workflow files change
- Confusing to reviewers who see failing checks

## Recommended Approach

**Option A: Wait for Release, Then Batch Update**

This is the simpler and cleaner approach. Since the issue already documents the exact scope of changes needed (issue #475 checklist), there's no benefit to creating a PR with failing checks. The mechanical nature of this change (simple find-and-replace) means the implementation risk is minimal.

**Implementation plan**:
1. Wait for first release (v0.1.0) to create the `v0` tag
2. Verify the tag exists: `git ls-remote --tags origin v0`
3. Update all 41 occurrences using find-and-replace
4. Run CI to verify all workflow references resolve correctly
5. Submit PR for review

**Estimated scope**: 19 files, 41 occurrences (15 workflow files + 4 docs)

## Implementation Details

The changes fall into three categories:

### 1. Input Defaults (8 files)
Files with `action_ref` input that needs default value updated:
- `sdlc-pipeline.yml:42`
- `reusable-review.yml:23`
- `reusable-autofix.yml:29` and `:74`
- `reusable-conflict-resolve.yml:24`
- `on-review-feedback.yml:36`
- `on-mention.yml:32`
- `on-push-doc-updater.yml:47`
- `self-improvement.yml:40`

### 2. Uses Statements (10 files)
Files with `uses: jwbron/egg/action@main` that needs ref updated:
- `sdlc-multi-agent.yml:367`, `:515`, `:643`, `:773`
- `sdlc-work-loop.yml:385`, `:883`
- `reusable-review.yml:464`
- `reusable-autofix.yml:217`
- `reusable-conflict-resolve.yml:182`
- `on-review-feedback.yml:405`
- `on-mention.yml:298`
- `on-push-doc-updater.yml:102`
- `self-improvement.yml:292`

### 3. Comments and Documentation (9 files)
References in comments or docs:
- `on-check-failure.yml:72` (comment)
- `on-pull-request.yml:24` (comment)
- `on-pull-request-agent-mode-design.yml:32` (comment)
- `on-pull-request-contract-verify.yml:77` (comment)
- `on-merge-conflict.yml:118`, `:138` (comments)
- `docs/guides/reusable-workflows.md` (10 occurrences)
- `docs/guides/deployment.md:143`
- `action/README.md:29`
- `README.md:186`

## Open Questions

There are no blocking questions. The implementation is straightforward once the prerequisite (v0 tag existence) is met.

**Prerequisite verification** (automated check before implementation):
```bash
# Verify v0 tag exists
git ls-remote --tags origin | grep -q "refs/tags/v0$" && echo "v0 tag exists" || echo "v0 tag NOT found"
```

---

*Authored-by: egg*
