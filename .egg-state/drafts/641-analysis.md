# Analysis: Docker-in-Docker Support for Deployment Validation (#641)

## Problem Statement

The egg check phase currently runs static validations only: linting, unit tests,
and merge conflict detection. For webapp changes, these checks cannot verify that
services actually start, respond to HTTP requests, execute GraphQL queries, or
integrate correctly. The webapp devserver is fully Dockerized (docker-compose
with ~25 microservices), making it feasible to run it inside the egg sandbox for
end-to-end deployment validation.

The goal is to let the check phase spin up the webapp devserver stack via
Docker-in-Docker, then have the agent run intelligent validation (health checks,
HTTP requests, GraphQL queries) against the live services before marking the
implementation phase as passing.

## Current Architecture

### Check System

Checks are defined as `CheckDefinition` objects in `phase_defaults.py` and
executed by `CheckRunner` subclasses registered in `run_check.py`. However, the
implement phase checker doesn't use this registry — it runs as a **separate
agent container** spawned by the orchestrator (`pipelines.py:2040-2127`):

1. Orchestrator builds a checker prompt via `_build_checker_prompt()`
2. A sandbox container with `AgentRole.CHECKER` is spawned
3. The agent discovers/runs test and lint commands inside the container
4. Results are written to `.egg-state/checks/implement-results.json`
5. If checks fail, an autofixer agent runs (up to 3 cycles)

This agent-based check execution is the integration point for deployment
validation — it's where the agent has full sandbox access and can run arbitrary
commands.

### Container Spawning

`ContainerSpawner` (`orchestrator/container_spawner.py`) creates sandbox
containers via docker-py with:
- Gateway session registration for git/gh access
- Network attachment to `egg-isolated` or `egg-external`
- `.git` shadow mounts (forces git through gateway)
- Environment injection (`GATEWAY_URL`, `EGG_SESSION_TOKEN`, proxy vars)
- Shared `build_sandbox_config()` from `egg_container/__init__.py`

The orchestrator container has Docker socket access
(`/var/run/docker.sock:/var/run/docker.sock`), which is how it spawns sandbox
containers. Sandbox containers themselves do **not** have Docker socket access.

### Network Architecture

- `egg-isolated` (172.32.0.0/24): Internal only, no external route. Sandbox
  containers must use gateway proxy (port 3129) for internet access.
- `egg-external` (172.33.0.0/24): Has internet access. Gateway is dual-homed.
- Gateway at 172.32.0.2 (isolated) / 172.33.0.2 (external)
- Orchestrator at 172.32.0.3 (isolated) / 172.33.0.3 (external)

Sandbox containers connect to one network based on mode (private → isolated,
public → external).

### Credential Flow

Sandbox containers never see GitHub tokens or GCP credentials directly. The
gateway holds credentials and injects them via session tokens. For Docker image
pulls from artifact registry (`us-central1-docker.pkg.dev`), the sandbox
currently has no mechanism to authenticate — this is a new requirement.

## Key Technical Challenges

### 1. Docker Access from Sandbox

Today sandbox containers cannot run Docker commands. The Docker socket is only
mounted in the orchestrator. There are two approaches:

**A. Docker Socket Mount (DinD via socket sharing)**
Mount `/var/run/docker.sock` into the sandbox container. The agent can then run
`docker compose up` directly.

- **Pro**: Simple, fast, no additional containers needed
- **Con**: Sandbox container gains host Docker access (security boundary
  weakened). All containers started by the sandbox run on the host Docker
  daemon, visible to other containers and the host.

**B. DinD Sidecar (`docker:dind`)**
Spawn a `docker:dind` container alongside the sandbox. The sandbox connects to
the DinD daemon instead of the host.

- **Pro**: True isolation — webapp containers run inside the DinD daemon, not
  on the host. Crash/cleanup is simpler (stop DinD = stop everything).
- **Con**: Performance overhead (nested Docker), more complex networking
  (sandbox must reach DinD's network), additional resource usage.

### 2. Image Pull Authentication

Webapp images are hosted on `us-central1-docker.pkg.dev/khan-internal-services/`.
Pulling requires GCP Application Default Credentials (ADC). The current
credential flow doesn't support Docker registry auth. Options:

- **Gateway proxy extension**: Gateway authenticates Docker pulls on behalf of
  sandbox (complex — requires implementing a Docker registry proxy)
- **ADC mount**: Mount host ADC credentials read-only into the sandbox or DinD
  container (simpler, but exposes credentials)
- **Pre-pull images**: Orchestrator pre-pulls images before spawning the
  sandbox (images are available on host Docker, sandbox just uses them)

### 3. Network Connectivity

Webapp containers need to be reachable from the sandbox agent for health checks
and HTTP requests. Options:

- **Shared network**: Attach webapp containers to `egg-isolated` or a new
  `egg-check` bridge network that the sandbox is also on
- **Host networking**: Use host network mode for webapp stack (port conflicts
  possible)
- **DinD internal network**: If using DinD sidecar, webapp containers share
  the DinD network. Sandbox reaches them via DinD's exposed ports.

Webapp containers themselves don't need internet access (all dependencies are
emulated locally: fake GCS, Pub/Sub emulator, Datastore emulator, etc.). Only
image pulling needs external access.

