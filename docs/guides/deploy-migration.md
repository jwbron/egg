# Deploy Migration Guide

This guide helps you migrate from the legacy Docker Compose deployment to the new Kubernetes (k3s) deployment.

> **Note:** egg has migrated from Docker Compose to Kubernetes (k3s) for container runtime management. Docker is still used for building images, but all runtime orchestration now uses Kubernetes. See [Kubernetes Architecture](../architecture/kubernetes.md) for the full design.

## Why Migrate?

The Kubernetes deployment offers:

- **Multi-node scalability**: Kubernetes scheduling replaces single-host Docker socket dependency
- **Fault tolerance**: k8s Job restarts and pod rescheduling replace manual container recovery
- **Native isolation**: Calico NetworkPolicies enforce agent isolation (replacing Docker `internal: true` networks)
- **Declarative infrastructure**: Kustomize manifests replace Docker Compose YAML
- **Standard tooling**: `kubectl` for debugging, monitoring, and management

## Migration Paths

### From Docker Compose (`docker-compose.yml`)

**Before (deprecated):**
```bash
# Start gateway + orchestrator via Docker Compose
docker compose up -d

# Or via egg-deploy
bin/egg-deploy up

# Or via egg CLI
egg --compose
```

**After (recommended):**
```bash
# One-time: install k3s with Calico CNI
make k3s-setup

# Build images and import into k3s
make build

# Deploy to k3s
make deploy

# Verify
kubectl get pods -n egg-system
```

### From Manual Docker Commands

**Before:**
```bash
# Create networks manually
docker network create --internal --subnet 172.32.0.0/24 egg-isolated
docker network create --subnet 172.33.0.0/24 egg-external

# Build and start gateway
docker build -t egg-gateway gateway/
docker run -d --name egg-gateway \
  --network egg-isolated --ip 172.32.0.2 \
  -v ~/.config/egg:/secrets:ro \
  ...many more flags...
  egg-gateway
```

**After:**
```bash
make k3s-setup    # Install k3s + Calico (first time only)
make build        # Build images and import into k3s
make deploy       # Deploy via Kustomize overlays
```

### From egg --compose

**Before:**
```bash
egg --compose           # Start gateway + orchestrator, auto-rebuild on changes
egg --compose --down    # Stop gateway + orchestrator
```

**After:**
```bash
make deploy             # Deploy/redeploy to k3s
make k3s-teardown       # Remove k3s and all resources
```

## What Changed

### Architecture

| Aspect | Docker (Previous) | Kubernetes (Current) |
|--------|-------------------|---------------------|
| Runtime | Docker Engine + Compose | k3s (lightweight Kubernetes) |
| Services | Docker containers | k8s Deployments (gateway, orchestrator) |
| Agents | Docker containers | k8s Jobs (in `egg-agents` namespace) |
| Network isolation | Docker networks (`egg-isolated`, `egg-external`) | Namespaces + Calico NetworkPolicies |
| Scheduling | None (single host) | k8s scheduler |
| Health checks | Docker Compose health checks | Kubernetes liveness/readiness probes |
| Logs | `docker logs` | `kubectl logs` |
| Cleanup | `docker compose down` | `make k3s-teardown` |

### Removed Files

The following Docker-specific files have been removed:

- `docker-compose.yml` — replaced by `k8s/base/` + `k8s/overlays/local/` Kustomize manifests
- `docker-compose.override.yml` — replaced by Kustomize overlays
- `orchestrator/docker_client.py` — replaced by `orchestrator/kubernetes_client.py`
- `orchestrator/container_spawner.py` — replaced by `orchestrator/kubernetes_spawner.py`
- `orchestrator/container_monitor.py` — replaced by `orchestrator/kubernetes_monitor.py`
- `integration_tests/docker-compose.yml` — updated to k8s-based test infra
- `integration_tests/local_pipeline/docker-compose.yml` — updated to k8s-based test infra

### New Files

