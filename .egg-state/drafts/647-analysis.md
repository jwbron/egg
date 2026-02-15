# Analysis: Set Up Docker-in-Docker Based Testing for Egg Orchestration

> Issue: #647 | Phase: refine

## Problem Statement

The egg orchestrator's local pipeline integration tests (`integration_tests/local_pipeline/`) currently mount the host Docker socket (`/var/run/docker.sock`) into the orchestrator container so it can spawn mock-sandbox containers during test runs. This creates a security concern: an untrusted orchestrator (or test code exercising orchestrator paths) has direct access to the host Docker daemon, which can manage any container on the host, not just test containers.

Issue #647 asks us to replace this host-socket mount with a Docker-in-Docker (DinD) sidecar so the orchestrator's Docker operations are sandboxed within an isolated Docker daemon. This is a prerequisite for building real integration/e2e tests (issue #645) where the orchestrator exercises DinD workloads like deployment validation stacks.

## Current Behavior

### Local Pipeline Test Stack

The local pipeline tests use a docker-compose stack defined at `integration_tests/local_pipeline/docker-compose.yml` (126 lines) that runs:

- **Gateway** — Policy enforcement API (172.40.0.2) with Squid proxy
- **Orchestrator** — Pipeline orchestration API (172.40.0.3) with Docker socket access

The orchestrator mounts the host Docker socket directly:

```yaml
# integration_tests/local_pipeline/docker-compose.yml:88
volumes:
  - /var/run/docker.sock:/var/run/docker.sock
```

And sets `DOCKER_HOST=unix:///var/run/docker.sock` (line 72).

### DockerClient Connection

The orchestrator's `DockerClient` (`orchestrator/docker_client.py:95-109`) already supports connecting via either a Unix socket or a TCP-based `DOCKER_HOST`:

```python
def __init__(self, docker_host: str | None = None):
    self.docker_host = docker_host or os.environ.get("DOCKER_HOST")
    if self.docker_host:
        self.client = docker.DockerClient(base_url=self.docker_host)
    else:
        self.client = docker.from_env()
```

This means switching from `unix:///var/run/docker.sock` to `tcp://dind:2375` requires **no code changes** to the Docker client — only a configuration change in the compose file and environment variables.

### Test Fixture Setup

The session-scoped `local_pipeline_stack` fixture in `integration_tests/local_pipeline/conftest.py`:

1. Builds the mock-sandbox image (`docker build -t mock-sandbox:latest`)
2. Starts the compose stack (`docker compose up -d --build`)
3. Waits for gateway and orchestrator health checks
4. Yields `LocalPipelineStack` with URLs and config
5. Tears down on cleanup

The mock-sandbox image is built against the **host Docker daemon** and then used by the orchestrator (also via the host daemon) to spawn test containers. When switching to DinD, the mock-sandbox image must be loaded into the DinD daemon instead.

### CI Integration

The GitHub Actions workflow (`.github/workflows/test-integration.yml`) currently runs only the gateway-focused integration tests. The local pipeline tests (which require both gateway and orchestrator with Docker access) do not appear to run in CI — they're run locally via `make test-integration` or directly with pytest. Issue #647 would enable these tests to also run safely in CI.

## Constraints

- **No orchestrator code changes**: The `DockerClient` already supports `DOCKER_HOST` via environment variable. The switch is purely infrastructure/compose configuration.
- **Image loading**: The mock-sandbox image must be available inside the DinD daemon. Since it's built locally (not from a registry), it needs to be transferred via `docker save | docker load` or built inside DinD.
- **Kernel compatibility**: The host is Linux 6.17 aarch64 (Asahi/Fedora). The DinD sidecar requires `--privileged` and `--device /dev/net/tun`. Both rootless and standard DinD require `--privileged` on the outer container.
- **Network topology**: Spawned containers must join the test network (`egg-lp-test-isolated`) so the gateway can reach them. When using DinD, the orchestrator communicates with the DinD daemon over TCP, and the DinD daemon creates containers. These DinD-spawned containers live inside the DinD namespace, which affects network visibility.
- **Volume sharing**: The orchestrator shares volumes (state, worktrees) with spawned containers. With DinD, the spawned containers run inside the DinD daemon's namespace, so volume mounts reference paths _inside_ the DinD container, not on the host.
- **Startup time**: DinD adds ~5s startup overhead while the inner Docker daemon initializes.
- **Foundation for #645**: This DinD setup will also serve as the foundation for deployment validation (issue #645), where the orchestrator runs `docker compose up` to bring up target application devserver stacks.

## Options Considered

### Option A: Rootless DinD Sidecar (Validated)

**Approach**: Add a `docker:27-dind-rootless` service to the test compose stack. The orchestrator connects via `DOCKER_HOST=tcp://dind:2375` instead of mounting the host socket. The mock-sandbox image is loaded into DinD at test setup time via `docker save | docker load`.

