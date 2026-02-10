# Plan: Support Releases

> Issue: #389 | Phase: plan

## Summary

This plan implements semantic versioning for the egg project, enabling version-pinned Docker images and GitHub Action references. Based on the approved analysis, we'll use semantic versioning with major version aliases (v1, v2) and a semi-automated release process starting at v0.1.0.

The implementation modifies the release workflow to produce versioned tags (vX.Y.Z, vX.Y, vX), updates all action references from `@main` to `@v0`, and creates a documented release process with a helper script.

## Implementation Phases

### Phase 1: Enhance Docker Image Tagging

**Goal**: Modify the release workflow to produce semantic version tags with floating major/minor aliases.

**Tasks**:
- [TASK-1-1] Update `release-images.yml` to generate all tag variants (vX.Y.Z, vX.Y, vX) — Acceptance: On release `v0.1.0`, images are tagged as `v0.1.0`, `v0.1`, and `v0`
- [TASK-1-2] Add validation to ensure release tags follow semver format — Acceptance: Workflow fails gracefully if tag doesn't match `vX.Y.Z` pattern
- [TASK-1-3] Update workflow to skip `latest` tag update for pre-release versions — Acceptance: Tags with `-alpha`, `-beta`, `-rc` suffix don't update `latest`

**Dependencies**: None

**Exit criteria**: Workflow can be manually triggered with a semver tag and produces all expected image tags.

### Phase 2: Create Release Automation Script

**Goal**: Provide a semi-automated release script that handles git tagging and floating tag updates.

**Tasks**:
- [TASK-2-1] Create `.github/scripts/create-release.sh` script — Acceptance: Script validates version format, creates git tag, updates floating tags (vX.Y, vX), and pushes to origin
- [TASK-2-2] Add `--dry-run` mode to script for testing — Acceptance: Dry-run shows what would happen without making changes
- [TASK-2-3] Update script to generate release notes template — Acceptance: Script outputs a markdown template suitable for GitHub release body

**Dependencies**: Phase 1 (need updated workflow to consume the tags)

**Exit criteria**: Running `./github/scripts/create-release.sh v0.1.0` creates the release with all necessary tags.

### Phase 3: Update Action References

**Goal**: Pin all workflow action references to the `@v0` floating tag.

**Tasks**:
- [TASK-3-1] Update `sdlc-pipeline.yml` action references from `@main` to `@v0` — Acceptance: All 5 `uses: jwbron/egg/action@main` references updated to `@v0`
- [TASK-3-2] Update `reusable-review.yml` action reference — Acceptance: `uses:` line updated, `action_ref` input default updated
- [TASK-3-3] Update `reusable-autofix.yml` action reference — Acceptance: `uses:` line updated, `action_ref` input default updated
- [TASK-3-4] Update `reusable-conflict-resolve.yml` action reference — Acceptance: `uses:` line updated, `action_ref` input default updated
- [TASK-3-5] Update `on-review-feedback.yml` action reference — Acceptance: `uses:` line updated, `action_ref` input default updated
- [TASK-3-6] Update `on-mention.yml` action reference — Acceptance: `uses:` line updated, `action_ref` input default updated
- [TASK-3-7] Update `on-push-doc-updater.yml` action reference — Acceptance: `uses:` line updated, `action_ref` input default updated
- [TASK-3-8] Update `self-improvement.yml` action reference — Acceptance: `uses:` line updated, `action_ref` input default updated

**Dependencies**: Phase 1 and 2 complete; v0.1.0 release must be created before merging this phase

**Exit criteria**: All action references point to `@v0`, and workflows continue to function correctly.

### Phase 4: Update Documentation

**Goal**: Document the versioning policy and release process.

**Tasks**:
- [TASK-4-1] Add versioning policy section to main README.md — Acceptance: Documents semver policy, how to pin versions, and what breaking changes mean
- [TASK-4-2] Update `docs/guides/reusable-workflows.md` with versioning guidance — Acceptance: Examples updated to use `@v0`, explanation of version pinning added
- [TASK-4-3] Update `action/README.md` with versioning examples — Acceptance: Quick start example uses versioned reference
- [TASK-4-4] Create `RELEASING.md` documenting the release process — Acceptance: Step-by-step guide for creating releases, including checklist

