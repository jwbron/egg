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

# Local image registry for the k3s deploy flow (issue #2999). When set,
# `make redeploy` publishes images by `docker push`-ing here and lets the
# cluster pull them back — both sides are layer-aware, so a typical
# code-only rebuild moves tens of MB instead of the full ~multi-GB sandbox
# image that `docker save` + `ctr import` always re-serialize. One-time
# host setup: `make registry-setup`. Set EGG_IMAGE_REGISTRY= (empty) to
# fall back to the save+import flow (`make k3s-import`) — CI does this so
# its inline import path keeps working without a registry.
EGG_IMAGE_REGISTRY ?= localhost:5000
# "localhost:5000/" or "" — spliced ahead of image names in build tags and
# in the manifest rewrites in `deploy`.
EGG_IMAGE_PREFIX := $(if $(EGG_IMAGE_REGISTRY),$(EGG_IMAGE_REGISTRY)/,)

# The full egg image set, and the subset published via the registry. The
# sandbox image is EXCLUDED from the registry subset by default: it bakes in
# private repo content (node_modules/.venv/anything repositories.yaml
# build_commands produce), so it never goes near a registry — even the
# loopback-only local one — unless the operator opts in by adding
# egg-sandbox to EGG_REGISTRY_IMAGES. Excluded images publish through the
# save+import path instead (slower for the big sandbox image, but entirely
# store-to-store on this host). push-egg-images.sh independently refuses any
# non-loopback registry, so opting in still cannot publish off-host.
EGG_ALL_IMAGES := egg-gateway egg-orchestrator egg-sandbox egg-litellm
EGG_REGISTRY_IMAGES ?= egg-gateway egg-orchestrator egg-litellm
# Images the registry path does NOT cover (imported via k3s-import instead).
EGG_IMPORT_IMAGES := $(filter-out $(EGG_REGISTRY_IMAGES),$(EGG_ALL_IMAGES))
# Per-image manifest prefix: registry-qualified only when registry mode is on
# AND the image is in the registry subset.
reg_prefix = $(if $(filter $(1),$(EGG_REGISTRY_IMAGES)),$(EGG_IMAGE_PREFIX),)

# Parallel image builds (issue #2999): the four images are independent, so
# build them concurrently. BUILD_JOBS=1 restores sequential builds — CI sets
# this because hosted runners build every stage cold, and four concurrent
# heavyweight builds risk memory/disk flake there. --output-sync (buffer
# each sub-build's output so the BuildKit progress UIs don't interleave)
# needs GNU make 4+; on 3.x (stock macOS make) output interleaves but the
# builds still work.
BUILD_JOBS ?= 4
BUILD_OUTPUT_SYNC := $(if $(filter 3.%,$(MAKE_VERSION)),,--output-sync=target)

.PHONY: help \
        setup deps venv sync-venv-if-uv sandbox-deps install-linters check-linters \
        lint lint-python lint-shell lint-yaml lint-docker lint-actions lint-custom \
        test test-all test-record-good security \
        test-integration test-security smoketest-long-poll \
        lint-fix lint-python-fix lint-shell-fix lint-yaml-fix \
        build build-gateway build-orchestrator build-sandbox build-litellm \
        k3s-setup k3s-secrets litellm-config routing-policy deploy redeploy k3s-teardown \
        k3s-import k3s-push k3s-publish registry-setup btrfs-reclaim sudo-keepalive \
        check-egg-images-present

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
	@echo "  make k3s-setup          - Install k3s with Cilium CNI"
	@echo "  make registry-setup     - One-time: local image registry + k3s registries.yaml"
	@echo "  make deploy             - Deploy egg to k3s"
	@echo "  make redeploy           - Rebuild, publish images, and redeploy in one step"
	@echo "  make k3s-push           - Push built images to the local registry"
	@echo "  make k3s-import         - Import built images into k3s (no-registry fallback)"
	@echo "  make btrfs-reclaim      - Reclaim btrfs over-allocated chunks (issue #2999)"
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
		hadolint --config .hadolint.yaml config/litellm/Dockerfile; \
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
		$(PYTEST) -v $(PYTEST_ARGS); \
	else \
		$(PYTEST) $$(cat "$$selected_file") -v $(PYTEST_ARGS); \
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
	@$(PYTEST) tests/ gateway/tests/ orchestrator/tests/ shared/tests/ -v $(PYTEST_ARGS); \
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
test-integration: venv  ## Run integration + security tests on k3s
	# Selects `integration or security` (not just `integration`) so a
	# single `make test-integration` covers the entire k3s tier — what
	# CI runs as `Test / integration`. `make test-security` remains
	# available for security-only runs.
	$(PYTEST) integration_tests -v -m "integration or security" --timeout=300

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

