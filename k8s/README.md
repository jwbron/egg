# Kubernetes Manifests

Kustomize manifests for deploying egg on Kubernetes. Uses a base + overlay pattern for environment-specific configuration.

## Structure

```
k8s/
├── base/                             # Shared base manifests
│   ├── kustomization.yaml            # Resource list
│   ├── namespaces.yaml               # egg-system + egg-agents namespaces
│   ├── rbac.yaml                     # ServiceAccount, ClusterRole, ClusterRoleBinding
│   ├── orchestrator-deployment.yaml  # Orchestrator Deployment
│   ├── orchestrator-service.yaml     # Orchestrator Service (port 9849)
│   ├── gateway-deployment.yaml       # Gateway Deployment
│   ├── gateway-service.yaml          # Gateway Service (ports 9848, 3129, 9851)
│   ├── agent-job-template.yaml       # ConfigMap with agent Job template
│   └── network-policies.yaml         # Calico NetworkPolicies for agent isolation
└── overlays/
    └── local/                        # k3s local development overlay
        ├── kustomization.yaml        # Overlay configuration
        └── patches/
            └── hostpath-volumes.yaml # hostPath volume patches for k3s
```

## Usage

### Deploy to k3s (local)

```bash
# From repository root
make k3s-setup    # Install k3s with Calico CNI (first time only)
make build        # Build images and import into k3s
make deploy       # Apply overlays/local to k3s
```

Or manually:

```bash
kubectl apply -k k8s/overlays/local/
kubectl rollout status -n egg-system deployment/orchestrator
kubectl rollout status -n egg-system deployment/gateway
```

### Verify

```bash
kubectl get pods -n egg-system
kubectl get networkpolicies -n egg-agents
kubectl get svc -n egg-system
```

## Namespaces

| Namespace | Purpose |
|-----------|---------|
| `egg-system` | Trusted infrastructure (orchestrator, gateway) |
| `egg-agents` | Untrusted agent workloads (Jobs) |

## Network Policies

All NetworkPolicies are in `base/network-policies.yaml`. They require **Calico CNI** (Flannel does not support NetworkPolicies).

| Policy | Effect |
|--------|--------|
| `default-deny-ingress` | Block all ingress in `egg-agents` |
| `default-deny-egress` | Block all egress in `egg-agents` |
| `allow-agents-to-gateway` | Agent → gateway (ports 9848, 3129, 9851) |
| `allow-agent-to-orchestrator` | Agent → orchestrator (port 9849) |
| `allow-orchestrator-to-agents` | Orchestrator → agent pods |
| `allow-dns-egress` | Agent → CoreDNS (port 53) |

## RBAC

The orchestrator ServiceAccount (`orchestrator-sa`) has a ClusterRole granting:

- `create`, `delete`, `list`, `watch` on `jobs` and `pods` in `egg-agents`
- `get` on `pods/log` in `egg-agents`

## Agent Job Template

The `agent-job-template.yaml` ConfigMap contains a parameterized Job template. The `KubernetesSpawner` reads this template at runtime and substitutes placeholders (`{{AGENT_IMAGE}}`, `{{WORKTREE_HOST_PATH}}`, etc.) when creating Jobs.

Key Job settings:
- `backoffLimit: 0` — no automatic retries
- `activeDeadlineSeconds: 7200` — 2-hour timeout
- `ttlSecondsAfterFinished: 300` — auto-cleanup after 5 minutes
- `restartPolicy: Never`

## Adding Cloud Overlays

To add a GKE overlay:

```bash
mkdir -p k8s/overlays/gke
```

Create `k8s/overlays/gke/kustomization.yaml`:

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ../../base
patches:
  - path: patches/pvc-volumes.yaml
  - path: patches/resource-limits.yaml
```

## Related Documentation

- [Kubernetes Architecture](../docs/architecture/kubernetes.md) — Full design and rationale
- [Network Isolation](../docs/architecture/network-isolation.md) — Security model
- [Deployment Guide](../docs/guides/deployment.md) — Setup instructions
