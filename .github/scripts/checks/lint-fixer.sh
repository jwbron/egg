#!/usr/bin/env bash
# lint-fixer.sh — Attempt to automatically fix lint errors
#
# This is a simple wrapper around check-fixer.sh that only runs lint fixers.
#
# Exit codes:
#   0 - Fixes applied successfully (or no fixes needed)
#   1 - Some issues could not be auto-fixed

set -euo pipefail

# Get the directory of this script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Run check-fixer with lint-only settings
export FIX_LINT=true
export FIX_FORMAT=true  # Format fixes often resolve lint issues
export AUTO_COMMIT=false

exec "${SCRIPT_DIR}/check-fixer.sh"