# Tag set for one image: bare names always (k3s-import + local tooling),
# registry-qualified names only when registry mode is on AND the image is in
# the registry subset (what `k3s-push` pushes and `deploy` rewrites the
# manifests to). Non-subset images (the sandbox, by default) never even get
# a registry-qualified tag.
define image_tags
-t $(1):latest -t $(1):$(EGG_IMAGE_TAG) $(if $(call reg_prefix,$(1)),-t $(call reg_prefix,$(1))$(1):latest -t $(call reg_prefix,$(1))$(1):$(EGG_IMAGE_TAG))
endef

build: sync-venv-if-uv
	@echo "==> Preparing sandbox build context from repositories.yaml..."
	@$(PYTHON) scripts/prepare-sandbox-build-context.py repo-deps
	@echo "==> Building images with tag $(EGG_IMAGE_TAG) ($(BUILD_JOBS) parallel jobs)..."
	@$(MAKE) --no-print-directory -j$(BUILD_JOBS) $(BUILD_OUTPUT_SYNC) \
		build-gateway build-orchestrator build-sandbox build-litellm

# Per-image sub-targets so `build` can run them under -j. They assume the
# repo-deps/ build context has been prepared (the `build` target does that
# first, sequentially) — run `build`, not these, unless you know repo-deps/
# is fresh.
#
# DOCKER_BUILDKIT=1 is required so BuildKit honors the per-Dockerfile
# `<Dockerfile>.dockerignore` lookup (see sandbox/Dockerfile.dockerignore).
# The legacy builder reads only the root .dockerignore — under it the
# sandbox build's `COPY repo-deps/ /tmp/repo-deps/` would fail because
# repo-deps/ is excluded at the root. Docker 23+ defaults to BuildKit;
# pinning the env var keeps older Docker (18.09–22.x, which supports
# BuildKit but doesn't default to it) on the same path.
build-gateway:
	DOCKER_BUILDKIT=1 docker build $(call image_tags,egg-gateway) -f gateway/Dockerfile .

build-orchestrator:
	DOCKER_BUILDKIT=1 docker build $(call image_tags,egg-orchestrator) -f orchestrator/Dockerfile .

build-sandbox:
	DOCKER_BUILDKIT=1 docker build $(call image_tags,egg-sandbox) -f sandbox/Dockerfile .

build-litellm:
	DOCKER_BUILDKIT=1 docker build $(call image_tags,egg-litellm) -f config/litellm/Dockerfile config/litellm

# ============================================================================
# Kubernetes (k3s) targets
# ============================================================================

