# Analysis: Support Releases

> Issue: #389 | Phase: refine

## Problem Statement

The egg project currently lacks a formal release versioning strategy. Docker images are built and pushed to GHCR on every push to `main` with the `latest` tag, and workflows reference the action via `@main`. This creates challenges:

1. **No rollback capability**: If a breaking change is deployed, there's no easy way to pin to a known-good version
2. **No version stability**: Consumers of the reusable workflows and Docker images have no guarantee of stability
3. **No changelog tracking**: There's no formal record of what changed between versions

The desired outcome is a tagging system for both Docker images and GitHub workflow references that enables:
- Pinning to specific versions for stability
- Creating releases with changelogs
- Rolling back to previous versions when needed

## Current Behavior

### Docker Images

The `release-images.yml` workflow (`.github/workflows/release-images.yml:1-70`) builds and pushes two images:
- `ghcr.io/jwbron/egg-gateway`
- `ghcr.io/jwbron/egg-sandbox`

Current tagging logic:
- On push to `main`: Tags as `latest`
- On release publish: Tags with release tag name AND `latest`
- On workflow_dispatch: Tags with input tag AND `latest`

The action supports an `image-tag` input (`action/action.yml:50-53`) that defaults to `latest`, which allows consumers to pin to specific image versions.

### Workflow References

There are two categories of workflow references:

**1. Internal reusable workflows** (within this repo):
```yaml
uses: ./.github/workflows/reusable-review.yml
```
These are always pinned to the current branch/commit since they use relative paths.

**2. Action references** (used by this repo and external consumers):
```yaml
uses: jwbron/egg/action@main
```
Found in:
- `reusable-review.yml:464`
- `reusable-autofix.yml:176`
- `reusable-conflict-resolve.yml:178`
- `sdlc-pipeline.yml:516, 1277, 1483, 1919`
- `on-mention.yml:298`
- `on-push-doc-updater.yml:102`
- `on-review-feedback.yml:399`

All action references currently point to `@main`, which means any push to main immediately affects all running workflows.

### Dependencies

The issue notes this "depends on reusable workflows being implemented" - reviewing the codebase shows reusable workflows ARE already implemented:
- `reusable-review.yml`
- `reusable-autofix.yml`
- `reusable-conflict-resolve.yml`

These are called from other workflows using `uses: ./.github/workflows/reusable-*.yml`.

## Constraints

- **GitHub Actions limitation**: The `uses:` field cannot be dynamic - it must be a literal string, so action references cannot be parameterized
- **Image tag propagation**: When creating a release, the action and workflows must agree on which image tag to use
- **Backwards compatibility**: Existing consumers using `@main` should continue to work
- **Dependabot**: Already configured for GitHub Actions version updates, which will help keep pinned versions current

## Options Considered

### Option A: Semantic Version Tags with Major Version Aliases

**Approach**: Use semantic versioning (v1.0.0, v1.0.1, etc.) with major version tags (v1, v2) that float to the latest minor/patch release.

Create releases like `v1.0.0` which:
- Build images tagged as `v1.0.0` and update floating `v1` tag
- Create git tag `v1.0.0` and update floating `v1` tag
- Workflows would reference `@v1` for stability with automatic minor/patch updates

**Pros**:
- Industry standard approach (used by actions/checkout, docker/build-push-action)
- Clear semantic meaning of version bumps
- Major version tags provide stability with security updates
- Dependabot can detect and PR version updates

**Cons**:
- Requires discipline to follow semver correctly
- Need to maintain floating tags (v1 → v1.x.y)
- More complex release process

### Option B: Simple Sequential Tags

**Approach**: Use simple sequential version numbers (v1, v2, v3) without floating tags. Each release is a complete version.

**Pros**:
- Simple to understand and maintain
- No floating tag management
- Clear lineage

**Cons**:
- No semantic meaning to version bumps
- Must update all references for any change
- More frequent dependabot PRs

### Option C: Date-Based Tags

**Approach**: Use date-based tags (2025.01, 2025.02) with monthly releases.

**Pros**:
- Clear timeline of when changes occurred
- Predictable release cadence

**Cons**:
- No indication of breaking vs non-breaking changes
- Unusual pattern for GitHub Actions
- Forced monthly releases may not align with development

## Recommended Approach

**Option A: Semantic Version Tags with Major Version Aliases** is recommended.

Justification:
1. **Industry standard**: This is how major GitHub Actions are versioned (actions/checkout@v4, docker/build-push-action@v6)
2. **Balances stability and updates**: Consumers can pin to `@v1` and get patches/minor updates, or pin to `@v1.2.3` for exact reproducibility
3. **Dependabot integration**: Works well with existing dependabot config - will detect major version bumps
4. **Clear communication**: Version numbers communicate the impact of changes

### Implementation Sketch

1. **Modify `release-images.yml`** to:
   - On release `vX.Y.Z`: Tag images as `vX.Y.Z`, `vX.Y`, and `vX`
   - Keep `latest` as an alias to the most recent release

2. **Update action references** in all workflows from `@main` to `@v1` (after first release)

3. **Create release workflow** or document release process:
   - Tag the release: `git tag v1.0.0 && git push origin v1.0.0`
   - Create GitHub release (triggers image build)
   - Update floating major tag: `git tag -f v1 v1.0.0 && git push -f origin v1`

4. **Document versioning policy** in README:
   - Major: Breaking changes to action inputs, workflow behavior, or image interfaces
   - Minor: New features, backward-compatible changes
   - Patch: Bug fixes, security updates

## Open Questions

**Multiple-choice: Release automation level**

How automated should the release process be?

- [ ] **Fully manual**: Human creates GitHub release, images auto-build, human updates floating tags
- [ ] **Semi-automated**: Human creates release, automation handles all tagging (images + git floating tags)
- [ ] **Fully automated**: Semantic-release or similar tool determines version from commit messages and creates releases automatically
- [ ] Other (explain in reply)

**Multiple-choice: Initial version number**

What should the first release version be?

- [ ] **v0.1.0**: Indicates pre-1.0 stability, allows breaking changes without major bumps
- [ ] **v1.0.0**: Indicates production readiness, follows strict semver from start
- [ ] Other (explain in reply)

**Open-ended questions**:

1. Are there any external consumers of the action/workflows that we need to coordinate with for the version cutover?

2. Should we maintain a CHANGELOG.md file, or rely solely on GitHub release notes?

---

*Authored-by: egg*
