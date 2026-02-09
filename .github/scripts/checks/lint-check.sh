#!/usr/bin/env bash
# lint-check.sh — Run linters and capture results
#
# This check runs the project's linters (if configured) and captures
# any failures. It looks for common linter configurations and runs
# the appropriate commands.
#
# Environment variables:
#   LINT_COMMAND — Override the lint command (optional)
#                  SECURITY NOTE: This variable is passed to `bash -c` for execution.
#                  Only set this from trusted sources (workflow inputs, not PR content).
#   SKIP_LINT    — Set to "true" to skip linting (optional)
#
# Exit codes:
#   0 - All linters passed
#   1 - Lint errors found
#   2 - Lint infrastructure error

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

if [[ "${SKIP_LINT:-false}" == "true" ]]; then
    echo "[lint-check] Linting skipped (SKIP_LINT=true)"
    exit 0
fi

# ---------------------------------------------------------------------------
# Detect and run linters
# ---------------------------------------------------------------------------

lint_exit_code=0
lint_output=""

run_lint() {
    local name="$1"
    shift
    local -a command=("$@")

    echo "[lint-check] Running: ${name}"
    echo "[lint-check] Command: ${command[*]}"

    local output
    local exit_code=0

    set +e
    output=$("${command[@]}" 2>&1)
    exit_code=$?
    set -e

    if [[ $exit_code -ne 0 ]]; then
        echo "[lint-check] ${name} failed (exit code: ${exit_code})"
        echo "$output"
        lint_output+="=== ${name} ===\n${output}\n\n"
        lint_exit_code=1
    else
        echo "[lint-check] ${name} passed"
    fi
}

# Use override command if provided
if [[ -n "${LINT_COMMAND:-}" ]]; then
    # For custom commands, we need to use bash -c to handle complex commands
    run_lint "Custom lint" bash -c "$LINT_COMMAND"
else
    # Auto-detect linters

    # Check for Makefile lint target
    if [[ -f "Makefile" ]] && grep -qE '^lint:' Makefile; then
        run_lint "make lint" make lint
    else
        # Run individual linters based on project files

        # Python (ruff, flake8, black, mypy)
        if [[ -f "pyproject.toml" ]] || [[ -f "setup.py" ]] || find . -maxdepth 2 -name "*.py" -type f | head -1 | grep -q .; then
            # Prefer ruff if available
            if command -v ruff &> /dev/null; then
                run_lint "ruff check" ruff check .
            elif command -v flake8 &> /dev/null; then
                run_lint "flake8" flake8 .
            fi

            # Run mypy if configured
            if [[ -f "mypy.ini" ]] || [[ -f ".mypy.ini" ]] || grep -q '\[tool.mypy\]' pyproject.toml 2>/dev/null; then
                if command -v mypy &> /dev/null; then
                    run_lint "mypy" mypy .
                fi
            fi
        fi

        # JavaScript/TypeScript (eslint)
        if [[ -f "package.json" ]]; then
            if [[ -f ".eslintrc.js" ]] || [[ -f ".eslintrc.json" ]] || [[ -f ".eslintrc.yml" ]] || [[ -f ".eslintrc" ]] || grep -q '"eslint"' package.json 2>/dev/null; then
                if [[ -f "node_modules/.bin/eslint" ]]; then
                    run_lint "eslint" npx eslint . --ext .js,.jsx,.ts,.tsx
                elif command -v npx &> /dev/null && npm list eslint &> /dev/null; then
                    run_lint "eslint" npx eslint . --ext .js,.jsx,.ts,.tsx
                fi
            fi

            # TypeScript type checking
            if [[ -f "tsconfig.json" ]]; then
                if [[ -f "node_modules/.bin/tsc" ]]; then
                    run_lint "tsc" npx tsc --noEmit
                elif command -v npx &> /dev/null && npm list typescript &> /dev/null; then
                    run_lint "tsc" npx tsc --noEmit
                fi
            fi
        fi

        # Go (golangci-lint, go vet)
        if [[ -f "go.mod" ]]; then
            if command -v golangci-lint &> /dev/null; then
                run_lint "golangci-lint" golangci-lint run
            elif command -v go &> /dev/null; then
                run_lint "go vet" go vet ./...
            fi
        fi

        # Rust (clippy, cargo check)
        if [[ -f "Cargo.toml" ]]; then
            if command -v cargo &> /dev/null; then
                run_lint "cargo clippy" cargo clippy -- -D warnings
            fi
        fi

        # Shell (shellcheck)
        if find . -maxdepth 3 -name "*.sh" -type f | head -1 | grep -q .; then
            if command -v shellcheck &> /dev/null; then
                run_lint "shellcheck" bash -c "find . -name '*.sh' -type f -not -path '*/node_modules/*' -not -path '*/.git/*' -exec shellcheck {} +"
            fi
        fi

        # YAML (yamllint)
        if [[ -f ".yamllint.yaml" ]] || [[ -f ".yamllint.yml" ]] || [[ -f ".yamllint" ]]; then
            if command -v yamllint &> /dev/null; then
                run_lint "yamllint" yamllint .
            fi
        fi
    fi
fi

# ---------------------------------------------------------------------------
# Report results
# ---------------------------------------------------------------------------

if [[ $lint_exit_code -eq 0 ]]; then
    echo "[lint-check] All linters passed"
else
    echo "[lint-check] Lint errors found"
    echo -e "$lint_output"
fi

exit $lint_exit_code