### 4. Resource and Timeout Constraints

The full backend stack is ~25 containers. Health checks take 60-90 seconds
before services respond. Current checker timeout is 1800s (30 min), which is
sufficient, but resource usage (CPU, memory, disk) for 25+ containers is
significant. The egg sandbox host must have enough resources.

### 5. Cleanup and Failure Modes

If the sandbox crashes or the check times out, all webapp containers must be
cleaned up. This is critical to avoid orphaned containers consuming resources.

## Implementation Approaches

### Approach A: Docker Socket Mount + Orchestrator Pre-pull (Recommended)

Mount the Docker socket into checker sandbox containers when deployment
validation is enabled. Have the orchestrator pre-pull webapp images before
spawning the checker. Use a dedicated Docker network for webapp containers
that the sandbox is also attached to.

**Architecture:**
```
Host Docker Daemon
├── egg-gateway (172.32.0.2)
├── egg-orchestrator (172.32.0.3)
│   └── [pre-pulls webapp images]
├── egg-{pipeline}-checker (sandbox)
│   ├── egg-isolated network (gateway access)
│   └── egg-check-{pipeline} network (webapp access)
│       ├── postgres
│       ├── redis
│       ├── graphql-gateway
│       ├── users
│       └── ... (webapp services)
```

**Flow:**
1. Orchestrator detects deployment check is configured for this repo
2. Orchestrator pre-pulls required webapp images (has Docker socket + can
   authenticate via gateway)
3. Orchestrator spawns checker container with Docker socket mounted
4. Checker agent creates an `egg-check-{pipeline}` Docker network
5. Checker runs `docker compose up` with webapp stack attached to that network
6. Agent waits for health checks, runs validation, tears down stack
7. On exit (success or failure), cleanup removes the check network and all
   webapp containers

**Changes required:**
- `container_spawner.py`: Add `docker_socket` parameter to
  `spawn_agent_container()` — when true, mount `/var/run/docker.sock`
- `pipelines.py`: Add deployment check logic after (or integrated with)
  existing checker loop. Pre-pull images before spawning checker.
- New `deployment_check.py`: Script or prompt builder for deployment validation
- `phase_defaults.py`: Add `check-deployment` CheckDefinition (optional,
  implement phase)
- `docker-compose.yml` / orchestrator env: Pass artifact registry credentials
  for image pulling

**Pros:**
- Simplest implementation — no DinD complexity
- Fastest startup — no nested Docker overhead
- Pre-pull eliminates credential exposure in sandbox
- Dedicated per-pipeline network prevents cross-contamination

**Cons:**
- Sandbox gets Docker socket access (must scope carefully — only checker role)
- Webapp containers visible on host Docker (but namespaced by pipeline ID)
- Cleanup must be robust to prevent orphaned containers

### Approach B: DinD Sidecar with Orchestrator Management

Spawn a `docker:dind` container as part of the check phase. The sandbox
connects to it via `DOCKER_HOST=tcp://dind:2376`. Webapp containers run
inside the DinD daemon.

**Flow:**
1. Orchestrator spawns DinD sidecar on shared network with sandbox
2. Orchestrator loads images into DinD (via `docker save | docker load`)
3. Sandbox runs `docker compose up` against DinD daemon
4. Validation runs normally
5. Stopping DinD automatically kills all webapp containers

**Pros:**
- True isolation — no host Docker access from sandbox
- Clean cleanup — stop DinD = stop everything
- No risk of host container namespace pollution

**Cons:**
- Significant complexity: DinD startup, image transfer, networking
- Performance overhead: nested Docker adds latency and resource usage
- Image transfer is slow (`docker save/load` for 25+ images)
- Networking between sandbox and DinD-hosted containers is complex

### Approach C: Orchestrator-Managed Stack (No Sandbox Docker Access)

The orchestrator itself starts the webapp stack (since it has Docker socket
access) and the sandbox checker only runs HTTP validation against it.

**Flow:**
1. Orchestrator starts webapp stack directly via docker-compose
2. Orchestrator attaches webapp containers to a network reachable by sandbox
3. Sandbox checker agent only runs HTTP requests / GraphQL queries
4. Orchestrator tears down stack after checker completes

**Pros:**
- Sandbox never gets Docker access
- Strongest security boundary
- Orchestrator already has Docker management infrastructure

