# Analysis: DinD Deployment Validation in Check Phase

> Issue: #645 | Phase: refine

## Problem Statement

The egg check phase currently runs static/unit-level validations: `make lint`, `make test`, and merge-conflict detection. These checks verify code correctness in isolation but cannot validate that agent-authored changes actually work in a running deployment — services start, respond to HTTP requests, integrate correctly with databases and caches, etc.

For target applications with fully Dockerized devserver stacks, we can leverage Docker-in-Docker to bring up the application during the check phase and validate against it. This gives egg end-to-end validation: not just "does the code compile and pass unit tests," but "does the service actually boot and respond correctly with the agent's modifications."

**Critical constraint**: Agent code is untrusted (AI-generated). The deployment validation must execute agent-modified code inside devserver containers while preventing exfiltration, credential theft, and sandbox escape.

## Current Architecture

### Check Phase Infrastructure

Checks are defined as `CheckDefinition` objects in `shared/egg_contracts/phase_defaults.py` and executed by `CheckRunner` subclasses in `.github/scripts/checks/`.

**Existing implement-phase checks:**
- `check-merge-conflict` — Detects git merge conflicts (required)
- `check-lint` — Runs `make lint` (required, 1 retry)
- `check-test` — Runs `make test` or `pytest` (required)
- `check-fixer` — Auto-fixes lint issues (optional)

**Check execution path:**
1. `run_check.py` loads the check class from the registry
2. Instantiates it with `(contract, repo_root)`
3. Calls `check.run()` which returns a `CheckResult` (PASS/FAIL/SKIP)
4. All checks run **inside the sandbox container** as subprocess calls

**Key limitation for deployment validation:** The sandbox container has no Docker socket access and no ability to manage containers. A deployment check cannot follow the same pattern of running entirely within the sandbox.

### Network Architecture

The egg system uses a dual-network topology:
- `egg-isolated` (172.32.0.0/24, `internal: true`) — Sandbox containers live here, routed through gateway proxy
- `egg-external` (172.33.0.0/24) — Gateway and orchestrator's external-facing network

The sandbox (172.32.0.10) can only reach the gateway (172.32.0.2) and orchestrator (172.32.0.3) on the isolated network. All external traffic is filtered through the gateway's Squid proxy.

### Orchestrator Capabilities

The orchestrator (`orchestrator/`) already has:
- Docker socket access (`/var/run/docker.sock` mounted)
- `DockerClient` wrapper (`docker_client.py`) for container lifecycle management
- `ContainerSpawner` (`container_spawner.py`) for creating sandbox containers with gateway integration
- Pipeline state management (`state_store.py`) and phase transition logic (`routes/phases.py`)
- Worktree management — per-pipeline worktrees at `/home/egg/.egg-worktrees/{pipeline-id}/{repo-name}/`

The orchestrator does **not** currently have:
- Docker Compose orchestration capability
- Devserver lifecycle management
- Any check execution logic (checks run inside the sandbox)

### Prerequisite: #644 (Commit-Level File Restrictions)

Issue #644 extends phase file restrictions from push-time to commit-time enforcement. This ensures `HEAD` in the agent's worktree is trustworthy for protected paths (compose files, Dockerfiles, init scripts). Without this, an agent could modify `docker-compose.yml` locally without pushing, and the orchestrator would read the tampered version.

**Status:** #644 is open. The existing `phase_filter.py` has phase-based file restriction infrastructure (`PhaseFileRestriction`), but enforcement currently only triggers on `git push` through the gateway.

## Constraints

