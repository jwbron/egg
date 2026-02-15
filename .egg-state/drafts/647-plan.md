# Plan: Replace host Docker socket with rootless DinD sidecar

**Issue**: #647 — Full-stack DinD integration testing for egg self-validation
**Revision**: 2 (addresses unified reviewer and plan reviewer feedback)

## Summary

Replace the host Docker socket mount (`/var/run/docker.sock`) in the local
pipeline integration test stack with a rootless Docker-in-Docker (DinD) sidecar
(`docker:27-dind-rootless`). This sandboxes all container operations within an
isolated Docker daemon, eliminating host Docker access from the test
orchestrator. No production code changes needed — `DockerClient` already
supports `DOCKER_HOST` override (`docker_client.py:95-109`), and
`ContainerSpawner` reads `EGG_ISOLATED_NETWORK`/`EGG_EXTERNAL_NETWORK` from
environment (`container_spawner.py:55-56`).

## Architectural Constraints (Definitive)

These are known Docker behaviors, not open questions:

1. **DinD-spawned containers cannot join outer compose networks** (AC-1).
   Networks are daemon-local. The DinD daemon has its own network namespace and
   cannot see networks created by the host daemon (e.g.,
   `egg-lp-test-<pid>-isolated`).

2. **Named volumes are daemon-local** (AC-2). Volumes created by the host
   Docker daemon (`certs`, `worktrees`, `state`) are invisible to DinD. Data
   must be shared via compose volume mounts through DinD's filesystem.

3. **EGG_HOST_REPO_MAP paths must resolve inside DinD** (AC-3). Since DinD is
   the daemon performing bind mounts into spawned containers, paths must be
   DinD-internal filesystem paths, not host temp directory paths.

## Networking Design: DinD-Internal Bridge + NAT

**Chosen approach** (Approach D from architect analysis):

- Create a user-defined bridge network (`egg-dind-internal`) inside DinD at
  test setup time.
- Override `EGG_ISOLATED_NETWORK=egg-dind-internal` and
  `EGG_EXTERNAL_NETWORK=egg-dind-internal` on the orchestrator so
  `_build_network_config()` (`container_spawner.py:137-170`) uses a network
  that exists inside DinD.
- DinD itself sits on the compose networks (172.40.0.4 on isolated, 172.41.0.4
  on external). Its internal containers route to 172.40.0.2 (gateway) through
  DinD's NAT — standard Docker behavior for privileged containers with IP
  forwarding.

**Why not alternatives:**
- `--network=host` on DinD: defeats isolation goals.
- Matching network names inside DinD: overlapping subnets cause routing
  confusion; separate daemon namespaces mean no actual connectivity.
- `--network=container:dind`: requires `network_mode` changes to the shared
  config builder (`egg_container/__init__.py`), violating the no-code-changes
  constraint. Also causes port conflicts.

**Network topology:**
```
Host Docker Daemon
  └─ compose stack (egg-lp-test-<pid>)
       ├─ gateway        (172.40.0.2 on isolated / 172.41.0.2 on external)
       ├─ orchestrator    (172.40.0.3 on isolated / 172.41.0.3 on external)
       │    └─ DOCKER_HOST=tcp://dind:2375
       └─ dind (rootless) (172.40.0.4 on isolated / 172.41.0.4 on external)
            │
            └─ DinD-internal Docker daemon
                 ├─ network: egg-dind-internal (Docker-assigned subnet)
                 └─ mock-sandbox containers
                      ├─ joined to egg-dind-internal
                      ├─ reach gateway via DinD NAT → 172.40.0.2:9848
                      └─ volume mounts resolve to DinD-internal paths
```

## Volume Design

Named volumes are mounted into DinD via compose volume mounts (same pattern as
gateway/orchestrator). DinD-spawned containers then access data via
DinD-internal filesystem paths:

| Volume | Compose mount on DinD | DinD-internal path | Notes |
|--------|----------------------|-------------------|-------|
| repos | `{repos_dir}:/home/egg/repos/{name}` | `/home/egg/repos/{name}` | Via override file |
| worktrees | `worktrees:/home/egg/.egg-worktrees` | `/home/egg/.egg-worktrees` | Named volume shared via compose |
| state | `state:/home/egg/.egg-state` | `/home/egg/.egg-state` | Accessed by orchestrator, not sandbox |
| certs | `certs:/shared/certs` | `/shared/certs` | Skipped — mock-sandbox doesn't need TLS |

**Key change**: `EGG_HOST_REPO_MAP` must map `repo_name` →
`/home/egg/repos/{repo_name}` (DinD-internal path), not the host temp
directory path.

## Files Modified

Only two files under `integration_tests/local_pipeline/`:

1. **`docker-compose.yml`** — Add DinD service, update orchestrator config
2. **`conftest.py`** — Network creation, image loading, volume path updates,
   `LocalPipelineStack` dataclass update, cleanup

