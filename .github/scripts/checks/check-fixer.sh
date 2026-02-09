#!/usr/bin/env bash
# check-fixer.sh — Attempt to automatically fix check failures
#
# This script is called after other checks have run and attempts to
# automatically fix any issues that can be auto-corrected.
#
# Environment variables:
#   FIX_LINT      — If "true", run lint auto-fixers (default: true)
#   FIX_FORMAT    — If "true", run code formatters (default: true)
#   AUTO_COMMIT   — If "true", commit fixes automatically (default: false)
#
# Exit codes:
#   0 - Fixes applied successfully (or no fixes needed)
#   1 - Fixes attempted but some issues remain
#   2 - Error running fixers

set -euo pipefail

FIX_LINT="${FIX_LINT:-true}"
FIX_FORMAT="${FIX_FORMAT:-true}"
AUTO_COMMIT="${AUTO_COMMIT:-false}"

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

log_info() {
    echo "[check-fixer] $*"
}

log_error() {
    echo "[check-fixer] ERROR: $*" >&2
}

fixes_applied=false

run_fixer() {
    local name="$1"
    shift
    local -a command=("$@")

    log_info "Running: ${name}"

    set +e
    "${command[@]}" 2>&1
    local exit_code=$?
    set -e

    if [[ $exit_code -eq 0 ]]; then
        log_info "${name} completed"
        fixes_applied=true
    else
        log_info "${name} encountered issues (exit code: ${exit_code})"
    fi
}

# ---------------------------------------------------------------------------
# Run fixers
# ---------------------------------------------------------------------------

log_info "Starting auto-fix process..."

# Check for Makefile fix target
if [[ -f "Makefile" ]] && grep -qE '^fix:' Makefile; then
    run_fixer "make fix" make fix
else
    # Run individual fixers

    # Python formatting and linting
    if [[ "$FIX_LINT" == "true" ]] || [[ "$FIX_FORMAT" == "true" ]]; then
        if [[ -f "pyproject.toml" ]] || [[ -f "setup.py" ]] || find . -maxdepth 2 -name "*.py" -type f | head -1 | grep -q .; then
            # Ruff (preferred - handles both linting and formatting)
            if command -v ruff &> /dev/null; then
                if [[ "$FIX_LINT" == "true" ]]; then
                    run_fixer "ruff check --fix" ruff check . --fix
                fi
                if [[ "$FIX_FORMAT" == "true" ]]; then
                    run_fixer "ruff format" ruff format .
                fi
            else
                # Fallback to individual tools
                if [[ "$FIX_FORMAT" == "true" ]]; then
                    if command -v black &> /dev/null; then
                        run_fixer "black" black .
                    fi
                    if command -v isort &> /dev/null; then
                        run_fixer "isort" isort .
                    fi
                fi
                if [[ "$FIX_LINT" == "true" ]]; then
                    if command -v autopep8 &> /dev/null; then
                        run_fixer "autopep8" autopep8 --in-place --recursive .
                    fi
                fi
            fi
        fi
    fi

    # JavaScript/TypeScript
    if [[ -f "package.json" ]]; then
        if [[ "$FIX_LINT" == "true" ]]; then
            # ESLint with fix
            if [[ -f ".eslintrc.js" ]] || [[ -f ".eslintrc.json" ]] || grep -q '"eslint"' package.json 2>/dev/null; then
                if npm list eslint &> /dev/null 2>&1 || [[ -f "node_modules/.bin/eslint" ]]; then
                    run_fixer "eslint --fix" npx eslint . --ext .js,.jsx,.ts,.tsx --fix
                fi
            fi
        fi

        if [[ "$FIX_FORMAT" == "true" ]]; then
            # Prettier
            if [[ -f ".prettierrc" ]] || [[ -f ".prettierrc.json" ]] || grep -q '"prettier"' package.json 2>/dev/null; then
                if npm list prettier &> /dev/null 2>&1 || [[ -f "node_modules/.bin/prettier" ]]; then
                    run_fixer "prettier" npx prettier --write .
                fi
            fi
        fi
    fi

    # Go
    if [[ -f "go.mod" ]] && command -v go &> /dev/null; then
        if [[ "$FIX_FORMAT" == "true" ]]; then
            run_fixer "gofmt" gofmt -w .
        fi
        if [[ "$FIX_LINT" == "true" ]]; then
            run_fixer "go mod tidy" go mod tidy
        fi
    fi

    # Rust
    if [[ -f "Cargo.toml" ]] && command -v cargo &> /dev/null; then
        if [[ "$FIX_FORMAT" == "true" ]]; then
            run_fixer "cargo fmt" cargo fmt
        fi
        if [[ "$FIX_LINT" == "true" ]]; then
            run_fixer "cargo clippy --fix" cargo clippy --fix --allow-dirty --allow-staged
        fi
    fi
fi

# ---------------------------------------------------------------------------
# Check for changes
# ---------------------------------------------------------------------------

if ! git diff --quiet 2>/dev/null; then
    log_info "Fixes applied - changes detected in working tree"
    git diff --stat

    if [[ "$AUTO_COMMIT" == "true" ]]; then
        log_info "Auto-committing fixes..."
        git add -A
        git commit -m "Auto-fix: Apply lint and format fixes

Authored-by: egg"
        log_info "Fixes committed"
    else
        log_info "Changes not committed (AUTO_COMMIT=false)"
        log_info "Review and commit the changes manually if appropriate"
    fi
else
    if [[ "$fixes_applied" == "true" ]]; then
        log_info "Fixers ran but no changes were made"
    else
        log_info "No fixers were applicable"
    fi
fi

log_info "Auto-fix process complete"
exit 0