**Cons:**
- Splits responsibility — orchestrator manages lifecycle, agent validates
- Agent cannot adjust stack configuration based on what it finds
- More orchestrator code (docker-compose management, health waiting)
- Less flexible — agent can't restart individual services or debug

## Recommendation

**Approach A (Docker Socket Mount + Orchestrator Pre-pull)** is recommended.

Rationale:
1. **Simplicity**: Minimal new infrastructure. The checker agent already runs
   inside a sandbox container — adding Docker socket access is a single mount.
2. **Flexibility**: The agent can manage the full lifecycle — start, inspect,
   restart, debug — which is essential for intelligent validation.
3. **Pre-pull solves credentials**: By having the orchestrator pre-pull images
   (it already has Docker socket access and can be given registry auth), the
   sandbox never needs GCP credentials.
4. **Scoped risk**: Docker socket is only mounted for `AgentRole.CHECKER`
   containers during deployment checks. Other agent roles never get it.
5. **Per-pipeline isolation**: A dedicated `egg-check-{pipeline}` network
   ensures webapp containers from different pipelines don't interfere.

The security tradeoff (Docker socket in sandbox) is acceptable because:
- The checker agent is already trusted to run arbitrary commands
- Docker socket access is scoped to the check phase only
- Container labels and naming conventions enable cleanup
- The alternative (DinD) adds complexity without eliminating the trust boundary
  — the agent still runs arbitrary code either way

## Constraints and Dependencies

| Constraint | Impact |
|-----------|--------|
| Docker socket sharing | Checker gets host Docker access — scope to checker role only |
| Artifact registry auth | Orchestrator needs GCP ADC or service account for image pulls |
| Resource requirements | ~25 containers need significant CPU/memory (8GB+ recommended) |
| Startup time | 60-90s health check floor adds to check phase duration |
| Image freshness | Pre-built images updated nightly — may lag behind code changes |
| Webapp repo dependency | Deployment check only makes sense for repos with docker-compose stacks |
| Network isolation | Webapp containers must not route through egg gateway proxy |

## Files to Modify

| File | Change |
|------|--------|
| `orchestrator/container_spawner.py` | Add `docker_socket` param to `spawn_agent_container()` for Docker socket mount |
| `orchestrator/routes/pipelines.py` | Add deployment check orchestration: image pre-pull, checker prompt with deployment instructions, cleanup |
| `shared/egg_contracts/phase_defaults.py` | Add `check-deployment` to implement phase checks (optional) |
| `shared/egg_contracts/models.py` | Add deployment check fields to `CheckDefinition` if needed (e.g., `requires_docker`) |
| `.github/scripts/checks/run_check.py` | Register `deployment` check in `CHECK_REGISTRY` |
| `.github/scripts/checks/deployment_check.py` | New: `DeploymentCheck` runner implementation |
| `shared/egg_config/constants.py` | Add `EGG_CHECK_NETWORK_PREFIX` constant |
| `docker-compose.yml` | Add artifact registry credential volume for orchestrator |
| `orchestrator/Dockerfile` | Ensure `docker compose` CLI is available in orchestrator image |

## Testing Strategy

1. **Unit tests**: `DeploymentCheck` runner with mocked Docker client — verify
   network creation, compose up/down, health check polling, result reporting
2. **Integration test**: New test in `orchestrator/tests/` that spawns a
   minimal docker-compose stack (e.g., single nginx container) and verifies the
   full lifecycle (create network, compose up, health check, compose down,
   cleanup)
3. **Existing test updates**: Verify `spawn_agent_container()` with
   `docker_socket=True` produces correct mount configuration
4. **Phase defaults test**: Verify `check-deployment` appears in implement
   phase config and is marked optional
5. **Cleanup test**: Simulate crash mid-check and verify orphaned container
   cleanup works via pipeline cleanup

## Open Questions for Human Decision

1. **DinD method**: Socket mount (recommended) vs DinD sidecar? The security
   tradeoff of socket mounting is acceptable given the checker already runs
   arbitrary code, but this deserves explicit sign-off.

2. **Service subset**: Start with full backend stack or define a minimal
   subset? Full stack gives maximum validation coverage but costs more
   resources. A `minimal` mode (postgres + graphql-gateway + 2-3 core
   services) could be a useful first milestone.

3. **Required vs optional**: The issue suggests starting as optional
   (`required=False`). Agree — this allows gradual rollout and avoids blocking
   pipelines while the feature stabilizes.

4. **Credential source for image pulls**: Host ADC mount vs dedicated service
   account? Host ADC is simpler for laptop deployment; a service account is
   better for cloud deployment (Phase 3).

5. **Timeout budget**: The current 1800s checker timeout should accommodate
   deployment validation (60-90s startup + validation time). Should there be a
   separate, configurable timeout for deployment checks?
