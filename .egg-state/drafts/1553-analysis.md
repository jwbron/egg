# Analysis: Migrate to Kubernetes

> Issue: #1553 | Phase: refine

## Problem Statement

The egg platform currently uses a three-tier Docker architecture where the orchestrator spawns agent containers via the Docker SDK. This design binds all agents to a single host, provides no native scheduling or fault tolerance, and leaves resource contention unmanaged. The issue requests migrating to Kubernetes (using k3s for local development) to enable multi-node scaling, proper scheduling, and automatic recovery.

The desired outcome is a fully working egg pipeline on k3s where `make k3s-setup && make deploy` replaces the current Docker Compose workflow, with no Docker dependencies remaining.

## Current Behavior

### Container Lifecycle

The orchestrator manages agent containers through three core modules (~2,400 lines total):

| Module | Lines | Responsibility |
|--------|-------|----------------|
| `orchestrator/docker_client.py` | 534 | Docker SDK wrapper: container CRUD, label-based listing, log retrieval, orphan cleanup. Singleton via `get_docker_client()`. Custom exception hierarchy (`DockerClientError`, `ContainerNotFoundError`, etc.). |
| `orchestrator/container_spawner.py` | 988 | Full agent lifecycle: gateway session registration, per-agent worktree creation, repo volume mounts with `.git` shadow binding, phase-based readonly enforcement, dual-network attachment with static/dynamic IP allocation, 30+ env vars per agent, post-exit uncommitted change detection. |
| `orchestrator/container_monitor.py` | 884 | Background health polling (10s interval), event-driven state callbacks (STARTED/STOPPED/EXITED/FAILED/UNHEALTHY), orphan cleanup via reconciliation. |

Additional Docker-dependent code:
- `orchestrator/concurrent_executor.py` (457 lines) — multi-agent phase orchestration via `spawn_fn` callbacks
- `sandbox/egg_lib/runtime.py` (1,197 lines) — CLI-side container exec, session management, IP allocation via `build_sandbox_docker_cmd()`

### Networking

Two Docker networks provide isolation:
- **`egg-isolated`** (172.32.0.0/24, `internal: true`): Private mode — no external gateway, all traffic forced through Squid proxy on gateway
- **`egg-external`** (172.33.0.0/24, bridged): Public mode — direct internet via gateway proxy

Gateway sits on both networks at `.2`; orchestrator at `.3`. Agent containers get dynamic IPs in `.128-.254` range. This "fail-closed" design ensures agents cannot reach the internet without passing through gateway policy enforcement.

### Deployment Infrastructure

- **Docker Compose**: `docker-compose.yml` (199 lines) — gateway + orchestrator as long-lived services
- **Integration tests**: `integration_tests/docker-compose.yml` (76 lines) — test-only gateway (172.40.x/172.41.x subnets)
- **Local pipeline tests**: `integration_tests/local_pipeline/docker-compose.yml` (126 lines) — full stack with mock sandbox
- **Dockerfiles**: sandbox (315 lines), orchestrator (50 lines), gateway (102 lines)
- **CI workflows**: `test-integration.yml`, `test-e2e.yml` use `docker build` + `docker compose`; `release-images.yml` pushes to GHCR

### No Existing k8s Code

There are zero references to Kubernetes, k3s, kubectl, or kustomize in the codebase outside of the #1558 analysis (DevserverManager removal). This is a greenfield k8s migration.

## Constraints

