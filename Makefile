# egg Makefile
# =============
# Single entry point for all development tasks.
#
# Lint, test, and security targets delegate to GitHub Actions workflows
# via act (https://github.com/nektos/act) for single-source-of-truth CI parity.
#
# For auto-fixing, use `make lint-fix` (runs natively since it modifies files).
# For running individual tools directly, use the venv: .venv/bin/ruff check .

# Virtual environment configuration
VENV_DIR := .venv
VENV_BIN := $(VENV_DIR)/bin
PYTHON := $(VENV_BIN)/python
RUFF := $(VENV_BIN)/ruff
YAMLLINT := $(VENV_BIN)/yamllint

.PHONY: help \
        setup venv install-linters check-linters \
        lint test security ci \
        lint-fix lint-python-fix lint-shell-fix lint-yaml-fix \
        build \
        _require-act

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
	@echo "CI checks (via act — same as GitHub Actions):"
	@echo "  make lint               - Run all linters"
	@echo "  make test               - Run all tests"
	@echo "  make security           - Run security scan"
	@echo "  make ci                 - Run full CI pipeline"
	@echo ""
	@echo "Auto-fix (native, modifies local files):"
	@echo "  make lint-fix           - Auto-fix lint issues (ruff, shfmt, yaml)"
	@echo ""
	@echo "Build:"
	@echo "  make build              - Build Docker images"
	@echo ""
	@echo "Individual tools are available directly via the venv:"
	@echo "  .venv/bin/ruff check .                   - Python lint"
	@echo "  .venv/bin/pytest tests/ -v                - Run tests"
	@echo "  .venv/bin/bandit -r gateway shared sandbox -ll  - Security scan"

# ============================================================================
# Setup
# ============================================================================

# Full development environment setup
setup: venv
	@echo "==> Installing pre-commit hooks..."
	@$(VENV_BIN)/pre-commit install || true
	@echo ""
	@echo "Setup complete! Run 'make help' to see available commands."
	@echo ""
	@echo "Note: 'make lint' requires act (https://github.com/nektos/act)."
	@echo "For auto-fixing lint issues, use 'make lint-fix'."

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
	@echo -n "act: "
	@if command -v act >/dev/null 2>&1; then act --version; else echo "NOT INSTALLED"; fi

# ============================================================================
# CI checks (via act — delegates to GitHub Actions workflows)
# ============================================================================

_require-act:
	@if ! command -v act >/dev/null 2>&1; then \
		echo "ERROR: act is not installed."; \
		echo "Install with:"; \
		echo "  macOS: brew install act"; \
		echo "  Linux: curl -s https://raw.githubusercontent.com/nektos/act/master/install.sh | bash -s -- -b ~/.local/bin"; \
		echo ""; \
		echo "See: https://github.com/nektos/act"; \
		exit 1; \
	fi

lint: _require-act
	act -j lint

test: _require-act
	act -j unit

security: _require-act
	act -j security

ci: _require-act
	act push

# ============================================================================
# Auto-fix (native — these modify local files, can't run via act)
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
