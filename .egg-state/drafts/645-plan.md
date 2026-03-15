# Plan: DinD Deployment Validation in Check Phase

> Issue: #645 | Phase: plan

## Summary

This plan implements orchestrator-driven Docker-in-Docker deployment validation for the egg check phase. The orchestrator (which already has Docker socket access) manages the full devserver lifecycle — extracting compose config from committed state, generating override mounts for agent-modified code, creating an air-gapped network, and tearing down after validation. The sandbox (checker) connects to the running services via a new `egg-check` network and runs HTTP health checks and smoke tests, returning a standard `CheckResult`.

The approach follows Option A from the [analysis](645-analysis.md): orchestrator manages infrastructure, sandbox validates. This preserves the existing trust model (orchestrator=trusted, sandbox=untrusted) and prevents the sandbox from gaining Docker socket access. Implementation is phased — starting with the devserver lifecycle manager, then check integration, then security hardening.

**Hard dependency**: Issue #644 (commit-level phase file restrictions) must be implemented first so compose files at `HEAD` are trustworthy. This plan assumes #644 is complete before Phase 2 begins.

## Implementation Phases

### Phase 1: Target Application Configuration Schema

**Goal**: Define the configuration format that target applications use to opt into deployment validation. This is the contract between target repos and egg's deployment checker.

**Tasks**:

- [TASK-1-1] Define `DeploymentConfig` Pydantic model — Create a new model in `shared/egg_contracts/models.py` representing the deployment validation configuration. Fields: `compose_file` (path to docker-compose file, default `docker-compose.yml`), `services` (list of `ServiceMapping` objects mapping source directories to service names), `health_endpoints` (dict mapping service name to health check path, e.g. `{"api": "/_api/ping"}`), `startup_timeout_seconds` (default 120), `validation_tests` (optional list of `ValidationTest` objects with method/path/expected_status), `image_registry` (optional registry prefix for pre-built images).
  - **File**: `shared/egg_contracts/models.py`
  - **Acceptance**: `DeploymentConfig` model validates correctly; `ServiceMapping` has `source_dir` and `service_name` fields; model is importable from `egg_contracts`.

- [TASK-1-2] Define `ServiceMapping` and `ValidationTest` sub-models — `ServiceMapping` maps a source directory (e.g. `services/api/`) to a docker-compose service name (e.g. `api`). `ValidationTest` defines an HTTP test: `service`, `method` (GET/POST), `path`, `expected_status` (default 200), `expected_body_contains` (optional).
  - **File**: `shared/egg_contracts/models.py`
  - **Acceptance**: Both models validate with Pydantic; `ServiceMapping` rejects paths with `../`; `ValidationTest` defaults method to GET.

- [TASK-1-3] Add deployment config loading to contract utilities — Add a function `load_deployment_config(repo_root: Path) -> DeploymentConfig | None` that reads `.egg/deployment.yml` (or `.egg/deployment.json`) from the repo root. Returns `None` if file doesn't exist (target app hasn't opted in). Validates against the Pydantic model.
  - **File**: `shared/egg_contracts/loader.py` (or new `shared/egg_contracts/deployment.py`)
  - **Acceptance**: Function returns `DeploymentConfig` when file exists and is valid; returns `None` when file missing; raises `ValidationError` when file is malformed.

**Dependencies**: None — this is foundational and can proceed in parallel with #644.

**Exit criteria**: Configuration schema is defined, loadable, and documented with inline docstrings.

### Phase 2: Orchestrator Devserver Lifecycle Manager

**Goal**: Build the orchestrator module that manages the full devserver lifecycle: extract compose from committed state, generate override mounts, create the air-gapped network, start/stop the stack.

**Tasks**:

- [TASK-2-1] Create `DevserverManager` class in orchestrator — New module `orchestrator/devserver.py` with a `DevserverManager` class. Constructor takes `pipeline_id`, `repo_path`, `worktree_path`, and `docker_client` (reuse existing `DockerClient`). Holds state for the current devserver stack (network ID, container IDs, temp directory for compose files).
  - **File**: `orchestrator/devserver.py` (new file)
  - **Acceptance**: Class instantiates with required parameters; has cleanup logic in `__del__` or explicit `teardown()`.

