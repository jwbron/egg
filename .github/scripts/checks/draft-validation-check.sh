#!/usr/bin/env bash
# draft-validation-check.sh — Validate that a draft document exists and has expected sections
#
# This check ensures that the draft output file exists and contains the
# expected structure for the current phase.
#
# Environment variables:
#   EGG_ISSUE_NUMBER — Issue number (required)
#   EGG_PHASE        — Pipeline phase (refine, plan, implement) (required)
#   DRAFT_PATH       — Override path to draft file (optional)
#
# Exit codes:
#   0 - Draft is valid
#   1 - Draft is missing or invalid
#   2 - Missing required environment variables

set -euo pipefail

# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------

if [[ -z "${EGG_ISSUE_NUMBER:-}" ]]; then
    echo "[draft-validation] ERROR: EGG_ISSUE_NUMBER is required" >&2
    exit 2
fi

if [[ -z "${EGG_PHASE:-}" ]]; then
    echo "[draft-validation] ERROR: EGG_PHASE is required" >&2
    exit 2
fi

# ---------------------------------------------------------------------------
# Determine draft path and expected sections
# ---------------------------------------------------------------------------

case "$EGG_PHASE" in
    refine)
        DRAFT_PATH="${DRAFT_PATH:-.egg-state/drafts/${EGG_ISSUE_NUMBER}-analysis.md}"
        EXPECTED_SECTIONS=(
            "Problem"
            "Analysis"
            "Recommendation"
        )
        OPTIONAL_SECTIONS=(
            "Current State"
            "Options"
            "Open Questions"
            "Constraints"
            "Dependencies"
        )
        MIN_LENGTH=500
        ;;
    plan)
        DRAFT_PATH="${DRAFT_PATH:-.egg-state/drafts/${EGG_ISSUE_NUMBER}-plan.md}"
        EXPECTED_SECTIONS=(
            "Summary"
            "Implementation"
            "Test Strategy"
        )
        OPTIONAL_SECTIONS=(
            "Phase"
            "Tasks"
            "Risk"
            "Rollback"
        )
        MIN_LENGTH=800
        # Plan must have YAML tasks block
        REQUIRE_YAML=true
        ;;
    implement)
        # Implement phase doesn't have a traditional draft
        # Skip validation or check for implementation notes
        DRAFT_PATH="${DRAFT_PATH:-.egg-state/drafts/${EGG_ISSUE_NUMBER}-implementation.md}"
        EXPECTED_SECTIONS=()
        OPTIONAL_SECTIONS=()
        MIN_LENGTH=0
        OPTIONAL_FILE=true
        ;;
    *)
        echo "[draft-validation] ERROR: Unknown phase: ${EGG_PHASE}" >&2
        exit 2
        ;;
esac

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

echo "[draft-validation] Checking draft: ${DRAFT_PATH}"
echo "[draft-validation] Phase: ${EGG_PHASE}"

# Check if file exists
if [[ ! -f "$DRAFT_PATH" ]]; then
    if [[ "${OPTIONAL_FILE:-false}" == "true" ]]; then
        echo "[draft-validation] Draft file not found but optional for ${EGG_PHASE} phase"
        exit 0
    fi
    echo "[draft-validation] ERROR: Draft file not found: ${DRAFT_PATH}" >&2
    exit 1
fi

# Read draft content
draft_content=$(cat "$DRAFT_PATH")
draft_length=${#draft_content}

echo "[draft-validation] Draft length: ${draft_length} characters"

# Check minimum length
if [[ $draft_length -lt $MIN_LENGTH ]]; then
    echo "[draft-validation] ERROR: Draft is too short (${draft_length} < ${MIN_LENGTH} chars)" >&2
    exit 1
fi

# Check for expected sections (case-insensitive header matching)
missing_sections=()
found_sections=()

for section in "${EXPECTED_SECTIONS[@]}"; do
    # Look for markdown headers containing the section name
    if echo "$draft_content" | grep -iE "^#+.*${section}" > /dev/null 2>&1; then
        found_sections+=("$section")
    else
        missing_sections+=("$section")
    fi
done

echo "[draft-validation] Found sections: ${found_sections[*]:-none}"

if [[ ${#missing_sections[@]} -gt 0 ]]; then
    echo "[draft-validation] WARNING: Missing expected sections: ${missing_sections[*]}" >&2
    # Allow some flexibility - only fail if majority are missing
    found_count=${#found_sections[@]}
    expected_count=${#EXPECTED_SECTIONS[@]}
    if [[ $found_count -lt $((expected_count / 2)) ]]; then
        echo "[draft-validation] ERROR: Too many expected sections missing" >&2
        exit 1
    fi
fi

# Check for YAML tasks block in plan phase
if [[ "${REQUIRE_YAML:-false}" == "true" ]]; then
    if ! echo "$draft_content" | grep -q '```yaml' && ! echo "$draft_content" | grep -q '# yaml-tasks'; then
        echo "[draft-validation] ERROR: Plan must contain YAML tasks block" >&2
        echo "[draft-validation] Expected: \`\`\`yaml block with # yaml-tasks marker" >&2
        exit 1
    fi
    echo "[draft-validation] YAML tasks block found"
fi

# Check for common issues
if echo "$draft_content" | grep -qE 'TODO|FIXME|XXX|PLACEHOLDER'; then
    echo "[draft-validation] WARNING: Draft contains TODO/FIXME markers" >&2
fi

# Check for incomplete template markers
if echo "$draft_content" | grep -qE '\[.*\.\.\.\]|\{.*\.\.\.\}|<.*\.\.\.>'; then
    echo "[draft-validation] WARNING: Draft may contain unfilled template placeholders" >&2
fi

echo "[draft-validation] Draft validation passed"
exit 0
