# Kubernetes Migration

This document describes the migration of egg's container runtime from Docker to Kubernetes (k3s), covering architecture decisions, the new deployment model, and the mapping from Docker concepts to Kubernetes equivalents.

> **Issue:** [#1553](https://github.com/jwbron/egg/issues/1553) | **Decision:** Full cutover (Option A) — replace Docker entirely, no dual-backend.

## Motivation

The original Docker-based architecture binds all agent containers to a single host via the Docker socket. This creates four scaling limitations:

| Limitation | Impact |
|-----------|--------|
| **Single-host bound** | Docker socket is local — all containers run on one machine |
| **No native scheduling** | Container placement, resource quotas, and auto-scaling are manual |
| **No fault tolerance** | Host failure kills all agents with no automatic recovery |
| **Resource contention** | Concurrent agents compete for host resources without proper scheduling |

Kubernetes solves all four: its scheduler places workloads across nodes, enforces resource quotas, and restarts failed pods automatically. k3s provides a lightweight, single-binary Kubernetes distribution suitable for local development.

## Architecture Overview

### Before (Docker)

```
Host Machine
├── docker-compose.yml
│   ├── egg-orchestrator (container)     ── Docker socket ──► spawn containers
│   └── egg-gateway (container)
│
├── egg-isolated network (172.32.0.0/24, internal)
│   ├── gateway     172.32.0.2
│   ├── orchestrator 172.32.0.3
│   └── agents      172.32.0.128-254
│
└── egg-external network (172.33.0.0/24, bridge)
    └── gateway     172.33.0.2  ──► internet
```

### After (Kubernetes)

```
k3s Cluster
├── Namespace: egg-system
│   ├── Deployment: orchestrator (+ Service :9849)
│   ├── Deployment: gateway (+ Service :9848, :3129, :9851)
│   └── Deployment: litellm (+ Service :4000)  # non-Claude model proxy; no-op until model_list populated (#2769)
│
├── Namespace: egg-agents
│   ├── Job: agent-coder-{pipeline-id}
│   ├── Job: agent-tester-{pipeline-id}
│   └── Job: agent-documenter-{pipeline-id}
│
└── NetworkPolicies (Cilium CNI)
    ├── default-deny-all (egg-agents ingress + egress)
    ├── allow-agent-to-gateway (egress to egg-system/gateway only)
    └── allow-orchestrator-to-agents (ingress from egg-system/orchestrator)
```

## Design Decisions

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| 1 | Manifest approach | **Kustomize overlays** | YAML-native, built into kubectl, no Helm templating complexity. `base/` + `overlays/local/` structure |
| 2 | Network isolation | **Separate namespaces + NetworkPolicies** | `egg-system` for orchestrator+gateway, `egg-agents` for Jobs. Default-deny maps to Docker's `internal: true` |
| 3 | CNI | **Cilium** (replacing Flannel) | Flannel (k3s default) does not support NetworkPolicies. Cilium is current; was Calico through [#2703](https://github.com/jwbron/egg/issues/2703), which swapped to Cilium after recurring on-host SA-token expiry wedged pod teardown ([#2580](https://github.com/jwbron/egg/issues/2580)) |
| 4 | Persistent storage | **hostPath** (k3s local-path) | Standard for single-node k3s. Future GKE work uses PVCs with ReadWriteMany |
| 5 | Agent primitive | **k8s Jobs** | Agents run to completion; exit codes matter. `backoffLimit: 0` prevents unwanted restarts. `activeDeadlineSeconds` replaces timeout mechanism |
| 6 | Gateway auth | **Token-only** (IP binding removed) | Pod IPs are ephemeral in k8s. Token auth is simpler and more portable |
| 7 | Target environment | **Local k3s only** | GKE deployment is follow-up work. Kustomize overlays structured for future extensibility |
| 8 | Image strategy | **k3s local image import** | `k3s ctr images import` for local dev. GHCR added with GKE follow-up |

## Component Mapping

### Docker → Kubernetes

| Docker Concept | Kubernetes Equivalent | Notes |
|---------------|----------------------|-------|
| `docker-compose.yml` | Kustomize manifests (`k8s/base/`, `k8s/overlays/local/`) | Declarative, overlay-based |
| Docker container | k8s Pod (via Job) | Jobs ensure run-to-completion semantics |
| `DockerClient` | `KubernetesClient` | Wraps `kubernetes` Python client |
| `ContainerSpawner` | `KubernetesSpawner` | Creates Jobs with env vars, volumes, labels |
| `ContainerMonitor` | `KubernetesMonitor` | Uses Job watch API / polling |
| Docker networks (`egg-isolated`, `egg-external`) | Namespaces + NetworkPolicies (Cilium) | See [Network Isolation](#network-isolation) |
| Docker `internal: true` | NetworkPolicy default-deny egress | Agents cannot reach internet directly |
| Container labels | Pod/Job labels + label selectors | Same filtering model |
| Docker bind mounts | hostPath volumes | Same for single-node; PVCs for multi-node |
| Docker health checks | k8s liveness/readiness probes | Native k8s health model |
| Fixed IPs (172.32.0.x) | k8s Service DNS names | `gateway.egg-system.svc.cluster.local` |
| Docker socket | k8s API via ServiceAccount | RBAC-scoped permissions |

### Code Module Mapping

| Old Module | New Module | Purpose |
|-----------|-----------|---------|
| `orchestrator/docker_client.py` | `orchestrator/kubernetes_client.py` | Low-level API wrapper |
| `orchestrator/container_spawner.py` | `orchestrator/kubernetes_spawner.py` | Agent lifecycle management |
| `orchestrator/container_monitor.py` | `orchestrator/kubernetes_monitor.py` | State monitoring + callbacks |
| `shared/egg_container/` (`build_sandbox_docker_cmd()`) | `shared/egg_container/` (`build_sandbox_job_spec()`) | Shared config builder |

### ContainerBackend Protocol

Both old and new implementations satisfy a common `ContainerBackend` protocol defined in `orchestrator/container_backend.py` (Python `Protocol` class with `@runtime_checkable` for structural typing):

```python
@runtime_checkable
class ContainerBackend(Protocol):
    def create_container(self, name: str, image: str | None = None,
                         environment: dict[str, str] | None = None,
                         volumes: dict[str, dict[str, str]] | None = None,
                         network: str | None = None, command: list[str] | None = None,
                         labels: dict[str, str] | None = None, **kwargs) -> ContainerInfo: ...
    def start_container(self, container_id: str) -> ContainerInfo: ...
    def stop_container(self, container_id: str, timeout: int = 10) -> ContainerInfo: ...
    def remove_container(self, container_id: str, force: bool = False, v: bool = True) -> None: ...
    def get_container_info(self, container_id: str) -> ContainerInfo: ...
    def list_containers(self, all: bool = True, labels: dict[str, str] | None = None) -> list[ContainerInfo]: ...
    def get_container_logs(self, container_id: str, tail: int = 100, since: datetime | None = None) -> str: ...
    def wait_for_container(self, container_id: str, timeout: int = 300) -> ContainerInfo: ...
    def cleanup_orphaned_containers(self, max_age_hours: int = 24) -> int: ...
    def is_connected(self) -> bool: ...
```

The `KubernetesClient` maps these to k8s API operations: `create_container` creates a Job, `stop_container` deletes the Job, `get_container_logs` reads pod logs, etc. Labels (`egg.pipeline.id`, `egg.agent.role`, `egg.container.name`) are used for filtering and identification.

## Network Isolation

The migration preserves the fail-closed network isolation model. The implementation changes but the security properties are identical:

### Docker Model (Before)

- `egg-isolated` network with `internal: true` — no external gateway, no route to internet
- `egg-external` network — gateway only, bridged to host
- Agents on isolated network can only reach gateway

### Kubernetes Model (After)

```
Namespace: egg-system          Namespace: egg-agents
┌──────────────┐               ┌──────────┐  ┌──────────┐
│ orchestrator │               │ agent-1  │  │ agent-2  │
│ (Deployment) │               │  (Job)   │  │  (Job)   │
└──────┬───────┘               └────┬─────┘  └────┬─────┘
       │                            │              │
       │                            │ (egress      │ (egress
       │                            │  only to     │  only to
       │                            │  gateway)    │  gateway)
       │                            │              │
┌──────┴───────┐                    │              │
│   gateway    │◄───────────────────┴──────────────┘
│ (Deployment) │
│  + Service   │──── (internet via Squid proxy)
└──────────────┘
```

**NetworkPolicies (enforced by Cilium):**

| Policy | Namespace | Effect |
|--------|-----------|--------|
| `default-deny-ingress` | `egg-agents` | No inbound traffic to agent pods |
| `default-deny-egress` | `egg-agents` | No outbound traffic from agent pods (except below) |
| `allow-agent-to-gateway` | `egg-agents` | Egress to gateway pods in `egg-system` on ports 9848 (API) and 3129 (proxy) |
| `allow-orchestrator-to-agent` | `egg-agents` | Ingress from orchestrator pods in `egg-system` |
| `allow-agent-dns` | `egg-agents` | Egress to `kube-system` on port 53 (UDP/TCP) for DNS resolution |

**Security properties preserved:**
- Agents cannot reach the internet directly (must go through gateway proxy)
- Agents cannot reach each other (default-deny ingress)
- Agents cannot bypass the gateway (no other egress permitted)
- All traffic is auditable through the gateway

### Service Discovery

Docker's fixed IP scheme is replaced by Kubernetes DNS:

| Docker | Kubernetes |
|--------|-----------|
| `172.32.0.2` (gateway) | `gateway.egg-system.svc.cluster.local` |
| `172.32.0.3` (orchestrator) | `orchestrator.egg-system.svc.cluster.local` |
| `HTTP_PROXY=http://gateway:3128` | `HTTP_PROXY=http://gateway.egg-system.svc.cluster.local:3129` |

## Storage Model

### Worktree Isolation

Each agent pod receives its own worktree via hostPath volumes on single-node k3s:

```yaml
volumes:
  - name: agent-worktree
    hostPath:
      path: /home/egg/.egg-worktrees/{job-name}/{repo-name}
      type: DirectoryOrCreate
```

The gateway manages worktree lifecycle and the orchestrator creates Jobs with the appropriate hostPath mounts. This is functionally identical to Docker bind mounts.

> **Multi-node limitation:** hostPath volumes are node-local. For multi-node clusters (GKE follow-up), this will need ReadWriteMany PVCs or a networked filesystem.

### .git Shadow Mount

The `.git` shadow mount (tmpfs overlay preventing direct git access) is replicated using a k8s init container:

```yaml
initContainers:
  - name: git-shadow
    # Creates tmpfs overlay on .git path
    volumeMounts:
      - name: git-shadow
        mountPath: /workspace/.git
volumes:
  - name: git-shadow
    emptyDir:
      medium: Memory  # tmpfs equivalent
```

## Gateway Auth Changes

Pod IPs are ephemeral in Kubernetes (assigned by the CNI, change on pod restart). The gateway's session binding is migrated from IP-based to token-only authentication:

| Aspect | Before (Docker) | After (Kubernetes) |
|--------|-----------------|-------------------|
| Session binding | IP address + token | Token only |
| Session creation | IP allocated from Docker network | Token generated, no IP binding |
| Request validation | Token + source IP match | Token only |
| Session lookup | By IP or token | By token only |

This simplifies the auth model and eliminates a class of session-binding bugs when pods restart with different IPs.

## Manifest Structure

```
k8s/
├── base/                              # Base manifests (environment-agnostic)
│   ├── kustomization.yaml
│   ├── namespaces.yaml                # egg-system, egg-agents
│   ├── orchestrator-deployment.yaml   # Orchestrator Deployment + env
│   ├── orchestrator-service.yaml      # Service on port 9849
│   ├── gateway-deployment.yaml        # Gateway Deployment + env
│   ├── gateway-service.yaml           # Service on ports 9848, 3129, 9851
│   ├── network-policies.yaml          # NetworkPolicies (Cilium-enforced)
│   └── rbac.yaml                      # ServiceAccount + RBAC for orchestrator

Agent Jobs are built programmatically by ``KubernetesClient.create_container`` — there is no standalone YAML template.
│
└── overlays/
    └── local/                         # k3s-specific patches
        ├── kustomization.yaml
        └── patches/                   # hostPath storage, local-path provisioner
```

## RBAC Model

The orchestrator uses a ServiceAccount (`egg-orchestrator` in `egg-system`) bound to four namespace- or cluster-scoped roles. There is no broad `egg-orchestrator` ClusterRole — every grant is least-privilege.

1. **Role** (`egg-agent-manager` in `egg-agents`): manage agent Jobs/Pods (`jobs`: create/delete/get/list/watch/patch; `pods`: delete/get/list/watch; `pods/log`: get; `pods/exec`: create). Jobs create their pods on the SA's behalf, so the SA does not need `pods: create`.
2. **Role** (`egg-service-log-reader` in `egg-system`): read the orchestrator's own Deployments and Pod logs (`deployments`: get/list — `list` added in #2648 for `_collect_egg_image_tags`; `pods`: get/list; `pods/log`: get).
3. **ClusterRole** (`egg-cluster-topology-reader`): cluster-scoped `nodes: get/list` for `_detect_k3s`'s kubelet-version probe — the only grant that genuinely needs cluster scope.
4. **Role** (`egg-kube-system-topology-reader` in `kube-system`): `apps/daemonsets: get/list` for `_detect_cni` and `_detect_k3s`'s image-name fallback. Scoped to kube-system to keep cluster-wide DaemonSet reads off the SA (least-privilege per #2658 review).

```yaml
# Namespace-scoped Role in egg-agents
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: egg-agent-manager
  namespace: egg-agents
rules:
  - apiGroups: ["batch"]
    resources: ["jobs"]
    verbs: ["create", "delete", "get", "list", "watch", "patch"]
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["delete", "get", "list", "watch"]
  - apiGroups: [""]
    resources: ["pods/log"]
    verbs: ["get"]
  - apiGroups: [""]
    resources: ["pods/exec"]
    verbs: ["create"]
```

This replaces the Docker socket mount with a principle-of-least-privilege API access model. The orchestrator can manage Jobs and Pods in `egg-agents`; the topology-reader roles are scoped minimally so `validate_network_isolation` and `get_deployment_context` can detect CNI and k3s state without cluster-wide DaemonSet access.

## Developer Workflow Changes

### Before

```bash
# Prerequisites: Docker Desktop / Docker Engine + Compose v2
docker compose up -d          # Start gateway + orchestrator
egg --public                  # Start sandbox session
```

### After

```bash
# Prerequisites: k3s + Cilium CNI
make k3s-setup                # Install k3s with Cilium, wait for ready
make deploy                   # kubectl apply -k k8s/overlays/local/
egg --public                  # Start sandbox session (creates k8s Job)
```

### New Makefile Targets

| Target | Description |
|--------|-------------|
| `make k3s-setup` | Install k3s with `--flannel-backend=none --disable-network-policy`, install Cilium, wait for cluster ready |
| `make deploy` | `kubectl apply -k k8s/overlays/local/` — deploy all resources |
| `make k3s-teardown` | Remove k3s installation |
| `make build` | Build images and import into k3s via `k3s ctr images import` |

### CNI Installation

k3s ships with Flannel which does **not** support NetworkPolicies. k3s must be installed with Flannel disabled:

```bash
curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="--flannel-backend=none --disable-network-policy" sh -
scripts/install-cilium.sh   # downloads cilium-cli and runs `cilium install`
```

This is automated by `make k3s-setup` and `scripts/install-cilium.sh`.

## CI/CD Changes

| Workflow | Before | After |
|----------|--------|-------|
| `test-integration.yml` | `docker build` + `docker compose up` | k3s setup + `k3s ctr images import` + `kubectl apply` |
| `test-e2e.yml` | `docker compose` for full stack | k3s cluster with dedicated test namespace |
| `release-images.yml` | Unchanged | Unchanged (GHCR is runtime-agnostic) |
| `lint.yml` | Dockerfile linting retained | Dockerfile linting retained (OCI images still built) |

Integration tests use a dedicated `egg-test-agents` namespace with per-test-run setup/teardown.

## Migration Phases

The migration is organized into 5 sequential phases within a single PR:

| Phase | Name | Purpose |
|-------|------|---------|
| 1 | ContainerBackend Protocol + KubernetesClient | Foundation: abstraction layer and k8s client |
| 2 | Kustomize Manifests + Network Policies | Infrastructure: declarative k8s resources |
| 3 | Gateway Auth + KubernetesSpawner + Monitor | Core: behavioral cutover from Docker to k8s |
| 4 | CLI Runtime Migration | CLI: interactive session spawning via k8s |
| 5 | CI/CD, Docker Removal + Integration Tests | Cleanup: remove Docker, update CI |

Each phase builds on the previous and can be verified independently.

## Files Removed

After the migration, these Docker-specific files are removed:

| File | Replacement |
|------|-------------|
| `docker-compose.yml` | `k8s/base/` + `k8s/overlays/local/` |
| `integration_tests/docker-compose.yml` | k3s test namespace fixtures |
| `integration_tests/local_pipeline/docker-compose.yml` | k3s test namespace fixtures |
| `orchestrator/docker_client.py` | `orchestrator/kubernetes_client.py` |
| `orchestrator/container_spawner.py` | `orchestrator/kubernetes_spawner.py` |
| `orchestrator/container_monitor.py` | `orchestrator/kubernetes_monitor.py` |

**Retained:** All Dockerfiles (`sandbox/Dockerfile`, `orchestrator/Dockerfile`, `gateway/Dockerfile`) — OCI images are runtime-agnostic.

## Future Work

- **GKE deployment:** Add `k8s/overlays/gke/` with cloud-specific patches (PVCs with ReadWriteMany, GHCR image references, Workload Identity)
- **Multi-node storage:** Replace hostPath with NFS or GCS-backed PVCs for cross-node worktree access
- **Resource limits:** Add CPU/memory limits when deploying to shared clusters
- **Image registry:** Publish to GHCR for remote clusters (currently local import only)
- **Auto-scaling:** Horizontal pod autoscaling for gateway, vertical scaling hints for agents

## Related Documentation

- [Network Isolation](network-isolation.md) — Full network security model
- [Orchestrator Architecture](orchestrator.md) — Pipeline state, agent lifecycle, deployment modes
- [Deployment Guide](../guides/deployment.md) — Setup and deployment instructions
- [Deployment Diagnostics](../guides/deployment-diagnostics.md) — Operator guide: when to use `/deployment-diagnose` vs `/agent-diagnose`, evidence boundaries, redaction guarantees
- [MCP Deployment Tools](../reference/mcp-deployment-tools.md) — Six k8s-facing MCP tools (`get_deployment_context`, `validate_deployment_manifests`, `prune_stale_worktrees`, `validate_network_isolation`, `rebuild_and_rollout`, `get_service_logs`) that the diagnostic skills compose
- [Concurrent Execution](../guides/concurrent-execution.md) — Multi-agent coordination