- `k8s/base/` — Kustomize base manifests (namespaces, deployments, services, RBAC, NetworkPolicies)
- `k8s/overlays/local/` — k3s-specific overlay (hostPath volumes)
- `orchestrator/container_backend.py` — `ContainerBackend` protocol (abstract interface)
- `orchestrator/kubernetes_client.py` — Kubernetes client (implements `ContainerBackend`)
- `orchestrator/kubernetes_spawner.py` — Agent Job spawning
- `orchestrator/kubernetes_monitor.py` — Job/Pod status monitoring

### Gateway Authentication

Gateway session authentication has changed from IP-based binding to **token-only auth**. Pod IPs are ephemeral in Kubernetes, making IP-based binding impractical. The session token is still required for all gateway API requests.

### Commands Mapping

| Old Command | New Command |
|-------------|-------------|
| `bin/egg-deploy init` | `make k3s-setup` |
| `bin/egg-deploy up` | `make deploy` |
| `bin/egg-deploy down` | `make k3s-teardown` |
| `bin/egg-deploy status` | `kubectl get pods -n egg-system` |
| `bin/egg-deploy logs` | `kubectl logs -n egg-system deploy/gateway` |
| `bin/egg-deploy build` | `make build` |
| `docker logs egg-gateway` | `kubectl logs -n egg-system deploy/gateway` |
| `docker logs egg-orchestrator` | `kubectl logs -n egg-system deploy/orchestrator` |
| `docker kill -s HUP egg-gateway` | `kubectl exec -n egg-system deploy/gateway -- kill -s HUP 1` |

## Step-by-Step Migration

### 1. Stop the Old Deployment

```bash
# If using Docker Compose
docker compose down

# If using egg-deploy
bin/egg-deploy down

# If using egg --compose
egg --compose --down

# Clean up old Docker networks (optional)
docker network rm egg-isolated egg-external 2>/dev/null
```

### 2. Install k3s

```bash
make k3s-setup
```

This installs k3s with Flannel disabled and Calico CNI for NetworkPolicy support. It also creates the `egg-system` and `egg-agents` namespaces.

### 3. Build and Import Images

```bash
make build
```

This builds the Docker images and imports them into k3s via `k3s ctr images import`.

### 4. Deploy

```bash
make deploy
```

### 5. Verify

```bash
# Check services
kubectl get pods -n egg-system

# Check network policies
kubectl get networkpolicies -n egg-agents

# Check gateway health
kubectl exec -n egg-system deploy/gateway -- curl -s http://localhost:9848/api/v1/health
```

### 6. Test

```bash
egg   # Start a sandbox session
```

## Configuration

Your existing `~/.config/egg/config.yaml` and `~/.config/egg/secrets.env` files are still used. No configuration migration is needed — the k8s manifests read from the same configuration paths.

## Rollback

To revert to Docker Compose (not recommended):

```bash
# Stop k3s deployment
make k3s-teardown

# Checkout a pre-migration version
git checkout v<previous-version>

# Start Docker Compose
docker compose up -d
```

## Troubleshooting

### k3s Not Running

```bash
# Check k3s status
systemctl status k3s    # Linux
# or
kubectl get nodes
```

If k3s is not running, re-run `make k3s-setup`.

### Calico Not Running

```bash
kubectl get pods -n calico-system
```

If Calico pods are not running, re-install:
```bash
scripts/install-calico.sh
```

### Images Not Found

If agent pods show `ImagePullBackOff`:
```bash
# Rebuild and reimport images
make build

# Verify images are available
k3s ctr images ls | grep egg
```

### Permission Issues

```
Permission denied: /home/egg/repos
```

Ensure `host_uid` and `host_gid` are set correctly in `~/.config/egg/config.yaml`:
```yaml
host_uid: 1000  # output of id -u
host_gid: 1000  # output of id -g
```

## Getting Help

- Check pod status: `kubectl get pods -n egg-system`
- View logs: `kubectl logs -n egg-system deploy/gateway`
- Gateway health: `kubectl exec -n egg-system deploy/gateway -- curl -s http://localhost:9848/api/v1/health`
- See [Deployment Guide](deployment.md) for full documentation
- See [Kubernetes Architecture](../architecture/kubernetes.md) for design details