1. **Sandbox never gets Docker socket** — The sandbox is untrusted. All Docker operations must go through the orchestrator.
2. **Agent code executes in devserver containers** — The agent's source files are mounted into service containers. Agent code runs with whatever access those containers have. Network isolation is the primary mitigation.
3. **Compose config must come from committed state** — The orchestrator reads compose files via `git show HEAD:...`, not from the working tree. Depends on #644 to make `HEAD` trustworthy.
4. **No cloud credentials in devserver containers** — All cloud services must be emulated locally (databases, caches, object storage, queues). Services use hardcoded dev defaults.
5. **Pre-built images only** — Egg does not build from source. Target applications must provide pre-built container images. The orchestrator pulls and runs them with agent code mounted in.
6. **Startup time budget** — A full backend stack may take 60-90s for health checks to pass. This is the floor for check phase duration.
7. **Target application must opt in** — Must provide docker-compose devserver with health checks, pre-built images, and local emulators.

## Options Considered

### Option A: Orchestrator-Driven DinD (Recommended)

The orchestrator manages the entire devserver lifecycle. The sandbox only makes HTTP requests to running services.

**Flow:**
1. Orchestrator determines which services are affected by agent's changes (file-to-service mapping)
2. Orchestrator extracts compose files from committed state (`git show HEAD:<compose-path>`)
3. Orchestrator writes compose files to a temp directory outside the agent's worktree
4. Orchestrator generates an override compose file adding read-only mounts for agent-modified source
5. Orchestrator creates `egg-check` network (`internal: true`, no gateway, no DNS)
6. Orchestrator runs `docker compose up` from the temp directory
7. Orchestrator attaches the sandbox to the `egg-check` network (scoped to service under test)
8. Sandbox runs validation: health checks, API queries, HTTP smoke tests
9. Orchestrator tears down the stack and removes the network

