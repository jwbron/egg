# Plan: Wire full-stack integration tests into CI and SDLC pipeline

> Issue: #647 | Phase: plan

## Summary

Get the existing 91+ integration tests and all lint/unit test workflows
running automatically on every PR in GitHub Actions, then enable egg's tester
agent to run those same tests from within the SDLC pipeline via an
orchestrator-managed Docker-in-Docker sidecar. This is delivered as a single PR
with two independent phases: CI gating (Phase 1) and SDLC self-testing
(Phase 2).

## Approach

### Phase 1: CI Integration (Option A from architect analysis)

Add `pull_request` triggers directly to the three existing reusable workflows
(`lint.yml`, `test.yml`, `test-integration.yml`). GitHub Actions supports dual
triggers (`workflow_call` + `pull_request`) on the same workflow file, so this
is purely additive. Workflow names (`Lint`, `Test`, `Integration Tests`) are
preserved, which is critical because `on-check-failure.yml` watches for those
exact names via `workflow_run` events to trigger autofix.

The `test-integration.yml` workflow currently only builds the `egg-gateway`
image. It needs to also build `egg-orchestrator` and `mock-sandbox` so the
local pipeline tests can use them. The cleanup step also needs to cover the
`integration_tests/local_pipeline/docker-compose.yml` stack in addition to
the existing `integration_tests/docker-compose.yml` gateway-only stack.

The autofix watcher (`on-check-failure.yml`) is updated to also watch for
`Integration Tests` workflow completions, so integration test failures can
trigger the autofix bot.

**Why not a caller `ci.yml`?** A wrapper workflow would change the
`workflow_run` event name from `Lint`/`Test` to `CI`, breaking the autofix
pipeline. Adding triggers directly avoids this entirely.

### Phase 2: SDLC Self-Testing via DinD (Option C from architect analysis)

The tester agent sandbox has no Docker socket access (by design). To run
full-stack integration tests from within the SDLC pipeline, the orchestrator
spawns a `docker:27-dind-rootless` sidecar container alongside the tester
sandbox. The orchestrator pre-loads the required images (`egg-gateway`,
`egg-orchestrator`, `mock-sandbox`) into the DinD daemon, then injects
`DOCKER_HOST=tcp://<dind-ip>:2375` into the tester's environment. The tester
runs pytest against the nested test stack without ever having Docker socket
access.

This follows the same trust architecture as `DevserverManager`
(`orchestrator/devserver.py`): the orchestrator provisions infrastructure,
the sandbox only consumes it.

### Design Decisions

1. **PR triggers added directly** (not via caller workflow) to preserve
   autofix workflow naming dependency.
2. **Pre-build all images in CI** for caching and clear failure reporting,
   even though the conftest fixture also builds them. The fixture should
   detect pre-built images and skip redundant builds.
3. **No path filtering initially** — run all checks on all PRs to catch
   unexpected regressions. Add path filtering later if CI time is a concern.
4. **Start as non-required checks** — integration tests have never run in CI.
   There may be environment-specific flakiness on `ubuntu-latest` (x86_64)
   vs the development host (aarch64 Asahi). Promote to required after
   stability is confirmed.
5. **DinD uses `docker:27-dind-rootless`** with `--privileged` flag, managed
   by the orchestrator. Rootless mode reduces attack surface. The DinD
   container is torn down after the tester completes.
6. **Image pre-loading via `docker save | docker load`** over TCP — standard
   approach that works in private mode (no registry access needed).

## Phases

### Phase 1: CI Workflow Changes

Add `pull_request` triggers to the three reusable test/lint workflows and fix
`test-integration.yml` to build all required Docker images. Update autofix
watcher. These are purely additive changes to 4 workflow files.

### Phase 2: Orchestrator DinD Support

Create a `DindManager` module in the orchestrator, wire it into the container
spawner for the tester role, and make the local pipeline conftest DinD-aware.
Add integration tests for the DinD lifecycle.

### Phase 3: Documentation

Document the CI test pipeline and DinD self-testing architecture.

## Files Modified

