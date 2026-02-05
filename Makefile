# egg Makefile
# =============
# Single entry point for all development tasks.
#
# Native targets run checks directly for fast iteration.
# CI targets (make ci-*) run via act for GitHub Actions parity.

# Virtual environment configuration
VENV_DIR := .venv
VENV_BIN := $(VENV_DIR)/bin
PYTHON := $(VENV_BIN)/python
RUFF := $(VENV_BIN)/ruff
YAMLLINT := $(VENV_BIN)/yamllint

.PHONY: help \
        setup venv install-linters check-linters \
        test test-deps test-quick test-python test-bash \
        lint lint-fix \
        lint-python lint-python-fix \
        lint-mypy \
        lint-shell lint-shell-fix \
        lint-yaml lint-yaml-fix \
        lint-docker lint-workflows \
        lint-container-paths lint-boundary lint-gh-cli lint-claude-imports \
        security \
        build \
        ci ci-lint ci-test ci-integration ci-security

# Default target
help:
	@echo "egg Development Commands"
	@echo "========================"
	@echo ""
	@echo "Setup:"
	@echo "  make setup              - Full development environment setup"
	@echo "  make venv               - Create venv with dev dependencies"
	@echo "  make install-linters    - Install system linting tools"
	@echo "  make check-linters      - Check if linting tools are installed"
	@echo ""
	@echo "Testing:"
	@echo "  make test               - Run all tests (pytest)"
	@echo "  make test-quick         - Quick syntax check (faster)"
	@echo "  make security           - Run security scan (bandit)"
	@echo ""
	@echo "Linting:"
	@echo "  make lint               - Run all linters"
	@echo "  make lint-fix           - Run all linters with auto-fix"
	@echo "  make lint-python        - Lint Python (ruff)"
	@echo "  make lint-mypy          - Type check (mypy)"
	@echo "  make lint-shell         - Lint shell scripts (shellcheck)"
	@echo "  make lint-yaml          - Lint YAML (yamllint)"
	@echo "  make lint-docker        - Lint Dockerfiles (hadolint)"
	@echo "  make lint-workflows     - Lint GitHub Actions (actionlint)"
	@echo "  make lint-container-paths    - Check sys.path patterns"
	@echo "  make lint-boundary      - Check host-container boundary"
	@echo "  make lint-gh-cli        - Check gh CLI usage"
	@echo "  make lint-claude-imports - Check Claude imports"
	@echo ""
	@echo "CI (via act, for GitHub Actions parity):"
	@echo "  make ci                 - Run full CI pipeline via act"
	@echo "  make ci-lint            - Run lint job via act"
	@echo "  make ci-test            - Run unit test job via act"
	@echo "  make ci-integration     - Run integration test job via act"
	@echo "  make ci-security        - Run security scan job via act"
	@echo ""
	@echo "Build:"
	@echo "  make build              - Build Docker images"

# ============================================================================
# Setup
# ============================================================================

# Full development environment setup
setup: venv
	@echo "==> Installing pre-commit hooks..."
	@$(VENV_BIN)/pre-commit install || true
	@echo ""
	@echo "Setup complete! Run 'make help' to see available commands."

# Ensure venv exists and has dev dependencies
venv:
	@if [ ! -f "$(RUFF)" ]; then \
		echo "==> Setting up venv..."; \
		if ! command -v uv >/dev/null 2>&1; then \
			echo "ERROR: uv is not installed."; \
			echo ""; \
			echo "Install uv with:"; \
			echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"; \
			echo ""; \
			echo "Or see: https://docs.astral.sh/uv/getting-started/installation/"; \
			exit 1; \
		fi; \
		uv sync --extra dev; \
	else \
		echo "Dev dependencies already installed."; \
	fi

