#!/usr/bin/env bash
#
# create-release.sh - Create a new release with semantic versioning
#
# Usage: ./create-release.sh [--dry-run] <version>
#
# Examples:
#   ./create-release.sh v0.1.0         # Create release v0.1.0
#   ./create-release.sh --dry-run v1.0.0  # Show what would happen
#
# This script:
#   1. Validates the version follows semver (vX.Y.Z)
#   2. Creates the version tag (v0.1.0)
#   3. Updates/creates floating tags (v0.1, v0) for stable releases only
#   4. Pushes all tags to origin
#   5. Outputs a release notes template

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

DRY_RUN=false
VERSION=""

usage() {
    echo "Usage: $0 [--dry-run] <version>"
    echo ""
    echo "Options:"
    echo "  --dry-run    Show what would happen without making changes"
    echo ""
    echo "Arguments:"
    echo "  version      Semantic version (e.g., v0.1.0, v1.0.0-beta)"
    echo ""
    echo "Examples:"
    echo "  $0 v0.1.0"
    echo "  $0 --dry-run v1.0.0"
    exit 1
}

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1" >&2
}

run_cmd() {
    if [[ "$DRY_RUN" == "true" ]]; then
        echo -e "${YELLOW}[DRY-RUN]${NC} Would run: $*"
    else
        "$@"
    fi
}

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        -h|--help)
            usage
            ;;
        -*)
            log_error "Unknown option: $1"
            usage
            ;;
        *)
            if [[ -z "$VERSION" ]]; then
                VERSION="$1"
            else
                log_error "Unexpected argument: $1"
                usage
            fi
            shift
            ;;
    esac
done

if [[ -z "$VERSION" ]]; then
    log_error "Version is required"
    usage
fi

# Validate semver format
if [[ ! "$VERSION" =~ ^v([0-9]+)\.([0-9]+)\.([0-9]+)(-[a-zA-Z0-9.]+)?$ ]]; then
    log_error "Invalid version format: $VERSION"
    echo "Version must match semver: vX.Y.Z or vX.Y.Z-prerelease"
    echo "Examples: v0.1.0, v1.0.0, v2.0.0-beta, v1.0.0-rc.1"
    exit 1
fi

MAJOR="${BASH_REMATCH[1]}"
MINOR="${BASH_REMATCH[2]}"
PRERELEASE="${BASH_REMATCH[4]}"

MAJOR_TAG="v${MAJOR}"
MINOR_TAG="v${MAJOR}.${MINOR}"

IS_PRERELEASE=false
if [[ -n "$PRERELEASE" ]]; then
    IS_PRERELEASE=true
fi

# Check we're in a git repo
if ! git rev-parse --git-dir > /dev/null 2>&1; then
    log_error "Not in a git repository"
    exit 1
fi

# Check for uncommitted changes
if ! git diff-index --quiet HEAD -- 2>/dev/null; then
    log_error "There are uncommitted changes. Please commit or stash them first."
    exit 1
fi

# Warn if not on main branch
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [[ "$CURRENT_BRANCH" != "main" ]]; then
    log_warn "Creating release from branch '$CURRENT_BRANCH' (not main)"
    read -p "Continue? [y/N] " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

# Fetch latest tags
log_info "Fetching latest tags from origin..."
if [[ "$DRY_RUN" != "true" ]]; then
    git fetch --tags origin
fi

# Check if version tag already exists
if git rev-parse "$VERSION" > /dev/null 2>&1; then
    log_error "Tag $VERSION already exists"
    exit 1
fi

# Get current commit
CURRENT_SHA=$(git rev-parse HEAD)
CURRENT_SHA_SHORT=$(git rev-parse --short HEAD)

echo ""
log_info "Release Configuration:"
echo "  Version:      $VERSION"
echo "  Major tag:    $MAJOR_TAG"
echo "  Minor tag:    $MINOR_TAG"
echo "  Commit:       $CURRENT_SHA_SHORT"
echo "  Branch:       $CURRENT_BRANCH"
echo "  Pre-release:  $IS_PRERELEASE"
echo ""

if [[ "$DRY_RUN" == "true" ]]; then
    log_warn "DRY-RUN MODE - No changes will be made"
    echo ""
fi

# Create the version tag
log_info "Creating tag $VERSION..."
run_cmd git tag -a "$VERSION" -m "Release $VERSION"

# Update floating tags only for stable releases
if [[ "$IS_PRERELEASE" == "false" ]]; then
    log_info "Updating floating tag $MINOR_TAG..."
    run_cmd git tag -f "$MINOR_TAG" -m "Floating tag for ${MAJOR}.${MINOR}.x releases"

    log_info "Updating floating tag $MAJOR_TAG..."
    run_cmd git tag -f "$MAJOR_TAG" -m "Floating tag for ${MAJOR}.x.x releases"
else
    log_info "Skipping floating tag updates for pre-release"
fi

# Push tags
log_info "Pushing tags to origin..."
run_cmd git push origin "$VERSION"
if [[ "$IS_PRERELEASE" == "false" ]]; then
    run_cmd git push -f origin "$MINOR_TAG"
    run_cmd git push -f origin "$MAJOR_TAG"
fi

echo ""
if [[ "$DRY_RUN" == "true" ]]; then
    log_success "Dry run complete. Run without --dry-run to create the release."
else
    log_success "Tags created and pushed successfully!"
fi

# Generate release notes template
echo ""
echo "=========================================="
echo "Release Notes Template"
echo "=========================================="
echo ""

# Get commits since last tag (or all if no tags)
PREV_TAG=$(git describe --tags --abbrev=0 HEAD^ 2>/dev/null || echo "")

# Sanitize PREV_TAG for use in output (strip control characters)
PREV_TAG_DISPLAY="${PREV_TAG//[^a-zA-Z0-9._-]/}"

cat << EOF
## $VERSION

### Highlights

<!-- Add 2-3 bullet points summarizing the key changes -->

### Changes

EOF

if [[ -n "$PREV_TAG" ]]; then
    echo "<!-- Commits since $PREV_TAG_DISPLAY -->"
    if [[ "$DRY_RUN" != "true" ]]; then
        git log --oneline "$PREV_TAG"..HEAD | sed 's/^/- /'
    else
        echo "<!-- (dry-run: commit list would appear here) -->"
    fi
else
    echo "<!-- This is the first release -->"
    if [[ "$DRY_RUN" != "true" ]]; then
        git log --oneline -10 | sed 's/^/- /'
        echo "<!-- ... and more -->"
    fi
fi

cat << EOF

### Docker Images

\`\`\`bash
docker pull ghcr.io/jwbron/egg-sandbox:$VERSION
docker pull ghcr.io/jwbron/egg-gateway:$VERSION
\`\`\`

### Versioned References

For stability, pin to the major version:
\`\`\`yaml
uses: jwbron/egg/action@$MAJOR_TAG
\`\`\`

For full reproducibility:
\`\`\`yaml
uses: jwbron/egg/action@$VERSION
\`\`\`
EOF

if [[ "$IS_PRERELEASE" == "true" ]]; then
    echo ""
    echo "---"
    echo "**Note:** This is a pre-release version ($PRERELEASE)."
fi

echo ""
echo "=========================================="

if [[ "$DRY_RUN" != "true" ]]; then
    echo ""
    log_info "Next steps:"
    echo "  1. Go to: https://github.com/jwbron/egg/releases/new?tag=$VERSION"
    echo "  2. Copy the release notes template above"
    echo "  3. Edit and publish the release"
fi
