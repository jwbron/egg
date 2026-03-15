# Plan: Implement remaining orchestrator integration items (AC-24, AC-27, AC-28, AC-29, AC-33)

> Issue: #544 | Phase: plan

## Summary

This plan implements five acceptance criteria from the orchestrator contract (#524) that form the integration layer for remote/distributed deployment modes. Based on the prior analysis and human decisions, we will: (1) create a shared `egg_orchestrator` package with signal and pipeline state types, (2) build a typed sandbox-to-orchestrator client using `urllib`, (3) modify the sandbox entrypoint to use a wrapper process approach for completion signaling, (4) enhance the gateway health endpoint with optional orchestrator connectivity reporting, and (5) document the three deployment modes in architecture documentation.

The implementation order follows dependencies: AC-29 (shared types) → AC-27 (client) → AC-28 (entrypoint) → AC-24 (health) → AC-33 (docs).

## Implementation Phases

### Phase 1: Shared Orchestrator Package (AC-29)

**Goal**: Create `shared/egg_orchestrator/` with types needed by both gateway and sandbox.

**Tasks**:
- [TASK-1-1] Create `shared/egg_orchestrator/` package structure — Acceptance: Package directory exists with `__init__.py`, `types.py`, and `py.typed`
- [TASK-1-2] Extract shared enums (SignalType, AgentRole, PipelinePhase, PipelineStatus, ContainerStatus) — Acceptance: All enums are defined with docstrings, use `StrEnum` for serialization
- [TASK-1-3] Create signal-related dataclasses (SignalPayload, SignalResponse) — Acceptance: Pydantic models match `orchestrator/routes/signals.py` request/response format
- [TASK-1-4] Update `shared/pyproject.toml` to include `egg_orchestrator*` — Acceptance: Package is discoverable by setuptools
- [TASK-1-5] Add unit tests for shared types — Acceptance: Tests verify enum values, Pydantic serialization/deserialization

**Dependencies**: None

**Exit criteria**: `shared/egg_orchestrator/` package is importable and tests pass

### Phase 2: Orchestrator Client (AC-27)

**Goal**: Create typed Python client for sandbox-to-orchestrator communication.

**Tasks**:
- [TASK-2-1] Create `sandbox/egg_lib/orchestrator_client.py` with `OrchestratorClient` class — Acceptance: Class follows patterns from `orchestrator/gateway_client.py`
- [TASK-2-2] Implement `signal_complete()` method — Acceptance: Sends POST to `/api/v1/pipelines/{id}/signal` with `signal_type=complete`
- [TASK-2-3] Implement `signal_error()` method — Acceptance: Sends error signal with message and recoverable flag
- [TASK-2-4] Implement `signal_progress()` method — Acceptance: Sends progress update with percentage and current task
- [TASK-2-5] Implement `signal_heartbeat()` method — Acceptance: Sends heartbeat with container ID
- [TASK-2-6] Add `OrchestratorError` exception class — Acceptance: Exception includes HTTP status code and error message
- [TASK-2-7] Add singleton accessor `get_orchestrator_client()` — Acceptance: Returns cached client instance, configurable via environment variables
- [TASK-2-8] Add unit tests for orchestrator client — Acceptance: Tests cover all signal methods with mocked HTTP responses

**Dependencies**: Phase 1 (imports `SignalType`, `AgentRole` from `egg_orchestrator`)

**Exit criteria**: `OrchestratorClient` can send all signal types; unit tests pass

### Phase 3: Sandbox Entrypoint Orchestrator Mode (AC-28)

**Goal**: Detect orchestrator mode and signal completion on exit using wrapper process approach.

**Tasks**:
- [TASK-3-1] Add orchestrator environment variable detection to entrypoint — Acceptance: Reads `EGG_ORCHESTRATOR_URL`, `EGG_PIPELINE_ID`, `EGG_AGENT_ROLE` from environment
- [TASK-3-2] Refactor `run_interactive()` to use `subprocess.run()` instead of `os.execvpe()` — Acceptance: Interactive mode launches subprocess and captures exit code
- [TASK-3-3] Refactor `run_exec()` to use `subprocess.run()` instead of `os.execvpe()` — Acceptance: Exec mode launches subprocess and captures exit code
- [TASK-3-4] Add `signal_completion()` function — Acceptance: Calls orchestrator client's `signal_complete()` or `signal_error()` based on exit code
- [TASK-3-5] Integrate completion signaling after subprocess exits — Acceptance: Completion signal sent before entrypoint exits when orchestrator mode detected
- [TASK-3-6] Update signal handlers to signal completion before cleanup — Acceptance: SIGTERM/SIGINT handlers signal completion if in orchestrator mode
- [TASK-3-7] Add integration test for orchestrator mode detection — Acceptance: Test verifies signal is sent on normal exit and error exit

**Dependencies**: Phase 2 (uses `OrchestratorClient`)

**Exit criteria**: Sandbox signals completion to orchestrator on exit when `EGG_ORCHESTRATOR_URL` is set

### Phase 4: Gateway Health Enhancement (AC-24)

**Goal**: Add optional orchestrator connectivity check to gateway health endpoint.

**Tasks**:
- [TASK-4-1] Add `ORCHESTRATOR_URL` environment variable support to gateway — Acceptance: Gateway reads `ORCHESTRATOR_URL` from environment
- [TASK-4-2] Create `check_orchestrator_health()` helper function — Acceptance: Uses `urllib` to GET orchestrator's `/api/v1/health` with 2-second timeout
- [TASK-4-3] Update `/api/v1/health` endpoint to include `orchestrator` field — Acceptance: Response includes `orchestrator: {reachable: bool, url: string}` when `ORCHESTRATOR_URL` is set
- [TASK-4-4] Ensure health check remains fast (<3 seconds) — Acceptance: Short timeout prevents slow health responses
- [TASK-4-5] Add unit test for health endpoint with orchestrator — Acceptance: Tests cover orchestrator reachable, unreachable, and not configured cases

**Dependencies**: None (can run in parallel with Phases 2-3, but scheduled after for logical ordering)

**Exit criteria**: Gateway health endpoint reports orchestrator connectivity when configured

### Phase 5: Architecture Documentation (AC-33)

**Goal**: Document orchestrator deployment modes and component interactions.

**Tasks**:
- [TASK-5-1] Create `docs/architecture/orchestrator.md` — Acceptance: Document exists with proper markdown structure
- [TASK-5-2] Document local deployment mode — Acceptance: Describes CLI-only mode without orchestrator, includes when to use
- [TASK-5-3] Document remote-single deployment mode — Acceptance: Describes Docker Compose mode with orchestrator on same host
- [TASK-5-4] Document distributed deployment mode — Acceptance: Describes multi-host mode with orchestrator on separate host (future)
- [TASK-5-5] Add component interaction diagram for each mode — Acceptance: ASCII or Mermaid diagram showing gateway, sandbox, orchestrator communication
- [TASK-5-6] Document configuration requirements per mode — Acceptance: Lists environment variables and network requirements
- [TASK-5-7] Update `docs/architecture/README.md` to link to orchestrator docs — Acceptance: New document is linked from architecture index

**Dependencies**: Phases 1-4 (documents the implemented functionality)

**Exit criteria**: Documentation accurately describes all three deployment modes

## Test Strategy

- **Unit tests**:
  - `shared/egg_orchestrator/` - Type serialization, enum values
  - `sandbox/egg_lib/orchestrator_client.py` - All signal methods with mocked responses
  - `gateway/gateway.py` - Health endpoint with orchestrator check
- **Integration tests**:
  - Sandbox entrypoint orchestrator mode - Mock orchestrator endpoint, verify signal on exit
  - Gateway health with orchestrator - Start mock orchestrator, verify health response
- **Manual testing**:
  - Run full SDLC pipeline locally with orchestrator enabled
  - Verify completion signals appear in orchestrator logs
  - Test container crash scenarios to verify error signaling

## Rollback Plan

All changes are additive and backward-compatible:

1. **AC-29 (shared types)**: If issues arise, orchestrator can continue using its local `models.py` types
2. **AC-27 (client)**: Client is opt-in via environment variables; if unused, no impact
3. **AC-28 (entrypoint)**: If `subprocess.run()` causes issues, revert to `os.execvpe()` - orchestrator mode won't work but interactive mode will
4. **AC-24 (health)**: Orchestrator check is conditional on `ORCHESTRATOR_URL`; if not set, no behavior change
5. **AC-33 (docs)**: Documentation only; no runtime impact

To rollback any phase:
```bash
git revert <commit-sha>
```

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| subprocess.run() changes interactive behavior | Medium | High | Extensive testing of both modes; preserve all environment variable handling |
| Shared types diverge from orchestrator models | Low | Medium | Import shared types in orchestrator; add type consistency tests |
| Health check timeout slows health endpoint | Low | Low | 2-second timeout cap; orchestrator check is non-blocking for overall status |
| Documentation becomes outdated | Medium | Low | Link to code references; keep docs close to implementation |

## Migration Notes

**No breaking changes**. All features are opt-in:
- Orchestrator mode requires `EGG_ORCHESTRATOR_URL` environment variable
- Gateway orchestrator health requires `ORCHESTRATOR_URL` environment variable
- Shared types are additive exports

**Future migration** (not in this PR): Orchestrator should import types from `egg_orchestrator` instead of defining them locally in `models.py`. This reduces type drift but is a separate concern.

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Add orchestrator integration layer (AC-24, AC-27, AC-28, AC-29, AC-33)"
  description: |
    Implements five acceptance criteria from the orchestrator contract (#524) that form
    the integration layer for remote/distributed deployment modes:

    - **AC-29**: Shared `egg_orchestrator` package with signal and pipeline state types
    - **AC-27**: Typed `OrchestratorClient` for sandbox-to-orchestrator communication
    - **AC-28**: Sandbox entrypoint orchestrator mode detection and completion signaling
    - **AC-24**: Gateway health endpoint reports orchestrator connectivity
    - **AC-33**: Architecture documentation for three deployment modes

    All features are opt-in via environment variables. No breaking changes.

    Closes #544
phases:
  - id: 1
    name: Shared Orchestrator Package (AC-29)
    goal: Create shared/egg_orchestrator/ with types needed by gateway and sandbox
    tasks:
      - id: TASK-1-1
        description: Create shared/egg_orchestrator/ package structure
        acceptance: Package directory exists with __init__.py, types.py, and py.typed
        files:
          - shared/egg_orchestrator/__init__.py
          - shared/egg_orchestrator/types.py
          - shared/egg_orchestrator/py.typed
      - id: TASK-1-2
        description: Extract shared enums (SignalType, AgentRole, PipelinePhase, PipelineStatus, ContainerStatus)
        acceptance: All enums are defined with docstrings, use StrEnum for serialization
        files:
          - shared/egg_orchestrator/types.py
      - id: TASK-1-3
        description: Create signal-related dataclasses (SignalPayload, SignalResponse)
        acceptance: Pydantic models match orchestrator/routes/signals.py request/response format
        files:
          - shared/egg_orchestrator/types.py
      - id: TASK-1-4
        description: Update shared/pyproject.toml to include egg_orchestrator*
        acceptance: Package is discoverable by setuptools
        files:
          - shared/pyproject.toml
      - id: TASK-1-5
        description: Add unit tests for shared types
        acceptance: Tests verify enum values, Pydantic serialization/deserialization
        files:
          - shared/tests/test_egg_orchestrator.py
  - id: 2
    name: Orchestrator Client (AC-27)
    goal: Create typed Python client for sandbox-to-orchestrator communication
    tasks:
      - id: TASK-2-1
        description: Create sandbox/egg_lib/orchestrator_client.py with OrchestratorClient class
        acceptance: Class follows patterns from orchestrator/gateway_client.py
        files:
          - sandbox/egg_lib/orchestrator_client.py
      - id: TASK-2-2
        description: Implement signal_complete() method
        acceptance: Sends POST to /api/v1/pipelines/{id}/signal with signal_type=complete
        files:
          - sandbox/egg_lib/orchestrator_client.py
      - id: TASK-2-3
        description: Implement signal_error() method
        acceptance: Sends error signal with message and recoverable flag
        files:
          - sandbox/egg_lib/orchestrator_client.py
      - id: TASK-2-4
        description: Implement signal_progress() method
        acceptance: Sends progress update with percentage and current task
        files:
          - sandbox/egg_lib/orchestrator_client.py
      - id: TASK-2-5
        description: Implement signal_heartbeat() method
        acceptance: Sends heartbeat with container ID
        files:
          - sandbox/egg_lib/orchestrator_client.py
      - id: TASK-2-6
        description: Add OrchestratorError exception class
        acceptance: Exception includes HTTP status code and error message
        files:
          - sandbox/egg_lib/orchestrator_client.py
      - id: TASK-2-7
        description: Add singleton accessor get_orchestrator_client()
        acceptance: Returns cached client instance, configurable via environment variables
        files:
          - sandbox/egg_lib/orchestrator_client.py
      - id: TASK-2-8
        description: Add unit tests for orchestrator client
        acceptance: Tests cover all signal methods with mocked HTTP responses
        files:
          - sandbox/tests/test_orchestrator_client.py
  - id: 3
    name: Sandbox Entrypoint Orchestrator Mode (AC-28)
    goal: Detect orchestrator mode and signal completion on exit using wrapper process
    tasks:
      - id: TASK-3-1
        description: Add orchestrator environment variable detection to entrypoint
        acceptance: Reads EGG_ORCHESTRATOR_URL, EGG_PIPELINE_ID, EGG_AGENT_ROLE from environment
        files:
          - sandbox/entrypoint.py
      - id: TASK-3-2
        description: Refactor run_interactive() to use subprocess.run() instead of os.execvpe()
        acceptance: Interactive mode launches subprocess and captures exit code
        files:
          - sandbox/entrypoint.py
      - id: TASK-3-3
        description: Refactor run_exec() to use subprocess.run() instead of os.execvpe()
        acceptance: Exec mode launches subprocess and captures exit code
        files:
          - sandbox/entrypoint.py
      - id: TASK-3-4
        description: Add signal_completion() function
        acceptance: Calls orchestrator client's signal_complete() or signal_error() based on exit code
        files:
          - sandbox/entrypoint.py
      - id: TASK-3-5
        description: Integrate completion signaling after subprocess exits
        acceptance: Completion signal sent before entrypoint exits when orchestrator mode detected
        files:
          - sandbox/entrypoint.py
      - id: TASK-3-6
        description: Update signal handlers to signal completion before cleanup
        acceptance: SIGTERM/SIGINT handlers signal completion if in orchestrator mode
        files:
          - sandbox/entrypoint.py
      - id: TASK-3-7
        description: Add integration test for orchestrator mode detection
        acceptance: Test verifies signal is sent on normal exit and error exit
        files:
          - sandbox/tests/test_entrypoint_orchestrator.py
  - id: 4
    name: Gateway Health Enhancement (AC-24)
    goal: Add optional orchestrator connectivity check to gateway health endpoint
    tasks:
      - id: TASK-4-1
        description: Add ORCHESTRATOR_URL environment variable support to gateway
        acceptance: Gateway reads ORCHESTRATOR_URL from environment
        files:
          - gateway/gateway.py
      - id: TASK-4-2
        description: Create check_orchestrator_health() helper function
        acceptance: Uses urllib to GET orchestrator /api/v1/health with 2-second timeout
        files:
          - gateway/gateway.py
      - id: TASK-4-3
        description: Update /api/v1/health endpoint to include orchestrator field
        acceptance: Response includes orchestrator reachable and url when ORCHESTRATOR_URL is set
        files:
          - gateway/gateway.py
      - id: TASK-4-4
        description: Ensure health check remains fast (less than 3 seconds)
        acceptance: Short timeout prevents slow health responses
        files:
          - gateway/gateway.py
      - id: TASK-4-5
        description: Add unit test for health endpoint with orchestrator
        acceptance: Tests cover orchestrator reachable, unreachable, and not configured cases
        files:
          - gateway/tests/test_health_orchestrator.py
  - id: 5
    name: Architecture Documentation (AC-33)
    goal: Document orchestrator deployment modes and component interactions
    tasks:
      - id: TASK-5-1
        description: Create docs/architecture/orchestrator.md
        acceptance: Document exists with proper markdown structure
        files:
          - docs/architecture/orchestrator.md
      - id: TASK-5-2
        description: Document local deployment mode
        acceptance: Describes CLI-only mode without orchestrator, includes when to use
        files:
          - docs/architecture/orchestrator.md
      - id: TASK-5-3
        description: Document remote-single deployment mode
        acceptance: Describes Docker Compose mode with orchestrator on same host
        files:
          - docs/architecture/orchestrator.md
      - id: TASK-5-4
        description: Document distributed deployment mode
        acceptance: Describes multi-host mode with orchestrator on separate host (future)
        files:
          - docs/architecture/orchestrator.md
      - id: TASK-5-5
        description: Add component interaction diagram for each mode
        acceptance: ASCII or Mermaid diagram showing gateway, sandbox, orchestrator communication
        files:
          - docs/architecture/orchestrator.md
      - id: TASK-5-6
        description: Document configuration requirements per mode
        acceptance: Lists environment variables and network requirements
        files:
          - docs/architecture/orchestrator.md
      - id: TASK-5-7
        description: Update docs/architecture/README.md to link to orchestrator docs
        acceptance: New document is linked from architecture index
        files:
          - docs/architecture/README.md
```

---

*Authored-by: egg*