- [TASK-2-2] Implement compose extraction from committed state — Method `_extract_compose_config(compose_path: str) -> str` that runs `git show HEAD:<compose_path>` against the worktree to retrieve the compose file content from the last commit (not working tree). Writes the extracted content to a temp directory. Validates that the extracted file is valid YAML.
  - **File**: `orchestrator/devserver.py`
  - **Acceptance**: Extracts compose content from `HEAD`; raises error if path doesn't exist in commit; writes to temp dir outside worktree; validates YAML syntax.

- [TASK-2-3] Implement service-to-file mapping resolution — Method `_resolve_affected_services(changed_files: list[str], service_mappings: list[ServiceMapping]) -> list[ServiceMapping]` that determines which devserver services are affected by the agent's changes. Uses the `ServiceMapping` from the deployment config. Returns the subset of services that need agent code mounted.
  - **File**: `orchestrator/devserver.py`
  - **Acceptance**: Given changed files `["services/api/views.py", "services/api/models.py"]` and a mapping `ServiceMapping(source_dir="services/api/", service_name="api")`, returns `[ServiceMapping(source_dir="services/api/", service_name="api")]`. Files outside any mapping are ignored.

- [TASK-2-4] Implement compose override generation — Method `_generate_compose_override(affected_services: list[ServiceMapping], worktree_path: Path) -> str` that generates a docker-compose override YAML. For each affected service, adds a read-only volume mount: `{worktree_path}/{source_dir}:{container_mount_path}:ro`. Also adds resource limits (CPU, memory, PID) and security options (no capabilities, seccomp default) to every service. Adds the `egg-check` network to all services.
  - **File**: `orchestrator/devserver.py`
  - **Acceptance**: Generated override is valid docker-compose YAML; volume mounts use `:ro`; resource limits present on all services; `egg-check` network attached to all services.

- [TASK-2-5] Implement `egg-check` network creation and teardown — Methods `_create_check_network() -> str` and `_remove_check_network(network_id: str)`. Creates a Docker bridge network with `internal=True` (no default gateway, no DNS, no internet route). Uses Docker SDK directly via the existing `DockerClient` or raw `docker.APIClient`. Network name: `egg-check-{pipeline_id}` to avoid collisions.
  - **File**: `orchestrator/devserver.py`
  - **Acceptance**: Network is created with `internal=True`; containers on this network cannot reach the internet; network name includes pipeline ID; teardown removes the network even if containers are still attached (force).

- [TASK-2-6] Implement stack lifecycle: `start()` and `teardown()` — `start(deployment_config: DeploymentConfig) -> DevserverStatus` orchestrates the full startup: (1) extract compose, (2) resolve affected services, (3) generate override, (4) create network, (5) run `docker compose -f base.yml -f override.yml up -d`, (6) wait for health checks. `teardown()` runs `docker compose down`, removes the network, and cleans up the temp directory. Both methods are idempotent.
  - **File**: `orchestrator/devserver.py`
  - **Acceptance**: `start()` brings up devserver with agent code mounted RO; health check polling respects `startup_timeout_seconds`; `teardown()` removes all containers, network, and temp files; calling `teardown()` twice doesn't error; hard time cap enforced via timeout.

- [TASK-2-7] Implement sandbox network attachment — Method `attach_checker(sandbox_container_id: str, service_names: list[str])` that attaches the sandbox container to the `egg-check` network. The sandbox should only be able to reach the service(s) under test, not database emulators or caches directly (Phase 4 hardens this further; for now, attach to the shared `egg-check` network).
  - **File**: `orchestrator/devserver.py`
  - **Acceptance**: Sandbox container gets an IP on the `egg-check` network; can reach devserver services by container name; attachment is recorded for teardown cleanup.

