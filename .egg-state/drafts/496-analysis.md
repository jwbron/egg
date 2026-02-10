# Analysis: Create a dedicated orchestration system for the SDLC pipeline

> Issue: #496 | Phase: refine

## Problem Statement

The current SDLC pipeline runs exclusively in GitHub Actions, which creates friction for local development and testing. Issue #437 attempted to enable local execution by wrapping the existing workflows with `nektos/act` or a hybrid approach using Claude Code sessions. However, this approach was abandoned because:

1. **Tight coupling to GitHub Actions**: The prompt builders and workflow logic assume GitHub Actions context (e.g., `GITHUB_OUTPUT`, `gh` API access, workflow job boundaries)
2. **No persistent state management**: Each workflow job is ephemeral; state must be passed via GitHub API or committed files
3. **No interactive feedback loop**: Human approval requires GitHub issue checkboxes, which is awkward for local workflows
4. **Container lifecycle complexity**: The current `egg-deploy` only manages the gateway; sandbox containers are started per-session with no pipeline-level orchestration

The desired outcome is a dedicated `egg-orchestrator` component that manages the full SDLC pipeline lifecycle locally, serving as a 1:1 drop-in replacement for the GitHub Actions orchestration.

## Current Behavior

### Architecture Overview

The egg system uses a two-container architecture:

| Component | Purpose | Lifecycle |
|-----------|---------|-----------|
| **egg-gateway** | Policy enforcement, credential injection, network filtering | Service (long-running) |
| **egg-sandbox** | LLM agent execution, code operations | Job (per-session) |

### Current SDLC Pipeline (GitHub Actions)

The production SDLC pipeline is defined in `.github/workflows/`:

- **`sdlc-pipeline.yml`**: Orchestrates the four phases (refine → plan → implement → pr)
- **`sdlc-work-loop.yml`**: Executes work/review/respond cycles within each phase
- **`sdlc-multi-agent.yml`**: Dispatches parallel agents during implement phase
- **`sdlc-hitl.yml`**: Handles human-in-the-loop decisions

**State Management**:
- Contract stored in `.egg-state/contracts/{issue}.json` (committed to feature branch)
- Drafts stored in `.egg-state/drafts/{issue}-{phase}.md`
- Reviews stored in `.egg-state/reviews/{issue}-{phase}-review.json`

**Communication**:
- Sandbox → Gateway via HTTP REST API (port 9848)
- Human → Pipeline via GitHub issue checkboxes and PR reviews
- Phase transitions via `POST /api/v1/phase/advance`

### Existing Orchestration Code

The `shared/egg_contracts/` library contains orchestration logic that currently runs within GitHub Actions:

- **`orchestrator.py`**: `Orchestrator` class for multi-agent dispatch decisions
- **`orchestration.py`**: `OrchestrationState` for tracking agent executions
- **`agent_roles.py`**: Agent role definitions (Coder, Tester, Documenter, Integrator)
- **`dependency_graph.py`**: Computes parallel execution waves

This code is framework-agnostic and could be used by a dedicated orchestrator service.

### Local Deployment

Currently, `bin/egg-deploy` manages Docker Compose for the gateway only:

```bash
egg-deploy up     # Start gateway service
egg --compose     # Start sandbox session (interactive)
```

There is no mechanism to:
- Start multiple sandbox containers for parallel agents
- Manage a pipeline that spans multiple sandbox sessions
- Store pipeline state independently of GitHub

## Constraints

### Technical Constraints

- **Container isolation**: Sandbox containers must not have direct network access or credentials
- **State persistence**: Pipeline state must survive container restarts and human review delays
- **Phase enforcement**: Operations must be validated against the current phase (gateway policy)
- **Credential injection**: GitHub tokens and Anthropic API keys are injected by the gateway
- **Network topology**: Dual-network architecture (isolated + external) for security

### Business Constraints

- **Feature parity**: Local workflow must support the same phases and quality gates as CI
- **Minimal disruption**: Existing gateway and sandbox should require minimal changes
- **Future extensibility**: Design should allow remote deployment (single server or distributed)

### Dependencies

- Docker and Docker Compose
- Anthropic API credentials
- GitHub authentication (for PR creation)
- Existing `egg_contracts` library for orchestration logic

## Options Considered

### Option A: Extend egg-gateway with Orchestration

**Approach**: Add orchestration endpoints to the existing gateway service. The gateway would manage pipeline state, start/stop sandbox containers, and expose REST APIs for phase transitions.