# k3s-setup INSTALL_K3S_EXEC flags:
#   --flannel-backend=none: Cilium replaces flannel as the CNI dataplane.
#   --disable-network-policy: Cilium owns NetworkPolicy enforcement; the
#     k3s-builtin policy controller would otherwise conflict.
#   --disable=metrics-server: disables k3s's BUNDLED metrics-server, which
#     runs on the pod network and under Cilium cannot reach the kubelet on
#     the node IP — it never becomes Ready, and the resulting
#     perpetually-unavailable v1beta1.metrics.k8s.io APIService makes the
#     namespace controller's discovery step fail, wedging *all* namespace
#     deletion (stuck Terminating forever). install-metrics-server.sh below
#     deploys a hostNetwork variant that reaches the kubelet and works; see
#     k8s/addons/metrics-server.yaml.
k3s-setup:  ## Install k3s with Cilium CNI
	@echo "Setting up k3s cluster..."
	curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="--flannel-backend=none --disable-network-policy --disable=metrics-server --write-kubeconfig-mode=644" sh -
	export KUBECONFIG=/etc/rancher/k3s/k3s.yaml && \
	scripts/install-cilium.sh && \
	echo "Waiting for k3s node to be ready..." && \
	kubectl wait --for=condition=Ready node --all --timeout=120s && \
	scripts/install-metrics-server.sh
	@if [ -n "$(EGG_IMAGE_REGISTRY)" ]; then \
		$(MAKE) --no-print-directory registry-setup; \
	fi
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
	@# The gateway routing policy (issue #2987) rides this same bundle: if
	@# present, ~/.config/egg/routing-policy.yaml is picked up by the
	@# --from-file line below and lands at /secrets/routing-policy.yaml,
	@# which the gateway hot-reads via an mtime cache. `make routing-policy`
	@# is a thin wrapper that just re-runs this target (no gateway rollout).
	@# LiteLLM master key (issue #2769): the in-cluster LiteLLM
	@# Deployment expects ``gateway-secrets.litellm-master-key`` so the
	@# gateway's injected x-api-key matches LiteLLM's master_key. The
	@# value lives in ``secrets.env`` as ``LITELLM_MASTER_KEY=...``;
	@# extract it and surface it as a discrete literal key so both
	@# sides of the wire share one source of truth. Empty value is the
	@# no-op default (the manifest reads the Secret with
	@# ``optional: true``).
	@#
	@# OpenRouter API key (issue #2799): the LiteLLM Deployment reads
	@# ``OPENROUTER_API_KEY`` at startup so ``openrouter/*`` model
	@# entries in ``litellm-configmap.yaml`` can authenticate against
	@# openrouter.ai. Same shape as the master-key extraction above —
	@# pull the value out of ``secrets.env`` and surface it as a
	@# literal key on ``gateway-secrets`` so the manifest's
	@# ``secretKeyRef`` resolves.
	@LITELLM_KEY="$$(grep -E '^[[:space:]]*LITELLM_MASTER_KEY[[:space:]]*=' "$$HOME/.config/egg/secrets.env" 2>/dev/null | tail -n1 | cut -d= -f2- | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$$//' -e 's/^"//' -e 's/"$$//' -e "s/^'//" -e "s/'$$//")"; \
	OPENROUTER_KEY="$$(grep -E '^[[:space:]]*OPENROUTER_API_KEY[[:space:]]*=' "$$HOME/.config/egg/secrets.env" 2>/dev/null | tail -n1 | cut -d= -f2- | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$$//' -e 's/^"//' -e 's/"$$//' -e "s/^'//" -e "s/'$$//")"; \
	export KUBECONFIG=$${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml} && \
	kubectl apply -f k8s/base/namespaces.yaml && \
	kubectl -n egg-system create secret generic gateway-secrets \
		--from-file=$$HOME/.config/egg/ \
		--from-literal=litellm-master-key="$$LITELLM_KEY" \
		--from-literal=openrouter-api-key="$$OPENROUTER_KEY" \
		--dry-run=client -o yaml | kubectl apply -f -

litellm-config:  ## Apply host-side LiteLLM model_list from ~/.config/egg/litellm-models.yaml
	@# Per-operator LiteLLM model_list lives in ~/.config/egg/litellm-models.yaml,
	@# parallel to secrets.env. The committed k8s/base/litellm-configmap.yaml
	@# ships an empty model_list so the pod starts healthy by default; this
	@# target merges the operator's backend choices on top after `make deploy`
	@# has applied the base configmap. No-op if the file is absent.
	@MODEL_FILE="$$HOME/.config/egg/litellm-models.yaml"; \
	if [ ! -f "$$MODEL_FILE" ]; then \
		echo "==> No $$MODEL_FILE; LiteLLM keeps the empty model_list from the base configmap."; \
		echo "    Copy config/litellm-models.template.yaml to register backends."; \
		exit 0; \
	fi; \
	export KUBECONFIG=$${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml} && \
	if ! kubectl -n egg-system get configmap litellm-config >/dev/null 2>&1; then \
		echo "==> litellm-config ConfigMap not present yet — run 'make deploy' first."; \
		exit 0; \
	fi; \
	echo "==> Patching litellm-config ConfigMap from $$MODEL_FILE..." && \
	PATCH_OUT="$$(kubectl patch configmap litellm-config -n egg-system \
		--type=merge --patch-file="$$MODEL_FILE")" && \
	echo "$$PATCH_OUT" && \
	if echo "$$PATCH_OUT" | grep -q '(no change)'; then \
		echo "==> ConfigMap unchanged; skipping LiteLLM rollout."; \
	else \
		echo "==> Rolling LiteLLM deployment to pick up new config..." && \
		kubectl rollout restart deployment litellm -n egg-system && \
		kubectl rollout status deployment litellm -n egg-system --timeout=180s; \
	fi