**Dependencies**: Phases 1-3 complete

**Exit criteria**: Documentation accurately reflects the versioning system and release process.

### Phase 5: Initial Release

**Goal**: Create the v0.1.0 release to enable versioned references.

**Tasks**:
- [TASK-5-1] Verify all tests pass on main — Acceptance: CI green on main branch
- [TASK-5-2] Create v0.1.0 release using the release script — Acceptance: Git tags exist, GitHub release created, images pushed with version tags
- [TASK-5-3] Verify versioned images are accessible — Acceptance: `docker pull ghcr.io/jwbron/egg-sandbox:v0.1.0` succeeds

**Dependencies**: Phases 1-4 merged to main

**Exit criteria**: v0.1.0 release published, versioned images available, workflows using `@v0` function correctly.

## Test Strategy

- **Unit tests**: None required (shell scripts don't have unit tests in this repo)
- **Integration tests**:
  - `test-action.yml` workflow validates the action works correctly
  - Manual test of `create-release.sh --dry-run` before actual release
- **Manual testing**:
  1. After Phase 1: Trigger workflow_dispatch with test tag, verify all expected image tags appear in GHCR
  2. After Phase 2: Run `create-release.sh --dry-run v0.0.0-test` to validate script behavior
  3. After Phase 3: Verify workflows still reference correct action (grep for `@v0`)
  4. After Phase 5: Run a full SDLC pipeline on a test issue to validate end-to-end

## Rollback Plan

**If image tagging breaks**:
1. Revert changes to `release-images.yml`
2. Re-push images with `latest` tag: `docker push ghcr.io/jwbron/egg-sandbox:latest`

**If action references break workflows**:
1. Revert action references from `@v0` to `@main`
2. Force-push `v0` tag to a known-good commit: `git tag -f v0 <commit> && git push -f origin v0`

**If release script creates bad tags**:
1. Delete bad tags: `git push --delete origin v0.1.0 v0.1 v0`
2. Delete local tags: `git tag -d v0.1.0 v0.1 v0`
3. Fix script and retry

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Workflows break when switching from @main to @v0 | Low | High | Create release before updating references; test in fork first |
| Force-push of floating tags (v0) causes issues | Medium | Low | Document that floating tags are updated on each release; use specific versions (v0.1.0) for reproducibility |
| Release script creates incorrect tags | Low | Medium | Dry-run mode for validation; tag validation in script |
| External consumers break on version change | Low | Low | Maintain @main as alias during transition; document in release notes |

## Migration Notes

**For external consumers of egg workflows**:
- Current `@main` references will continue to work
- Recommend updating to `@v0` for stability with automatic updates
- Use specific versions like `@v0.1.0` for full reproducibility
- Dependabot will detect major version updates automatically

**Order of operations for initial release**:
1. Merge Phases 1-2 (image tagging + release script)
2. Create v0.1.0 release using new script
3. Merge Phases 3-4 (update references + documentation)
4. Complete Phase 5 verification

This sequencing ensures versioned images exist before workflows reference them.

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Add semantic versioning for releases"
  description: |
    Implements semantic versioning for Docker images and GitHub Action references.
    This enables version pinning for stability and rollback capability.

    Key changes:
    - Release workflow produces vX.Y.Z, vX.Y, and vX tags for images
    - All internal action references pinned to @v0
    - Helper script for creating releases with proper tag management
    - Documentation for versioning policy and release process

    Closes #389
phases:
  - id: 1
    name: Enhance Docker Image Tagging
    goal: Modify the release workflow to produce semantic version tags with floating major/minor aliases
    tasks:
      - id: TASK-1-1
        description: Update release-images.yml to generate all tag variants (vX.Y.Z, vX.Y, vX)
        acceptance: On release v0.1.0, images are tagged as v0.1.0, v0.1, and v0
        files:
          - .github/workflows/release-images.yml
      - id: TASK-1-2
        description: Add validation to ensure release tags follow semver format
        acceptance: Workflow fails gracefully if tag doesn't match vX.Y.Z pattern
        files:
          - .github/workflows/release-images.yml
      - id: TASK-1-3
        description: Update workflow to skip latest tag update for pre-release versions
        acceptance: Tags with -alpha, -beta, -rc suffix don't update latest
        files:
          - .github/workflows/release-images.yml
  - id: 2
    name: Create Release Automation Script
    goal: Provide a semi-automated release script that handles git tagging and floating tag updates
    tasks:
      - id: TASK-2-1
        description: Create .github/scripts/create-release.sh script
        acceptance: Script validates version format, creates git tag, updates floating tags (vX.Y, vX), and pushes to origin
        files:
          - .github/scripts/create-release.sh
      - id: TASK-2-2
        description: Add --dry-run mode to script for testing
        acceptance: Dry-run shows what would happen without making changes
        files:
          - .github/scripts/create-release.sh
      - id: TASK-2-3
        description: Update script to generate release notes template
        acceptance: Script outputs a markdown template suitable for GitHub release body
        files:
          - .github/scripts/create-release.sh
  - id: 3
    name: Update Action References
    goal: Pin all workflow action references to the @v0 floating tag
    tasks:
      - id: TASK-3-1
        description: Update sdlc-pipeline.yml action references from @main to @v0
        acceptance: All 5 uses jwbron/egg/action@main references updated to @v0
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-3-2
        description: Update reusable-review.yml action reference
        acceptance: uses line updated, action_ref input default updated
        files:
          - .github/workflows/reusable-review.yml
      - id: TASK-3-3
        description: Update reusable-autofix.yml action reference
        acceptance: uses line updated, action_ref input default updated
        files:
          - .github/workflows/reusable-autofix.yml
      - id: TASK-3-4
        description: Update reusable-conflict-resolve.yml action reference
        acceptance: uses line updated, action_ref input default updated
        files:
          - .github/workflows/reusable-conflict-resolve.yml
      - id: TASK-3-5
        description: Update on-review-feedback.yml action reference
        acceptance: uses line updated, action_ref input default updated
        files:
          - .github/workflows/on-review-feedback.yml
      - id: TASK-3-6
        description: Update on-mention.yml action reference
        acceptance: uses line updated, action_ref input default updated
        files:
          - .github/workflows/on-mention.yml
      - id: TASK-3-7
        description: Update on-push-doc-updater.yml action reference
        acceptance: uses line updated, action_ref input default updated
        files:
          - .github/workflows/on-push-doc-updater.yml
      - id: TASK-3-8
        description: Update self-improvement.yml action reference
        acceptance: uses line updated, action_ref input default updated
        files:
          - .github/workflows/self-improvement.yml
  - id: 4
    name: Update Documentation
    goal: Document the versioning policy and release process
    tasks:
      - id: TASK-4-1
        description: Add versioning policy section to main README.md
        acceptance: Documents semver policy, how to pin versions, and what breaking changes mean
        files:
          - README.md
      - id: TASK-4-2
        description: Update docs/guides/reusable-workflows.md with versioning guidance
        acceptance: Examples updated to use @v0, explanation of version pinning added
        files:
          - docs/guides/reusable-workflows.md
      - id: TASK-4-3
        description: Update action/README.md with versioning examples
        acceptance: Quick start example uses versioned reference
        files:
          - action/README.md
      - id: TASK-4-4
        description: Create RELEASING.md documenting the release process
        acceptance: Step-by-step guide for creating releases, including checklist
        files:
          - RELEASING.md
  - id: 5
    name: Initial Release
    goal: Create the v0.1.0 release to enable versioned references
    tasks:
      - id: TASK-5-1
        description: Verify all tests pass on main
        acceptance: CI green on main branch
        files: []
      - id: TASK-5-2
        description: Create v0.1.0 release using the release script
        acceptance: Git tags exist, GitHub release created, images pushed with version tags
        files: []
      - id: TASK-5-3
        description: Verify versioned images are accessible
        acceptance: docker pull ghcr.io/jwbron/egg-sandbox:v0.1.0 succeeds
        files: []
```

---

*Authored-by: egg*
