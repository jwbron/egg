#!/usr/bin/env bash
# test-check.sh — Run tests and capture results
#
# This check runs the project's test suite and captures any failures.
# It auto-detects the test framework and runs appropriate commands.
#
# Environment variables:
#   TEST_COMMAND — Override the test command (optional)
#                  SECURITY NOTE: This variable is passed to `bash -c` for execution.
#                  Only set this from trusted sources (workflow inputs, not PR content).
#   SKIP_TESTS   — Set to "true" to skip tests (optional)
#   TEST_PATTERN — Pattern to filter tests (optional)
#
# Exit codes:
#   0 - All tests passed
#   1 - Test failures
#   2 - Test infrastructure error

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

if [[ "${SKIP_TESTS:-false}" == "true" ]]; then
    echo "[test-check] Tests skipped (SKIP_TESTS=true)"
    exit 0
fi

# ---------------------------------------------------------------------------
# Detect and run tests
# ---------------------------------------------------------------------------

test_exit_code=0

run_tests() {
    local name="$1"
    shift
    local -a command=("$@")

    echo "[test-check] Running: ${name}"
    echo "[test-check] Command: ${command[*]}"

    local exit_code=0

    set +e
    "${command[@]}"
    exit_code=$?
    set -e

    if [[ $exit_code -ne 0 ]]; then
        echo "[test-check] ${name} failed (exit code: ${exit_code})"
        test_exit_code=1
    else
        echo "[test-check] ${name} passed"
    fi

    return $exit_code
}

# Use override command if provided
if [[ -n "${TEST_COMMAND:-}" ]]; then
    # For custom commands, we need to use bash -c to handle complex commands
    run_tests "Custom tests" bash -c "$TEST_COMMAND"
else
    # Auto-detect test framework

    # Check for Makefile test target (most common)
    if [[ -f "Makefile" ]] && grep -qE '^test:' Makefile; then
        run_tests "make test" make test
    else
        # Detect based on project files
        found_tests=false

        # Python (pytest, unittest)
        if [[ -f "pytest.ini" ]] || [[ -f "pyproject.toml" ]] || [[ -f "setup.py" ]] || [[ -d "tests" ]]; then
            if command -v pytest &> /dev/null || [[ -f ".venv/bin/pytest" ]]; then
                found_tests=true

                # Build pytest command with optional pattern
                if [[ -n "${TEST_PATTERN:-}" ]]; then
                    run_tests "pytest" pytest -k "$TEST_PATTERN" --tb=short -q
                else
                    run_tests "pytest" pytest --tb=short -q
                fi
            elif command -v python3 &> /dev/null && [[ -d "tests" ]]; then
                found_tests=true
                run_tests "python unittest" python3 -m unittest discover -s tests
            fi
        fi

        # JavaScript/TypeScript (jest, mocha, vitest)
        if [[ -f "package.json" ]]; then
            # Check for test script in package.json
            if grep -q '"test":' package.json; then
                found_tests=true
                run_tests "npm test" npm test -- --passWithNoTests
            else
                # Check for specific test frameworks
                if grep -q '"jest"' package.json || [[ -f "jest.config.js" ]] || [[ -f "jest.config.ts" ]]; then
                    found_tests=true
                    if [[ -n "${TEST_PATTERN:-}" ]]; then
                        run_tests "jest" npx jest --testNamePattern="$TEST_PATTERN"
                    else
                        run_tests "jest" npx jest --passWithNoTests
                    fi
                elif grep -q '"vitest"' package.json || [[ -f "vitest.config.ts" ]]; then
                    found_tests=true
                    run_tests "vitest" npx vitest run
                elif grep -q '"mocha"' package.json; then
                    found_tests=true
                    run_tests "mocha" npx mocha
                fi
            fi
        fi

        # Go
        if [[ -f "go.mod" ]]; then
            found_tests=true
            if [[ -n "${TEST_PATTERN:-}" ]]; then
                run_tests "go test" go test ./... -run "$TEST_PATTERN"
            else
                run_tests "go test" go test ./...
            fi
        fi

        # Rust
        if [[ -f "Cargo.toml" ]]; then
            found_tests=true
            if [[ -n "${TEST_PATTERN:-}" ]]; then
                run_tests "cargo test" cargo test "$TEST_PATTERN"
            else
                run_tests "cargo test" cargo test
            fi
        fi

        # Ruby (rspec, minitest)
        if [[ -f "Gemfile" ]]; then
            if grep -q 'rspec' Gemfile || [[ -d "spec" ]]; then
                found_tests=true
                run_tests "rspec" bundle exec rspec
            elif [[ -d "test" ]]; then
                found_tests=true
                run_tests "minitest" bundle exec rake test
            fi
        fi

        # Java (maven, gradle)
        if [[ -f "pom.xml" ]]; then
            found_tests=true
            run_tests "mvn test" mvn test -B
        elif [[ -f "build.gradle" ]] || [[ -f "build.gradle.kts" ]]; then
            found_tests=true
            run_tests "gradle test" ./gradlew test
        fi

        # If no tests found, report success
        if [[ "$found_tests" == "false" ]]; then
            echo "[test-check] No test framework detected"
            echo "[test-check] Checked for: pytest, jest, vitest, mocha, go test, cargo test, rspec, minitest, maven, gradle"
            echo "[test-check] To fix: Add a 'test' target to Makefile or set TEST_COMMAND environment variable"
            # Don't fail - missing tests might be intentional
            exit 0
        fi
    fi
fi

# ---------------------------------------------------------------------------
# Report results
# ---------------------------------------------------------------------------

if [[ $test_exit_code -eq 0 ]]; then
    echo "[test-check] All tests passed"
else
    echo "[test-check] Test failures detected"
fi

exit $test_exit_code