# Install all linting tools
install-linters: venv
	@echo "Installing linting tools..."
	@echo ""
	@echo "==> ruff and yamllint installed in venv via 'make venv'"
	@echo ""
	@echo "==> Checking for shfmt..."
	@if ! command -v shfmt >/dev/null 2>&1; then \
		echo "shfmt not found. Install with:"; \
		echo "  Ubuntu/Debian: sudo apt-get install shfmt"; \
		echo "  macOS: brew install shfmt"; \
		echo "  Go: go install mvdan.cc/sh/v3/cmd/shfmt@latest"; \
	else \
		echo "shfmt is installed: $$(shfmt --version)"; \
	fi
	@echo ""
	@echo "==> Checking for shellcheck..."
	@if ! command -v shellcheck >/dev/null 2>&1; then \
		echo "shellcheck not found. Install with:"; \
		echo "  Ubuntu/Debian: sudo apt-get install shellcheck"; \
		echo "  macOS: brew install shellcheck"; \
	else \
		echo "shellcheck is installed: $$(shellcheck --version | head -1)"; \
	fi
	@echo ""
	@echo "==> Checking for hadolint..."
	@if ! command -v hadolint >/dev/null 2>&1; then \
		echo "hadolint not found. Install with:"; \
		echo "  macOS: brew install hadolint"; \
		echo "  Linux: Download from https://github.com/hadolint/hadolint/releases"; \
	else \
		echo "hadolint is installed: $$(hadolint --version)"; \
	fi
	@echo ""
	@echo "==> Checking for actionlint..."
	@if ! command -v actionlint >/dev/null 2>&1; then \
		echo "actionlint not found. Install with:"; \
		echo "  macOS: brew install actionlint"; \
		echo "  Go: go install github.com/rhysd/actionlint/cmd/actionlint@latest"; \
	else \
		echo "actionlint is installed: $$(actionlint --version)"; \
	fi
	@echo ""
	@echo "Linting tools installation complete!"

# Check if linting tools are installed
check-linters:
	@echo "Checking linting tools..."
	@echo ""
	@echo -n "ruff (venv): "
	@if [ -f "$(RUFF)" ]; then $(RUFF) --version; else echo "NOT INSTALLED - run 'make venv'"; fi
	@echo -n "yamllint (venv): "
	@if [ -f "$(YAMLLINT)" ]; then $(YAMLLINT) --version; else echo "NOT INSTALLED - run 'make venv'"; fi
	@echo -n "shfmt: "
	@if command -v shfmt >/dev/null 2>&1; then shfmt --version; else echo "NOT INSTALLED"; fi
	@echo -n "shellcheck: "
	@if command -v shellcheck >/dev/null 2>&1; then shellcheck --version | head -1; else echo "NOT INSTALLED"; fi
	@echo -n "hadolint: "
	@if command -v hadolint >/dev/null 2>&1; then hadolint --version; else echo "NOT INSTALLED"; fi
	@echo -n "actionlint: "
	@if command -v actionlint >/dev/null 2>&1; then actionlint --version; else echo "NOT INSTALLED"; fi

# ============================================================================
# Testing
# ============================================================================

# Ensure test dependencies are installed
test-deps:
	@if ! $(PYTHON) -c "import pytest" 2>/dev/null; then \
		echo "==> Installing test dependencies..."; \
		uv sync --extra dev; \
	fi

# Run all tests using pytest
test: test-deps
	@echo "==> Running main tests..."
	PYTHONPATH=shared $(PYTHON) -m pytest tests/ -v
	@echo ""
	@echo "==> Running gateway tests..."
	PYTHONPATH=shared:gateway $(PYTHON) -m pytest gateway/tests/ -v

# Quick syntax-only check (no pytest overhead)
test-quick: test-deps
	$(PYTHON) tests/run_tests.py --quick -v

# Run Python tests only
test-python: test-deps
	PYTHONPATH=shared $(PYTHON) -m pytest tests/test_python_syntax.py -v

# Run Bash tests only
test-bash: test-deps
	PYTHONPATH=shared $(PYTHON) -m pytest tests/test_bash_syntax.py -v

# Security scan with bandit
security: test-deps
	@echo "==> Running security scan with bandit..."
	$(VENV_BIN)/bandit -r gateway shared sandbox -ll -c pyproject.toml

# ============================================================================
# Linting
# ============================================================================