```
egg-gateway (enhanced)
├── Existing: Git/GH operations, policy enforcement
└── New: Pipeline orchestration, container lifecycle, HITL polling
```

**Pros**:
- Single service to deploy and maintain
- Reuses existing session management and authentication
- Gateway already holds credentials and has external network access
- Minimal new infrastructure

**Cons**:
- Increases gateway complexity and attack surface
- Mixes policy enforcement with orchestration concerns
- Harder to scale orchestration independently
- Gateway restart would disrupt all active pipelines

### Option B: New egg-orchestrator Service (Recommended)

**Approach**: Create a dedicated `egg-orchestrator` container that manages the SDLC pipeline lifecycle. It communicates with the gateway for credentials and policy validation, and manages sandbox containers for agent execution.

```
┌─────────────────────────────────────────────────────────────┐
│  Host Machine                                                │
│                                                              │
│  ┌──────────────────┐   ┌──────────────────┐                │
│  │  egg-orchestrator │   │   egg-gateway    │                │
│  │  (pipeline state, │   │ (policy, creds)  │                │
│  │   container mgmt) │   │                  │                │
│  └────────┬─────────┘   └────────┬─────────┘                │
│           │                      │                           │
│           ▼                      │                           │
│  ┌────────────────┐              │                           │
│  │  egg-sandbox   │◄─────────────┘                           │
│  │  (phase: refine)              │                           │
│  └────────────────┘                                          │
└─────────────────────────────────────────────────────────────┘
```

**Orchestrator Responsibilities**:
- Pipeline state management (create, advance, fail)
- Sandbox container lifecycle (start, monitor, stop)
- HITL decision management (queue decisions, wait for human input)
- Error handling and retries (circuit breaker, escalation)
- Notifications (via Slack or local files)

**Gateway Responsibilities** (unchanged):
- Git/GitHub operations with policy enforcement
- Credential injection (Anthropic API key, GitHub token)
- Network filtering (proxy)
- Phase validation for individual operations

**Communication**:
```
Human ──/sdlc command──▶ Initial Sandbox ──REST──▶ Orchestrator
                                                        │
Orchestrator ──spawns──▶ Phase Sandbox ──REST──▶ Gateway
                                                        │
Gateway ──policy check──▶ Orchestrator (via callback or polling)
```

**Pros**:
- Clean separation of concerns
- Orchestrator can be restarted without affecting gateway security
- Easier to add features (webhooks, remote API, distributed execution)
- Can run multiple orchestrators for different pipelines
- Orchestration logic can be tested independently

**Cons**:
- Additional container to deploy
- New REST API to design and maintain
- More complex Docker Compose configuration
- Slightly higher resource usage

### Option C: Sidecar Pattern (Orchestrator in Sandbox)

**Approach**: Embed orchestration logic in the sandbox container, with a persistent orchestrator process that survives between agent sessions.

**Pros**:
- No new container
- Orchestrator has same context as agents

**Cons**:
- Sandbox is meant to be ephemeral and untrusted
- Mixing trusted orchestration with untrusted agent execution
- Container lifecycle becomes complex (when does it restart?)
- Violates the security model

## Recommended Approach

**Option B: New egg-orchestrator Service** is recommended for the following reasons:

1. **Security model preservation**: Orchestration is a trusted operation (managing credentials, making policy decisions). It should run in a separate, trusted container like the gateway.

2. **Separation of concerns**: The gateway handles request-level policy; the orchestrator handles pipeline-level coordination. This mirrors how GitHub Actions (orchestration) is separate from the runner (execution).

3. **Future extensibility**: A dedicated orchestrator can be:
   - Exposed via remote API for non-local access
   - Deployed on a separate server
   - Scaled horizontally for multiple concurrent pipelines
   - Replaced with a more sophisticated system (Temporal, Prefect) if needed

4. **Resilience**: Pipeline state persisted by the orchestrator survives gateway restarts. Human review can take hours or days; orchestrator state must persist.

