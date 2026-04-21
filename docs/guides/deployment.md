# Deployment Guide

This guide covers the various ways to deploy egg, from local development to production environments.

## Deployment Methods

egg supports multiple deployment methods depending on your use case:

| Method | Best For | Prerequisites |
|--------|----------|---------------|
| **egg CLI** | Local development (recommended) | k3s |
| **Kubernetes (k3s)** | Local and production deployments | k3s + Calico CNI |
| **GitHub Action** | CI/CD automation | GitHub repository |

### Prerequisites by Platform

| Platform | Runtime | Notes |
|----------|---------|-------|
| **Linux** | k3s (native) | `make k3s-setup` handles installation |
| **macOS** | k3s via Lima or Rancher Desktop | Requires a Linux VM; see [k3s on macOS](#k3s-on-macos) |

> **Migration note:** egg previously used Docker Compose for deployments. As of [#1553](https://github.com/jwbron/egg/issues/1553), all container management uses Kubernetes via k3s. See [Kubernetes Migration](../architecture/kubernetes-migration.md) for architecture details.

## egg CLI (Recommended)

The simplest way to run egg. The CLI manages the gateway and sandbox lifecycle automatically:

```bash
# Install
pip install ./sandbox

# Run — auto-setup on first run, gateway started automatically
egg
```

On first run, egg prompts to configure repositories and credentials via `egg --setup`. Subsequent runs start the gateway and sandbox with a single command.

See the [CLI Reference](../../README.md#cli-reference) for all flags and options.

## Kubernetes (k3s) Deployment

egg runs on Kubernetes using k3s for local development. The orchestrator and gateway run as Deployments in the `egg-system` namespace, and agent containers run as Jobs in the `egg-agents` namespace.

### Quick Start

```bash
# Clone the repository
git clone https://github.com/jwbron/egg.git
cd egg

# Install k3s with Calico CNI
make k3s-setup

# Build and import images into k3s
make build

# Deploy egg to the cluster
make deploy

# Verify everything is running
kubectl get pods -n egg-system

# Start a sandbox session
egg --public
```

### Setup Details

#### k3s Installation

`make k3s-setup` installs k3s with Flannel disabled (required for NetworkPolicy support) and installs Calico CNI:

```bash
# What make k3s-setup does:
curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="--flannel-backend=none --disable-network-policy" sh -
kubectl apply -f https://raw.githubusercontent.com/projectcalico/calico/v3.31.5/manifests/calico.yaml
# Waits for cluster to become ready
```

> **Why Calico?** k3s ships with Flannel as default CNI. Flannel does **not** support NetworkPolicies, which are required for agent network isolation. Calico replaces Flannel and enforces the NetworkPolicies that prevent agents from reaching the internet directly.

#### Image Management

Images are built locally and imported directly into k3s (no remote registry required):

```bash
# Build all images
make build

# This runs:
# docker build -t egg-sandbox:latest sandbox/
# docker build -t egg-orchestrator:latest orchestrator/
# docker build -t egg-gateway:latest gateway/
# k3s ctr images import <image-tarballs>
```

### Configuration

1. **Initialize configuration:**
   ```bash
   bin/egg-deploy init
   ```
   This creates `~/.config/egg/config.yaml` with system defaults and generates a launcher secret.

2. **Set your GitHub token:**
   ```bash
   echo 'ghp_xxxxx' > ~/.config/egg/github-token
   chmod 600 ~/.config/egg/github-token
   ```
   Or add `GITHUB_USER_TOKEN=ghp_xxxxx` to `~/.config/egg/secrets.env`.

3. **Review settings** in `~/.config/egg/config.yaml` (host_home, host_uid, host_gid are auto-detected).

4. **Create repositories.yaml:**
   ```yaml
   github_username: your-github-username
   bot_username: your-bot-name  # Required for bot operations

   local_repos:
     paths:
       - /home/user/repos/my-project
   ```

### Deployment Commands

| Command | Description |
|---------|-------------|
| `make k3s-setup` | Install k3s + Calico CNI (idempotent) |
| `make deploy` | Deploy all k8s resources (`kubectl apply -k k8s/overlays/local/`) |
| `make build` | Build images and import into k3s |
| `make k3s-teardown` | Remove k3s installation |

### Network Topology

Kubernetes uses namespace separation and Calico NetworkPolicies for network isolation:

```
Namespace: egg-system                    Namespace: egg-agents
┌──────────────────────────┐            ┌───────────────────┐
│                          │            │                   │
│  orchestrator (:9849)    │            │  agent-coder      │
│         │                │            │       │           │
│         ▼                │            │       │ egress    │
│  gateway (:9848/:3129)   │◄───────────│───────┘ (only to  │
│         │                │            │         gateway)  │
│         │                │            │                   │
│         ▼                │            │  agent-tester     │
│    Squid Proxy           │◄───────────│───────┘           │
│         │                │            │                   │
└─────────┼────────────────┘            └───────────────────┘
          │
          ▼
       Internet (filtered by Squid allowlist)
```

- **egg-system namespace**: Orchestrator and gateway run as Deployments with Services
- **egg-agents namespace**: Agent containers run as Jobs with strict NetworkPolicies
- **NetworkPolicies**: Default-deny ingress and egress in `egg-agents`; agents can only reach the gateway Service
- **Gateway**: Only component with internet access, all traffic filtered through Squid proxy

### k3s on macOS

k3s is Linux-native. On macOS, use one of:

- **[Lima](https://lima-vm.io/)**: `limactl start --name=k3s template://k3s`
- **[Rancher Desktop](https://rancherdesktop.io/)**: Provides k3s in a managed VM
- **Docker Desktop with k3s**: Enable Kubernetes in Docker Desktop settings

```bash
# Start egg session
egg --public   # Public mode (full internet via proxy)
egg --private  # Private mode (Anthropic API only)

# Execute a one-off command
egg --exec claude --print "Fix the tests"
```

## GitHub Action Deployment

For CI/CD automation, use the egg GitHub Action:

```yaml
name: Run egg
on:
  issue_comment:
    types: [created]

jobs:
  egg:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: jwbron/egg@main
        with:
          prompt: "Fix the failing tests"
          anthropic-oauth-token: ${{ secrets.ANTHROPIC_OAUTH_TOKEN }}
          github-token: ${{ secrets.GITHUB_TOKEN }}
```

### Action Inputs

| Input | Required | Description |
|-------|----------|-------------|
| `prompt` | Yes* | Task prompt |
| `prompt-file` | Yes* | Path to file containing prompt |
| `anthropic-oauth-token` | Yes | Claude API authentication |
| `github-token` | Yes | GitHub API access |
| `mode` | No | Network mode: auto, public, private |
| `timeout` | No | Timeout in minutes (default: 30) |
| `image-tag` | No | Docker image version (default: latest) |

*Either `prompt` or `prompt-file` is required.

## Pre-built Images

Pre-built images are available on GHCR:

| Image | Description |
|-------|-------------|
| `ghcr.io/jwbron/egg-gateway:latest` | Gateway sidecar (latest build) |
| `ghcr.io/jwbron/egg-sandbox:latest` | Sandbox container (latest build) |

Images are built on every push to main and on releases.

### Image Versioning

egg follows [semantic versioning](https://semver.org/) with floating tags for stable releases:

| Tag Pattern | Description | Updates When |
|-------------|-------------|--------------|
| `latest` | Latest build from main | Every push to main and every stable release |
| `vX` | Major version (e.g., `v0`) | Every stable vX.y.z release |
| `vX.Y` | Minor version (e.g., `v0.1`) | Every stable vX.Y.z release |
| `vX.Y.Z` | Exact version (e.g., `v0.1.0`) | Never (immutable) |
| `vX.Y.Z-suffix` | Pre-release (e.g., `v1.0.0-alpha`) | Never (immutable, no floating tags) |

Pre-release versions (with suffixes like `-alpha`, `-beta`, `-rc`) do not update floating tags or `latest`.

For details on creating releases, see [RELEASING.md](../../RELEASING.md).

### Using Pre-built Images

For stability, pin to a major version in `~/.config/egg/config.yaml`:

```yaml
gateway_image: ghcr.io/jwbron/egg-gateway:v0
sandbox_image: ghcr.io/jwbron/egg-sandbox:v0
```

For full reproducibility, pin to an exact version:

```yaml
gateway_image: ghcr.io/jwbron/egg-gateway:v0.1.0
sandbox_image: ghcr.io/jwbron/egg-sandbox:v0.1.0
```

Or use `latest` for automatic updates (not recommended for production):

```yaml
gateway_image: ghcr.io/jwbron/egg-gateway:latest
sandbox_image: ghcr.io/jwbron/egg-sandbox:latest
```

For reproducible builds, pin to an exact version tag.

## Configuration Files

### Required Files

| File | Purpose |
|------|---------|
| `~/.config/egg/config.yaml` | Non-secret settings for compose |
| `repositories.yaml` | Repository configuration |

### Optional Files

| File | Purpose |
|------|---------|
| `secrets.env` | Additional secrets (GitHub App credentials) |
| `launcher-secret` | Gateway authentication token |

## Health Checks

The gateway exposes health endpoints on two ports:

- **Port 9851** — dedicated lightweight health check server. k8s liveness probes use this port so health checks are never blocked by long-running git operations on the main thread pool.
- **Port 9848** — full health endpoint with additional detail (active sessions, orchestrator process checks). Use this for manual diagnostics.

```bash
# Check gateway health via kubectl port-forward
kubectl port-forward -n egg-system svc/gateway 9848:9848
curl http://localhost:9848/api/v1/health

# Or from within the cluster
kubectl exec -n egg-system deploy/orchestrator -- curl http://gateway:9848/api/v1/health

# Expected response
{
  "status": "healthy",
  "github_token_valid": true,
  "auth_configured": true,
  "squid_proxy": {"running": true, "listening": true},
  "active_sessions": 0,
  "service": "gateway",
  ...
}
```

The `status` field is `"healthy"` only when all three conditions are met: the GitHub token is valid, the launcher secret is configured, and the Squid proxy is listening on port 3129. A Squid crash returns `"degraded"` and causes the k8s liveness probe to fail, triggering a pod restart.

The k8s Deployment includes liveness and readiness probes on port 9851:
- Period: 10 seconds
- Timeout: 5 seconds
- Failure threshold: 12
- Initial delay: 30 seconds

## Troubleshooting

### Claude binary not found

If the sandbox exits with `Claude Code CLI not found in PATH`, the Claude binary is missing from the container (failed build or changed install path).

Fix:
```bash
egg --reset
```

This clears cached images and rebuilds the sandbox with Claude Code installed.

### Gateway fails to start

1. Check k3s is running: `kubectl get nodes`
2. Check pod status: `kubectl get pods -n egg-system`
3. Check logs: `kubectl logs -n egg-system deploy/gateway`

**Network unavailable at startup**: The gateway retries GitHub App token initialization with exponential backoff for up to 120 seconds if the network is temporarily unavailable (e.g., DNS not yet ready). During this window you'll see log lines like `Token refresher not ready, retrying`. If the token never initializes within the timeout, the gateway exits with code 1. Increase the window with `EGG_TOKEN_INIT_TIMEOUT=<seconds>` if your network takes longer to come up.

**Missing or invalid credentials**: Configuration errors (missing key file, invalid credentials) are detected immediately and do not trigger retries. The gateway logs a warning and continues running, but GitHub operations will fail.

### Agent pod cannot reach gateway

1. Verify gateway is healthy: `kubectl get pods -n egg-system`
2. Check gateway Service exists: `kubectl get svc -n egg-system`
3. Check NetworkPolicies: `kubectl get networkpolicies -n egg-agents`
4. Test connectivity from agent namespace: `kubectl run -n egg-agents test --rm -it --image=busybox -- wget -qO- http://gateway.egg-system:9848/api/v1/health`

### Git operations fail

1. Verify GITHUB_USER_TOKEN is set
2. Check launcher-secret exists and matches
3. Verify session token in container: `echo $EGG_SESSION_TOKEN`

### Permission denied errors

1. Check HOST_UID/HOST_GID match your user: `id -u && id -g`
2. Ensure repositories directory is accessible
3. Check SELinux/AppArmor if on Linux

### Orchestrator refuses to start as root

If the orchestrator exits on startup with a root-related error, `HOST_UID` and `HOST_GID` are either not set or set to 0. You may see one of:

- `ERROR: running as root but HOST_UID/HOST_GID are not set.` — entrypoint cannot drop privileges
- `ERROR: HOST_UID must not be 0 (root).` — HOST_UID is explicitly set to 0
- `ERROR: orchestrator must not run as root.` — Python process is still running as root

The orchestrator requires these environment variables to drop privileges before starting. Running as root would create git artifacts with root:root ownership, breaking host git operations.

Fix:
```yaml
# In your ~/.config/egg/config.yaml
host_uid: 1000  # output of id -u
host_gid: 1000  # output of id -g
```

If `.git` directories already have root-owned files:
```bash
sudo chown -R $(id -u):$(id -g) ~/repos/*/.git
```

## Security Considerations

### Credentials

- Never commit `.env` or `secrets.env` to version control
- Use GitHub App authentication for production
- Rotate launcher secret periodically

### Network

- The gateway is the only component with external network access
- In private mode, only api.anthropic.com is accessible
- All outbound traffic from sandbox routes through gateway proxy

### Pod Security

- Agent pods run as non-root user matching host UID
- Git metadata is shadowed (emptyDir with `medium: Memory` on .git/)
- No credentials are passed to agent pod environment
- NetworkPolicies enforce egress-only-to-gateway isolation
- RBAC restricts orchestrator to Job/Pod management in `egg-agents` namespace only
