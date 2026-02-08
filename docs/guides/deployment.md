# Deployment Guide

This guide covers the various ways to deploy egg, from local development to production environments.

## Deployment Methods

egg supports multiple deployment methods depending on your use case:

| Method | Best For | Prerequisites |
|--------|----------|---------------|
| **Docker Compose** | Production, local development | Docker, Docker Compose |
| **egg CLI** | Quick local testing | Docker |
| **GitHub Action** | CI/CD automation | GitHub repository |
| **Launcher Container** | Single-container deployment | Docker |

## Docker Compose (Recommended)

The recommended deployment method uses Docker Compose to manage the gateway stack.

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
   # Generate a launcher secret
   EGG_LAUNCHER_SECRET=$(openssl rand -hex 32)

   # Set your GitHub token
   GITHUB_USER_TOKEN=ghp_xxxxx

   # Set your user identity
   HOST_UID=$(id -u)
   HOST_GID=$(id -g)
   ```

3. **Create repositories.yaml:**
   ```yaml
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
                       │         │
                       └─────────┼──▶ egg-external
                                 │         │
                                 ▼         ▼
                            API + Proxy   Internet
```

- **egg-isolated**: Internal network with no external route
- **egg-external**: Standard bridge network with internet access
- **Gateway**: Dual-homed, acts as the only egress point for sandboxes

## CLI-Based Deployment

For quick local testing, use the `egg` CLI directly:

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

## Launcher Container (Future)

A single-container deployment option is planned that encapsulates the gateway and sandbox management:

```bash
# Pull and run the launcher
docker run -it \
  -v ~/.config/egg:/config:ro \
  -v ~/repos:/repos \
  -v /var/run/docker.sock:/var/run/docker.sock \
  ghcr.io/jwbron/egg-launcher:latest
```

This will:
1. Create the necessary networks on the host Docker
2. Start the gateway container
3. Start the sandbox with proper configuration
4. Forward stdin/stdout for interactive use
5. Clean up on exit

## Pre-built Images

Pre-built images are available on GHCR:

| Image | Description |
|-------|-------------|
| `ghcr.io/jwbron/egg-gateway:latest` | Gateway sidecar |
| `ghcr.io/jwbron/egg-sandbox:latest` | Sandbox container |
| `ghcr.io/jwbron/egg-launcher:latest` | Launcher container (future) |

Images are built on every push to main and on releases.

### Using Pre-built Images

In your `.env` file:

```bash
EGG_GATEWAY_IMAGE=ghcr.io/jwbron/egg-gateway:latest
EGG_SANDBOX_IMAGE=ghcr.io/jwbron/egg-sandbox:latest
```

Or specify directly in docker-compose.yml override.

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
