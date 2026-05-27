# Deployment Guide

This guide covers the various ways to deploy egg, from local development to production environments.

## Deployment Methods

egg supports two deployment methods depending on your use case:

| Method | Best For | Prerequisites |
|--------|----------|---------------|
| **Kubernetes (k3s)** via `bin/egg-deploy` | Local development and production deployments | k3s + Cilium CNI |
| **GitHub Action** | CI/CD automation | GitHub repository |

> **Removal note:** The legacy `egg` CLI / `bin/egg` interactive mode
> and its Docker Compose fallback were removed in
> [#1762](https://github.com/jwbron/egg/issues/1762). All deployments
> now use `bin/egg-deploy` against k3s.

### Prerequisites by Platform

| Platform | Runtime | Notes |
|----------|---------|-------|
| **Linux** | k3s (native) | `make k3s-setup` handles installation |
| **macOS** | k3s via Lima or Rancher Desktop | Requires a Linux VM; see [k3s on macOS](#k3s-on-macos) |

> **Migration note:** egg previously used Docker Compose for deployments. As of [#1553](https://github.com/jwbron/egg/issues/1553), all container management uses Kubernetes via k3s. See [Kubernetes Migration](../architecture/kubernetes-migration.md) for architecture details.

## Kubernetes (k3s) Deployment

egg runs on Kubernetes using k3s for local development. The orchestrator and gateway run as Deployments in the `egg-system` namespace, and agent containers run as Jobs in the `egg-agents` namespace.

### Quick Start

```bash
# Clone the repository
git clone https://github.com/jwbron/egg.git
cd egg

# Install k3s with Cilium CNI
make k3s-setup

# Build and import images into k3s
make build

# Deploy egg to the cluster
make deploy

# Verify everything is running
kubectl get pods -n egg-system
```

### Setup Details

#### k3s Installation

`make k3s-setup` installs k3s with Flannel disabled (required for NetworkPolicy support) and installs Cilium CNI:

```bash
# What make k3s-setup does:
curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="--flannel-backend=none --disable-network-policy --disable=metrics-server --write-kubeconfig-mode=644" sh -
scripts/install-cilium.sh   # downloads cilium-cli and runs `cilium install`
# Waits for cluster to become ready
```

> **Why Cilium?** k3s ships with Flannel as default CNI. Flannel does **not** support NetworkPolicies, which are required for agent network isolation. Cilium replaces Flannel and enforces the NetworkPolicies that prevent agents from reaching the internet directly. Calico filled this role until [#2703](https://github.com/jwbron/egg/issues/2703) — see that issue for the swap rationale.
>
> **Why `--disable=metrics-server`?** Under Cilium, the metrics-server pod cannot reach the kubelet on the node IP, so it never becomes Ready. The resulting perpetually-unavailable `v1beta1.metrics.k8s.io` APIService causes the namespace controller's discovery step to fail, which wedges all namespace deletion (namespaces become stuck in `Terminating` indefinitely). egg does not use metrics-server; disabling it avoids this hang with no functional loss.
>
> **Migrating from a pre-#2703 install:** in-place CNI swap on a live k3s cluster is not supported (host CNI binaries, conflists, CRDs, `tunl0`, and per-pod veth pairs persist after deleting the calico-node DaemonSet). Run `make k3s-teardown && make k3s-setup` for a clean install. `install-cilium.sh` will refuse if it detects leftover Calico state.
>
> **Migrating from a pre-#2713 install:** `install-cilium.sh` chains the portmap CNI plugin (`cni.chainingMode=portmap`) for hostPort support and installs a pod-egress MASQUERADE iptables rule that Cilium omits in chained mode. If the running cluster predates #2713, `install-cilium.sh` will exit with a `cni-chaining-mode` mismatch error and instruct you to run `make k3s-teardown && make k3s-setup` — the chainingMode cannot be changed on a live cluster.
>
> **After a host reboot:** re-run `scripts/install-cilium.sh` (or `make k3s-setup`) to restore the pod-egress MASQUERADE iptables rule. This rule is not persisted across reboots. Without it, pod-to-external traffic (gateway → GitHub, sandbox agents → Anthropic API) silently fails while intra-cluster traffic continues working. On long-running hosts where re-running after every reboot is painful, wire the rule into `netfilter-persistent`/`iptables-restore` at the system level instead.

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
   This creates `~/.config/egg/config.yaml` with system defaults and generates a `launcher-secret`.

   > **Note:** `bin/egg-deploy init` only generates the `launcher-secret`. The `lifecycle-secret` (required for HITL resolve, pipeline CRUD, and phase-control endpoints) must be generated manually — the legacy `egg --setup` wizard that auto-generated it was removed in [#1762](https://github.com/jwbron/egg/issues/1762):
   > ```bash
   > openssl rand -hex 32 > ~/.config/egg/lifecycle-secret && chmod 600 ~/.config/egg/lifecycle-secret
   > ```

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
| `make k3s-setup` | Install k3s + Cilium CNI (idempotent) |
| `make deploy` | Deploy all k8s resources via Kustomize + `envsubst` (see [details below](#make-deploy-details)) |
| `make build` | Build images and import into k3s |
| `make litellm-config` | Apply host-side LiteLLM `model_list` from `~/.config/egg/litellm-models.yaml`; no-op if absent |
| `make k3s-teardown` | Remove k3s installation |

#### `make deploy` details

`make deploy` runs `envsubst` to expand two variables into the Kustomize output before applying:

- **`EGG_HOST_HOME`** — defaults to `$HOME`.
- **`EGG_HOST_REPO_MAP`** — auto-derived from `~/.config/egg/repositories.yaml` via `scripts/build-host-repo-map.py`.

**Prerequisite:** `envsubst` from GNU gettext (`dnf install gettext` / `brew install gettext`).

`make deploy` also invokes `make litellm-config` automatically at the end, applying any
host-side LiteLLM backend overlay from `~/.config/egg/litellm-models.yaml` (no-op if the file is absent).
See the [Per-Agent Models guide](per-agent-models.md) for the overlay format.

If you invoke `make litellm-config` standalone before the cluster has been deployed, the target
also short-circuits with a notice when the in-cluster `litellm-config` ConfigMap is not yet present —
run `make deploy` first so the base ConfigMap exists, then re-run `make litellm-config` to overlay.

**Override at deploy time:**

```bash
make deploy EGG_HOST_HOME=/data/egg
EGG_HOST_REPO_MAP='{"owner/repo":"/path"}' make deploy
```

### Network Topology

Kubernetes uses namespace separation and NetworkPolicies (enforced by Cilium) for network isolation:

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

Once deployed, interact with egg through the MCP server (port 9850) from any MCP-compatible client.

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

### Limitations of Pre-built Images

Pre-built `egg-sandbox` images on GHCR are built without a host
`repositories.yaml`, so per-repo `build_commands` (e.g. `make sandbox-deps`
to populate `.venv`, `npm ci` to populate `node_modules`) are **not**
applied to the published image. Operators relying on prebuilt deps —
including the project's own `.venv` for `make test` / `make lint` — should
build the image locally with `make build` and import it via
`make k3s-import` rather than pulling from GHCR. `make build` runs
`scripts/prepare-sandbox-build-context.py` to populate `repo-deps/` from
your `repositories.yaml` before the Docker build, so per-repo deps are
included (see #2499).

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
| `lifecycle-secret` | Orchestrator lifecycle-control auth token (required for k8s deployments) |
| `litellm-models.yaml` | Host-side LiteLLM `model_list` overlay; copy from `config/litellm-models.template.yaml` (see [Per-Agent Models](per-agent-models.md)) |

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

### Orchestrator health

The orchestrator exposes three probe endpoints on port 9849. Each has a single, narrow job — the split (introduced in [#2191](https://github.com/jwbron/egg/issues/2191)) keeps kubelet probe latency decoupled from in-flight workload latency under burst BRC load.

| Endpoint | Purpose | Wired to | Cost on request path |
|----------|---------|----------|----------------------|
| `GET /api/v1/live` | Process liveness | kubelet `livenessProbe` | Pure JSON return |
| `GET /api/v1/ready` | Traffic-routing readiness — flips when state-store cache is stale or unhealthy | kubelet `readinessProbe`, `startupProbe` | Single dict read |
| `GET /api/v1/health` | Rich operator/dashboard payload (status, components, transitions) | `mcp__egg__check_health`, manual diagnostics | Single dict read |

All three serve cached values populated by a background thread (`orchestrator/state_store_probe.py`). The thread runs the curative state-store probe on a fixed cadence (default 15s) and stores the result in a thread-safe cache; request handlers never run `git`, never call `get_state_store()`, and never block on locks held by long-polls.

`/api/v1/health` always returns HTTP 200 — degraded status is signaled only via the `status` field in the JSON body, so a `degraded` response does not trigger a pod restart. Branch on the `status` field, not the HTTP code:

```bash
kubectl exec -n egg-system deploy/orchestrator -- curl -s http://localhost:9849/api/v1/health
```

Normal response:

```json
{
  "status": "healthy",
  "service": "egg-orchestrator",
  "components": {
    "state_store": {"/home/egg/repos/egg": {"status": "ok"}},
    "state_store_summary": "ok",
    "docker": "unknown"
  },
  "healthy_since": "2026-04-27T12:00:00+00:00",
  "probe": {"fresh": true, "age_seconds": 4.2}
}
```

`components.state_store` is a per-repo map keyed by repo path (#2176), so multi-repo deployments surface every wedged repo in a single response rather than just the first one the probe loop hit. Each value is `{"status": "ok"}` or `{"status": "error", "error": "<git error>"}`. `components.state_store_summary` is the human-readable aggregate (`"ok"`, `"probe-skipped: ..."`, or `"N/M repos wedged: <paths>"`) — useful for log lines and skip cases where the per-repo map is empty.

`healthy_since` is the timestamp of the most recent healthy → unhealthy → healthy transition (or process start if the orchestrator has been healthy since boot); use it to distinguish "stable since boot" from "just recovered." Transitions are recorded at BG-thread cadence (every 15s by default, tunable via `EGG_ORCH_STATE_STORE_PROBE_INTERVAL`) via the probe's `on_observation` callback, and `/api/v1/health` request hits also drive the tracker on the staleness-corrected value — so wedge cycles between sporadic operator/dashboard hits are still observed, and a wedged BG thread can surface as an unhealthy transition that the BG itself cannot record. `probe.age_seconds` is how long ago the background probe last ran — values consistently above ~2× the probe interval indicate the BG thread itself has wedged, and `/api/v1/ready` will flip to 503 even if the cached observation was healthy.

If the state-store worktree is wedged (for example, after a state-volume reset that left a stale `.git/worktrees/` admin dir), `status` becomes `"degraded"` and `components.state_store[<repo>]["error"]` shows the underlying git error for each wedged repo (`components.state_store_summary` carries the aggregate "N/M repos wedged: <paths>" string). The probe is **curative**: each tick of the background thread attempts to remove the stale admin dir and retry `git worktree add` on **every** wedged repo, so a wedge on repo A no longer hides an independent wedge on repo B. The curative cadence (15s) is now independent of kubelet probe traffic — operators do not need to do anything; under normal conditions the orchestrator self-heals within a few BG-thread cycles. See [Orchestrator status: degraded](#orchestrator-status-degraded-state-store-wedge) if degraded persists.

## Troubleshooting

> **Tip**: For most deployment failures, start with the two diagnostic
> skills rather than raw `kubectl`:
>
> - [`/deployment-diagnose`](../../skills/deployment-diagnose/SKILL.md) —
>   control-plane triage (`egg-orchestrator` + `egg-gateway`). Produces
>   a prioritized report with the Top finding first.
> - [`/agent-diagnose <pipeline_id> <container_id>`](../../skills/agent-diagnose/SKILL.md) —
>   per-agent-pod triage with a pattern-matched error classifier.
>
> The skills compose six k8s-facing MCP tools documented in the
> [MCP Deployment Tools reference](../reference/mcp-deployment-tools.md).
> See the [Deployment Diagnostics guide](deployment-diagnostics.md) for
> when to use which skill, evidence boundaries, and the redaction
> guarantee. The manual steps below remain useful as a fallback when the
> skills are unavailable.

### Claude binary not found

If the sandbox exits with `Claude Code CLI not found in PATH`, the Claude binary is missing from the container (failed build or changed install path).

Fix: rebuild and re-import the sandbox image. The legacy `egg --reset` shortcut was removed in [#1762](https://github.com/jwbron/egg/issues/1762); run the underlying commands directly:

```bash
make build         # rebuild egg-sandbox / egg-orchestrator / egg-gateway images
make k3s-import    # import rebuilt images into k3s
make deploy        # roll out deployments in egg-system
```

### Gateway fails to start

1. Check k3s is running: `kubectl get nodes`
2. Check pod status: `kubectl get pods -n egg-system`
3. Check logs: `kubectl logs -n egg-system deploy/gateway`

**Network unavailable at startup**: The gateway retries GitHub App token initialization with exponential backoff for up to 120 seconds if the network is temporarily unavailable (e.g., DNS not yet ready). During this window you'll see log lines like `Token refresher not ready, retrying`. If the token never initializes within the timeout, the gateway exits with code 1. Increase the window with `EGG_TOKEN_INIT_TIMEOUT=<seconds>` if your network takes longer to come up.

**Missing `EGG_ORCHESTRATOR_URL` in Kubernetes** (#1803): When `KUBERNETES_SERVICE_HOST` is present (i.e. the gateway is running inside a k8s pod), `EGG_ORCHESTRATOR_URL` must be explicitly set — the docker-compose default hostname `egg-orchestrator` doesn't resolve under k3s kube-dns. The `k8s/base/gateway-deployment.yaml` sets this automatically to `http://orchestrator.egg-system.svc.cluster.local:9849`. Custom overlays that omit this variable will cause the gateway to exit at startup with:
```
Startup failed: EGG_ORCHESTRATOR_URL must be set when running in Kubernetes
```
Fix by adding the env var to your gateway Deployment:
```yaml
- name: EGG_ORCHESTRATOR_URL
  value: "http://orchestrator.egg-system.svc.cluster.local:9849"
```

**Missing or invalid credentials**: Configuration errors (missing key file, invalid credentials) are detected immediately and do not trigger retries. The gateway logs a warning and continues running, but GitHub operations will fail.

### Pipeline submission returns 503 (gateway not ready)

On fresh deploys or pod restarts the orchestrator may be accepting requests while the gateway HTTP listener is still coming up. Without a readiness gate, the first pipeline submission would proceed, reach the gateway during worktree creation or per-agent fan-out, and cascade into per-agent `ConnectionRefused` errors that are hard to diagnose.

The orchestrator now waits for the gateway to become healthy before creating a pipeline. If the gateway doesn't become healthy within the timeout, the API returns HTTP 503 with a `Retry-After` header and a `gateway_not_ready` reason code:

```json
{
  "success": false,
  "message": "Gateway not ready after 60s (status=unreachable): Connection refused. Retry once the gateway has finished starting up.",
  "details": {
    "reason": "gateway_not_ready",
    "gateway_status": "unreachable",
    "gateway_error": "Connection refused",
    "timeout_seconds": 60
  }
}
```

> **Note:** `gateway_error` is `null` when the gateway reports unhealthy without a specific error string. The example above shows the most common case (`Connection refused` during startup).

**To tune the wait**: set `EGG_GATEWAY_READY_TIMEOUT_SECONDS` on the orchestrator (default: `60`). Increase it if your gateway routinely takes longer than 60 seconds to start under load:

```yaml
# In your orchestrator Deployment
- name: EGG_GATEWAY_READY_TIMEOUT_SECONDS
  value: "120"
```

**To disable the gate**: set `EGG_GATEWAY_READY_TIMEOUT_SECONDS=0`. Use this only in environments where gateway startup is managed out-of-band (e.g., integration test harnesses that ensure the gateway is healthy before submitting pipelines).

### Agent pod cannot reach gateway

1. Verify gateway is healthy: `kubectl get pods -n egg-system`
2. Check gateway Service exists: `kubectl get svc -n egg-system`
3. Check NetworkPolicies: `kubectl get networkpolicies -n egg-agents`
4. Test connectivity from agent namespace: `kubectl run -n egg-agents test --rm -it --image=busybox -- wget -qO- http://gateway.egg-system:9848/api/v1/health`

### Session expired before agent finished

The gateway auto-prunes sessions that have been idle (no requests from the container) for more than 60 minutes. If an agent is killed or its pipeline is cancelled, its session is cleaned up automatically within the next prune cycle rather than lingering until its 24-hour TTL.

If legitimate long-running agents are losing their sessions, tune the idle threshold on the gateway:

```yaml
# In your gateway Deployment
- name: EGG_SESSION_IDLE_TIMEOUT_MINUTES
  value: "120"  # default: 60, minimum: 5
- name: EGG_SESSION_CLEANUP_INTERVAL_MINUTES
  value: "15"   # default: 15, minimum: 1
```

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

### Orchestrator status: degraded (state-store wedge)

If `GET /api/v1/health` returns `"status": "degraded"` with one or more `components.state_store[<repo>]["error"]` values mentioning `"is already used by worktree"`, the state branch for that repo is pinned by a stale git admin dir — typically left behind after a state-volume reset or a deployment that changed the worktree path. In multi-repo deployments, every wedged repo appears in the per-repo map at once; `components.state_store_summary` lists them as `"N/M repos wedged: <paths>"`.

Because the orchestrator's background state-store probe runs the curative self-heal every 15s (see [Orchestrator health](#orchestrator-health)), the recovery attempt happens continuously without operator action. Wait 30–60 seconds and re-check; the orchestrator should recover on its own. If `degraded` persists, the self-heal could not match the stale entry — typically because the path embedded in the git error is still on disk (so `_add_worktree_with_branch_recovery` refuses to touch it, treating it as a live worktree) or because no admin dir under `<repo>/.git/worktrees/` references that path. Check orchestrator logs for the underlying error:

```bash
kubectl logs -n egg-system deploy/orchestrator | grep "State store"
```

When the self-heal cannot match, the stale admin dir must be removed manually from the volume holding the source repo at `EGG_REPO_PATH` (mounted at `/home/egg/repos` in the local overlay; production deployments should substitute their own `EGG_REPO_PATH`). The state volume at `/home/egg/.egg-state` only holds the worktree itself (e.g. `pipeline-worktree`, or `pipeline-worktree-<repo>` in multi-repo setups) — admin dirs live in the source repo's `.git/worktrees/`. The wedged path is the key of the entry in `components.state_store` (each `state_store[<repo>]["error"]` value contains the underlying git error); resolve it to the matching admin dir under `<repo>/.git/worktrees/<name>/` (the `gitdir` file inside each admin dir points to the worktree it was created for) and `rm -rf` that single admin dir. Repeat for every wedged repo — the probe heals each repo independently, so removing the admin dir for one will not heal the others. After removal, the next BG probe tick will succeed at `git worktree add` and the orchestrator returns to `healthy` without further action. Rolling the pod is not required and will not help on its own — without removing the admin dir, the wedge reproduces immediately on restart:

```bash
# After locating the matching admin dir under EGG_REPO_PATH:
kubectl exec -n egg-system deploy/orchestrator -- rm -rf /home/egg/repos/<repo>/.git/worktrees/<stale-entry>
```

## Security Considerations

### Credentials

- Never commit `.env` or `secrets.env` to version control
- Use GitHub App authentication for production
- Rotate `launcher-secret` and `lifecycle-secret` periodically

### Network

- The gateway is the only component with external network access
- In private mode, only api.anthropic.com is accessible
- All outbound traffic from sandbox routes through gateway proxy

### Pod Security

- Agent pods run as non-root user matching host UID
- Git metadata is shadowed (emptyDir with `medium: Memory` on .git/)
- No credentials are passed to agent pod environment; `EGG_LIFECYCLE_SECRET` is explicitly blocked from agent pods
- NetworkPolicies enforce egress-only-to-gateway isolation
- RBAC restricts orchestrator to Job/Pod management in `egg-agents` namespace only