| File | Phase | Change |
|------|-------|--------|
| `.github/workflows/lint.yml` | 1 | Add `pull_request` trigger |
| `.github/workflows/test.yml` | 1 | Add `pull_request` trigger |
| `.github/workflows/test-integration.yml` | 1 | Add `pull_request` trigger, build orchestrator + mock-sandbox images, add local_pipeline cleanup |
| `.github/workflows/on-check-failure.yml` | 1 | Add `Integration Tests` to watched workflows |
| `orchestrator/dind_manager.py` | 2 | New module: DinD sidecar lifecycle management |
| `orchestrator/container_spawner.py` | 2 | Optionally provision DinD sidecar for tester role |
| `orchestrator/multi_agent.py` | 2 | Pass `integration_test_enabled` flag to tester spawn |
| `integration_tests/local_pipeline/conftest.py` | 2 | Detect `DOCKER_HOST` and adapt fixture for DinD mode |
| `orchestrator/tests/test_dind_manager.py` | 2 | Unit tests for DindManager |
| `integration_tests/local_pipeline/test_dind_integration.py` | 2 | Integration tests for DinD lifecycle |
| `docs/guides/testing.md` | 3 | Document CI and DinD test infrastructure |

## Test Strategy

### Phase 1 Testing

Phase 1 changes are workflow YAML files — they are tested by the CI system
itself. The PR for this issue serves as the validation: all three workflows
should trigger and appear as PR checks. Verify:

- `Lint` check appears and runs (or fails with expected issues)
- `Test` check appears and runs
- `Integration Tests` check appears, builds both images, runs local pipeline
  tests, and cleans up both compose stacks
- Workflow names in the GitHub UI match `Lint`, `Test`, `Integration Tests`
  (for autofix compatibility)

### Phase 2 Testing

**Unit tests** (`orchestrator/tests/test_dind_manager.py`):
- DindManager initialization and configuration
- Health check polling logic (mock TCP connection)
- Image pre-load command construction
- Cleanup on success and failure paths

**Integration tests** (`integration_tests/local_pipeline/test_dind_integration.py`):
- DinD sidecar starts and becomes healthy
- Images pre-loaded into DinD are usable (`docker images` via TCP)
- Tester sandbox receives correct `DOCKER_HOST` environment variable
- DinD cleanup after tester completes (no orphaned containers)
- Full local pipeline test suite runs against DinD-backed stack

### Regression Testing

- Existing unit tests (`make test`) pass without modification
- Existing integration tests pass in both direct Docker and DinD modes
- Tier 1/2 pipelines (no DinD) are unaffected

## Risks

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Integration tests flaky on `ubuntu-latest` (arch differences) | Medium | Medium | Start as non-required checks; add retry/timeout adjustments |
| CI time increase (~5-10 min for integration tests) | High | Low | Docker layer caching; path filtering as future optimization |
| DinD startup latency adds to tester execution time | Medium | Low | Pre-warm DinD daemon; pre-load images concurrently |
| DinD `--privileged` flag expands attack surface | Low | Medium | Rootless DinD; managed by trusted orchestrator; torn down after use |
| Autofix ineffective for Docker-related integration failures | Medium | Low | Monitor and remove `Integration Tests` from autofix watcher if needed |
| Dual `workflow_call` + `pull_request` triggers cause double runs | Low | Low | No existing caller triggers on PRs; theoretical risk only |

---

