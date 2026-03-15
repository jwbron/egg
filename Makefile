# egg Makefile
# =============
# Single entry point for all development tasks.
#
# Runs checks natively for consistent behavior in CI, local dev, and the
# SDLC pipeline sandbox. Use `make lint-fix` to auto-fix lint issues.

# Virtual environment configuration
VENV_DIR := .venv
VENV_BIN := $(VENV_DIR)/bin

# Tool resolution: prefer venv, fall back to system PATH.
# CI uses venv (via uv sync); the sandbox has tools installed globally.
RUFF := $(if $(wildcard $(VENV_BIN)/ruff),$(VENV_BIN)/ruff,ruff)
PYTEST := $(if $(wildcard $(VENV_BIN)/pytest),$(VENV_BIN)/pytest,pytest)
MYPY := $(if $(wildcard $(VENV_BIN)/mypy),$(VENV_BIN)/mypy,mypy)
YAMLLINT := $(if $(wildcard $(VENV_BIN)/yamllint),$(VENV_BIN)/yamllint,yamllint)
BANDIT := $(if $(wildcard $(VENV_BIN)/bandit),$(VENV_BIN)/bandit,bandit)
PYTHON := $(if $(wildcard $(VENV_BIN)/python),$(VENV_BIN)/python,python3)

# PYTHONPATH — set per-target to avoid leaking into unrelated recipes

.PHONY: help \
        setup venv install-linters check-linters \
        lint lint-python lint-shell lint-yaml lint-docker lint-actions lint-custom \
        test security \
        test-integration test-e2e test-security \
        lint-fix lint-python-fix lint-shell-fix lint-yaml-fix \
        build

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
	@echo "CI checks (same commands run in GitHub Actions):"
	@echo "  make lint               - Run all linters"
	@echo "  make test               - Run all tests"
	@echo "  make security           - Run security scan"
	@echo ""
	@echo "Individual lint targets:"
	@echo "  make lint-python        - Ruff check + format + mypy"
	@echo "  make lint-shell         - Shellcheck"
	@echo "  make lint-yaml          - Yamllint"
	@echo "  make lint-docker        - Hadolint (requires hadolint)"
	@echo "  make lint-actions       - Actionlint (requires actionlint)"
	@echo "  make lint-custom        - Project-specific check scripts"
	@echo ""
	@echo "Integration tests (requires Docker):"
	@echo "  make test-integration   - Run integration tests"
	@echo "  make test-e2e           - Run E2E tests (requires API keys)"
	@echo "  make test-security      - Run security/pentesting tests"
	@echo ""
	@echo "Auto-fix (modifies local files):"
	@echo "  make lint-fix           - Auto-fix lint issues (ruff, shfmt, yaml)"
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
	@if [ ! -f "$(VENV_BIN)/ruff" ]; then \
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
	@echo "Linting tools installation complete!"

# Check if linting tools are installed
check-linters:
	@echo "Checking linting tools..."
	@echo ""
	@echo -n "ruff: "
	@if command -v $(RUFF) >/dev/null 2>&1; then $(RUFF) --version; else echo "NOT INSTALLED"; fi
	@echo -n "mypy: "
	@if command -v $(MYPY) >/dev/null 2>&1; then $(MYPY) --version; else echo "NOT INSTALLED"; fi
	@echo -n "yamllint: "
	@if command -v $(YAMLLINT) >/dev/null 2>&1; then $(YAMLLINT) --version; else echo "NOT INSTALLED"; fi
	@echo -n "shellcheck: "
	@if command -v shellcheck >/dev/null 2>&1; then shellcheck --version | head -1; else echo "NOT INSTALLED"; fi
	@echo -n "hadolint: "
	@if command -v hadolint >/dev/null 2>&1; then hadolint --version; else echo "NOT INSTALLED"; fi
	@echo -n "actionlint: "
	@if command -v actionlint >/dev/null 2>&1; then actionlint --version; else echo "NOT INSTALLED"; fi
	@echo -n "shfmt: "
	@if command -v shfmt >/dev/null 2>&1; then shfmt --version; else echo "NOT INSTALLED"; fi

# ============================================================================
# CI checks (native — same commands used in GitHub Actions workflows)
# ============================================================================

# Aggregate lint target: runs all linters that are available.
# In CI, all tools are installed so all sub-targets run.
# In the sandbox, missing tools (hadolint, actionlint, yamllint) are skipped.
lint: lint-python lint-shell lint-yaml lint-docker lint-actions lint-custom

lint-python: export PYTHONPATH := shared:gateway:orchestrator
lint-python:
	@echo "==> Ruff check..."
	@$(RUFF) check .
	@echo "==> Ruff format check..."
	@$(RUFF) format --check .
	@echo "==> Mypy..."
	@if command -v $(MYPY) >/dev/null 2>&1; then \
		$(MYPY) gateway shared sandbox --exclude 'gateway/tests/' --exclude 'shared/egg_contracts/tests/'; \
	else \
		echo "SKIP: mypy not installed"; \
	fi

