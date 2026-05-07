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
# Recursive (`=`, not `:=`) so the wildcard re-evaluates after a `sync-venv-if-uv`
# prereq creates the venv on first use in a fresh worktree (issue #2280).
RUFF = $(if $(wildcard $(VENV_BIN)/ruff),$(VENV_BIN)/ruff,ruff)
PYTEST = $(if $(wildcard $(VENV_BIN)/pytest),$(VENV_BIN)/pytest,pytest)
MYPY = $(if $(wildcard $(VENV_BIN)/mypy),$(VENV_BIN)/mypy,mypy)
YAMLLINT = $(if $(wildcard $(VENV_BIN)/yamllint),$(VENV_BIN)/yamllint,yamllint)
BANDIT = $(if $(wildcard $(VENV_BIN)/bandit),$(VENV_BIN)/bandit,bandit)
PYTHON = $(if $(wildcard $(VENV_BIN)/python),$(VENV_BIN)/python,python3)

# PYTHONPATH — set per-target to avoid leaking into unrelated recipes

# Image tag derived from git state — distinct per build so Deployment
# podTemplate changes and k8s actually rolls out a new pod (issue #1763).
# Falls back to "latest" outside a git checkout.
EGG_IMAGE_TAG := $(shell git describe --always --dirty 2>/dev/null || echo latest)

.PHONY: help \
        setup deps venv sync-venv-if-uv sandbox-deps install-linters check-linters \
        lint lint-python lint-shell lint-yaml lint-docker lint-actions lint-custom \
        test test-all test-record-good security \
        test-integration test-security smoketest-long-poll \
        lint-fix lint-python-fix lint-shell-fix lint-yaml-fix \
        build \
        k3s-setup k3s-secrets deploy redeploy k3s-teardown k3s-import

# Default target
help:
	@echo "egg Development Commands"
	@echo "========================"
	@echo ""
	@echo "Setup:"
	@echo "  make setup              - Full development environment setup"
	@echo "  make deps               - Install all dependencies (installs uv + venv)"
	@echo "  make venv               - Create venv with dev dependencies"
	@echo "  make install-linters    - Install system linting tools"
	@echo "  make check-linters      - Check if linting tools are installed"
	@echo ""
	@echo "CI checks (same commands run in GitHub Actions):"
	@echo "  make lint               - Run all linters"
	@echo "  make test               - Run unit tests narrowed to the changeset (issue #1973)"
	@echo "  make test-all           - Run full unit-test suite + record LKG on green"
	@echo "  make test-record-good   - Manually record HEAD as Last-Known-Good baseline"
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
	@echo "Integration tests (requires k3s):"
	@echo "  make test-integration   - Run integration tests"
	@echo "  make test-security      - Run security/pentesting tests"
	@echo ""
	@echo "Auto-fix (modifies local files):"
	@echo "  make lint-fix           - Auto-fix lint issues (ruff, shfmt, yaml)"
	@echo ""
	@echo "Build:"
	@echo "  make build              - Build Docker images"
	@echo ""
	@echo "Kubernetes (k3s):"
	@echo "  make k3s-setup          - Install k3s with Calico CNI"
	@echo "  make deploy             - Deploy egg to k3s"
	@echo "  make redeploy           - Rebuild, re-import, and redeploy in one step"
	@echo "  make k3s-import         - Import built images into k3s"
	@echo "  make k3s-teardown       - Remove k3s"

# ============================================================================
# Setup
# ============================================================================

# Full development environment setup
setup: deps
	@echo "==> Installing pre-commit hooks..."
	@$(VENV_BIN)/pre-commit install || true
	@echo ""
	@echo "Setup complete! Run 'make help' to see available commands."

# Install all dependencies (installs uv if needed, then syncs venv)
deps:
	@if ! command -v uv >/dev/null 2>&1; then \
		echo "==> Installing uv..."; \
		curl -LsSf https://astral.sh/uv/install.sh | sh; \
	fi
	@PATH="$$HOME/.local/bin:$$HOME/.cargo/bin:$$PATH" $(MAKE) venv

# Ensure venv exists and has dev dependencies.
# Always runs `uv sync` — it's a fast no-op when the lockfile already matches
# the installed environment, and avoids fragile single-binary sentinel checks
# that can miss partial installs.
venv:
	@if ! command -v uv >/dev/null 2>&1; then \
		echo "ERROR: uv is not installed."; \
		echo ""; \
		echo "Install uv with:"; \
		echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"; \
		echo ""; \
		echo "Or see: https://docs.astral.sh/uv/getting-started/installation/"; \
		exit 1; \
	fi
	@echo "==> Syncing venv..."
	@uv sync --extra dev