No production code changes. No new test files.

## Rollback Plan

Revert the two modified files to pre-DinD state:
```bash
git checkout origin/main -- integration_tests/local_pipeline/docker-compose.yml \
                            integration_tests/local_pipeline/conftest.py
```
No production code was changed, so rollback is trivially safe.

## Test Strategy

- **Primary validation**: Run the full existing integration test suite
  (`pytest integration_tests/local_pipeline/ -v -m integration --timeout=300`).
  These tests exercise complete pipeline lifecycle including container spawning,
  environment injection, volume mounts, and gateway connectivity.
- **No new tests required**: Existing tests cover the same operations, now
  routed through DinD. Mock-sandbox `phase-runner.sh` has built-in validation
  for env vars (exit code 3), repo volumes (exit code 4), and `.git` (exit
  code 5).
- **Early validation**: TASK-1-3 is a concrete blocking gate — a container
  spawned inside DinD must reach the gateway before proceeding.
- **Graceful degradation**: The fixture should skip tests (not fail hard) if
  DinD is unavailable, preserving compatibility with environments that don't
  support DinD.

## Risks

| ID | Title | Severity | Likelihood | Mitigation |
|----|-------|----------|-----------|-----------|
| RISK-1 | DinD NAT routing doesn't reach gateway | Critical | Low | TASK-1-3 validates before Phase 2. Fallback: `--network=host` on DinD |
| RISK-2 | Volume path mismatch | High | Medium | TASK-2-3 end-to-end verification via phase-runner.sh exit codes |
| RISK-3 | DinD startup latency | Low | Low | 40s health check start_period |
| RISK-4 | Container IP resolution on DinD-internal | Medium | Low | Both `EGG_*_NETWORK` vars overridden; `_get_container_ip()` finds correct IP |

## Design Decisions

| ID | Decision | Rationale |
|----|----------|-----------|
| DD-1 | DinD-internal bridge + NAT routing | Preserves isolation, no code changes, standard Docker behavior |
| DD-2 | Bind-mount volume data into DinD | Named volumes are daemon-local; compose mounts share data |
| DD-3 | Both `EGG_*_NETWORK` = `egg-dind-internal` | Tests use `local` mode (isolated network); covers `public` too |
| DD-4 | Skip certs volume for mock-sandbox | phase-runner.sh doesn't validate TLS; volume mount may fail silently |
| DD-5 | Add `dind_docker_host` to `LocalPipelineStack` | Needed for image loading and debugging |

## Reviewer Feedback Resolution

### Unified Reviewer

1. **Network connectivity validation → Phase 1 (blocking gate)**: Done.
   TASK-1-3 is a Phase 1 gate that spawns a container inside DinD and verifies
   it can reach the gateway at 172.40.0.2:9848 before any Phase 2+ work.

2. **Named volume sharing assumption corrected**: Done. Named volumes are
   mounted into DinD via compose volume mounts (same as gateway/orchestrator).
   `EGG_HOST_REPO_MAP` uses DinD-internal paths. TASK-2-3 verifies end-to-end
   with phase-runner.sh exit code validation.

3. **`EGG_ISOLATED_NETWORK` mismatch resolved**: Done. Both
   `EGG_ISOLATED_NETWORK` and `EGG_EXTERNAL_NETWORK` are overridden to
   `egg-dind-internal` (a network created inside DinD). Resolved together with
   networking in Phase 1 (TASK-1-2 + TASK-1-3).

4. **Rollback plan**: Added explicit section above.

5. **`dind_docker_host` on `LocalPipelineStack`**: Added as DD-5 / TASK-1-3.

6. **TASK-3-1 simplification**: Acknowledged — compose down removes DinD and
   children. Task evaluates whether additional cleanup adds value.

### Plan Reviewer

1. **Network architecture in Phase 1**: Done. Approach D (DinD-internal bridge
   + NAT) is the chosen design, implemented in TASK-1-1 through TASK-1-3.

2. **`EGG_EXTERNAL_NETWORK` handling**: Done. Set to `egg-dind-internal` in
   TASK-1-2. Only `local` mode is exercised by tests, but `public` mode is
   covered too.

3. **TASK-2-3 acceptance criteria strengthened**: Done. Acceptance now includes
   concrete verification — mock-sandbox phase-runner.sh must not exit with
   code 4 (missing repo) or 5 (invalid .git).

4. **OQ-1 resolved as definitive constraint**: Done. Converted to AC-1 —
   DinD-spawned containers definitively cannot join outer compose networks.

## Implementation Phases

### Phase 1: Add DinD Service with Networking Solution

**Goal**: Add rootless DinD sidecar, wire networking, validate connectivity
before proceeding to any other work.

