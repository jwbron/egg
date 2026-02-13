# Plan: Create a dedicated orchestration system for the SDLC pipeline

> Issue: #496 | Phase: plan

## Summary

This plan introduces `egg-orchestrator`, a new Docker container service that manages the full SDLC pipeline lifecycle locally, providing a 1:1 replacement for GitHub Actions orchestration. The orchestrator will store state in git (consistent with contracts/checkpoints), manage sandbox container lifecycle via Docker API, and support multi-agent parallel execution. The implementation reuses existing orchestration logic from `shared/egg_contracts/` while adding REST API endpoints, container lifecycle management, and HITL integration for Claude Code sessions.

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│  Docker Compose (local deployment)                               │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  egg-orchestrator (NEW)                                   │   │
│  │  Port: 9849                                               │   │
│  │                                                           │   │
│  │  - Pipeline state management (git-backed)                 │   │
│  │  - Sandbox container lifecycle (Docker API)               │   │
│  │  - HITL decision coordination                             │   │
│  │  - Webhook endpoint for future expansion                  │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  egg-gateway (minimal changes)                            │   │
│  │  Port: 9848, 3129                                         │   │
│  │                                                           │   │
│  │  - Existing: git/gh operations, policy enforcement        │   │
│  │  - New: orchestrator callback endpoint                    │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  egg-sandbox (spawned per phase/agent)                    │   │
│  │  Managed by orchestrator                                  │   │
│  │                                                           │   │
│  │  - Reports state to orchestrator on completion            │   │
│  │  - HITL sandbox polls orchestrator for decisions          │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

## Implementation Phases

### Phase 1: Core Infrastructure

**Goal**: Establish the orchestrator service foundation with state management and REST API skeleton.

**Tasks**:
- [TASK-1-1] Create orchestrator directory structure and Dockerfile — Acceptance: `orchestrator/Dockerfile` builds successfully, produces runnable image
- [TASK-1-2] Define pipeline state model with Pydantic — Acceptance: State model supports all SDLC phases, agent executions, HITL decisions; unit tests pass
- [TASK-1-3] Implement git-backed state persistence — Acceptance: State reads/writes to `.egg-state/pipelines/{id}.json` on work branch; survives container restart
- [TASK-1-4] Create Flask REST API skeleton with health endpoint — Acceptance: `/api/v1/health` returns 200; `/api/v1/pipelines` CRUD endpoints respond
- [TASK-1-5] Add orchestrator to docker-compose.yml — Acceptance: `docker compose up` starts orchestrator alongside gateway; health checks pass

**Dependencies**: None

**Exit criteria**: Orchestrator container starts, persists state to git, exposes working REST API

### Phase 2: Container Lifecycle Management

**Goal**: Enable orchestrator to spawn and manage sandbox containers for phase execution.

**Tasks**:
- [TASK-2-1] Implement Docker API client for container operations — Acceptance: Can create, start, stop, remove sandbox containers programmatically
- [TASK-2-2] Create sandbox container template configuration — Acceptance: Spawned sandboxes have correct network, volume, and environment configuration matching existing sandbox setup
- [TASK-2-3] Implement container health monitoring and cleanup — Acceptance: Orchestrator detects unhealthy/exited containers; removes orphaned containers on startup
- [TASK-2-4] Add container lifecycle endpoints to REST API — Acceptance: `POST /api/v1/pipelines/{id}/spawn` creates sandbox; `DELETE` removes it
- [TASK-2-5] Implement retry logic with circuit breaker — Acceptance: Failed container spawns retry up to 3 times with exponential backoff; circuit breaker prevents runaway loops

**Dependencies**: Phase 1

**Exit criteria**: Orchestrator can spawn sandbox containers, monitor health, and clean up failures

### Phase 3: Pipeline Orchestration Logic

**Goal**: Port existing orchestration logic to orchestrator service for phase sequencing and multi-agent dispatch.