# Sync the venv if uv is on PATH; no-op otherwise.
# The sandbox container pre-installs pytest/ruff/mypy globally (see
# sandbox/Dockerfile) and does not ship uv, so test targets that depend
# on this stay green there. Dev machines and CI both have uv and get
# the same `uv sync` behavior as the strict `venv` target. Issue #2065.
sync-venv-if-uv:
	@if command -v uv >/dev/null 2>&1; then $(MAKE) venv; fi

# Sync only third-party dependencies into .venv; do NOT install the local
# `egg` package. This is the variant invoked by sandbox image build_commands,
# which run in a synthetic context containing only watch_files (Makefile,
# pyproject.toml, uv.lock) — no source dirs, no README — so a full
# `uv sync` fails when the hatchling build backend tries to package the
# project. Dev tools (ruff, pytest, mypy, etc.) install fine without the
# project itself. Issue #2087.
sandbox-deps:
	@echo "==> Syncing dev dependencies (no project install)..."
	@uv sync --extra dev --no-install-project

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
lint-python: sync-venv-if-uv
	@echo "==> Ruff check..."
	@$(RUFF) check .
	@echo "==> Ruff format check..."
	@$(RUFF) format --check .
	@echo "==> Mypy..."
	@if command -v $(MYPY) >/dev/null 2>&1; then \
		$(MYPY) gateway shared sandbox --exclude 'gateway/tests/' --exclude 'shared/egg_contracts/tests/' --exclude 'shared/tests/'; \
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

lint-yaml: sync-venv-if-uv
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
lint-custom: sync-venv-if-uv
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

## Changeset-aware narrow default (issue #1973).
## `make test` invokes scripts/select_tests/__main__.py to compute the
## transitive reverse-import closure of files touched since the
## per-branch Last-Known-Good (LKG) commit (or base branch when no
## LKG sidecar exists), and runs pytest on only that subset.  Any
## sign of static-analysis fog (conftest / Makefile / pyproject /
## uv.lock / workflow / shared/tests / non-.py / dynamic-import /
## unresolvable-baseline / LKG-not-ancestor) widens to the full
## suite with an explicit trigger string on stderr.  See
## docs/guides/testing.md and scripts/select_tests/__init__.py for
## the full design.
##
## CI runs `make test-all` (below) to keep the 80% coverage gate
## enforced; narrowing is a local-inner-loop optimisation only.
##
## After pytest returns, this target invokes
## `select_tests/__main__.py --patch-selection-json` to append
## `pytest_ms` to the existing `.egg-state/selection/<head>.json`
## record so the JSON envelope captures both compute_ms and
## pytest_ms in one place.  LKG sidecar is NEVER updated by
## `make test` (Q12); only `make test-all` records LKG on green.
test: export PYTHONPATH := shared:gateway:orchestrator
test: sync-venv-if-uv
	@echo "==> Running narrowed unit tests (changeset-aware; see docs/guides/testing.md)..."
	@selected_file=$$(mktemp); \
	PYTEST_ARGS_RAW="$(PYTEST_ARGS)" \
		env -u PYTHONPATH $(PYTHON) scripts/select_tests/__main__.py >"$$selected_file"; \
	selector_rc=$$?; \
	if [ "$$selector_rc" -ne 0 ]; then \
		echo "select-tests: selector exited $$selector_rc; running full suite as fallback"; \
		printf '%s\n' tests gateway/tests orchestrator/tests shared/tests >"$$selected_file"; \
	fi; \
	bypass=0; \
	if [ ! -s "$$selected_file" ]; then \
		head_sha_check=$$(git rev-parse HEAD 2>/dev/null || echo unknown); \
		json_path=".egg-state/selection/$$head_sha_check.json"; \
		if [ -f "$$json_path" ] && grep -q '"mode": "bypass"' "$$json_path" 2>/dev/null; then \
			bypass=1; \
		fi; \
		if [ "$$bypass" = "0" ]; then \
			echo "select-tests: no tests selected"; \
			rm -f "$$selected_file"; \
			exit 0; \
		fi; \
	fi; \
	head_sha=$$(git rev-parse HEAD 2>/dev/null || echo unknown); \
	t0=$$(date +%s%N); \
	if [ "$$bypass" = "1" ]; then \
		$(PYTEST) -v -m "not functional" $(PYTEST_ARGS); \
	else \
		$(PYTEST) $$(cat "$$selected_file") -v -m "not functional" $(PYTEST_ARGS); \
	fi; \
	pytest_rc=$$?; \
	t1=$$(date +%s%N); \
	rm -f "$$selected_file"; \
	pytest_ms=$$(( (t1 - t0) / 1000000 )); \
	env -u PYTHONPATH $(PYTHON) scripts/select_tests/__main__.py --patch-selection-json --head "$$head_sha" --pytest-ms "$$pytest_ms" || true; \
	exit $$pytest_rc