```yaml
services:
  dind:
    image: docker:27-dind-rootless
    privileged: true
    devices:
      - /dev/net/tun
    environment:
      - DOCKER_TLS_CERTDIR=
    networks:
      egg-test-isolated:
    healthcheck:
      test: ["CMD", "docker", "info"]
      interval: 5s
      timeout: 3s
      retries: 10

  orchestrator:
    environment:
      - DOCKER_HOST=tcp://dind:2375
    # NO docker.sock mount
```

**Pros**:
- Validated on the target host (Linux 6.17 aarch64, overlay2 storage driver)
- Full container lifecycle works over TCP: create, start, logs, stop, remove
- Image loading via `docker save | docker load` works
- Rootless runs the inner daemon as non-root, reducing blast radius if the orchestrator compromises the DinD daemon
- No orchestrator code changes needed — `DockerClient` already supports `DOCKER_HOST`
- Removes the host Docker socket from the test stack entirely

**Cons**:
- ~5s slower startup than socket mount (rootless daemon initialization)
- Requires `--privileged` on the DinD container (both rootless and standard DinD need this)
- Network topology is more complex: DinD-spawned containers live in DinD's network namespace, requiring careful network configuration for gateway connectivity
- Volume sharing between orchestrator and DinD-spawned containers requires coordinated mount paths
- Image transfer step adds complexity to test fixture setup

### Option B: Standard (Root) DinD Sidecar

**Approach**: Same as Option A but using `docker:27-dind` (standard, root-mode DinD) instead of rootless.

**Pros**:
- Slightly faster startup (~2-3s less than rootless)
- Simpler daemon configuration (no user namespace remapping)
- Wider community usage and documentation

**Cons**:
- Inner Docker daemon runs as root — larger blast radius if compromised
- Still requires `--privileged` on the DinD container
- Same network and volume complexity as Option A
- Security posture is weaker than rootless for the same operational complexity

### Option C: Keep Host Socket Mount (Status Quo)

**Approach**: Continue mounting `/var/run/docker.sock` into the orchestrator container.

**Pros**:
- Already working — no changes needed
- Simplest network topology (all containers share the host Docker namespace)
- No image transfer needed (host daemon has the image already)

**Cons**:
- Untrusted orchestrator has access to the full host Docker daemon
- Can manage any container on the host, not just test containers
- Not suitable for CI environments where other containers may be running
- Does not address the security concern raised in issue #647
- Does not establish the DinD pattern needed for deployment validation (#645)

### Option D: Docker Socket Proxy (e.g., Tecnativa/docker-socket-proxy)

**Approach**: Place a filtering proxy between the orchestrator and the host Docker socket. The proxy restricts which Docker API endpoints the orchestrator can call (e.g., only container operations, no volume/network/exec).

**Pros**:
- Keeps the simpler network topology of the host daemon
- Reduces blast radius without full DinD overhead
- No image transfer needed

**Cons**:
- Still connected to the host Docker daemon — can see and potentially interact with host containers
- Does not establish the DinD pattern needed for #645
- Additional component to configure and maintain
- The proxy's allowlist must be carefully tuned — too permissive and it's ineffective, too restrictive and tests break

## Recommended Approach

**Option A: Rootless DinD Sidecar.** This is the validated approach (confirmed working on the target host per the issue comment) and provides the best security posture. The ~5s startup overhead is negligible for integration tests. The rootless variant reduces blast radius compared to standard DinD for marginal additional complexity.

Key implementation considerations:

1. **Network topology**: The DinD-spawned containers (mock-sandbox instances) need to be reachable by the gateway for session registration and policy enforcement. Since DinD containers live inside the DinD daemon's namespace, the orchestrator's `EGG_ISOLATED_NETWORK` and related network configuration must be adapted. The DinD sidecar itself needs to be on the test network, and spawned containers need to be connected to that network (which the DinD daemon can do since it has access to the Docker network primitives within its scope).

2. **Image loading**: The conftest fixture currently builds the mock-sandbox image on the host daemon. With DinD, it must either: (a) build on the host then `docker save | docker -H tcp://dind:2375 load`, or (b) build directly inside DinD. Option (a) is validated and simpler.

3. **Volume sharing**: The orchestrator and DinD-spawned containers share named volumes (state, worktrees). Since both the orchestrator and DinD run in the same compose stack, shared named volumes work naturally — Docker maps them to the same underlying host path. The key constraint is that volume mount paths inside DinD-spawned containers must reference paths as they appear inside the DinD container, not host paths.

4. **Host repo map**: The orchestrator uses `EGG_HOST_REPO_MAP` to translate host paths to container mount paths for spawned containers. With DinD, the "host" from the spawned container's perspective is the DinD daemon, so this mapping needs to reference paths inside the DinD container (or shared volume mount points).

## Open Questions

1. **Network bridge sharing**: Can the DinD daemon create containers on Docker networks defined by the outer compose stack, or does it have its own isolated network namespace? If isolated, how do DinD-spawned containers communicate with the gateway (172.40.0.2)?

2. **CI runner privileges**: GitHub Actions runners support `--privileged` Docker containers. Should we add the local pipeline tests to the CI integration test workflow now, or defer to a follow-up?

---

*Authored-by: egg*