lint-shell:
	@echo "==> Shellcheck..."
	@if command -v shellcheck >/dev/null 2>&1; then \
		SHELL_FILES=$$(find . -name "*.sh" -not -path "./.venv/*" -not -path "./.git/*"); \
		if [ -d "sandbox/scripts" ]; then \
			for f in sandbox/scripts/*; do \
				[ -f "$$f" ] && SHELL_FILES="$$SHELL_FILES $${f}"; \
			done; \
		fi; \
		if [ -n "$$SHELL_FILES" ]; then \
			echo $$SHELL_FILES | tr ' ' '\n' | sort -u | xargs shellcheck --severity=warning; \
		fi; \
	else \
		echo "SKIP: shellcheck not installed"; \
	fi

lint-yaml:
	@echo "==> Yamllint..."
	@if command -v $(YAMLLINT) >/dev/null 2>&1; then \
		$(YAMLLINT) -c .yamllint.yaml .; \
	else \
		echo "SKIP: yamllint not installed"; \
	fi

lint-docker:
	@echo "==> Hadolint..."
	@if command -v hadolint >/dev/null 2>&1; then \
		hadolint --config .hadolint.yaml gateway/Dockerfile; \
		hadolint --config .hadolint.yaml sandbox/Dockerfile; \
	else \
		echo "SKIP: hadolint not installed"; \
	fi

lint-actions:
	@echo "==> Actionlint..."
	@if command -v actionlint >/dev/null 2>&1; then \
		actionlint; \
	else \
		echo "SKIP: actionlint not installed"; \
	fi

lint-custom: export PYTHONPATH := shared:gateway:orchestrator
lint-custom:
	@echo "==> Custom checks..."
	@failed=""; \
	for script in scripts/check-*.py; do \
		name=$$(basename "$$script" .py | sed 's/^check-//'); \
		echo "  $$name..."; \
		if ! $(PYTHON) "$$script" 2>&1; then \
			failed="$$failed $$name"; \
		fi; \
	done; \
	if [ -n "$$failed" ]; then \
		echo ""; \
		echo "FAILED custom checks:$$failed"; \
		exit 1; \
	fi

test: export PYTHONPATH := shared:gateway:orchestrator
test: venv
	@echo "==> Running unit tests..."
	$(PYTEST) tests/ gateway/tests/ orchestrator/tests/ -v $(PYTEST_ARGS)

security:
	@echo "==> Running security scan..."
	@if command -v $(BANDIT) >/dev/null 2>&1; then \
		$(BANDIT) -r gateway shared sandbox orchestrator -ll -c pyproject.toml; \
	else \
		echo "SKIP: bandit not installed"; \
	fi

# ============================================================================
# Integration tests (native — requires Docker)
# ============================================================================

test-integration: export PYTHONPATH := shared
test-integration: venv  ## Run integration tests (requires Docker)
	$(PYTEST) integration_tests -v -m integration --timeout=300

test-e2e: export PYTHONPATH := shared
test-e2e: venv  ## Run E2E tests (requires API keys)
	$(PYTEST) integration_tests -v -m e2e --timeout=600

test-security: export PYTHONPATH := shared
test-security: venv  ## Run security/pentesting tests
	$(PYTEST) integration_tests -v -m security --timeout=300

# ============================================================================
# Auto-fix (native — these modify local files)
# ============================================================================

# Shell files for shfmt
SHELL_FILES := $(shell find . -name "*.sh" -not -path "./.venv/*" -not -path "./venv/*" -not -path "./host-services/.venv/*" -not -path "./.git/*" 2>/dev/null)

# YAML files for whitespace fixing
YAML_FILES := $(shell find . \( -name "*.yaml" -o -name "*.yml" \) \
	-not -path "./.venv/*" -not -path "./venv/*" -not -path "./host-services/.venv/*" -not -path "./node_modules/*" -not -path "./.git/*" 2>/dev/null)

lint-fix: lint-python-fix lint-shell-fix lint-yaml-fix
	@echo ""
	@echo "Auto-fixes applied. Run 'make lint' to verify."

lint-python-fix:
	@echo "==> Fixing Python files with ruff..."
	@$(RUFF) check --fix --unsafe-fixes .
	@$(RUFF) format .
	@echo "Python files fixed!"

lint-shell-fix:
	@echo "==> Formatting shell scripts with shfmt..."
	@if ! command -v shfmt >/dev/null 2>&1; then \
		echo "ERROR: shfmt not installed."; \
		echo "Install with:"; \
		echo "  Ubuntu/Debian: sudo apt-get install shfmt"; \
		echo "  macOS: brew install shfmt"; \
		exit 1; \
	fi
	@if [ -n "$(SHELL_FILES)" ]; then \
		shfmt -w -i 2 -ci -bn $(SHELL_FILES); \
		echo "Shell scripts formatted!"; \
	else \
		echo "No shell scripts found."; \
	fi

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
	else \
		echo "No YAML files found."; \
	fi

# ============================================================================
# Build
# ============================================================================

build:
	@echo "==> Building gateway container..."
	docker build -t egg-gateway -f gateway/Dockerfile .
	@echo "==> Building sandbox container..."
	docker build -t egg-sandbox -f sandbox/Dockerfile .