## Full-suite escape hatch (issue #1973).  Runs the historical
## `pytest tests/ gateway/tests/ orchestrator/tests/ shared/tests/`
## command unconditionally — no narrowing, no fallback evaluation.
## On green exit, atomically writes the LKG sidecar at
## `.egg-state/last-known-good/<branch>.sha` so the next
## `make test` can narrow against it.  On red exit, the sidecar
## is NOT updated (Q12) — partial / failing test runs cannot
## become a future LKG baseline.
##
## CI uses this target to keep the 80% coverage gate enforced
## unchanged (decision-d2).  Local developers can run it any time
## to refresh their LKG without remembering the script invocation.
test-all: export PYTHONPATH := shared:gateway:orchestrator
test-all: sync-venv-if-uv  ## Run the full unit-test suite + record LKG on green
	@echo "==> Running full unit-test suite (issue #1973: this updates LKG on green)..."
	@$(PYTEST) tests/ gateway/tests/ orchestrator/tests/ shared/tests/ -v -m "not functional" $(PYTEST_ARGS); \
	pytest_rc=$$?; \
	if [ "$$pytest_rc" -eq 0 ]; then \
		env -u PYTHONPATH $(PYTHON) scripts/select_tests/__main__.py --record-good \
			|| echo "select-tests: --record-good failed; LKG not updated"; \
	else \
		echo "select-tests: pytest failed; LKG not updated"; \
	fi; \
	exit $$pytest_rc

## Manually record the current HEAD as a Last-Known-Good baseline
## (issue #1973).  Use this when you know the suite passed but
## didn't run via `make test-all` (e.g., ran a subset that you
## know is exhaustive for your branch).  Validates the sha against
## the object DB and against ancestor-of-HEAD; refuses on bad
## input.
test-record-good:
	@echo "==> Recording HEAD as Last-Known-Good baseline (issue #1973)..."
	@env -u PYTHONPATH $(PYTHON) scripts/select_tests/__main__.py --record-good

smoketest-long-poll: export PYTHONPATH := shared:gateway:orchestrator
smoketest-long-poll: venv  ## Smoke-test the long-poll / event-driven wait infrastructure
	$(PYTEST) \
		orchestrator/tests/test_messages.py::TestWaitEndpoint \
		orchestrator/tests/test_messages.py::TestLongPolling \
		orchestrator/tests/test_messages.py::TestInflightLongPollGauge \
		orchestrator/tests/test_messages.py::TestWaitTimeoutFloorRegression \
		orchestrator/tests/test_message_store.py::TestWaitForTypesFilter \
		orchestrator/tests/test_message_store.py::TestNotifyMultipleWaiters \
		orchestrator/tests/test_cli.py::TestWaitressSizing \
		orchestrator/tests/test_concurrent_integration.py::TestEventDrivenConsensusWait \
		-v --timeout=90

security: sync-venv-if-uv
	@echo "==> Running security scan..."
	@if command -v $(BANDIT) >/dev/null 2>&1; then \
		$(BANDIT) -r gateway shared sandbox orchestrator -ll -c pyproject.toml; \
	else \
		echo "SKIP: bandit not installed"; \
	fi

# ============================================================================
# Integration tests (native — requires k3s; see docs/guides/testing.md)
# ============================================================================

test-integration: export PYTHONPATH := shared
test-integration: venv  ## Run integration tests on k3s (cross-module regressions)
	$(PYTEST) integration_tests -v -m integration --timeout=300

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

lint-python-fix: sync-venv-if-uv
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

lint-yaml-fix: sync-venv-if-uv
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
	@echo "==> Preparing sandbox build context (repo-deps marker)..."
	@mkdir -p repo-deps && touch repo-deps/.empty
	@echo "==> Building images with tag $(EGG_IMAGE_TAG)..."
	@echo "==> Building gateway container..."
	docker build -t egg-gateway:latest -t egg-gateway:$(EGG_IMAGE_TAG) -f gateway/Dockerfile .
	@echo "==> Building orchestrator container..."
	docker build -t egg-orchestrator:latest -t egg-orchestrator:$(EGG_IMAGE_TAG) -f orchestrator/Dockerfile .
	@echo "==> Building sandbox container..."
	docker build -t egg-sandbox:latest -t egg-sandbox:$(EGG_IMAGE_TAG) -f sandbox/Dockerfile .

