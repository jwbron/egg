# egg Makefile
# =============
# Single entry point for common development tasks

# Virtual environment configuration
VENV_DIR := .venv
VENV_BIN := $(VENV_DIR)/bin
PYTHON := $(VENV_BIN)/python
RUFF := $(VENV_BIN)/ruff
YAMLLINT := $(VENV_BIN)/yamllint

.PHONY: help \
        test test-deps test-quick test-python test-bash \
        lint lint-fix \
        lint-python lint-python-fix \
        lint-shell lint-shell-fix \
        lint-yaml lint-yaml-fix \
        lint-docker lint-workflows \
        lint-host-services lint-container-paths lint-bin-symlinks \
        lint-claude-imports lint-container-host-boundary lint-gh-cli-usage lint-validate-config \
        install-linters check-linters venv

# Default target
help:
	@echo "egg Development Commands"
	@echo "========================"
	@echo ""
	@echo "Testing:"
	@echo "  make test              - Run all tests (pytest)"
	@echo "  make test-quick        - Quick syntax check (faster)"
	@echo "  make test-python       - Run Python syntax tests only"
	@echo "  make test-bash         - Run Bash syntax tests only"
	@echo ""
	@echo "Linting:"
	@echo "  make lint              - Run all linters"
	@echo "  make lint-fix          - Run all linters with auto-fix"
	@echo "  make lint-python       - Lint Python files with ruff"
	@echo "  make lint-python-fix   - Lint and fix Python files"
	@echo "  make lint-shell        - Lint shell scripts with shellcheck"
	@echo "  make lint-shell-fix    - Format shell scripts with shfmt"
	@echo "  make lint-yaml         - Lint YAML files with yamllint"
	@echo "  make lint-yaml-fix     - Fix common YAML issues (trailing spaces)"
	@echo "  make lint-docker       - Lint Dockerfiles with hadolint"
	@echo "  make lint-workflows    - Lint GitHub Actions with actionlint"
	@echo "  make lint-container-paths - Check for problematic sys.path patterns"
	@echo "  make lint-bin-symlinks  - Check container bin symlinks are valid"
	@echo "  make lint-claude-imports - Check host-services for forbidden Claude imports"
	@echo "  make lint-container-host-boundary - Check sandbox for host-services imports"
	@echo "  make lint-gh-cli-usage  - Check host-services for forbidden gh CLI writes"
	@echo "  make lint-validate-config - Validate configuration loading"
	@echo ""
	@echo "Setup:"
	@echo "  make install-linters   - Install all linting tools"
	@echo "  make check-linters     - Check if linting tools are installed"

# ============================================================================
# Testing Targets
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

# ============================================================================
# Linting Targets
# ============================================================================

# Run all linters
lint: lint-python lint-shell lint-yaml lint-docker lint-container-paths lint-bin-symlinks lint-claude-imports lint-container-host-boundary lint-gh-cli-usage lint-validate-config
	@echo ""
	@echo "All linters completed!"

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
# Shell (shellcheck + shfmt)
# ----------------------------------------------------------------------------
SHELL_FILES := $(shell find . -name "*.sh" -not -path "./.venv/*" -not -path "./venv/*" -not -path "./host-services/.venv/*" -not -path "./.git/*" 2>/dev/null)

lint-shell:
	@echo "==> Linting shell scripts with shellcheck..."
	@if ! command -v shellcheck >/dev/null 2>&1; then \
		echo "ERROR: shellcheck not installed."; \
		echo "Install with:"; \
		echo "  Fedora/RHEL: sudo dnf install ShellCheck"; \
		echo "  macOS: brew install shellcheck"; \
		exit 1; \
	elif [ -n "$(SHELL_FILES)" ]; then \
		shellcheck --severity=warning $(SHELL_FILES) || (echo "Shell linting failed!" && exit 1); \
		echo "Shell linting passed!"; \
	else \
		echo "No shell scripts found."; \
	fi

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
		echo "  Manual: wget -O ~/.local/bin/hadolint https://github.com/hadolint/hadolint/releases/download/v2.12.0/hadolint-Linux-arm64 && chmod +x ~/.local/bin/hadolint"; \
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
# Container Path Checks
# ----------------------------------------------------------------------------
lint-container-paths:
	@echo "==> Checking for problematic sys.path patterns..."
	@$(PYTHON) scripts/check-container-paths.py

# ----------------------------------------------------------------------------
# Sandbox Bin Symlinks Check
# ----------------------------------------------------------------------------
lint-bin-symlinks:
	@echo "==> Checking sandbox/bin/ symlinks..."
	@$(PYTHON) scripts/check-bin-symlinks.py

# ----------------------------------------------------------------------------
# Security: Claude/Anthropic Import Check
# ----------------------------------------------------------------------------
lint-claude-imports:
	@echo "==> Checking for forbidden Claude/Anthropic imports in host-services..."
	@$(PYTHON) scripts/check-claude-imports.py

# ----------------------------------------------------------------------------
# Security: Container/Host Boundary Check
# ----------------------------------------------------------------------------
lint-container-host-boundary:
	@echo "==> Checking for forbidden host-services imports in sandbox..."
	@$(PYTHON) scripts/check-container-host-boundary.py

# ----------------------------------------------------------------------------
# Security: gh CLI Write Operations Check
# ----------------------------------------------------------------------------
lint-gh-cli-usage:
	@echo "==> Checking for forbidden gh CLI write operations in host-services..."
	@$(PYTHON) scripts/check-gh-cli-usage.py

# ----------------------------------------------------------------------------
# Configuration Validation
# ----------------------------------------------------------------------------
lint-validate-config:
	@echo "==> Validating configuration loading..."
	@$(PYTHON) scripts/validate-config.py || echo "Warning: config validation failed (may require environment setup)"

# ============================================================================
# Setup Targets
# ============================================================================

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
		echo "  Or: https://github.com/mvdan/sh#shfmt"; \
	else \
		echo "shfmt is installed: $$(shfmt --version)"; \
	fi
	@echo ""
	@echo "==> Checking for shellcheck..."
	@if ! command -v shellcheck >/dev/null 2>&1; then \
		echo "shellcheck not found. Install with:"; \
		echo "  Ubuntu/Debian: sudo apt-get install shellcheck"; \
		echo "  macOS: brew install shellcheck"; \
		echo "  Or: https://github.com/koalaman/shellcheck#installing"; \
	else \
		echo "shellcheck is installed: $$(shellcheck --version | head -1)"; \
	fi
	@echo ""
	@echo "==> Checking for hadolint..."
	@if ! command -v hadolint >/dev/null 2>&1; then \
		echo "hadolint not found. Install with:"; \
		echo "  macOS: brew install hadolint"; \
		echo "  Linux: Download from https://github.com/hadolint/hadolint/releases"; \
		echo "  Or: docker run --rm -i hadolint/hadolint < Dockerfile"; \
	else \
		echo "hadolint is installed: $$(hadolint --version)"; \
	fi
	@echo ""
	@echo "==> Checking for actionlint..."
	@if ! command -v actionlint >/dev/null 2>&1; then \
		echo "actionlint not found. Install with:"; \
		echo "  macOS: brew install actionlint"; \
		echo "  Go: go install github.com/rhysd/actionlint/cmd/actionlint@latest"; \
		echo "  Or: https://github.com/rhysd/actionlint#installation"; \
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