routing-policy:  ## Apply host-side gateway routing policy from ~/.config/egg/routing-policy.yaml
	@# The gateway routing/fallback policy (issue #2987) lives at
	@# ~/.config/egg/routing-policy.yaml, parallel to secrets.env. Unlike
	@# litellm-config (which patches a ConfigMap and ROLLS the LiteLLM pod),
	@# the routing policy rides the gateway-secrets mount: it is already
	@# bundled by the `--from-file=~/.config/egg/` line in k3s-secrets, and
	@# the gateway re-reads it via an mtime cache, so applying it is just
	@# re-creating the Secret — NO gateway rollout, no in-flight-turn loss.
	@# Copy config/routing-policy.template.yaml to register routes; an
	@# absent file is the no-op default (fail-open to the spawn-time route).
	@if [ ! -f "$$HOME/.config/egg/routing-policy.yaml" ]; then \
		echo "==> No ~/.config/egg/routing-policy.yaml; gateway uses the no-op default route."; \
		echo "    Copy config/routing-policy.template.yaml to register routes."; \
		exit 0; \
	fi
	@echo "==> Re-creating gateway-secrets to publish routing-policy.yaml (no gateway rollout)..."
	@$(MAKE) --no-print-directory k3s-secrets
	@echo "==> routing-policy.yaml published. kubelet propagates the volume update to the"
	@echo "    running gateway pod in ~60s; the gateway re-reads it on the next request."

check-egg-images-present:
	@scripts/check-egg-images-present.sh "$(EGG_IMAGE_TAG)" "$(EGG_IMAGE_REGISTRY)" $(EGG_REGISTRY_IMAGES)

# Cluster-mutating steps (k3s-secrets, kubectl apply) are invoked from the
# recipe body so the ordering survives `make -j`: two prerequisites of the
# same target may run in parallel under -j, but recipe lines never do. The
# check MUST run before k3s-secrets — k3s-secrets reconciles namespaces + the
# gateway-secrets Secret and would otherwise mutate the cluster before the
# image check could fail, defeating the zero-mutation abort this guard exists
# to provide. sudo-keepalive is a sibling prerequisite (not a recipe line) so
# `make deploy` standalone can leave the sudo credential cache fresh through
# the tail of the deploy: check-egg-images-present uses sudo at the very
# start, but the post-rollout reap (reap-stale-egg-images.sh) needs sudo
# minutes later after the long kubectl apply / await-egg-deploy steps, and
# the default sudo timestamp would otherwise have aged out, prompting on an
# attended deploy and silently skipping the reap (via `|| true`) on an
# unattended one. Neither sudo-keepalive nor check-egg-images-present
# mutates the cluster, so running them in parallel under -j is safe.
deploy: sudo-keepalive check-egg-images-present  ## Deploy egg to k3s
	@$(MAKE) --no-print-directory k3s-secrets
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
		sed -e "s|egg-orchestrator:latest|$(call reg_prefix,egg-orchestrator)egg-orchestrator:$(EGG_IMAGE_TAG)|g" \
		    -e "s|egg-gateway:latest|$(call reg_prefix,egg-gateway)egg-gateway:$(EGG_IMAGE_TAG)|g" \
		    -e "s|egg-sandbox:latest|$(call reg_prefix,egg-sandbox)egg-sandbox:$(EGG_IMAGE_TAG)|g" \
		    -e "s|egg-litellm:latest|$(call reg_prefix,egg-litellm)egg-litellm:$(EGG_IMAGE_TAG)|g" | \
		kubectl apply -f - && \
	scripts/clear-stuck-egg-pods.sh && \
	scripts/await-egg-deploy.sh "$(EGG_IMAGE_TAG)"
	@# Rollout confirmed on $(EGG_IMAGE_TAG): reap stale egg images from
	@# containerd, the docker store/BuildKit cache, and the local registry so
	@# none of them accumulates a ~10 GB sandbox image per deployed commit and
	@# pushes the root fs over kubelet's image-GC threshold. On btrfs hosts it
	@# also warns/auto-balances when chunk over-allocation runs the unallocated
	@# pool low (issue #2999). Best-effort -- a reap hiccup must not fail an
	@# otherwise-green deploy.
	@scripts/reap-stale-egg-images.sh "$(EGG_IMAGE_TAG)" "$(EGG_IMAGE_REGISTRY)" $(EGG_REGISTRY_IMAGES) || true
	@# routing-policy.yaml (issue #2987) was already bundled by the
	@# k3s-secrets call at the top of this target; no separate apply needed
	@# here. `make routing-policy` is the standalone hot-reload path between
	@# deploys.
	@$(MAKE) --no-print-directory litellm-config
	@echo "Deployment complete"