**TASK-1-1**: Add `dind` service to `docker-compose.yml`
- Image: `docker:27-dind-rootless` with `privileged: true`
- Health check: `docker info` with 10s interval, 40s `start_period`
- Networks: `egg-test-isolated` (172.40.0.4), `egg-test-external` (172.41.0.4)
- Volumes: `worktrees`, `state`, `certs` (same named volumes as
  gateway/orchestrator)
- Environment: `DOCKER_TLS_CERTDIR=` (disable TLS for test simplicity)
- Port: expose 2375 for host-side access during fixture setup
- **Acceptance**: `docker compose up dind` starts healthy; DinD is on both test
  networks.
- **File**: `integration_tests/local_pipeline/docker-compose.yml`

**TASK-1-2**: Update orchestrator service to use DinD
- Change `DOCKER_HOST` from `unix:///var/run/docker.sock` to `tcp://dind:2375`
- Remove `/var/run/docker.sock` volume mount
- Set `EGG_ISOLATED_NETWORK=egg-dind-internal`
- Set `EGG_EXTERNAL_NETWORK=egg-dind-internal`
- Add `depends_on: dind: condition: service_healthy`
- **Acceptance**: Orchestrator connects to DinD daemon; no host socket mount;
  both `EGG_*_NETWORK` vars point to DinD-internal network.
- **File**: `integration_tests/local_pipeline/docker-compose.yml`

**TASK-1-3**: Network connectivity validation gate in `conftest.py`
- After compose up and health checks pass, detect DinD's mapped port via
  `docker compose port dind 2375`
- Create bridge network inside DinD:
  `docker -H tcp://localhost:<port> network create --driver bridge egg-dind-internal`
- Spawn minimal container to validate connectivity:
  `docker -H tcp://localhost:<port> run --rm --network egg-dind-internal alpine:3.19 wget -qO- --timeout=5 http://172.40.0.2:9848/api/v1/health`
- If check fails: `pytest.fail()` with clear message about DinD networking
  constraint
- Add `dind_docker_host` field to `LocalPipelineStack` dataclass
- **Acceptance**: A container spawned inside DinD successfully reaches the
  gateway health endpoint at 172.40.0.2:9848 through DinD's NAT.
- **File**: `integration_tests/local_pipeline/conftest.py`

### Phase 2: Update Test Fixture for Image Loading and Volume Paths

**Goal**: Load mock-sandbox image into DinD, configure DinD-internal volume
paths correctly, verify end-to-end data access.

**TASK-2-1**: Load mock-sandbox image into DinD
- After compose up and network validation, transfer image:
  `docker save mock-sandbox:latest | docker -H tcp://localhost:<port> load`
- Verify image available:
  `docker -H tcp://localhost:<port> images mock-sandbox:latest`
- **Acceptance**: `mock-sandbox:latest` is available inside DinD daemon after
  fixture setup.
- **File**: `integration_tests/local_pipeline/conftest.py`

**TASK-2-2**: Update override file to mount repo volume into DinD
- The override file (generated in `conftest.py`) currently adds repo bind
  mounts to gateway and orchestrator
- Add the same bind mount to the `dind` service:
  `{repos_dir}:/home/egg/repos/{repo_name}`
- **Acceptance**: DinD has repo volume mounted at
  `/home/egg/repos/{repo_name}`.
- **File**: `integration_tests/local_pipeline/conftest.py`

**TASK-2-3**: Update `EGG_HOST_REPO_MAP` to DinD-internal paths
- Currently: maps `repo_name` → `repos_dir` (host temp dir path)
- Change to: map `repo_name` → `/home/egg/repos/{repo_name}` (DinD-internal
  path)
- Verify end-to-end: mock-sandbox `phase-runner.sh` validates repo volume
  mount (exits 4 if missing) and `.git` directory (exits 5 if invalid)
- **Acceptance**: Mock-sandbox phase-runner.sh does not exit with code 4 or 5.
  A spawned container inside DinD successfully reads a file from the mounted
  repo volume.
- **File**: `integration_tests/local_pipeline/conftest.py`

### Phase 3: Cleanup and Orphan Handling

**Goal**: Ensure cleanup works correctly with DinD; simplify if compose down
already suffices.

**TASK-3-1**: Evaluate and update `_cleanup_orphaned_containers()`
- With DinD, orphaned test containers live inside DinD, not on the host.
  `compose down` removes the DinD container and all its children.
- Evaluate whether `_cleanup_orphaned_containers()` (which targets the host
  daemon) still adds value for DinD-hosted containers.
- If cleanup of stale DinD instances from crashed previous runs is needed,
  update to also target the DinD endpoint.
- If compose down already handles everything, simplify or add a comment
  documenting why host-side cleanup is sufficient.
- **Acceptance**: No stale test containers remain after test runs.
- **File**: `integration_tests/local_pipeline/conftest.py`