**Tasks**:
- [TASK-3-1] Integrate existing Orchestrator class from egg_contracts — Acceptance: Dispatch logic works with new state model; existing tests pass
- [TASK-3-2] Implement phase transition endpoints — Acceptance: `POST /api/v1/pipelines/{id}/phase` advances phases with validation
- [TASK-3-3] Add multi-agent parallel execution support — Acceptance: Orchestrator spawns multiple sandbox containers for parallel agent waves
- [TASK-3-4] Implement agent result collection and handoffs — Acceptance: Agent outputs collected from `.egg-state/agent-outputs/`; handoff data passed to dependent agents
- [TASK-3-5] Create notification endpoint for sandbox callbacks — Acceptance: Sandbox calls `POST /api/v1/pipelines/{id}/signal` on completion; orchestrator updates state

**Dependencies**: Phase 2

**Exit criteria**: Full phase sequencing works locally; multi-agent waves execute in parallel

### Phase 4: HITL Integration

**Goal**: Enable human-in-the-loop decisions through Claude Code sessions in sandbox.

**Tasks**:
- [TASK-4-1] Implement decision queue and polling mechanism — Acceptance: `POST /api/v1/pipelines/{id}/decisions` queues decisions; `GET` returns pending decisions
- [TASK-4-2] Create HITL sandbox mode for Claude sessions — Acceptance: HITL sandbox receives pending decisions and displays to human via Claude
- [TASK-4-3] Implement decision resolution endpoints — Acceptance: `POST /api/v1/pipelines/{id}/decisions/{decision_id}/resolve` updates state and resumes pipeline
- [TASK-4-4] Add timeout and escalation for stale decisions — Acceptance: Decisions older than configurable timeout trigger notification; state shows "awaiting_human"
- [TASK-4-5] Create `/sdlc` skill for Claude Code — Acceptance: `/sdlc` command initializes pipeline, gathers context, and starts orchestration

**Dependencies**: Phase 3

**Exit criteria**: Humans can interact with pipeline decisions through Claude Code in sandbox

### Phase 5: Gateway Integration

**Goal**: Add minimal gateway changes for orchestrator callbacks and session coordination.

**Tasks**:
- [TASK-5-1] Add orchestrator callback endpoint to gateway — Acceptance: `POST /api/v1/orchestrator/callback` forwards phase completion to orchestrator
- [TASK-5-2] Implement session token coordination — Acceptance: Orchestrator requests session tokens from gateway for spawned sandboxes
- [TASK-5-3] Add orchestrator URL to sandbox environment injection — Acceptance: Spawned sandboxes have `ORCHESTRATOR_URL` environment variable
- [TASK-5-4] Update gateway health check to include orchestrator status — Acceptance: Gateway health reports orchestrator connectivity

**Dependencies**: Phase 3

**Exit criteria**: Gateway and orchestrator communicate bidirectionally; session coordination works

### Phase 6: CLI and Deployment

**Goal**: Update CLI and deployment tooling for the new three-container architecture.

**Tasks**:
- [TASK-6-1] Update `bin/egg-deploy` for orchestrator lifecycle — Acceptance: `egg-deploy up` starts gateway + orchestrator; `status` shows both
- [TASK-6-2] Add orchestrator constants to egg_config — Acceptance: `ORCHESTRATOR_PORT`, `ORCHESTRATOR_CONTAINER_NAME` defined in `constants.py`
- [TASK-6-3] Create orchestrator client library for sandbox — Acceptance: `sandbox/egg_lib/orchestrator_client.py` provides typed API client
- [TASK-6-4] Update sandbox entrypoint for orchestrator mode — Acceptance: Sandbox detects orchestrator mode and reports completion on exit
- [TASK-6-5] Add orchestrator to shared module installation — Acceptance: `egg_orchestrator` package available in gateway and sandbox containers

**Dependencies**: Phase 5

**Exit criteria**: Full local deployment works with `egg-deploy up`; sandboxes communicate with orchestrator

### Phase 7: Webhook and Future Extensibility