# ============================================================================
# Kubernetes (k3s) targets
# ============================================================================

k3s-setup:  ## Install k3s with Calico CNI
	@echo "Setting up k3s cluster..."
	curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="--flannel-backend=none --disable-network-policy --write-kubeconfig-mode=644" sh -
	export KUBECONFIG=/etc/rancher/k3s/k3s.yaml && \
	scripts/install-calico.sh && \
	echo "Waiting for k3s node to be ready..." && \
	kubectl wait --for=condition=Ready node --all --timeout=120s
	@echo "k3s cluster ready"

k3s-secrets:  ## Create gateway secrets from ~/.config/egg/
	@if [ ! -f "$$HOME/.config/egg/launcher-secret" ]; then \
		echo "ERROR: $$HOME/.config/egg/launcher-secret not found."; \
		echo "Run 'bin/egg-deploy init' to generate it."; \
		exit 1; \
	fi
	@if [ ! -f "$$HOME/.config/egg/lifecycle-secret" ]; then \
		echo "ERROR: $$HOME/.config/egg/lifecycle-secret not found."; \
		echo "Generate it: openssl rand -hex 32 > $$HOME/.config/egg/lifecycle-secret"; \
		exit 1; \
	fi
	@echo "==> Creating gateway-secrets in egg-system namespace..."
	@echo "   (all files under ~/.config/egg/ become keys in the secret)"
	export KUBECONFIG=$${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml} && \
	kubectl -n egg-system create secret generic gateway-secrets \
		--from-file=$$HOME/.config/egg/ \
		--dry-run=client -o yaml | kubectl apply -f -

deploy: k3s-secrets  ## Deploy egg to k3s
	@echo "Deploying to k3s with tag $(EGG_IMAGE_TAG)..."
	@command -v envsubst >/dev/null 2>&1 || { \
		echo "ERROR: envsubst not found. Install GNU gettext: 'dnf install gettext' or 'brew install gettext'." >&2; \
		exit 1; \
	}
	export KUBECONFIG=$${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml} && \
	export EGG_HOST_HOME="$${EGG_HOST_HOME:-$$HOME}" && \
	export EGG_HOST_REPO_MAP="$${EGG_HOST_REPO_MAP:-$$(scripts/build-host-repo-map.py)}" && \
	echo "  EGG_HOST_HOME=$$EGG_HOST_HOME" && \
	echo "  EGG_HOST_REPO_MAP=$$EGG_HOST_REPO_MAP" && \
	kubectl kustomize k8s/overlays/local/ | \
		envsubst '$$EGG_HOST_HOME $$EGG_HOST_REPO_MAP' | \
		sed -E "/name: EGG_HOST_REPO_MAP$$/{N;s|^(\s*- name: EGG_HOST_REPO_MAP\s*\n\s*value: )(\{.*\})$$|\1'\2'|}" | \
		sed -e "s|egg-orchestrator:latest|egg-orchestrator:$(EGG_IMAGE_TAG)|g" \
		    -e "s|egg-gateway:latest|egg-gateway:$(EGG_IMAGE_TAG)|g" \
		    -e "s|egg-sandbox:latest|egg-sandbox:$(EGG_IMAGE_TAG)|g" | \
		kubectl apply -f - && \
	kubectl -n egg-system wait --for=condition=Available deployment/orchestrator --timeout=120s && \
	kubectl -n egg-system wait --for=condition=Available deployment/gateway --timeout=120s
	@echo "Deployment complete"

redeploy: build k3s-import deploy  ## Rebuild, re-import, and redeploy in one step

k3s-import:  ## Import built images into k3s
	docker save egg-gateway:latest | sudo k3s ctr images import -
	docker save egg-gateway:$(EGG_IMAGE_TAG) | sudo k3s ctr images import -
	docker save egg-orchestrator:latest | sudo k3s ctr images import -
	docker save egg-orchestrator:$(EGG_IMAGE_TAG) | sudo k3s ctr images import -
	docker save egg-sandbox:latest | sudo k3s ctr images import -
	docker save egg-sandbox:$(EGG_IMAGE_TAG) | sudo k3s ctr images import -

k3s-teardown:  ## Remove k3s
	/usr/local/bin/k3s-uninstall.sh || true
	@echo "k3s removed"