### Proposed Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Docker Compose (local deployment)                               │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  egg-orchestrator                                         │   │
│  │  Port: 9849                                               │   │
│  │                                                           │   │
│  │  REST API:                                                │   │
│  │    POST /api/v1/pipelines              (create pipeline)  │   │
│  │    GET  /api/v1/pipelines/{id}         (get state)        │   │
│  │    POST /api/v1/pipelines/{id}/phase   (advance phase)    │   │
│  │    POST /api/v1/pipelines/{id}/hitl    (submit decision)  │   │
│  │    POST /api/v1/pipelines/{id}/signal  (agent callback)   │   │
│  │                                                           │   │
│  │  State Store:                                             │   │
│  │    /state/pipelines/{id}.json                             │   │
│  │    /state/decisions/{id}/{decision_id}.json               │   │
│  │                                                           │   │
│  │  Docker Socket: (for container management)                │   │
│  │    /var/run/docker.sock                                   │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  egg-gateway (existing, minimal changes)                  │   │
│  │  Port: 9848, 3129                                         │   │
│  │                                                           │   │
│  │  New: /api/v1/orchestrator/callback (phase complete)      │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  egg-sandbox (spawned per phase/agent)                    │   │
│  │  Managed by orchestrator                                  │   │
│  │                                                           │   │
│  │  Entry: orchestrator injects phase prompt and context     │   │
│  │  Exit: signals completion to orchestrator                 │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

### Workflow

1. **Human starts initial sandbox**: `egg --compose` or equivalent
2. **Human invokes `/sdlc` skill**: Claude asks for problem context
3. **Agent initializes pipeline**: `POST /api/v1/pipelines` to orchestrator
4. **Agent submits refine context**: `POST /api/v1/pipelines/{id}/phase` with initial analysis
5. **Orchestrator spawns refine sandbox**: Runs the refine phase prompt
6. **Refine agent completes**: Signals orchestrator with draft
7. **Orchestrator returns to initial sandbox**: Shows draft to human for review
8. **Human approves**: `POST /api/v1/pipelines/{id}/hitl` with approval
9. **Orchestrator spawns plan sandbox**: Runs the plan phase prompt
10. **Repeat** for implement phase
11. **Optional PR creation**: Based on user configuration

### Implementation Phases

**Phase 1: Core Orchestrator Infrastructure**
- Create `orchestrator/` directory with Dockerfile
- Implement pipeline state model and persistence
- Implement REST API for pipeline lifecycle
- Add to `docker-compose.yml`

**Phase 2: Container Management**
- Implement sandbox spawning via Docker API
- Implement health monitoring and cleanup
- Handle container failures with retries

**Phase 3: HITL Integration**
- Implement decision queue and polling
- Create notification mechanism (files or Slack)
- Build `/sdlc` skill for Claude Code

**Phase 4: Phase Execution**
- Integrate with existing prompt builders
- Implement internal review loop
- Add circuit breaker for runaway loops

**Phase 5: PR and GitHub Integration**
- Implement optional PR creation
- Branch preservation on host
- GitHub notifications

## Open Questions

The following questions would help refine the implementation:

<!-- HITL Decision: orchestrator_scope -->
**Should the orchestrator manage multi-agent parallel execution?**

The current CI pipeline supports parallel agents (Coder, Tester, Documenter, Integrator) via `sdlc-multi-agent.yml`. This adds complexity to the orchestrator.

- [ ] Yes, full multi-agent support (orchestrator manages parallel sandbox containers)
- [ ] Simplified model (single agent per phase, orchestrator handles sequencing only)
- [ ] Other (explain in reply)

---

<!-- HITL Decision: state_persistence -->
**How should pipeline state be persisted?**

Pipeline state (phase, decisions, agent outputs) needs to survive restarts and human review delays.

- [ ] File-based (JSON in `/state/` volume) - simpler, no dependencies
- [ ] SQLite database - better for querying, atomic updates
- [ ] PostgreSQL (shared with other egg components if available)
- [ ] Other (explain in reply)

---

<!-- HITL Decision: hitl_mechanism -->
**How should humans interact with HITL decisions locally?**

The CI pipeline uses GitHub issue checkboxes. Local alternatives:

- [ ] Claude Code session (agent polls orchestrator, shows decisions inline)
- [ ] CLI prompts (orchestrator writes to stdout, reads stdin)
- [ ] File-based (human edits a file, orchestrator watches for changes)
- [ ] Local web UI (orchestrator serves a simple HTML page)
- [ ] Other (explain in reply)

---

<!-- HITL Decision: initial_sandbox -->
**Should the initial human interaction use a dedicated sandbox or run on host?**

The `/sdlc` skill needs to gather context from the human before starting the pipeline.

- [ ] Run in a sandbox container (consistent with other phases, but requires container startup)
- [ ] Run directly on host with Claude Code (faster, simpler, but different environment)
- [ ] Other (explain in reply)

---

**Open-ended questions:**

- Should the orchestrator expose a webhook for external triggers (e.g., GitHub app, remote API)?
- What is the expected concurrent pipeline count for local development?
- Should the orchestrator support resuming pipelines after a full system restart (durability requirement)?

---

*Authored-by: egg*