redeploy: sudo-keepalive build k3s-publish deploy  ## Rebuild, publish images, and redeploy in one step

# Publish dispatch (issue #2999): in registry mode, push the registry subset
# (layer-incremental — the fast path) and save+import the rest (the sandbox,
# unless opted in via EGG_REGISTRY_IMAGES); without a registry — or with an
# empty registry subset (EGG_REGISTRY_IMAGES="") — save+import everything.
k3s-publish: sudo-keepalive
	@if [ -n "$(EGG_IMAGE_REGISTRY)" ] && [ -n "$(strip $(EGG_REGISTRY_IMAGES))" ]; then \
		$(MAKE) --no-print-directory k3s-push; \
		if [ -n "$(strip $(EGG_IMPORT_IMAGES))" ]; then \
			$(MAKE) --no-print-directory k3s-import K3S_IMPORT_IMAGES="$(EGG_IMPORT_IMAGES)"; \
		fi; \
	else \
		$(MAKE) --no-print-directory k3s-import; \
	fi

# Push the registry-subset images to the local registry, then pre-pull them
# into k3s's containerd. The pre-pull (sudo, hence the sudo-keepalive
# prerequisite) keeps pod starts instant and lets reap-stale-egg-images.sh's
# safety gate see the new refs before anything references them.
k3s-push: sudo-keepalive  ## Push registry-subset images to the local registry
	@scripts/push-egg-images.sh "$(EGG_IMAGE_TAG)" "$(EGG_IMAGE_REGISTRY)" $(EGG_REGISTRY_IMAGES)

registry-setup:  ## One-time: run the local image registry + point k3s at it
	@scripts/setup-local-registry.sh "$(EGG_IMAGE_REGISTRY)"

# Manual escape hatch for #2999's root disease: btrfs data-chunk
# over-allocation from image churn. The post-deploy reap warns when this is
# needed and auto-runs it when critically low; run it by hand any time
# DiskPressure / "images not in k3s" shows up while `df -h` claims space.
btrfs-reclaim:  ## Reclaim btrfs over-allocated data chunks (issue #2999)
	@scripts/btrfs-reclaim.sh

# Prompt for the sudo password immediately so `make redeploy` can be left
# unattended through the long `build` step. A detached background loop refreshes
# the credential cache every 60s; it stops within ~60s of this make run exiting,
# and is hard-capped at 120 iterations (~2h) so a recycled make PID cannot keep
# it alive indefinitely. Its standard streams are redirected away from make's so
# it never holds make's output pipe open past make's own exit.
sudo-keepalive:
	@sudo -v
	@make_pid=$$PPID; \
	( i=0; while [ $$i -lt 120 ] && kill -0 "$$make_pid" 2>/dev/null; do \
		sudo -n -v 2>/dev/null; sleep 60; i=$$((i + 1)); \
	done ) >/dev/null 2>&1 </dev/null &

