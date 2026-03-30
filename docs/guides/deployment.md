# Deployment Guide

This guide covers the various ways to deploy egg, from local development to production environments.

## Deployment Methods

egg supports multiple deployment methods depending on your use case:

| Method | Best For | Prerequisites |
|--------|----------|---------------|
| **egg CLI** | Local development (recommended) | Docker |
| **Docker Compose** | Production, advanced deployments | Docker, Docker Compose |
| **GitHub Action** | CI/CD automation | GitHub repository |

### Prerequisites by Platform

| Platform | Docker | Notes |
|----------|--------|-------|
| **Linux** | Docker Engine + Compose v2 | Native performance |
| **macOS** | [Docker Desktop](https://www.docker.com/products/docker-desktop/) | Ensure Docker Desktop is running; enable "Use Rosetta for x86_64/amd64 emulation" on Apple Silicon for best compatibility |

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

## Docker Compose (Advanced)

For production deployments or managing the gateway stack separately, use Docker Compose.

### Quick Start

```bash
# Clone the repository
git clone https://github.com/jwbron/egg.git
cd egg

# Initialize configuration
bin/egg-deploy init

# Review and edit configuration
vim ~/.config/egg/config.yaml

# Start the gateway
bin/egg-deploy up

# Start a sandbox session
egg --public
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
| `bin/egg-deploy init` | Generate initial configuration |
| `bin/egg-deploy up` | Start the gateway stack |
| `bin/egg-deploy down` | Stop the gateway stack |
| `bin/egg-deploy status` | Show container status and health |
| `bin/egg-deploy logs` | Follow gateway logs |
| `bin/egg-deploy build` | Rebuild images |

### Network Topology

Docker Compose creates a dual-network architecture:

```
sandbox (172.32.0.x) ──┐
                       │
                       ├──▶ egg-isolated (internal)
                       │         │
                       │         ▼
                       │    gateway (172.32.0.2)
                       │    orchestrator (172.32.0.3)
                       │         │
                       └─────────┼──▶ egg-external
                                 │         │
                                 ▼         ▼
                            API + Proxy   Internet
```

- **egg-isolated**: Internal network with no external route
- **egg-external**: Standard bridge network with internet access
- **Gateway**: Dual-homed, acts as the only egress point for sandboxes
- **Orchestrator**: Dual-homed, manages SDLC pipelines and spawns sandbox containers

## CLI with Docker Compose Gateway

To use the `egg` CLI with a separately-managed Docker Compose gateway:

### Using --compose Mode

```bash
# Start gateway via compose, then launch sandbox (auto-rebuilds when code changes)
egg --compose

# Stop the compose stack
egg --compose --down
```

### Traditional Mode

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

- **Port 9851** — dedicated lightweight health check server. Docker Compose uses this port for liveness probes so health checks are never blocked by long-running git operations on the main thread pool.
- **Port 9848** — full health endpoint with additional detail (active sessions, orchestrator process checks). Use this for manual diagnostics.

```bash
# Check gateway health (manual diagnostics)
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

The `status` field is `"healthy"` only when all three conditions are met: the GitHub token is valid, the launcher secret is configured, and the Squid proxy is listening on port 3129. A Squid crash returns `"degraded"` and causes Docker's health check to fail, triggering a container restart.

The Docker Compose configuration includes automatic health checks (on port 9851) with:
- 10 second interval
- 5 second timeout
- 12 retries
- 30 second start period

## Troubleshooting

### Claude binary not found

If the sandbox exits with `Claude Code CLI not found in PATH`, the Claude binary is missing from the container (failed build or changed install path).

Fix:
```bash
egg --reset
```

This clears cached images and rebuilds the sandbox with Claude Code installed.

### Gateway fails to start

1. Check Docker is running: `docker info`
2. Check port availability: `lsof -i :9848; lsof -i :9851  # main + health-check ports`
3. Check logs: `bin/egg-deploy logs`

**Network unavailable at startup**: The gateway retries GitHub App token initialization with exponential backoff for up to 120 seconds if the network is temporarily unavailable (e.g., DNS not yet ready). During this window you'll see log lines like `Token refresher not ready, retrying`. If the token never initializes within the timeout, the gateway exits with code 1. Increase the window with `EGG_TOKEN_INIT_TIMEOUT=<seconds>` if your network takes longer to come up.

**Missing or invalid credentials**: Configuration errors (missing key file, invalid credentials) are detected immediately and do not trigger retries. The gateway logs a warning and continues running, but GitHub operations will fail.

### Sandbox cannot reach gateway

1. Verify gateway is healthy: `bin/egg-deploy status`
2. Check network exists: `docker network ls | grep egg`
3. Check gateway IP: `docker inspect egg-gateway --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'`

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

### Container Security

- Sandbox runs as non-root user matching host UID
- Git metadata is shadowed (tmpfs mount on .git/)
- No credentials are passed to sandbox environment
