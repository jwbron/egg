# Releasing

This document describes the release process for egg.

## Version Scheme

egg uses [semantic versioning](https://semver.org/):

- **Major version (vX.0.0)**: Breaking changes to workflows, action inputs/outputs, or gateway API
- **Minor version (v0.X.0)**: New features, backward-compatible enhancements
- **Patch version (v0.0.X)**: Bug fixes, documentation updates

### Pre-1.0 Releases

During the v0.x.y phase, minor versions may contain breaking changes. Pin to exact versions for maximum stability.

### Pre-release Versions

Use suffixes for pre-release versions:
- `-alpha.N`: Early development, may be unstable
- `-beta.N`: Feature complete, seeking feedback
- `-rc.N`: Release candidate, final testing

Pre-release versions do not update floating tags (`vX`, `vX.Y`) or the `latest` Docker tag.

## Release Artifacts

Each release produces:

| Artifact | Tags |
|----------|------|
| Docker images | `vX.Y.Z`, `vX.Y`, `vX`, `latest` (stable only) |
| Git tags | `vX.Y.Z`, `vX.Y`, `vX` (floating, stable only) |
| GitHub Release | `vX.Y.Z` with changelog |

Pre-release versions only produce the exact version tag (`vX.Y.Z-suffix`).

## Creating a Release

### Prerequisites

- All tests passing on main
- No critical open issues
- CHANGELOG.md updated (if applicable)

### Using the Release Script

```bash
# Dry run first
.github/scripts/create-release.sh --dry-run v0.2.0

# Create the release
.github/scripts/create-release.sh v0.2.0
```

The script will:
1. Validate the version format
2. Warn if not running from the `main` branch
3. Create the version tag (v0.2.0)
4. Update floating tags (v0.2, v0) — skipped for pre-releases
5. Push all tags to origin
6. Output a release notes template

### Creating the GitHub Release

After running the script:

1. Go to https://github.com/jwbron/egg/releases/new?tag=vX.Y.Z
2. Copy the release notes template from the script output
3. Edit the highlights and changelog sections
4. For pre-release versions, check "Set as a pre-release"
5. Publish the release

The `release-images.yml` workflow will automatically build and push Docker images with all version tags.

## Release Checklist

Before releasing:

- [ ] All CI checks passing on main
- [ ] Version number follows semver
- [ ] Breaking changes documented (if any)
- [ ] Migration notes included for breaking changes

During release:

- [ ] Run `create-release.sh --dry-run` to verify
- [ ] Run `create-release.sh` to create and push tags
- [ ] Create GitHub release with changelog
- [ ] Verify Docker images are pushed

After release:

- [ ] Verify `docker pull ghcr.io/jwbron/egg-sandbox:vX.Y.Z` works
- [ ] Verify `@vX` floating tag is updated
- [ ] Notify users of breaking changes (if any)

## Rollback

### Bad Release

If a release is broken:

```bash
# Delete the bad tags (example: rolling back v0.2.0)
git push --delete origin v0.2.0 v0.2 v0
git tag -d v0.2.0 v0.2 v0

# Point floating tags to the last good release (e.g., v0.1.3)
git tag -f v0.1 v0.1.3
git tag -f v0 v0.1.3
git push -f origin v0.1 v0
```

Note: The rollback target should be the last known-good release in the
previous minor series. Adjust `v0.1.3` to whatever your actual last good
release was.

### Emergency Hotfix

For critical bugs in a released version:

1. Create a hotfix branch from the release tag
2. Fix the issue
3. Release as vX.Y.Z+1 patch version
4. Floating tags will update automatically

## Dependabot

External consumers using Dependabot will receive automatic PRs when:
- New major versions are released (requires manual merge)
- New minor/patch versions are released (can auto-merge if configured)

Configure in consumer repos:
```yaml
# .github/dependabot.yml
version: 2
updates:
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "weekly"
```