# Run this recipe under bash: the `${var//pat/repl}` parameter expansion, the
# `<<<` here-string, and `set -o pipefail` are bash builtins, all unsupported
# under dash (the default /bin/sh on Debian/Ubuntu).
#
# Import via temp file rather than `docker save ... | sudo k3s ctr images
# import -`. The stdin path silently drops the layer payload for some images
# on this host (observed for orchestrator/sandbox under aarch64+16k-pages on
# Fedora Asahi): ctr prints "saved" with the manifest digest, exits 0, and
# the tag never appears in `ctr images list`. The same image imports cleanly
# from a file. After each import we re-list and grep for the tag, so a
# silent drop fails the recipe instead of poisoning the cluster with a
# half-imported tag that surfaces hours later as ImagePullBackOff in
# await-egg-deploy.sh. Tarballs go in /var/tmp (disk-backed) -- the sandbox
# image is ~12 GB, so /tmp (tmpfs) would consume that much RAM.
#
# Skip-and-tag unchanged images (issue #2999 lever A). `docker save` +
# `ctr import` of the ~12 GB egg-sandbox every redeploy is the dominant churn
# that fragments btrfs and pushes the root fs into DiskPressure, after which
# kubelet's image GC evicts the freshly-imported (not-yet-referenced) tags
# mid-run and check-egg-images-present.sh fails. So per image we compare the
# current docker image id against a marker of the last id we imported (under
# ${XDG_CACHE_HOME:-$HOME/.cache}/egg/k3s-import-ids/<image>.id); when it is
# unchanged AND containerd still holds docker.io/library/<image>:latest we
# just `ctr images tag` the wanted tags off :latest instead of re-importing.
# The :latest-present guard is load-bearing: if GC already evicted the image
# we fall through to a real import. reap-stale-egg-images.sh deliberately
# keeps :latest, so it stays a stable retag source across deploys. The retag
# path further assumes nothing else has re-pointed containerd's :latest to
# non-egg content -- no tool in this repo does so, and an out-of-band retag
# would only mismatch if :$(EGG_IMAGE_TAG) is also absent (the inner grep
# guard skips when it is already present).
# NOTE (#2999): k3s-import is the save+import publish path. The default
# `make redeploy` flow uses it only for the images excluded from the registry
# subset (the sandbox, which must not be pushed to any registry by default —
# see EGG_REGISTRY_IMAGES above); with EGG_IMAGE_REGISTRY= (empty) it covers
# everything, e.g. for CI. K3S_IMPORT_IMAGES narrows the image set
# (k3s-publish passes the registry-subset complement). It deals in BARE
# image names (egg-sandbox:<tag>), matching what `deploy` writes into the
# manifests for non-registry images.
K3S_IMPORT_IMAGES ?= $(EGG_ALL_IMAGES)
k3s-import: SHELL := /bin/bash
k3s-import: sudo-keepalive  ## Import built images into k3s (registry-excluded set)
	@set -euo pipefail; \
	tmp=$$(mktemp -d -p /var/tmp egg-k3s-import.XXXXXX); \
	trap 'rm -rf "$$tmp"' EXIT; \
	tags="latest"; \
	if [ "$(EGG_IMAGE_TAG)" != "latest" ]; then tags="$$tags $(EGG_IMAGE_TAG)"; fi; \
	id_dir="$${XDG_CACHE_HOME:-$$HOME/.cache}/egg/k3s-import-ids"; \
	mkdir -p "$$id_dir"; \
	for image in $(K3S_IMPORT_IMAGES); do \
		cur_id=$$(docker image inspect "$$image:$(EGG_IMAGE_TAG)" --format '{{.Id}}'); \
		marker="$$id_dir/$$image.id"; \
		prev_id=$$(cat "$$marker" 2>/dev/null || true); \
		present=$$(sudo k3s ctr images list -q); \
		if [ "$$cur_id" = "$$prev_id" ] && grep -qx "docker.io/library/$$image:latest" <<<"$$present"; then \
			echo ">>> $$image unchanged ($$cur_id), :latest present in containerd; retagging instead of re-importing"; \
			for tag in $$tags; do \
				if ! grep -qx "docker.io/library/$$image:$$tag" <<<"$$present"; then \
					echo "    tag docker.io/library/$$image:latest -> :$$tag"; \
					sudo k3s ctr images tag "docker.io/library/$$image:latest" "docker.io/library/$$image:$$tag"; \
					present="$$present"$$'\n'"docker.io/library/$$image:$$tag"; \
				fi; \
			done; \
		else \
			refs=""; \
			for tag in $$tags; do refs="$$refs $$image:$$tag"; done; \
			f="$$tmp/$$image.tar"; \
			echo ">>> importing$$refs (one tarball: tags share all layers)"; \
			docker save $$refs -o "$$f"; \
			sudo k3s ctr images import "$$f"; \
			rm -f "$$f"; \
			printf '%s\n' "$$cur_id" > "$$marker.tmp" && mv -f "$$marker.tmp" "$$marker"; \
			present=$$(sudo k3s ctr images list -q); \
		fi; \
		for tag in $$tags; do \
			if ! grep -qx "docker.io/library/$$image:$$tag" <<<"$$present"; then \
				echo "ERROR: $$image:$$tag not present in k3s containerd after import/retag" >&2; \
				exit 1; \
			fi; \
		done; \
	done

k3s-teardown:  ## Remove k3s
	/usr/local/bin/k3s-uninstall.sh || true
	@echo "k3s removed"