- [TASK-2-8] Add `DevserverStatus` dataclass — Return type for lifecycle operations. Fields: `status` (enum: STARTING, HEALTHY, UNHEALTHY, STOPPED, ERROR), `services` (dict of service name → `ServiceStatus` with `healthy: bool`, `ip: str`, `port: int`), `network_id`, `error_message`.
  - **File**: `orchestrator/devserver.py`
  - **Acceptance**: Status accurately reflects devserver state; service IPs are resolvable from the `egg-check` network.

**Dependencies**: Phase 1 (for `DeploymentConfig` model). Hard dependency on #644 for trusted compose extraction.

**Exit criteria**: `DevserverManager` can start a devserver stack from committed compose config, mount agent code read-only, create an air-gapped network, and tear everything down cleanly.

### Phase 3: Orchestrator API Endpoints

**Goal**: Expose devserver lifecycle management via REST endpoints so the sandbox check runner can coordinate with the orchestrator.

**Tasks**:

- [TASK-3-1] Add `POST /api/v1/pipelines/<id>/deployment-check/start` endpoint — Triggers the orchestrator to start the devserver for the given pipeline. Loads `DeploymentConfig` from the target repo, determines changed files from the pipeline's worktree, calls `DevserverManager.start()`. Returns `DevserverStatus` as JSON with service endpoints the checker can hit.
  - **File**: `orchestrator/routes/checks.py` (new file, new blueprint)
  - **Acceptance**: Endpoint returns 200 with service endpoints on success; returns 404 if pipeline not found; returns 422 if no deployment config; returns 409 if devserver already running.

- [TASK-3-2] Add `GET /api/v1/pipelines/<id>/deployment-check/status` endpoint — Returns current `DevserverStatus` for the pipeline's devserver. The checker polls this to know when services are healthy.
  - **File**: `orchestrator/routes/checks.py`
  - **Acceptance**: Returns current status; returns 404 if no devserver started for this pipeline.

- [TASK-3-3] Add `POST /api/v1/pipelines/<id>/deployment-check/teardown` endpoint — Triggers `DevserverManager.teardown()`. Called by the checker when validation is complete, or by the orchestrator on timeout.
  - **File**: `orchestrator/routes/checks.py`
  - **Acceptance**: Teardown completes successfully; returns 200; idempotent (calling twice returns 200).

- [TASK-3-4] Register the new blueprint in `api.py` — Add the checks blueprint to the Flask app alongside existing blueprints.
  - **File**: `orchestrator/api.py`
  - **Acceptance**: Blueprint registered; endpoints accessible; health check still works.

- [TASK-3-5] Add `DevserverManager` lifecycle tracking to orchestrator state — Store active `DevserverManager` instances keyed by pipeline_id. Ensure teardown is called on pipeline completion/failure (integrate with phase transition logic in `routes/phases.py`).
  - **File**: `orchestrator/routes/checks.py`, `orchestrator/routes/phases.py`
  - **Acceptance**: Devserver is automatically torn down when pipeline phase completes or fails; no orphaned devserver stacks after pipeline lifecycle ends.

**Dependencies**: Phase 2 (DevserverManager).

**Exit criteria**: Sandbox can trigger devserver start/status/teardown via HTTP API; orchestrator manages lifecycle with automatic cleanup.

### Phase 4: Check Runner Integration

**Goal**: Implement the `DeploymentCheck` check runner that coordinates with the orchestrator-managed devserver to validate agent changes against running services.

**Tasks**:

- [TASK-4-1] Create `DeploymentCheck` check runner — New file `.github/scripts/checks/deployment_check.py` implementing `CheckRunner`. `check_id` is `"check-deployment"`. The `run()` method: (1) calls orchestrator API to start devserver, (2) polls status until healthy or timeout, (3) runs health checks against each service endpoint, (4) runs validation tests from `DeploymentConfig`, (5) signals teardown, (6) returns `CheckResult`.
  - **File**: `.github/scripts/checks/deployment_check.py` (new file)
  - **Acceptance**: Check returns PASS when all health checks and validation tests pass; returns FAIL with details on which service/test failed; returns SKIP when no `DeploymentConfig` exists (target app not opted in); handles timeout gracefully.

