# Implementation Plan: GitHub Actions Support

**ADR:** [GitHub Actions Support](../adr/not-implemented/ADR-GitHub-Actions-Support.md)
**Issue:** #78
**ADR PR:** #98

> **Dependency note:** The ADR (PR #98) is still under review and may
> change before merging. This plan should be re-validated against the
> final ADR before implementation begins. If the ADR's design changes
> materially, sections of this plan may need revision.

## Overview

This plan covers the Phase 1 (MVP) implementation of running egg as a
GitHub Action. The ADR proposes a composite GitHub Action that
orchestrates the full egg stack (gateway + sandbox) within a GHA runner,
accepting the checked-out repo as the working repository.

## Codebase Observations

Findings from reviewing the existing codebase that affect implementation
decisions.

### 1. Port Discrepancy

The gateway `Dockerfile` EXPOSE says 9847/3128, but the actual ports are
**9848** (API, per `sandbox/egg_lib/config.py:53` and
`gateway/entrypoint.sh:133`) and **3129** (proxy, per
`sandbox/egg_lib/config.py:54`). The ADR references 9847 in one place —
the implementation must use the correct ports (9848/3129). The Dockerfile
EXPOSE should be fixed as part of this work.

### 2. Session Creation Is Atomic

`POST /api/v1/sessions/create` takes
`{container_id, container_ip, mode, repos, uid, gid}` and atomically
creates session + worktrees + filters by visibility
(`sandbox/egg_lib/gateway.py:449-508`). The GHA entrypoint must replicate
this flow via `curl` rather than the existing Python client.

### 3. Gateway Config Requirements

The gateway's config layer expects a full `repositories.yaml` including
`writable_repos`, `bot_username`, `repo_settings`, `user_mode` (parsed by
`config/repo_config.py`), and `local_repos.paths` (parsed by
`shared/egg_config/config.py`). See `config/repositories.yaml.example`
for the complete schema. The config generator must produce all required
fields — a minimal config will cause parse failures.

Note: `gateway/repo_parser.py` is a URL/path parsing utility for
extracting owner/repo from GitHub URLs — it does not parse the config
file.

### 4. Credential File Layout

The gateway reads Anthropic credentials from a `secrets.env` file mounted
at `~/.config/egg/secrets.env` (`gateway/anthropic_credentials.py:31-33`).
Launcher auth uses a separate `launcher-secret` file
(`gateway/entrypoint.sh:47-53`). The GHA entrypoint must create both files
and mount them into the gateway container.

### 5. Build Context

Both Dockerfiles use the **repo root** as build context:
- Gateway: `docker build -f gateway/Dockerfile .`
- Sandbox: `docker build -f sandbox/Dockerfile .`
  (sandbox Dockerfile copies the entire repo into `/opt/egg-runtime/` and
  sets `PYTHONPATH` to reference `sandbox/` and `shared/` subdirectories)

### 6. Dual-Network Architecture

Locally, the gateway is dual-homed on `egg-isolated` (172.32.0.0/24) and
`egg-external` (172.33.0.0/24). The GHA entrypoint must replicate this
for the full security model — private sandbox containers route through the
proxy on the isolated network, public containers use the external network
with direct internet access.

### 7. No Gateway/Sandbox Code Changes Required

The ADR was designed to reuse existing infrastructure. After reviewing the
codebase, this holds:
- Session management API already supports external callers via
  `launcher_secret` auth
- Worktree creation works with any repo mounted at
  `/home/egg/repos/<name>`
- Anthropic credential injection reads from `secrets.env`, no changes
  needed
- Policy enforcement (branch ownership, merge blocking) works as-is
- Config parsing (`config/repo_config.py`, `shared/egg_config/config.py`)
  accepts the YAML format we generate

The only change to existing code is the cosmetic EXPOSE fix in
`gateway/Dockerfile`.

## Deliverables

### 1. `action/action.yml` — Composite Action Definition

Defines the GitHub Action interface with inputs, outputs, and a composite
`runs` block that invokes the entrypoint script.

**Inputs:**

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `prompt` | Yes | — | Task prompt for Claude Code |
| `anthropic-oauth-token` | Yes | — | Anthropic OAuth token |
| `github-token` | Yes | `${{ github.token }}` | GitHub token for git ops |
| `bot-github-token` | No | — | Bot GitHub App token |
| `bot-username` | No | `egg` | Bot GitHub username |
| `mode` | No | `auto` | `public`, `private`, or `auto` |
| `timeout` | No | `30` | Timeout in minutes |
| `model` | No | `opus` | Claude model to use |

**Outputs:**

| Output | Description |
|--------|-------------|
| `exit-code` | Sandbox container exit code |
| `pr-url` | URL of created PR, if any |
| `log-file` | Path to full Claude output log |

Inputs are passed to the entrypoint as environment variables following
GitHub's composite action convention (`INPUT_PROMPT`, etc.).

### 2. `action/entrypoint.sh` — Container Orchestration (~300 lines)

The most complex deliverable. Replicates the orchestration flow from
`sandbox/egg_lib/runtime.py` and `sandbox/egg_lib/gateway.py` in bash.

**Step-by-step flow:**

| Step | What | How | Existing reference |
|------|------|-----|-------------------|
| 1 | Pull images | `docker pull ghcr.io/jwbron/egg-gateway:<tag>` + sandbox | New |
| 2 | Create networks | Inspect existing Docker networks, allocate unused 172.x.0.0/24 subnets, create `egg-gha-isolated-$RUN_ID` and `egg-gha-external-$RUN_ID`. **Note:** The local dev setup uses hardcoded subnets (`172.32.0.0/24`, `172.33.0.0/24` per `config.py:59-65`), but GHA intentionally uses dynamic allocation to avoid collisions when multiple concurrent runs share a self-hosted runner (or when a runner also runs egg locally). | `config.py:59-65` |
| 3 | Detect mode | If `auto`: read `$GITHUB_EVENT_REPOSITORY_VISIBILITY` (set from `${{ github.event.repository.visibility }}` in `action.yml`) — `private`/`internal`→private, `public`→public. This avoids an extra API call since the visibility is already available in the workflow event context | ADR spec |
| 4 | Generate config | Call `generate-config.sh` to produce repositories.yaml, secrets.env, launcher-secret in temp dir | New (see deliverable #3) |
| 5 | Start gateway | `docker run -d` with mounts for config dir, workspace `.git`, worktrees dir, state dir, certs dir. Env vars: `GITHUB_USER_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`, `HOST_UID`, `HOST_GID`, `BOT_GITHUB_TOKEN` (if provided), gateway policy vars | `sandbox/egg_lib/gateway.py:808-831` |
| 6 | Health check | Poll `http://<gateway-container-ip>:9848/api/v1/health` via `curl` on Docker network (no port publishing needed) | `sandbox/egg_lib/gateway.py:681-754` |
| 7 | Allocate IP | `docker network inspect` to find assigned IPs, select next available in subnet | `sandbox/egg_lib/runtime.py:189-247` |
| 8 | Create session | `curl -X POST http://<gateway-ip>:9848/api/v1/sessions/create` with `Authorization: Bearer <launcher-secret>`. Body: `{container_id, container_ip, mode, repos, uid, gid}`. Parse response for `session_token` and `worktrees` | `sandbox/egg_lib/gateway.py:449-508` |
| 9 | Start sandbox | `docker run` with: worktree path from session response mounted at `/home/egg/repos/<repo>`, `.git` shadowed via bind-mount of `/dev/null` (prevents the sandbox from accessing the full repo history or manipulating the worktree's parent repo directly — the sandbox should only interact with its isolated worktree copy), `EGG_SESSION_TOKEN`, `ANTHROPIC_BASE_URL=http://egg-gateway:9848`, Claude Code in `--exec` mode with `--print --output-format=stream-json` | `sandbox/egg_lib/runtime.py:646-740` |
| 10 | Capture output | Tee sandbox container logs to file and `$GITHUB_STEP_SUMMARY`, extract PR URLs via regex for step outputs | New |
| 11 | Cleanup | `trap EXIT` handler: delete session via gateway API, stop+rm containers, remove networks | `sandbox/egg_lib/runtime.py:351-388` |

**Implementation details:**

- **Container naming:** Include `$GITHUB_RUN_ID` to avoid conflicts on
  self-hosted runners: `egg-gha-gateway-${GITHUB_RUN_ID}`,
  `egg-gha-sandbox-${GITHUB_RUN_ID}`
- **Network naming:** `egg-gha-isolated-${GITHUB_RUN_ID}` and
  `egg-gha-external-${GITHUB_RUN_ID}`
- **No port publishing:** Containers communicate over Docker network.
  Health checks use container IP on the network or `docker exec`
- **Timeout:** Run the sandbox container with
  `docker run --stop-timeout 30 ...` and use a background timer that
  calls `docker stop` after `$TIMEOUT_MINUTES`. This ensures Docker
  sends SIGTERM to PID 1 inside the container and waits for graceful
  shutdown, rather than `timeout(1)` sending SIGTERM to the `docker`
  client process which may not propagate cleanly to the workload
- **Gateway dual-homing:** Start on isolated network, then
  `docker network connect` to external network (matching
  `sandbox/egg_lib/gateway.py:839-853`)
- **Exit code propagation:** Capture sandbox exit code and write to
  `$GITHUB_OUTPUT`

### 3. `action/generate-config.sh` — Config Generation (~100 lines)

Creates a temp directory (`$RUNNER_TEMP/egg-config-$RUN_ID/`) containing
the three files the gateway needs.

**`repositories.yaml`** (full format for `config/repo_config.py`):
```yaml
github_username: <$GITHUB_ACTOR>
bot_username: <bot-username input, default "egg">
writable_repos:
  - <$GITHUB_REPOSITORY>
repo_settings:
  <$GITHUB_REPOSITORY>:
    auth_mode: <"bot" if bot-github-token provided, else "user">
user_mode:
  github_user: <$GITHUB_ACTOR>
  git_name: <$GITHUB_ACTOR>
  git_email: <$GITHUB_ACTOR_ID>+<$GITHUB_ACTOR>@users.noreply.github.com
local_repos:
  paths:
    - /home/egg/repos/<repo-name>
```

**`secrets.env`:**
```
CLAUDE_CODE_OAUTH_TOKEN=<anthropic-oauth-token input>
```

**`launcher-secret`:** Generated via `openssl rand -base64 32`.

**Workspace path mapping:** `$GITHUB_WORKSPACE` is bind-mounted into the
gateway container at `/home/egg/repos/<repo-name>` where `<repo-name>` is
derived from `$GITHUB_REPOSITORY` (strips owner prefix). The gateway's
worktree manager uses the `.git` directory at that path to create
worktrees at `~/.egg-worktrees/<container-id>/<repo-name>`.

### 4. `.github/workflows/release-images.yml` — GHCR Publishing

Builds and pushes gateway and sandbox Docker images to GHCR on release
events.

**Triggers:** `release: [published]` + `workflow_dispatch` for manual
testing.

**Images:**
- `ghcr.io/jwbron/egg-gateway:latest` and `:$TAG`
- `ghcr.io/jwbron/egg-sandbox:latest` and `:$TAG`

**Build details:**
- Uses `docker/build-push-action@v6` with repo root as context
- Gateway: `-f gateway/Dockerfile`
- Sandbox: `-f sandbox/Dockerfile`
- amd64 only for Phase 1 (matches `ubuntu-latest` runners)
- Requires `packages: write` permission

### 5. `.github/workflows/test-action.yml` — Integration Tests

Runs the action on PRs that modify `action/**` files.

**Test strategy:**
- Minimal test: verify config generation, container startup, gateway
  health (no API key required)
- Full test: run with a simple prompt and verify exit code (requires
  `ANTHROPIC_OAUTH_TOKEN` secret)
- Validate outputs: exit-code is set, log-file exists

### 6. Gateway Dockerfile EXPOSE Fix

Change `EXPOSE 9847 3128` to `EXPOSE 9848 3129` in `gateway/Dockerfile`
to match actual ports.

## Implementation Sequence

| Order | Deliverable | Depends on | Notes |
|-------|------------|------------|-------|
| 1 | Gateway Dockerfile EXPOSE fix | — | One-line change |
| 2 | `release-images.yml` | — | Must publish images before action can pull them |
| 3 | `action/generate-config.sh` | — | Self-contained, testable in isolation |
| 4 | `action/entrypoint.sh` | #2, #3 | Core logic, largest piece |
| 5 | `action/action.yml` | #4 | Thin wrapper around entrypoint |
| 6 | `test-action.yml` | #5 | Integration verification |

## Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| **Sandbox image size (2-3GB)** causes slow pulls | Cold start adds 30-60s | GitHub CDN + `actions/cache` for Docker layers (Phase 2) |
| **Gateway health check timing** | Action may timeout waiting for startup | Aggressive polling (0.5s interval), 60s max wait before failing |
| **`GITHUB_TOKEN` permission scope** insufficient | Push/PR operations fail | Document required permissions: `contents: write`, `pull-requests: write`. Recommend PAT for full functionality |
| **Self-hosted runner conflicts** | Container/network name collisions with concurrent runs | Include `$GITHUB_RUN_ID` in all Docker resource names |
| **Workspace `.git` ownership mismatch** | Gateway can't read `.git` directory | Use existing `HOST_UID`/`HOST_GID` mechanism in gateway entrypoint to match runner user |
| **Session API calls via curl** harder to debug than Python | Opaque failures | Verbose logging with `set -x` in debug mode, clear error messages on each API call failure |
| **ARM64 runners** not supported | Action fails on ARM self-hosted runners | Phase 1: amd64 only (matches `ubuntu-latest`). Document limitation. |
| **Network subnet collisions** | Docker network creation fails | Dynamic subnet allocation: inspect existing networks and pick unused range |

## Open Items to Resolve During Implementation

1. **Claude Code `--exec` invocation:** Verify exact CLI flags for
   non-interactive mode. ADR specifies `--print --output-format=stream-json`
   — validate against installed Claude Code version in sandbox image.

2. **PR URL extraction from output:** Determine reliable pattern to
   extract PR URLs from Claude Code's stream-json output for the `pr-url`
   action output. May need to parse JSON lines for specific event types.

3. **`$GITHUB_WORKSPACE` ownership:** The checkout action creates the repo
   as the runner user (typically uid 1001). Verify the gateway's
   `HOST_UID`/`HOST_GID` + `gosu` mechanism handles this correctly when
   creating worktrees from the mounted `.git`.

4. **Sandbox Dockerfile build context:** Confirm that
   `sandbox/Dockerfile` builds correctly with repo root as context
   (the `COPY . /opt/egg-runtime/` line copies the entire repo).
   A `.dockerignore` to exclude unnecessary files (docs, tests,
   `.github/`) would reduce image size but is a change to the existing
   build — this should be handled in a separate PR.

5. **Image tagging strategy:** `action/action.yml` should reference a
   pinned version tag (e.g., `:v1.0.0`) rather than `:latest`. Using
   `latest` in a GitHub Action is unreliable — users who pin `@v1` of
   the action would still get unpredictable image versions. The release
   workflow should tag images with the release version, and the
   `action.yml` should be updated as part of each release to reference
   the corresponding image tag.
