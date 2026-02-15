# Plan: Full-stack DinD integration testing for egg self-validation

> Issue: #647 | Phase: plan

## Summary

Replace the host Docker socket mount (`/var/run/docker.sock`) in the local pipeline integration test stack with a rootless Docker-in-Docker (DinD) sidecar (`docker:27-dind-rootless`). This sandboxes all container operations within an isolated Docker daemon, eliminating host Docker access from the test orchestrator and establishing the foundation for self-validation testing (#645).

No orchestrator code changes are needed — `DockerClient` already supports `DOCKER_HOST` via environment variable (docker_client.py:95-109), and `ContainerSpawner` already reads `EGG_ISOLATED_NETWORK` from environment (container_spawner.py:55). The work is purely compose configuration and test fixture updates.

## Approach

**Rootless DinD sidecar** (Approach A from the architecture analysis). The DinD container joins the test compose networks so the orchestrator can reach it via `tcp://dind:2375`. Mock-sandbox images are loaded into DinD at setup time via `docker save | docker load`. Named volumes (state, worktrees, certs) are shared between compose services so DinD-spawned containers can access the same data.

Key design decisions:
- **Rootless over standard DinD** — Both require `--privileged`. Rootless runs the inner daemon as non-root, reducing blast radius. ~5s startup overhead is acceptable for integration tests.
- **Save/load for image transfer** — Build mock-sandbox on the host daemon (fast, cached), then transfer to DinD via pipe. The image is ~5MB (alpine + curl).
- **Network bridging** — DinD joins the test networks directly. DinD-spawned containers communicate with the gateway via DinD's network position. The orchestrator's `EGG_ISOLATED_NETWORK` override ensures spawned containers join the correct network.
- **CI deferred** — CI integration (GitHub Actions `--privileged` support) is a follow-up concern. The fixture gracefully skips when DinD is unavailable.

## Implementation Phases

### Phase 1: Add DinD Service to Compose Stack

**Goal**: Add `docker:27-dind-rootless` as a compose service, wire networking, update orchestrator to use DinD instead of host Docker socket.

**Tasks**:

- [TASK-1-1] Add `dind` service to `integration_tests/local_pipeline/docker-compose.yml` — Define a `docker:27-dind-rootless` service with `privileged: true`, `/dev/net/tun` device, `DOCKER_TLS_CERTDIR=` (disable TLS for test simplicity), and a health check (`docker info`). Connect it to both `egg-test-isolated` and `egg-test-external` networks with static IPs (e.g., 172.40.0.4 and 172.41.0.4). Mount shared named volumes (state, worktrees, certs) so DinD-spawned containers can access them.
  - **File**: `integration_tests/local_pipeline/docker-compose.yml`
  - **Acceptance**: `docker compose up dind` starts successfully. `docker -H tcp://localhost:<port> info` returns valid daemon info. DinD container is on both test networks.

- [TASK-1-2] Update orchestrator service to use DinD — Change `DOCKER_HOST` from `unix:///var/run/docker.sock` to `tcp://dind:2375`. Remove the `/var/run/docker.sock` volume mount. Add `depends_on: dind: condition: service_healthy`.
  - **File**: `integration_tests/local_pipeline/docker-compose.yml`
  - **Acceptance**: Orchestrator service has no Docker socket mount. `DOCKER_HOST` points to DinD. Orchestrator waits for DinD health before starting.

### Phase 2: Update Test Fixture for Image Loading

**Goal**: Load the mock-sandbox image into the DinD daemon at test setup time so the orchestrator can spawn it.

**Tasks**:

- [TASK-2-1] Add image loading step to `local_pipeline_stack` fixture — After compose up and health checks pass, transfer the mock-sandbox image from the host Docker daemon into DinD using `docker save mock-sandbox:latest | docker -H tcp://localhost:<dind_port> load`. Determine the DinD mapped port via `docker compose port dind 2375`.
  - **File**: `integration_tests/local_pipeline/conftest.py`
  - **Acceptance**: After fixture setup, `docker -H tcp://localhost:<dind_port> images` shows `mock-sandbox:latest`. If image load fails, test session fails with clear error message.

- [TASK-2-2] Update override file generation to include DinD repo volume — The override file currently adds per-repo volumes to `gateway` and `orchestrator`. Add the same volume to the `dind` service so DinD-spawned containers can mount the repo path.
  - **File**: `integration_tests/local_pipeline/conftest.py`
  - **Acceptance**: Override file includes `dind` service with repo volume mount. DinD-internal path matches what `EGG_HOST_REPO_MAP` references.

- [TASK-2-3] Update `EGG_HOST_REPO_MAP` to use DinD-internal paths — Since the orchestrator now spawns containers via DinD, volume mount paths must resolve inside DinD, not on the host. Update the repo map values to reference the path where the repo is mounted inside the DinD container (same path as inside the orchestrator: `/home/egg/repos/<repo_name>`).
  - **File**: `integration_tests/local_pipeline/conftest.py`
  - **Acceptance**: `EGG_HOST_REPO_MAP` values are DinD-internal paths. Spawned mock-sandbox containers can access the repo volume.

### Phase 3: Update Orphan Cleanup for DinD

**Goal**: Ensure orphaned container cleanup targets the DinD daemon instead of the host daemon.

**Tasks**:

- [TASK-3-1] Update `_cleanup_orphaned_containers` to target DinD — The cleanup function currently runs `docker ps` and `docker rm` against the host daemon. With DinD, orphaned test containers live inside DinD. Update the function to accept an optional `docker_host` parameter and target the DinD endpoint when available. During fixture teardown, compose down already removes the DinD container (and all its children), so this is mainly needed for pre-test cleanup of leftover DinD instances from crashed previous runs.
  - **File**: `integration_tests/local_pipeline/conftest.py`
  - **Acceptance**: Orphan cleanup works correctly. No stale test containers remain after test runs.

### Phase 4: Validate Existing Tests Against DinD Stack

**Goal**: Run the full existing integration test suite and fix any failures caused by the DinD switch.

**Tasks**:

- [TASK-4-1] Run full integration test suite against DinD-backed stack — Execute `PYTHONPATH=shared pytest integration_tests/local_pipeline/ -v -m integration --timeout=300`. Identify and categorize any failures (network connectivity, volume mounts, timing, container lifecycle).
  - **Files**: `integration_tests/local_pipeline/test_local_pipeline.py`, `integration_tests/local_pipeline/test_concurrent_pipelines.py`, `integration_tests/local_pipeline/test_error_recovery.py`, `integration_tests/local_pipeline/test_hitl_edge_cases.py`, `integration_tests/local_pipeline/test_signals.py`, `integration_tests/local_pipeline/test_api_validation.py`, `integration_tests/local_pipeline/test_unified_pipeline_behavior.py`, `integration_tests/local_pipeline/test_worktree_integration.py`
  - **Acceptance**: All existing integration tests pass (or any pre-existing failures are documented).

- [TASK-4-2] Fix network connectivity issues if DinD-spawned containers cannot reach gateway — If containers spawned by DinD cannot join the outer compose networks directly, implement a workaround: either use `network_mode: host` on DinD, create test networks as `external` so both daemons can see them, or route through DinD's IP. This is the highest-risk task and may require iterative debugging.
  - **Files**: `integration_tests/local_pipeline/docker-compose.yml`, `integration_tests/local_pipeline/conftest.py`
  - **Acceptance**: Mock-sandbox containers spawned inside DinD can `curl` the gateway health endpoint. All pipeline phases that involve container-to-gateway communication pass.

- [TASK-4-3] Adjust timeouts if needed — DinD adds ~5s startup overhead and may add latency to container operations. If tests fail due to timeouts, increase relevant timeout values in test fixtures or compose health checks.
  - **Files**: `integration_tests/local_pipeline/conftest.py`, `integration_tests/local_pipeline/docker-compose.yml`
  - **Acceptance**: No test failures caused by timeout. Health check retries and start_period accommodate DinD startup.

## Network Topology After Change

```
Host Docker Daemon
  └─ compose stack (egg-lp-test-*)
       ├─ gateway       (172.40.0.2 / 172.41.0.2)
       ├─ orchestrator   (172.40.0.3 / 172.41.0.3)  ← DOCKER_HOST=tcp://dind:2375
       └─ dind (rootless) (172.40.0.4 / 172.41.0.4)
            └─ mock-sandbox containers (spawned by orchestrator via DinD)
                 └─ communicate with gateway via DinD's network position
```

## Test Strategy

- **Primary validation**: Run the full existing integration test suite (`pytest integration_tests/local_pipeline/ -v -m integration --timeout=300`). These tests exercise the complete pipeline lifecycle (create → start → spawn containers → run phases → review → complete) and cover the critical paths: container spawning, environment injection, volume mounts, gateway connectivity, and artifact writing.
- **No new tests required**: The existing tests are sufficient to validate the DinD switch. They test the same Docker operations (create, start, logs, stop, remove) but now through DinD instead of the host socket.
- **Early network validation**: In Phase 1, before proceeding to full test runs, manually verify that a container spawned inside DinD can reach the gateway by running a minimal connectivity test.
- **Graceful degradation**: The fixture should skip tests (not fail hard) if DinD is unavailable, preserving the ability to run with the host socket in environments that don't support DinD.

## Risks

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| DinD-spawned containers can't join outer compose networks | Medium | High | Early validation in Phase 1. Fallback: use DinD's IP as network bridge or host networking mode |
| Volume path mismatch between DinD-internal and host paths | Medium | Medium | Use consistent named volumes and DinD-internal paths in EGG_HOST_REPO_MAP |
| DinD startup adds too much latency for health checks | Low | Low | Increase health check start_period and retries |
| Rootless DinD has overlay2/cgroup limitations on some kernels | Low | Medium | Validated on target host (Linux 6.17 aarch64). Fall back to standard DinD if needed |

## Files Modified

| File | Changes |
|------|---------|
| `integration_tests/local_pipeline/docker-compose.yml` | Add `dind` service; update orchestrator `DOCKER_HOST`; remove socket mount; add DinD to depends_on |
| `integration_tests/local_pipeline/conftest.py` | Image loading into DinD; override file includes DinD volumes; EGG_HOST_REPO_MAP uses DinD paths; orphan cleanup targets DinD |

## Open Questions

1. **Can DinD-spawned containers directly join outer compose networks?** The DinD daemon is isolated — it may not see networks created by the host daemon. This must be validated early in Phase 1. If it fails, the fallback is routing through DinD's IP address on the compose network.

2. **Should tests have a direct handle to the DinD Docker endpoint?** Most tests interact via the orchestrator API, but adding `dind_docker_host` to `LocalPipelineStack` would allow direct container inspection in debugging scenarios.

```yaml
# yaml-tasks
pr:
  title: "Replace host Docker socket with rootless DinD sidecar"
  description: |
    Replace the host Docker socket mount in the local pipeline integration
    test stack with a rootless DinD sidecar (docker:27-dind-rootless).
    This sandboxes all container operations within an isolated Docker daemon,
    removing host Docker access from the test orchestrator. No orchestrator
    code changes needed — DockerClient already supports DOCKER_HOST.

    Fixes #647
phases:
  - id: 1
    name: Add DinD Service to Compose Stack
    goal: Add rootless DinD sidecar and wire orchestrator to use it
    tasks:
      - id: TASK-1-1
        description: Add dind service (docker:27-dind-rootless) with health check, networks, and shared volumes
        acceptance: DinD starts healthy and is reachable on both test networks
        files:
          - integration_tests/local_pipeline/docker-compose.yml
      - id: TASK-1-2
        description: Update orchestrator DOCKER_HOST to tcp://dind:2375 and remove docker.sock mount
        acceptance: Orchestrator connects to DinD; no host socket mount in compose
        files:
          - integration_tests/local_pipeline/docker-compose.yml
  - id: 2
    name: Update Test Fixture for Image Loading
    goal: Load mock-sandbox image into DinD and configure volume paths
    tasks:
      - id: TASK-2-1
        description: Add docker save/load step to transfer mock-sandbox image into DinD after compose up
        acceptance: mock-sandbox:latest is available inside DinD daemon after fixture setup
        files:
          - integration_tests/local_pipeline/conftest.py
      - id: TASK-2-2
        description: Update override file to mount repo volume into DinD service
        acceptance: DinD has repo volume mounted at same path as orchestrator
        files:
          - integration_tests/local_pipeline/conftest.py
      - id: TASK-2-3
        description: Update EGG_HOST_REPO_MAP to use DinD-internal paths
        acceptance: Spawned containers receive correct volume mount paths
        files:
          - integration_tests/local_pipeline/conftest.py
  - id: 3
    name: Update Orphan Cleanup for DinD
    goal: Target orphaned container cleanup at DinD daemon
    tasks:
      - id: TASK-3-1
        description: Update _cleanup_orphaned_containers to optionally target DinD endpoint
        acceptance: Pre-test cleanup removes stale containers from DinD, not host
        files:
          - integration_tests/local_pipeline/conftest.py
  - id: 4
    name: Validate Existing Tests Against DinD Stack
    goal: All existing integration tests pass with DinD-backed stack
    tasks:
      - id: TASK-4-1
        description: Run full integration test suite and identify failures
        acceptance: All tests pass or pre-existing failures documented
        files:
          - integration_tests/local_pipeline/test_local_pipeline.py
          - integration_tests/local_pipeline/test_concurrent_pipelines.py
          - integration_tests/local_pipeline/test_error_recovery.py
          - integration_tests/local_pipeline/test_hitl_edge_cases.py
          - integration_tests/local_pipeline/test_signals.py
          - integration_tests/local_pipeline/test_api_validation.py
          - integration_tests/local_pipeline/test_unified_pipeline_behavior.py
          - integration_tests/local_pipeline/test_worktree_integration.py
      - id: TASK-4-2
        description: Fix network connectivity if DinD-spawned containers cannot reach gateway
        acceptance: Mock-sandbox containers can curl gateway health endpoint
        files:
          - integration_tests/local_pipeline/docker-compose.yml
          - integration_tests/local_pipeline/conftest.py
      - id: TASK-4-3
        description: Adjust timeouts for DinD startup overhead if needed
        acceptance: No timeout-related test failures
        files:
          - integration_tests/local_pipeline/conftest.py
          - integration_tests/local_pipeline/docker-compose.yml
```

---

### Ready for Review

<!-- egg-phase-approval -->
- [ ] Approve and advance to implement phase

---

*Authored-by: egg*