- [TASK-4-2] Implement defensive HTTP response parsing — The checker consumes HTTP responses from agent-modified services, which are attacker-controlled. Implement: max response size limit (1MB), JSON parse with exception handling, timeout on individual requests (10s), no redirect following to external hosts, content-type validation.
  - **File**: `.github/scripts/checks/deployment_check.py`
  - **Acceptance**: Oversized responses are truncated and reported as warnings; malformed JSON doesn't crash the checker; request timeouts produce clear error messages; redirects to non-`egg-check` network hosts are blocked.

- [TASK-4-3] Add `"deployment"` to `CHECK_REGISTRY` — Register the new check in `run_check.py` so it can be loaded dynamically.
  - **File**: `.github/scripts/checks/run_check.py`
  - **Acceptance**: `load_check_class("deployment")` returns `DeploymentCheck`; `run_check.py deployment <contract>` executes the check.

- [TASK-4-4] Add `check-deployment` to implement phase defaults — Add `CheckDefinition` for `check-deployment` to `_IMPLEMENT_CHECKS` in `phase_defaults.py`. Start as `required=False` (optional) so existing pipelines aren't broken. Set `retry_on_fail=True, max_retries=1` to handle transient startup failures.
  - **File**: `shared/egg_contracts/phase_defaults.py`
  - **Acceptance**: `check-deployment` appears in implement phase defaults as optional; existing required checks are unaffected; phase config merging still works correctly.

**Dependencies**: Phase 3 (API endpoints).

**Exit criteria**: `DeploymentCheck` can be invoked via `run_check.py deployment <contract>`, coordinates with orchestrator, and returns correct `CheckResult` for pass/fail/skip scenarios.

### Phase 5: Network Constants and Configuration

**Goal**: Add the `egg-check` network configuration to shared constants, ensuring consistency across orchestrator and any future compose definitions.

**Tasks**:

- [TASK-5-1] Add `egg-check` network constants — Add `EGG_CHECK_NETWORK_PREFIX`, `EGG_CHECK_SUBNET` (e.g. `172.34.0.0/24`), and related constants to `shared/egg_config/constants.py`. The actual network name is `{prefix}-{pipeline_id}` to allow multiple concurrent pipelines.
  - **File**: `shared/egg_config/constants.py`
  - **Acceptance**: Constants defined; subnet doesn't overlap with `egg-isolated` (172.32.0.0/24) or `egg-external` (172.33.0.0/24); importable from `egg_config`.

- [TASK-5-2] Add resource limit constants — Define default resource limits for devserver containers: `DEVSERVER_CPU_LIMIT` (e.g. `"1.0"`), `DEVSERVER_MEMORY_LIMIT` (e.g. `"512m"`), `DEVSERVER_PIDS_LIMIT` (e.g. `256`), `DEVSERVER_HARD_TIMEOUT_SECONDS` (e.g. `300`).
  - **File**: `shared/egg_config/constants.py`
  - **Acceptance**: Constants defined with sensible defaults; documented with inline comments explaining the rationale.

**Dependencies**: None — can proceed in parallel with other phases.

**Exit criteria**: All network and resource constants are defined and consistent.

### Phase 6: Security Hardening

**Goal**: Harden the deployment validation beyond the baseline air-gapped network. Add inter-container isolation, seccomp profiles, and validate credential-free operation.

**Tasks**:

- [TASK-6-1] Implement per-service scoped networks — Modify `DevserverManager._generate_compose_override()` to create per-service-boundary networks instead of a single shared `egg-check` network. The checker should only reach the service(s) under test. Database emulators and caches are on separate internal networks accessible only to the services that need them. This limits lateral movement if agent code in one service is malicious.
  - **File**: `orchestrator/devserver.py`
  - **Acceptance**: Checker cannot reach database emulators directly; each service boundary has its own bridge; `docker network inspect` confirms isolation.

- [TASK-6-2] Add seccomp profile for devserver containers — Apply the default Docker seccomp profile explicitly to all devserver containers via the compose override. This blocks syscalls that could be used for container escape (e.g. `unshare`, `mount`, `ptrace`).
  - **File**: `orchestrator/devserver.py`
  - **Acceptance**: All devserver containers run with seccomp profile; `docker inspect` confirms `SecurityOpt` includes seccomp.