### Technical
- **Network isolation is security-critical**: The current model physically prevents agents from bypassing the gateway. k8s NetworkPolicies must replicate this fail-closed behavior. Flannel (k3s default) does NOT support NetworkPolicies — Calico CNI is required.
- **Per-agent worktree isolation**: Each agent gets its own filesystem worktree (since #1481). Gateway manages worktree lifecycle. k8s must support shared filesystem access between gateway and agent pods.
- **Docker socket dependency**: Orchestrator uses `docker` Python SDK for container management → must be replaced with `kubernetes` Python client.
- **Gateway session binding**: Currently uses IP-based binding. Pod IPs are ephemeral in k8s — token-only auth is more appropriate.
- **CI compatibility**: GitHub Actions workflows use `docker build` + `docker-compose`. Must be converted to k3s-based test infra or a k3s setup step.
- **Image registry**: Currently builds locally. k3s can import via `k3s ctr images import` but GKE would need a remote registry (future work).
- **No existing abstractions**: `DockerClient` and `ContainerSpawner` have no interface/protocol — they are concrete classes used directly by routes and concurrent executor.

### Business
- **Scope**: Issue explicitly selects Option A (full cutover). No dual-backend support.
- **Local-only**: Target is k3s local development. GKE/cloud deployment is explicitly deferred.
- **DevserverManager**: Already removed in #1558 — no Docker Compose dependency from that subsystem.

### Dependencies
- `docker` Python SDK — to be fully removed
- `kubernetes` Python client — new dependency
- Calico CNI — required for NetworkPolicy support on k3s
- k3s — new local development prerequisite (replaces Docker Desktop/daemon)
- Kustomize — built into kubectl, no extra install

## Options Considered

### Option A: Full Cutover — Replace Docker with Kubernetes Entirely (Issue-Selected)

**Approach**: Remove all Docker Compose files and Docker client code. Replace with Kustomize manifests, `KubernetesClient` replacing `DockerClient`, and `KubernetesSpawner` replacing `ContainerSpawner`. Local dev and CI use k3s. Define a clean `ContainerBackend` protocol for testability even in the single-backend world.

**Pros**:
- Clean architecture with one deployment model
- No conditional paths or dual-backend maintenance burden
- Enables multi-node scaling, proper scheduling, fault tolerance
- k3s is lightweight (~100MB binary) and well-suited for local dev
- Kustomize overlays provide a clean path to future GKE deployment
- Protocol/interface enables easy mocking for tests

**Cons**:
- Big-bang migration — large PR surface area (~4,000+ lines of Docker code replaced)
- All tests rewritten simultaneously
- No rollback path to Docker once merged
- k3s becomes a new prerequisite for all developers
- Network isolation testing requires Calico, adding CNI complexity

### Option B: Abstraction Layer with Dual Backend

**Approach**: Introduce `ContainerBackend` protocol with both `DockerBackend` and `KubernetesBackend`. Feature flag selects backend. Migrate incrementally.

**Pros**:
- Incremental migration, lower risk per change
- Rollback trivial — switch flag back to Docker
- Can validate k8s path in CI while Docker remains default

**Cons**:
- Two backends to maintain indefinitely (risk of Docker backend never being removed)
- Abstraction leakage — Docker networks vs k8s NetworkPolicies have different semantics
- Doubles the test matrix
- Issue explicitly rejects this approach

### Option C: Kubernetes for Orchestrator/Gateway Only

**Approach**: Deploy orchestrator and gateway as k8s Deployments but keep spawning agents via Docker socket mounted into the orchestrator pod.

**Pros**:
- Smallest initial scope
- Orchestrator/gateway get k8s benefits (scaling, health checks)

**Cons**:
- Doesn't solve the core scalability problem (agents still single-host)
- Docker socket in a k8s pod is a well-known security anti-pattern
- Hybrid model increases operational complexity
- Issue explicitly rejects this approach

## Recommended Approach

**Option A: Full Cutover** — as selected in the issue. The issue author has already evaluated the tradeoffs and made a clear decision. The approach is architecturally sound:

1. **k8s Jobs** are the correct primitive for agents — they run to completion, exit codes matter, `activeDeadlineSeconds` replaces timeout mechanisms, and `backoffLimit: 0` prevents unwanted restarts.
2. **Calico + NetworkPolicies** can faithfully replicate the current dual-network isolation model using namespace-level default-deny + selective egress to gateway.
3. **Kustomize overlays** (`base/` + `overlays/local/`) are the right choice for YAML-native manifest management with a clear path to `overlays/gke/`.
4. **`ContainerBackend` protocol** should still be defined (as the issue notes) for testability, even though only one implementation will exist.

The key risk is the size of the changeset. The plan phase should decompose this into parallelizable workstreams (manifests, k8s client, spawner, monitor, network policies, storage, CI, CLI, tests) to manage scope.

## Open Questions

> **Note**: `egg-contract add-decision` / `egg-contract add-feedback` commands fail with "Worktree not found for container" — the gateway cannot resolve this agent's worktree for contract mutations. Questions are documented below and should be registered by the orchestrator or manually once the worktree mapping is resolved.

### Decision 1: ContainerBackend interface type

**Question**: Should the `ContainerBackend` protocol be defined as a Python `Protocol` (structural typing) or an `ABC` (nominal typing)?

- **Option A: Python Protocol** — Allows duck typing, easier mocking in tests, no inheritance required. Consistent with modern Python patterns.
- **Option B: ABC** — Enforces explicit inheritance, clearer error messages when methods are missing.
- **Other** (explain in reply)

### Decision 2: Shared filesystem approach for worktree isolation

**Question**: For shared filesystem access between gateway and agent pods (worktree isolation), which storage approach should be used?

- **Option A: PVC with ReadWriteMany** — Standard k8s abstraction. Requires NFS provisioner or similar for RWX access class. Most portable to GKE.
- **Option B: hostPath volumes** — Simple for single-node k3s. Gateway and agent pods see the same host directories. Not portable to multi-node clusters.
- **Option C: EmptyDir with init container** — Each agent pod gets a fresh worktree via init container that clones/prepares it. No shared state needed.
- **Option D: Gateway manages worktrees via API** — Agents access worktree contents over the network via gateway API instead of shared filesystem. Major refactor.
- **Other** (explain in reply)

### Decision 3: Dockerfiles and image build strategy

**Question**: Should the existing Dockerfiles be retained as-is (they produce OCI images regardless of runtime), or should they be modified as part of this migration?

- **Option A: Keep Dockerfiles unchanged** — `docker build` still produces the images; k3s imports them. Minimal change.
- **Option B: Migrate to multi-stage builds optimized for k3s** — Optimize layer caching, reduce image size for k3s import.
- **Option C: Add Makefile targets that abstract the build** — `make build` works for both Docker and k3s contexts.
- **Other** (explain in reply)

### Decision 4: GHCR image publishing

**Question**: `release-images.yml` currently pushes to GHCR using `docker/build-push-action`. Should this workflow be updated as part of this migration, or left as-is since GHCR is Docker-registry-compatible regardless of runtime?

- **Option A: Leave release-images.yml as-is** — GHCR doesn't care about the runtime; Docker buildx still works for CI publishing.
- **Option B: Update to use k3s-based build in CI** — Full consistency between local and CI.
- **Other** (explain in reply)

### Decision 5: `.git` shadow mount pattern in k8s

**Question**: The current sandbox uses a `.git` shadow mount (tmpfs overlay on `.git` device file) to force all git operations through the gateway API. How should this be replicated in k8s?

- **Option A: Init container creates the shadow mount** — Agent pod init container sets up the tmpfs overlay before the main container starts.
- **Option B: SecurityContext with device mounts** — Use k8s volume mounts to achieve the same effect.
- **Option C: Redesign — use git wrapper scripts only** — Remove the device-file approach entirely; rely on git wrapper scripts that route to gateway (simpler, but changes security model).
- **Other** (explain in reply)

### Decision 6: Integration test infrastructure

**Question**: Integration tests currently use `docker-compose` to spin up isolated test clusters. How should they work with k8s?

- **Option A: k3s in CI with dedicated test namespace** — Tests create/destroy namespaces. Same runtime as production.
- **Option B: kind (Kubernetes in Docker)** — Lighter weight for CI, doesn't require k3s. But introduces a second k8s distribution.
- **Option C: Keep docker-compose for tests only** — Tests don't need to match production runtime exactly. Simplest migration.
- **Other** (explain in reply)

### Feedback 1: Scope and timeline expectations

- What is the expected timeline for this migration? Is it acceptable to have a multi-week implementation period?
- Are there any upcoming features or changes that depend on the Docker-based architecture being stable?
- Should the `sandbox/egg_lib/runtime.py` CLI-side Docker code (1,197 lines) also be migrated in this PR, or should that be a follow-up? The CLI's `build_sandbox_docker_cmd()` is used for interactive `egg` sessions, which have different requirements than pipeline-spawned agents.

### Feedback 2: Developer experience requirements

- Is k3s the only acceptable local Kubernetes distribution, or would alternatives like `minikube` or `kind` be acceptable?
- What is the minimum supported host OS? k3s is Linux-native; macOS/Windows users need a Linux VM (e.g., via Lima, Rancher Desktop, or WSL2).
- Should `make k3s-setup` handle Calico CNI installation automatically, or should it be a documented manual step?

### Feedback 3: Gateway session binding migration

- The issue recommends switching from IP-based to token-only gateway auth. Should this be done as part of this migration, or is it a separate concern? The current IP binding is deeply integrated into `container_spawner.py` and `runtime.py`.
- Are there any security implications of removing IP-based binding that need review?

## Complexity Assessment

**High** — This is a fundamental architectural change affecting:

- Core container lifecycle management (~2,400 lines across 3 files)
- CLI-side container execution (~1,200 lines in runtime.py)
- Deployment infrastructure (3 docker-compose files → Kustomize manifests)
- Network isolation model (Docker networks → Calico NetworkPolicies)
- Storage model (Docker bind mounts → k8s PVCs/hostPath)
- Integration test infrastructure (docker-compose → k8s-based)
- CI/CD pipeline (2 workflows + release workflow)
- Developer experience (Docker → k3s prerequisite)

Multiple independent workstreams (manifests, k8s client, network policies, storage, CI, CLI, tests) should be planned as a parallelizable multi-phase effort in the plan phase.

---

*Authored-by: egg*