# Shell files: .sh files + bash scripts without .sh extension
SHELL_FILES := $(shell find . -name "*.sh" -not -path "./.venv/*" -not -path "./venv/*" -not -path "./host-services/.venv/*" -not -path "./.git/*" 2>/dev/null)
SANDBOX_SCRIPTS := $(wildcard sandbox/scripts/*)

# Run all linters
lint: lint-python lint-shell lint-yaml lint-docker lint-container-paths lint-boundary lint-gh-cli lint-claude-imports
	@echo ""
	@echo "All linters passed!"

# Run all linters with auto-fix where possible
lint-fix: lint-python-fix lint-shell-fix lint-yaml-fix
	@echo ""
	@echo "==> Running lint to check for remaining issues..."
	@$(MAKE) lint 2>&1 | tee /tmp/lint-output.txt; \
	if grep -q "failed\|error\|Error" /tmp/lint-output.txt 2>/dev/null; then \
		echo ""; \
		echo "Some issues remain. Review and fix manually."; \
	else \
		echo "All linters completed with auto-fixes applied!"; \
	fi

# ----------------------------------------------------------------------------
# Python (ruff)
# ----------------------------------------------------------------------------
lint-python:
	@echo "==> Linting Python files with ruff..."
	@$(RUFF) check . || (echo "Python linting failed. Run 'make lint-python-fix' to auto-fix." && exit 1)
	@$(RUFF) format --check . || (echo "Python formatting issues found. Run 'make lint-python-fix' to auto-fix." && exit 1)
	@echo "Python linting passed!"

lint-python-fix:
	@echo "==> Fixing Python files with ruff..."
	@$(RUFF) check --fix --unsafe-fixes .
	@$(RUFF) format .
	@echo "Python files fixed!"

# ----------------------------------------------------------------------------
# Python type checking (mypy)
# ----------------------------------------------------------------------------
lint-mypy:
	@echo "==> Type checking with mypy..."
	@$(VENV_BIN)/mypy gateway shared sandbox --exclude 'gateway/tests/' || true
	@echo "Mypy check complete (non-blocking)."

# ----------------------------------------------------------------------------
# Shell (shellcheck + shfmt)
# ----------------------------------------------------------------------------
lint-shell:
	@echo "==> Linting shell scripts with shellcheck..."
	@if ! command -v shellcheck >/dev/null 2>&1; then \
		echo "ERROR: shellcheck not installed."; \
		echo "Install with:"; \
		echo "  Fedora/RHEL: sudo dnf install ShellCheck"; \
		echo "  macOS: brew install shellcheck"; \
		exit 1; \
	fi
	@if [ -n "$(SHELL_FILES)" ]; then \
		shellcheck --severity=warning $(SHELL_FILES) || (echo "Shell linting failed!" && exit 1); \
	fi
	@if [ -n "$(SANDBOX_SCRIPTS)" ]; then \
		shellcheck --severity=warning $(SANDBOX_SCRIPTS) || (echo "Sandbox scripts linting failed!" && exit 1); \
	fi
	@echo "Shell linting passed!"

lint-shell-fix:
	@echo "==> Formatting shell scripts with shfmt..."
	@if [ -n "$(SHELL_FILES)" ]; then \
		shfmt -w -i 2 -ci -bn $(SHELL_FILES); \
		echo "Shell scripts formatted!"; \
		echo "==> Running shellcheck..."; \
		shellcheck --severity=warning $(SHELL_FILES) || echo "Some shellcheck issues require manual fixes (see above)."; \
	else \
		echo "No shell scripts found."; \
	fi

# ----------------------------------------------------------------------------
# YAML (yamllint)
# ----------------------------------------------------------------------------
YAML_FILES := $(shell find . \( -name "*.yaml" -o -name "*.yml" \) \
	-not -path "./.venv/*" -not -path "./venv/*" -not -path "./host-services/.venv/*" -not -path "./node_modules/*" -not -path "./.git/*" 2>/dev/null)

lint-yaml:
	@echo "==> Linting YAML files with yamllint..."
	@$(YAMLLINT) -c .yamllint.yaml . || (echo "YAML linting failed!" && exit 1)
	@echo "YAML linting passed!"

lint-yaml-fix:
	@echo "==> Fixing YAML files..."
	@if [ -n "$(YAML_FILES)" ]; then \
		echo "  Removing trailing whitespace..."; \
		for f in $(YAML_FILES); do \
			sed -i 's/[[:space:]]*$$//' "$$f"; \
		done; \
		echo "  Ensuring newline at end of files..."; \
		for f in $(YAML_FILES); do \
			[ -n "$$(tail -c1 "$$f")" ] && echo "" >> "$$f"; \
		done; \
		echo "YAML files fixed!"; \
		echo "==> Running yamllint..."; \
		$(YAMLLINT) -c .yamllint.yaml . || echo "Some YAML issues require manual fixes (see above)."; \
	else \
		echo "No YAML files found."; \
	fi

# ----------------------------------------------------------------------------
# Docker (hadolint)
# ----------------------------------------------------------------------------
DOCKERFILES := $(shell find . -name "Dockerfile*" -not -path "./.venv/*" -not -path "./.git/*" 2>/dev/null)

lint-docker:
	@echo "==> Linting Dockerfiles with hadolint..."
	@if ! command -v hadolint >/dev/null 2>&1; then \
		echo "ERROR: hadolint not installed."; \
		echo "Install with:"; \
		echo "  Fedora/RHEL: sudo dnf install hadolint"; \
		echo "  macOS: brew install hadolint"; \
		exit 1; \
	elif [ -n "$(DOCKERFILES)" ]; then \
		for f in $(DOCKERFILES); do \
			echo "  Checking $$f..."; \
			hadolint --config .hadolint.yaml "$$f" || exit 1; \
		done; \
		echo "Docker linting passed!"; \
	else \
		echo "No Dockerfiles found."; \
	fi

# ----------------------------------------------------------------------------
# GitHub Actions (actionlint)
# ----------------------------------------------------------------------------
lint-workflows:
	@echo "==> Linting GitHub Actions workflows with actionlint..."
	@if [ -d ".github/workflows" ]; then \
		actionlint || (echo "GitHub Actions linting failed!" && exit 1); \
		echo "GitHub Actions linting passed!"; \
	else \
		echo "No .github/workflows directory found."; \
	fi

# ----------------------------------------------------------------------------
# Custom lint scripts
# ----------------------------------------------------------------------------
lint-container-paths:
	@echo "==> Checking for problematic sys.path patterns..."
	@$(PYTHON) scripts/check-container-paths.py

lint-boundary:
	@echo "==> Checking host-container boundary..."
	@$(PYTHON) scripts/check-container-host-boundary.py

lint-gh-cli:
	@echo "==> Checking gh CLI usage..."
	@$(PYTHON) scripts/check-gh-cli-usage.py

lint-claude-imports:
	@echo "==> Checking Claude imports..."
	@$(PYTHON) scripts/check-claude-imports.py

# ============================================================================
# Build
# ============================================================================

build:
	@echo "==> Building gateway container..."
	docker build -t egg-gateway -f gateway/Dockerfile . || echo "WARNING: gateway build failed"
	@echo "==> Building sandbox container..."
	docker build -t egg-sandbox -f sandbox/Dockerfile . || echo "WARNING: sandbox build failed"

# ============================================================================
# CI targets (via act, for GitHub Actions parity)
# ============================================================================

ci:
	@if ! command -v act >/dev/null 2>&1; then \
		echo "ERROR: act is not installed."; \
		echo "Install with:"; \
		echo "  macOS: brew install act"; \
		echo "  Linux: curl -s https://raw.githubusercontent.com/nektos/act/master/install.sh | bash -s -- -b ~/.local/bin"; \
		exit 1; \
	fi
	act push

ci-lint:
	@if ! command -v act >/dev/null 2>&1; then \
		echo "ERROR: act is not installed. See 'make ci' for install instructions."; \
		exit 1; \
	fi
	act -j lint

ci-test:
	@if ! command -v act >/dev/null 2>&1; then \
		echo "ERROR: act is not installed. See 'make ci' for install instructions."; \
		exit 1; \
	fi
	act -j unit

ci-integration:
	@if ! command -v act >/dev/null 2>&1; then \
		echo "ERROR: act is not installed. See 'make ci' for install instructions."; \
		exit 1; \
	fi
	act -j integration

ci-security:
	@if ! command -v act >/dev/null 2>&1; then \
		echo "ERROR: act is not installed. See 'make ci' for install instructions."; \
		exit 1; \
	fi
	act -j security