- [TASK-6-3] Validate credential-free operation — Add a pre-flight check to `DevserverManager.start()` that inspects the compose config for any environment variables or secrets that look like cloud credentials (AWS_*, GCP_*, AZURE_*, *_SECRET_KEY, *_API_KEY). Warn (don't block) if found, as they should be replaced by emulator defaults.
  - **File**: `orchestrator/devserver.py`
  - **Acceptance**: Pre-flight check runs before stack start; logs warnings for suspicious env vars; doesn't block startup (emulator defaults may use these names).

- [TASK-6-4] Implement image pre-pull mechanism — Add method `DevserverManager.pre_pull_images(deployment_config: DeploymentConfig)` that pulls all container images referenced in the compose file before starting the stack. This can be called at pipeline start to reduce startup latency during checks. Uses `DockerClient` to pull images.
  - **File**: `orchestrator/devserver.py`
  - **Acceptance**: All images referenced in compose are pulled; pull errors are logged but don't fail the pre-pull (images may already exist locally); method is idempotent.

**Dependencies**: Phase 2 (DevserverManager exists).

**Exit criteria**: Inter-container isolation limits lateral movement; seccomp profiles applied; credential-free operation validated; images pre-pulled.

### Phase 7: Testing

**Goal**: Comprehensive test coverage for all new components.

**Tasks**:

- [TASK-7-1] Unit tests for `DeploymentConfig` model — Test validation, defaults, edge cases (missing fields, malformed YAML, path traversal in `ServiceMapping`).
  - **File**: `shared/tests/test_deployment_config.py` (new file)
  - **Acceptance**: All model validation rules tested; path traversal rejected; optional fields have correct defaults.

- [TASK-7-2] Unit tests for `DevserverManager` — Mock Docker SDK calls. Test compose extraction, override generation, network creation, service mapping resolution, teardown idempotency, timeout handling.
  - **File**: `orchestrator/tests/test_devserver.py` (new file)
  - **Acceptance**: All public methods tested; Docker SDK interactions verified via mocks; error paths covered (compose not found, network creation failure, health check timeout).

- [TASK-7-3] Unit tests for `DeploymentCheck` runner — Mock orchestrator HTTP API. Test PASS/FAIL/SKIP scenarios, defensive parsing (oversized response, malformed JSON, timeout), and orchestrator communication errors.
  - **File**: `.github/scripts/checks/tests/test_deployment_check.py` (new file, or alongside existing check tests)
  - **Acceptance**: All three result states tested; defensive parsing verified; orchestrator API errors produce FAIL with clear messages.

- [TASK-7-4] Unit tests for orchestrator API endpoints — Test start/status/teardown endpoints with mocked `DevserverManager`. Test error responses (pipeline not found, no deployment config, already running).
  - **File**: `orchestrator/tests/test_routes_checks.py` (new file)
  - **Acceptance**: All endpoints return correct status codes and response bodies; error cases covered.

- [TASK-7-5] Integration test for end-to-end deployment validation — Create a minimal test docker-compose stack (e.g., a simple HTTP echo server) with a `.egg/deployment.yml` config. Run the full flow: orchestrator starts stack, checker validates, orchestrator tears down. This tests the real Docker compose interaction.
  - **File**: `integration_tests/deployment_validation/test_deployment_check_e2e.py` (new file)
  - **Acceptance**: Full lifecycle works end-to-end with real Docker containers; health checks pass; validation tests run; teardown is clean (no orphaned containers or networks).

- [TASK-7-6] Test compose extraction from committed state — Verify that `_extract_compose_config` reads from `HEAD` (committed state), not the working tree. Modify compose in working tree, confirm extracted version matches committed version.
  - **File**: `orchestrator/tests/test_devserver.py`
  - **Acceptance**: Working tree modifications to compose file are not reflected in extracted config; only committed changes are used.

**Dependencies**: All prior phases.

**Exit criteria**: All unit tests pass (`pytest`). Integration test passes with real Docker. No orphaned resources after test runs.

## Test Strategy

- **Unit tests**: Mock-based tests for all new classes (`DeploymentConfig`, `DevserverManager`, `DeploymentCheck`, API endpoints). Cover happy paths, error paths, and edge cases.
- **Integration tests**: End-to-end test with a minimal Docker compose stack. Validates real Docker interactions, network isolation, and volume mounting.
- **Security tests**: Verify `egg-check` network is `internal: true` (no internet access from devserver containers). Verify agent code is mounted read-only. Verify devserver containers have resource limits.
- **Regression tests**: Existing check phase tests must continue to pass — the new check is optional and shouldn't affect existing checks.
- **Test commands**:
  - Unit: `PYTHONPATH=shared:orchestrator:.github/scripts pytest orchestrator/tests/test_devserver.py shared/tests/test_deployment_config.py -v`
  - Integration: `pytest integration_tests/deployment_validation/ -v` (requires Docker)

## Rollback Plan

1. **Feature toggle**: `check-deployment` starts as `required=False`. If issues arise, it can be removed from phase defaults without affecting any existing pipeline.
2. **No schema migrations**: No database changes. All state is ephemeral (devserver containers + temp files).
3. **Clean revert**: All changes are additive (new files + new constants + new registry entry). Reverting the PR removes the feature entirely.
4. **Network cleanup**: If the orchestrator crashes mid-lifecycle, orphaned `egg-check-*` networks can be cleaned up with `docker network prune` or a periodic cleanup job in `DockerClient.cleanup_orphaned_containers()` (extended to also clean networks).
5. **Git revert**: `git revert <merge-commit>` removes all changes cleanly since no existing files have behavioral modifications (only additions to `phase_defaults.py`, `constants.py`, `run_check.py`, and `api.py`).

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| #644 not implemented yet — compose files not trustworthy at HEAD | High (not started) | High | Block Phase 2 until #644 is merged. Phase 1 and 5 can proceed independently. |
| Devserver startup exceeds timeout (60-90s baseline) | Medium | Medium | Configurable `startup_timeout_seconds` in `DeploymentConfig`; image pre-pull reduces cold-start; start devserver in parallel with lint/test checks. |
| Target application lacks local emulators for cloud services | Medium | Medium | Pre-flight credential check warns about cloud env vars; document emulator requirements in deployment config schema. |
| Docker compose version incompatibilities across hosts | Low | Medium | Pin to compose v2 (`docker compose` CLI); validate compose file version in extraction step. |
| Orphaned devserver containers/networks after orchestrator crash | Low | Low | Add cleanup to `DockerClient.cleanup_orphaned_containers()`; extend to networks with `egg-check-*` prefix older than threshold. |
| Agent code exploits container runtime vulnerability | Very Low | High | Same risk as `make test` in sandbox; mitigated by unprivileged containers, seccomp profile, no capabilities, resource limits. |
| `egg-check` network subnet conflicts with existing infrastructure | Very Low | Medium | Use a dedicated subnet (172.34.0.0/24) that doesn't overlap with egg-isolated or egg-external. |

## Migration Notes

- **No breaking changes**: The deployment check is optional (`required=False`) and only activates for target repos with `.egg/deployment.yml`.
- **New target repo requirement**: Applications that want deployment validation must create `.egg/deployment.yml` with service mappings, health endpoints, and pre-built images.
- **Network mode consideration**: In private mode, the orchestrator needs access to a container registry to pull pre-built images. The orchestrator is on `egg-external` and can reach the registry directly. This is existing behavior for pulling sandbox images.
- **Docker compose dependency**: The orchestrator host must have `docker compose` v2 CLI available. This should already be the case since the orchestrator runs via docker-compose itself.

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.

```yaml
# yaml-tasks
pr:
  title: "Add DinD deployment validation to check phase"
  description: |
    Enables the egg check phase to spin up Docker containers (a target
    application's devserver stack) and validate agent-authored changes
    against locally running services. The orchestrator manages the full
    devserver lifecycle while the sandbox runs HTTP validation checks.

    Closes #645
phases:
  - id: 1
    name: Target Application Configuration Schema
    goal: Define the configuration format for target apps to opt into deployment validation
    tasks:
      - id: TASK-1-1
        description: Define DeploymentConfig Pydantic model with compose_file, services, health_endpoints, startup_timeout, validation_tests, image_registry fields
        acceptance: Model validates correctly and is importable from egg_contracts
        files:
          - shared/egg_contracts/models.py
      - id: TASK-1-2
        description: Define ServiceMapping and ValidationTest sub-models
        acceptance: Both models validate with Pydantic; ServiceMapping rejects path traversal
        files:
          - shared/egg_contracts/models.py
      - id: TASK-1-3
        description: Add deployment config loading function for .egg/deployment.yml
        acceptance: Returns DeploymentConfig when valid, None when missing, raises on malformed
        files:
          - shared/egg_contracts/deployment.py
  - id: 2
    name: Orchestrator Devserver Lifecycle Manager
    goal: Build the orchestrator module managing full devserver lifecycle
    tasks:
      - id: TASK-2-1
        description: Create DevserverManager class with constructor and cleanup
        acceptance: Class instantiates with required params; has explicit teardown method
        files:
          - orchestrator/devserver.py
      - id: TASK-2-2
        description: Implement compose extraction from committed state via git show HEAD
        acceptance: Extracts from HEAD not working tree; validates YAML; writes to temp dir
        files:
          - orchestrator/devserver.py
      - id: TASK-2-3
        description: Implement service-to-file mapping resolution
        acceptance: Correctly maps changed files to affected services using ServiceMapping
        files:
          - orchestrator/devserver.py
      - id: TASK-2-4
        description: Implement compose override generation with RO mounts and resource limits
        acceptance: Valid compose YAML; RO volume mounts; CPU/memory/PID limits; egg-check network
        files:
          - orchestrator/devserver.py
      - id: TASK-2-5
        description: Implement egg-check network creation and teardown
        acceptance: Network is internal=true; name includes pipeline_id; teardown is forced
        files:
          - orchestrator/devserver.py
      - id: TASK-2-6
        description: Implement start() and teardown() stack lifecycle methods
        acceptance: start() brings up stack with health polling; teardown() is idempotent and cleans all resources
        files:
          - orchestrator/devserver.py
      - id: TASK-2-7
        description: Implement sandbox network attachment to egg-check network
        acceptance: Sandbox gets IP on egg-check; can reach devserver services by container name
        files:
          - orchestrator/devserver.py
      - id: TASK-2-8
        description: Add DevserverStatus dataclass as return type for lifecycle operations
        acceptance: Status reflects devserver state with per-service health and network info
        files:
          - orchestrator/devserver.py
  - id: 3
    name: Orchestrator API Endpoints
    goal: Expose devserver lifecycle via REST API for sandbox-orchestrator coordination
    tasks:
      - id: TASK-3-1
        description: Add POST /api/v1/pipelines/<id>/deployment-check/start endpoint
        acceptance: Returns 200 with service endpoints; 404 for missing pipeline; 422 for no config; 409 if running
        files:
          - orchestrator/routes/checks.py
      - id: TASK-3-2
        description: Add GET /api/v1/pipelines/<id>/deployment-check/status endpoint
        acceptance: Returns current DevserverStatus; 404 if no devserver started
        files:
          - orchestrator/routes/checks.py
      - id: TASK-3-3
        description: Add POST /api/v1/pipelines/<id>/deployment-check/teardown endpoint
        acceptance: Teardown completes; idempotent; returns 200
        files:
          - orchestrator/routes/checks.py
      - id: TASK-3-4
        description: Register checks blueprint in api.py
        acceptance: Blueprint registered; endpoints accessible; existing routes unaffected
        files:
          - orchestrator/api.py
      - id: TASK-3-5
        description: Add DevserverManager lifecycle tracking with auto-teardown on phase complete/fail
        acceptance: Devserver torn down on pipeline completion or failure; no orphaned stacks
        files:
          - orchestrator/routes/checks.py
          - orchestrator/routes/phases.py
  - id: 4
    name: Check Runner Integration
    goal: Implement DeploymentCheck that validates agent changes against running services
    tasks:
      - id: TASK-4-1
        description: Create DeploymentCheck check runner with orchestrator coordination
        acceptance: Returns PASS/FAIL/SKIP correctly; handles timeout; coordinates via orchestrator API
        files:
          - .github/scripts/checks/deployment_check.py
      - id: TASK-4-2
        description: Implement defensive HTTP response parsing (size limits, timeouts, no external redirects)
        acceptance: Oversized responses truncated; malformed JSON handled; timeouts produce clear errors
        files:
          - .github/scripts/checks/deployment_check.py
      - id: TASK-4-3
        description: Add deployment to CHECK_REGISTRY in run_check.py
        acceptance: load_check_class("deployment") returns DeploymentCheck
        files:
          - .github/scripts/checks/run_check.py
      - id: TASK-4-4
        description: Add check-deployment to implement phase defaults as optional check
        acceptance: Check appears in implement defaults; required=False; retry_on_fail=True; max_retries=1
        files:
          - shared/egg_contracts/phase_defaults.py
  - id: 5
    name: Network Constants and Configuration
    goal: Define egg-check network and resource limit constants
    tasks:
      - id: TASK-5-1
        description: Add egg-check network constants (prefix, subnet 172.34.0.0/24)
        acceptance: No subnet overlap with egg-isolated/egg-external; importable from egg_config
        files:
          - shared/egg_config/constants.py
      - id: TASK-5-2
        description: Add devserver resource limit constants (CPU, memory, PIDs, timeout)
        acceptance: Constants defined with documented rationale
        files:
          - shared/egg_config/constants.py
  - id: 6
    name: Security Hardening
    goal: Add inter-container isolation, seccomp profiles, credential-free validation, image pre-pull
    tasks:
      - id: TASK-6-1
        description: Implement per-service scoped networks to limit lateral movement
        acceptance: Checker cannot reach DB emulators directly; each service boundary isolated
        files:
          - orchestrator/devserver.py
      - id: TASK-6-2
        description: Add seccomp profile for devserver containers
        acceptance: All devserver containers run with default seccomp; confirmed via docker inspect
        files:
          - orchestrator/devserver.py
      - id: TASK-6-3
        description: Add pre-flight credential check for suspicious env vars in compose
        acceptance: Warns on cloud credential env vars; doesn't block startup
        files:
          - orchestrator/devserver.py
      - id: TASK-6-4
        description: Implement image pre-pull mechanism for reduced startup latency
        acceptance: All compose images pulled before start; errors logged but don't fail; idempotent
        files:
          - orchestrator/devserver.py
  - id: 7
    name: Testing
    goal: Comprehensive unit and integration test coverage
    tasks:
      - id: TASK-7-1
        description: Unit tests for DeploymentConfig model validation
        acceptance: All validation rules tested; path traversal rejected; optional field defaults correct
        files:
          - shared/tests/test_deployment_config.py
      - id: TASK-7-2
        description: Unit tests for DevserverManager with mocked Docker SDK
        acceptance: All public methods tested; error paths covered; teardown idempotency verified
        files:
          - orchestrator/tests/test_devserver.py
      - id: TASK-7-3
        description: Unit tests for DeploymentCheck runner with mocked orchestrator API
        acceptance: PASS/FAIL/SKIP tested; defensive parsing verified; API errors produce FAIL
        files:
          - .github/scripts/checks/tests/test_deployment_check.py
      - id: TASK-7-4
        description: Unit tests for orchestrator API endpoints with mocked DevserverManager
        acceptance: All endpoints return correct status codes; error cases covered
        files:
          - orchestrator/tests/test_routes_checks.py
      - id: TASK-7-5
        description: Integration test for end-to-end deployment validation with real Docker
        acceptance: Full lifecycle works; health checks pass; teardown is clean
        files:
          - integration_tests/deployment_validation/test_deployment_check_e2e.py
      - id: TASK-7-6
        description: Test compose extraction reads from HEAD not working tree
        acceptance: Working tree modifications not reflected in extracted config
        files:
          - orchestrator/tests/test_devserver.py
```

---

*Authored-by: egg*
