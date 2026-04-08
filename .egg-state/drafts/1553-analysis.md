# Analysis: Migrate to Kubernetes

> Issue: #1553 | Phase: refine

## Problem Statement

The current egg platform runs orchestrator and gateway as Docker Compose services, with the orchestrator spawning agent sandbox containers via the Docker SDK (`docker-py`). This architecture has scalability limitations:

- **Single-host bound**: The Docker socket is local — all containers run on one machine.
- **No native scheduling**: Container placement, resource quotas, and auto-scaling are manual.
- **No fault tolerance**: If the host goes down, all agents die with no automatic recovery.
- **Resource contention**: Multiple concurrent agent containers compete for host resources without proper scheduling.

The desired outcome is a Kubernetes-based deployment where orchestrator and gateway run as k8s Services (Deployments), and agent containers are spawned as k8s Jobs. k3s is the chosen distribution for local development due to its lightweight footprint.

## Current Behavior

### Architecture Overview

The system currently uses a three-tier Docker architecture:

1. **Docker Compose stack** (`docker-compose.yml`) runs gateway and orchestrator as long-lived services on two Docker networks:
   - `egg-isolated` (172.32.0.0/24, internal-only) — sandbox-to-gateway traffic
   - `egg-external` (172.33.0.0/24, bridged) — public internet access via gateway proxy

2. **`DockerClient`** (`orchestrator/docker_client.py`, ~535 lines) wraps the Docker SDK with:
   - Container create/start/stop/remove lifecycle
   - Label-based container listing (`egg.pipeline.id`, `egg.agent.role`)
   - Container ID validation, log retrieval, wait-for-exit
   - Orphaned container cleanup (>24h)
   - Singleton pattern via `get_docker_client()`

3. **`ContainerSpawner`** (`orchestrator/container_spawner.py`, ~988 lines) orchestrates the full lifecycle:
   - Gateway session registration before container start
   - Per-agent worktree creation (via gateway API)
   - Repository volume mounts with `.git` shadow binding
   - Phase-based readonly mount enforcement
   - Dual-network attachment with static/dynamic IP allocation
   - Environment variable injection (30+ variables per agent)
   - Post-exit uncommitted change detection

4. **`ContainerMonitor`** (`orchestrator/container_monitor.py`) provides:
   - Background health polling (default 10s interval)
   - Event-driven state change callbacks (STARTED, STOPPED, EXITED, FAILED, UNHEALTHY)
   - Orphaned container cleanup

5. **`DevserverManager`** (`orchestrator/devserver.py`) manages Docker-in-Docker devserver validation stacks.

### Key Integration Points

| Consumer | Method | Purpose |
|----------|--------|---------|
| `routes/pipelines.py` | `spawner.spawn_agent_container()` | Phase execution |
| `routes/pipelines.py` | `spawner.spawn_overseer_container()` | Health monitoring agent |
| `routes/pipelines.py` | `spawner.cleanup_pipeline()` | Pipeline teardown |
| `routes/containers.py` | `docker_client.*` | REST API for container ops |
| `concurrent_executor.py` | `spawner.create_concurrent_spawn_fn()` | Parallel agent spawning |
| `sandbox/egg_lib/runtime.py` | Direct `docker run` subprocess | CLI interactive sessions |

### Container Configuration

Each agent container receives:
- **Volumes**: Repo bind mounts, .git shadow mounts, certs volume, worktree mounts, phase-readonly mounts
- **Networks**: Attached to either isolated or external network with allocated IP
- **Environment**: ~30+ variables (EGG_PIPELINE_ID, EGG_AGENT_ROLE, GATEWAY_URL, session tokens, proxy config, etc.)
- **Labels**: Pipeline ID, agent role, creation timestamp for filtering
- **Resource limits** (devserver only): 1 CPU, 512MB RAM, 256 PIDs

### Docker Compose Services

**Production stack** (`docker-compose.yml`, 199 lines):
- Gateway: dual-homed on both networks, ports 9848/3129/9851, health checks
- Orchestrator: dual-homed, ports 9849/9850, Docker socket mount, depends on gateway

**Integration test stacks**:
- `integration_tests/docker-compose.yml` — Gateway-only for gateway tests (test subnets 172.40.x/172.41.x)
- `integration_tests/local_pipeline/docker-compose.yml` — Full stack with mock sandbox

### Images Built

- `sandbox/Dockerfile` (315 lines) — Ubuntu 22.04, Python 3.13, Claude SDK, dev tools
- `orchestrator/Dockerfile` (50 lines) — python:3.13-slim, Flask API
- `gateway/Dockerfile` (102 lines) — python:3.13-slim, Flask + Squid proxy

## Constraints

### Technical Constraints