**Goal**: Add webhook endpoint and configuration for future remote deployment.

**Tasks**:
- [TASK-7-1] Implement webhook receiver endpoint — Acceptance: `POST /api/v1/webhooks/github` can receive GitHub events (for future use)
- [TASK-7-2] Add authentication mechanism for remote access — Acceptance: Webhook and API endpoints support bearer token authentication
- [TASK-7-3] Create configuration for remote orchestrator URL — Acceptance: Sandbox can be configured to use remote orchestrator via environment variable
- [TASK-7-4] Document deployment modes (local, remote-single, distributed) — Acceptance: Architecture docs describe all deployment modes

**Dependencies**: Phase 6

**Exit criteria**: Webhook infrastructure in place; architecture supports future remote deployment

## Test Strategy

- **Unit tests**: State model serialization, phase transitions, container template generation, decision queue logic
- **Integration tests**:
  - Orchestrator ↔ Docker API container lifecycle
  - Orchestrator ↔ Gateway callback communication
  - Sandbox ↔ Orchestrator signal/polling
  - Git state persistence across restarts
- **E2E tests**:
  - Full pipeline: `/sdlc` → refine → plan → implement → PR
  - Multi-agent parallel execution wave
  - HITL decision flow with human approval
  - Container failure and retry
- **Manual testing**:
  1. Start `egg-deploy up`, verify gateway + orchestrator healthy
  2. Start sandbox with `/sdlc`, provide issue context
  3. Observe phase transitions through orchestrator logs
  4. Approve HITL decisions in Claude session
  5. Verify PR created on work branch

## Rollback Plan

1. **Feature flag**: Add `EGG_USE_ORCHESTRATOR=false` to bypass orchestrator and use direct sandbox execution (legacy mode)
2. **Independent containers**: Orchestrator failure should not affect gateway; sandboxes fall back to direct communication
3. **Git state recovery**: If orchestrator state corrupts, delete `.egg-state/pipelines/` and reinitialize from issue
4. **Container cleanup**: `docker rm -f $(docker ps -aq -f name=egg-sandbox-)` removes all spawned sandboxes
5. **Full rollback**: Revert to pre-orchestrator docker-compose.yml; remove orchestrator container and image

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Docker socket access security | Medium | High | Run orchestrator as non-root; limit Docker API operations to egg-sandbox-* containers only |
| State corruption from concurrent writes | Low | Medium | Use file locking for git operations; single-writer pattern for state files |
| Container resource exhaustion | Medium | Medium | Limit max concurrent sandboxes; implement container cleanup on orchestrator startup |
| Gateway-orchestrator communication failure | Low | High | Health checks with retry; graceful degradation to manual mode |
| HITL decision timeout blocking pipeline | Medium | Low | Configurable timeout with notification; explicit "awaiting human" state |

## Migration Notes

- **No breaking changes**: Existing gateway and sandbox continue to work independently
- **Opt-in orchestration**: Pipelines only use orchestrator when initiated via `/sdlc` skill
- **Config additions**: New environment variables (`ORCHESTRATOR_URL`, `EGG_USE_ORCHESTRATOR`) with sensible defaults
- **Docker Compose**: Gateway service unchanged; orchestrator added as new service
- **Shared modules**: New `egg_orchestrator` package added to shared; no changes to existing `egg_contracts`

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Add egg-orchestrator for local SDLC pipeline execution"
  description: |
    Introduces egg-orchestrator, a new Docker container service that manages the
    full SDLC pipeline lifecycle locally, replacing GitHub Actions orchestration.

    The orchestrator stores state in git (consistent with contracts), manages
    sandbox container lifecycle via Docker API, supports multi-agent parallel
    execution, and integrates HITL decisions through Claude Code sessions.

    Issue: #496
