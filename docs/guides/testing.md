# Testing Guide

This guide covers how to run tests locally, how the CI pipeline gates PRs,
and how the SDLC tester agent runs integration tests via Docker-in-Docker.

## Test Tiers

| Tier | Framework | Command | Scope |
|------|-----------|---------|-------|
| Unit tests | pytest | `make test` | Gateway, orchestrator, shared modules |
| Lint | ruff, mypy, shellcheck, yamllint, hadolint, actionlint | `make lint` | All code |
| Integration tests | pytest + Docker | `make test-integration` | Full gateway + orchestrator stack |
| Local pipeline tests | pytest + Docker Compose | See below | End-to-end SDLC pipeline |

## Running Tests Locally

### Unit tests

```bash
make test
# or directly:
PYTHONPATH=shared:gateway:orchestrator pytest tests/ gateway/tests/ orchestrator/tests/ -v
```

### Lint

```bash
make lint
# or directly:
ruff check . && ruff format --check . && mypy gateway shared sandbox
```

### Integration tests

```bash
make test-integration
```

This runs `pytest integration_tests -v -m integration --timeout=300`. The test
fixtures handle Docker image builds and compose stack setup automatically via
`conftest.py`.

To build images manually before running (useful for debugging):

```bash
docker build -t egg-gateway -f gateway/Dockerfile .
docker build -t egg-orchestrator -f orchestrator/Dockerfile .
docker build -t mock-sandbox -f integration_tests/local_pipeline/mock-sandbox/Dockerfile integration_tests/local_pipeline/mock-sandbox
```

Clean up after integration tests:

```bash
docker compose -f integration_tests/docker-compose.yml down -v --remove-orphans
docker compose -f integration_tests/local_pipeline/docker-compose.yml down -v --remove-orphans
```

### Local pipeline tests

These tests build a full gateway + orchestrator stack via Docker Compose and
run real SDLC pipeline flows with a mock sandbox:

```bash
docker compose -f integration_tests/local_pipeline/docker-compose.yml up -d --build
PYTHONPATH=shared pytest integration_tests/local_pipeline -v -m integration --timeout=300
docker compose -f integration_tests/local_pipeline/docker-compose.yml down -v --remove-orphans
```

## CI Pipeline

Three GitHub Actions workflows gate every PR:

| Workflow | File | Checks |
|----------|------|--------|
| **Lint** | `.github/workflows/lint.yml` | ruff, mypy, shellcheck, yamllint, hadolint, actionlint, custom checks |
| **Test** | `.github/workflows/test.yml` | Unit tests, security scan (bandit) |
| **Integration Tests** | `.github/workflows/test-integration.yml` | Full integration test suite |

All three workflows trigger on `pull_request` events (`opened`, `synchronize`,
`reopened`) and are also callable as reusable workflows via `workflow_call`.

### Concurrency and path filtering

All three workflows use concurrency groups with `cancel-in-progress: true`,
so rapid pushes cancel stale CI runs instead of stacking them. The
concurrency group key includes the branch ref (e.g.,
`integration-tests-<branch>`), so runs for different PRs never cancel each
other.

The Integration Tests workflow has path filters so it only triggers when
relevant code changes (gateway, orchestrator, shared, sandbox, integration
tests, dependency files, or the workflow itself). Docs-only or config-only
PRs skip this expensive workflow.

### Autofix

When any of these workflows fails on a PR, the
[on-check-failure.yml](../../.github/workflows/on-check-failure.yml) watcher
triggers the autofix bot, which attempts to fix lint and test issues
automatically. It watches for `Lint` and `Test` workflow completions.
Integration test failures are not watched by the autofix bot because they
are typically Docker/compose infrastructure issues rather than code issues
the bot can fix.

### Non-required checks

Integration tests start as non-required checks since they have never run in
CI before. There may be environment-specific differences between
`ubuntu-latest` (x86_64) and development hosts (aarch64). Promote to
required after stability is confirmed.

## DinD Self-Testing (SDLC Pipeline)

The SDLC tester agent runs in a sandbox without Docker socket access. To
run full-stack integration tests from within the pipeline, the orchestrator
provisions a Docker-in-Docker sidecar.