- **Network isolation is security-critical**: The isolated network prevents sandboxes from reaching the internet directly — all traffic must route through the gateway's Squid proxy. This isolation model must be preserved in Kubernetes.
- **Per-agent worktree isolation**: Each agent gets its own filesystem worktree (since #1481). Kubernetes must support shared filesystem access between gateway (which manages worktrees) and agent pods.
- **Gateway session binding**: Each agent container is bound to a gateway session with a specific IP and session token. The gateway validates requests by source IP. Kubernetes changes how pod IPs are allocated and this binding model needs rethinking.
- **Docker socket dependency**: The orchestrator currently needs the Docker socket to spawn containers. In k8s, it needs k8s API access (ServiceAccount with RBAC permissions) instead.
- **CI compatibility**: GitHub Actions CI currently builds images with `docker build` and runs integration tests with `docker-compose`. The CI pipeline needs to work with k3s.
- **`egg` CLI local usage**: The `egg` CLI (`sandbox/egg_lib/runtime.py`) also spawns containers via direct `docker run`. This path also needs migration or an alternative local dev experience.
- **Image registry**: k8s Jobs need to pull images from a registry. Currently images are built locally and available on the Docker daemon. k3s can import local images, but a registry strategy is needed.

### Business Constraints

- **Backward compatibility**: The `egg` CLI is used by developers locally. Requiring k3s for local `egg` usage is a significant UX change.
- **Migration scope**: This is a large architectural change touching orchestrator core, deployment scripts, CI pipelines, integration tests, and the CLI.
- **Production readiness**: The current Docker Compose setup works reliably. The migration should not regress reliability.

### Dependencies

- **`kubernetes` Python client library** — New dependency needed for the orchestrator to interact with the k8s API
- **k3s** — For local development and CI environments
- **Container registry** — k8s needs to pull images from somewhere (local k3s import, or a registry like GHCR)
- **CNI plugin** — For NetworkPolicy enforcement (k3s bundles Flannel by default, which does NOT support NetworkPolicies; Calico or Cilium needed)

## Options Considered

### Option A: Full Cutover — Replace Docker with Kubernetes Entirely

**Approach**: Remove all Docker Compose files and Docker client code. Replace with k8s manifests/Helm charts, a `KubernetesClient` replacing `DockerClient`, and a `KubernetesSpawner` replacing `ContainerSpawner`. Local dev and CI use k3s.

**Pros**:
- Clean architecture — one deployment model, no conditional paths
- Matches issue's stated scope ("remove Docker workflow entirely")
- Simpler long-term maintenance
- Enables multi-node scaling naturally

**Cons**:
- Big-bang migration — high risk of regression
- k3s is a heavier local dev prerequisite than Docker alone
- All integration tests must be rewritten simultaneously
- The `egg` CLI experience changes significantly (users need k3s running)
- No rollback path if issues are found post-migration

### Option B: Abstraction Layer with Dual Backend (Docker + Kubernetes)

**Approach**: Introduce a `ContainerBackend` interface that both `DockerBackend` and `KubernetesBackend` implement. The orchestrator selects the backend based on configuration. Existing Docker code stays working while k8s support is added incrementally.

**Pros**:
- Incremental migration — lower risk per step
- Docker path remains available for simple local dev
- Can be tested side-by-side
- Rollback is trivial (switch config back to Docker)
- Developers without k8s can still use `egg` CLI via Docker backend

**Cons**:
- More code to maintain (two backends)
- Abstraction layer must be general enough for both paradigms (volumes vs PVCs, networks vs NetworkPolicies)
- Risk of the Docker backend never being removed ("temporary" becomes permanent)
- Some concepts don't map cleanly (Docker IP allocation vs k8s pod IPs)

### Option C: Kubernetes Only for Orchestrator/Gateway, Keep Docker for Agent Spawning

**Approach**: Deploy orchestrator and gateway as k8s Deployments/Services, but keep spawning agent containers via Docker (the k8s node's Docker/containerd socket). This is a partial migration.

**Pros**:
- Orchestrator/gateway get k8s benefits (scaling, health, rolling updates)
- Agent spawning logic changes minimally
- Smallest scope of change

**Cons**:
- Doesn't solve the core scalability problem (agents still run on one node)
- Docker socket access from within a k8s pod is a security concern
- Hybrid model is confusing operationally
- Doesn't match the issue's stated goal

## Recommended Approach

**Option A: Full Cutover** is recommended, aligned with the issue's explicitly stated scope. The issue specifically calls for removing Docker Compose, docker_client.py, and container_spawner.py entirely.

However, the implementation should be structured to minimize risk:

1. **Define clean interfaces first** — Create `ContainerBackend` protocol/ABC even for the cutover. This makes the code testable and leaves the door open if Docker support is ever needed again.
2. **k3s setup tooling** — Provide a `make k3s-setup` target that installs k3s and imports local images, minimizing developer friction.
3. **Migrate tests incrementally** — Unit tests can use mocked k8s client. Integration tests can use k3s in CI.

### Key Architecture Decisions for Kubernetes

**Agent containers as k8s Jobs** (not Deployments):
- Jobs are the right primitive — agents run to completion, exit codes matter
- `activeDeadlineSeconds` replaces the current timeout mechanism
- `backoffLimit: 0` prevents unwanted restarts (orchestrator manages retries)

**Network isolation via NetworkPolicies**:
- Requires a CNI that supports NetworkPolicies (Calico or Cilium, not default Flannel in k3s)
- Default-deny policy on agent namespace, allow only egress to gateway service
- Gateway service acts as the single egress point (same as current Squid proxy model)

**Storage via PersistentVolumeClaims**:
- Worktrees: ReadWriteMany PVC shared between gateway and agent pods (or hostPath for k3s local dev)
- State: PVC mounted by orchestrator
- Certs: Shared volume (Secret or PVC) for gateway CA certificates

**Gateway session binding**:
- Replace IP-based binding with k8s pod name/UID-based binding
- Gateway can validate via k8s downward API (pod name in environment) or token-only auth

## Open Questions

> **Note**: The `egg-contract` CLI is unable to register decisions due to a gateway session configuration issue (role not propagated to session). The decisions and feedback questions below need to be registered once this is resolved, or answered directly on the issue.

### Decisions Needed

**Decision 1: Kubernetes manifest approach**
Which approach should be used for deploying orchestrator, gateway, and agent Jobs?
- [ ] Raw YAML manifests (simpler, no tooling dependency)
- [ ] Helm charts (templated, parameterized, standard ecosystem tool)
- [ ] Kustomize overlays (YAML-native, no templating language, built into kubectl)
- [ ] Other (explain in reply)

**Decision 2: Network isolation implementation**
How should network isolation (currently Docker networks with isolated/external) be implemented in Kubernetes?
- [ ] Kubernetes NetworkPolicies with Calico CNI (native k8s, widely supported)
- [ ] Kubernetes NetworkPolicies with Cilium CNI (eBPF-based, more features, heavier)
- [ ] Separate namespaces with NetworkPolicies (stronger isolation boundary)
- [ ] Other (explain in reply)

**Decision 3: Persistent storage strategy**
How should persistent storage (worktrees, state, certs) be handled?
- [ ] hostPath volumes (simplest for k3s local dev, not portable to multi-node)
- [ ] PersistentVolumeClaims with k3s local-path provisioner (semi-portable)
- [ ] NFS or shared filesystem PVCs (fully portable, more infrastructure)
- [ ] Other (explain in reply)

**Decision 4: Migration strategy**
Should both Docker and Kubernetes backends be supported during transition?
- [ ] Complete cutover — remove Docker entirely, replace with k8s
- [ ] Dual-backend during transition — keep Docker working while adding k8s
- [ ] Other (explain in reply)

**Decision 5: `egg` CLI local experience**
How should the `egg` CLI work for local developers after the migration?
- [ ] Require k3s for all local usage (via `make k3s-setup`)
- [ ] Keep Docker-based `egg` CLI for interactive use, k8s for SDLC pipelines only
- [ ] Other (explain in reply)

**Decision 6: Gateway IP-based session binding**
The gateway currently binds sessions to container IPs. How should this change?
- [ ] Switch to token-only authentication (remove IP binding entirely)
- [ ] Use k8s pod names/UIDs instead of IPs
- [ ] Keep IP-based binding (pod IPs are stable for the pod's lifetime in k8s)
- [ ] Other (explain in reply)

### Feedback Needed

1. **What is the target deployment environment?** Is this intended for local dev only (k3s), or also for cloud k8s (EKS/GKE/AKS)? This affects storage, networking, and registry decisions significantly.

2. **What is the image registry strategy?** Currently images are built locally. Should we push to GHCR and pull in k8s, use k3s's local image import (`k3s ctr images import`), or set up a local registry?

3. **Are there resource limits/quotas for agent pods?** The current setup has no resource limits on agent containers (only devserver has limits). Should agent Jobs have CPU/memory limits in k8s?

4. **Should the DevserverManager also be migrated?** It currently uses Docker Compose to spin up validation stacks. Migrating it to k8s adds significant scope.

5. **What is the timeline expectation?** This is a major architectural change. Is there a deadline, or is incremental delivery acceptable?

6. **How should the CI pipeline change?** GitHub Actions currently uses Docker natively. k3s in CI requires either a k3s installation step in the workflow or a different CI approach (e.g., kind for CI, k3s for local dev).

## Complexity Assessment

**High** — This is a fundamental architectural change affecting:
- Core container lifecycle management (docker_client.py, container_spawner.py, container_monitor.py)
- Deployment infrastructure (docker-compose.yml files, all k8s manifests)
- Network isolation model (Docker networks → NetworkPolicies)
- Storage model (Docker volumes → PVCs)
- Integration test infrastructure (3 docker-compose files)
- CI/CD pipeline (GitHub Actions workflows)
- CLI tooling (egg, egg-deploy)
- Developer experience (Docker → k3s prerequisite)

Multiple independent workstreams (manifests, k8s client, network policies, storage, CI, CLI, tests) suggest this should be planned as a parallelizable multi-phase effort.

---

*Authored-by: egg*
