# Deployment Guide

This guide covers the various ways to deploy egg, from local development to production environments.

## Deployment Methods

egg supports multiple deployment methods depending on your use case:

| Method | Best For | Prerequisites |
|--------|----------|---------------|
| **egg CLI** | Local development (recommended) | Docker (for image builds), k3s |
| **Kubernetes (k3s)** | Local and production deployments | k3s, Calico CNI |
| **GitHub Action** | CI/CD automation | GitHub repository |

### Prerequisites by Platform

| Platform | Requirements | Notes |
|----------|-------------|-------|
| **Linux** | Docker Engine (for builds), k3s | Native performance. `make k3s-setup` installs k3s + Calico |
| **macOS** | [Docker Desktop](https://www.docker.com/products/docker-desktop/) | k3s runs in Docker Desktop's VM; enable "Use Rosetta for x86_64/amd64 emulation" on Apple Silicon for best compatibility |

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

## Kubernetes Deployment (k3s)

egg runs on Kubernetes using k3s for local development. The orchestrator and gateway run as Deployments in the `egg-system` namespace, while agent containers run as Jobs in the `egg-agents` namespace. Calico NetworkPolicies enforce network isolation.

### Quick Start

```bash
# Clone the repository
git clone https://github.com/jwbron/egg.git
cd egg

# Install k3s with Calico CNI
make k3s-setup

# Build images and import into k3s
make build

# Deploy egg to k3s
make deploy

# Verify everything is running
kubectl get pods -n egg-system
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

### Commands

| Command | Description |
|---------|-------------|
| `make k3s-setup` | Install k3s with Calico CNI |
| `make build` | Build images and import into k3s |
| `make deploy` | Deploy egg to k3s cluster |
| `make k3s-teardown` | Remove k3s and all resources |
| `kubectl get pods -n egg-system` | Check service status |
| `kubectl logs -n egg-system deploy/gateway` | View gateway logs |

### Network Topology

Kubernetes uses namespace-based isolation with Calico NetworkPolicies:

```
egg-agents namespace (default-deny)
┌──────────────────────────────────────────────────────┐
│  agent pods ──► egress only to gateway Service       │
│  (no internet, no inter-agent communication)         │
└──────────────────────┬───────────────────────────────┘
                       │
                       ▼
egg-system namespace
┌──────────────────────────────────────────────────────┐
│  gateway (Service)    orchestrator (Service)         │
│  Ports: 9848,         Port: 9849                     │
│  3129, 9851                                          │
│       │                                              │
│       ▼                                              │
│  Squid proxy ──► Internet (filtered by allowlist)    │
└──────────────────────────────────────────────────────┘
```

- **`egg-agents` namespace**: Default-deny ingress and egress. Agents can only reach the gateway Service
- **`egg-system` namespace**: Gateway and orchestrator run as Deployments with Services
- **Gateway**: Acts as the only egress point for agents, proxies all external traffic
- **Orchestrator**: Manages SDLC pipelines and spawns agent Jobs via k8s API

See [Kubernetes Architecture](../architecture/kubernetes.md) for the full design.

### CLI Modes

```bash
# Start gateway and sandbox manually
egg --public   # Public mode (full internet)
egg --private  # Private mode (API only)

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

You can also specify tags directly in a docker-compose.yml override.

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

- **Port 9851** — dedicated lightweight health check server. Kubernetes uses this port for liveness probes so health checks are never blocked by long-running git operations on the main thread pool.
- **Port 9848** — full health endpoint with additional detail (active sessions, orchestrator process checks). Use this for manual diagnostics.

```bash
# Check gateway health (manual diagnostics — from within the cluster)
kubectl exec -n egg-system deploy/gateway -- curl -s http://localhost:9848/api/v1/health

# Or port-forward for host access
kubectl port-forward -n egg-system svc/gateway 9848:9848
curl http://localhost:9848/api/v1/health

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

The `status` field is `"healthy"` only when all three conditions are met: the GitHub token is valid, the launcher secret is configured, and the Squid proxy is listening on port 3129. A Squid crash returns `"degraded"` and causes the Kubernetes liveness probe to fail, triggering a pod restart.

The Kubernetes Deployment includes liveness probes (on port 9851) with:
- 10 second period
- 5 second timeout
- 12 failure threshold
- 30 second initial delay

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

### Agent cannot reach gateway

1. Verify gateway is healthy: `kubectl get pods -n egg-system`
2. Check Calico is running: `kubectl get pods -n calico-system`
3. Check NetworkPolicies: `kubectl get networkpolicies -n egg-agents`
4. Check gateway Service: `kubectl get svc -n egg-system`

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

### Agent pod stuck in Pending

If agent Jobs are created but pods remain in `Pending`:
1. Check if images are imported: `k3s ctr images ls | grep egg`
2. Run `make build` to rebuild and import images
3. Check node resources: `kubectl describe node | grep -A 5 "Allocated resources"`

### Calico not running

If NetworkPolicies are not enforced (agents can reach the internet):
1. Check Calico pods: `kubectl get pods -n calico-system`
2. Re-install Calico: `scripts/install-calico.sh`
3. Verify k3s was installed without Flannel: check for `--flannel-backend=none` in k3s config

## Security Considerations

### Credentials

- Never commit `.env` or `secrets.env` to version control
- Use GitHub App authentication for production
- Rotate launcher secret periodically

### Network

- The gateway is the only component with external network access
- Calico NetworkPolicies enforce default-deny egress in the `egg-agents` namespace
- Agents can only reach the gateway Service — no direct internet access
- In private mode, only api.anthropic.com is accessible via the gateway proxy
- All outbound traffic from agents routes through the gateway's Squid proxy

### Container Security

- Agent pods run as non-root user matching host UID
- Git metadata is shadowed (tmpfs mount on .git/ via init container)
- No credentials are passed to agent pods
- RBAC restricts orchestrator to Job management in `egg-agents` only