```yaml
# yaml-tasks
pr:
  title: "Wire integration tests into CI and add DinD self-testing"
  description: |
    Adds pull_request triggers to lint.yml, test.yml, and test-integration.yml
    so all tests gate PRs in GitHub Actions. Fixes test-integration.yml to
    build both gateway and orchestrator images. Adds orchestrator-managed
    Docker-in-Docker sidecar support so egg's tester agent can run full-stack
    integration tests from within the SDLC pipeline sandbox.
phases:
  - id: 1
    name: CI workflow changes
    goal: Get lint, unit tests, and integration tests running on every PR
    tasks:
      - id: TASK-1-1
        description: Add pull_request trigger (opened, synchronize, reopened) to lint.yml alongside existing workflow_call and workflow_dispatch triggers
        acceptance: lint.yml has pull_request trigger; workflow name remains "Lint"; workflow_call outputs still work; actionlint passes on the modified file
        files:
          - .github/workflows/lint.yml
      - id: TASK-1-2
        description: Add pull_request trigger (opened, synchronize, reopened) to test.yml alongside existing workflow_call and workflow_dispatch triggers
        acceptance: test.yml has pull_request trigger; workflow name remains "Test"; workflow_call outputs still work; actionlint passes on the modified file
        files:
          - .github/workflows/test.yml
      - id: TASK-1-3
        description: "Fix test-integration.yml: add pull_request trigger, add build steps for egg-orchestrator (orchestrator/Dockerfile) and mock-sandbox (integration_tests/local_pipeline/mock-sandbox/Dockerfile), add cleanup step for integration_tests/local_pipeline/docker-compose.yml stack"
        acceptance: test-integration.yml triggers on PRs; builds egg-gateway, egg-orchestrator, and mock-sandbox images before running pytest; cleanup tears down both compose stacks (gateway-only and local_pipeline); workflow name remains "Integration Tests"; actionlint passes
        files:
          - .github/workflows/test-integration.yml
      - id: TASK-1-4
        description: "Add 'Integration Tests' to the workflows list in on-check-failure.yml (line 8) so integration test failures also trigger the autofix bot"
        acceptance: on-check-failure.yml watches for ["Lint", "Test", "Integration Tests"] workflow completions; actionlint passes
        files:
          - .github/workflows/on-check-failure.yml
  - id: 2
    name: DinD manager and orchestrator integration
    goal: Enable tester agent to run integration tests via orchestrator-managed DinD sidecar
    tasks:
      - id: TASK-2-1
        description: "Create orchestrator/dind_manager.py with DindManager class that manages docker:27-dind-rootless sidecar lifecycle: spawn with --privileged, wait for daemon health via TCP, pre-load images via docker save/load, attach to tester network, cleanup on completion. Follow DevserverManager architectural pattern."
        acceptance: DindManager can start a DinD container, wait for health, pre-load images, return the daemon URL, and clean up. Resource limits enforced (CPU, memory, timeout). Comprehensive error handling for startup failures.
        files:
          - orchestrator/dind_manager.py
      - id: TASK-2-2
        description: Add unit tests for DindManager covering initialization, health check polling, image pre-load command construction, and cleanup paths (success and failure)
        acceptance: Tests pass; cover both happy path and error scenarios (daemon startup failure, image load failure, timeout); mock Docker client interactions
        files:
          - orchestrator/tests/test_dind_manager.py
      - id: TASK-2-3
        description: "Extend container_spawner.py spawn_container() to optionally provision a DinD sidecar when spawning a tester agent with integration_test_enabled=True. Inject DOCKER_HOST=tcp://<dind-ip>:2375 into tester environment. Manage DinD lifecycle alongside tester container (start before, stop after)."
        acceptance: Tester container spawned with DinD receives DOCKER_HOST env var pointing to running DinD daemon. DinD is torn down when tester container stops. Non-tester containers and testers without integration_test_enabled are unaffected.
        files:
          - orchestrator/container_spawner.py
      - id: TASK-2-4
        description: "Update multi_agent.py to pass integration_test_enabled flag when spawning tester agent during the check phase, based on pipeline configuration or the presence of integration tests in the repository"
        acceptance: Tester agent in check phase receives integration_test_enabled=True when appropriate. Other phases and roles are unaffected.
        files:
          - orchestrator/multi_agent.py
      - id: TASK-2-5
        description: "Make integration_tests/local_pipeline/conftest.py DinD-aware: detect DOCKER_HOST env, skip mock-sandbox build when image already exists, adjust compose commands to use the DinD daemon"
        acceptance: Conftest fixture works in both direct Docker mode (CI and local) and DinD mode (SDLC pipeline). Image existence check prevents redundant builds. Compose stack starts against the correct Docker daemon.
        files:
          - integration_tests/local_pipeline/conftest.py
      - id: TASK-2-6
        description: Add integration tests for DinD lifecycle (sidecar start, health, image pre-load, tester execution, teardown) in a new test file
        acceptance: Tests verify end-to-end DinD workflow; DinD container starts and becomes healthy; pre-loaded images are usable; cleanup removes DinD container
        files:
          - integration_tests/local_pipeline/test_dind_integration.py
  - id: 3
    name: Documentation
    goal: Document the CI test pipeline and DinD self-testing architecture
    tasks:
      - id: TASK-3-1
        description: "Create docs/guides/testing.md documenting: how to run integration tests locally (make test-integration), CI workflow behavior (which workflows run on PRs, how autofix works), DinD architecture for SDLC self-testing, and troubleshooting common failures"
        acceptance: Guide covers local testing, CI pipeline, and DinD architecture. Cross-references relevant workflow files and orchestrator modules.
        files:
          - docs/guides/testing.md
```

---

*Authored-by: egg*
