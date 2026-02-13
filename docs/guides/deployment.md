# Deployment Guide

This guide covers the various ways to deploy egg, from local development to production environments.

## Deployment Methods

egg supports multiple deployment methods depending on your use case:

| Method | Best For | Prerequisites |
|--------|----------|---------------|
| **egg CLI** | Local development (recommended) | Docker |
| **Docker Compose** | Production, advanced deployments | Docker, Docker Compose |
| **GitHub Action** | CI/CD automation | GitHub repository |

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

# Edit .env with your credentials
vim .env

# Start the gateway
bin/egg-deploy up

# Start a sandbox session
egg --public
```

### Configuration

1. **Copy the environment template:**
   ```bash
   cp .env.example .env
   ```

2. **Configure required variables:**
   ```bash
   # Generate a session secret
   EGG_LAUNCHER_SECRET=$(openssl rand -hex 32)

   # Set your GitHub token
   GITHUB_USER_TOKEN=ghp_xxxxx

   # Set your user identity and home directory
   HOST_UID=$(id -u)
   HOST_GID=$(id -g)
   HOST_HOME=$HOME  # REQUIRED: orchestrator mounts $HOST_HOME/.egg-worktrees to read pipeline artifacts
   ```

3. **Create repositories.yaml:**
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
# Start gateway via compose, then launch sandbox
egg --compose

# Stop the compose stack
egg --compose --down

# Rebuild and start
egg --compose --build
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

For stability, pin to a major version in your `.env` file:

```bash
EGG_GATEWAY_IMAGE=ghcr.io/jwbron/egg-gateway:v0
EGG_SANDBOX_IMAGE=ghcr.io/jwbron/egg-sandbox:v0
```

For full reproducibility, pin to an exact version:

```bash
EGG_GATEWAY_IMAGE=ghcr.io/jwbron/egg-gateway:v0.1.0
EGG_SANDBOX_IMAGE=ghcr.io/jwbron/egg-sandbox:v0.1.0
```

Or use `latest` for automatic updates (not recommended for production):

```bash
EGG_GATEWAY_IMAGE=ghcr.io/jwbron/egg-gateway:latest
EGG_SANDBOX_IMAGE=ghcr.io/jwbron/egg-sandbox:latest
```

You can also specify tags directly in a docker-compose.yml override.

## Configuration Files

### Required Files

| File | Purpose |
|------|---------|
| `.env` | Environment variables for compose |
| `repositories.yaml` | Repository configuration |

### Optional Files

| File | Purpose |
|------|---------|
| `secrets.env` | Additional secrets (GitHub App credentials) |
| `launcher-secret` | Gateway authentication token |

## Health Checks

The gateway exposes a health endpoint:

```bash
# Check gateway health
curl http://localhost:9848/api/v1/health

# Expected response
{"status": "healthy", "timestamp": "..."}
```

The Docker Compose configuration includes automatic health checks with:
- 10 second interval
- 5 second timeout
- 12 retries
- 30 second start period

## Troubleshooting

### Gateway fails to start

1. Check Docker is running: `docker info`
2. Check port availability: `lsof -i :9848`
3. Check logs: `bin/egg-deploy logs`

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
```bash
# In your .env file (or hardcode the output of id -u / id -g)
HOST_UID=$(id -u)
HOST_GID=$(id -g)
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
