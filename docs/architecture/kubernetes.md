# Kubernetes Architecture

This document describes the Kubernetes-based container runtime architecture that replaces the previous Docker Compose stack. The migration (issue #1553) introduces k3s for local development, Kustomize manifests for declarative infrastructure, and Calico NetworkPolicies for agent isolation.

## Overview

egg runs on Kubernetes using k3s as the local runtime. The system uses two namespaces to maintain security isolation between trusted infrastructure and untrusted agent workloads:

- **`egg-system`** — Orchestrator and gateway run as long-lived Deployments
- **`egg-agents`** — Agent containers run as Kubernetes Jobs that execute to completion

This replaces the previous Docker Compose dual-network architecture (`egg-isolated` + `egg-external`) with Kubernetes-native constructs while preserving the same security properties.

## Architecture

```
Namespace: egg-system              Namespace: egg-agents
┌──────────────────┐               ┌──────────┐  ┌──────────┐
│   orchestrator   │               │ agent-1  │  │ agent-2  │
│   (Deployment)   │               │  (Job)   │  │  (Job)   │
│   Port: 9849     │               └────┬─────┘  └────┬─────┘
└──────┬───────────┘                    │              │
       │                                │ (egress      │ (egress
       │ k8s API                        │  only to     │  only to
       │ (RBAC)                         │  gateway)    │  gateway)
       │                                │              │
┌──────┴───────────┐                    │              │
│     gateway      │◄───────────────────┴──────────────┘
│   (Deployment)   │
│   + Service      │
│   Ports: 9848,   │
│   3129, 9851     │──── (internet via Squid proxy)
└──────────────────┘
```

### Key Design Decisions

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| 1 | Manifest approach | Kustomize overlays | YAML-native, built into kubectl, no Helm templating complexity. `base/` + `overlays/local/` structure for future extensibility (e.g., `overlays/gke/`) |
| 2 | Network isolation | Namespaces + NetworkPolicies | Default-deny in `egg-agents`, egress only to gateway. Maps directly to the Docker dual-network model |
| 3 | CNI | Calico (replacing Flannel) | k3s ships with Flannel which does not support NetworkPolicies. Calico is required for network isolation enforcement |
| 4 | Persistent storage | hostPath volumes (k3s local-path) | Standard k8s abstraction, works out of box with k3s. StorageClass swap for cloud environments later |
| 5 | Agent primitive | Kubernetes Jobs | Agents run to completion — Jobs match this lifecycle. `backoffLimit: 0` prevents unwanted restarts; `activeDeadlineSeconds` for timeouts |
| 6 | Gateway auth | Token-only (IP binding removed) | Pod IPs are ephemeral in k8s. Token auth is simpler and more portable |
| 7 | Local dev runtime | k3s | Lightweight k8s distribution suitable for single-node development |
| 8 | Image distribution | k3s local image import | `k3s ctr images import` for local dev. No remote registry needed for local workflows |

## Namespace Model

```
┌─────────────────────────────────────────────────────┐
│  egg-system namespace                                │
│                                                      │
│  ┌──────────────────┐  ┌────────────────────┐       │
│  │  orchestrator    │  │  gateway           │       │
│  │  Deployment      │  │  Deployment        │       │
│  │  + Service       │  │  + Service         │       │
│  │                  │  │                    │       │
│  │  ServiceAccount  │  │  Ports: 9848,     │       │
│  │  + RBAC for Job  │  │  3129, 9851       │       │
│  │  management in   │  │                    │       │
│  │  egg-agents      │  │  Squid proxy for  │       │
│  └──────────────────┘  │  outbound traffic  │       │
│                        └────────────────────┘       │
└─────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────┐
│  egg-agents namespace                                │
│                                                      │
│  Default-deny ingress                                │
│  Default-deny egress (except → gateway Service)     │
│                                                      │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐          │
│  │ coder    │  │ tester   │  │ docmter  │          │
│  │  Job     │  │  Job     │  │  Job     │  ...     │
│  │          │  │          │  │          │          │
│  │ hostPath │  │ hostPath │  │ hostPath │          │
│  │ volumes  │  │ volumes  │  │ volumes  │          │
│  └──────────┘  └──────────┘  └──────────┘          │
└─────────────────────────────────────────────────────┘
```

### RBAC

The orchestrator's ServiceAccount has permissions to manage Jobs and Pods in the `egg-agents` namespace:

- `create`, `delete`, `list`, `watch` on `jobs` and `pods`
- `get` on `pods/log` for log retrieval

This follows the principle of least privilege — the orchestrator can manage agent workloads but has no permissions in `egg-system` beyond its own Deployment.

## Network Isolation

Network isolation is enforced by Calico NetworkPolicies, replacing Docker's `internal: true` network configuration.

### NetworkPolicy Rules

| Policy | Namespace | Effect |
|--------|-----------|--------|
| Default deny ingress | `egg-agents` | Agents cannot receive unsolicited connections |
| Default deny egress | `egg-agents` | Agents cannot reach the internet directly |
| Allow egress to gateway | `egg-agents` | Agents can reach the gateway Service in `egg-system` |
| Allow orchestrator to agents | `egg-system` → `egg-agents` | Orchestrator can reach agent pods for health checks and logs |

### Security Properties Preserved

The k8s network isolation preserves all security properties from the Docker architecture:

| Property | Docker Implementation | Kubernetes Implementation |
|----------|----------------------|--------------------------|
| Agents cannot reach internet | `internal: true` network, no external route | Default-deny egress NetworkPolicy |
| Agents can only reach gateway | Gateway on both networks | Egress allow to gateway Service only |
| Agents cannot reach each other | Separate containers on internal network | Default-deny ingress + no peer egress allow |
| Gateway is dual-homed | `egg-isolated` + `egg-external` networks | `egg-system` namespace with internet access |
| Orchestrator can reach agents | Both on `egg-isolated` network | Cross-namespace allow from orchestrator |

### CNI Requirement

**k3s ships with Flannel as the default CNI, which does NOT support NetworkPolicies.** For network isolation to work, k3s must be installed with Flannel disabled and Calico installed separately:

```bash
# Install k3s without Flannel
curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="--flannel-backend=none --disable-network-policy" sh -

# Install Calico CNI
kubectl apply -f https://raw.githubusercontent.com/projectcalico/calico/v3.27.0/manifests/calico.yaml
```

This is handled automatically by `make k3s-setup`.

## Agent Jobs

Agent containers are modeled as Kubernetes Jobs with the following configuration:

| Setting | Value | Rationale |
|---------|-------|-----------|
| `backoffLimit` | `0` | No automatic retries — the orchestrator manages retry logic |
| `activeDeadlineSeconds` | Configurable | Replaces the Docker container timeout mechanism |
| `restartPolicy` | `Never` | Agents should not restart on failure |

### Job Lifecycle

```
1. Orchestrator creates Job in egg-agents namespace
   ├── Pod spec: env vars, volume mounts, labels
   ├── Init container: .git shadow mount (tmpfs overlay)
   └── Labels: pipeline-id, agent-role, creation timestamp

2. k8s scheduler places Pod on node
   └── Calico NetworkPolicy restricts network access

3. Agent runs to completion
   ├── Signals progress/completion to orchestrator
   └── Pushes commits via gateway

4. Job completes (exit code 0) or fails (non-zero)
   └── Orchestrator detects via watch/poll

5. Cleanup: orchestrator deletes Jobs by label selector
```

### Volume Mounts

Each agent Job receives:

| Volume | Type | Purpose |
|--------|------|---------|
| Worktree | `hostPath` | Per-agent isolated working directory |
| Certs | `Secret` or `ConfigMap` | TLS certificates for gateway communication |
| `.git` shadow | `emptyDir` (tmpfs) via init container | Prevents direct `.git` access, forces operations through gateway |

### Environment Variables

Agent pods receive the same ~30+ environment variables as the previous Docker containers, including `EGG_PIPELINE_ID`, `EGG_AGENT_ROLE`, `GATEWAY_URL`, session tokens, and proxy configuration. The `KubernetesSpawner` translates these from the spawner configuration into the Job's pod spec.

## Gateway Authentication

Gateway session authentication has been simplified from IP-based binding to **token-only auth**. This change was driven by the k8s migration:

- **Before (Docker)**: Sessions were bound to the container's static IP address on the `egg-isolated` network. The gateway validated both the session token and the source IP.
- **After (Kubernetes)**: Sessions are authenticated by token only. Pod IPs are ephemeral and assigned by the CNI, making IP-based binding impractical.

This simplification reduces complexity without weakening security — the token is still required for all gateway API requests, and NetworkPolicies prevent unauthorized pods from reaching the gateway.

## Kustomize Structure

Manifests are organized using Kustomize with a base + overlay pattern:

```
k8s/
├── base/
│   ├── kustomization.yaml
│   ├── namespaces.yaml              # egg-system, egg-agents
│   ├── orchestrator-deployment.yaml  # Orchestrator Deployment + Service
│   ├── orchestrator-service.yaml
│   ├── gateway-deployment.yaml       # Gateway Deployment + Service
│   ├── gateway-service.yaml
│   ├── agent-job-template.yaml       # Agent Job template
│   ├── rbac.yaml                     # ServiceAccount + ClusterRole + Binding
│   └── network-policies.yaml         # Calico NetworkPolicies
└── overlays/
    └── local/
        ├── kustomization.yaml
        └── patches/                  # k3s-specific patches (hostPath, etc.)
```

### Extending for Cloud Environments

The overlay pattern allows adding cloud-specific configurations without modifying the base manifests:

```
k8s/overlays/
├── local/        # k3s with hostPath volumes
└── gke/          # GKE with PersistentVolumeClaims, node pools, etc.
```

## Component Migration Map

| Docker Component | Kubernetes Replacement | Notes |
|-----------------|----------------------|-------|
| `docker-compose.yml` | `k8s/base/` + `k8s/overlays/local/` | Kustomize manifests |
| `DockerClient` | `KubernetesClient` | Wraps `kubernetes` Python client |
| `ContainerSpawner` | `KubernetesSpawner` | Creates Jobs instead of containers |
| `ContainerMonitor` | `KubernetesMonitor` | Watches Job/Pod status |
| Docker networks (`egg-isolated`, `egg-external`) | Namespaces + Calico NetworkPolicies | Same isolation model |
| Docker volume mounts | `hostPath` volumes + PV/PVCs | k3s local-path provisioner |
| Docker labels | Kubernetes labels | Same filtering semantics |
| `docker run` (CLI) | `kubectl run` / Job creation | For interactive sessions |
| `docker logs` | `kubectl logs` / Pod log API | Log retrieval |

## Developer Workflow

### Setup

```bash
# Install k3s with Calico CNI
make k3s-setup

# Build and import images into k3s
make build

# Deploy egg to k3s
make deploy

# Verify
kubectl get pods -n egg-system
kubectl get networkpolicies -n egg-agents
```

### Teardown

```bash
make k3s-teardown
```

### Differences from Docker Workflow

| Aspect | Docker (Previous) | Kubernetes (Current) |
|--------|-------------------|---------------------|
| Prerequisites | Docker Engine + Compose | k3s |
| Start services | `egg --compose` / `docker-compose up` | `make deploy` |
| Stop services | `egg --compose --down` | `make k3s-teardown` |
| View logs | `docker logs egg-gateway` | `kubectl logs -n egg-system deploy/gateway` |
| Check health | `curl localhost:9848/api/v1/health` | `kubectl get pods -n egg-system` |
| Image build | `docker build` | `docker build` + `k3s ctr images import` |

## Monitoring and Debugging

### Checking Agent Status

```bash
# List agent Jobs
kubectl get jobs -n egg-agents -l pipeline-id=issue-123

# View agent pod logs
kubectl logs -n egg-agents -l pipeline-id=issue-123,agent-role=coder

# Check NetworkPolicy enforcement
kubectl get networkpolicies -n egg-agents

# Describe a Job for events and status
kubectl describe job -n egg-agents <job-name>
```

### Common Issues

| Issue | Cause | Fix |
|-------|-------|-----|
| Agent pod stuck in `Pending` | Image not imported into k3s | `make build` to rebuild and import |
| Agent cannot reach gateway | NetworkPolicy misconfigured | Verify Calico is running: `kubectl get pods -n calico-system` |
| Jobs not being created | RBAC misconfigured | Check orchestrator ServiceAccount permissions |
| Pod evicted | Node resource pressure | Check node resources: `kubectl top nodes` |

## Security Model

The Kubernetes architecture maintains egg's zero-trust security model:

1. **Credential isolation** — Agent pods have no credentials. All authenticated operations route through the gateway.
2. **Network isolation** — Calico NetworkPolicies enforce that agents can only reach the gateway. No internet access, no inter-agent communication.
3. **Gateway as choke point** — All external access (GitHub, Anthropic API, package registries) is proxied through the gateway's Squid proxy.
4. **No merge capability** — The gateway has no merge endpoint. This is unchanged from the Docker architecture.
5. **Phase-locked operations** — The gateway validates all git operations against the current SDLC phase. This is unchanged.

## Future Work

- **GKE overlay**: Add `k8s/overlays/gke/` for cloud deployment with PersistentVolumeClaims, node auto-scaling, and GHCR image pulls
- **Multi-node support**: Current `hostPath` volumes are single-node only. Cloud deployments will need shared storage (e.g., GCS FUSE, NFS)
- **Resource limits**: Add CPU/memory limits for agent Jobs when deploying to shared clusters
- **GHCR image registry**: Replace `k3s ctr images import` with GHCR pulls for cloud environments

## Related Documentation

- [Network Isolation](network-isolation.md) — Proxy-based traffic control and domain allowlist
- [Architecture Overview](README.md) — System design and security model
- [Deployment Guide](../guides/deployment.md) — Setup and configuration
- [Local Quickstart](../guides/local-quickstart.md) — Getting started with k3s
- [Orchestrator Architecture](orchestrator.md) — Pipeline state, agent lifecycle, deployment modes