phases:
  - id: 1
    name: Core Infrastructure
    goal: Establish orchestrator service foundation with state management and REST API
    tasks:
      - id: TASK-1-1
        description: Create orchestrator directory structure and Dockerfile
        acceptance: Dockerfile builds successfully, produces runnable image
        files:
          - orchestrator/Dockerfile
          - orchestrator/__init__.py
          - orchestrator/requirements.txt
      - id: TASK-1-2
        description: Define pipeline state model with Pydantic
        acceptance: State model supports all SDLC phases, agent executions, HITL decisions; unit tests pass
        files:
          - orchestrator/models.py
          - orchestrator/tests/test_models.py
      - id: TASK-1-3
        description: Implement git-backed state persistence
        acceptance: State reads/writes to .egg-state/pipelines/{id}.json on work branch; survives restart
        files:
          - orchestrator/state_store.py
          - orchestrator/tests/test_state_store.py
      - id: TASK-1-4
        description: Create Flask REST API skeleton with health endpoint
        acceptance: /api/v1/health returns 200; /api/v1/pipelines CRUD endpoints respond
        files:
          - orchestrator/api.py
          - orchestrator/routes/health.py
          - orchestrator/routes/pipelines.py
      - id: TASK-1-5
        description: Add orchestrator to docker-compose.yml
        acceptance: docker compose up starts orchestrator alongside gateway; health checks pass
        files:
          - docker-compose.yml
  - id: 2
    name: Container Lifecycle Management
    goal: Enable orchestrator to spawn and manage sandbox containers for phase execution
    tasks:
      - id: TASK-2-1
        description: Implement Docker API client for container operations
        acceptance: Can create, start, stop, remove sandbox containers programmatically
        files:
          - orchestrator/docker_client.py
          - orchestrator/tests/test_docker_client.py
      - id: TASK-2-2
        description: Create sandbox container template configuration
        acceptance: Spawned sandboxes have correct network, volume, and environment configuration
        files:
          - orchestrator/sandbox_template.py
      - id: TASK-2-3
        description: Implement container health monitoring and cleanup
        acceptance: Orchestrator detects unhealthy/exited containers; removes orphaned containers
        files:
          - orchestrator/container_monitor.py
      - id: TASK-2-4
        description: Add container lifecycle endpoints to REST API
        acceptance: POST /api/v1/pipelines/{id}/spawn creates sandbox; DELETE removes it
        files:
          - orchestrator/routes/containers.py
      - id: TASK-2-5
        description: Implement retry logic with circuit breaker
        acceptance: Failed spawns retry up to 3 times with backoff; circuit breaker prevents loops
        files:
          - orchestrator/resilience.py
  - id: 3
    name: Pipeline Orchestration Logic
    goal: Port existing orchestration logic for phase sequencing and multi-agent dispatch
    tasks:
      - id: TASK-3-1
        description: Integrate existing Orchestrator class from egg_contracts
        acceptance: Dispatch logic works with new state model; existing tests pass
        files:
          - orchestrator/dispatch.py
      - id: TASK-3-2
        description: Implement phase transition endpoints
        acceptance: POST /api/v1/pipelines/{id}/phase advances phases with validation
        files:
          - orchestrator/routes/phases.py
      - id: TASK-3-3
        description: Add multi-agent parallel execution support
        acceptance: Orchestrator spawns multiple sandbox containers for parallel agent waves
        files:
          - orchestrator/multi_agent.py
      - id: TASK-3-4
        description: Implement agent result collection and handoffs
        acceptance: Agent outputs collected; handoff data passed to dependent agents
        files:
          - orchestrator/handoffs.py
      - id: TASK-3-5
        description: Create notification endpoint for sandbox callbacks
        acceptance: Sandbox calls POST /api/v1/pipelines/{id}/signal on completion
        files:
          - orchestrator/routes/signals.py
  - id: 4
    name: HITL Integration
    goal: Enable human-in-the-loop decisions through Claude Code sessions in sandbox
    tasks:
      - id: TASK-4-1
        description: Implement decision queue and polling mechanism
        acceptance: POST /api/v1/pipelines/{id}/decisions queues; GET returns pending
        files:
          - orchestrator/decision_queue.py
          - orchestrator/routes/decisions.py
      - id: TASK-4-2
        description: Create HITL sandbox mode for Claude sessions
        acceptance: HITL sandbox receives pending decisions and displays to human
        files:
          - sandbox/egg_lib/hitl_mode.py
      - id: TASK-4-3
        description: Implement decision resolution endpoints
        acceptance: POST /api/v1/pipelines/{id}/decisions/{id}/resolve updates state
        files:
          - orchestrator/routes/decisions.py
      - id: TASK-4-4
        description: Add timeout and escalation for stale decisions
        acceptance: Decisions older than timeout trigger notification
        files:
          - orchestrator/decision_timeout.py
      - id: TASK-4-5
        description: Create /sdlc skill for Claude Code
        acceptance: /sdlc command initializes pipeline and starts orchestration
        files:
          - sandbox/.claude/commands/sdlc.md
  - id: 5
    name: Gateway Integration
    goal: Add minimal gateway changes for orchestrator callbacks and session coordination
    tasks:
      - id: TASK-5-1
        description: Add orchestrator callback endpoint to gateway
        acceptance: POST /api/v1/orchestrator/callback forwards phase completion
        files:
          - gateway/orchestrator_api.py
      - id: TASK-5-2
        description: Implement session token coordination
        acceptance: Orchestrator requests session tokens from gateway for spawned sandboxes
        files:
          - orchestrator/session_coordinator.py
      - id: TASK-5-3
        description: Add orchestrator URL to sandbox environment injection
        acceptance: Spawned sandboxes have ORCHESTRATOR_URL environment variable
        files:
          - orchestrator/sandbox_template.py
      - id: TASK-5-4
        description: Update gateway health check to include orchestrator status
        acceptance: Gateway health reports orchestrator connectivity
        files:
          - gateway/gateway.py
  - id: 6
    name: CLI and Deployment
    goal: Update CLI and deployment tooling for three-container architecture
    tasks:
      - id: TASK-6-1
        description: Update bin/egg-deploy for orchestrator lifecycle
        acceptance: egg-deploy up starts gateway + orchestrator; status shows both
        files:
          - bin/egg-deploy
      - id: TASK-6-2
        description: Add orchestrator constants to egg_config
        acceptance: ORCHESTRATOR_PORT, ORCHESTRATOR_CONTAINER_NAME defined
        files:
          - shared/egg_config/constants.py
      - id: TASK-6-3
        description: Create orchestrator client library for sandbox
        acceptance: sandbox/egg_lib/orchestrator_client.py provides typed API client
        files:
          - sandbox/egg_lib/orchestrator_client.py
      - id: TASK-6-4
        description: Update sandbox entrypoint for orchestrator mode
        acceptance: Sandbox detects orchestrator mode and reports completion on exit
        files:
          - sandbox/entrypoint.py
      - id: TASK-6-5
        description: Add orchestrator to shared module installation
        acceptance: egg_orchestrator package available in containers
        files:
          - shared/egg_orchestrator/__init__.py
          - shared/setup.py
  - id: 7
    name: Webhook and Future Extensibility
    goal: Add webhook endpoint and configuration for future remote deployment
    tasks:
      - id: TASK-7-1
        description: Implement webhook receiver endpoint
        acceptance: POST /api/v1/webhooks/github can receive GitHub events
        files:
          - orchestrator/routes/webhooks.py
      - id: TASK-7-2
        description: Add authentication mechanism for remote access
        acceptance: Webhook and API endpoints support bearer token authentication
        files:
          - orchestrator/auth.py
      - id: TASK-7-3
        description: Create configuration for remote orchestrator URL
        acceptance: Sandbox can use remote orchestrator via environment variable
        files:
          - shared/egg_config/configs/orchestrator.py
      - id: TASK-7-4
        description: Document deployment modes
        acceptance: Architecture docs describe local, remote-single, distributed modes
        files:
          - docs/architecture/orchestrator.md
```

---

*Authored-by: egg*