### Architecture

```
Orchestrator (Docker socket access)
  |
  +-- DindManager
  |     |
  |     +-- docker:27-dind-rootless container (--privileged)
  |           Exposes Docker daemon on tcp://<ip>:2375
  |
  +-- Tester Sandbox (no Docker socket)
        DOCKER_HOST=tcp://<dind-ip>:2375
        Runs: pytest integration_tests/local_pipeline
```

### How it works

1. The orchestrator sets `integration_test_enabled=True` when spawning the
   tester agent during the check phase.
2. `ContainerSpawner` creates a `DindManager` which starts a
   `docker:27-dind-rootless` container with `--privileged`.
3. The DinD daemon starts and the manager waits for TCP health on port 2375.
4. Required Docker images (`egg-gateway`, `egg-orchestrator`, `mock-sandbox`)
   are pre-loaded into the DinD daemon via `docker save | docker load`.
5. The tester sandbox receives `DOCKER_HOST=tcp://<dind-ip>:2375` in its
   environment.
6. The tester runs `pytest` which uses the DinD daemon for all Docker
   operations (compose up, container creation, etc.).
7. After the tester completes, the DinD container is torn down.

### Key files

| File | Purpose |
|------|---------|
| `orchestrator/dind_manager.py` | DinD sidecar lifecycle management |
| `orchestrator/container_spawner.py` | DinD provisioning for tester agents |
| `orchestrator/multi_agent.py` | `integration_test_enabled` flag propagation |
| `integration_tests/local_pipeline/conftest.py` | DinD-aware test fixtures |

### Trust model

The DinD architecture follows the same trust model as `DevserverManager`:
- The **orchestrator** (trusted, has Docker socket) provisions infrastructure
- The **sandbox** (untrusted, no Docker socket) only consumes it via TCP

The DinD container runs with `--privileged` but uses the rootless
(`docker:27-dind-rootless`) image to reduce attack surface. It is managed
entirely by the orchestrator and torn down after the tester completes.

### Security controls

**Watchdog timeout.** The DinD container has a maximum lifetime of 10
minutes (`DIND_MAX_LIFETIME_SECONDS`), enforced by a background watchdog
timer in `DindManager`. If the tester agent hangs or the orchestrator
crashes, the watchdog auto-kills the privileged container. The timer is
cancelled on normal teardown to avoid double-cleanup.

**Resource limits.** The DinD container is constrained to 2 CPUs and 2 GB
memory, matching the devserver sidecar limits.

**Cleanup on all error paths.** If the gateway session registration or
container creation fails after the DinD sidecar has been provisioned, the
spawner tears down the DinD container before propagating the error. This
prevents privileged container leaks on partial failures.

### Known limitations

- **Production Docker path not wired (Phase 2 deferred).** The
  `integration_test_enabled` parameter propagates correctly through the
  `spawn_fn` callback path (used in tests and local mode), but the
  production callers in `pipelines.py` do not pass it yet. DinD
  provisioning via the Docker-based spawn path will be wired in Phase 2.
- **Lint and Test workflows have no path filters.** Only the Integration
  Tests workflow uses path filters. Lint and Test run on all PRs regardless
  of which files changed. This is acceptable because they are inexpensive
  compared to integration tests.

## Troubleshooting

### Integration tests fail with "port already in use"

```bash
docker compose -f integration_tests/docker-compose.yml down -v --remove-orphans
docker compose -f integration_tests/local_pipeline/docker-compose.yml down -v --remove-orphans
docker network prune -f
```

### Orphaned sandbox containers from previous test runs

```bash
docker ps -a --filter "name=egg-sandbox-egg-" --format "{{.Names}}" | xargs -r docker rm -f
```

### DinD daemon not healthy

Check that the DinD container is running and the TCP port is accessible:

```bash
docker ps --filter "label=egg.dind=true"
docker logs <dind-container-id>
```

### Image pre-load fails

Ensure the images exist on the host Docker daemon before pre-loading:

```bash
docker images | grep -E "egg-gateway|egg-orchestrator|mock-sandbox"
```