### Phase 4: Validate Existing Tests

**Goal**: All existing integration tests pass with the DinD-backed stack.

**TASK-4-1**: Run full integration test suite
- Execute: `PYTHONPATH=shared pytest integration_tests/local_pipeline/ -v -m integration --timeout=300`
- Expected failure categories: timeout (DinD adds ~5s startup), volume path
  resolution, container IP resolution
- Fix any DinD-caused failures; document pre-existing failures
- **Acceptance**: All tests pass or pre-existing failures documented.
- **Files**: All test files under `integration_tests/local_pipeline/`

**TASK-4-2**: Adjust timeouts for DinD startup overhead if needed
- DinD health check `start_period` is 40s to accommodate rootless startup
- If tests fail due to timeouts, increase relevant timeout values
- **Acceptance**: No timeout-related test failures.
- **Files**: `integration_tests/local_pipeline/conftest.py`,
  `integration_tests/local_pipeline/docker-compose.yml`

```yaml
# yaml-tasks
pr:
  title: "Replace host Docker socket with rootless DinD sidecar"
  description: |
    Replace the host Docker socket mount in the local pipeline integration
    test stack with a rootless DinD sidecar (docker:27-dind-rootless). This
    sandboxes all container operations within an isolated Docker daemon,
    removing host Docker access from the test orchestrator. No orchestrator
    code changes needed — DockerClient already supports DOCKER_HOST override.

    Networking solved via DinD-internal bridge network + NAT routing: spawned
    containers join a bridge network inside DinD and reach the gateway through
    DinD's NAT. Both EGG_ISOLATED_NETWORK and EGG_EXTERNAL_NETWORK are
    overridden to point to the DinD-internal network.

    Fixes #647
phases:
  - id: 1
    name: Add DinD Service with Networking Solution
    goal: Add rootless DinD sidecar, wire networking, validate connectivity
    tasks:
      - id: TASK-1-1
        description: Add dind service (docker:27-dind-rootless) with health check, networks, volumes to compose
        acceptance: DinD starts healthy and is reachable on both test networks
        files:
          - integration_tests/local_pipeline/docker-compose.yml
      - id: TASK-1-2
        description: Update orchestrator DOCKER_HOST to tcp://dind:2375, remove socket mount, set EGG_*_NETWORK to egg-dind-internal
        acceptance: Orchestrator connects to DinD; no host socket mount; both network overrides set
        files:
          - integration_tests/local_pipeline/docker-compose.yml
      - id: TASK-1-3
        description: Create DinD-internal network and validate connectivity gate (container inside DinD reaches gateway at 172.40.0.2:9848)
        acceptance: Alpine container inside DinD on egg-dind-internal network successfully curls gateway health endpoint; dind_docker_host added to LocalPipelineStack
        files:
          - integration_tests/local_pipeline/conftest.py
  - id: 2
    name: Update Test Fixture for Image Loading and Volume Paths
    goal: Load mock-sandbox image into DinD, configure DinD-internal volume paths, verify end-to-end data access
    tasks:
      - id: TASK-2-1
        description: Add docker save/load step to transfer mock-sandbox image into DinD after compose up
        acceptance: mock-sandbox:latest is available inside DinD daemon after fixture setup
        files:
          - integration_tests/local_pipeline/conftest.py
      - id: TASK-2-2
        description: Update override file to mount repo volume into DinD service (same bind mount as gateway/orchestrator)
        acceptance: DinD has repo volume mounted at /home/egg/repos/{repo_name}
        files:
          - integration_tests/local_pipeline/conftest.py
      - id: TASK-2-3
        description: Update EGG_HOST_REPO_MAP to DinD-internal paths; verify end-to-end volume access
        acceptance: Mock-sandbox phase-runner.sh does not exit with code 4 (missing repo) or 5 (invalid .git); spawned container reads file from mounted repo
        files:
          - integration_tests/local_pipeline/conftest.py
  - id: 3
    name: Cleanup and Orphan Handling
    goal: Ensure cleanup targets DinD; simplify if compose down suffices
    tasks:
      - id: TASK-3-1
        description: Evaluate and update _cleanup_orphaned_containers for DinD (compose down removes DinD children — may simplify)
        acceptance: No stale test containers remain after test runs
        files:
          - integration_tests/local_pipeline/conftest.py
  - id: 4
    name: Validate Existing Tests
    goal: All existing integration tests pass with DinD-backed stack
    tasks:
      - id: TASK-4-1
        description: Run full integration test suite and fix any DinD-caused failures
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
        description: Adjust timeouts for DinD startup overhead if needed
        acceptance: No timeout-related test failures
        files:
          - integration_tests/local_pipeline/conftest.py
          - integration_tests/local_pipeline/docker-compose.yml
```

---

*Authored-by: egg*