**Security layers:**
| Layer | Protection |
|---|---|
| Network | `egg-check` bridge is `internal: true` — no default gateway, no DNS, no route to internet |
| Inter-container | Restrict traffic to only required paths (per-service scoped networks or iptables) |
| Credentials | No cloud credentials in devserver containers; all cloud services emulated locally |
| Compose config | Read from committed state, not working tree (depends on #644) |
| Code mount | Agent source mounted read-only into specific service containers |
| Ephemeral | Entire stack torn down after checks; no persistent state survives |
| Resource limits | CPU/memory/PID caps on every devserver container; hard time limit with forced teardown |
| No capabilities | Devserver containers run unprivileged, no Docker socket, default seccomp profile |

**Pros:**
- Clean separation: orchestrator manages Docker, sandbox validates HTTP responses
- Same trust model as today — orchestrator is trusted, sandbox is not
- Agent code in devserver containers is fully air-gapped (no egress)
- Extends naturally from existing orchestrator container management capabilities

**Cons:**
- Adds significant complexity to orchestrator (compose orchestration, network management, service mapping)
- Splits the "check" across two trust domains (orchestrator sets up, sandbox validates)
- Startup latency (60-90s) extends check phase duration
- Requires target applications to provide pre-built images and local emulators

### Option B: Orchestrator-Only Validation (No Sandbox Involvement)

The orchestrator both manages the devserver AND runs validation scripts against it, without involving the sandbox at all.

**Flow:** Orchestrator brings up the stack, runs health checks and smoke tests from the orchestrator container itself, then tears down.

**Pros:**
- Simpler — no cross-container coordination between orchestrator and sandbox
- Orchestrator already has Docker access and network connectivity

**Cons:**
- Validation logic runs in the trusted orchestrator, not the untrusted sandbox
- If validation scripts have bugs, they could affect orchestrator stability
- Loses the principle that untrusted operations run in the sandbox
- Agent-controlled HTTP responses are consumed by the trusted orchestrator — larger blast radius

**Verdict:** Rejected. Violates the trust model. Validation should run in the sandbox where blast radius is bounded.

### Option C: Sidecar Validation Container

Instead of using the existing sandbox, spawn a dedicated lightweight validation container that only has HTTP client tools (curl, python requests) and attach it to the `egg-check` network.

**Pros:**
- Clean separation — validation container is purpose-built for deployment checks
- Can be even more locked down than the general sandbox
- No interference with the agent's main sandbox

**Cons:**
- Yet another container type to manage
- Duplicates sandbox infrastructure (gateway session, etc.) for minimal benefit
- The existing sandbox already provides the needed isolation

**Verdict:** Not recommended for v1. Could be considered later if the shared-sandbox approach causes interference.

## Recommended Approach: Option A (Orchestrator-Driven DinD)

### Architecture

```
Orchestrator (trusted)
├── Extracts compose from committed state
├── Generates override compose with RO agent code mounts
├── Creates egg-check network (internal: true)
├── Runs docker compose up
├── Attaches sandbox to egg-check network
└── Tears down after checker exits

Sandbox (untrusted, checker)
├── Already on egg-isolated network
├── Also attached to egg-check network
├── Runs HTTP health checks, API queries
├── Parses responses defensively
└── Returns CheckResult (PASS/FAIL/SKIP)
```

### Implementation Components

#### 1. Target Application Configuration

Target applications opt in by providing a deployment validation config (committed to their repo, not modifiable by the agent):

- `docker-compose.yml` (or similar) defining the devserver stack
- Health check endpoints on all services
- Pre-built container images available in a registry
- Local emulators for cloud dependencies
- A service mapping file indicating which source directories map to which services

#### 2. Orchestrator: Devserver Lifecycle Manager

New orchestrator module (`orchestrator/devserver.py` or similar) responsible for:

- **Compose extraction**: Read compose files from committed state via `git show HEAD:<path>`
- **Override generation**: Create a compose override that adds read-only volume mounts for agent-modified source files into the appropriate service containers
- **Network creation**: Create `egg-check` Docker network (`internal: true`, no default gateway)
- **Stack management**: `docker compose up -d`, wait for health checks, attach sandbox, then `docker compose down`
- **Service mapping**: Determine which services need agent code based on changed files
- **Resource enforcement**: Apply CPU/memory/PID limits and a hard time cap
- **Image pre-pull**: Pre-pull images to reduce startup latency (can be done nightly or on pipeline start)

#### 3. Orchestrator: API Endpoint

New endpoint for sandbox to trigger and interact with deployment validation:

- `POST /api/v1/pipelines/{id}/deployment-check/start` — Orchestrator starts devserver, returns service endpoints
- `GET /api/v1/pipelines/{id}/deployment-check/status` — Check devserver health status
- `POST /api/v1/pipelines/{id}/deployment-check/teardown` — Signal orchestrator to tear down

Alternatively, the orchestrator could manage the full lifecycle triggered by phase start, with the sandbox just performing HTTP validation against the running services.

#### 4. Check Runner: DeploymentCheck

New check in `.github/scripts/checks/deployment_check.py`:

```python
CheckDefinition(
    id="check-deployment",
    name="Deployment Validation",
    script="deployment_check.py",
    required=False,  # Start as optional, promote when stable
    retry_on_fail=True,
    max_retries=1,
)
```

The `DeploymentCheck` runner is unique because the orchestrator manages infrastructure while the sandbox runs validation. The check runner:

1. Signals orchestrator to start the devserver (or discovers it's already running)
2. Waits for services to become healthy (polling orchestrator status endpoint)
3. Runs HTTP health checks against service endpoints
4. Runs smoke tests (configurable per target application)
5. Parses responses defensively (handle malformed JSON, enforce max response sizes)
6. Returns `CheckResult`

#### 5. Network Isolation: egg-check Network

A new Docker bridge network, separate from `egg-isolated` and `egg-external`:

- `internal: true` — No default gateway, no DNS, no route to internet
- Contains: devserver service containers + sandbox (checker)
- Does NOT contain: gateway, orchestrator (orchestrator manages it from outside)
- Inter-container traffic restricted to only required paths (sandbox → service under test, service → emulators)

The sandbox gets attached to both `egg-isolated` (existing, for gateway/orchestrator communication) and `egg-check` (new, for devserver access).

#### 6. Inter-Container Isolation

Beyond the `internal: true` network flag, restrict lateral movement within the devserver stack:

- **Option 6a**: Per-service scoped networks — Each service on its own bridge, with the checker only having access to the service(s) under test
- **Option 6b**: Iptables rules — Orchestrator injects iptables rules after stack creation to restrict inter-container traffic
- **Option 6c**: Separate bridge networks per service boundary — Services grouped by trust level

Recommendation: Start with Option 6a (per-service scoped networks) for simplicity. The compose override can define multiple networks to isolate service boundaries.

### Phasing

**Phase 1: Foundation (depends on #644)**
- Implement commit-level file restrictions (#644) so compose files are trustworthy at `HEAD`
- Define target application configuration schema (compose requirements, health check spec, service mapping)
- Add `egg-check` network definition to constants

**Phase 2: Orchestrator Devserver Manager**
- Compose extraction from committed state
- Override generation with read-only agent code mounts
- Network creation and management
- Stack lifecycle (up, health wait, teardown)
- Resource limits and hard time cap

**Phase 3: Check Integration**
- API endpoints for sandbox-orchestrator coordination
- `DeploymentCheck` check runner
- Add `check-deployment` to phase defaults (optional initially)
- Defensive response parsing in checker

**Phase 4: Security Hardening**
- Inter-container network isolation (per-service scoped networks)
- Image pre-pull mechanism
- Seccomp profile for devserver containers
- Validate that services start without cloud credentials

**Phase 5: Validation and Rollout**
- End-to-end test with a real application change
- Performance benchmarking (startup time, total check duration)
- Promote `check-deployment` from optional to required once stable

### Residual Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Agent code interacts with emulated infrastructure (DB, cache) | Low | Intended behavior; ephemeral and air-gapped |
| Agent code returns crafted HTTP responses to checker | Low | Checker runs in sandbox; blast radius bounded |
| Agent code corrupts ephemeral environment | Low | Stack torn down after every run; no persistent state |
| Container runtime exploits | Low | Same risk as `make test`; unprivileged + seccomp |
| Startup latency extends check phase | Medium | Pre-pull images; start devserver in parallel with other checks |
| Target application has insufficient health checks | Medium | Require health check endpoints as prerequisite; fail-open (SKIP) if not configured |

### Open Questions

1. **Service mapping format**: How does the orchestrator determine which changed files map to which devserver services? Should this be a config file in the target repo, or inferred from directory structure?
2. **Checker test definitions**: How are the HTTP smoke tests defined? Hardcoded per-service health checks, or a configurable test suite per target application?
3. **Parallel execution**: Should the devserver start in parallel with lint/test checks, or sequentially after they pass? Parallel reduces total time but wastes resources if lint/test fail.
4. **Image registry access**: In private mode, the proxy blocks all external traffic. How does the orchestrator pull pre-built images? Does it need registry access on the external network?
5. **Multiple target repos**: If egg manages changes across multiple repositories, does each get its own devserver, or is there a combined stack?

## Dependencies

- **#644** (Enforce phase file restrictions on local commits) — Hard dependency. Without this, compose files in the worktree are not trustworthy. Must be implemented first.
- **Target application prerequisites** — Docker-compose devserver, pre-built images, local emulators, health check endpoints. This is a per-application onboarding requirement.
- **Orchestrator Docker Compose support** — Currently absent. The orchestrator manages individual containers but has no compose orchestration. This is the largest new capability to build.

## Recommendation

Proceed with Option A (Orchestrator-Driven DinD) using the phased approach above. The implementation naturally extends the existing trust model (orchestrator=trusted, sandbox=untrusted) and leverages existing infrastructure (Docker client, network architecture, check framework).

Start with #644 as the prerequisite, then build the orchestrator devserver manager as the foundational component. The check runner and network isolation can follow incrementally.
